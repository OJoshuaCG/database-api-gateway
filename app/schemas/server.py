"""
Schemas Pydantic del recurso Server.

Regla de oro: NINGÚN schema de salida (`ServerOut`) expone la credencial
pseudo-root (ni cifrada ni descifrada). Solo se informa `has_root_password`.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EngineType, ServerStatus


class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    engine: EngineType
    root_username: str = Field(..., min_length=1, max_length=128)
    # Entra en texto plano; el controller lo cifra antes de persistir.
    root_password: str = Field(..., min_length=1)
    notes: str | None = None
    is_active: bool = True


class ServerUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    host: str | None = Field(None, min_length=1, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    engine: EngineType | None = None
    root_username: str | None = Field(None, min_length=1, max_length=128)
    # Si se provee, se re-cifra; si se omite, no cambia.
    root_password: str | None = Field(None, min_length=1)
    notes: str | None = None
    is_active: bool | None = None


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    engine: EngineType
    root_username: str
    status: ServerStatus
    is_active: bool
    notes: str | None = None
    has_root_password: bool = False
    created_at: datetime
    updated_at: datetime
