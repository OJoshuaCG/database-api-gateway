"""
Integration API (``/integration``): the closed set of operations a user's own web project can
automate with a REST bearer ``datumint.<public_id>.<secret>``.

BEARER ONLY: every route resolves its caller with ``require_integration``, which never reads the
session cookie, so a browser session cannot reach it and a bearer cannot reach the SPA routes.
Each route declares ONE scope; the gate checks, in order, the kill switch, the credential, the
effective scope, the per-token quotas, the server/blueprint allowlists and layer 2 at the target.

The global SlowAPI default limit is exempted on purpose: its key is the client address, which would
make every integration of one NAT share a quota. The per-token buckets of the gate replace it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.controllers.integration_ops_controller import IntegrationOpsController
from app.core.integration_auth import IntegrationCall, require_integration
from app.core.limiter import limiter
from app.core.scope_targets import database as database_target
from app.core.scope_targets import managed_create, payload_server, server_user
from app.core.scope_targets import server as server_target
from app.schemas.integration import (
    IntegrationBlueprintOut,
    IntegrationDatabaseCreate,
    IntegrationDatabaseOut,
    IntegrationEngineUserCreate,
    IntegrationAssignBlueprintRequest,
    IntegrationAssignDatabaseRequest,
    IntegrationApplyMigrationsRequest,
    IntegrationApplyProfileRequest,
    IntegrationEngineUserCreatedOut,
    IntegrationMigrationVersionOut,
    IntegrationRollbackRequest,
    IntegrationServerOut,
    IntegrationStampRequest,
)
from app.schemas.grant import ApplyProfileResult
from app.schemas.model_migration import MigrationApplyOut, MigrationRollbackOut, MigrationStatusOut
from app.services.integration_scope_catalog import IntegrationScope
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/integration", tags=["Integration API"])

# One alias per scope: the scope is part of the route contract and shows up in the signature.
ServersListCall = Annotated[
    IntegrationCall, Depends(require_integration(IntegrationScope.SERVERS_LIST))
]
DatabasesListCall = Annotated[
    IntegrationCall,
    Depends(require_integration(IntegrationScope.DATABASES_LIST, target=server_target)),
]
BlueprintReadCall = Annotated[
    IntegrationCall,
    Depends(
        require_integration(IntegrationScope.BLUEPRINT_READ_ASSIGNED, target=database_target)
    ),
]
MigrationsReadCall = Annotated[
    IntegrationCall,
    Depends(
        require_integration(IntegrationScope.MIGRATIONS_READ_VERSION, target=database_target)
    ),
]
DatabasesCreateCall = Annotated[
    IntegrationCall,
    Depends(require_integration(IntegrationScope.DATABASES_CREATE, target=managed_create)),
]
EngineUsersCreateCall = Annotated[
    IntegrationCall,
    Depends(require_integration(IntegrationScope.ENGINE_USERS_CREATE, target=payload_server)),
]
ApplyMigrationsCall = Annotated[
    IntegrationCall,
    Depends(
        require_integration(IntegrationScope.MIGRATIONS_APPLY_FORWARD, target=database_target)
    ),
]
RollbackMigrationsCall = Annotated[
    IntegrationCall,
    Depends(require_integration(IntegrationScope.MIGRATIONS_ROLLBACK, target=database_target)),
]
StampMigrationCall = Annotated[
    IntegrationCall,
    Depends(require_integration(IntegrationScope.MIGRATIONS_STAMP, target=database_target)),
]
AssignBlueprintCall = Annotated[
    IntegrationCall,
    Depends(
        require_integration(IntegrationScope.DATABASES_ASSIGN_BLUEPRINT, target=database_target)
    ),
]
AssignProfileCall = Annotated[
    IntegrationCall,
    Depends(require_integration(IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE, target=server_user)),
]
AssignDatabaseCall = Annotated[
    IntegrationCall,
    Depends(
        require_integration(IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE, target=database_target)
    ),
]


@router.get("/servers", response_model=ApiResponse[list[IntegrationServerOut]])
@limiter.exempt
def list_servers(call: ServersListCall, pagination: PaginationDep):
    """
    The servers of the token allowlist that the issuer can read. Minimal projection (id, name,
    engine): host, port and credentials never leave through the integration API.
    """
    servers, total = IntegrationOpsController().list_servers(
        call, limit=pagination.size, offset=pagination.offset
    )
    return paginated(servers, total=total, pagination=pagination)


@router.get("/databases", response_model=ApiResponse[list[IntegrationDatabaseOut]])
@limiter.exempt
def list_databases(
    call: DatabasesListCall,
    pagination: PaginationDep,
    server_id: int = Query(..., ge=1, description="Server to list; must be in the allowlist."),
):
    """
    The managed databases of one allowlisted server that the issuer can read there.

    A server outside the allowlist and one that does not exist answer the same 403
    ``integration.server_not_allowed`` (no way to enumerate ids).
    """
    databases, total = IntegrationOpsController().list_databases(
        call, server_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(databases, total=total, pagination=pagination)


@router.get("/databases/{db_id}/blueprint", response_model=ApiResponse[IntegrationBlueprintOut])
@limiter.exempt
def read_assigned_blueprint(call: BlueprintReadCall, db_id: int):
    """The blueprint assigned to the database. 404 ``integration.blueprint_not_assigned`` if none."""
    return success(data=IntegrationOpsController().read_assigned_blueprint(db_id))


@router.get(
    "/databases/{db_id}/migrations/version",
    response_model=ApiResponse[IntegrationMigrationVersionOut],
)
@limiter.exempt
def read_migration_version(call: MigrationsReadCall, db_id: int):
    """Current, latest and pending migration versions of the database against its blueprint."""
    return success(data=IntegrationOpsController().read_migration_version(db_id))


@router.post("/databases", response_model=ApiResponse[IntegrationDatabaseOut], status_code=201)
@limiter.exempt
def create_database(call: DatabasesCreateCall, payload: IntegrationDatabaseCreate):
    """
    Creates and provisions an EMPTY managed database on an allowlisted server.

    It never assigns a blueprint or migrates (their own scopes do): ``model_id``,
    ``model_version``, ``apply_migrations`` and ``target_version`` are rejected with 422.
    """
    created = IntegrationOpsController().create_database(call, payload.model_dump())
    return success(data=created, message="Base de datos creada y aprovisionada en el motor.")


@router.post(
    "/engine-users", response_model=ApiResponse[IntegrationEngineUserCreatedOut], status_code=201
)
@limiter.exempt
def create_engine_user(call: EngineUsersCreateCall, payload: IntegrationEngineUserCreate):
    """
    Creates an engine user and returns its GENERATED password in this response only.

    The password is stored encrypted and no integration endpoint can read it back, so a client
    that loses it has to ask for a new user.
    """
    created = IntegrationOpsController().create_engine_user(call, payload.model_dump())
    return success(
        data=created,
        message="Usuario del motor creado. Copiá la contraseña ahora: no se vuelve a mostrar.",
    )


@router.post(
    "/engine-users/{user_id}/profiles/{profile_id}",
    response_model=ApiResponse[ApplyProfileResult],
)
@limiter.exempt
def assign_profile(
    call: AssignProfileCall, user_id: int, profile_id: int, payload: IntegrationApplyProfileRequest
):
    """
    Applies a permission profile to an engine user on the mapped databases.

    Each mapping must name a managed database of the user's server (409
    ``integration.server_mismatch`` otherwise) and a level other than ``global``. A profile with
    any item that delegates power (``ALL PRIVILEGES``, ``GRANT OPTION``) is refused with 403
    ``integration.profile_requires_grant_admin`` before a single grant runs.
    """
    result = IntegrationOpsController().assign_profile(
        call, user_id, profile_id, payload.object_mappings
    )
    return success(data=result, message="Perfil asignado al usuario del motor.")


@router.post(
    "/engine-users/{user_id}/databases/{db_id}",
    response_model=ApiResponse[ApplyProfileResult],
)
@limiter.exempt
def assign_profile_to_database(
    call: AssignDatabaseCall, user_id: int, db_id: int, payload: IntegrationAssignDatabaseRequest
):
    """
    Applies the database-level items of a profile to one database. The gateway builds the object
    mapping from the database row, so the caller only names the profile.

    A user of another server and a missing user answer the same 409 ``integration.server_mismatch``.
    """
    result = IntegrationOpsController().assign_profile_to_database(
        call, user_id, db_id, payload.profile_id
    )
    return success(data=result, message="Perfil asignado al usuario sobre la base de datos.")


@router.put("/databases/{db_id}/blueprint", response_model=ApiResponse[IntegrationDatabaseOut])
@limiter.exempt
def assign_blueprint(
    call: AssignBlueprintCall, db_id: int, payload: IntegrationAssignBlueprintRequest
):
    """
    Assigns a blueprint to a database that has none. Repeating the same one is a no-op success.

    The blueprint must be in the token allowlist when it has one (403
    ``integration.blueprint_not_allowed``). Replacing an assigned blueprint is refused with 409
    ``integration.blueprint_already_assigned``.
    """
    updated = IntegrationOpsController().assign_blueprint(call, db_id, payload.model_id)
    return success(data=updated, message="Blueprint asignado a la base de datos.")


@router.post(
    "/databases/{db_id}/migrations/apply", response_model=ApiResponse[MigrationApplyOut]
)
@limiter.exempt
def apply_migrations_forward(
    call: ApplyMigrationsCall, db_id: int, payload: IntegrationApplyMigrationsRequest
):
    """
    Applies the pending migrations of the database's blueprint, forward only.

    ``force`` and ``on_failure`` do not exist here (422 in the body, ignored in the query): the
    gateway fixes them. A ``version`` older than the current one is 422
    ``integration.migration_target_not_forward``; the current one is a successful no-op. The
    destructive-migration guard of protected environments applies as for a person.
    """
    result = IntegrationOpsController().apply_migrations_forward(
        call, db_id, target_version=payload.version, dry_run=payload.dry_run
    )
    return success(data=result, message=_apply_message(result, dry_run=payload.dry_run))


def _apply_message(result: dict, *, dry_run: bool) -> str:
    """A short message for the three outcomes an automation tells apart: plan, no-op, applied."""
    if dry_run:
        return "Plan (dry-run): no se aplicó nada."
    if result.get("no_op"):
        return "No hay migraciones pendientes hasta la versión pedida; no se aplicó nada."
    if result.get("failed"):
        return "La migración falló. Revisá el estado de la base antes de reintentar."
    return "Migraciones aplicadas."


@router.post(
    "/databases/{db_id}/migrations/rollback", response_model=ApiResponse[MigrationRollbackOut]
)
@limiter.exempt
def rollback_migrations(call: RollbackMigrationsCall, db_id: int, payload: IntegrationRollbackRequest):
    """
    Reverts the database from ``from_version`` back to ``to_version`` (destructive tier).

    Both versions are required: there is no "one step back" and no "back to the base". ``force``,
    ``purge`` and ``dry_run`` do not exist (422). Refused with a stable code when the blueprint is
    not in the token allowlist, the environment is protected or unclassified, the database is
    quarantined, or a version to undo has no proof of having been applied by the gateway.
    """
    result = IntegrationOpsController().rollback_migrations(
        call, db_id, from_version=payload.from_version, to_version=payload.to_version
    )
    return success(data=result, message="Rollback ejecutado.")


@router.post("/databases/{db_id}/migrations/stamp", response_model=ApiResponse[MigrationStatusOut])
@limiter.exempt
def stamp_migration(call: StampMigrationCall, db_id: int, payload: IntegrationStampRequest):
    """
    Declares the database to be on ``version`` without running SQL (destructive tier).

    ``expected_current_version`` is compare-and-set (required key, may be null). Stamping the
    version the database is already on is a successful, audited no-op. ``force`` and ``purge`` do
    not exist (422), so a stamp can never clear a quarantine.
    """
    result = IntegrationOpsController().stamp_migration(
        call,
        db_id,
        expected_current_version=payload.expected_current_version,
        version=payload.version,
    )
    return success(data=result, message="Versión declarada.")
