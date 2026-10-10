"""
Integration API operations (``/api/v1/integration/...``): the read/write scopes of the REST
bearer, with the engine adapters mocked (no real engine).

What is measured here is the OPERATION contract on top of the gate that ``test_integration_auth``
already covers: allowlist filtering of lists, the closed request bodies, the server-built grant
mapping, the one-time generated engine-user password, the blueprint-assignment and forward-only
migration rules. The authentication, rate-limit and layer-2 gates are not re-tested here beyond
the cases where an operation depends on them.

The REAL controllers run: only the call that would reach a remote engine is replaced.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import Database
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_BLUEPRINT_NOT_ASSIGNED,
    CODE_INTEGRATION_SCOPE_MISSING,
    CODE_INTEGRATION_SERVER_NOT_ALLOWED,
    IntegrationScope,
)
from tests.scope_helpers import sembrar_bd
from tests.test_integration_auth import (
    _audit_rows,
    _bearer,
    _create_issuer,
    _make_token,
    _public_context,
)

API_PREFIX = "/api/v1/integration"
SERVERS_PATH = f"{API_PREFIX}/servers"
DATABASES_PATH = f"{API_PREFIX}/databases"


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def integration_api(client):
    """A cookie-less client on the full app with the kill switch ON (the normal case here)."""
    import app.core.integration_auth as integration_auth
    import main

    previous_flag = integration_auth.INTEGRATION_API_ENABLED
    integration_auth.INTEGRATION_API_ENABLED = True
    try:
        yield TestClient(main.app)
    finally:
        integration_auth.INTEGRATION_API_ENABLED = previous_flag


@pytest.fixture()
def owner_issuer(client) -> int:
    return _create_issuer("emisor-ops", "owner")


def _create_server(admin_client, port: int, engine: str = "mysql") -> int:
    response = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}",
            "host": "10.0.0.7",
            "port": port,
            "engine": engine,
            "root_username": "root",
            "root_password": "rootpw",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _create_blueprint(name: str) -> int:
    from app.models.database_model import DatabaseModel

    session = Database().get_declarative_base_session()
    try:
        blueprint = DatabaseModel(name=name, slug=name.lower().replace(" ", "-"))
        session.add(blueprint)
        session.commit()
        return blueprint.id
    finally:
        session.close()


def _assign_blueprint_row(database_id: int, model_id: int | None) -> None:
    with Database().engine.begin() as connection:
        connection.execute(
            text("UPDATE managed_databases SET model_id = :m WHERE id = :d"),
            {"m": model_id, "d": database_id},
        )


def _token_headers(issuer_id: int, scopes: list[IntegrationScope], **allowlists: Any) -> dict:
    minted = _make_token(issuer_id, [scope.value for scope in scopes], **allowlists)
    return _bearer(minted.bearer)


# --------------------------------------------------------------------------- #
# Ops 1-4: reads                                                               #
# --------------------------------------------------------------------------- #


def test_servers_list_returns_only_the_allowlisted_servers_with_a_minimal_projection(
    integration_api, admin_client, owner_issuer
):
    allowed_server = _create_server(admin_client, 3401)
    _create_server(admin_client, 3402)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.SERVERS_LIST], server_ids=(allowed_server,)
    )

    response = integration_api.get(SERVERS_PATH, headers=headers)

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert [item["id"] for item in items] == [allowed_server]
    # No host, port, credential flags or timestamps of the inventory row leave through here.
    assert set(items[0]) == {"id", "name", "engine"}
    assert response.json()["pagination"]["total"] == 1


def test_servers_list_paginates_over_the_allowlist_only(
    integration_api, admin_client, owner_issuer
):
    server_ids = tuple(_create_server(admin_client, 3410 + offset) for offset in range(3))
    _create_server(admin_client, 3419)
    headers = _token_headers(owner_issuer, [IntegrationScope.SERVERS_LIST], server_ids=server_ids)

    first_page = integration_api.get(SERVERS_PATH, params={"page": 1, "size": 2}, headers=headers)
    second_page = integration_api.get(SERVERS_PATH, params={"page": 2, "size": 2}, headers=headers)

    assert first_page.json()["pagination"]["total"] == 3
    assert [item["id"] for item in first_page.json()["data"]] == sorted(server_ids, reverse=True)[
        :2
    ]
    assert [item["id"] for item in second_page.json()["data"]] == sorted(server_ids)[:1]


def test_servers_list_skips_an_allowlisted_server_that_no_longer_exists(
    integration_api, admin_client, owner_issuer
):
    existing_server = _create_server(admin_client, 3420)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.SERVERS_LIST], server_ids=(existing_server, 987654)
    )

    response = integration_api.get(SERVERS_PATH, headers=headers)

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == [existing_server]


def test_servers_list_needs_its_own_scope(integration_api, admin_client, owner_issuer):
    server_id = _create_server(admin_client, 3421)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_LIST], server_ids=(server_id,)
    )

    response = integration_api.get(SERVERS_PATH, headers=headers)

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SCOPE_MISSING


def test_databases_list_returns_the_databases_of_the_requested_server_only(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3430)
    other_server_id = _create_server(admin_client, 3431)
    own_database = sembrar_bd(server_id=server_id, name="ops_list_own")
    sembrar_bd(server_id=other_server_id, name="ops_list_other")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_LIST], server_ids=(server_id, other_server_id)
    )

    response = integration_api.get(DATABASES_PATH, params={"server_id": server_id}, headers=headers)

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert [item["id"] for item in items] == [own_database]
    assert {"id", "name", "server_id", "model_id", "status", "environment_id"} <= set(items[0])
    assert "owner_id" not in items[0]
    assert "notes" not in items[0]


def test_databases_list_hides_a_database_whose_blueprint_is_outside_the_allowlist(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3440)
    allowed_blueprint_id = _create_blueprint("List Allowed")
    forbidden_blueprint_id = _create_blueprint("List Forbidden")
    allowed_database = sembrar_bd(server_id=server_id, name="ops_list_bp_allowed")
    forbidden_database = sembrar_bd(server_id=server_id, name="ops_list_bp_forbidden")
    unassigned_database = sembrar_bd(server_id=server_id, name="ops_list_bp_none")
    _assign_blueprint_row(allowed_database, allowed_blueprint_id)
    _assign_blueprint_row(forbidden_database, forbidden_blueprint_id)
    _assign_blueprint_row(unassigned_database, None)
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.DATABASES_LIST],
        server_ids=(server_id,),
        blueprint_ids=(allowed_blueprint_id,),
    )

    response = integration_api.get(DATABASES_PATH, params={"server_id": server_id}, headers=headers)

    assert response.status_code == 200, response.text
    listed_ids = {item["id"] for item in response.json()["data"]}
    # A database with no blueprint stays visible so it can still be assigned one.
    assert listed_ids == {allowed_database, unassigned_database}


def test_databases_list_on_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, owner_issuer
):
    allowed_server = _create_server(admin_client, 3432)
    foreign_server = _create_server(admin_client, 3433)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_LIST], server_ids=(allowed_server,)
    )

    foreign = integration_api.get(
        DATABASES_PATH, params={"server_id": foreign_server}, headers=headers
    )
    unknown = integration_api.get(DATABASES_PATH, params={"server_id": 987654}, headers=headers)

    # Anti-enumeration: an unknown server and a real one outside the allowlist answer identically.
    # This deliberately replaces the "unknown server -> 404" of the spec.
    assert foreign.status_code == unknown.status_code == 403
    assert _public_context(foreign)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert _public_context(unknown)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert foreign.json() == unknown.json()


def test_databases_list_requires_the_server_id(integration_api, admin_client, owner_issuer):
    server_id = _create_server(admin_client, 3434)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_LIST], server_ids=(server_id,)
    )

    response = integration_api.get(DATABASES_PATH, headers=headers)

    assert response.status_code == 422


def test_databases_list_omits_a_database_the_issuer_cannot_read_there(
    integration_api, admin_client, owner_issuer, monkeypatch
):
    """
    Layer 2 of a list is the row filter (the list has no single target the gate could check).
    The verdict of ``partition_by_scope`` is replaced by one that forbids a single database.
    """
    import app.controllers.integration_ops_controller as ops_controller
    from app.core.scope import ScopePartition

    server_id = _create_server(admin_client, 3435)
    visible_database = sembrar_bd(server_id=server_id, name="ops_visible")
    hidden_database = sembrar_bd(server_id=server_id, name="ops_hidden")

    def fake_partition(*, actor, capability, points):
        return ScopePartition((visible_database,), (hidden_database,))

    monkeypatch.setattr(ops_controller, "partition_by_scope", fake_partition)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_LIST], server_ids=(server_id,)
    )

    response = integration_api.get(DATABASES_PATH, params={"server_id": server_id}, headers=headers)

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == [visible_database]
    assert response.json()["pagination"]["total"] == 1


def test_blueprint_read_returns_the_assigned_blueprint(integration_api, admin_client, owner_issuer):
    server_id = _create_server(admin_client, 3440)
    database_id = sembrar_bd(server_id=server_id, name="ops_bp_read")
    blueprint_id = _create_blueprint("Billing Model")
    _assign_blueprint_row(database_id, blueprint_id)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED], server_ids=(server_id,)
    )

    response = integration_api.get(f"{DATABASES_PATH}/{database_id}/blueprint", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["model_id"] == blueprint_id
    assert data["name"] == "Billing Model"
    assert data["slug"] == "billing-model"
    assert set(data) == {"model_id", "name", "slug", "model_version"}


def test_blueprint_read_of_a_database_without_blueprint_is_a_404_with_a_stable_code(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3441)
    database_id = sembrar_bd(server_id=server_id, name="ops_bp_none")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED], server_ids=(server_id,)
    )

    response = integration_api.get(f"{DATABASES_PATH}/{database_id}/blueprint", headers=headers)

    assert response.status_code == 404
    assert _public_context(response)["code"] == CODE_INTEGRATION_BLUEPRINT_NOT_ASSIGNED


def test_blueprint_read_of_a_database_on_a_server_outside_the_allowlist_is_403(
    integration_api, admin_client, owner_issuer
):
    allowed_server = _create_server(admin_client, 3442)
    foreign_server = _create_server(admin_client, 3443)
    foreign_database = sembrar_bd(server_id=foreign_server, name="ops_bp_foreign")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED], server_ids=(allowed_server,)
    )

    response = integration_api.get(
        f"{DATABASES_PATH}/{foreign_database}/blueprint", headers=headers
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED


def test_migration_version_returns_current_latest_and_pending_only(
    integration_api, admin_client, owner_issuer, monkeypatch
):
    from app.controllers.managed_migration_controller import ManagedMigrationController

    server_id = _create_server(admin_client, 3450)
    database_id = sembrar_bd(server_id=server_id, name="ops_version")
    seen_database_ids: list[int] = []

    def fake_status(self, db_id: int) -> dict:
        seen_database_ids.append(db_id)
        return {
            "managed_database_id": db_id,
            "current_version": "0002",
            "latest_available": "0004",
            "pending_versions": ["0003", "0004"],
            "pending_count": 2,
            "cached_version": "0002",
            "orphan_version_tables": ["_gw_v_old"],
            "has_orphan_accounting": False,
        }

    monkeypatch.setattr(ManagedMigrationController, "status", fake_status)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.MIGRATIONS_READ_VERSION], server_ids=(server_id,)
    )

    response = integration_api.get(
        f"{DATABASES_PATH}/{database_id}/migrations/version", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "current_version": "0002",
        "latest_version": "0004",
        "pending": ["0003", "0004"],
    }
    assert seen_database_ids == [database_id]


def test_migration_version_needs_its_own_scope(integration_api, admin_client, owner_issuer):
    server_id = _create_server(admin_client, 3451)
    database_id = sembrar_bd(server_id=server_id, name="ops_version_scope")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED], server_ids=(server_id,)
    )

    response = integration_api.get(
        f"{DATABASES_PATH}/{database_id}/migrations/version", headers=headers
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SCOPE_MISSING


def test_every_read_operation_is_audited_as_an_integration_call(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3452)
    headers = _token_headers(owner_issuer, [IntegrationScope.SERVERS_LIST], server_ids=(server_id,))

    integration_api.get(SERVERS_PATH, headers=headers)

    assert len(_audit_rows("integration.call")) == 1


# --------------------------------------------------------------------------- #
# Ops 5-6: create a database / create an engine user                          #
# --------------------------------------------------------------------------- #

ENGINE_USERS_PATH = f"{API_PREFIX}/engine-users"
MINIMUM_GENERATED_PASSWORD_LENGTH = 24


class _RecordingDatabaseAdapter:
    """Stands in for the engine: records ``CREATE DATABASE`` calls and creates nothing."""

    def __init__(self) -> None:
        self.created_databases: list[dict[str, Any]] = []

    def create_database(self, name, charset=None, collation=None, owner=None) -> None:
        self.created_databases.append(
            {"name": name, "charset": charset, "collation": collation, "owner": owner}
        )


class _RecordingUserAdapter:
    """Stands in for the engine: records ``CREATE USER`` calls (and the password it received)."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.created_users: list[dict[str, Any]] = []
        self.failure = failure

    def create_user(self, username, password, host="%") -> None:
        if self.failure is not None:
            raise self.failure
        self.created_users.append({"username": username, "password": password, "host": host})


