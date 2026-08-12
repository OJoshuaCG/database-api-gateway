"""
Schemas Pydantic del catálogo global de charsets/collations.

Nota sobre ``collation``: en la tabla es NOT NULL con centinela ``""`` ("sin collation
específica") para que la UNIQUE de la allowlist funcione en los tres motores. Hacia afuera ese
centinela se expone/acepta como ``null``, que es lo que el frontend espera.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CharsetCollationOptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    engine_family: str
    charset: str
    collation: str | None = None
    enabled: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("collation", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        # "" (centinela de la tabla) → null en la API.
        return v or None


class CharsetCollationOptionCreate(BaseModel):
    """Alta de una combinación custom (no sembrada)."""

    engine_family: str = Field(
        ...,
        max_length=16,
        description="mysql (cubre MySQL y MariaDB) | postgresql",
    )
    charset: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="MySQL/MariaDB: CHARACTER SET. PostgreSQL: ENCODING.",
    )
    collation: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "MySQL/MariaDB: COLLATE. PostgreSQL: LOCALE (LC_COLLATE/LC_CTYPE). "
            "null = el charset sin collation específica."
        ),
    )
    enabled: bool = Field(
        default=False,
        description="Por defecto se agrega DESHABILITADA: habilitarla es un acto explícito.",
    )


class CharsetCollationOptionUpdate(BaseModel):
    """PATCH parcial: habilitar/deshabilitar y/o marcar como default de su familia."""

    enabled: bool | None = None
    is_default: bool | None = None
