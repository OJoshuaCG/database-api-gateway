"""Schemas Pydantic del catálogo de privilegios."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PrivilegeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    engine: str
    name: str
    category: str
    context: str | None = None
    description: str
    is_sensitive: bool = False
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class PrivilegeUpdate(BaseModel):
    """Activar/desactivar un privilegio del catálogo."""

    is_active: bool
