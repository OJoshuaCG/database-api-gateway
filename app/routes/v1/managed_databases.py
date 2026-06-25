"""
Endpoints de ManagedDatabases (bases de datos gestionadas).

Crea/otorga/borra BDs reales en el motor destino. Flags que tocan el motor:
- ``?provision=true`` en POST → CREATE DATABASE + GRANT al propietario.
- ``?drop_remote=true`` en DELETE → DROP DATABASE.
- ``?provision=true`` en reassign-owner → re-grant / ALTER OWNER en el motor.
"""

from fastapi import APIRouter, Query

from app.controllers.managed_database_controller import ManagedDatabaseController
from app.controllers.managed_migration_controller import ManagedMigrationController
from app.core.auth import AdminDep
from app.models.enums import ProvisionStatus
from app.schemas.managed_database import (
    ManagedDatabaseCreate,
    ManagedDatabaseOut,
    ManagedDatabaseUpdate,
    ReassignOwnerIn,
)
from app.schemas.model_migration import MigrationStatusOut
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


@router.post("/{db_id}/migrations/apply", response_model=ApiResponse[dict])
def apply_migrations(
    admin: AdminDep,
    db_id: int,
    version: str | None = Query(
        None,
        pattern=r"^\d{4,10}$",
        description="Aplicar solo hasta esta versión (inclusive). Por defecto: todas.",
    ),
):
    result = ManagedMigrationController().apply(db_id, up_to_version=version, admin=admin)
    return success(data=result, message="Migraciones aplicadas.")


@router.post("/{db_id}/migrations/rollback", response_model=ApiResponse[dict])
def rollback_migration(admin: AdminDep, db_id: int):
    result = ManagedMigrationController().rollback(db_id, admin=admin)
    return success(data=result, message="Rollback ejecutado.")


@router.post("/{db_id}/migrations/stamp", response_model=ApiResponse[MigrationStatusOut])
def stamp_migration(
    admin: AdminDep,
    db_id: int,
    version: str = Query(..., pattern=r"^\d{4,10}$", description="Versión a marcar"),
):
    result = ManagedMigrationController().stamp(db_id, version, admin=admin)
    return success(data=result, message="Versión marcada (stamp).")
