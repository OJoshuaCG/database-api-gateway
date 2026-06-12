"""
Modelo ManagedDatabase — base de datos real creada/gestionada en un servidor.

Reglas de negocio:
- Pertenece a EXACTAMENTE un usuario del motor (``owner_id``), del MISMO servidor.
- Puede replicar un ``DatabaseModel`` (blueprint), opcional.
- Nombre único por servidor.

El campo ``status`` refleja la consistencia entre el inventario y el motor:
``pending`` → ``active`` | ``error`` (ver ``ProvisionStatus``).
"""

from sqlalchemy import Enum as SQLAEnum
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import ProvisionStatus


class ManagedDatabase(Base, TimestampMixin):
    __tablename__ = "managed_databases"
    __table_args__ = (
        UniqueConstraint(
            "server_id", "name", name="uq_managed_databases_server_name"
        ),
        {"comment": "Bases de datos reales gestionadas por el gateway en cada servidor"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la BD gestionada"
    )

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Nombre de la base de datos en el motor"
    )

    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor donde vive la base de datos",
    )

    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("server_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Usuario del motor propietario (único). RESTRICT: reasignar antes de borrar",
    )

    model_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("database_models.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Blueprint que replica esta BD (opcional)",
    )

    model_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Versión del blueprint implementada"
    )

    charset: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Charset (MySQL/MariaDB); p. ej. utf8mb4"
    )

    collation: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Collation (MySQL/MariaDB)"
    )

    status: Mapped[ProvisionStatus] = mapped_column(
        SQLAEnum(ProvisionStatus, native_enum=False, length=20),
        nullable=False,
        default=ProvisionStatus.pending,
        server_default=ProvisionStatus.pending.value,
        comment="Estado de consistencia inventario↔motor",
    )

    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Notas / detalle de error de aprovisionamiento"
    )

    def __repr__(self) -> str:
        return (
            f"<ManagedDatabase(id={self.id}, name='{self.name}', "
            f"server_id={self.server_id}, status='{self.status}')>"
        )
