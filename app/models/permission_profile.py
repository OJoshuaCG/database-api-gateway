"""
Modelos de PERFILES DE PERMISOS (templates de privilegios).

Un `PermissionProfile` es una plantilla **clasificada por motor** (mysql/mariadb/
postgresql) que agrupa privilegios por nivel. Sirve para asignar permisos "rápido":
se aplica a un usuario sobre un objeto concreto y sus items se traducen en GRANTs.

Decisión (snapshot): asignar un perfil **aplica** sus privilegios en el momento; NO crea
una relación viva usuario↔perfil (cambiar el perfil después no re-sincroniza al usuario).
Por eso aquí solo se modela la PLANTILLA; la aplicación real (ejecutar los GRANT) la hará
el motor de permisos granular (Plan 07).

Cada `PermissionProfileItem` define, para un NIVEL (table/column/schema/...), la lista de
privilegios canónicos (validados contra el catálogo del motor en `db_admin/privileges.py`).
El OBJETO destino (qué BD/tabla) se indica al APLICAR el perfil, no en la plantilla.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PermissionProfile(Base, TimestampMixin):
    __tablename__ = "permission_profiles"
    __table_args__ = (
        UniqueConstraint("engine", "name", name="uq_permission_profiles_engine_name"),
        {"comment": "Plantillas de privilegios (perfiles) por motor"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Nombre del perfil (único por motor)"
    )
    engine: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Motor al que aplica: mysql | mariadb | postgresql",
    )
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Para qué sirve el perfil (breve)"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Permite deshabilitar el perfil sin borrarlo",
    )

    def __repr__(self) -> str:
        return f"<PermissionProfile(id={self.id}, name='{self.name}', engine='{self.engine}')>"


class PermissionProfileItem(Base, TimestampMixin):
    __tablename__ = "permission_profile_items"
    __table_args__ = (
        UniqueConstraint("profile_id", "level", name="uq_profile_items_profile_level"),
        {"comment": "Privilegios por nivel que componen un perfil"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("permission_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Perfil al que pertenece este item",
    )
    level: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Nivel del privilegio: database|schema|table|column|sequence|routine",
    )
    privileges: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Privilegios canónicos separados por coma (validados contra el catálogo)",
    )

    def __repr__(self) -> str:
        return f"<PermissionProfileItem(profile_id={self.profile_id}, level='{self.level}')>"
