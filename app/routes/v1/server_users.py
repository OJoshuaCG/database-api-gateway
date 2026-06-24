"""
Endpoints de ServerUsers (usuarios del motor).

Recurso top-level ``/server-users`` (no se anida bajo ``/servers/{id}/users`` para
no chocar con la introspección en vivo de usuarios del motor de la Iteración 1).
Se filtra por servidor con ``?server_id=``.

Flags que tocan el motor:
- ``?provision=true`` en POST  → CREATE USER en el motor.
- ``?provision=true`` en PATCH → ALTER USER (solo si se envía nuevo password).
- ``?drop_remote=true`` en DELETE → DROP USER en el motor.
"""

from fastapi import APIRouter, Query

from app.controllers.grant_controller import GrantController
from app.controllers.server_user_controller import ServerUserController
from app.core.auth import AdminDep
from app.schemas.grant import ApplyProfileRequest, ApplyProfileResult, GrantInfo, GrantRequest, GrantableResult, RevokeRequest
from app.schemas.managed_database import ManagedDatabaseOut
from app.schemas.server_user import ServerUserCreate, ServerUserFullCreate, ServerUserFullOut, ServerUserOut, ServerUserUpdate
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/server-users", tags=["Server Users"])


@router.get("", response_model=ApiResponse[list[ServerUserOut]])
def list_server_users(
    admin: AdminDep,
    pagination: PaginationDep,
    server_id: int | None = Query(None, ge=1),
):
    items, total = ServerUserController().list_server_users(
        server_id=server_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ServerUserOut], status_code=201)
def create_server_user(
    admin: AdminDep, payload: ServerUserCreate, provision: bool = Query(False)
):
    created = ServerUserController().create_server_user(
        payload.model_dump(), provision=provision, admin=admin
    )
    msg = "Usuario creado en el inventario."
    if provision:
        msg = "Usuario creado y aprovisionado en el motor."
    return success(data=created, message=msg)


@router.get("/{user_id}", response_model=ApiResponse[ServerUserOut])
def get_server_user(admin: AdminDep, user_id: int):
    return success(data=ServerUserController().get_server_user(user_id))


@router.patch("/{user_id}", response_model=ApiResponse[ServerUserOut])
def update_server_user(
    admin: AdminDep,
    user_id: int,
    payload: ServerUserUpdate,
    provision: bool = Query(False),
):
    updated = ServerUserController().update_server_user(
        user_id, payload.model_dump(exclude_unset=True), provision=provision, admin=admin
    )
    return success(data=updated, message="Usuario actualizado.")


@router.delete("/{user_id}", response_model=ApiResponse[None])
def delete_server_user(
    admin: AdminDep,
    user_id: int,
    drop_remote: bool = Query(False),
    confirm_username: str | None = Query(
        None,
        description="Obligatorio si drop_remote=true: repetir el username exacto para confirmar el DROP USER en el motor.",
    ),
):
    ServerUserController().delete_server_user(
        user_id, drop_remote=drop_remote, confirm_username=confirm_username, admin=admin
    )
    return empty("Usuario eliminado.")


@router.get(
    "/{user_id}/databases", response_model=ApiResponse[list[ManagedDatabaseOut]]
)
def list_user_databases(admin: AdminDep, user_id: int):
    return success(data=ServerUserController().list_user_databases(user_id))


# ----------------------- Grants granulares -------------------------------- #
@router.get("/{user_id}/grants", response_model=ApiResponse[list[GrantInfo]])
def list_grants(
    admin: AdminDep,
    user_id: int,
    database: str | None = Query(
        None,
        description=(
            "Obligatorio en PostgreSQL: base de datos donde se consultan los grants "
            "de objeto (tablas/columnas/secuencias/rutinas). En MySQL/MariaDB se ignora."
        ),
    ),
):
    grants = GrantController().list_grants(user_id, database=database)
    return success(data=grants)


@router.post("/{user_id}/grants", response_model=ApiResponse[dict])
def grant_object(admin: AdminDep, user_id: int, payload: GrantRequest):
    result = GrantController().grant_object(user_id, payload, admin=admin)
    priv_summary = ", ".join(payload.privileges)
    return success(
        data=result,
        message=f"Privilegio(s) otorgado(s): {priv_summary} a nivel {payload.level.value}.",
    )


@router.delete("/{user_id}/grants", response_model=ApiResponse[None])
def revoke_object(admin: AdminDep, user_id: int, payload: RevokeRequest):
    GrantController().revoke_object(user_id, payload, admin=admin)
    priv_summary = ", ".join(payload.privileges)
    return empty(f"Privilegio(s) revocado(s): {priv_summary} a nivel {payload.level.value}.")


@router.post(
    "/{user_id}/apply-profile/{profile_id}",
    response_model=ApiResponse[ApplyProfileResult],
)
def apply_profile(
    admin: AdminDep,
    user_id: int,
    profile_id: int,
    payload: ApplyProfileRequest,
):
    """Aplica un perfil de permisos guardado al usuario. Los niveles sin mapeo se omiten."""
    result = GrantController().apply_profile(user_id, profile_id, payload, admin=admin)
    msg = f"Perfil '{result.profile_name}' aplicado: {result.grants_applied} grant(s)."
    if result.errors:
        msg += f" {len(result.errors)} error(es) parciales."
    return success(data=result, message=msg)


# ──────────────────── Endpoint unificado crear + grants ────────────────────── #
@router.post(
    "/provision",
    response_model=ApiResponse[ServerUserFullOut],
    status_code=201,
    summary="Crear usuario + aprovisionar en motor + aplicar grants iniciales",
)
def provision_with_grants(admin: AdminDep, payload: ServerUserFullCreate):
    """
    Endpoint unificado: crea el usuario en el inventario, lo aprovisiona en el motor
    destino (CREATE USER) y aplica los ``initial_grants`` indicados. Los grants son
    best-effort: un fallo en un grant no revierte la creación del usuario.
    """
    result = ServerUserController().provision_with_grants(
        payload.model_dump(exclude={"initial_grants"}),
        payload.initial_grants,
        admin=admin,
    )
    msg = f"Usuario '{payload.username}' aprovisionado."
    if result.grants_applied:
        msg += f" {result.grants_applied} grant(s) aplicado(s)."
    failed = [r for r in result.grant_results if not r.success]
    if failed:
        msg += f" {len(failed)} grant(s) fallido(s)."
    return success(data=result, message=msg)
