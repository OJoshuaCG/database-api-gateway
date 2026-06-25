"""
Endpoints de migraciones de blueprints (``/database-models/{id}/migrations``).

CRUD de migraciones sobre el inventario del gateway (NO toca motores) y el apply
masivo (síncrono, acotado) sobre todas las BDs del blueprint.
"""

from fastapi import APIRouter, Path, Query

from app.controllers.managed_migration_controller import ManagedMigrationController
from app.controllers.model_migration_controller import ModelMigrationController
from app.core.auth import AdminDep
from app.schemas.model_migration import (
    ApplyAllOut,
    ModelMigrationCreate,
    ModelMigrationOut,
    ModelMigrationPatch,
    ModelMigrationSummary,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/database-models", tags=["Model Migrations"])

_VERSION_PATH = Path(..., pattern=r"^\d{4,10}$", description="Versión: 0001, 0002…")


@router.get(
    "/{model_id}/migrations",
    response_model=ApiResponse[list[ModelMigrationSummary]],
)
def list_migrations(admin: AdminDep, model_id: int, pagination: PaginationDep):
    items, total = ModelMigrationController().list_migrations(
        model_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post(
    "/{model_id}/migrations",
    response_model=ApiResponse[ModelMigrationOut],
    status_code=201,
)
def create_migration(admin: AdminDep, model_id: int, payload: ModelMigrationCreate):
    created = ModelMigrationController().create_migration(
        model_id, payload.model_dump(), admin=admin
    )
    return success(data=created, message="Migración creada.")


@router.post(
    "/{model_id}/migrations/apply-all",
    response_model=ApiResponse[ApplyAllOut],
)
def apply_all(
    admin: AdminDep,
    model_id: int,
    max_databases: int = Query(10, ge=1, le=100, description="Cota de BDs a procesar"),
):
    result = ManagedMigrationController().apply_all(
        model_id, max_databases=max_databases, admin=admin
    )
    return success(data=result, message="Aplicación masiva ejecutada.")


@router.get(
    "/{model_id}/migrations/{version}",
    response_model=ApiResponse[ModelMigrationOut],
)
def get_migration(admin: AdminDep, model_id: int, version: str = _VERSION_PATH):
    return success(data=ModelMigrationController().get_migration(model_id, version))


@router.patch(
    "/{model_id}/migrations/{version}",
    response_model=ApiResponse[ModelMigrationOut],
)
def update_migration(
    admin: AdminDep,
    model_id: int,
    payload: ModelMigrationPatch,
    version: str = _VERSION_PATH,
):
    updated = ModelMigrationController().update_migration(
        model_id, version, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=updated, message="Migración actualizada.")


@router.delete(
    "/{model_id}/migrations/{version}",
    response_model=ApiResponse[None],
)
def delete_migration(admin: AdminDep, model_id: int, version: str = _VERSION_PATH):
    ModelMigrationController().delete_migration(model_id, version, admin=admin)
    return empty("Migración eliminada.")
