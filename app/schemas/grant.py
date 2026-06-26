"""Schemas Pydantic para los endpoints de grants granulares."""

from pydantic import BaseModel, Field

from app.services.db_admin.dtos import GrantInfo, GrantLevel, ObjectRef


class GrantRequest(BaseModel):
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)
    with_grant_option: bool = False


class RevokeRequest(BaseModel):
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)
    cascade: bool = Field(
        default=False,
        description=(
            "Solo PostgreSQL: revoca en cascada los privilegios que el grantee haya "
            "re-delegado. Operación GATE: exige confirmación (query 'confirm_grantee'). "
            "MySQL/MariaDB no lo soporta (422). Por defecto RESTRICT."
        ),
    )


class GrantableRequest(BaseModel):
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)


class GrantableResult(BaseModel):
    can_grant: bool
    level: GrantLevel
    privileges: list[str]


# ─── Apply-profile endpoint ────────────────────────────────────────────────── #

class LevelObjectMapping(BaseModel):
    """Mapeo de un nivel de permiso a un objeto concreto para aplicar un perfil."""
    level: GrantLevel
    object_ref: ObjectRef


class ApplyProfileRequest(BaseModel):
    """
    Parámetros para aplicar un perfil de permisos a un usuario. Para cada nivel
    definido en el perfil, se debe proveer el objeto destino (BD, tabla, etc.).
    Los niveles del perfil sin mapeo se omiten (se reportan como 'skipped').
    """
    object_mappings: list[LevelObjectMapping] = Field(
        default_factory=list,
        description=(
            "Lista de (nivel → objeto) para cada nivel del perfil que se quiere aplicar. "
            "Niveles del perfil sin mapeo son omitidos."
        ),
    )


class ApplyProfileResult(BaseModel):
    profile_id: int
    profile_name: str
    engine: str
    grants_applied: int
    skipped_levels: list[str]
    errors: list[str]


# Re-export GrantInfo as the list-grants output type.
__all__ = [
    "GrantRequest", "RevokeRequest", "GrantableRequest", "GrantableResult",
    "GrantInfo", "LevelObjectMapping", "ApplyProfileRequest", "ApplyProfileResult",
]
