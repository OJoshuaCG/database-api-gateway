"""
Operations of the integration API (``/integration/...``).

Every operation DELEGATES to the controller the SPA already uses, so the engine-side behaviour,
the validations and the audit trail are the ones of the human endpoints. What lives here is only
what an integration token adds on top:

- the allowlist FILTER of the list operations (their scopes carry no single target the gate could
  check, so the rows themselves are filtered);
- the narrower contract of each operation (closed bodies, minimal projections);
- the rules that exist only for a machine caller (blueprint assignment, forward-only migrations,
  the server-generated engine-user password, the grant-admin pre-check of profile assignments);
- the safety envelope of the destructive tier (rollback and stamp): see ``_assert_destructive_guards``.

The gate (``require_integration``) has already authenticated the token, applied the allowlists and
checked layer 2 at the single target when there is one. ``require_integration`` never asks for a
step-up: the issuer confirmed their password when the token was issued (D6), and a machine cannot
answer a password prompt.
"""

import secrets
from typing import Any

from app.controllers.common import engine_value
from app.controllers.database_model_controller import DatabaseModelController
from app.controllers.grant_controller import GrantController
from app.controllers.managed_database_controller import ManagedDatabaseController
from app.controllers.managed_migration_controller import ManagedMigrationController
from app.controllers.server_user_controller import ServerUserController
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.core.integration_auth import IntegrationCall
from app.core.scope import can_at, partition_by_scope
from app.core.scope_targets import points_for_databases
from app.core.scope_targets import server as server_target
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.enums import MigrationStatus, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.permission_profile import PermissionProfile, PermissionProfileItem
from app.models.server import Server
from app.models.server_user import ServerUser
from app.schemas.grant import ApplyProfileRequest, ApplyProfileResult, LevelObjectMapping
from app.schemas.integration import IntegrationLevelObjectMapping
from app.services import audit
from app.services.db_admin.dtos import GrantLevel, ObjectRef
from app.services.db_admin.migration_integrity import version_sort_key
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_BLUEPRINT_ALREADY_ASSIGNED,
    CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED,
    CODE_INTEGRATION_BLUEPRINT_NOT_ASSIGNED,
    CODE_INTEGRATION_DATABASE_QUARANTINED,
    CODE_INTEGRATION_ENVIRONMENT_BLOCKS_DESTRUCTIVE,
    CODE_INTEGRATION_ENVIRONMENT_UNCLASSIFIED,
    CODE_INTEGRATION_MIGRATION_TARGET_NOT_FORWARD,
    CODE_INTEGRATION_PROFILE_REQUIRES_GRANT_ADMIN,
    CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION,
    CODE_INTEGRATION_SERVER_MISMATCH,
    CODE_INTEGRATION_STAMP_ORPHAN_ACCOUNTING,
    CODE_INTEGRATION_STAMP_VERSION_CONFLICT,
    INTEGRATION_ALLOWED,
)

#: Entropy of the engine-user password the gateway generates (``token_urlsafe`` turns 24 random
#: bytes into 32 characters of [A-Za-z0-9_-], which need no escaping in the engine's quoting).
GENERATED_ENGINE_PASSWORD_BYTES = 24

#: ``on_failure`` of every integration migration: undo what was applied only when all of it can be
#: undone, so a failed run leaves the database on its previous version and not half-migrated.
FIXED_ON_FAILURE_MODE = "auto"

#: Audit actions written BEFORE a destructive integration operation touches anything.
AUDIT_ACTION_ROLLBACK = "integration.migration.rollback"
AUDIT_ACTION_STAMP = "integration.migration.stamp"
AUDIT_TARGET_TYPE_MANAGED_DATABASE = "managed_database"

#: Direction value the gateway writes in ``database_migration_history`` for an apply.
HISTORY_DIRECTION_UP = "up"

#: Shown in the audit detail when a version is absent (a database with no version yet).
AUDIT_NO_VERSION_LABEL = "sin-version"


