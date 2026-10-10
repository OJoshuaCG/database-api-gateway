"""
Integration API, destructive tier: ``migrations.rollback`` and ``migrations.stamp``.

What is measured here is the SAFETY ENVELOPE the adapter adds on top of the human controller
(``ManagedMigrationController.rollback`` / ``.stamp``, which are never modified):

- blueprint allowlist required (fail closed), protected and unclassified environments denied,
  quarantined databases denied;
- rollback: compare-and-set on BOTH ends, and a history proof (the versions to undo must have been
  applied by THIS gateway, with the checksum the blueprint has today) before anything runs;
- stamp: compare-and-set on the current version, idempotent when already current, refused on
  orphan accounting, and unable to clear a quarantine;
- ``record_intent`` is fail closed and runs BEFORE the controller is called;
- ``force`` / ``purge`` / ``dry_run`` do not exist.

The controller methods are replaced by recording fakes: the engine is never reached. The guards
themselves (allowlists, history, environments, audit) are the REAL ones and run on the test DB.
"""

# ruff: noqa: F811 — the imported fixtures are requested by parameter, which is how pytest uses them.

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.enums import MigrationStatus
from app.models.model_migration import ModelMigration
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED,
    CODE_INTEGRATION_DATABASE_QUARANTINED,
    CODE_INTEGRATION_ENVIRONMENT_BLOCKS_DESTRUCTIVE,
    CODE_INTEGRATION_ENVIRONMENT_UNCLASSIFIED,
    CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION,
    CODE_INTEGRATION_SCOPE_MISSING,
    CODE_INTEGRATION_STAMP_ORPHAN_ACCOUNTING,
    CODE_INTEGRATION_STAMP_VERSION_CONFLICT,
    IntegrationScope,
)
from tests.test_integration_auth import _audit_rows, _bearer, _make_token, _public_context
from tests.test_integration_ops_api import (
    DATABASES_PATH,
    SAFE_MIGRATION_SQL,
    _all_audit_text,
    _create_blueprint,
    _create_database_with_blueprint,
    _create_server,
    integration_api,  # noqa: F401  (pytest fixture, requested by name)
    owner_issuer,  # noqa: F401  (pytest fixture, requested by name)
)

ROLLBACK_ACTION = "integration.migration.rollback"
STAMP_ACTION = "integration.migration.stamp"
FIRST_VERSION = "0001"
SECOND_VERSION = "0002"
THIRD_VERSION = "0003"
THREE_MIGRATIONS = [
    {"version": FIRST_VERSION, "name": "first", "up_sql": SAFE_MIGRATION_SQL},
    {"version": SECOND_VERSION, "name": "second", "up_sql": "CREATE TABLE dest_t2 (id INT PRIMARY KEY)"},
    {"version": THIRD_VERSION, "name": "third", "up_sql": "CREATE TABLE dest_t3 (id INT PRIMARY KEY)"},
]
SECRET_FRAGMENT_IN_ENGINE_ERROR = "ENGINE-INTERNAL-DETAIL-must-not-leak"


def _rollback_path(database_id: int) -> str:
    return f"{DATABASES_PATH}/{database_id}/migrations/rollback"


