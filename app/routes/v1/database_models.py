"""
Endpoints de DatabaseModels (blueprints/categorías).

CRUD puro sobre el inventario del gateway (no toca ningún motor).
"""

from fastapi import APIRouter

from app.controllers.database_model_controller import DatabaseModelController
from app.core.auth import AdminDep
from app.schemas.database_model import (
    DatabaseModelCreate,
    DatabaseModelOut,
    DatabaseModelUpdate,
)
from app.schemas.managed_database import ManagedDatabaseOut
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
    "/{model_id}/databases", response_model=ApiResponse[list[ManagedDatabaseOut]]
)
def list_model_databases(admin: AdminDep, model_id: int):
    return success(data=DatabaseModelController().list_model_databases(model_id))
