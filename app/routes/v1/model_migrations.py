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
    MigrationDeleteOut,
    MigrationDeletePlanOut,
    MigrationValidateIn,
    MigrationValidateOut,
    ModelMigrationCreate,
    ModelMigrationOut,
    ModelMigrationPatch,
    ModelMigrationSummary,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

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
    environment_id: int | None = Query(
        None,
        ge=1,
        description=(
            "Acota el lote a un entorno. Es lo que convierte 'aplicá a todo' en 'aplicá a "
            "desarrollo'. Se filtra ANTES del tope, así que 'max_databases' no se consume con "
            "BDs de otros entornos. Combinado con 'database_ids', un id fuera del entorno "
            "devuelve 422 en vez de desaparecer del lote en silencio."
        ),
    ),
    force: bool = Query(
        False,
        description=(
            "Override de cuarentena en cada BD. **NO** saltea el bloqueo de migraciones "
            "destructivas por entorno: ese guard no tiene override."
        ),
    ),
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
):
    """
    Aplica las pendientes del blueprint a TODAS sus BDs (síncrono, acotado) 🔌.

    **Captura de resultados**: una versión pendiente con 'capture_selects=true' SIN revisar
    frena solo a esa BD — el rechazo llega como ítem dentro de una respuesta 200, con
    'error_code=migration.capture_unreviewed' y las versiones en 'unreviewed_capture'. No hay
    consentimiento por corrida: se retiró (entre otras razones, porque un único query param
    autorizaba N bases de entornos distintos, que es lo contrario de lo que decía proteger).
    """
    result = ManagedMigrationController().apply_all(
        model_id, max_databases=max_databases, database_ids=database_ids,
        environment_id=environment_id,
        force=force, dry_run=dry_run,
        on_failure=on_failure, admin=admin,
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


@router.get(
    "/{model_id}/migrations/{version}/delete-plan",
    response_model=ApiResponse[MigrationDeletePlanOut],
)
def plan_delete_migration(admin: AdminDep, model_id: int, version: str = _VERSION_PATH):
    """
    Preview del borrado: qué versiones se renumeran, qué punteros se mueven y qué lo bloquea.

    Solo lectura, pero **no es gratis**: abre conexión a cada BD del blueprint para leer su
    versión en vivo. Es lo que hace que el veredicto sea autoritativo y no la caché que pinta
    el listado.

    Emite ``confirm_token`` solo si el plan implica mover punteros. Si no hay nada que
    stampear, el ``DELETE`` sigue sin exigir confirmación.
    """
    return success(data=ModelMigrationController().plan_delete(model_id, version))


@router.delete(
    "/{model_id}/migrations/{version}",
    response_model=ApiResponse[MigrationDeleteOut],
)
def delete_migration(
    admin: AdminDep,
    model_id: int,
    version: str = _VERSION_PATH,
    confirm_token: str | None = Query(
        None,
        description=(
            "Token del preview 'delete-plan'. OBLIGATORIO solo si el borrado implica mover "
            "el puntero de alguna BD (escritura remota). Sin BDs adelante no se pide."
        ),
    ),
):
    """
    Elimina una versión, renumerando las posteriores y moviendo el puntero de las BDs que
    estén adelante.

    **No ejecuta ningún SQL del blueprint**: no es un rollback. Lo único que se escribe en
    cada BD destino es su tabla de versión de Alembic.

    Se puede eliminar si y solo si **ninguna BD está parada exactamente en esa versión**
    (409 ``model_migration.version_in_use``). Las que están adelante siguen el renombre: su
    puntero pasa a la etiqueta nueva de la MISMA migración. Las que están atrás no se tocan.

    Una BD del blueprint cuya versión no se pueda leer bloquea la operación
    (409 ``model_migration.unreadable_databases``): sin esa lectura no se puede descartar que
    esté parada acá ni moverle el puntero, y renumerar la dejaría huérfana.
    """
    result = ModelMigrationController().delete_migration(
        model_id, version, confirm_token_value=confirm_token, admin=admin
    )
    return success(data=result, message="Migración eliminada.")
