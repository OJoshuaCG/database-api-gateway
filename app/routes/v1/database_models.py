"""
Endpoints de DatabaseModels (blueprints/categorías).

CRUD sobre el inventario del gateway. Dos rutas SÍ tocan motores destino y por eso tienen
rate limit propio: ``/from-snapshot`` (fotografía una BD existente) y
``/{id}/databases/refresh`` (relee la versión real de cada BD del blueprint).
"""

from fastapi import APIRouter, Request

from app.controllers.database_model_controller import DatabaseModelController
from app.controllers.model_migration_controller import ModelMigrationController
from app.core.authz import (
    BlueprintsRead,
    BlueprintsWrite,
    DatabasesRead,
    DatabasesWrite,
    assert_capability,
)
from app.core.limiter import limiter
from app.schemas.database_model import (
    DatabaseModelCreate,
    DatabaseModelOut,
    DatabaseModelUpdate,
    FromSnapshotIn,
    FromSnapshotOut,
    ModelDatabaseStatusOut,
    RenameSlugIn,
    RenameSlugOut,
    RenameSlugPlanOut,
)
from app.services import audit
from app.services.capability_catalog import Capability
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/database-models", tags=["Database Models"])


@router.get("", response_model=ApiResponse[list[DatabaseModelOut]])
def list_models(actor: BlueprintsRead, pagination: PaginationDep):
    items, total = DatabaseModelController().list_models(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[DatabaseModelOut], status_code=201)
def create_model(actor: BlueprintsWrite, payload: DatabaseModelCreate):
    created = DatabaseModelController().create_model(payload.model_dump(), admin=actor)
    return success(data=created, message="Blueprint creado.")


@router.post("/from-snapshot", response_model=ApiResponse[FromSnapshotOut], status_code=201)
@limiter.limit("10/minute")
def create_from_snapshot(request: Request, actor: BlueprintsWrite, payload: FromSnapshotIn):
    """
    Crea un blueprint NUEVO cuyo baseline (v0001) es el snapshot de una BD existente
    (Plan 09, modo 3). Si incluye objetos procedurales, el baseline queda atado a su motor de
    origen (no aplicable cross-engine).

    **``data_tables`` exige ``blueprints.captures``, no ``write``.** Sin ese parámetro esto lee
    solo estructura; con él EXTRAE FILAS de la BD de origen y las deja como datos-semilla
    dentro de una migración del blueprint — o sea, dentro de algo que después lee cualquiera
    con ``blueprints.read``. Es el mismo tipo de camino que ``capture_selects`` y por eso pide
    la misma capacidad: es la que gobierna que el gateway persista datos de negocio.
    """
    if payload.data_tables:
        assert_capability(actor, Capability.BLUEPRINTS_CAPTURES)
    result = ModelMigrationController().create_from_snapshot(payload.model_dump(), admin=actor)
    return success(data=result, message="Blueprint baseline creado desde snapshot.")


@router.get("/{model_id}", response_model=ApiResponse[DatabaseModelOut])
def get_model(actor: BlueprintsRead, model_id: int):
    return success(data=DatabaseModelController().get_model(model_id))


@router.patch("/{model_id}", response_model=ApiResponse[DatabaseModelOut])
def update_model(actor: BlueprintsWrite, model_id: int, payload: DatabaseModelUpdate):
    updated = DatabaseModelController().update_model(
        model_id, payload.model_dump(exclude_unset=True), admin=actor
    )
    return success(data=updated, message="Blueprint actualizado.")


@router.post(
    "/{model_id}/rename-slug/plan", response_model=ApiResponse[RenameSlugPlanOut]
)
@limiter.limit("10/minute")
def rename_slug_plan(
    request: Request, actor: BlueprintsWrite, model_id: int, payload: RenameSlugIn
):
    """
    Preflight del renombrado del slug. **No escribe nada**, ni en el gateway ni en un motor.

    Abre una conexión por servidor del blueprint para preguntar, en cada BD gestionada, si
    tiene la tabla de versión vieja y si el nombre nuevo está libre. Por eso tiene rate
    limit propio aunque sea una lectura.

    Clasifica cada base en `rename`, `skip` (nunca fue posicionada, no hay tabla),
    `conflict` (el nombre destino YA existe ahí) o `unreachable`. Los dos últimos son
    **bloqueantes de toda la operación**, no solo de esa base: el gateway apunta a UN
    nombre, así que dejar medio parque renombrado deja a la otra mitad huérfana.

    Si hay bases que renombrar y nada bloquea, emite `confirm_token` atado a la huella del
    parque. Si no hay ninguna, no emite token: uno que no hace falta entrena a mandarlo
    siempre.
    """
    result = DatabaseModelController().rename_slug_plan(model_id, payload.new_slug)
    return success(data=result, message="Plan de renombrado del slug.")


