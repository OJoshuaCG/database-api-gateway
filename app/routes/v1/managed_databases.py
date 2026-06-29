"""
Endpoints de ManagedDatabases (bases de datos gestionadas).

Crea/otorga/borra BDs reales en el motor destino. Flags que tocan el motor:
- ``?provision=true`` en POST → CREATE DATABASE + GRANT al propietario.
- ``?drop_remote=true`` en DELETE → DROP DATABASE.
- ``?provision=true`` en reassign-owner → re-grant / ALTER OWNER en el motor.
"""

from fastapi import APIRouter, Query, Request

from app.controllers.managed_database_controller import ManagedDatabaseController
from app.controllers.managed_migration_controller import ManagedMigrationController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.models.enums import ProvisionStatus
from app.schemas.managed_database import (
    AdoptDatabaseIn,
    ManagedDatabaseCreate,
    ManagedDatabaseOut,
    ManagedDatabaseUpdate,
    ReassignOwnerIn,
)
from app.schemas.model_migration import (
    MigrationApplyOut,
    MigrationHistoryOut,
    MigrationRollbackOut,
    MigrationStatusOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/managed-databases", tags=["Managed Databases"])


@router.get("", response_model=ApiResponse[list[ManagedDatabaseOut]])
def list_databases(
    admin: AdminDep,
    pagination: PaginationDep,
    server_id: int | None = Query(None, ge=1),
    owner_id: int | None = Query(None, ge=1),
    model_id: int | None = Query(None, ge=1),
    status: ProvisionStatus | None = Query(None),
):
    items, total = ManagedDatabaseController().list_databases(
        server_id=server_id,
        owner_id=owner_id,
        model_id=model_id,
        status=status,
        limit=pagination.size,
        offset=pagination.offset,
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ManagedDatabaseOut], status_code=201)
def create_database(
    admin: AdminDep, payload: ManagedDatabaseCreate, provision: bool = Query(False)
):
    created = ManagedDatabaseController().create_database(
        payload.model_dump(), provision=provision, admin=admin
    )
    msg = "Base de datos registrada en el inventario."
    if provision:
        msg = "Base de datos creada y aprovisionada en el motor."
    return success(data=created, message=msg)


@router.post("/adopt", response_model=ApiResponse[ManagedDatabaseOut], status_code=201)
def adopt_database(admin: AdminDep, payload: AdoptDatabaseIn):
    """
    Adopta una BD que YA existe en el motor (Plan 09): registra metadata sin ejecutar
    CREATE DATABASE. 404 si la BD no existe; 409 si ya está en el inventario.
    """
    created = ManagedDatabaseController().adopt_database(payload.model_dump(), admin=admin)
    return success(data=created, message="Base de datos existente adoptada al inventario.")


@router.get("/{db_id}", response_model=ApiResponse[ManagedDatabaseOut])
def get_database(admin: AdminDep, db_id: int):
    return success(data=ManagedDatabaseController().get_database(db_id))


@router.patch("/{db_id}", response_model=ApiResponse[ManagedDatabaseOut])
def update_database(admin: AdminDep, db_id: int, payload: ManagedDatabaseUpdate):
    updated = ManagedDatabaseController().update_database(
        db_id, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=updated, message="Base de datos actualizada.")


@router.delete("/{db_id}", response_model=ApiResponse[None])
def delete_database(
    admin: AdminDep,
    db_id: int,
    drop_remote: bool = Query(False),
    confirm_name: str | None = Query(
        None,
        description="Obligatorio si drop_remote=true: repetir el nombre exacto de la BD para confirmar el DROP en el motor.",
    ),
):
    ManagedDatabaseController().delete_database(
        db_id, drop_remote=drop_remote, confirm_name=confirm_name, admin=admin
    )
    return empty("Base de datos eliminada.")


@router.post(
    "/{db_id}/reassign-owner", response_model=ApiResponse[ManagedDatabaseOut]
)
def reassign_owner(
    admin: AdminDep,
    db_id: int,
    payload: ReassignOwnerIn,
    provision: bool = Query(False),
):
    updated = ManagedDatabaseController().reassign_owner(
        db_id, payload.owner_id, provision=provision, admin=admin
    )
    return success(data=updated, message="Propietario reasignado.")


# --------------------------------------------------------------------------- #
# Migraciones del blueprint sobre ESTA BD (tocan el motor destino vía Alembic) #
# --------------------------------------------------------------------------- #
@router.get(
    "/{db_id}/migrations/status", response_model=ApiResponse[MigrationStatusOut]
)
def migration_status(admin: AdminDep, db_id: int):
    return success(data=ManagedMigrationController().status(db_id))


