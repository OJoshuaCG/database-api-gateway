"""
Modelo MigrationStatementProgress — checkpoint de sentencias SQL dentro de UNA
migración, por BD gestionada y dirección (up/down).

Permite que un ``apply``/``rollback`` que falló A MITAD de una migración (DDL en
AUTOCOMMIT: las sentencias previas ya commitearon físicamente) se retome desde la
última sentencia exitosa en el próximo intento, en vez de re-ejecutar desde cero y
chocar con "el objeto ya existe". Ver ``app/services/db_admin/migration_progress.py``
para la lógica de elegibilidad (``is_resumable``) y el manejo del checkpoint.

NO es la fuente de verdad de qué migración está aplicada (esa sigue siendo la tabla
``_gw_v_{slug}`` que Alembic mantiene DENTRO de cada BD gestionada). Esta tabla es
efímera por naturaleza: una fila existe solo mientras una migración está A MITAD de
camino en una dirección; se borra al completarse con éxito.
"""

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MigrationStatementProgress(Base, TimestampMixin):
    __tablename__ = "migration_statement_progress"
    __table_args__ = (
        UniqueConstraint(
            "managed_database_id", "model_migration_id", "direction",
            name="uq_msp_db_migration_direction",
        ),
        {
            "comment": (
                "Checkpoint de sentencias SQL ejecutadas dentro de UNA migración "
                "(permite resumir tras un fallo parcial)"
            )
        },
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del checkpoint"
    )

    managed_database_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=False,
        # El UniqueConstraint(managed_database_id, model_migration_id, direction) ya
        # sirve el lookup por su prefijo izquierdo: sin índice propio adicional.
        comment="BD gestionada sobre la que se está aplicando/revirtiendo",
    )

    model_migration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_migrations.id", ondelete="CASCADE"),
        nullable=False,
        comment="Migración cuyo progreso se rastrea",
    )

    direction: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="'up' (apply) | 'down' (rollback)"
    )

    total_statements: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cantidad total de sentencias de esta dirección"
    )

    last_statement_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Sentencias (1-based) ejecutadas con éxito antes del último fallo",
    )

    migration_checksum: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "Checksum de ModelMigration al generar el checkpoint — si no coincide con "
            "el checksum ACTUAL (SQL editado), el checkpoint es inválido y no se "
            "reanuda (fail-closed)"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MigrationStatementProgress(managed_database_id={self.managed_database_id}, "
            f"model_migration_id={self.model_migration_id}, direction='{self.direction}', "
            f"{self.last_statement_index}/{self.total_statements})>"
        )
