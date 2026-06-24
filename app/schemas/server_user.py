"""
Schemas Pydantic del recurso ServerUser (usuario del motor).

Regla de oro: ningún schema de salida expone el password (ni cifrado ni en claro).
Solo se informa ``has_password``.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services.db_admin.dtos import GrantLevel, ObjectRef

# Whitelist alineada con app/services/db_admin/identifiers.py (validación fail-fast).
_USERNAME = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_HOST = r"^[A-Za-z0-9_.%:\-]{1,255}$"


class ServerUserCreate(BaseModel):
    server_id: int = Field(..., ge=1)
    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="Solo MySQL/MariaDB; ignorado en PostgreSQL")
    # Requerido solo si se aprovisiona (?provision=true); el controller lo valida.
    password: str | None = Field(None, min_length=1)
    notes: str | None = None
    is_active: bool = True


class ServerUserUpdate(BaseModel):
    # username/host/server_id son inmutables (cambiarlos = drop + create).
    password: str | None = Field(None, min_length=1)
    is_active: bool | None = None
    notes: str | None = None


class ServerUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    username: str
    host: str
    is_active: bool
    notes: str | None = None
    has_password: bool = False
    created_at: datetime
    updated_at: datetime


# ─── Endpoint unificado: crear usuario + grants iniciales ─────────────────── #

class GrantOnCreate(BaseModel):
    """Grant a aplicar justo después de crear/aprovisionar el usuario."""
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)
    with_grant_option: bool = False


class ServerUserFullCreate(ServerUserCreate):
    """Igual que ServerUserCreate + lista opcional de grants a aplicar al crear."""
    initial_grants: list[GrantOnCreate] = Field(
        default_factory=list,
        description="Permisos a otorgar en el motor justo después de aprovisionar el usuario.",
    )


class GrantApplyResult(BaseModel):
    level: str
    object: str | None = None
    privileges: list[str]
    success: bool
    error: str | None = None


class ServerUserFullOut(BaseModel):
    """Respuesta del endpoint unificado crear+permisos."""
    user: ServerUserOut
    grants_applied: int
    grant_results: list[GrantApplyResult]