@pytest.fixture()
def database_adapter(monkeypatch) -> _RecordingDatabaseAdapter:
    import app.controllers.managed_database_controller as managed_database_controller

    adapter = _RecordingDatabaseAdapter()
    monkeypatch.setattr(managed_database_controller, "get_adapter", lambda target: adapter)
    return adapter


@pytest.fixture()
def user_adapter(monkeypatch) -> _RecordingUserAdapter:
    import app.controllers.server_user_controller as server_user_controller

    adapter = _RecordingUserAdapter()
    monkeypatch.setattr(server_user_controller, "get_adapter", lambda target: adapter)
    return adapter


def _create_engine_user_row(admin_client, server_id: int, username: str) -> int:
    response = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _all_audit_text() -> str:
    """Every column of every audit row, as one string: what an auditor (or a leak) could read."""
    with Database().engine.begin() as connection:
        rows = connection.execute(text("SELECT * FROM audit_log")).fetchall()
    return "\n".join(str(value) for row in rows for value in row)


def test_databases_create_provisions_an_empty_database_and_returns_the_projection(
    integration_api, admin_client, owner_issuer, database_adapter
):
    server_id = _create_server(admin_client, 3501)
    owner_user_id = _create_engine_user_row(admin_client, server_id, "ops_db_owner")
    minted = _make_token(
        owner_issuer, [IntegrationScope.DATABASES_CREATE.value], server_ids=(server_id,)
    )

    response = integration_api.post(
        DATABASES_PATH,
        json={"name": "tenant_a", "server_id": server_id, "owner_id": owner_user_id},
        headers=_bearer(minted.bearer),
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["name"] == "tenant_a"
    assert data["server_id"] == server_id
    assert data["status"] == "active"
    assert data["model_id"] is None
    assert "owner_id" not in data
    assert [call["name"] for call in database_adapter.created_databases] == ["tenant_a"]
    assert database_adapter.created_databases[0]["owner"] == "ops_db_owner"
    # The audit row carries the TOKEN as the actor of the creation, not a human session.
    creation_rows = _audit_rows("managed_database.create")
    assert len(creation_rows) == 1
    assert creation_rows[0].integration_token_id == minted.pk


@pytest.mark.parametrize(
    "forbidden_field",
    [
        {"model_id": 1},
        {"model_version": "0001"},
        {"apply_migrations": True},
        {"target_version": "0001"},
        {"provision": False},
    ],
)
def test_databases_create_rejects_the_fields_that_belong_to_other_scopes(
    integration_api, admin_client, owner_issuer, database_adapter, forbidden_field
):
    server_id = _create_server(admin_client, 3502)
    owner_user_id = _create_engine_user_row(admin_client, server_id, "ops_db_owner2")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_CREATE], server_ids=(server_id,)
    )

    response = integration_api.post(
        DATABASES_PATH,
        json={
            "name": "tenant_b",
            "server_id": server_id,
            "owner_id": owner_user_id,
            **forbidden_field,
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert database_adapter.created_databases == []


def test_databases_create_rejects_an_invalid_name_before_touching_the_engine(
    integration_api, admin_client, owner_issuer, database_adapter
):
    server_id = _create_server(admin_client, 3503)
    owner_user_id = _create_engine_user_row(admin_client, server_id, "ops_db_owner3")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_CREATE], server_ids=(server_id,)
    )

    response = integration_api.post(
        DATABASES_PATH,
        json={"name": "bad-name; DROP", "server_id": server_id, "owner_id": owner_user_id},
        headers=headers,
    )

    assert response.status_code == 422
    assert database_adapter.created_databases == []