def _stamp_path(database_id: int) -> str:
    return f"{DATABASES_PATH}/{database_id}/migrations/stamp"


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def controller_calls(monkeypatch) -> dict[str, Any]:
    """
    Replaces ``ManagedMigrationController.status/rollback/stamp``. ``state`` is settable per test:
    the version the engine reports, orphan accounting, and an error to raise from each method.
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController

    state: dict[str, Any] = {
        "current_version": THIRD_VERSION,
        "has_orphan_accounting": False,
        "rollback_error": None,
        "stamp_error": None,
        "rollback_calls": [],
        "stamp_calls": [],
    }

    def fake_status(self, db_id: int) -> dict:
        return {
            "managed_database_id": db_id,
            "current_version": state["current_version"],
            "has_orphan_accounting": state["has_orphan_accounting"],
            "has_partial_application": False,
            "pending_count": 0,
            "pending_versions": [],
        }

    def fake_rollback(self, db_id, *, confirm_version, target_version, admin=None):
        state["rollback_calls"].append(
            {
                "db_id": db_id,
                "confirm_version": confirm_version,
                "target_version": target_version,
                "actor_kind": getattr(admin, "kind", None),
            }
        )
        if state["rollback_error"] is not None:
            raise state["rollback_error"]
        return {
            "managed_database_id": db_id,
            "from_version": confirm_version,
            "to_version": target_version,
            "reverted_count": 1,
        }

    def fake_stamp(self, db_id, version, *, force=False, purge=False, admin=None):
        state["stamp_calls"].append(
            {
                "db_id": db_id,
                "version": version,
                "force": force,
                "purge": purge,
                "actor_kind": getattr(admin, "kind", None),
            }
        )
        if state["stamp_error"] is not None:
            raise state["stamp_error"]
        return {
            "managed_database_id": db_id,
            "current_version": version,
            "pending_count": 0,
            "pending_versions": [],
        }

    monkeypatch.setattr(ManagedMigrationController, "status", fake_status)
    monkeypatch.setattr(ManagedMigrationController, "rollback", fake_rollback)
    monkeypatch.setattr(ManagedMigrationController, "stamp", fake_stamp)
    return state


def _model_id_of(database_id: int) -> int:
    with Database().engine.begin() as connection:
        return connection.execute(
            text("SELECT model_id FROM managed_databases WHERE id = :d"), {"d": database_id}
        ).scalar_one()


def _checksum_of(model_id: int, version: str) -> str:
    session = Database().get_declarative_base_session()
    try:
        migration = (
            session.query(ModelMigration)
            .filter(ModelMigration.model_id == model_id, ModelMigration.version == version)
            .one()
        )
        return migration.checksum
    finally:
        session.close()


def _seed_history(
    database_id: int,
    version: str | None,
    *,
    direction: str | None = "up",
    status: MigrationStatus = MigrationStatus.applied,
    checksum: str | None = None,
    minutes_ago: int = 60,
) -> None:
    """One ``database_migration_history`` row; the checksum defaults to the blueprint's CURRENT one."""
    model_id = _model_id_of(database_id)
    recorded_checksum = checksum
    if recorded_checksum is None and version is not None:
        recorded_checksum = _checksum_of(model_id, version)
    session = Database().get_declarative_base_session()
    try:
        session.add(
            DatabaseMigrationHistory(
                managed_database_id=database_id,
                direction=direction,
                applied_version=version,
                applied_checksum=recorded_checksum,
                status=status,
                applied_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago),
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_full_applied_history(database_id: int) -> None:
    """The three versions were applied by this gateway, in order."""
    _seed_history(database_id, FIRST_VERSION, minutes_ago=30)
    _seed_history(database_id, SECOND_VERSION, minutes_ago=20)
    _seed_history(database_id, THIRD_VERSION, minutes_ago=10)


def _destructive_headers(
    issuer_id: int, server_id: int, database_id: int, scopes: list[IntegrationScope] | None = None
) -> dict:
    chosen_scopes = scopes or [IntegrationScope.MIGRATIONS_ROLLBACK, IntegrationScope.MIGRATIONS_STAMP]
    minted = _make_token(
        issuer_id,
        [scope.value for scope in chosen_scopes],
        server_ids=(server_id,),
        blueprint_ids=(_model_id_of(database_id),),
    )
    return _bearer(minted.bearer)


@pytest.fixture()
def world(integration_api, admin_client, owner_issuer, controller_calls) -> dict[str, Any]:
    """A development database on a blueprint with three versions, a token for it, full history."""
    server_id = _create_server(admin_client, 3951)
    database_id = _create_database_with_blueprint(
        admin_client,
        server_id,
        name="dest_world",
        migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_full_applied_history(database_id)
    return {
        "server_id": server_id,
        "database_id": database_id,
        "headers": _destructive_headers(owner_issuer, server_id, database_id),
        "issuer": owner_issuer,
    }


def _rollback_body(from_version: str = THIRD_VERSION, to_version: str = SECOND_VERSION) -> dict:
    return {"from_version": from_version, "to_version": to_version}


def _stamp_body(expected: str | None = THIRD_VERSION, version: str = SECOND_VERSION) -> dict:
    return {"expected_current_version": expected, "version": version}


def _set_environment_slug(database_id: int, slug: str | None) -> None:
    with Database().engine.begin() as connection:
        if slug is None:
            connection.execute(
                text("UPDATE managed_databases SET environment_id = NULL WHERE id = :d"),
                {"d": database_id},
            )
            return
        connection.execute(
            text(
                "UPDATE managed_databases SET environment_id = "
                "(SELECT id FROM environments WHERE slug = :s) WHERE id = :d"
            ),
            {"s": slug, "d": database_id},
        )


def _quarantine(database_id: int) -> None:
    with Database().engine.begin() as connection:
        connection.execute(
            text("UPDATE managed_databases SET status = 'error' WHERE id = :d"), {"d": database_id}
        )


# --------------------------------------------------------------------------- #
# Rollback: the happy path and its fixed arguments                             #
# --------------------------------------------------------------------------- #


def test_rollback_delegates_with_both_versions_and_the_integration_actor(
    integration_api, world, controller_calls
):
    response = integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=world["headers"]
    )

    assert response.status_code == 200, response.text
    assert controller_calls["rollback_calls"] == [
        {
            "db_id": world["database_id"],
            "confirm_version": THIRD_VERSION,
            "target_version": SECOND_VERSION,
            "actor_kind": "integration",
        }
    ]
    assert response.json()["data"]["to_version"] == SECOND_VERSION


def test_rollback_records_the_intent_before_the_controller_runs(
    integration_api, world, controller_calls, monkeypatch
):
    from app.services import audit

    order_of_events: list[str] = []
    real_record_intent = audit.record_intent

    def spying_record_intent(action, **kwargs):
        order_of_events.append(f"intent:{action}")
        return real_record_intent(action, **kwargs)

    monkeypatch.setattr(audit, "record_intent", spying_record_intent)
    original_calls = controller_calls["rollback_calls"]

    class _OrderedList(list):
        def append(self, item):
            order_of_events.append("controller")
            super().append(item)

    controller_calls["rollback_calls"] = _OrderedList(original_calls)

    integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=world["headers"]
    )

    assert order_of_events == [f"intent:{ROLLBACK_ACTION}", "controller"]
    attempt_rows = _audit_rows(ROLLBACK_ACTION, "attempt")
    assert len(attempt_rows) == 1
    assert attempt_rows[0].actor_type == "integration"