def _blueprint_is_fenced_out(call: IntegrationCall, model_id: int | None) -> bool:
    """
    True when the token carries a blueprint allowlist and the database's blueprint is outside it.

    Mirrors the gate's rule for database targets: an empty allowlist restricts nothing, and a
    database with no blueprint is never fenced out, so a freshly created database can still be
    given one.
    """
    if not call.allowed_blueprint_ids or model_id is None:
        return False
    return model_id not in call.allowed_blueprint_ids


class IntegrationOpsController:
    def __init__(self) -> None:
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Reads                                                                #
    # ------------------------------------------------------------------ #
    def list_servers(
        self, call: IntegrationCall, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """
        The servers of the token allowlist that still exist and that the issuer can read.

        The scope has no target (there is no single server to check), so layer 2 is applied here,
        per server. The allowlist is small by construction (a handful of servers per token), which
        is what makes a per-server verdict acceptable.
        """
        capability = INTEGRATION_ALLOWED[call.scope]
        session = self._session()
        try:
            allowlisted_servers = (
                session.query(Server)
                .filter(Server.id.in_(sorted(call.allowed_server_ids)))
                .order_by(Server.id.desc())
                .all()
            )
            readable_servers = [
                {
                    "id": server.id,
                    "name": server.name,
                    "engine": _enum_value(server.engine),
                }
                for server in allowlisted_servers
                if can_at(call.actor, capability, server_target(server.id))
            ]
        finally:
            session.close()
        return readable_servers[offset : offset + limit], len(readable_servers)

    def list_databases(
        self, call: IntegrationCall, server_id: int, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """
        The managed databases of ONE allowlisted server that the issuer can read there.

        The gate already checked the server against the allowlist. Layer 2 is applied per
        database, in a single partition, so a server that mixes environments does not leak the
        databases of the ones the issuer cannot read. A database whose blueprint is outside the
        token's blueprint allowlist is not listed either (same rule as the gate: a database with
        no blueprint stays visible so it can be assigned one).
        """
        capability = INTEGRATION_ALLOWED[call.scope]
        session = self._session()
        try:
            server_databases = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.server_id == server_id)
                .order_by(ManagedDatabase.id.desc())
                .all()
            )
            partition = partition_by_scope(
                actor=call.actor,
                capability=capability,
                points=points_for_databases(server_databases),
            )
            readable_database_ids = set(partition.permitted)
            readable_databases = [
                _project_database(database)
                for database in server_databases
                if database.id in readable_database_ids
                and not _blueprint_is_fenced_out(call, database.model_id)
            ]
        finally:
            session.close()
        return readable_databases[offset : offset + limit], len(readable_databases)

    def read_assigned_blueprint(self, database_id: int) -> dict[str, Any]:
        """The blueprint assigned to the database; 404 with a stable code when there is none."""
        database = ManagedDatabaseController().get_database(database_id)
        blueprint_id = database["model_id"]
        if blueprint_id is None:
            raise AppHttpException(
                message="La base de datos no tiene un blueprint asignado.",
                status_code=404,
                public_context={"code": CODE_INTEGRATION_BLUEPRINT_NOT_ASSIGNED},
                context={"managed_database_id": database_id},
            )
        blueprint = DatabaseModelController().get_model(blueprint_id)
        return {
            "model_id": blueprint["id"],
            "name": blueprint["name"],
            "slug": blueprint["slug"],
            "model_version": database["model_version"],
        }

    def read_migration_version(self, database_id: int) -> dict[str, Any]:
        """Current, latest and pending versions: the projection an integration needs."""
        migration_status = ManagedMigrationController().status(database_id)
        return {
            "current_version": migration_status["current_version"],
            "latest_version": migration_status["latest_available"],
            "pending": list(migration_status["pending_versions"]),
        }

    # ------------------------------------------------------------------ #
    # Creations                                                            #
    # ------------------------------------------------------------------ #
    def create_database(self, call: IntegrationCall, data: dict[str, Any]) -> dict[str, Any]:
        """
        Creates the database in the inventory AND in the engine (always provisioned), empty.

        The payload schema already excludes the blueprint fields, so this never migrates: the
        blueprint is assigned and applied by their own scopes. Layer 2 at the declared
        environment is checked by the gate and again by the controller at the resolved one.
        """
        return ManagedDatabaseController().create_database(data, provision=True, admin=call.actor)

    def create_engine_user(self, call: IntegrationCall, data: dict[str, Any]) -> dict[str, Any]:
        """
        Creates an engine user with a SERVER-GENERATED password and returns it, once.

        The client never chooses the secret. It is stored encrypted by the controller and appears
        nowhere else: not in the audit row (which records identities and ids), not in an error
        context (the engine error table carries only the username), and no integration operation
        reads it back. This method must never log ``generated_password``.
        """
        generated_password = secrets.token_urlsafe(GENERATED_ENGINE_PASSWORD_BYTES)
        created_user = ServerUserController().create_server_user(
            {**data, "password": generated_password}, provision=True, admin=call.actor
        )
        return {**created_user, "password": generated_password}

    # ------------------------------------------------------------------ #
    # Blueprint assignment                                                 #
    # ------------------------------------------------------------------ #
    def assign_blueprint(
        self, call: IntegrationCall, database_id: int, blueprint_id: int
    ) -> dict[str, Any]:
        """
        Assigns a blueprint to a database that has none (or already has this very one).

        The token's blueprint allowlist is enforced here because the gate only sees the blueprint
        the database ALREADY has. Replacing an assigned blueprint is refused (409): swapping the
        blueprint of a database changes what every later migration does to it, and that is a
        decision for a human, not for an automation. Repeating the same assignment is a success
        that writes nothing.

        Check-then-write, like the human PATCH it delegates to: two concurrent assignments of
        DIFFERENT blueprints to a database that has none can both pass the check and the last one
        wins. The window is the gap between two inventory statements.
        """
        if call.allowed_blueprint_ids and blueprint_id not in call.allowed_blueprint_ids:
            raise AppHttpException(
                message="El token no puede operar con ese blueprint.",
                status_code=403,
                public_context={"code": CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED},
                context={"model_id": blueprint_id},
            )
        database_controller = ManagedDatabaseController()
        current_database = database_controller.get_database(database_id)
        current_blueprint_id = current_database["model_id"]
        if current_blueprint_id == blueprint_id:
            return current_database
        if current_blueprint_id is not None:
            raise AppHttpException(
                message="La base de datos ya tiene otro blueprint asignado.",
                status_code=409,
                public_context={"code": CODE_INTEGRATION_BLUEPRINT_ALREADY_ASSIGNED},
                context={"managed_database_id": database_id},
            )
        return database_controller.update_database(
            database_id, {"model_id": blueprint_id}, admin=call.actor
        )

    # ------------------------------------------------------------------ #
    # Migrations                                                           #
    # ------------------------------------------------------------------ #
    def apply_migrations_forward(
        self,
        call: IntegrationCall,
        database_id: int,
        *,
        target_version: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """
        Applies the pending migrations up to ``target_version`` (or the latest).

        What this adds to the human endpoint it delegates to:

        - ``force`` and ``on_failure`` are FIXED (``False`` / ``"auto"``), not parameters. ``force``
          overrides the quarantine of a failed previous run, which needs a person to inspect the
          database first; ``auto`` undoes a failed run when everything applied can be undone.
        - A target OLDER than the current version is rejected. The controller would answer a 200
          no-op, which an automation would read as "migrated to the version I asked for".
        - The environment guard (destructive migrations on protected environments) is the
          controller's own and has no override, so production is covered exactly as for a person.

        No step-up is asked here: the issuer confirmed their password when the token was issued
        (D6) and a machine cannot answer a prompt.
        """
        if target_version is not None:
            current_version = ManagedMigrationController().status(database_id)["current_version"]
            target_is_older_than_current = current_version is not None and version_sort_key(
                target_version
            ) < version_sort_key(current_version)
            if target_is_older_than_current:
                raise AppHttpException(
                    message=(
                        "La versión pedida es anterior a la actual. La API de integración solo "
                        "migra hacia adelante."
                    ),
                    status_code=422,
                    public_context={"code": CODE_INTEGRATION_MIGRATION_TARGET_NOT_FORWARD},
                    context={"target_version": target_version, "current_version": current_version},
                )
        return ManagedMigrationController().apply(
            database_id,
            up_to_version=target_version,
            force=False,
            dry_run=dry_run,
            on_failure=FIXED_ON_FAILURE_MODE,
            admin=call.actor,
        )

    # ------------------------------------------------------------------ #
    # Destructive tier: rollback and stamp                                 #
    # ------------------------------------------------------------------ #
    def rollback_migrations(
        self,
        call: IntegrationCall,
        database_id: int,
        *,
        from_version: str,
        to_version: str,
    ) -> dict[str, Any]:
        """
        Reverts a database from ``from_version`` back to ``to_version`` (which stays applied).

        What this adds to ``ManagedMigrationController.rollback`` (not modified, so it keeps its
        own guards: integrity check, partial-application guard, ``from_version`` must equal the
        live version, ``down_sql`` present, reviewed captures):

        - the shared destructive guards (blueprint allowlist, environment, quarantine);
        - the HISTORY PROOF: every version that would be undone must have been applied by this
          gateway with the checksum the blueprint has today. A version that was only stamped, or
          whose definition was edited after it ran, has no proof that its ``down_sql`` undoes
          what actually ran on the engine;
        - a fail-closed ``audit.record_intent`` right before delegating: if the trace cannot be
          written, nothing is reverted.

        There is no ``dry_run``, ``force`` or ``purge``, and no default for ``to_version``.
        """
        model_id, server_id = self._assert_destructive_guards(call, database_id)
        self._assert_versions_were_applied_by_the_gateway(
            database_id, model_id, from_version=from_version, to_version=to_version
        )
        audit.record_intent(
            AUDIT_ACTION_ROLLBACK,
            admin=call.actor,
            target_type=AUDIT_TARGET_TYPE_MANAGED_DATABASE,
            target_id=database_id,
            server_id=server_id,
            detail=f"rollback {from_version} -> {to_version}",
        )
        return ManagedMigrationController().rollback(
            database_id,
            confirm_version=from_version,
            target_version=to_version,
            admin=call.actor,
        )

    def stamp_migration(
        self,
        call: IntegrationCall,
        database_id: int,
        *,
        expected_current_version: str | None,
        version: str,
    ) -> dict[str, Any]:
        """
        Declares the database to be on ``version`` without running SQL, with compare-and-set.

        What this adds to ``ManagedMigrationController.stamp`` (not modified, so its unreviewed
        capture and partial-checkpoint guards and the unknown-version 422 stay):

        - the shared destructive guards. The quarantine guard matters here in particular: the
          human stamp CLEARS a quarantine, and that is a decision for a person;
        - orphan accounting is refused: stamping over a version table nobody owns would bless it;
        - ``expected_current_version`` must match the live version, so a stale or replayed call
          conflicts instead of overwriting someone else's stamp;
        - stamping the version the database is already on is a successful no-op. It is audited
          (an operation was requested) but never reaches the controller.

        ``force`` and ``purge`` are fixed to ``False``.
        """
        _model_id, server_id = self._assert_destructive_guards(call, database_id)
        migration_status = ManagedMigrationController().status(database_id)

        if migration_status["has_orphan_accounting"]:
            raise AppHttpException(
                message=(
                    "La base tiene contabilidad de migraciones huérfana. Un stamp la daría por "
                    "buena; debe resolverla una persona."
                ),
                status_code=409,
                public_context={"code": CODE_INTEGRATION_STAMP_ORPHAN_ACCOUNTING},
                context={"managed_database_id": database_id},
            )

        current_version = migration_status["current_version"]
        if current_version != expected_current_version:
            raise AppHttpException(
                message="La versión actual de la base cambió. Releé el estado y reintentá.",
                status_code=409,
                public_context={"code": CODE_INTEGRATION_STAMP_VERSION_CONFLICT},
                context={
                    "expected_current_version": expected_current_version,
                    "current_version": current_version,
                },
            )

        is_already_current = current_version == version
        audit_detail = f"stamp {current_version or AUDIT_NO_VERSION_LABEL} -> {version}" + (
            " (sin cambios)" if is_already_current else ""
        )
        audit.record_intent(
            AUDIT_ACTION_STAMP,
            admin=call.actor,
            target_type=AUDIT_TARGET_TYPE_MANAGED_DATABASE,
            target_id=database_id,
            server_id=server_id,
            detail=audit_detail,
            touched_engine=not is_already_current,
        )
        if is_already_current:
            return migration_status
        return ManagedMigrationController().stamp(
            database_id, version, force=False, purge=False, admin=call.actor
        )

    def _assert_destructive_guards(
        self, call: IntegrationCall, database_id: int
    ) -> tuple[int, int | None]:
        """
        The guards both destructive operations share; returns ``(model_id, server_id)``.

        Order matters: the cheapest and most generic refusals first. Every refusal happens BEFORE
        any audit intent, so a denied call leaves no ``attempt`` row that never ran.

        - Blueprint allowlist, FAIL CLOSED: a token with an empty allowlist (for example after
          its blueprints were deleted) is refused, where the generic gate would let it through.
        - Environment: a protected environment blocks destructive work, and an UNCLASSIFIED
          database is refused too. The human flow lets an unclassified database through so that
          existing installations keep working; a machine caller has no such history to protect.
        - Quarantine: a database left in ``error`` needs a person to inspect it.
        """
        session = self._session()
        try:
            database = session.get(ManagedDatabase, database_id)
            model_id = database.model_id if database is not None else None
            server_id = database.server_id if database is not None else None
            provision_status = database.status if database is not None else None
            environment_policy = ManagedMigrationController._env_policy_for(session, [database_id])
        finally:
            session.close()

        # An empty allowlist contains nothing, so it fails closed through this same test.
        blueprint_is_allowed = model_id is not None and model_id in call.allowed_blueprint_ids
        if not blueprint_is_allowed:
            raise AppHttpException(
                message="El token no autoriza el blueprint de esta base de datos.",
                status_code=403,
                public_context={"code": CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED},
            )

        database_environment = environment_policy.get(database_id)
        if database_environment is None:
            raise AppHttpException(
                message="La base no tiene entorno asignado; no se permite operar sobre ella.",
                status_code=409,
                public_context={"code": CODE_INTEGRATION_ENVIRONMENT_UNCLASSIFIED},
                context={"managed_database_id": database_id},
            )
        blocks_destructive_migrations, environment_slug = database_environment
        if blocks_destructive_migrations:
            raise AppHttpException(
                message="El entorno de esta base bloquea las operaciones destructivas.",
                status_code=409,
                public_context={"code": CODE_INTEGRATION_ENVIRONMENT_BLOCKS_DESTRUCTIVE},
                context={"managed_database_id": database_id, "environment": environment_slug},
            )

        if provision_status == ProvisionStatus.error:
            raise AppHttpException(
                message="La base está en cuarentena; debe revisarla una persona.",
                status_code=409,
                public_context={"code": CODE_INTEGRATION_DATABASE_QUARANTINED},
                context={"managed_database_id": database_id},
            )
        return model_id, server_id

    def _assert_versions_were_applied_by_the_gateway(
        self, database_id: int, model_id: int, *, from_version: str, to_version: str
    ) -> None:
        """
        History proof for a rollback: each blueprint version ``v`` with ``to_version < v <=
        from_version`` must have, as its LATEST history row for this database, an ``up`` row that
        succeeded and recorded the checksum the blueprint holds today.

        Anything weaker is denied: no row (never applied here, or only stamped), a failed or
        reverted last attempt, a legacy row with NULL direction or version, or a checksum that no
        longer matches (the definition was edited after it ran, so its ``down_sql`` may not undo
        what the engine executed).
        """
        session = self._session()
        try:
            blueprint_specs = ManagedMigrationController._load_specs(session, model_id)
            from_sort_key = version_sort_key(from_version)
            to_sort_key = version_sort_key(to_version)
            specs_to_undo = [
                spec
                for spec in blueprint_specs
                if to_sort_key < version_sort_key(spec.version) <= from_sort_key
            ]
            if not specs_to_undo:
                return
            versions_to_undo = [spec.version for spec in specs_to_undo]
            history_rows = (
                session.query(
                    DatabaseMigrationHistory.applied_version,
                    DatabaseMigrationHistory.direction,
                    DatabaseMigrationHistory.status,
                    DatabaseMigrationHistory.applied_checksum,
                )
                .filter(
                    DatabaseMigrationHistory.managed_database_id == database_id,
                    DatabaseMigrationHistory.applied_version.in_(versions_to_undo),
                )
                .order_by(
                    DatabaseMigrationHistory.applied_at.desc(),
                    DatabaseMigrationHistory.id.desc(),
                )
                .all()
            )
        finally:
            session.close()

        latest_row_by_version: dict[str, Any] = {}
        for history_row in history_rows:
            latest_row_by_version.setdefault(history_row.applied_version, history_row)

        unproven_versions: list[str] = []
        for spec in specs_to_undo:
            latest_row = latest_row_by_version.get(spec.version)
            has_proof = (
                latest_row is not None
                and latest_row.direction == HISTORY_DIRECTION_UP
                and latest_row.status == MigrationStatus.applied
                and latest_row.applied_checksum == spec.checksum
            )
            if not has_proof:
                unproven_versions.append(spec.version)

        if unproven_versions:
            raise AppHttpException(
                message=(
                    "Alguna versión a revertir no consta como aplicada por el gateway con su "
                    "definición actual. Revertirla a ciegas no es seguro."
                ),
                status_code=409,
                public_context={
                    "code": CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION,
                    "versions": unproven_versions,
                },
                context={"managed_database_id": database_id},
            )

    # ------------------------------------------------------------------ #
    # Permission profiles                                                  #
    # ------------------------------------------------------------------ #
    def assign_profile(
        self,
        call: IntegrationCall,
        user_id: int,
        profile_id: int,
        object_mappings: list[IntegrationLevelObjectMapping],
    ) -> ApplyProfileResult:
        """
        Applies a profile to an engine user on the databases the caller maps.

        Every mapped database must be a MANAGED database of the user's own server: a token that
        may touch one server cannot grant on a database the gateway does not manage there, nor on
        a homonym of another server, nor on a database whose blueprint is outside the token's
        blueprint allowlist (the same fence the database-level route gets from the gate). Then the
        grant-admin pre-check, then the human controller.
        """
        user_server_id = self._server_id_of_user(user_id)
        for mapping in object_mappings:
            self._assert_database_is_managed_on_server(
                call, mapping.object_ref.database, user_server_id
            )
        self._assert_profile_cannot_delegate(user_id, profile_id)
        return GrantController().apply_profile(
            user_id,
            profile_id,
            ApplyProfileRequest(object_mappings=list(object_mappings)),
            admin=call.actor,
        )

    def assign_profile_to_database(
        self, call: IntegrationCall, user_id: int, database_id: int, profile_id: int
    ) -> ApplyProfileResult:
        """
        Applies the DATABASE-level items of a profile to ONE database, with a mapping the gateway
        builds itself from the database row: the caller cannot name the object. The items of the
        other levels have no mapping and are reported as skipped, never guessed.

        A missing user and a user of another server answer the same 409, so the endpoint cannot be
        used to probe which engine-user ids exist.
        """
        session = self._session()
        try:
            database = session.get(ManagedDatabase, database_id)
            user = session.get(ServerUser, user_id)
            user_belongs_to_database_server = (
                database is not None and user is not None and user.server_id == database.server_id
            )
            database_name = database.name if database is not None else None
        finally:
            session.close()
        if not user_belongs_to_database_server:
            raise _server_mismatch()

        self._assert_profile_cannot_delegate(user_id, profile_id)
        database_level_mapping = LevelObjectMapping(
            level=GrantLevel.DATABASE, object_ref=ObjectRef(database=database_name)
        )
        return GrantController().apply_profile(
            user_id,
            profile_id,
            ApplyProfileRequest(object_mappings=[database_level_mapping]),
            admin=call.actor,
        )

    def _server_id_of_user(self, user_id: int) -> int:
        session = self._session()
        try:
            server_id = (
                session.query(ServerUser.server_id).filter(ServerUser.id == user_id).scalar()
            )
        finally:
            session.close()
        if server_id is None:
            raise _server_mismatch()
        return server_id

    def _assert_database_is_managed_on_server(
        self, call: IntegrationCall, database_name: str, server_id: int
    ) -> None:
        session = self._session()
        try:
            managed_database = (
                session.query(ManagedDatabase.id, ManagedDatabase.model_id)
                .filter(
                    ManagedDatabase.name == database_name,
                    ManagedDatabase.server_id == server_id,
                )
                .first()
            )
        finally:
            session.close()
        if managed_database is None:
            raise _server_mismatch()
        if _blueprint_is_fenced_out(call, managed_database.model_id):
            raise AppHttpException(
                message="El token no puede operar con el blueprint de esta base.",
                status_code=403,
                public_context={"code": CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED},
            )

    def _assert_profile_cannot_delegate(self, user_id: int, profile_id: int) -> None:
        """
        D11: refuses, BEFORE any grant runs, a profile with any item that delegates power
        (``WITH GRANT OPTION`` or a GATE privilege such as ``ALL PRIVILEGES``).

        ``GrantController.apply_profile`` never asks for ``engine_users.grant_admin``, because the
        human endpoint that calls it is gated by a capability that already covers it. An
        integration token has no such capability, so the criterion the grant operation itself uses
        (``grant_admin_reason``) is applied here to every item of the profile up front: refusing
        halfway would leave a profile partially applied. The profile is read with the SERVER's
        dialect, like the real application does.
        """
        session = self._session()
        try:
            user = session.get(ServerUser, user_id)
            profile = session.get(PermissionProfile, profile_id)
            if profile is None:
                raise AppHttpException(
                    message="Perfil de permisos no encontrado.",
                    status_code=404,
                    context={"profile_id": profile_id},
                )
            server = session.get(Server, user.server_id)
            server_dialect = engine_value(server)
            profile_items = [
                (item.level, item.privileges)
                for item in session.query(PermissionProfileItem)
                .filter(PermissionProfileItem.profile_id == profile_id)
                .all()
            ]
        finally:
            session.close()

        for level_value, privileges_csv in profile_items:
            privileges = [token.strip() for token in privileges_csv.split(",") if token.strip()]
            delegation_reason = GrantController.grant_admin_reason(
                dialect=server_dialect,
                level=GrantLevel(level_value),
                privileges=privileges,
                with_grant_option=False,
            )
            if delegation_reason is not None:
                raise AppHttpException(
                    message=(
                        "El perfil otorga privilegios que delegan poder (por ejemplo ALL "
                        "PRIVILEGES o GRANT OPTION). Un token de integración no puede asignarlo."
                    ),
                    status_code=403,
                    public_context={
                        "code": CODE_INTEGRATION_PROFILE_REQUIRES_GRANT_ADMIN,
                        "reason": delegation_reason,
                    },
                    context={"profile_id": profile_id, "level": level_value},
                )


def _server_mismatch() -> AppHttpException:
    """
    The one 409 of "this user / database does not belong here". It carries no id on purpose, so a
    user of another server, a missing user and an unmanaged database are indistinguishable.
    """
    return AppHttpException(
        message="El usuario o la base de datos no pertenecen al servidor indicado.",
        status_code=409,
        public_context={"code": CODE_INTEGRATION_SERVER_MISMATCH},
    )


def _enum_value(value: Any) -> Any:
    """The plain value of an enum column, or the value itself when it is already plain."""
    return getattr(value, "value", value)


def _project_database(database: ManagedDatabase) -> dict[str, Any]:
    """The minimal view of a managed database: no owner, notes or agent-access flags."""
    return {
        "id": database.id,
        "name": database.name,
        "server_id": database.server_id,
        "model_id": database.model_id,
        "model_version": database.model_version,
        "environment_id": database.environment_id,
        "status": _enum_value(database.status),
        "charset": database.charset,
        "collation": database.collation,
    }
