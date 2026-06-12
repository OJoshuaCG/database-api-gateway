"""
Modelo DatabaseModel — blueprint/categoría de base de datos.

Describe una "plantilla" lógica (p. ej. "Whatsapp", "SMS", "Llamadas") que varias
bases de datos gestionadas pueden compartir (misma estructura conceptual, BDs
distintas). En esta iteración es solo metadato del inventario; el versionado y la
migración real del blueprint se abordan en la Iteración 3 (ver docs/plans/02).

NOTA de naming: la clase se llama ``DatabaseModel`` (blueprint), distinta de los
modelos de datos ``*_model.py`` que acceden a la BD con SQL directo.
"""

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DatabaseModel(Base, TimestampMixin):
    __tablename__ = "database_models"
    __table_args__ = (
        {"comment": "Blueprints/categorías de base de datos (plantillas lógicas)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del blueprint"
    )

    name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
        comment="Nombre legible del blueprint (p. ej. 'Whatsapp')",
    )

    slug: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        index=True,
        nullable=False,
        comment="Identificador estable en kebab/snake-case",
    )

    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Descripción del blueprint"
    )

    current_version: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="0.0.0",
        server_default="0.0.0",
        comment="Versión actual del blueprint (string libre por ahora)",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Soft-disable del blueprint",
    )

    def __repr__(self) -> str:
        return f"<DatabaseModel(id={self.id}, slug='{self.slug}')>"