def test_rollback_with_a_failing_intent_is_a_500_and_the_controller_never_runs(
    integration_api, world, controller_calls, monkeypatch
):
    from app.services import audit

    def failing_record_intent(action, **kwargs):
        raise AppHttpException(message="No se pudo registrar la auditoría.", status_code=500)

    monkeypatch.setattr(audit, "record_intent", failing_record_intent)

    response = integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=world["headers"]
    )

    assert response.status_code == 500
    assert controller_calls["rollback_calls"] == []


# --------------------------------------------------------------------------- #
# Rollback: closed body                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"from_version": THIRD_VERSION},
        {"to_version": SECOND_VERSION},
        {},
        {"from_version": THIRD_VERSION, "to_version": SECOND_VERSION, "force": True},
        {"from_version": THIRD_VERSION, "to_version": SECOND_VERSION, "purge": True},
        {"from_version": THIRD_VERSION, "to_version": SECOND_VERSION, "dry_run": True},
        {"from_version": THIRD_VERSION, "to_version": None},
        {"from_version": THIRD_VERSION, "to_version": "not-a-version"},
    ],
    ids=[
        "missing to_version (no implicit one-step)",
        "missing from_version",
        "empty body",
        "force rejected",
        "purge rejected",
        "dry_run rejected",
        "null to_version (no rollback to base)",
        "malformed version",
    ],
)
def test_rollback_body_is_closed_and_both_versions_are_required(
    integration_api, world, controller_calls, body
):
    response = integration_api.post(
        _rollback_path(world["database_id"]), json=body, headers=world["headers"]
    )

    assert response.status_code == 422, response.text
    assert controller_calls["rollback_calls"] == []


