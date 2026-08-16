"""
Modelos ``ExportJob`` / ``ExportJobItem`` / ``ExportArtifact`` — exportación de una base
de datos a un artefacto descargable (estructura, datos o ambos).

Un ``ExportJob`` describe una operación **asíncrona y de SOLO LECTURA** sobre el origen:
el admin crea el plan (servidor + BD + ``ExportSpec``), inspecciona el catálogo, resuelve
la selección, previsualiza y confirma, y un worker en segundo plano genera el artefacto.
Cualquier sentencia destructiva existe únicamente como TEXTO dentro del artefacto: el
gateway nunca la ejecuta.

Identidad de la BD: SIEMPRE física (``server_id`` + ``database_name``, NOT NULL) y,
ADEMÁS, ``database_id`` (``managed_database_id``) si está en el inventario (``NULL`` si es
cruda) — mismo criterio "por identidad" que ``CloneJob``/``CollationConversionJob``, y por
eso la exportación funciona igual sobre una BD adoptada que sobre una que nunca se
registró.

Tres piezas y por qué son tres tablas:

- ``export_jobs`` es la CABECERA: el ``ExportSpec`` completo (§4 del diseño, JSON) para que
  el job sea reproducible por sí solo, la selección ya congelada en el preview (§5.2), el
  fingerprint anti-TOCTOU y el estado del pipeline.
- ``export_job_items`` es el REPORTE DE INCIDENCIAS por objeto (§14): qué se exportó, qué
  se omitió y por qué. Es lo que permite auditar una exportación **sin abrir el archivo**.
- ``export_artifacts`` es el objeto SENSIBLE EN REPOSO (§9.3): nombre de almacenamiento
  opaco, checksum, tamaño y ciclo de vida (``available`` → ``consumed``/``purged``). Va
  aparte del job a propósito: el job es un registro histórico que se conserva, el artefacto
  es efímero y se borra por TTL. Fundirlos obligaría a borrar el rastro de auditoría junto
  con el archivo.

Nota de fase: F2 crea el esquema y lo llena hasta el ``preview``. La ejecución, el writer
y la entrega (que son quienes escriben ``export_artifacts`` y la mayoría de los ítems)
llegan en F3/F4.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---- Valores permitidos (strings planos, NO ENUM nativo de SQL) ---------------------- #
# Mismo enfoque deliberado que ``CloneJob``/``CollationConversionJob``: un tipo ENUM en el
# motor obliga a una migración de esquema para agregar un valor, y Alembic lo detecta como
# drift en cuanto los valores del código y los del motor divergen. Con String + constantes,
# agregar un estado es un despliegue de código.

# ExportJob.status — MISMO vocabulario que el clon y la conversión de collation: un
# frontend que ya hace polling de esos jobs no tiene que aprender un tercer juego.
EXPORT_STATUS_PENDING = "pending"          # plan creado, aún no ejecutado
EXPORT_STATUS_RUNNING = "running"          # worker generando el artefacto
EXPORT_STATUS_SUCCEEDED = "succeeded"      # terminó sin fallos
EXPORT_STATUS_FAILED = "failed"            # terminó con al menos un fallo bloqueante
EXPORT_STATUS_INTERRUPTED = "interrupted"  # el proceso murió a mitad (barrido de lifespan)
EXPORT_STATUS_CANCELED = "canceled"        # cancelado cooperativamente

# ExportJob.phase — las fases del orden de emisión del §8.4, agrupadas.
EXPORT_PHASE_PREAMBLE = "preamble"
EXPORT_PHASE_SCOPE = "scope"
EXPORT_PHASE_PREREQUISITES = "prerequisites"  # extensiones, tipos, secuencias
EXPORT_PHASE_STRUCTURE = "structure"
EXPORT_PHASE_DATA = "data"
EXPORT_PHASE_CONSTRAINTS = "constraints"
EXPORT_PHASE_BODIES = "bodies"
EXPORT_PHASE_EPILOGUE = "epilogue"
EXPORT_PHASE_DONE = "done"

# ExportJobItem.status — se adopta el vocabulario de collation-conversion
# (``ok``/``error``/``skipped``) y NO el del clon (``applied``/``failed``/``skipped``):
# acá no se "aplica" nada, se LEE. Queda anotado que las dos familias divergen y que eso
# es deuda: un quinto módulo debería ser el disparador de unificarlas (§12 del diseño).
EXPORT_ITEM_PENDING = "pending"
EXPORT_ITEM_OK = "ok"
EXPORT_ITEM_ERROR = "error"
EXPORT_ITEM_SKIPPED = "skipped"

# ExportArtifact.state
EXPORT_ARTIFACT_AVAILABLE = "available"  # en disco, descargable
EXPORT_ARTIFACT_CONSUMED = "consumed"    # descargado y borrado (descarga de un solo uso)
EXPORT_ARTIFACT_PURGED = "purged"        # borrado por TTL o por barrido de huérfanos

# El ``ExportSpec`` y la selección resuelta son JSON que puede crecer con el catálogo (una
# BD con miles de tablas): LONGTEXT en MySQL/MariaDB, TEXT en PostgreSQL/SQLite. Mismo
# criterio que ``CloneJobItem.sql``.
_JSON_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class ExportJob(Base, TimestampMixin):
    __tablename__ = "export_jobs"
    __table_args__ = (
        {"comment": "Cabecera + estado de una exportación de BD a un artefacto"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del job de exportación"
    )

    # ---- Identidad física de la BD a exportar ----------------------------------- #
    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor donde vive la BD a exportar",
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
        String(20),
        nullable=False,
        comment="Motor del origen ('mysql'|'mariadb'|'postgresql')",
    )

    # ---- Petición y selección congelada ----------------------------------------- #
    spec: Mapped[str] = mapped_column(
        _JSON_TEXT,
        nullable=False,
        comment=(
            "ExportSpec COMPLETO en JSON (§4). Autosuficiente: basta por sí solo para "
            "reproducir el mismo artefacto, sin depender de defaults del código"
        ),
    )
    resolved_selection: Mapped[str | None] = mapped_column(
        _JSON_TEXT,
        nullable=True,
        comment=(
            "JSON de la selección ya RESUELTA a objetos explícitos; se congela en el "
            "preview (§5.2). NULL = todavía sin previsualizar"
        ),
    )

    # ---- Anti-TOCTOU / TTL ------------------------------------------------------ #
    source_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA256 del snapshot normalizado del origen (anti-TOCTOU)",
    )
    confirm_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Hash del PLAN RESUELTO del último preview; execute exige que coincida",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="TTL del plan: tras expirar, preview/execute exigen replanear (410)",
    )

    # ---- Estado de ejecución ---------------------------------------------------- #
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EXPORT_STATUS_PENDING,
        server_default=EXPORT_STATUS_PENDING,
        index=True,
        comment="pending | running | succeeded | failed | interrupted | canceled",
    )
    phase: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        comment="Fase actual del orden de emisión (§8.4): structure | data | bodies | done",
    )
    progress: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON de progreso (objetos/filas/bytes emitidos)",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error bloqueante (motivo acotado, NUNCA str(exc) del motor) si failed",
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Flag cooperativo: el worker corta en el próximo punto seguro",
    )
    structure_drift_detected: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment=(
            "El catálogo del origen cambió DURANTE la corrida. No invalida el artefacto "
            "(los datos siguen siendo consistentes) pero el operador debe enterarse: en "
            "MySQL/MariaDB el snapshot MVCC no cubre el diccionario de datos (§6.2)"
        ),
    )

    # ---- Quién y con qué clave de reintento ------------------------------------- #
    created_by_admin_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment=(
            "Admin que creó el plan. Sin FK a propósito (mismo criterio que "
            "audit_log.admin_id): borrar un admin no debe borrar ni mutilar el rastro de "
            "quién exportó. Hoy hay un solo admin, pero el chequeo 'quien descarga es "
            "quien exportó' (§9.3) se escribe igual para que el día que haya "
            "multiusuario no sea un agujero"
        ),
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
        comment=(
            "Clave de reintento del cliente. UNIQUE: un reintento (timeout de red, doble "
            "click) devuelve el MISMO plan en vez de disparar una segunda exportación "
            "—que es cara para el servidor de origen—. NULL = sin idempotencia"
        ),
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que el worker empezó a generar"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que el worker terminó"
    )

    def __repr__(self) -> str:
        return (
            f"<ExportJob(id={self.id}, {self.server_id}/{self.database_name}, "
            f"status={self.status})>"
        )


class ExportJobItem(Base, TimestampMixin):
    __tablename__ = "export_job_items"
    __table_args__ = (
        {"comment": "Resultado por objeto de una exportación (reporte de incidencias)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del ítem"
    )

    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("export_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job al que pertenece este ítem",
    )

    seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Orden GLOBAL de emisión del objeto en el artefacto (§8.4)",
    )

    object_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="table | view | materialized_view | routine | trigger | event | sequence | ...",
    )

    object_name: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="Nombre del objeto tal como lo da el catálogo"
    )

    phase: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="Fase del orden de emisión en la que salió"
    )

    status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="pending | ok | error | skipped (NULL = aún no procesado)",
    )

    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Motivo ACOTADO de un skip/error (sin permiso, tipo no soportado, definición "
            "ilegible, tipo de valor no soportado). NUNCA str(exc) del motor: el mensaje "
            "de un driver puede incrustar VALORES de filas (criterio R4, §9.5)"
        ),
    )

    rows_exported: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Filas emitidas (solo objetos con datos)"
    )

    bytes_written: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Bytes que este objeto aportó al artefacto"
    )

    deterministic: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment=(
            "False = las filas salieron SIN orden garantizado (tabla sin PK y sin tupla "
            "de columnas ordenable). Es la degradación honesta del §8.3: fingir "
            "determinismo ahí sería mentir sobre la comparabilidad de dos volcados"
        ),
    )

    execution_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Duración de la emisión de este objeto (ms)"
    )

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que se emitió el objeto"
    )

    def __repr__(self) -> str:
        return (
            f"<ExportJobItem(id={self.id}, job={self.job_id}, seq={self.seq}, "
            f"{self.object_type}:{self.object_name})>"
        )


class ExportArtifact(Base, TimestampMixin):
    __tablename__ = "export_artifacts"
    __table_args__ = (
        {"comment": "Artefacto generado por una exportación (efímero, con TTL)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del artefacto"
    )

    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("export_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="Job que lo generó (uno por job)",
    )

    storage_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment=(
            "Nombre OPACO del archivo en el directorio de spool "
            "(secrets.token_urlsafe). El cliente solo maneja el id del job: nunca envía "
            "ni recibe una ruta, que es lo que neutraliza el recorrido de directorios"
        ),
    )

    byte_size: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Tamaño en bytes del artefacto entregado"
    )

    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Checksum del artefacto: verificación de integridad sin abrir el archivo",
    )

    content_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="MIME del artefacto (text/plain, application/gzip, application/zip)",
    )

    part_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Cantidad de partes (>1 con split_max_bytes / organization=per_object)",
    )

    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EXPORT_ARTIFACT_AVAILABLE,
        server_default=EXPORT_ARTIFACT_AVAILABLE,
        index=True,
        comment="available | consumed (descarga de un solo uso) | purged (TTL/huérfano)",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
        comment=(
            "Momento de purga. Indexado porque la tarea periódica del lifespan barre por "
            "esta columna: sin índice, la purga escanea la tabla entera cada pasada"
        ),
    )

    downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento de la última descarga"
    )

    download_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Cuántas veces se descargó (con un solo uso, a lo sumo 1)",
    )

    def __repr__(self) -> str:
        return (
            f"<ExportArtifact(id={self.id}, job={self.job_id}, state={self.state}, "
            f"bytes={self.byte_size})>"
        )