@router.post("/{model_id}/rename-slug", response_model=ApiResponse[RenameSlugOut])
@limiter.limit("3/minute")
def rename_slug(
    request: Request, actor: BlueprintsWrite, model_id: int, payload: RenameSlugIn
):
    """
    Cambia el `slug` del blueprint y **renombra su tabla de versión en cada BD gestionada**.

    El `slug` nombra la tabla de Alembic (`_gw_v_{slug}`) DENTRO de cada base, así que
    cambiarlo son N escrituras remotas sobre bases de terceros, sin transacción compartida.
    Por eso no se acepta por el `PATCH` común, que sigue respondiendo 409.

    **El orden importa y no es negociable**: primero los renames remotos, y el slug del
    gateway se actualiza último. Al revés, un fallo dejaría al gateway apuntando a un nombre
    que no existe en ningún motor, toda la cadena figuraría pendiente y un `apply` la
    reaplicaría desde la primera versión.

    Ante un fallo a mitad se compensa renombrando de vuelta, y el 409 trae `not_compensated`
    con las bases que quedaron con el nombre nuevo y hay que reparar a mano. El slug del
    blueprint **no se modifica** en ese caso.
    """
    result = DatabaseModelController().rename_slug(
        model_id, payload.new_slug, confirm_token=payload.confirm_token, admin=actor
    )
    return success(data=result, message="Slug renombrado.")


@router.delete("/{model_id}", response_model=ApiResponse[None])
def delete_model(actor: BlueprintsWrite, model_id: int):
    DatabaseModelController().delete_model(model_id, admin=actor)
    return empty("Blueprint eliminado.")


@router.get(
    "/{model_id}/databases", response_model=ApiResponse[list[ModelDatabaseStatusOut]]
)
def list_model_databases(actor: DatabasesRead, model_id: int):
    """
    BDs del blueprint **con su estado de despliegue** (versión actual, pendientes, parcial).

    Antes esto exigía una llamada por BD a ``/migrations/status``, y cada una abría una
    conexión al motor. Los tres campos nuevos salen de datos que el gateway ya tiene, así que
    la tabla entera cuesta 3 queries locales y **cero conexiones**.

    Sin rate limit propio a propósito: es una lectura barata que la UI refresca al reenfocar
    la ventana. Lo que cuesta es el refresco, y ese tiene su propio endpoint.
    """
    return success(data=DatabaseModelController().list_model_databases(model_id))


@router.post(
    "/{model_id}/databases/refresh",
    response_model=ApiResponse[list[ModelDatabaseStatusOut]],
)
@limiter.limit("10/minute")
def refresh_model_databases(request: Request, actor: DatabasesWrite, model_id: int):
    """
    🔌 Relee la versión REAL de cada BD del blueprint y resincroniza la copia del gateway.

    Es la vía para corregir el dato si alguien migró una BD por fuera del gateway. Va como
    ``POST`` y no como ``?refresh=true`` sobre el ``GET`` porque **tiene efectos**: abre
    conexiones y reescribe ``model_version``. Colgarlo del GET obligaba además a limitar por
    tasa la lectura barata, que es el 99 % de las llamadas.

    Devuelve la lista ya actualizada para que el cliente no tenga que pedirla otra vez.
    """
    data = DatabaseModelController().list_model_databases(model_id, refresh=True)
    audit.record(
        "database_model.databases.refresh",
        admin=actor,
        target_type="database_model",
        target_id=model_id,
        touched_engine=True,
        detail=f"versión resincronizada desde el motor en {len(data)} BD(s)",
    )
    return success(data=data)
