"""Schemas Pydantic de los perfiles de permisos."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EngineType
from app.services.db_admin.dtos import GrantLevel


class PermissionProfileItemIn(BaseModel):
    level: GrantLevel
    privileges: list[str] = Field(..., min_length=1)


class PermissionProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    engine: EngineType
    description: str | None = Field(None, max_length=255)
    items: list[PermissionProfileItemIn] = Field(..., min_length=1)


class PermissionProfileUpdate(BaseModel):
    # engine es inmutable (cambiarlo invalidaría los items). name/desc/is_active/items.
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    # Si se provee, REEMPLAZA por completo los items (se revalidan).
    items: list[PermissionProfileItemIn] | None = Field(None, min_length=1)


class PermissionProfileItemOut(BaseModel):
    level: GrantLevel
    privileges: list[str]
    requires_confirmation: bool = False


class PermissionProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    engine: EngineType
    description: str | None = None
    is_active: bool
    items: list[PermissionProfileItemOut]
    created_at: datetime
    updated_at: datetime
