"""
Modelo DatabaseMigrationHistory — log de aplicación de migraciones por BD gestionada.

Es el ESPEJO de auditoría del gateway: registra cada intento de aplicar o revertir
una migración de blueprint sobre una BD gestionada (cuándo, resultado, duración,
error). Permite responder "¿qué BDs están atrasadas / fallaron?" sin abrir N
conexiones a los motores destino.

NO es la fuente de verdad de la versión actual de una BD: esa la mantiene Alembic en
la tabla ``_gw_v_{slug}`` DENTRO de cada BD gestionada. Aquí solo se acumula el
historial de desenlaces.
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLAEnum
from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import MigrationStatus


class DatabaseMigrationHistory(Base, TimestampMixin):
    __tablename__ = "database_migration_history"
    __table_args__ = (
        # Índice compuesto para el patrón real de consulta del historial de una BD:
        # WHERE managed_database_id = ? ORDER BY applied_at DESC (cubre filtro + orden).
        Index(
            "ix_dmh_managed_db_applied_at",
            "managed_database_id",
            "applied_at",
        ),
        {"comment": "Historial de aplicación/rollback de migraciones por BD gestionada"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del registro de historial"
    )

    managed_database_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=False,
        # El índice compuesto (managed_database_id, applied_at) de __table_args__
        # cubre los filtros por managed_database_id por su prefijo izquierdo.
        comment="BD gestionada sobre la que se aplicó la migración",
    )

    model_migration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_migrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Migración aplicada/revertida",
    )

    applied_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="Momento en que se ejecutó el intento"
    )

    status: Mapped[MigrationStatus] = mapped_column(
        SQLAEnum(MigrationStatus, native_enum=False, length=20),
        nullable=False,
        comment="Desenlace del intento (applied | failed)",
    )

    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Detalle del error si status=failed (sin secretos)"
    )

    execution_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Duración de la ejecución en milisegundos"
    )

    def __repr__(self) -> str:
        return (
            f"<DatabaseMigrationHistory(id={self.id}, "
            f"managed_database_id={self.managed_database_id}, status='{self.status}')>"
        )