def test_rollback_ignores_force_and_dry_run_sent_as_query_parameters(
    integration_api, world, controller_calls
):
    response = integration_api.post(
        _rollback_path(world["database_id"]),
        params={"force": "true", "dry_run": "true", "purge": "true"},
        json=_rollback_body(),
        headers=world["headers"],
    )

    assert response.status_code == 200, response.text
    assert set(controller_calls["rollback_calls"][0]) == {
        "db_id",
        "confirm_version",
        "target_version",
        "actor_kind",
    }


# --------------------------------------------------------------------------- #
# Shared guards: allowlist, environment, quarantine (both operations)         #
# --------------------------------------------------------------------------- #

BOTH_OPERATIONS = [
    pytest.param("rollback", _rollback_path, _rollback_body, id="rollback"),
    pytest.param("stamp", _stamp_path, _stamp_body, id="stamp"),
]


def _calls_of(controller_calls: dict[str, Any], operation: str) -> list:
    return controller_calls[f"{operation}_calls"]


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_a_blueprint_outside_the_token_allowlist_is_a_403_and_nothing_runs(
    integration_api, admin_client, world, controller_calls, operation, path_of, body_of
):
    other_blueprint_headers = _make_token(
        world["issuer"],
        [IntegrationScope.MIGRATIONS_ROLLBACK.value, IntegrationScope.MIGRATIONS_STAMP.value],
        server_ids=(world["server_id"],),
        blueprint_ids=(_create_blueprint("dest-other-blueprint"),),
    )

    response = integration_api.post(
        path_of(world["database_id"]),
        json=body_of(),
        headers=_bearer(other_blueprint_headers.bearer),
    )

    assert response.status_code == 403, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED
    assert _calls_of(controller_calls, operation) == []


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_a_token_with_an_empty_blueprint_allowlist_is_refused_even_if_it_holds_the_scope(
    integration_api, world, controller_calls, operation, path_of, body_of
):
    """Fail closed: an allowlist emptied after issuance (FK cascade) must not mean "any blueprint"."""
    minted = _make_token(
        world["issuer"],
        [IntegrationScope.MIGRATIONS_ROLLBACK.value, IntegrationScope.MIGRATIONS_STAMP.value],
        server_ids=(world["server_id"],),
    )

    response = integration_api.post(
        path_of(world["database_id"]), json=body_of(), headers=_bearer(minted.bearer)
    )

    assert response.status_code == 403, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED
    assert _calls_of(controller_calls, operation) == []


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_a_database_without_a_blueprint_is_refused(
    integration_api, world, controller_calls, operation, path_of, body_of
):
    with Database().engine.begin() as connection:
        connection.execute(
            text("UPDATE managed_databases SET model_id = NULL WHERE id = :d"),
            {"d": world["database_id"]},
        )

    response = integration_api.post(
        path_of(world["database_id"]), json=body_of(), headers=world["headers"]
    )

    assert response.status_code == 403, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED
    assert _calls_of(controller_calls, operation) == []


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_a_protected_environment_is_a_409_and_nothing_runs(
    integration_api, world, controller_calls, operation, path_of, body_of
):
    _set_environment_slug(world["database_id"], "production")

    response = integration_api.post(
        path_of(world["database_id"]), json=body_of(), headers=world["headers"]
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_ENVIRONMENT_BLOCKS_DESTRUCTIVE
    assert _calls_of(controller_calls, operation) == []
    assert _audit_rows(f"integration.migration.{operation}", "attempt") == []


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_an_unclassified_environment_is_a_409_and_nothing_runs(
    integration_api, world, controller_calls, operation, path_of, body_of
):
    """The human flow lets an unclassified database through; a machine caller must not."""
    _set_environment_slug(world["database_id"], None)

    response = integration_api.post(
        path_of(world["database_id"]), json=body_of(), headers=world["headers"]
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_ENVIRONMENT_UNCLASSIFIED
    assert _calls_of(controller_calls, operation) == []


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_a_quarantined_database_is_a_409_and_nothing_runs(
    integration_api, world, controller_calls, operation, path_of, body_of
):
    _quarantine(world["database_id"])

    response = integration_api.post(
        path_of(world["database_id"]), json=body_of(), headers=world["headers"]
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_DATABASE_QUARANTINED
    assert _calls_of(controller_calls, operation) == []


@pytest.mark.parametrize(("operation", "path_of", "body_of"), BOTH_OPERATIONS)
def test_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, world, controller_calls, operation, path_of, body_of
):
    other_server_id = _create_server(admin_client, 3952)
    minted = _make_token(
        world["issuer"],
        [IntegrationScope.MIGRATIONS_ROLLBACK.value, IntegrationScope.MIGRATIONS_STAMP.value],
        server_ids=(other_server_id,),
        blueprint_ids=(_model_id_of(world["database_id"]),),
    )

    response = integration_api.post(
        path_of(world["database_id"]), json=body_of(), headers=_bearer(minted.bearer)
    )

    assert response.status_code == 403, response.text
    assert _calls_of(controller_calls, operation) == []


def test_each_destructive_scope_is_needed_on_its_own(
    integration_api, world, controller_calls
):
    rollback_only = _destructive_headers(
        world["issuer"],
        world["server_id"],
        world["database_id"],
        [IntegrationScope.MIGRATIONS_ROLLBACK],
    )
    stamp_only = _destructive_headers(
        world["issuer"],
        world["server_id"],
        world["database_id"],
        [IntegrationScope.MIGRATIONS_STAMP],
    )

    stamp_with_rollback_scope = integration_api.post(
        _stamp_path(world["database_id"]), json=_stamp_body(), headers=rollback_only
    )
    rollback_with_stamp_scope = integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=stamp_only
    )

    assert stamp_with_rollback_scope.status_code == 403
    assert _public_context(stamp_with_rollback_scope)["code"] == CODE_INTEGRATION_SCOPE_MISSING
    assert rollback_with_stamp_scope.status_code == 403
    assert _public_context(rollback_with_stamp_scope)["code"] == CODE_INTEGRATION_SCOPE_MISSING
    assert controller_calls["rollback_calls"] == []
    assert controller_calls["stamp_calls"] == []


# --------------------------------------------------------------------------- #
# Rollback: history proof                                                      #
# --------------------------------------------------------------------------- #


def _assert_rollback_refused_as_unapplied(integration_api, world, controller_calls) -> None:
    response = integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=world["headers"]
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION
    assert controller_calls["rollback_calls"] == []
    assert _audit_rows(ROLLBACK_ACTION, "attempt") == []


def test_rollback_of_a_version_with_no_history_row_is_refused(
    integration_api, admin_client, owner_issuer, controller_calls
):
    server_id = _create_server(admin_client, 3953)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_nohist", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, FIRST_VERSION)
    _seed_history(database_id, SECOND_VERSION)
    world = {
        "database_id": database_id,
        "headers": _destructive_headers(owner_issuer, server_id, database_id),
    }

    _assert_rollback_refused_as_unapplied(integration_api, world, controller_calls)


def test_a_stamped_version_cannot_be_rolled_back_by_the_api(
    integration_api, admin_client, owner_issuer, controller_calls
):
    """Stamp writes no history row, so the version it declared has no proof of ever having run."""
    server_id = _create_server(admin_client, 3954)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_stampchain", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, FIRST_VERSION)
    _seed_history(database_id, SECOND_VERSION)
    headers = _destructive_headers(owner_issuer, server_id, database_id)

    stamp_response = integration_api.post(
        _stamp_path(database_id),
        json=_stamp_body(expected=THIRD_VERSION, version=THIRD_VERSION),
        headers=headers,
    )
    rollback_response = integration_api.post(
        _rollback_path(database_id), json=_rollback_body(), headers=headers
    )

    assert stamp_response.status_code == 200, stamp_response.text
    assert rollback_response.status_code == 409, rollback_response.text
    assert (
        _public_context(rollback_response)["code"] == CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION
    )
    assert controller_calls["rollback_calls"] == []


def test_rollback_is_refused_when_the_applied_checksum_no_longer_matches_the_blueprint(
    integration_api, admin_client, owner_issuer, controller_calls
):
    server_id = _create_server(admin_client, 3955)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_edited", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, FIRST_VERSION)
    _seed_history(database_id, SECOND_VERSION)
    _seed_history(database_id, THIRD_VERSION, checksum="0" * 64)
    world = {
        "database_id": database_id,
        "headers": _destructive_headers(owner_issuer, server_id, database_id),
    }

    _assert_rollback_refused_as_unapplied(integration_api, world, controller_calls)


def test_rollback_is_refused_when_the_history_row_has_a_null_direction(
    integration_api, admin_client, owner_issuer, controller_calls
):
    """Legacy rows (before the direction column) prove nothing: deny, never assume."""
    server_id = _create_server(admin_client, 3956)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_legacy", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, FIRST_VERSION)
    _seed_history(database_id, SECOND_VERSION)
    _seed_history(database_id, THIRD_VERSION, direction=None)
    world = {
        "database_id": database_id,
        "headers": _destructive_headers(owner_issuer, server_id, database_id),
    }

    _assert_rollback_refused_as_unapplied(integration_api, world, controller_calls)


def test_rollback_is_refused_when_the_latest_history_row_of_a_version_is_a_failed_attempt(
    integration_api, admin_client, owner_issuer, controller_calls
):
    server_id = _create_server(admin_client, 3957)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_failed", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, FIRST_VERSION)
    _seed_history(database_id, SECOND_VERSION)
    _seed_history(database_id, THIRD_VERSION, minutes_ago=30)
    _seed_history(database_id, THIRD_VERSION, status=MigrationStatus.failed, minutes_ago=5)
    world = {
        "database_id": database_id,
        "headers": _destructive_headers(owner_issuer, server_id, database_id),
    }

    _assert_rollback_refused_as_unapplied(integration_api, world, controller_calls)


def test_rollback_is_refused_when_the_latest_history_row_of_a_version_is_a_rollback(
    integration_api, admin_client, owner_issuer, controller_calls
):
    server_id = _create_server(admin_client, 3958)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_undone", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, FIRST_VERSION)
    _seed_history(database_id, SECOND_VERSION)
    _seed_history(database_id, THIRD_VERSION, minutes_ago=30)
    _seed_history(database_id, THIRD_VERSION, direction="down", minutes_ago=5)
    world = {
        "database_id": database_id,
        "headers": _destructive_headers(owner_issuer, server_id, database_id),
    }

    _assert_rollback_refused_as_unapplied(integration_api, world, controller_calls)


def test_the_history_proof_covers_only_the_versions_that_would_be_undone(
    integration_api, admin_client, owner_issuer, controller_calls
):
    """A version at or below ``to_version`` stays applied, so its history is not required."""
    server_id = _create_server(admin_client, 3959)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_partial", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_history(database_id, SECOND_VERSION)
    _seed_history(database_id, THIRD_VERSION)
    headers = _destructive_headers(owner_issuer, server_id, database_id)

    response = integration_api.post(
        _rollback_path(database_id), json=_rollback_body(), headers=headers
    )

    assert response.status_code == 200, response.text
    assert len(controller_calls["rollback_calls"]) == 1


# --------------------------------------------------------------------------- #
# Rollback: errors that belong to the controller pass through unchanged       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (422, "migration.rollback_confirm_mismatch"),
        (409, "migration.rollback_no_down_sql"),
        (409, "migration.partial_application"),
    ],
)
def test_rollback_errors_of_the_controller_reach_the_caller_with_their_own_code(
    integration_api, world, controller_calls, status_code, code
):
    controller_calls["rollback_error"] = AppHttpException(
        message="Rechazado por el controlador.",
        status_code=status_code,
        public_context={"code": code},
    )

    response = integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=world["headers"]
    )

    assert response.status_code == status_code
    assert _public_context(response)["code"] == code


