"""
Endpoints de Servers.

CRUD del inventario (solo BD del gateway) + operaciones contra el servidor destino
(test-connection e introspección de estructura). Todos requieren admin autenticado.
"""

from fastapi import APIRouter, Query, Request

from app.controllers.grant_controller import GrantController
from app.controllers.query_console_controller import QueryConsoleController
from app.controllers.server_controller import ServerController
from app.controllers.server_database_controller import ServerDatabaseController
from app.controllers.server_user_controller import ServerUserController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.grant import GrantableRequest, GrantableResult
from app.schemas.query_console import (
    QueryExecuteIn,
    QueryExecuteOut,
    QueryHistoryOut,
    QueryPreviewIn,
    QueryPreviewOut,
)
from app.schemas.server_database import (
    DatabaseCreateIn,
    DatabaseCreateOut,
    DatabaseDropIn,
    DatabaseDropOut,
    DatabaseGranteesOut,
    DropPreviewOut,
)
from app.schemas.server import ReconcileResult, ServerCreate, ServerOut, ServerUpdate
from app.schemas.server_user import (
    AddHostIn,
    AddHostOut,
    AdoptAllHostsIn,
    BatchAdoptOut,
    DefineKnownPasswordIn,
    EnginePasswordChangeAllHostsIn,
    EnginePasswordChangeIn,
    EngineRevealPasswordIn,
    EngineUserActionOut,
    EngineUserCreateIn,
    GroupedEngineUsersOut,
    KnownPasswordSetOut,
    PasswordChangeBatchOut,
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
    "/{server_id}/users/adopt-all-hosts",
    response_model=ApiResponse[BatchAdoptOut],
    status_code=201,
)
def adopt_engine_user_all_hosts(admin: AdminDep, server_id: int, payload: AdoptAllHostsIn):
    """
    Adopta TODAS las identidades en vivo de un username en una sola operación (nunca
    ejecuta CREATE USER). Con ``known_password`` opcional, la guarda cifrada en todas
    las filas adoptadas para habilitar reveal-password (tampoco ejecuta ALTER USER).
    """
    result = ServerUserController().adopt_user_all_hosts(
        server_id, payload.model_dump(), admin=admin
    )
    return success(
        data=result, message=f"{result.adopted}/{result.total_hosts} hosts adoptados."
    )


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


@router.patch(
    "/{server_id}/users/password-all-hosts",
    response_model=ApiResponse[PasswordChangeBatchOut],
)
def change_engine_user_password_all_hosts(
    admin: AdminDep, server_id: int, payload: EnginePasswordChangeAllHostsIn
):
    """
    Rota la contraseña REAL (ALTER USER/ROLE) en TODOS los hosts en vivo de un
    username. Irreversible sobre N cuentas a la vez: exige ``confirm_username`` igual
    al username (doble intención, mismo patrón que DROP USER). Fail-tolerant por host:
    un fallo en uno no aborta el resto (ver ``results`` para el detalle por host).
    """
    result = ServerUserController().set_password_by_identity_all_hosts(
        server_id, payload.model_dump(), admin=admin
    )
    return success(
        data=result, message=f"{result.updated}/{result.total_hosts} hosts rotados."
    )


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
    "/{server_id}/users/define-password",
    response_model=ApiResponse[KnownPasswordSetOut],
)
def define_engine_user_known_password(
    admin: AdminDep, server_id: int, payload: DefineKnownPasswordIn
):
    """
    Registra una contraseña YA conocida por el admin humano SIN ejecutar ALTER USER —
    solo la cifra y guarda para habilitar reveal-password después. Distinto de
    ``PATCH /users/password[-all-hosts]``, que sí ejecutan ALTER USER/ROLE real en el
    motor. ``scope='all_hosts'`` aplica a todos los hosts en vivo del username;
    ``overwrite=true`` es obligatorio para reemplazar una contraseña ya conocida.
    """
    result = ServerUserController().set_known_password(
        server_id, payload.model_dump(), admin=admin
    )
    return success(
        data=result, message=f"Contraseña definida en {result.updated} identidad(es)."
    )


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


# --------- Ciclo de vida de BDs a NIVEL SERVIDOR (crear / borrar / usuarios) --------- #
# Operan por (server_id, database) directamente sobre el motor; NO requieren que la BD
# esté adoptada en el inventario. Compatibles con MySQL/MariaDB y PostgreSQL.
@router.post(
    "/{server_id}/databases",
    response_model=ApiResponse[DatabaseCreateOut],
    status_code=201,
)
@limiter.limit("10/minute")
def create_database(
    request: Request, admin: AdminDep, server_id: int, payload: DatabaseCreateIn
):
    """Crea una BD en el servidor. Con ``register=true`` (requiere ``owner_id``) además la registra."""
    result = ServerDatabaseController().create_database(
        server_id,
        name=payload.name,
        charset=payload.charset,
        collation=payload.collation,
        owner=payload.owner,
        register=payload.register_inventory,
        owner_id=payload.owner_id,
        notes=payload.notes,
        admin=admin,
    )
    return success(data=result, message="Base de datos creada.")


