"""
Endpoints de Servers.

CRUD del inventario (solo BD del gateway) + operaciones contra el servidor destino
(test-connection e introspección de estructura). Todos requieren admin autenticado.
"""

from fastapi import APIRouter, Query

from app.controllers.grant_controller import GrantController
from app.controllers.server_controller import ServerController
from app.controllers.server_user_controller import ServerUserController
from app.core.auth import AdminDep
from app.schemas.grant import GrantableRequest, GrantableResult
from app.schemas.server import ReconcileResult, ServerCreate, ServerOut, ServerUpdate
from app.schemas.server_user import (
    AddHostIn,
    AddHostOut,
    EnginePasswordChangeIn,
    EngineRevealPasswordIn,
    EngineUserActionOut,
    EngineUserCreateIn,
    GroupedEngineUsersOut,
    RevealedPasswordOut,
)
from app.services.db_admin.dtos import (
    ConnectionInfo,
    EngineUserInfo,
    StructureDump,
    TableSchema,
)
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


# --------- Usuarios del motor por IDENTIDAD (adoptados y NO adoptados) --------- #
# Estos endpoints operan por (server_id, username, host) directamente sobre el
# motor; NO requieren que el usuario esté adoptado en el inventario del gateway.
@router.get(
    "/{server_id}/users/grouped", response_model=ApiResponse[GroupedEngineUsersOut]
)
def list_users_grouped(admin: AdminDep, server_id: int):
    """
    Usuarios del motor AGRUPADOS por username (sin repetir el nombre por cada host) y
    cruzados con el inventario: cada host se marca adopted | unmanaged | orphan. En
    PostgreSQL ``supports_hosts=false`` y cada usuario tiene una sola identidad.
    """
    return success(data=ServerUserController().list_users_grouped(server_id))


@router.post(
    "/{server_id}/users",
    response_model=ApiResponse[EngineUserActionOut],
    status_code=201,
)
def create_engine_user(admin: AdminDep, server_id: int, payload: EngineUserCreateIn):
    """Crea un usuario en el motor (CREATE USER). Con ``adopt=true`` lo registra además en el inventario."""
    created = ServerUserController().create_user_by_identity(
        server_id, payload.model_dump(), admin=admin
    )
    return success(data=created, message="Usuario creado en el motor.")


@router.patch(
    "/{server_id}/users/password", response_model=ApiResponse[EngineUserActionOut]
)
def change_engine_user_password(
    admin: AdminDep, server_id: int, payload: EnginePasswordChangeIn
):
    """Cambia la contraseña de un usuario en el motor (esté o no adoptado). Si hay fila de inventario, se sincroniza."""
    updated = ServerUserController().set_password_by_identity(
        server_id, payload.model_dump(), admin=admin
    )
    return success(data=updated, message="Contraseña actualizada en el motor.")


@router.post(
    "/{server_id}/users/reveal-password",
    response_model=ApiResponse[RevealedPasswordOut],
)
def reveal_engine_user_password(
    admin: AdminDep, server_id: int, payload: EngineRevealPasswordIn
):
    """
    Revela la contraseña de un usuario — SOLO posible si el gateway la fijó y la guarda
    cifrada (create/rotación por el gateway). Una contraseña que el gateway nunca conoció
    es irrecuperable (el motor solo guarda un hash): 409. Acción auditada.
    """
    revealed = ServerUserController().reveal_password(
        server_id, payload.username, payload.host, admin=admin
    )
    return success(data=revealed)


@router.post(
    "/{server_id}/users/add-host",
    response_model=ApiResponse[AddHostOut],
    status_code=201,
)
def add_engine_user_host(admin: AdminDep, server_id: int, payload: AddHostIn):
    """
    Agrega un host a un usuario (clona la cuenta a ``new_host``). Solo MySQL/MariaDB
    (422 en PostgreSQL). ``reuse_password=true`` copia el hash de la cuenta origen;
    ``false`` exige ``new_password``. Con ``copy_grants=true`` replica sus permisos.
    """
    result = ServerUserController().add_host(server_id, payload.model_dump(), admin=admin)
    return success(
        data=result, message=f"Host '{payload.new_host}' agregado a '{payload.username}'."
    )


@router.delete("/{server_id}/users", response_model=ApiResponse[None])
def drop_engine_user(
    admin: AdminDep,
    server_id: int,
    username: str = Query(..., description="Username del usuario a eliminar del motor."),
    host: str = Query("%", description="Host de la identidad (ignorado en PostgreSQL)."),
    confirm_username: str | None = Query(
        None,
        description="Obligatorio: repetir el username exacto para confirmar el DROP USER en el motor.",
    ),
):
    """Elimina un usuario del motor (DROP USER) por identidad. Si hay fila de inventario, se borra también."""
    ServerUserController().drop_user_by_identity(
        server_id, username, host, confirm_username=confirm_username, admin=admin
    )
    return empty("Usuario eliminado del motor.")


@router.get("/{server_id}/reconcile", response_model=ApiResponse[ReconcileResult])
def reconcile(admin: AdminDep, server_id: int):
    """
    Cruza el plano EN VIVO (motor) con el INVENTARIO (gateway): marca cada BD/usuario
    como managed | unmanaged (adoptable) | orphan (borrado por fuera). Read-only.
    """
    return success(data=ServerController().reconcile(server_id))


@router.get(
    "/{server_id}/databases/{database}/snapshot",
    response_model=ApiResponse[StructureDump],
)
def snapshot_database(
    admin: AdminDep, server_id: int, database: str, include_data_stats: bool = False
):
    """
    Snapshot estructural EN VIVO de una BD (tablas, vistas, rutinas, triggers, etc.).
    Solo estructura, nunca filas. Es la PREVIEW (no persiste): para fijarlo como
    blueprint baseline use POST /database-models/from-snapshot.

    Con ``?include_data_stats=true`` agrega ``table_stats`` (estimación de filas y si
    tiene PK por tabla) para que el frontend informe la selección de datos-semilla. Es
    opt-in porque implica una consulta extra de catálogo por tabla.
    """
    ctrl = ServerController()
    dump = ctrl.snapshot(server_id, database)
    if include_data_stats:
        dump = dump.model_copy(update={"table_stats": ctrl.table_stats(server_id, database)})
    return success(data=dump)


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
