"""Schemas Pydantic para los endpoints de grants granulares."""

from typing import Annotated

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


# ─── Apply-profile MASIVO (mismo perfil, N bases de datos) ──────────────────── #

# Nombre de BD: mismo criterio que app/schemas/clone.py (min 1, max 64 — el techo de
# MySQL/MariaDB; PostgreSQL es 63). La validación REAL de identificador (charset,
# anti-inyección) la hace el adapter antes de construir el DCL; esto solo corta en el
# borde los casos absurdos (cadena vacía, nombre kilométrico) sin llegar al motor.
DatabaseName = Annotated[str, Field(min_length=1, max_length=64)]


class ApplyProfileBulkRequest(BaseModel):
    """
    Parámetros para aplicar el MISMO perfil al MISMO usuario sobre N bases de datos
    en una sola llamada (caso típico multi-tenant: una BD por cliente con idéntica
    estructura relativa).

    ``object_mappings`` es una PLANTILLA: para cada BD de ``databases`` se reusan
    ``schema``/``table``/``columns``/``sequence``/``routine`` tal cual, y el campo
    ``database`` de cada ``object_ref`` se **sobreescribe** con el nombre de la BD de
    la iteración (por eso mandarlo es opcional: se ignora).
    """

    databases: list[DatabaseName] = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Nombres de las bases de datos destino (no ids de inventario: el módulo de "
            "grants trabaja por identidad, así que sirve para BDs adoptadas y crudas). "
            "Cota de 100 por llamada; ver la nota de latencia en la doc del endpoint."
        ),
    )
    object_mappings: list[LevelObjectMapping] = Field(
        default_factory=list,
        description=(
            "Plantilla (nivel → objeto) reusada en cada BD. El 'database' de cada "
            "object_ref se ignora y se reemplaza por el de la iteración. Niveles del "
            "perfil sin mapeo son omitidos (skipped_levels)."
        ),
    )


class ApplyProfileBulkItemOut(BaseModel):
    """Resultado del apply del perfil sobre UNA base de datos del lote."""

    database: str
    grants_applied: int
    skipped_levels: list[str]
    errors: list[str]
    ok: bool


class ApplyProfileBulkResult(BaseModel):
    profile_id: int
    profile_name: str
    engine: str
    total_databases: int
    results: list[ApplyProfileBulkItemOut]


# Re-export GrantInfo as the list-grants output type.
__all__ = [
    "GrantRequest", "RevokeRequest", "GrantableRequest", "GrantableResult",
    "GrantInfo", "LevelObjectMapping", "ApplyProfileRequest", "ApplyProfileResult",
    "ApplyProfileBulkRequest", "ApplyProfileBulkItemOut", "ApplyProfileBulkResult",
]
