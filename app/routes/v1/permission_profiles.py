"""
Endpoints de Perfiles de Permisos (`/permission-profiles`).

Un perfil es una plantilla de privilegios **por motor** (ver decisión en
docs/plans/07). Permite gestionar (CRUD) plantillas reutilizables para luego asignarlas
rápido a un usuario. Todo requiere admin autenticado.
"""

from fastapi import APIRouter, Query

from app.controllers.permission_profile_controller import PermissionProfileController
from app.core.auth import AdminDep
from app.schemas.permission_profile import (
    PermissionProfileCreate,
    PermissionProfileOut,
    PermissionProfileUpdate,
)
from app.utils.response import ApiResponse, empty, success

router = APIRouter(prefix="/permission-profiles", tags=["Permission Profiles"])


@router.get("", response_model=ApiResponse[list[PermissionProfileOut]])
def list_profiles(
    admin: AdminDep,
    engine: str | None = Query(None, description="mysql | mariadb | postgresql"),
    active: bool | None = Query(None),
):
    data = PermissionProfileController().list_profiles(engine=engine, active=active)
    return success(data=data)


@router.post("", response_model=ApiResponse[PermissionProfileOut], status_code=201)
def create_profile(admin: AdminDep, payload: PermissionProfileCreate):
    created = PermissionProfileController().create_profile(payload.model_dump())
    return success(data=created, message="Perfil de permisos creado.")


@router.get("/{profile_id}", response_model=ApiResponse[PermissionProfileOut])
def get_profile(admin: AdminDep, profile_id: int):
    return success(data=PermissionProfileController().get_profile(profile_id))


@router.patch("/{profile_id}", response_model=ApiResponse[PermissionProfileOut])
def update_profile(admin: AdminDep, profile_id: int, payload: PermissionProfileUpdate):
    updated = PermissionProfileController().update_profile(
        profile_id, payload.model_dump(exclude_unset=True)
    )
    return success(data=updated, message="Perfil de permisos actualizado.")


@router.delete("/{profile_id}", response_model=ApiResponse[None])
def delete_profile(admin: AdminDep, profile_id: int):
    PermissionProfileController().delete_profile(profile_id)
    return empty("Perfil de permisos eliminado.")
