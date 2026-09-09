"""
Modelo User - Ejemplo de modelo ORM con SQLAlchemy 2.0.

Este modelo demuestra las mejores prácticas para definir modelos con:
- Type hints modernos (Mapped[])
- Constraints (unique, index)
- Server defaults
- Timestamps automáticos (via TimestampMixin)
- Comentarios descriptivos
"""

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """
    Modelo de usuario del sistema.

    Representa un usuario con autenticación, permisos y perfil básico.
    Hereda timestamps automáticos (created_at, updated_at) del TimestampMixin.
    """

    __tablename__ = "users"
    __table_args__ = {"comment": "Tabla de usuarios del sistema"}

    # Primary Key
    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del usuario"
    )

    # Campos únicos con índices
    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
        comment="Nombre de usuario único para login",
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
        comment="Correo electrónico único del usuario",
    )

    # Autenticación
    hashed_password: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Contraseña hasheada (bcrypt/argon2)"
    )

    # Información de perfil
    full_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Nombre completo del usuario"
    )

    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Notas adicionales sobre el usuario"
    )

    # Permisos y estado
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
        comment="Indica si el usuario está activo en el sistema",
    )

    # `is_superuser` se RETIRÓ acá. Se escribía en tres lugares y no se leía en ninguno para
    # autorizar: `get_current_admin` solo verificaba sesión + `is_active`. O sea no era "todavía
    # no hay permisos", era un sistema multiusuario SIN PUERTA, y un flag inerte —que este repo
    # prohíbe— con la peor forma posible: la que hace creer que algo está protegido.
    # Retirarlo no rompe el contrato con la SPA: `AdminOut` es solo `{id, username}`.
    gateway_role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="viewer",
        index=True,
        comment="Rol base del usuario en el gateway: viewer | operator | owner",
    )

    def __repr__(self) -> str:
        """Representación string del modelo para debugging."""
        return f"<User(id={self.id}, username='{self.username}', email='{self.email}')>"
