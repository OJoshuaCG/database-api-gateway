"""
Modelo ManagedDatabaseDataCredential: credencial de DATOS (SELECT-only) de UNA base gestionada.

Es 1:1 con ``managed_databases`` y vive en tabla propia (design D9) por tres razones: el secreto
cifrado no viaja en la fila caliente que serializa el inventario; el alcance es POR BASE (a
diferencia de ``servers.readonly_*``, que es por servidor y solo sirve a la estructura); y
``ON DELETE CASCADE`` la mata junto con la base.

Las columnas ``data_access_*`` (opt-in con segundo aprobador) entran ya en esta migración y nacen
cerradas (D13): una segunda migración para ellas sería otra ventana con el esquema a medias.
Ningún lector las consulta todavía; las usan las slices siguientes.

``password_encrypted`` es Fernet (``app.core.crypto``) y NUNCA se serializa ni se loguea.
``probe_violations`` guarda códigos cortos en JSON, jamás el texto de un grant.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ManagedDatabaseDataCredential(Base, TimestampMixin):
    __tablename__ = "managed_database_data_credentials"
    __table_args__ = (
        UniqueConstraint(
            "managed_database_id",
            name="uq_managed_database_data_credentials_managed_database_id",
        ),
        {"comment": "Credencial de datos SELECT-only por base gestionada (1:1, cifrada)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la credencial de datos"
    )

    managed_database_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=False,
        comment="Base gestionada a la que pertenece (1:1). CASCADE: muere con la base",
    )

    username: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Cuenta del motor (mcp_d_<id>)"
    )

    account_host: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="%",
        server_default="%",
        comment="Host del grantee (MySQL/MariaDB); se ignora en PostgreSQL",
    )

    password_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Password CIFRADO (Fernet). Nunca se expone ni se loguea"
    )

    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Última sonda exitosa sobre ESTA base. NULL = sin verificar (no usable)",
    )

    probed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Última vez que se corrió la sonda (pase o falle)"
    )

    probe_violations: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON con códigos cortos de la última sonda; sin texto de grants"
    )

    probe_warnings: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON con advertencias no bloqueantes de la última sonda"
    )

    data_access_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Opt-in de LECTURA DE DATOS por base para agentes. Nace cerrado",
    )

    data_access_requested_by_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Usuario del gateway que pidió abrir el acceso a datos",
    )

    data_access_approved_by_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Segundo aprobador del acceso a datos (distinto del solicitante)",
    )

    data_access_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cuándo se aprobó el acceso a datos"
    )

    def __repr__(self) -> str:
        return (
            f"<ManagedDatabaseDataCredential(id={self.id}, "
            f"managed_database_id={self.managed_database_id}, verified={self.verified_at is not None})>"
        )
