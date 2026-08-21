"""
Endpoints de migraciones de blueprints (``/database-models/{id}/migrations``).

CRUD de migraciones sobre el inventario del gateway (NO toca motores) y el apply
masivo (síncrono, acotado) sobre todas las BDs del blueprint.
"""

from fastapi import APIRouter, Path, Query, Request

from app.controllers.managed_migration_controller import ManagedMigrationController
from app.controllers.model_migration_controller import ModelMigrationController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.model_migration import (
    ApplyAllOut,
    MigrationValidateIn,
    MigrationValidateOut,
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
@limiter.limit("3/minute")
def apply_all(
    request: Request,
    admin: AdminDep,
    model_id: int,
    max_databases: int = Query(10, ge=1, le=100, description="Cota de BDs a procesar"),
    database_ids: list[int] | None = Query(
        None,
        description=(
            "Destinos concretos. Sin él se aplica a TODAS las BDs del blueprint (hasta "
            "'max_databases'). Un id que no pertenezca al blueprint devuelve 422 con la lista: "
            "es la frontera que impide aplicar sus migraciones a una BD ajena."
        ),
    ),
    force: bool = Query(False, description="Override de cuarentena en cada BD."),
    dry_run: bool = Query(
        False, description="No aplica: devuelve el plan por BD (pendientes)."
    ),
    on_failure: str = Query(
        "auto",
        description=(
            "'auto' | 'reconcile' | 'leave' — qué hacer si una migración falla a mitad en "
            "alguna BD. Se valida en el controlador contra la misma lista que el apply por BD."
        ),
    ),
    allow_result_capture: bool = Query(
        False,
        description=(
            "Consentimiento explícito para aplicar versiones con 'capture_selects=true': "
            "esos SELECT guardan filas de CADA BD del blueprint (cifradas) en el gateway. "
            "Sin este flag, una versión con captura pendiente devuelve 409 por BD."
        ),
    ),
):
    result = ManagedMigrationController().apply_all(
        model_id, max_databases=max_databases, database_ids=database_ids,
        force=force, dry_run=dry_run,
        on_failure=on_failure, allow_result_capture=allow_result_capture, admin=admin,
    )
    msg = "Plan masivo (dry-run)." if dry_run else "Aplicación masiva ejecutada."
    return success(data=result, message=msg)


@router.post(
    "/{model_id}/migrations/validate",
    response_model=ApiResponse[MigrationValidateOut],
)
@limiter.limit("20/minute")
def validate_migration(
    request: Request,
    admin: AdminDep,
    model_id: int,
    payload: MigrationValidateIn,
):
    """
    Analiza el SQL de una migración ANTES de aplicarla.

    Se declara ANTES de ``/{version}`` a propósito: FastAPI resuelve por orden de
    declaración y "validate" no casa con el ``pattern`` de versión, así que puesto después
    daría 422 en vez de llegar aquí.

    Sin ``managed_database_id`` es análisis estático puro (no abre conexión) y el rate
    limit sobra; con él se consulta el catálogo de esa BD, y por eso el límite es común a
    los dos casos.
    """
    result = ModelMigrationController().validate_migration(
        model_id, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=result, message="SQL analizado.")


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