def test_an_engine_error_text_never_reaches_the_rollback_response(
    integration_api, world, controller_calls, monkeypatch
):
    import app.exceptions.HandlerExceptions as handler_exceptions

    # `context` only travels to the client in development; a production client is what matters.
    monkeypatch.setattr(handler_exceptions, "APP_ENV", "production")
    controller_calls["rollback_error"] = AppHttpException(
        message="La operación falló.",
        status_code=409,
        public_context={"code": "migration.rollback_failed"},
        context={"engine": SECRET_FRAGMENT_IN_ENGINE_ERROR},
    )

    response = integration_api.post(
        _rollback_path(world["database_id"]), json=_rollback_body(), headers=world["headers"]
    )

    assert SECRET_FRAGMENT_IN_ENGINE_ERROR not in response.text, response.text


# --------------------------------------------------------------------------- #
# Stamp                                                                        #
# --------------------------------------------------------------------------- #


def test_stamp_delegates_with_force_and_purge_fixed_to_false(
    integration_api, world, controller_calls
):
    response = integration_api.post(
        _stamp_path(world["database_id"]), json=_stamp_body(), headers=world["headers"]
    )

    assert response.status_code == 200, response.text
    assert controller_calls["stamp_calls"] == [
        {
            "db_id": world["database_id"],
            "version": SECOND_VERSION,
            "force": False,
            "purge": False,
            "actor_kind": "integration",
        }
    ]
    assert len(_audit_rows(STAMP_ACTION, "attempt")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"version": SECOND_VERSION},
        {"expected_current_version": THIRD_VERSION},
        {"expected_current_version": THIRD_VERSION, "version": SECOND_VERSION, "force": True},
        {"expected_current_version": THIRD_VERSION, "version": SECOND_VERSION, "purge": True},
        {"expected_current_version": THIRD_VERSION, "version": None},
        {"expected_current_version": "not-a-version", "version": SECOND_VERSION},
    ],
    ids=[
        "expected_current_version key omitted",
        "version omitted",
        "force rejected",
        "purge rejected",
        "null version",
        "malformed expected version",
    ],
)
def test_stamp_body_is_closed_and_the_expected_version_key_is_required(
    integration_api, world, controller_calls, body
):
    response = integration_api.post(
        _stamp_path(world["database_id"]), json=body, headers=world["headers"]
    )

    assert response.status_code == 422, response.text
    assert controller_calls["stamp_calls"] == []


