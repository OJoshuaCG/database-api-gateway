"""
Schemas Pydantic del recurso ServerUser (usuario del motor).

Regla de oro: ningún schema de salida expone el password (ni cifrado ni en claro).
Solo se informa ``has_password``.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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
