"""
Endpoints del catálogo de privilegios (``/privileges``).

Permite consultar, por motor, qué privilegios controla la plataforma y activarlos o
desactivarlos. El caso de uso principal: traer SOLO los privilegios activos de un
motor (``GET /privileges?engine=mysql&active=true``) para no mostrar los que la
plataforma no gestiona.
"""

from fastapi import APIRouter, Query

from app.controllers.privilege_controller import PrivilegeController
from app.core.auth import AdminDep
from app.schemas.privilege import PrivilegeOut, PrivilegeUpdate
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/privileges", tags=["Privileges"])


@router.get("", response_model=ApiResponse[list[PrivilegeOut]])
def list_privileges(
    admin: AdminDep,
    engine: str | None = Query(None, description="mysql | mariadb | postgresql"),
    active: bool | None = Query(
        None, description="true = solo los privilegios que la plataforma controla"
    ),
):
    items = PrivilegeController().list_privileges(engine=engine, active=active)
    return success(data=items)


@router.patch("/{privilege_id}", response_model=ApiResponse[PrivilegeOut])
def update_privilege(admin: AdminDep, privilege_id: int, payload: PrivilegeUpdate):
    priv = PrivilegeController().set_active(privilege_id, payload.is_active)
    msg = "Privilegio activado." if payload.is_active else "Privilegio desactivado."
    return success(data=priv, message=msg)
