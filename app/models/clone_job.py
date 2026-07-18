"""
Modelos CloneJob / CloneJobItem — clonado de una base de datos hacia un servidor
destino (mismo servidor u otro, mismo motor o distinto).

Un ``CloneJob`` describe una operación de clonación **asíncrona**: el usuario arma el
plan (origen, destino, selección de objetos, opciones), lo previsualiza y confirma, y
un worker en segundo plano ejecuta el pipeline (limpiar → estructura → datos → adopt),
actualizando ``status``/``phase``/``progress`` para que el frontend haga polling.

Como en ``SchemaComparison``, la identidad de cada lado es SIEMPRE física
(``*_server_id`` + ``*_database_name``, NOT NULL) y, ADEMÁS, ``*_database_id``
(``managed_database_id``) si esa BD está en el inventario (``NULL`` si es cruda). El
origen y el destino pueden ser BDs no adoptadas; el destino puede no existir todavía
(``target_mode='new'``).

``source_fingerprint`` (hash del snapshot normalizado del origen al planear) habilita
el chequeo anti-TOCTOU antes de ejecutar; ``expires_at`` es el TTL del plan. Las
sentencias/objetos y su resultado por paso viven en ``CloneJobItem``.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---- Valores permitidos (strings, no enums nativos: mismo enfoque que
# SchemaComparisonItem.change_type/execution_status; evita tipos ENUM en Alembic) ---- #
# CloneJob.status
CLONE_STATUS_PENDING = "pending"        # plan creado, aún no ejecutado
CLONE_STATUS_RUNNING = "running"        # worker ejecutando
CLONE_STATUS_SUCCEEDED = "succeeded"    # terminó sin fallos
CLONE_STATUS_FAILED = "failed"          # terminó con al menos un fallo bloqueante
CLONE_STATUS_INTERRUPTED = "interrupted"  # el proceso murió a mitad (barrido de lifespan)
CLONE_STATUS_CANCELED = "canceled"      # cancelado cooperativamente

# CloneJob.clean_mode
CLONE_CLEAN_NONE = "none"                # preservar lo que tenga el destino
CLONE_CLEAN_OBJECTS = "objects"          # borrar objeto por objeto (preserva la BD y su config)
CLONE_CLEAN_DROP_DATABASE = "drop_database"  # reset total: DROP DATABASE + recrear

# CloneJob.target_mode
CLONE_TARGET_NEW = "new"                 # crear una BD nueva
CLONE_TARGET_EXISTING = "existing"       # usar una BD existente

# CloneJobItem.kind
CLONE_ITEM_CLEAN = "clean"
CLONE_ITEM_STRUCTURE = "structure"
CLONE_ITEM_DATA = "data"
CLONE_ITEM_ADOPT = "adopt"

# CloneJobItem.status
CLONE_ITEM_PENDING = "pending"
CLONE_ITEM_APPLIED = "applied"
CLONE_ITEM_FAILED = "failed"
CLONE_ITEM_SKIPPED = "skipped"

# El DDL renderizado puede ser grande; LONGTEXT en MySQL/MariaDB, TEXT en PG/SQLite.
_SQL_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class CloneJob(Base, TimestampMixin):
    __tablename__ = "clone_jobs"
    __table_args__ = (
        {"comment": "Cabecera + estado de una operación de clonación de BD"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del job de clonación"
    )

    # ---- Origen ----------------------------------------------------------------- #
    source_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor del origen (siempre poblado)",
    )
    source_database_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Nombre de la BD origen en el motor"
    )
    source_database_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="managed_database_id del origen si está en inventario; NULL si es cruda",
    )
    source_engine: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Motor del origen ('mysql'|'mariadb'|'postgresql')"
    )

    # ---- Destino ---------------------------------------------------------------- #
    target_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor del destino (siempre poblado)",
    )
    target_database_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Nombre de la BD destino en el motor"
    )
    target_database_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="managed_database_id del destino si está en inventario; NULL si es cruda/nueva",
    )
    target_engine: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Motor del destino (el DDL se renderiza para este dialecto)"
    )

    # ---- Opciones del plan ------------------------------------------------------ #
    include_data: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
        comment="True = clonar estructura + datos; False = solo estructura",
    )
    clean_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CLONE_CLEAN_NONE, server_default=CLONE_CLEAN_NONE,
        comment="none | objects (borra objeto por objeto) | drop_database (reset total)",
    )
    target_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="new (crear BD) | existing (BD ya existente)",
    )
    adopt_target: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
        comment="True = adoptar el destino y asignarle el blueprint del origen (solo clon completo)",
    )
    adopt_owner_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("server_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Owner (ServerUser del servidor destino) para el registro al adoptar; requerido si adopt_target",
    )
    selection: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON de la selección de objetos (cierre resuelto); NULL = clon completo",
    )

    # ---- Anti-TOCTOU / TTL ------------------------------------------------------ #
    source_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA256 del snapshot normalizado del origen al planear (anti-TOCTOU)",
    )
    confirm_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Token del último preview; execute exige que coincida",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="TTL del plan: tras expirar, execute exige replanear (410)",
    )

    # ---- Estado de ejecución ---------------------------------------------------- #
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CLONE_STATUS_PENDING, server_default=CLONE_STATUS_PENDING,
        index=True,
        comment="pending | running | succeeded | failed | interrupted | canceled",
    )
    phase: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="Fase actual: clean | structure | data | adopt | done",
    )
    progress: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON de progreso (conteos por tabla/fase)",
    )
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Error bloqueante (limpio, sin secretos) si status=failed",
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
        comment="Flag cooperativo: el worker corta en el próximo punto seguro",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que el worker empezó a ejecutar",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento en que el worker terminó (éxito/fallo/cancel)",
    )

    def __repr__(self) -> str:
        return (
            f"<CloneJob(id={self.id}, "
            f"src={self.source_server_id}/{self.source_database_name}, "
            f"dst={self.target_server_id}/{self.target_database_name}, status={self.status})>"
        )


class CloneJobItem(Base, TimestampMixin):
    __tablename__ = "clone_job_items"
    __table_args__ = (
        {"comment": "Paso individual de un job de clonación (limpieza/estructura/datos/adopt)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del paso"
    )

    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("clone_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job al que pertenece este paso",
    )

    seq: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Orden GLOBAL de aplicación del paso",
    )

    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="clean | structure | data | adopt",
    )

    object_type: Mapped[str] = mapped_column(
        String(40), nullable=False,
        comment="table|view|routine|trigger|column|index|... o 'database' para clean total",
    )

    object_name: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="Nombre del objeto (cualificado donde aplica)",
    )

    sql: Mapped[str | None] = mapped_column(
        _SQL_TEXT, nullable=True,
        comment="Sentencia DDL renderizada (estructura/clean); NULL para pasos de datos",
    )

    status: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
        comment="pending | applied | failed | skipped (NULL = aún no ejecutado)",
    )

    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Error del motor si el paso falló (limpio, sin secretos)",
    )

    rows_copied: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Filas copiadas (solo pasos de datos)",
    )

    execution_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Duración del paso en milisegundos",
    )

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento de ejecución del paso",
    )

    def __repr__(self) -> str:
        return (
            f"<CloneJobItem(id={self.id}, job={self.job_id}, seq={self.seq}, "
            f"{self.kind}:{self.object_type}:{self.object_name})>"
        )