def test_databases_create_with_a_duplicate_name_is_a_409(
    integration_api, admin_client, owner_issuer, database_adapter
):
    server_id = _create_server(admin_client, 3504)
    owner_user_id = _create_engine_user_row(admin_client, server_id, "ops_db_owner4")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_CREATE], server_ids=(server_id,)
    )
    body = {"name": "tenant_dup", "server_id": server_id, "owner_id": owner_user_id}

    first = integration_api.post(DATABASES_PATH, json=body, headers=headers)
    second = integration_api.post(DATABASES_PATH, json=body, headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert len(database_adapter.created_databases) == 1


def test_databases_create_with_an_owner_of_another_server_is_a_409(
    integration_api, admin_client, owner_issuer, database_adapter
):
    server_id = _create_server(admin_client, 3505)
    other_server_id = _create_server(admin_client, 3506)
    foreign_owner_id = _create_engine_user_row(admin_client, other_server_id, "ops_foreign_owner")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_CREATE], server_ids=(server_id,)
    )

    response = integration_api.post(
        DATABASES_PATH,
        json={"name": "tenant_c", "server_id": server_id, "owner_id": foreign_owner_id},
        headers=headers,
    )

    assert response.status_code == 409
    assert database_adapter.created_databases == []


def test_databases_create_on_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, owner_issuer, database_adapter
):
    allowed_server = _create_server(admin_client, 3507)
    foreign_server = _create_server(admin_client, 3508)
    foreign_owner_id = _create_engine_user_row(admin_client, foreign_server, "ops_out_owner")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_CREATE], server_ids=(allowed_server,)
    )

    response = integration_api.post(
        DATABASES_PATH,
        json={"name": "tenant_d", "server_id": foreign_server, "owner_id": foreign_owner_id},
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert database_adapter.created_databases == []


def test_databases_create_needs_its_own_scope(
    integration_api, admin_client, owner_issuer, database_adapter
):
    server_id = _create_server(admin_client, 3509)
    owner_user_id = _create_engine_user_row(admin_client, server_id, "ops_db_owner5")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_LIST], server_ids=(server_id,)
    )

    response = integration_api.post(
        DATABASES_PATH,
        json={"name": "tenant_e", "server_id": server_id, "owner_id": owner_user_id},
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SCOPE_MISSING


def test_engine_user_create_generates_the_password_and_returns_it_exactly_once(
    integration_api, admin_client, owner_issuer, user_adapter
):
    server_id = _create_server(admin_client, 3510)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(server_id,)
    )

    response = integration_api.post(
        ENGINE_USERS_PATH,
        json={"server_id": server_id, "username": "app_user"},
        headers=headers,
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["username"] == "app_user"
    assert data["host"] == "%"
    assert data["has_password"] is True
    generated_password = data["password"]
    assert len(generated_password) >= MINIMUM_GENERATED_PASSWORD_LENGTH
    # The engine received exactly the password the client was handed.
    assert user_adapter.created_users == [
        {"username": "app_user", "password": generated_password, "host": "%"}
    ]
    # Nothing readable afterwards: the human read of the same row shows only ``has_password``.
    reread = admin_client.get(f"/api/v1/server-users/{data['id']}")
    assert reread.status_code == 200, reread.text
    assert "password" not in reread.json()["data"]
    assert generated_password not in reread.text


def test_engine_user_create_generates_a_different_password_every_time(
    integration_api, admin_client, owner_issuer, user_adapter
):
    server_id = _create_server(admin_client, 3511)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(server_id,)
    )

    passwords = []
    for username in ("pw_user_one", "pw_user_two"):
        response = integration_api.post(
            ENGINE_USERS_PATH, json={"server_id": server_id, "username": username}, headers=headers
        )
        assert response.status_code == 201, response.text
        passwords.append(response.json()["data"]["password"])

    assert passwords[0] != passwords[1]


def test_engine_user_create_refuses_a_client_chosen_password(
    integration_api, admin_client, owner_issuer, user_adapter
):
    server_id = _create_server(admin_client, 3512)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(server_id,)
    )

    response = integration_api.post(
        ENGINE_USERS_PATH,
        json={"server_id": server_id, "username": "chosen_pw", "password": "client-picked-1"},
        headers=headers,
    )

    assert response.status_code == 422
    assert user_adapter.created_users == []
    assert "client-picked-1" not in response.text


def test_engine_user_create_with_a_duplicate_username_is_a_409_without_a_password(
    integration_api, admin_client, owner_issuer, user_adapter
):
    server_id = _create_server(admin_client, 3513)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(server_id,)
    )
    body = {"server_id": server_id, "username": "dup_user"}

    first = integration_api.post(ENGINE_USERS_PATH, json=body, headers=headers)
    second = integration_api.post(ENGINE_USERS_PATH, json=body, headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert first.json()["data"]["password"] not in second.text


def test_engine_user_create_on_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, owner_issuer, user_adapter
):
    allowed_server = _create_server(admin_client, 3514)
    foreign_server = _create_server(admin_client, 3515)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(allowed_server,)
    )

    response = integration_api.post(
        ENGINE_USERS_PATH,
        json={"server_id": foreign_server, "username": "out_user"},
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert user_adapter.created_users == []


def test_engine_user_create_failure_leaves_no_row_and_no_password_in_the_response(
    integration_api, admin_client, owner_issuer, monkeypatch
):
    import app.controllers.server_user_controller as server_user_controller
    from app.exceptions import AppHttpException

    failing_adapter = _RecordingUserAdapter(
        failure=AppHttpException("motor inaccesible", 502, {"op": "create_user"})
    )
    monkeypatch.setattr(server_user_controller, "get_adapter", lambda target: failing_adapter)
    server_id = _create_server(admin_client, 3516)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(server_id,)
    )

    response = integration_api.post(
        ENGINE_USERS_PATH, json={"server_id": server_id, "username": "fail_user"}, headers=headers
    )

    assert response.status_code == 502, response.text
    # No `data` envelope means no generated password was returned. The raw text is not searched:
    # in development the error also echoes the test's own name, which contains "password".
    assert "data" not in response.json()
    listing = admin_client.get(f"/api/v1/server-users?server_id={server_id}").json()["data"]
    assert all(user["username"] != "fail_user" for user in listing)


