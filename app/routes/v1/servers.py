"""
Endpoints de Servers.

CRUD del inventario (solo BD del gateway) + operaciones contra el servidor destino
(test-connection e introspección de estructura). Todos requieren admin autenticado.
"""

from fastapi import APIRouter

from app.controllers.grant_controller import GrantController
from app.controllers.server_controller import ServerController
from app.core.auth import AdminDep
from app.schemas.grant import GrantableRequest, GrantableResult
from app.schemas.server import ServerCreate, ServerOut, ServerUpdate
from app.services.db_admin.dtos import ConnectionInfo, EngineUserInfo, TableSchema
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/servers", tags=["Servers"])


# ----------------------------- CRUD (gateway) ----------------------------- #
@router.get("", response_model=ApiResponse[list[ServerOut]])
def list_servers(admin: AdminDep, pagination: PaginationDep):
    items, total = ServerController().list_servers(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ServerOut], status_code=201)
def create_server(admin: AdminDep, payload: ServerCreate):
    created = ServerController().create_server(payload.model_dump())
    return success(data=created, message="Servidor registrado exitosamente.")


@router.get("/{server_id}", response_model=ApiResponse[ServerOut])
def get_server(admin: AdminDep, server_id: int):
    return success(data=ServerController().get_server(server_id))


@router.patch("/{server_id}", response_model=ApiResponse[ServerOut])
def update_server(admin: AdminDep, server_id: int, payload: ServerUpdate):
    updated = ServerController().update_server(
        server_id, payload.model_dump(exclude_unset=True)
    )
    return success(data=updated, message="Servidor actualizado.")


@router.delete("/{server_id}", response_model=ApiResponse[None])
def delete_server(admin: AdminDep, server_id: int):
    ServerController().delete_server(server_id)
    return empty("Servidor eliminado.")


# ----------------------- Operaciones en el destino ------------------------ #
@router.post("/{server_id}/test-connection", response_model=ApiResponse[ConnectionInfo])
def test_connection(admin: AdminDep, server_id: int):
    return success(data=ServerController().test_connection(server_id))


@router.get("/{server_id}/databases", response_model=ApiResponse[list[str]])
def list_databases(admin: AdminDep, server_id: int):
    return success(data=ServerController().list_databases(server_id))


@router.get("/{server_id}/users", response_model=ApiResponse[list[EngineUserInfo]])
def list_users(admin: AdminDep, server_id: int):
    return success(data=ServerController().list_users(server_id))


@router.get(
    "/{server_id}/databases/{database}/tables",
    response_model=ApiResponse[list[str]],
)
def list_tables(admin: AdminDep, server_id: int, database: str):
    return success(data=ServerController().list_tables(server_id, database))


@router.get(
    "/{server_id}/databases/{database}/tables/{table}/schema",
    response_model=ApiResponse[TableSchema],
)
def get_table_schema(admin: AdminDep, server_id: int, database: str, table: str):
    return success(data=ServerController().get_table_schema(server_id, database, table))


@router.post("/{server_id}/grantable", response_model=ApiResponse[GrantableResult])
def check_grantable(admin: AdminDep, server_id: int, payload: GrantableRequest):
    """Verifica si la credencial admin del servidor puede delegar los privilegios indicados."""
    can = GrantController().check_grantable(server_id, payload)
    result = GrantableResult(
        can_grant=can,
        level=payload.level,
        privileges=payload.privileges,
    )
    return success(data=result)
