"""
Endpoints de DatabaseModels (blueprints/categorías).

CRUD puro sobre el inventario del gateway (no toca ningún motor).
"""

from fastapi import APIRouter, Request

from app.controllers.database_model_controller import DatabaseModelController
from app.controllers.model_migration_controller import ModelMigrationController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.database_model import (
    DatabaseModelCreate,
    DatabaseModelOut,
    DatabaseModelUpdate,
    FromSnapshotIn,
    FromSnapshotOut,
    ModelDatabaseStatusOut,
)
from app.services import audit
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/database-models", tags=["Database Models"])


@router.get("", response_model=ApiResponse[list[DatabaseModelOut]])
def list_models(admin: AdminDep, pagination: PaginationDep):
    items, total = DatabaseModelController().list_models(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[DatabaseModelOut], status_code=201)
def create_model(admin: AdminDep, payload: DatabaseModelCreate):
    created = DatabaseModelController().create_model(payload.model_dump(), admin=admin)
    return success(data=created, message="Blueprint creado.")


@router.post("/from-snapshot", response_model=ApiResponse[FromSnapshotOut], status_code=201)
@limiter.limit("10/minute")
def create_from_snapshot(request: Request, admin: AdminDep, payload: FromSnapshotIn):
    """
    Crea un blueprint NUEVO cuyo baseline (v0001) es el snapshot estructural de una BD
    existente (Plan 09, modo 3). Lee la estructura del motor (nunca filas) y la fija
    como migración baseline. Si incluye objetos procedurales, el baseline queda atado a
    su motor de origen (no aplicable cross-engine).
    """
    result = ModelMigrationController().create_from_snapshot(payload.model_dump(), admin=admin)
    return success(data=result, message="Blueprint baseline creado desde snapshot.")


@router.get("/{model_id}", response_model=ApiResponse[DatabaseModelOut])
def get_model(admin: AdminDep, model_id: int):
    return success(data=DatabaseModelController().get_model(model_id))


@router.patch("/{model_id}", response_model=ApiResponse[DatabaseModelOut])
def update_model(admin: AdminDep, model_id: int, payload: DatabaseModelUpdate):
    updated = DatabaseModelController().update_model(
        model_id, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=updated, message="Blueprint actualizado.")


@router.delete("/{model_id}", response_model=ApiResponse[None])
def delete_model(admin: AdminDep, model_id: int):
    DatabaseModelController().delete_model(model_id, admin=admin)
    return empty("Blueprint eliminado.")


@router.get(
    "/{model_id}/databases", response_model=ApiResponse[list[ModelDatabaseStatusOut]]
)
def list_model_databases(admin: AdminDep, model_id: int):
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
def refresh_model_databases(request: Request, admin: AdminDep, model_id: int):
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
        admin=admin,
        target_type="database_model",
        target_id=model_id,
        touched_engine=True,
        detail=f"versión resincronizada desde el motor en {len(data)} BD(s)",
    )
    return success(data=data)