def test_the_generated_password_never_reaches_the_audit_trail_or_the_logs(
    integration_api, admin_client, owner_issuer, user_adapter
):
    import logging

    # A handler of our own instead of `caplog`: it also runs under scripts/run_tests_direct.py.
    # Loggers that set `propagate=False` never reach the root logger, so this covers the
    # propagating ones only.
    captured_messages: list[str] = []

    class _CollectingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_messages.append(record.getMessage())

    collecting_handler = _CollectingHandler(level=logging.DEBUG)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(collecting_handler)
    root_logger.setLevel(logging.DEBUG)

    server_id = _create_server(admin_client, 3517)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.ENGINE_USERS_CREATE], server_ids=(server_id,)
    )

    try:
        response = integration_api.post(
            ENGINE_USERS_PATH,
            json={"server_id": server_id, "username": "quiet_user"},
            headers=headers,
        )
    finally:
        root_logger.removeHandler(collecting_handler)
        root_logger.setLevel(previous_level)

    assert response.status_code == 201, response.text
    generated_password = response.json()["data"]["password"]
    assert generated_password not in _all_audit_text()
    assert generated_password not in "\n".join(captured_messages)


def test_a_driver_error_raised_while_creating_the_user_carries_neither_statement_nor_password(
    monkeypatch,
):
    """
    ``CREATE USER ... IDENTIFIED BY '<password>'`` is the one statement that holds the generated
    secret. The error the adapter raises for a failing engine must not echo it: not in the message,
    not in the structured context, not in the serialized detail.
    """
    from sqlalchemy.exc import ProgrammingError

    import app.services.db_admin.base_adapter as base_adapter
    from app.core.remote_engine import ServerTarget
    from app.exceptions import AppHttpException
    from app.services.db_admin.mysql_adapter import MySQLAdapter

    secret_password = "Zq9-leak-canary-Zq9"
    statement_text = f"CREATE USER 'u'@'%' IDENTIFIED BY '{secret_password}'"

    class _FailingConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, statement):
            raise ProgrammingError(
                statement_text, {}, Exception(1064, f"syntax error near '{secret_password}'")
            )

    monkeypatch.setattr(base_adapter, "server_connection", lambda target: _FailingConnection())
    adapter = MySQLAdapter(
        ServerTarget(
            server_id=1,
            dialect="mysql",
            host="10.0.0.7",
            port=3306,
            admin_user="root",
            admin_password="root-secret",
        )
    )
    with pytest.raises(AppHttpException) as raised:
        adapter.create_user("u", secret_password, "%")

    leaked_surfaces = [
        raised.value.message,
        repr(raised.value.context),
        repr(raised.value.public_context),
        repr(raised.value.detail),
    ]
    for surface in leaked_surfaces:
        assert secret_password not in surface
        assert "IDENTIFIED BY" not in surface


# --------------------------------------------------------------------------- #
# Ops 7-8: assign a permission profile (to an engine user, to one database)   #
# --------------------------------------------------------------------------- #

APPLIED_DATABASE_NAME = "app_db"
DATABASE_AND_TABLE_ITEMS = [
    {"level": "database", "privileges": ["SELECT"]},
    {"level": "table", "privileges": ["SELECT", "INSERT"]},
]
# ALL PRIVILEGES is in the GATE set of MySQL at database level: granting it delegates power.
SENSITIVE_DATABASE_ITEM = {"level": "database", "privileges": ["ALL PRIVILEGES"]}
CODE_PROFILE_REQUIRES_GRANT_ADMIN = "integration.profile_requires_grant_admin"
CODE_SERVER_MISMATCH = "integration.server_mismatch"


def _create_profile(admin_client, name: str, items: list[dict[str, Any]]) -> int:
    response = admin_client.post(
        "/api/v1/permission-profiles", json={"name": name, "engine": "mysql", "items": items}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


@pytest.fixture()
def grant_calls(monkeypatch) -> list[tuple]:
    """Replaces the two adapter calls that reach the engine; records ``(op, level, database, table)``."""
    from app.services.db_admin.mysql_adapter import MySQLAdapter

    recorded_calls: list[tuple] = []

    def fake_can_grant(self, level, ref, privileges):
        recorded_calls.append(("can_grant", level.value, ref.database, ref.table))
        return True

    def fake_grant_object(self, grantee, level, ref, privileges, **kwargs):
        recorded_calls.append(("grant", level.value, ref.database, ref.table))

    monkeypatch.setattr(MySQLAdapter, "can_grant", fake_can_grant)
    monkeypatch.setattr(MySQLAdapter, "grant_object", fake_grant_object)
    return recorded_calls


def _executed_grants(recorded_calls: list[tuple]) -> list[tuple]:
    return [recorded_call for recorded_call in recorded_calls if recorded_call[0] == "grant"]


@pytest.fixture()
def profile_world(admin_client, owner_issuer) -> dict[str, Any]:
    """A server, an engine user, a managed database and a harmless two-level profile."""
    server_id = _create_server(admin_client, 3601)
    return {
        "server_id": server_id,
        "user_id": _create_engine_user_row(admin_client, server_id, "profile_user"),
        "database_id": sembrar_bd(server_id=server_id, name=APPLIED_DATABASE_NAME),
        "profile_id": _create_profile(admin_client, "ops-rw", DATABASE_AND_TABLE_ITEMS),
    }


def _profile_path(user_id: int, profile_id: int) -> str:
    return f"{ENGINE_USERS_PATH}/{user_id}/profiles/{profile_id}"


def _assign_database_path(user_id: int, database_id: int) -> str:
    return f"{ENGINE_USERS_PATH}/{user_id}/databases/{database_id}"


def _object_mappings(database_name: str = APPLIED_DATABASE_NAME) -> dict[str, Any]:
    return {
        "object_mappings": [
            {"level": "database", "object_ref": {"database": database_name}},
            {"level": "table", "object_ref": {"database": database_name, "table": "orders"}},
        ]
    }


def test_assign_profile_applies_the_mapped_levels_on_the_managed_database(
    integration_api, owner_issuer, profile_world, grant_calls
):
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE.value],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _profile_path(profile_world["user_id"], profile_world["profile_id"]),
        json=_object_mappings(),
        headers=_bearer(minted.bearer),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["grants_applied"] == 2
    assert _executed_grants(grant_calls) == [
        ("grant", "database", APPLIED_DATABASE_NAME, None),
        ("grant", "table", APPLIED_DATABASE_NAME, "orders"),
    ]
    applied_rows = _audit_rows("server_user.apply_profile")
    assert len(applied_rows) == 1
    assert applied_rows[0].integration_token_id == minted.pk


