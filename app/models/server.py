"""
Modelo Server — servidor de base de datos DESTINO administrado por el gateway.

Guarda los datos de conexión y la credencial pseudo-root (CIFRADA con Fernet).
La credencial nunca se expone en respuestas ni se loguea.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SQLAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import EngineType, ServerStatus


class Server(Base, TimestampMixin):
    __tablename__ = "servers"
    __table_args__ = (
        UniqueConstraint("host", "port", name="uq_servers_host_port"),
        {"comment": "Servidores de base de datos destino administrados por el gateway"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del servidor"
    )

    name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
        comment="Alias legible del servidor",
    )

    host: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Hostname o IP del servidor destino"
    )

    port: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Puerto de conexión del motor"
    )

    engine: Mapped[EngineType] = mapped_column(
        SQLAEnum(EngineType, native_enum=False, length=20),
        nullable=False,
        comment="Motor de base de datos: mysql | mariadb | postgresql",
    )

    root_username: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Usuario pseudo-root para administrar el servidor"
    )

    root_password_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Password pseudo-root CIFRADO (Fernet). Nunca se expone ni se loguea",
    )

    ssl_mode: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment=(
            "Política TLS hacia ESTE servidor. NULL/vacío = sin TLS. PostgreSQL: "
            "require|verify-ca|verify-full|prefer|allow|disable. MySQL/MariaDB: "
            "cualquier valor distinto de 'disable' cifra el transporte."
        ),
    )

    status: Mapped[ServerStatus] = mapped_column(
        SQLAEnum(ServerStatus, native_enum=False, length=20),
        nullable=False,
        default=ServerStatus.active,
        server_default=ServerStatus.active.value,
        comment="Estado operativo del servidor en el inventario",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Permite deshabilitar el servidor sin borrarlo (soft-disable)",
    )

    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Notas adicionales sobre el servidor"
    )

    # ---- Credencial de SOLO LECTURA del MCP (plan 12 §5.2) ---- #
    # Separada de la pseudo-root a propósito: el MCP nunca habla con el motor como root, ni
    # siquiera como fallback. Sin estas tres columnas completas y con la sonda reciente, el
    # servidor queda fuera del alcance de toda tool que lea el motor. Borrarlas es la palanca
    # de emergencia granular: saca a UN servidor del MCP sin tocar nada más.
    readonly_username: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Usuario de SOLO LECTURA para el MCP. NULL = servidor fuera del MCP",
    )

    readonly_password_encrypted: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Password de solo lectura CIFRADO (Fernet). Nunca se expone ni se loguea",
    )

    readonly_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Última sonda negativa exitosa: el motor rechazó escribir con esta credencial",
    )

    def __repr__(self) -> str:
        return f"<Server(id={self.id}, name='{self.name}', engine='{self.engine}')>"