@router.post(
    "/{server_id}/databases/{database}/drop-preview",
    response_model=ApiResponse[DropPreviewOut],
)
@limiter.limit("10/minute")
def drop_database_preview(
    request: Request, admin: AdminDep, server_id: int, database: str
):
    """
    Paso 1 del borrado: valida la BD, corre guards y devuelve un ``confirm_token`` firmado
    (TTL 2 min), el conteo de conexiones activas y si está en el inventario. NO borra nada.
    """
    return success(
        data=ServerDatabaseController().drop_preview(server_id, database, admin=admin)
    )


@router.delete(
    "/{server_id}/databases/{database}",
    response_model=ApiResponse[DatabaseDropOut],
)
@limiter.limit("3/minute")
def drop_database(
    request: Request, admin: AdminDep, server_id: int, database: str, payload: DatabaseDropIn
):
    """
    Paso 2 del borrado (IRREVERSIBLE): exige ``confirm_target_name`` == nombre real +
    ``confirm_token`` vigente. Si la BD está en el inventario, también borra su registro.
    """
    result = ServerDatabaseController().drop_database(
        server_id,
        database,
        confirm_target_name=payload.confirm_target_name,
        confirm_token_value=payload.confirm_token,
        force_disconnect=payload.force_disconnect,
        admin=admin,
    )
    return success(data=result, message="Base de datos eliminada.")


@router.get(
    "/{server_id}/databases/{database}/users",
    response_model=ApiResponse[DatabaseGranteesOut],
)
@limiter.limit("30/minute")
def list_database_users(
    request: Request, admin: AdminDep, server_id: int, database: str
):
    """Usuarios/roles con algún privilegio sobre la BD, cruzados con el inventario."""
    return success(
        data=ServerDatabaseController().list_database_grantees(
            server_id, database, admin=admin
        )
    )


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


# ------------------------- Consola SQL (queries ad-hoc) ------------------------- #
# Ejecuta SQL arbitrario sobre una BD del servidor, con el usuario del motor que se
# elija (pseudo-root, uno del inventario, uno con contraseña provista, o un rol
# adoptado con SET ROLE en PostgreSQL). Modo seguro: todo lo que no sea lectura pura
# exige el ciclo preview → confirmación, y hay sentencias PROHIBIDAS incluso confirmando.
@router.post(
    "/{server_id}/query/preview",
    response_model=ApiResponse[QueryPreviewOut],
)
@limiter.limit("30/minute")
def preview_query(
    request: Request, admin: AdminDep, server_id: int, payload: QueryPreviewIn
):
    """
    Paso 1: clasifica el SQL (lectura / escritura / DDL / prohibido), estima cuántas filas
    afectaría cada UPDATE/DELETE y emite el ``confirm_token`` (TTL 2 min) atado al SQL, la
    base de datos y el usuario elegidos. NO ejecuta nada.
    """
    return success(
        data=QueryConsoleController().preview(
            server_id,
            database=payload.database,
            sql=payload.sql,
            connection=payload.connection,
            estimate_impact=payload.estimate_impact,
            admin=admin,
        )
    )


@router.post(
    "/{server_id}/query/execute",
    response_model=ApiResponse[QueryExecuteOut],
)
@limiter.limit("30/minute")
def execute_query(
    request: Request, admin: AdminDep, server_id: int, payload: QueryExecuteIn
):
    """
    Paso 2: ejecuta el lote. Una consulta de solo lectura corre directo (dentro de una
    transacción READ ONLY); cualquier otra exige ``confirm_target_name`` == nombre de la
    BD + ``confirm_token`` del preview.

    Un rechazo del MOTOR (por ejemplo por falta de permisos) devuelve **200** con
    ``success=false`` y el error nativo: es un resultado válido de la prueba, no un fallo
    de la API.
    """
    result = QueryConsoleController().execute(
        server_id,
        database=payload.database,
        sql=payload.sql,
        connection=payload.connection,
        confirm_token_value=payload.confirm_token,
        confirm_target_name=payload.confirm_target_name,
        dry_run=payload.dry_run,
        max_rows=payload.max_rows,
        timeout_ms=payload.timeout_ms,
        admin=admin,
    )
    return success(data=result)


@router.get(
    "/{server_id}/query/history",
    response_model=ApiResponse[list[QueryHistoryOut]],
)
@limiter.limit("60/minute")
def list_query_history(
    request: Request,
    admin: AdminDep,
    server_id: int,
    pagination: PaginationDep,
    database: str | None = Query(default=None, description="Filtra por base de datos."),
):
    """Historial de ejecuciones de la consola (sin las filas devueltas, solo conteos)."""
    items, total = QueryConsoleController().list_history(
        server_id, database=database, limit=pagination.size, offset=pagination.offset
    )
    return paginated(
        [QueryHistoryOut.model_validate(i) for i in items],
        total=total,
        pagination=pagination,
    )