def test_assign_profile_on_a_database_with_a_blueprint_outside_the_allowlist_is_a_403(
    integration_api, owner_issuer, profile_world, grant_calls
):
    allowed_blueprint_id = _create_blueprint("Profile Allowed")
    forbidden_blueprint_id = _create_blueprint("Profile Forbidden")
    _assign_blueprint_row(profile_world["database_id"], forbidden_blueprint_id)
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE],
        server_ids=(profile_world["server_id"],),
        blueprint_ids=(allowed_blueprint_id,),
    )
    path = _profile_path(profile_world["user_id"], profile_world["profile_id"])

    fenced_out = integration_api.post(path, json=_object_mappings(), headers=headers)
    _assign_blueprint_row(profile_world["database_id"], allowed_blueprint_id)
    allowed = integration_api.post(path, json=_object_mappings(), headers=headers)

    assert fenced_out.status_code == 403
    assert _public_context(fenced_out)["code"] == CODE_BLUEPRINT_NOT_ALLOWED
    assert allowed.status_code == 200, allowed.text
    assert len(_executed_grants(grant_calls)) == 2


def test_assign_profile_with_a_delegating_item_is_a_403_and_grants_nothing(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    """
    ``apply_profile`` never asks for ``engine_users.grant_admin``, so without the pre-check a
    profile holding ``ALL PRIVILEGES`` would hand a token the power it was never given. The
    harmless item comes FIRST: the check must cover every item before the first grant runs.
    """
    sensitive_profile_id = _create_profile(
        admin_client,
        "ops-sensitive",
        [{"level": "table", "privileges": ["SELECT"]}, SENSITIVE_DATABASE_ITEM],
    )
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _profile_path(profile_world["user_id"], sensitive_profile_id),
        json=_object_mappings(),
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_PROFILE_REQUIRES_GRANT_ADMIN
    assert _executed_grants(grant_calls) == []


def test_assign_profile_to_a_database_that_is_not_managed_on_the_users_server_is_a_409(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    other_server_id = _create_server(admin_client, 3602)
    sembrar_bd(server_id=other_server_id, name="db_of_the_other_server")
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE],
        server_ids=(profile_world["server_id"], other_server_id),
    )

    on_other_server = integration_api.post(
        _profile_path(profile_world["user_id"], profile_world["profile_id"]),
        json=_object_mappings("db_of_the_other_server"),
        headers=headers,
    )
    never_registered = integration_api.post(
        _profile_path(profile_world["user_id"], profile_world["profile_id"]),
        json=_object_mappings("db_nobody_registered"),
        headers=headers,
    )

    for response in (on_other_server, never_registered):
        assert response.status_code == 409
        assert _public_context(response)["code"] == CODE_SERVER_MISMATCH
    assert _executed_grants(grant_calls) == []


@pytest.mark.parametrize(
    "invalid_mappings",
    [
        {"object_mappings": [{"level": "global", "object_ref": {"database": "app_db"}}]},
        {"object_mappings": [{"level": "database", "object_ref": {}}]},
        {"object_mappings": []},
        {"object_mappings": [{"level": "database", "object_ref": {"database": "app_db"}}], "x": 1},
    ],
)
def test_assign_profile_rejects_global_or_database_less_or_unknown_mappings(
    integration_api, owner_issuer, profile_world, grant_calls, invalid_mappings
):
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _profile_path(profile_world["user_id"], profile_world["profile_id"]),
        json=invalid_mappings,
        headers=headers,
    )

    assert response.status_code == 422
    assert _executed_grants(grant_calls) == []


def test_assign_profile_to_a_user_of_a_foreign_or_unknown_server_is_the_uniform_403(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    foreign_server_id = _create_server(admin_client, 3603)
    foreign_user_id = _create_engine_user_row(admin_client, foreign_server_id, "foreign_user")
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE],
        server_ids=(profile_world["server_id"],),
    )

    foreign = integration_api.post(
        _profile_path(foreign_user_id, profile_world["profile_id"]),
        json=_object_mappings(),
        headers=headers,
    )
    unknown = integration_api.post(
        _profile_path(987654, profile_world["profile_id"]),
        json=_object_mappings(),
        headers=headers,
    )

    assert foreign.status_code == unknown.status_code == 403
    assert _public_context(foreign)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert foreign.json() == unknown.json()
    assert _executed_grants(grant_calls) == []


def test_assign_profile_with_an_unknown_profile_is_a_404_and_grants_nothing(
    integration_api, owner_issuer, profile_world, grant_calls
):
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _profile_path(profile_world["user_id"], 987654), json=_object_mappings(), headers=headers
    )

    assert response.status_code == 404
    assert _executed_grants(grant_calls) == []


def test_assign_profile_needs_its_own_scope(
    integration_api, owner_issuer, profile_world, grant_calls
):
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _profile_path(profile_world["user_id"], profile_world["profile_id"]),
        json=_object_mappings(),
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SCOPE_MISSING


def test_assign_database_builds_the_database_level_mapping_on_the_server_side(
    integration_api, owner_issuer, profile_world, grant_calls
):
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE.value],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _assign_database_path(profile_world["user_id"], profile_world["database_id"]),
        json={"profile_id": profile_world["profile_id"]},
        headers=_bearer(minted.bearer),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["grants_applied"] == 1
    # The table-level item has no mapping the gateway could build: it is skipped, not guessed.
    assert data["skipped_levels"] == ["table"]
    assert _executed_grants(grant_calls) == [("grant", "database", APPLIED_DATABASE_NAME, None)]
    applied_rows = _audit_rows("server_user.apply_profile")
    assert len(applied_rows) == 1
    assert applied_rows[0].integration_token_id == minted.pk


def test_assign_database_does_not_accept_client_supplied_mappings(
    integration_api, owner_issuer, profile_world, grant_calls
):
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _assign_database_path(profile_world["user_id"], profile_world["database_id"]),
        json={"profile_id": profile_world["profile_id"], **_object_mappings("other_db")},
        headers=headers,
    )

    assert response.status_code == 422
    assert _executed_grants(grant_calls) == []


def test_assign_database_with_a_delegating_item_anywhere_in_the_profile_is_a_403(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    """Only the database level would run, but a delegating item ANYWHERE in the profile blocks it."""
    sensitive_table_profile_id = _create_profile(
        admin_client,
        "ops-sensitive-table",
        [
            {"level": "database", "privileges": ["SELECT"]},
            {"level": "table", "privileges": ["ALL PRIVILEGES"]},
        ],
    )
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _assign_database_path(profile_world["user_id"], profile_world["database_id"]),
        json={"profile_id": sensitive_table_profile_id},
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_PROFILE_REQUIRES_GRANT_ADMIN
    assert _executed_grants(grant_calls) == []


def test_assign_database_with_a_user_of_another_server_or_a_missing_user_is_the_same_409(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    other_server_id = _create_server(admin_client, 3604)
    foreign_user_id = _create_engine_user_row(admin_client, other_server_id, "mismatch_user")
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(profile_world["server_id"], other_server_id),
    )
    body = {"profile_id": profile_world["profile_id"]}

    foreign = integration_api.post(
        _assign_database_path(foreign_user_id, profile_world["database_id"]),
        json=body,
        headers=headers,
    )
    missing = integration_api.post(
        _assign_database_path(987654, profile_world["database_id"]), json=body, headers=headers
    )

    # Same answer for a real user of another server and for an id that does not exist: the
    # endpoint is not an oracle for engine-user ids.
    assert foreign.status_code == missing.status_code == 409
    assert _public_context(foreign)["code"] == CODE_SERVER_MISMATCH
    assert foreign.json() == missing.json()
    assert _executed_grants(grant_calls) == []


def test_assign_database_on_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    allowed_server_id = _create_server(admin_client, 3605)
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(allowed_server_id,),
    )

    response = integration_api.post(
        _assign_database_path(profile_world["user_id"], profile_world["database_id"]),
        json={"profile_id": profile_world["profile_id"]},
        headers=headers,
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert _executed_grants(grant_calls) == []


def test_assign_database_with_a_profile_that_has_no_database_level_item_is_a_422(
    integration_api, admin_client, owner_issuer, profile_world, grant_calls
):
    table_only_profile_id = _create_profile(
        admin_client, "ops-table-only", [{"level": "table", "privileges": ["SELECT"]}]
    )
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _assign_database_path(profile_world["user_id"], profile_world["database_id"]),
        json={"profile_id": table_only_profile_id},
        headers=headers,
    )

    assert response.status_code == 422
    assert _executed_grants(grant_calls) == []


