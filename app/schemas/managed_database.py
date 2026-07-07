"""Schemas Pydantic del recurso ManagedDatabase (BD gestionada en un servidor)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ProvisionStatus

# Whitelist alineada con identifiers.py (validación fail-fast en la API).
_DBNAME = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_CHARSET = r"^[A-Za-z0-9_]{1,64}$"


class ManagedDatabaseCreate(BaseModel):
    name: str = Field(..., pattern=_DBNAME)
    server_id: int = Field(..., ge=1)
    owner_id: int = Field(..., ge=1, description="ServerUser propietario, del mismo servidor")
    model_id: int | None = Field(None, ge=1)
    model_version: str | None = Field(None, max_length=50)
    charset: str | None = Field(None, pattern=_CHARSET, description="MySQL/MariaDB")
    collation: str | None = Field(None, pattern=_CHARSET, description="MySQL/MariaDB")
    notes: str | None = None


class ManagedDatabaseUpdate(BaseModel):
    # name/server_id/owner_id NO se editan aquí (owner: usar reassign-owner).
    model_id: int | None = Field(None, ge=1)
    model_version: str | None = Field(None, max_length=50)
    charset: str | None = Field(None, pattern=_CHARSET)
    collation: str | None = Field(None, pattern=_CHARSET)
    notes: str | None = None


class ReassignOwnerIn(BaseModel):
    owner_id: int = Field(..., ge=1, description="Nuevo propietario (ServerUser del mismo servidor)")


class AdoptDatabaseIn(BaseModel):
    """
    Adopta una BD que YA existe en el motor (Plan 09): registra metadata SIN ejecutar
    CREATE DATABASE. El gateway verifica que la BD exista realmente (404 si no).
    """

    name: str = Field(..., pattern=_DBNAME, description="Nombre EXACTO de la BD existente en el motor")
    server_id: int = Field(..., ge=1)
    owner_id: int = Field(..., ge=1, description="ServerUser propietario, del mismo servidor")
    model_id: int | None = Field(None, ge=1, description="Blueprint a vincular (opcional)")
    model_version: str | None = Field(
        None,
        max_length=50,
        description=(
            "Versión del blueprint en la que YA se encuentra la BD adoptada. Si se indica, "
            "el gateway hace 'stamp' de esa versión en el motor (sin ejecutar DDL) para que "
            "el 'apply' no reintente crear lo que ya existe. Omitir = la BD llega 'en ceros'. "
            "Requiere 'model_id'."
        ),
    )
    charset: str | None = Field(None, pattern=_CHARSET, description="Opcional (no se aplica DDL)")
    collation: str | None = Field(None, pattern=_CHARSET)
    notes: str | None = None


class ManagedDatabaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    server_id: int
    owner_id: int
    model_id: int | None = None
    model_version: str | None = None
    charset: str | None = None
    collation: str | None = None
    status: ProvisionStatus
    notes: str | None = None
    origin: str = "provisioned"
    created_at: datetime
    updated_at: datetime
