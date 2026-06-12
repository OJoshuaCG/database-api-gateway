"""
Modelo ServerUser — usuario del MOTOR de un servidor destino (el "propietario").

Representa un usuario real del motor (par ``'usuario'@'host'`` en MySQL/MariaDB o
ROLE con LOGIN en PostgreSQL) que el gateway gestiona en un servidor concreto. La
credencial, si se almacena, va CIFRADA con Fernet y NUNCA se expone ni se loguea.

Es el "propietario" de las bases de datos gestionadas: una BD pertenece a
exactamente un ServerUser del mismo servidor.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ServerUser(Base, TimestampMixin):
    __tablename__ = "server_users"
    __table_args__ = (
        UniqueConstraint(
            "server_id",
            "username",
            "host",
            name="uq_server_users_server_username_host",
        ),
        {"comment": "Usuarios del motor (propietarios) gestionados por el gateway"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del usuario"
    )

    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor destino al que pertenece el usuario",
    )

    username: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Nombre del usuario/rol en el motor"
    )

    host: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="%",
        server_default="%",
        comment="Host MySQL ('user'@'host'); ignorado en PostgreSQL",
    )

    password_encrypted: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Password del usuario CIFRADO (Fernet), opcional. Nunca se expone",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Soft-disable del usuario en el inventario",
    )

    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Notas adicionales sobre el usuario"
    )

    def __repr__(self) -> str:
        return (
            f"<ServerUser(id={self.id}, server_id={self.server_id}, "
            f"username='{self.username}', host='{self.host}')>"
        )
