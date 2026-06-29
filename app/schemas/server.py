"""
Schemas Pydantic del recurso Server.

Regla de oro: NINGÚN schema de salida (`ServerOut`) expone la credencial
pseudo-root (ni cifrada ni descifrada). Solo se informa `has_root_password`.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import EngineType, ServerStatus

# Modos TLS válidos hacia el motor destino. None/"" => sin TLS (se omite el paso).
_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def _normalize_ssl_mode(value: str | None) -> str | None:
    """None o vacío => None (sin TLS). En otro caso debe ser un modo válido."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == "":
        return None
    if v not in _SSL_MODES:
        raise ValueError(f"ssl_mode inválido. Use uno de: {', '.join(sorted(_SSL_MODES))}")
    return v


class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    engine: EngineType
    root_username: str = Field(..., min_length=1, max_length=128)
    # Entra en texto plano; el controller lo cifra antes de persistir.
    root_password: str = Field(..., min_length=1)
    # TLS por conexión: si se especifica, se usa; si no, se omite. Opcional.
    ssl_mode: str | None = Field(None, description="disable|allow|prefer|require|verify-ca|verify-full")
    notes: str | None = None
    is_active: bool = True

    _v_ssl = field_validator("ssl_mode")(staticmethod(_normalize_ssl_mode))


class ServerUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    host: str | None = Field(None, min_length=1, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    engine: EngineType | None = None
    root_username: str | None = Field(None, min_length=1, max_length=128)
    # Si se provee, se re-cifra; si se omite, no cambia.
    root_password: str | None = Field(None, min_length=1)
    ssl_mode: str | None = Field(None, description="disable|allow|prefer|require|verify-ca|verify-full")
    notes: str | None = None
    is_active: bool | None = None

    _v_ssl = field_validator("ssl_mode")(staticmethod(_normalize_ssl_mode))


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    engine: EngineType
    root_username: str
    ssl_mode: str | None = None
    status: ServerStatus
    is_active: bool
    notes: str | None = None
    has_root_password: bool = False
    created_at: datetime
    updated_at: datetime


# ─── Reconciliación (drift): plano en vivo vs inventario del gateway ───────── #


class ReconcileDatabaseItem(BaseModel):
    """Estado de una BD cruzando el motor en vivo con el inventario del gateway."""

    name: str
    # managed = en motor y en inventario · unmanaged = solo en motor (adoptable)
    # · orphan = solo en inventario (se borró por fuera)
    state: str
    managed_id: int | None = None
    owner_id: int | None = None
    status: str | None = None


class ReconcileUserItem(BaseModel):
    username: str
    host: str | None = None
    state: str
    managed_id: int | None = None


class ReconcileResult(BaseModel):
    server_id: int
    databases: list[ReconcileDatabaseItem]
    users: list[ReconcileUserItem]