@router.post("/{db_id}/migrations/apply", response_model=ApiResponse[MigrationApplyOut])
@limiter.limit("10/minute")
def apply_migrations(
    request: Request,
    admin: AdminDep,
    db_id: int,
    version: str | None = Query(
        None,
        pattern=r"^\d{4,10}$",
        description=(
            "Versión objetivo (inclusive). En UNA sola llamada aplica secuencialmente, en "
            "orden, TODAS las migraciones pendientes hasta esta versión. Si se omite, aplica "
            "hasta la ÚLTIMA disponible. Forward-only: una versión ≤ la actual no aplica nada "
            "(para revertir, usa /rollback). 422 si la versión no existe en el blueprint."
        ),
    ),
    force: bool = Query(
        False, description="Override de cuarentena tras un fallo previo (inspeccionado)."
    ),
    dry_run: bool = Query(
        False, description="No aplica: devuelve el plan (versión actual + pendientes)."
    ),
):
    result = ManagedMigrationController().apply(
        db_id, up_to_version=version, force=force, dry_run=dry_run, admin=admin
    )
    msg = _apply_message(result, dry_run=dry_run)
    return success(data=result, message=msg)


def _apply_message(result: dict, *, dry_run: bool) -> str:
    """Mensaje legible del resultado de apply (real o dry-run)."""
    frm, to = result.get("from_version"), result.get("to_version")
    target = result.get("target_version")
    pend = result.get("pending_versions") or []
    if dry_run:
        if result.get("no_op"):
            return f"Plan (dry-run): la BD ya está al día en {frm or 'sin versión'}; nada pendiente."
        return f"Plan (dry-run): {len(pend)} pendiente(s) — {frm or '∅'} → {to}: {', '.join(pend)}."
    if result.get("no_op"):
        if target is not None:
            return (
                f"La versión solicitada ({target}) ya está aplicada o es anterior a la actual "
                f"({frm}): no se aplica nada (usa /rollback para revertir)."
            )
        return f"La BD ya está en la versión más reciente ({frm or 'sin versión'}); nada que aplicar."
    suffix = " (con fallo: revisa cuarentena)" if result.get("failed") else ""
    return f"Aplicadas {result.get('applied_count', 0)} migración(es): {frm or '∅'} → {to}.{suffix}"


@router.post("/{db_id}/migrations/rollback", response_model=ApiResponse[MigrationRollbackOut])
@limiter.limit("10/minute")
def rollback_migration(
    request: Request,
    admin: AdminDep,
    db_id: int,
    confirm_version: str = Query(
        ...,
        pattern=r"^\d{4,10}$",
        description=(
            "Confirmación obligatoria (operación DESTRUCTIVA): repetir la versión "
            "ACTUAL de la BD desde la que se parte."
        ),
    ),
    target_version: str | None = Query(
        None,
        pattern=r"^\d{4,10}$",
        description=(
            "Versión destino a la que revertir (debe ser ANTERIOR a la actual). En UNA "
            "sola llamada aplica secuencialmente, en orden, todos los downgrades "
            "necesarios. Si se omite, revierte solo la última. 409 si alguna migración "
            "del camino no tiene down_sql confirmado; 422 si la versión no existe o no "
            "es anterior a la actual."
        ),
    ),
):
    result = ManagedMigrationController().rollback(
        db_id, confirm_version=confirm_version, target_version=target_version, admin=admin
    )
    return success(data=result, message=_rollback_message(result))


def _rollback_message(result: dict) -> str:
    """Mensaje legible del resultado de rollback."""
    frm, to = result.get("from_version"), result.get("to_version")
    n = result.get("reverted_count", 0)
    if result.get("no_op"):
        return f"Nada que revertir: la BD ya está en {frm or 'base'}."
    if result.get("failed"):
        return (
            f"Rollback con fallo: revertidas {n}, la BD quedó en {to or 'base'}. "
            "Revisa la cuarentena."
        )
    return f"Revertidas {n} migración(es): {frm} → {to or 'base'}."


@router.post("/{db_id}/migrations/stamp", response_model=ApiResponse[MigrationStatusOut])
@limiter.limit("10/minute")
def stamp_migration(
    request: Request,
    admin: AdminDep,
    db_id: int,
    version: str = Query(..., pattern=r"^\d{4,10}$", description="Versión a marcar"),
):
    result = ManagedMigrationController().stamp(db_id, version, admin=admin)
    return success(data=result, message="Versión marcada (stamp).")


@router.get(
    "/{db_id}/migrations/history",
    response_model=ApiResponse[list[MigrationHistoryOut]],
)
def migration_history(admin: AdminDep, db_id: int, pagination: PaginationDep):
    items, total = ManagedMigrationController().history(
        db_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)
