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
