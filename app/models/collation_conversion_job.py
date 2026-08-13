"""
Modelos ``CollationConversionJob`` / ``CollationConversionJobItem`` — conversión del
charset/collation de una base de datos COMPLETA (MySQL/MariaDB).

PROBLEMA QUE RESUELVE (el bug que HeidiSQL tiene): cambiar el charset/collation de una BD
y de sus tablas NO alcanza. Los parámetros ``VARCHAR``/``CHAR`` de una PROCEDURE/FUNCTION,
las variables ``DECLARE`` de un TRIGGER/EVENT y los literales de texto del cuerpo de una
VIEW heredan la collation de la SESIÓN/BD **del momento en que el objeto fue creado** y
quedan congelados ahí. Si después se cambia la collation de la BD/tablas sin recrear esos
objetos, en producción aparecen ``Illegal mix of collations``. La ÚNICA forma de arreglar
un objeto ya creado es ``DROP`` + ``CREATE`` con el MISMO cuerpo (no existe un
``ALTER PROCEDURE``/``ALTER TRIGGER`` que cambie el cuerpo ni la collation de sus
parámetros).

Un ``CollationConversionJob`` describe la operación **asíncrona** completa: el usuario crea
el plan (servidor + BD + charset/collation objetivo), inspecciona el inventario, selecciona
qué tablas y qué objetos convertir, previsualiza y confirma, y un worker en segundo plano
ejecuta ``ALTER DATABASE`` → ``ALTER TABLE ... CONVERT TO`` → ``DROP``+``CREATE`` de cada
objeto, actualizando ``status``/``phase`` para el polling del frontend. Es asíncrono porque
``ALTER TABLE ... CONVERT TO CHARACTER SET`` REESCRIBE la tabla completa y puede tardar
minutos u horas en tablas grandes: no cabe en una llamada HTTP síncrona.

Identidad de la BD: SIEMPRE física (``server_id`` + ``database_name``, NOT NULL) y, ADEMÁS,
``database_id`` (``managed_database_id``) si está en el inventario (``NULL`` si es cruda) —
mismo criterio "por identidad" que ``CloneJob``/``SchemaComparison``.

``mode`` separa DOS operaciones que comparten recurso, flujo y estado pero NO comparten
semántica de motor:

- ``universal`` (MySQL/MariaDB): BD + tablas + los 5 tipos de objeto con collation congelada.
- ``columns`` (PostgreSQL): SOLO ``ALTER TABLE ... ALTER COLUMN ... TYPE ... COLLATE ...``.
  PostgreSQL no tiene el problema de la collation congelada (la resuelve dinámicamente en
  cada llamada contra el tipo real de la columna), así que NO hay fase de objetos; y el
  ``ENCODING``/``LC_COLLATE`` de una BD es INMUTABLE tras el ``CREATE DATABASE``, así que
  tampoco hay fase de ``ALTER DATABASE``. La única unidad de cambio es la COLUMNA (la
  selección sigue siendo por TABLA y se traduce a las columnas de texto de esa tabla).

El motor del servidor determina el modo: el usuario no lo elige (ver
``CollationConversionController._mode_for_dialect``).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---- Valores permitidos (strings, no enums nativos: mismo enfoque que CloneJob.status;
# evita tipos ENUM en Alembic y el churn de migración al agregar un valor) -------------- #
# CollationConversionJob.status — MISMO vocabulario que CloneJob (un frontend que ya hace
# polling de clones no tiene que aprender otro juego de estados).
COLLATION_STATUS_PENDING = "pending"          # plan creado, aún no ejecutado
COLLATION_STATUS_RUNNING = "running"          # worker ejecutando
COLLATION_STATUS_SUCCEEDED = "succeeded"      # terminó sin fallos
COLLATION_STATUS_FAILED = "failed"            # terminó con al menos un paso fallido
COLLATION_STATUS_INTERRUPTED = "interrupted"  # el proceso murió a mitad (barrido de lifespan)
COLLATION_STATUS_CANCELED = "canceled"        # cancelado cooperativamente

# CollationConversionJob.mode
COLLATION_MODE_UNIVERSAL = "universal"  # MySQL/MariaDB: BD + tablas + los 5 tipos de objeto
# PostgreSQL: SOLO columnas. No hay paso de BD (``ENCODING``/``LC_COLLATE`` son inmutables
# tras el ``CREATE DATABASE``) ni paso de objetos (PostgreSQL resuelve la collation
# dinámicamente en cada llamada: una función/vista no congela nada al crearse). La única
# unidad de cambio posible es ``ALTER TABLE ... ALTER COLUMN ... TYPE ... COLLATE ...``.
COLLATION_MODE_COLUMNS = "columns"

# CollationConversionJob.phase
COLLATION_PHASE_DATABASE = "database"
COLLATION_PHASE_TABLES = "tables"
COLLATION_PHASE_OBJECTS = "objects"
COLLATION_PHASE_DONE = "done"

# CollationConversionJobItem.object_type
COLLATION_OBJ_DATABASE = "database"
COLLATION_OBJ_TABLE = "table"
COLLATION_OBJ_PROCEDURE = "procedure"
COLLATION_OBJ_FUNCTION = "function"
COLLATION_OBJ_TRIGGER = "trigger"
COLLATION_OBJ_EVENT = "event"
COLLATION_OBJ_VIEW = "view"

# Los 5 tipos con collation CONGELADO (los que requieren DROP+CREATE verbatim).
COLLATION_FROZEN_OBJECT_TYPES: tuple[str, ...] = (
    COLLATION_OBJ_PROCEDURE,
    COLLATION_OBJ_FUNCTION,
    COLLATION_OBJ_TRIGGER,
    COLLATION_OBJ_EVENT,
    COLLATION_OBJ_VIEW,
)

# CollationConversionJobItem.status — 'skipped' cubre el paso que se decidió NO ejecutar
# (tabla ya en el objetivo, objeto desaparecido, grants ilegibles) sin marcarlo como error.
COLLATION_ITEM_PENDING = "pending"
COLLATION_ITEM_OK = "ok"
COLLATION_ITEM_ERROR = "error"
COLLATION_ITEM_SKIPPED = "skipped"

# El DDL capturado de una rutina/trigger puede ser grande: LONGTEXT en MySQL/MariaDB,
# TEXT en PostgreSQL/SQLite (mismo criterio que CloneJobItem.sql).
_SQL_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class CollationConversionJob(Base, TimestampMixin):
    __tablename__ = "collation_conversions"
    __table_args__ = (
        {"comment": "Cabecera + estado de una conversión de charset/collation de una BD"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del job de conversión"
    )

    # ---- Identidad física de la BD a convertir ---------------------------------- #
    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor donde vive la BD a convertir",
    )
    database_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Nombre de la BD en el motor"
    )
    database_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="managed_database_id si la BD está en el inventario; NULL si es cruda",
    )
    engine: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Motor ('mysql'|'mariadb'; 'postgresql' no aplica)"
    )

    # ---- Objetivo --------------------------------------------------------------- #
    mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=COLLATION_MODE_UNIVERSAL,
        server_default=COLLATION_MODE_UNIVERSAL,
        comment=(
            "universal (MySQL/MariaDB: BD + tablas + los 5 tipos de objeto) | "
            "columns (PostgreSQL: solo ALTER COLUMN ... COLLATE)"
        ),
    )
    # NULLABLE desde el modo ``columns``: PostgreSQL NO tiene charset por columna ni por
    # tabla, y el ``ENCODING`` de la BD es inmutable tras el ``CREATE DATABASE`` — no hay
    # "charset objetivo" que pedir. En el modo ``universal`` sigue siendo obligatorio (lo
    # exige el controller, no la BD).
    target_charset: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Charset objetivo (forma CANÓNICA del catálogo). NULL en el modo columns: "
            "PostgreSQL no tiene charset por columna."
        ),
    )
    target_collation: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Collation objetivo (forma CANÓNICA del catálogo)"
    )
    previous_db_charset: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Charset default que tenía la BD antes (auditoría)"
    )
    previous_db_collation: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Collation default que tenía la BD antes (auditoría)"
    )

    selection: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "JSON {tables: [...], objects: [{object_type, name}]} de la selección del "
            "preview; NULL = todavía sin previsualizar"
        ),
    )

    # ---- Anti-TOCTOU / TTL ------------------------------------------------------ #
    source_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA256 del inventario normalizado al planear (anti-TOCTOU)",
    )
    confirm_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Hash del plan del último preview; execute exige que coincida",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="TTL del plan: tras expirar, execute exige replanear (410)",
    )

    # ---- Estado de ejecución ---------------------------------------------------- #
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=COLLATION_STATUS_PENDING,
        server_default=COLLATION_STATUS_PENDING,
        index=True,
        comment="pending | running | succeeded | failed | interrupted | canceled",
    )
    phase: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="Fase actual: database | tables | objects | done"
    )
    progress: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON de progreso (conteos por fase)"
    )
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Error bloqueante (limpio, sin secretos) si status=failed"
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Flag cooperativo: el worker corta en el próximo punto seguro",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que el worker empezó a ejecutar"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que el worker terminó (éxito/fallo/cancel)"
    )

    def __repr__(self) -> str:
        return (
            f"<CollationConversionJob(id={self.id}, "
            f"{self.server_id}/{self.database_name} → {self.target_collation}, "
            f"status={self.status})>"
        )


class CollationConversionJobItem(Base, TimestampMixin):
    __tablename__ = "collation_conversion_items"
    __table_args__ = (
        {"comment": "Paso individual de una conversión de charset/collation (BD/tabla/objeto)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del paso"
    )

    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("collation_conversions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job al que pertenece este paso",
    )

    seq: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Orden GLOBAL de ejecución del paso"
    )

    object_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="database | table | procedure | function | trigger | event | view",
    )

    object_name: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="Nombre del objeto en el motor"
    )

    # Auditoría de qué tenía el objeto ANTES (tablas: su charset/collation previos;
    # objetos con cuerpo: la collation_connection congelada que arrastraban).
    previous_charset: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Charset que tenía el objeto antes del paso"
    )
    previous_collation: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Collation que tenía antes: TABLE_COLLATION en tablas, collation_connection "
            "congelada en los objetos con cuerpo"
        ),
    )

    # DDL capturado ANTES del DROP. Se persiste ANTES de ejecutar cualquier cosa: si el
    # CREATE fallara después de un DROP exitoso, el objeto se perdió del motor y esta
    # columna es la ÚNICA copia con la que el operador puede recrearlo a mano.
    captured_ddl: Mapped[str | None] = mapped_column(
        _SQL_TEXT,
        nullable=True,
        comment="DDL exacto capturado con SHOW CREATE antes del DROP (copia de recuperación)",
    )

    sql: Mapped[str | None] = mapped_column(
        _SQL_TEXT,
        nullable=True,
        comment="Sentencia principal del paso (ALTER DATABASE / ALTER TABLE / CREATE)",
    )

    status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="pending | ok | error | skipped (NULL = aún no ejecutado)",
    )

    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Error del motor si el paso falló (limpio, sin secretos)"
    )

    # Grants a nivel de RUTINA reaplicados tras el DROP+CREATE (solo procedure/function:
    # MySQL/MariaDB borran mysql.procs_priv al dropear la rutina).
    grants_captured: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Cuántos privilegios de rutina se leyeron antes del DROP"
    )
    grants_reapplied: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Cuántos privilegios de rutina se reaplicaron tras el CREATE"
    )
    grants_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Motivo si los privilegios de rutina no se pudieron leer o reaplicar",
    )

    # Modo ``columns`` (PostgreSQL): cuántas COLUMNAS cambió este paso. El paso sigue siendo
    # por TABLA porque todas sus columnas viajan en UN solo ``ALTER TABLE`` (una sola pasada
    # sobre la tabla y un solo ACCESS EXCLUSIVE lock, en vez de uno por columna), así que el
    # conteo es la única forma de saber cuántas cambiaron sin releer el ``sql``.
    columns_affected: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Columnas cambiadas por el paso (modo columns); NULL en el modo universal",
    )

    execution_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Duración del paso en milisegundos"
    )

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento de ejecución del paso"
    )

    def __repr__(self) -> str:
        return (
            f"<CollationConversionJobItem(id={self.id}, job={self.job_id}, "
            f"seq={self.seq}, {self.object_type}:{self.object_name})>"
        )