def test_assign_database_with_an_unknown_profile_is_a_404(
    integration_api, owner_issuer, profile_world, grant_calls
):
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE],
        server_ids=(profile_world["server_id"],),
    )

    response = integration_api.post(
        _assign_database_path(profile_world["user_id"], profile_world["database_id"]),
        json={"profile_id": 987654},
        headers=headers,
    )

    assert response.status_code == 404
    assert _executed_grants(grant_calls) == []


# --------------------------------------------------------------------------- #
# Op 9: assign a blueprint to a database                                       #
# --------------------------------------------------------------------------- #

CODE_BLUEPRINT_ALREADY_ASSIGNED = "integration.blueprint_already_assigned"
CODE_BLUEPRINT_NOT_ALLOWED = "integration.blueprint_not_allowed"


def _blueprint_path(database_id: int) -> str:
    return f"{DATABASES_PATH}/{database_id}/blueprint"


def _stored_blueprint_id(database_id: int) -> int | None:
    with Database().engine.begin() as connection:
        return connection.execute(
            text("SELECT model_id FROM managed_databases WHERE id = :d"), {"d": database_id}
        ).scalar()


def test_assign_blueprint_to_a_database_without_one_assigns_it(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3701)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_bp")
    blueprint_id = _create_blueprint("Assign One")
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT.value],
        server_ids=(server_id,),
    )

    response = integration_api.put(
        _blueprint_path(database_id),
        json={"model_id": blueprint_id},
        headers=_bearer(minted.bearer),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["model_id"] == blueprint_id
    assert _stored_blueprint_id(database_id) == blueprint_id
    update_rows = _audit_rows("managed_database.update")
    assert len(update_rows) == 1
    assert update_rows[0].integration_token_id == minted.pk


def test_assigning_the_same_blueprint_again_is_an_idempotent_success(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3702)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_same")
    blueprint_id = _create_blueprint("Assign Same")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT], server_ids=(server_id,)
    )

    first = integration_api.put(
        _blueprint_path(database_id), json={"model_id": blueprint_id}, headers=headers
    )
    second = integration_api.put(
        _blueprint_path(database_id), json={"model_id": blueprint_id}, headers=headers
    )

    assert first.status_code == second.status_code == 200
    assert second.json()["data"]["model_id"] == blueprint_id
    # The repeat changed nothing, so it left no second inventory-update row.
    assert len(_audit_rows("managed_database.update")) == 1


def test_assigning_a_different_blueprint_is_a_409_and_changes_nothing(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3703)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_other")
    current_blueprint_id = _create_blueprint("Current One")
    other_blueprint_id = _create_blueprint("Other One")
    _assign_blueprint_row(database_id, current_blueprint_id)
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT], server_ids=(server_id,)
    )

    response = integration_api.put(
        _blueprint_path(database_id), json={"model_id": other_blueprint_id}, headers=headers
    )

    assert response.status_code == 409
    assert _public_context(response)["code"] == CODE_BLUEPRINT_ALREADY_ASSIGNED
    assert _stored_blueprint_id(database_id) == current_blueprint_id


def test_assigning_a_blueprint_outside_the_token_allowlist_is_a_403_and_changes_nothing(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3704)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_blocked")
    allowed_blueprint_id = _create_blueprint("Allowed One")
    forbidden_blueprint_id = _create_blueprint("Forbidden One")
    headers = _token_headers(
        owner_issuer,
        [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT],
        server_ids=(server_id,),
        blueprint_ids=(allowed_blueprint_id,),
    )

    forbidden = integration_api.put(
        _blueprint_path(database_id), json={"model_id": forbidden_blueprint_id}, headers=headers
    )
    allowed = integration_api.put(
        _blueprint_path(database_id), json={"model_id": allowed_blueprint_id}, headers=headers
    )

    assert forbidden.status_code == 403
    assert _public_context(forbidden)["code"] == CODE_BLUEPRINT_NOT_ALLOWED
    assert allowed.status_code == 200, allowed.text
    assert _stored_blueprint_id(database_id) == allowed_blueprint_id


def test_assigning_a_blueprint_that_does_not_exist_is_a_422(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3705)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_ghost")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT], server_ids=(server_id,)
    )

    response = integration_api.put(
        _blueprint_path(database_id), json={"model_id": 987654}, headers=headers
    )

    assert response.status_code == 422
    assert _stored_blueprint_id(database_id) is None


@pytest.mark.parametrize(
    "unexpected_body",
    [
        {"model_id": 1, "environment_id": 1},
        {"model_id": 1, "notes": "x"},
        {"model_id": 0},
        {},
    ],
)
def test_assign_blueprint_only_accepts_a_model_id(
    integration_api, admin_client, owner_issuer, unexpected_body
):
    server_id = _create_server(admin_client, 3706)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_body")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT], server_ids=(server_id,)
    )

    response = integration_api.put(
        _blueprint_path(database_id), json=unexpected_body, headers=headers
    )

    assert response.status_code == 422
    assert _stored_blueprint_id(database_id) is None


