"""Schemas Pydantic del recurso DatabaseModel (blueprint/categoría)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_SLUG = r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$"


class DatabaseModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=120, pattern=_SLUG)
    description: str | None = None
    current_version: str = Field("0.0.0", max_length=50)
    is_active: bool = True


class DatabaseModelUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    slug: str | None = Field(None, min_length=1, max_length=120, pattern=_SLUG)
    description: str | None = None
    current_version: str | None = Field(None, max_length=50)
    is_active: bool | None = None


class DatabaseModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None = None
    current_version: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ─── Snapshot → blueprint baseline (Plan 09) ──────────────────────────────── #


class FromSnapshotIn(BaseModel):
    """
    Crea un blueprint NUEVO cuyo baseline (v0001) es el snapshot estructural de una BD
    existente. El baseline queda atado al motor de origen si incluye objetos
    procedurales (``has_non_portable``). Solo estructura, nunca datos.
    """

    server_id: int = Field(..., ge=1)
    database: str = Field(..., min_length=1, max_length=64, description="BD existente a fotografiar")
    name: str = Field(..., min_length=1, max_length=100, description="Nombre del blueprint a crear")
    slug: str = Field(..., min_length=1, max_length=120, pattern=_SLUG)
    description: str | None = None
    baseline_name: str = Field(
        "Snapshot baseline", min_length=1, max_length=200,
        description="Nombre de la migración baseline (v0001)",
    )


class FromSnapshotOut(BaseModel):
    """Resultado de from-snapshot: el blueprint creado + resumen del baseline."""

    model: DatabaseModelOut
    baseline_version: str
    source_engine: str
    has_non_portable: bool
    object_counts: dict[str, int]
    statements_captured: int