def test_stamp_accepts_a_null_expected_version_for_a_database_with_no_version(
    integration_api, world, controller_calls
):
    controller_calls["current_version"] = None

    response = integration_api.post(
        _stamp_path(world["database_id"]),
        json=_stamp_body(expected=None, version=FIRST_VERSION),
        headers=world["headers"],
    )

    assert response.status_code == 200, response.text
    assert controller_calls["stamp_calls"][0]["version"] == FIRST_VERSION


def test_stamp_with_a_stale_expected_version_is_a_409_and_stamps_nothing(
    integration_api, world, controller_calls
):
    controller_calls["current_version"] = SECOND_VERSION

    response = integration_api.post(
        _stamp_path(world["database_id"]),
        json=_stamp_body(expected=THIRD_VERSION, version=FIRST_VERSION),
        headers=world["headers"],
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_STAMP_VERSION_CONFLICT
    assert controller_calls["stamp_calls"] == []
    assert _audit_rows(STAMP_ACTION, "attempt") == []


def test_stamp_to_the_version_the_database_is_already_on_is_an_audited_no_op(
    integration_api, world, controller_calls
):
    controller_calls["current_version"] = THIRD_VERSION

    response = integration_api.post(
        _stamp_path(world["database_id"]),
        json=_stamp_body(expected=THIRD_VERSION, version=THIRD_VERSION),
        headers=world["headers"],
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["current_version"] == THIRD_VERSION
    assert controller_calls["stamp_calls"] == []
    assert len(_audit_rows(STAMP_ACTION, "attempt")) == 1


def test_a_no_op_stamp_with_a_stale_expected_version_is_still_a_conflict(
    integration_api, world, controller_calls
):
    """Already-current only counts when the caller's belief about the current version is right."""
    controller_calls["current_version"] = THIRD_VERSION

    response = integration_api.post(
        _stamp_path(world["database_id"]),
        json=_stamp_body(expected=SECOND_VERSION, version=THIRD_VERSION),
        headers=world["headers"],
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_STAMP_VERSION_CONFLICT


def test_stamp_with_orphan_accounting_is_a_409_and_stamps_nothing(
    integration_api, world, controller_calls
):
    controller_calls["has_orphan_accounting"] = True

    response = integration_api.post(
        _stamp_path(world["database_id"]), json=_stamp_body(), headers=world["headers"]
    )

    assert response.status_code == 409, response.text
    assert _public_context(response)["code"] == CODE_INTEGRATION_STAMP_ORPHAN_ACCOUNTING
    assert controller_calls["stamp_calls"] == []


def test_stamp_with_a_failing_intent_is_a_500_and_the_controller_never_runs(
    integration_api, world, controller_calls, monkeypatch
):
    from app.services import audit

    def failing_record_intent(action, **kwargs):
        raise AppHttpException(message="No se pudo registrar la auditoría.", status_code=500)

    monkeypatch.setattr(audit, "record_intent", failing_record_intent)

    response = integration_api.post(
        _stamp_path(world["database_id"]), json=_stamp_body(), headers=world["headers"]
    )

    assert response.status_code == 500
    assert controller_calls["stamp_calls"] == []


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (422, "migration.version_not_found"),
        (409, "migration.unreviewed_capture_stamp"),
        (409, "migration.partial_application"),
    ],
)
def test_stamp_errors_of_the_controller_reach_the_caller_with_their_own_code(
    integration_api, world, controller_calls, status_code, code
):
    controller_calls["stamp_error"] = AppHttpException(
        message="Rechazado por el controlador.",
        status_code=status_code,
        public_context={"code": code},
    )

    response = integration_api.post(
        _stamp_path(world["database_id"]), json=_stamp_body(), headers=world["headers"]
    )

    assert response.status_code == status_code
    assert _public_context(response)["code"] == code


# --------------------------------------------------------------------------- #
# Transversal                                                                  #
# --------------------------------------------------------------------------- #

STILL_UNROUTED_OPERATIONS = [
    ("POST", f"{DATABASES_PATH}/1/migrations/reconcile"),
    ("POST", f"{DATABASES_PATH}/1/migrations/apply-all"),
    ("DELETE", f"{DATABASES_PATH}/1"),
    ("POST", f"{DATABASES_PATH}/1/migrations/rollback-all"),
    ("POST", f"{DATABASES_PATH}/1/migrations/purge"),
]


@pytest.mark.parametrize(("http_method", "path"), STILL_UNROUTED_OPERATIONS)
def test_the_destructive_tier_does_not_open_any_other_destructive_route(
    integration_api, admin_client, owner_issuer, http_method, path
):
    server_id = _create_server(admin_client, 3960)
    headers = _bearer(
        _make_token(
            owner_issuer, [scope.value for scope in IntegrationScope], server_ids=(server_id,)
        ).bearer
    )

    response = integration_api.request(http_method, path, headers=headers)

    assert response.status_code in {404, 405}, response.text


def test_the_responses_and_the_audit_trail_carry_no_token_secret(
    integration_api, admin_client, owner_issuer, controller_calls
):
    server_id = _create_server(admin_client, 3961)
    database_id = _create_database_with_blueprint(
        admin_client, server_id, name="dest_secret", migrations=THREE_MIGRATIONS,
        environment_slug="development",
    )
    _seed_full_applied_history(database_id)
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.MIGRATIONS_ROLLBACK.value],
        server_ids=(server_id,),
        blueprint_ids=(_model_id_of(database_id),),
    )

    response = integration_api.post(
        _rollback_path(database_id), json=_rollback_body(), headers=_bearer(minted.bearer)
    )

    assert minted.secret not in response.text
    assert minted.bearer not in response.text
    assert minted.secret not in _all_audit_text()
    assert minted.bearer not in _all_audit_text()