def test_assign_blueprint_on_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, owner_issuer
):
    allowed_server_id = _create_server(admin_client, 3707)
    foreign_server_id = _create_server(admin_client, 3708)
    foreign_database_id = sembrar_bd(server_id=foreign_server_id, name="ops_assign_foreign")
    blueprint_id = _create_blueprint("Foreign One")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT], server_ids=(allowed_server_id,)
    )

    response = integration_api.put(
        _blueprint_path(foreign_database_id), json={"model_id": blueprint_id}, headers=headers
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert _stored_blueprint_id(foreign_database_id) is None


def test_assign_blueprint_needs_its_own_scope(integration_api, admin_client, owner_issuer):
    server_id = _create_server(admin_client, 3709)
    database_id = sembrar_bd(server_id=server_id, name="ops_assign_scope")
    blueprint_id = _create_blueprint("Scope One")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED], server_ids=(server_id,)
    )

    response = integration_api.put(
        _blueprint_path(database_id), json={"model_id": blueprint_id}, headers=headers
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SCOPE_MISSING


# --------------------------------------------------------------------------- #
# Op 10: apply migrations forward                                              #
# --------------------------------------------------------------------------- #

CODE_MIGRATION_TARGET_NOT_FORWARD = "integration.migration_target_not_forward"
CODE_DESTRUCTIVE_BLOCKED = "environment.destructive_blocked"
SAFE_MIGRATION_SQL = "CREATE TABLE ops_t1 (id INT PRIMARY KEY)"
DESTRUCTIVE_MIGRATION_SQL = "DROP TABLE clientes"


def _apply_path(database_id: int) -> str:
    return f"{DATABASES_PATH}/{database_id}/migrations/apply"


@pytest.fixture()
def recorded_apply(monkeypatch) -> list[dict[str, Any]]:
    """
    Replaces ``ManagedMigrationController.apply`` and ``.status`` to see the EXACT arguments the
    integration operation delegates with. The current version is ``"0002"``.
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController

    recorded_calls: list[dict[str, Any]] = []

    def fake_status(self, db_id: int) -> dict:
        return {
            "current_version": "0002",
            "latest_available": "0005",
            "pending_versions": ["0003", "0004", "0005"],
        }

    def fake_apply(
        self,
        db_id,
        *,
        up_to_version=None,
        force=False,
        dry_run=False,
        on_failure="auto",
        admin=None,
    ):
        recorded_calls.append(
            {
                "db_id": db_id,
                "up_to_version": up_to_version,
                "force": force,
                "dry_run": dry_run,
                "on_failure": on_failure,
                "actor_kind": getattr(admin, "kind", None),
            }
        )
        return {"managed_database_id": db_id}

    monkeypatch.setattr(ManagedMigrationController, "status", fake_status)
    monkeypatch.setattr(ManagedMigrationController, "apply", fake_apply)
    return recorded_calls


def _apply_token_headers(issuer_id: int, server_id: int) -> dict:
    return _token_headers(
        issuer_id, [IntegrationScope.MIGRATIONS_APPLY_FORWARD], server_ids=(server_id,)
    )


def _create_database_with_blueprint(
    admin_client, server_id: int, *, name: str, migrations: list[dict], environment_slug: str
) -> int:
    """A real blueprint with real migrations and a database in the given environment."""
    environments = admin_client.get("/api/v1/environments?size=50").json()["data"]
    environment_id = next(item["id"] for item in environments if item["slug"] == environment_slug)
    blueprint = admin_client.post("/api/v1/database-models", json={"name": name, "slug": name})
    assert blueprint.status_code == 201, blueprint.text
    blueprint_id = blueprint.json()["data"]["id"]
    for migration in migrations:
        created = admin_client.post(
            f"/api/v1/database-models/{blueprint_id}/migrations", json=migration
        )
        assert created.status_code == 201, created.text
    owner_id = _create_engine_user_row(admin_client, server_id, f"owner_{name}")
    database = admin_client.post(
        "/api/v1/managed-databases",
        json={
            "name": name,
            "server_id": server_id,
            "owner_id": owner_id,
            "model_id": blueprint_id,
            "environment_id": environment_id,
        },
    )
    assert database.status_code == 201, database.text
    return database.json()["data"]["id"]


@pytest.fixture()
def runner_calls(monkeypatch) -> dict[str, list]:
    """The migration runner without an engine: the current version is settable, ``apply`` is logged."""
    from app.services.db_admin.migrations import MigrationRunner

    state: dict[str, Any] = {"current_version": None, "apply_calls": []}

    monkeypatch.setattr(
        MigrationRunner,
        "get_current_version",
        lambda self, *args, **kwargs: state["current_version"],
    )

    def fake_runner_apply(self, *args, **kwargs):
        state["apply_calls"].append((args, kwargs))
        return []

    monkeypatch.setattr(MigrationRunner, "apply", fake_runner_apply)
    return state


def test_apply_delegates_with_the_fixed_safety_arguments_and_the_integration_actor(
    integration_api, admin_client, owner_issuer, recorded_apply
):
    server_id = _create_server(admin_client, 3801)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_args")
    headers = _apply_token_headers(owner_issuer, server_id)

    response = integration_api.post(
        _apply_path(database_id), json={"version": "0004"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert recorded_apply == [
        {
            "db_id": database_id,
            "up_to_version": "0004",
            "force": False,
            "dry_run": False,
            "on_failure": "auto",
            "actor_kind": "integration",
        }
    ]


def test_apply_without_a_version_targets_the_latest(
    integration_api, admin_client, owner_issuer, recorded_apply
):
    server_id = _create_server(admin_client, 3802)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_latest")

    response = integration_api.post(
        _apply_path(database_id), json={}, headers=_apply_token_headers(owner_issuer, server_id)
    )

    assert response.status_code == 200, response.text
    assert recorded_apply[0]["up_to_version"] is None


@pytest.mark.parametrize(
    "forbidden_field",
    [{"force": True}, {"on_failure": "leave"}, {"version": "v1"}, {"version": "12"}],
)
def test_apply_rejects_force_on_failure_and_malformed_versions(
    integration_api, admin_client, owner_issuer, recorded_apply, forbidden_field
):
    server_id = _create_server(admin_client, 3803)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_closed")

    response = integration_api.post(
        _apply_path(database_id),
        json=forbidden_field,
        headers=_apply_token_headers(owner_issuer, server_id),
    )

    assert response.status_code == 422
    assert recorded_apply == []


def test_apply_ignores_a_force_query_parameter(
    integration_api, admin_client, owner_issuer, recorded_apply
):
    """``force`` is not part of the contract in ANY position: a query string cannot enable it."""
    server_id = _create_server(admin_client, 3804)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_query")

    response = integration_api.post(
        f"{_apply_path(database_id)}?force=true&on_failure=leave",
        json={},
        headers=_apply_token_headers(owner_issuer, server_id),
    )

    assert response.status_code == 200, response.text
    assert recorded_apply[0]["force"] is False
    assert recorded_apply[0]["on_failure"] == "auto"


def test_apply_to_a_version_older_than_the_current_one_is_rejected_and_applies_nothing(
    integration_api, admin_client, owner_issuer, recorded_apply
):
    server_id = _create_server(admin_client, 3805)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_downgrade")

    response = integration_api.post(
        _apply_path(database_id),
        json={"version": "0001"},
        headers=_apply_token_headers(owner_issuer, server_id),
    )

    assert response.status_code == 422
    assert _public_context(response)["code"] == CODE_MIGRATION_TARGET_NOT_FORWARD
    assert recorded_apply == []


def test_apply_to_the_current_version_is_not_a_downgrade(
    integration_api, admin_client, owner_issuer, recorded_apply
):
    server_id = _create_server(admin_client, 3806)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_same")

    response = integration_api.post(
        _apply_path(database_id),
        json={"version": "0002"},
        headers=_apply_token_headers(owner_issuer, server_id),
    )

    assert response.status_code == 200, response.text
    assert recorded_apply[0]["up_to_version"] == "0002"


def test_apply_dry_run_returns_the_plan_and_changes_nothing(
    integration_api, admin_client, owner_issuer, runner_calls
):
    server_id = _create_server(admin_client, 3807)
    database_id = _create_database_with_blueprint(
        admin_client,
        server_id,
        name="ops_dry",
        migrations=[
            {"version": "0001", "name": "first", "up_sql": SAFE_MIGRATION_SQL},
            {
                "version": "0002",
                "name": "second",
                "up_sql": "CREATE TABLE ops_t2 (id INT PRIMARY KEY)",
            },
        ],
        environment_slug="development",
    )

    response = integration_api.post(
        _apply_path(database_id),
        json={"dry_run": True},
        headers=_apply_token_headers(owner_issuer, server_id),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["pending_versions"] == ["0001", "0002"]
    assert runner_calls["apply_calls"] == []


def test_apply_when_the_database_is_already_at_the_latest_is_a_successful_no_op(
    integration_api, admin_client, owner_issuer, runner_calls
):
    server_id = _create_server(admin_client, 3808)
    database_id = _create_database_with_blueprint(
        admin_client,
        server_id,
        name="ops_latest",
        migrations=[{"version": "0001", "name": "first", "up_sql": SAFE_MIGRATION_SQL}],
        environment_slug="development",
    )
    runner_calls["current_version"] = "0001"

    response = integration_api.post(
        _apply_path(database_id), json={}, headers=_apply_token_headers(owner_issuer, server_id)
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["no_op"] is True
    # The managed controller always delegates to the runner, which is the one that finds nothing
    # pending; asserting "runner not called" would test the implementation, not the contract.
    assert data["applied_count"] == 0


def test_apply_to_production_still_hits_the_destructive_guard_and_applies_nothing(
    integration_api, admin_client, owner_issuer, runner_calls
):
    """
    ``blueprints.apply`` for a human needs a step-up; for a token the issuer confirmed it when the
    token was issued. What must NOT be waived is the environment guard: a destructive migration on
    a production database is refused for the token exactly as for a person, ``force`` or not.
    """
    server_id = _create_server(admin_client, 3809)
    database_id = _create_database_with_blueprint(
        admin_client,
        server_id,
        name="ops_prod",
        migrations=[{"version": "0001", "name": "drop", "up_sql": DESTRUCTIVE_MIGRATION_SQL}],
        environment_slug="production",
    )
    headers = _apply_token_headers(owner_issuer, server_id)

    plain = integration_api.post(_apply_path(database_id), json={}, headers=headers)
    with_query_force = integration_api.post(
        f"{_apply_path(database_id)}?force=true", json={}, headers=headers
    )

    for response in (plain, with_query_force):
        assert response.status_code == 409, response.text
        assert _public_context(response)["code"] == CODE_DESTRUCTIVE_BLOCKED
    assert runner_calls["apply_calls"] == []


def test_apply_needs_its_own_scope(integration_api, admin_client, owner_issuer, recorded_apply):
    server_id = _create_server(admin_client, 3810)
    database_id = sembrar_bd(server_id=server_id, name="ops_apply_scope")
    headers = _token_headers(
        owner_issuer, [IntegrationScope.MIGRATIONS_READ_VERSION], server_ids=(server_id,)
    )

    response = integration_api.post(_apply_path(database_id), json={}, headers=headers)

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SCOPE_MISSING
    assert recorded_apply == []


def test_apply_on_a_server_outside_the_allowlist_is_the_uniform_403(
    integration_api, admin_client, owner_issuer, recorded_apply
):
    allowed_server_id = _create_server(admin_client, 3811)
    foreign_server_id = _create_server(admin_client, 3812)
    foreign_database_id = sembrar_bd(server_id=foreign_server_id, name="ops_apply_foreign")

    response = integration_api.post(
        _apply_path(foreign_database_id),
        json={},
        headers=_apply_token_headers(owner_issuer, allowed_server_id),
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED
    assert recorded_apply == []


# --------------------------------------------------------------------------- #
# Transversal: what the integration API must NOT do, and what it always records #
# --------------------------------------------------------------------------- #

MANAGEMENT_TOKENS_PATH = "/api/v1/integration-tokens"
ROUTE_DOES_NOT_EXIST_STATUSES = {404, 405}

#: Operations that stay out of the integration API in this tier: deletes, server edit, password
#: reveal, partial reconcile, apply-all and any data access. Rollback and stamp are NOT listed
#: here: the destructive tier adds them with their own contract (test_integration_destructive_ops).
OUT_OF_SCOPE_OPERATIONS = [
    ("DELETE", f"{DATABASES_PATH}/1"),
    ("DELETE", f"{DATABASES_PATH}/1/blueprint"),
    ("DELETE", f"{API_PREFIX}/servers/1"),
    ("DELETE", f"{API_PREFIX}/engine-users/1"),
    ("PATCH", f"{API_PREFIX}/servers/1"),
    ("PUT", f"{API_PREFIX}/servers/1"),
    ("POST", f"{API_PREFIX}/servers"),
    ("GET", f"{API_PREFIX}/engine-users/1/password"),
    ("POST", f"{API_PREFIX}/engine-users/1/reveal-password"),
    ("POST", f"{DATABASES_PATH}/1/migrations/reconcile"),
    ("POST", f"{DATABASES_PATH}/1/migrations/reconcile-partial"),
    ("POST", f"{DATABASES_PATH}/1/migrations/apply-all"),
    ("POST", f"{API_PREFIX}/migrations/apply-all"),
    ("POST", f"{DATABASES_PATH}/1/query"),
    ("GET", f"{DATABASES_PATH}/1/data"),
    ("GET", f"{DATABASES_PATH}/1/tables/clientes/rows"),
    ("POST", f"{API_PREFIX}/blueprints"),
    ("POST", f"{API_PREFIX}/blueprints/1/apply"),
]


@pytest.mark.parametrize(
    ("http_method", "path"),
    OUT_OF_SCOPE_OPERATIONS,
    ids=[f"{method} {path}" for method, path in OUT_OF_SCOPE_OPERATIONS],
)
def test_operations_outside_the_closed_set_are_not_routed_even_with_a_full_token(
    integration_api, admin_client, owner_issuer, http_method, path
):
    server_id = _create_server(admin_client, 3901)
    every_scope = list(IntegrationScope)
    headers = _token_headers(owner_issuer, every_scope, server_ids=(server_id,))

    response = integration_api.request(http_method, path, headers=headers)

    assert response.status_code in ROUTE_DOES_NOT_EXIST_STATUSES, response.text


def test_the_scope_vocabulary_never_names_blueprint_authoring_or_data_access():
    forbidden_prefixes = ("data.", "blueprints.", "access.", "crypto.", "audit.")

    for scope in IntegrationScope:
        assert not scope.value.startswith(forbidden_prefixes), scope.value


def test_an_integration_bearer_does_not_reach_the_token_management_routes(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3902)
    headers = _token_headers(owner_issuer, [IntegrationScope.SERVERS_LIST], server_ids=(server_id,))

    list_response = integration_api.get(MANAGEMENT_TOKENS_PATH, headers=headers)
    create_response = integration_api.post(
        MANAGEMENT_TOKENS_PATH,
        json={"name": "escalation", "scopes": [IntegrationScope.SERVERS_LIST.value]},
        headers=headers,
    )

    assert list_response.status_code == 401
    assert create_response.status_code == 401


def test_every_call_is_audited_with_the_integration_actor_and_the_token_id(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3903)
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.SERVERS_LIST.value, IntegrationScope.DATABASES_LIST.value],
        server_ids=(server_id,),
    )
    headers = {"Authorization": f"Bearer {minted.bearer}"}

    integration_api.get(SERVERS_PATH, headers=headers)
    integration_api.get(DATABASES_PATH, params={"server_id": server_id}, headers=headers)

    call_rows = _audit_rows("integration.call", "success")
    assert len(call_rows) == 2
    for call_row in call_rows:
        assert call_row.actor_type == "integration"
        assert call_row.integration_token_id == minted.pk


def test_no_secret_reaches_a_response_or_the_audit_trail(
    integration_api, admin_client, owner_issuer
):
    server_id = _create_server(admin_client, 3904)
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.SERVERS_LIST.value, IntegrationScope.DATABASES_LIST.value],
        server_ids=(server_id,),
    )
    headers = {"Authorization": f"Bearer {minted.bearer}"}

    responses = [
        integration_api.get(SERVERS_PATH, headers=headers),
        integration_api.get(DATABASES_PATH, params={"server_id": server_id}, headers=headers),
        integration_api.get(f"{DATABASES_PATH}/999999/blueprint", headers=headers),
    ]

    for response in responses:
        assert minted.secret not in response.text
        assert minted.bearer not in response.text
        assert "rootpw" not in response.text
    all_audit_text = _all_audit_text()
    assert minted.secret not in all_audit_text
    assert minted.bearer not in all_audit_text
