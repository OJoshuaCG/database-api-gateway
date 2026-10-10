"""
Management API of the integration tokens (``/api/v1/integration-tokens``), read and write tiers.

WHAT IS PINNED
--------------
- The secret travels once, in the creation response, and only its HMAC is stored.
- Ownership: ``integration_tokens.own`` manages only the caller's tokens (a foreign token is the
  same 404 as a missing one); ``access.admin`` lists and revokes every token and edits none.
- Lifetime: 90 days with read scopes only, 30 days with any write scope; an edit that adds a write
  scope to a token that outlives the write cap is refused and leaves the token untouched (D9).
- The issuer ceiling: a scope the caller does not hold is refused, and the ceiling endpoint omits
  it entirely (no entry, flag or count).
- Suspended scopes are reported on the owner's own tokens, cannot be re-added and can be removed.
- The kill switch stops issuing and editing, but never listing or revoking.

The destructive tier (rollback and stamp, CR-1) is covered at the end of this module: 7 days,
mandatory blueprint allowlist, an explicit fresh step-up of the issuer when a destructive scope is
added, and the ``max_destructive_ttl_days`` of the ceiling.
"""

# ruff: noqa: F811 — the imported fixtures are requested by parameter, which is how pytest uses them.
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import Database
from app.core.integration_auth import integration_token_hmac
from app.models.audit_log import AuditLog
from app.models.integration_token import (
    IntegrationToken,
    IntegrationTokenBlueprint,
    IntegrationTokenServer,
)
from app.services.capability_catalog import CODE_FORBIDDEN, CODE_STEP_UP_REQUIRED
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_DISABLED,
    CODE_INTEGRATION_TOKEN_ALREADY_REVOKED,
    CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED,
    CODE_INTEGRATION_TOKEN_BLUEPRINT_NOT_FOUND,
    CODE_INTEGRATION_TOKEN_NON_EXPIRING_NOT_ALLOWED,
    CODE_INTEGRATION_TOKEN_NOT_FOUND,
    CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED,
    CODE_INTEGRATION_TOKEN_SERVER_ALLOWLIST_REQUIRED,
    CODE_INTEGRATION_TOKEN_SERVER_NOT_FOUND,
    CODE_INTEGRATION_TOKEN_TTL_TOO_LONG,
    CODE_INTEGRATION_TOKEN_UNKNOWN_SCOPE,
    IntegrationScope,
)
from tests.access_request_helpers import client_as, create_user

TOKENS_PATH = "/api/v1/integration-tokens"
CEILING_PATH = f"{TOKENS_PATH}/ceiling"

READ_SCOPE = IntegrationScope.SERVERS_LIST.value
SECOND_READ_SCOPE = IntegrationScope.DATABASES_LIST.value
WRITE_SCOPE = IntegrationScope.DATABASES_CREATE.value
SECOND_WRITE_SCOPE = IntegrationScope.ENGINE_USERS_CREATE.value
APPLY_SCOPE = IntegrationScope.MIGRATIONS_APPLY_FORWARD.value
ROLLBACK_SCOPE = IntegrationScope.MIGRATIONS_ROLLBACK.value
STAMP_SCOPE = IntegrationScope.MIGRATIONS_STAMP.value

READ_ONLY_MAX_TTL_DAYS = 90
WRITE_MAX_TTL_DAYS = 30
DESTRUCTIVE_MAX_TTL_DAYS = 7
SECONDS_PER_DAY = 24 * 60 * 60

BEARER_PREFIX = "datumint."


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def integration_enabled(monkeypatch):
    """The kill switch ON, which is the normal case of this module."""
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", True)


def _person(admin_client, username: str, gateway_role: str = "viewer"):
    """``(client, user_id)`` of an account WITHOUT ``access.admin``, invitation already accepted."""
    invitation = create_user(admin_client, username, gateway_role=gateway_role)
    return client_as(invitation, username), invitation["id"]


def _server(admin_client, port: int) -> int:
    response = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}",
            "host": "10.0.0.5",
            "port": port,
            "engine": "mysql",
            "root_username": "root",
            "root_password": "pw",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _blueprint(admin_client, slug: str) -> int:
    response = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _create_payload(server_ids: list[int], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "ci-pipeline",
        "scopes": [READ_SCOPE],
        "server_ids": server_ids,
    }
    payload.update(overrides)
    return payload


def _issue(client, server_ids: list[int], **overrides: Any):
    return client.post(TOKENS_PATH, json=_create_payload(server_ids, **overrides))


def _code(response) -> str:
    return response.json()["detail"]["public_context"]["code"]


def _row(token_pk: int) -> IntegrationToken:
    session = Database().get_declarative_base_session()
    try:
        row = session.get(IntegrationToken, token_pk)
        session.expunge(row)
        return row
    finally:
        session.close()


def _stored_server_ids(token_pk: int) -> list[int]:
    session = Database().get_declarative_base_session()
    try:
        rows = (
            session.query(IntegrationTokenServer.server_id)
            .filter(IntegrationTokenServer.token_pk == token_pk)
            .order_by(IntegrationTokenServer.server_id)
            .all()
        )
        return [server_id for (server_id,) in rows]
    finally:
        session.close()


def _stored_blueprint_ids(token_pk: int) -> list[int]:
    session = Database().get_declarative_base_session()
    try:
        rows = (
            session.query(IntegrationTokenBlueprint.model_id)
            .filter(IntegrationTokenBlueprint.token_pk == token_pk)
            .order_by(IntegrationTokenBlueprint.model_id)
            .all()
        )
        return [model_id for (model_id,) in rows]
    finally:
        session.close()


def _audit_rows(action: str) -> list[AuditLog]:
    session = Database().get_declarative_base_session()
    try:
        rows = session.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
        session.expunge_all()
        return rows
    finally:
        session.close()


def _set_user_role(user_id: int, gateway_role: str) -> None:
    with Database().engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET gateway_role = :role WHERE id = :id"),
            {"role": gateway_role, "id": user_id},
        )


def _remaining_days(token_pk: int) -> float:
    from datetime import UTC, datetime

    expires_at = _row(token_pk).expires_at
    now = datetime.now(UTC).replace(tzinfo=None)
    return (expires_at - now).total_seconds() / SECONDS_PER_DAY


# --------------------------------------------------------------------------- #
# The secret travels once                                                      #
# --------------------------------------------------------------------------- #


def test_the_secret_is_returned_only_by_the_creation_and_only_its_hmac_is_stored(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3401)

    created = _issue(admin_client, [server_id])

    assert created.status_code == 201, created.text
    data = created.json()["data"]
    bearer = data["token"]
    assert bearer.startswith(BEARER_PREFIX)
    _, public_id, secret = bearer.split(".", 2)
    assert data["token_id"] == public_id
    row = _row(data["id"])
    assert row.secret_hmac == integration_token_hmac(secret)
    assert secret not in row.secret_hmac
    assert data["scopes"] == [READ_SCOPE]
    assert data["suspended_scopes"] == []
    assert data["server_ids"] == [server_id]

    listed = admin_client.get(TOKENS_PATH)

    assert listed.status_code == 200, listed.text
    assert secret not in listed.text
    assert "secret_hmac" not in listed.text
    assert all("token" not in item for item in listed.json()["data"])


def test_a_creation_response_never_leaks_the_secret_into_the_audit_trail(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3402)

    created = _issue(admin_client, [server_id])

    secret = created.json()["data"]["token"].split(".", 2)[2]
    for action in ("integration_token.create",):
        for audit_row in _audit_rows(action):
            assert secret not in (audit_row.detail or "")


# --------------------------------------------------------------------------- #
# Ownership                                                                    #
# --------------------------------------------------------------------------- #


def test_a_person_lists_only_their_own_tokens(admin_client, integration_enabled):
    server_id = _server(admin_client, 3403)
    ana, _ = _person(admin_client, "ana-it-list")
    beto, _ = _person(admin_client, "beto-it-list")
    ana_token = _issue(ana, [server_id], name="de-ana").json()["data"]["id"]
    beto_token = _issue(beto, [server_id], name="de-beto").json()["data"]["id"]

    listed = ana.get(f"{TOKENS_PATH}?size=50")

    assert listed.status_code == 200, listed.text
    listed_ids = {item["id"] for item in listed.json()["data"]}
    assert listed_ids == {ana_token}
    assert beto_token not in listed_ids


def test_a_foreign_token_is_the_same_404_as_a_missing_one_for_edit_and_revoke(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3404)
    ana, _ = _person(admin_client, "ana-it-foreign")
    beto, _ = _person(admin_client, "beto-it-foreign")
    ana_token = _issue(ana, [server_id]).json()["data"]["id"]
    missing_token_pk = 987654

    foreign_edit = beto.patch(f"{TOKENS_PATH}/{ana_token}", json={"name": "robado"})
    missing_edit = beto.patch(f"{TOKENS_PATH}/{missing_token_pk}", json={"name": "robado"})
    foreign_revoke = beto.delete(f"{TOKENS_PATH}/{ana_token}")
    missing_revoke = beto.delete(f"{TOKENS_PATH}/{missing_token_pk}")

    for response in (foreign_edit, missing_edit, foreign_revoke, missing_revoke):
        assert response.status_code == 404, response.text
        assert _code(response) == CODE_INTEGRATION_TOKEN_NOT_FOUND
    assert foreign_edit.json() == missing_edit.json()
    assert foreign_revoke.json() == missing_revoke.json()
    assert _row(ana_token).revoked_at is None
    assert _row(ana_token).name == "ci-pipeline"


def test_access_admin_lists_and_revokes_every_token_but_edits_none(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3405)
    ana, _ = _person(admin_client, "ana-it-admin")
    ana_token = _issue(ana, [server_id], name="de-ana").json()["data"]["id"]

    listed = admin_client.get(f"{TOKENS_PATH}?size=50")
    edited = admin_client.patch(f"{TOKENS_PATH}/{ana_token}", json={"name": "tocado-por-admin"})
    revoked = admin_client.delete(f"{TOKENS_PATH}/{ana_token}")

    assert ana_token in {item["id"] for item in listed.json()["data"]}
    assert edited.status_code == 404, edited.text
    assert _row(ana_token).name == "de-ana"
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["active"] is False
    assert _row(ana_token).revoked_at is not None


def test_a_bearer_is_rejected_on_the_management_routes(admin_client, integration_enabled):
    from main import app

    server_id = _server(admin_client, 3406)
    bearer = _issue(admin_client, [server_id]).json()["data"]["token"]
    bearer_only_client = TestClient(app)
    headers = {"Authorization": f"Bearer {bearer}"}

    listed = bearer_only_client.get(TOKENS_PATH, headers=headers)
    ceiling = bearer_only_client.get(CEILING_PATH, headers=headers)
    created = bearer_only_client.post(
        TOKENS_PATH, json=_create_payload([server_id]), headers=headers
    )

    for response in (listed, ceiling, created):
        assert response.status_code == 401, response.text


# --------------------------------------------------------------------------- #
# Lifetime                                                                     #
# --------------------------------------------------------------------------- #


def test_a_read_only_token_defaults_to_and_is_capped_at_ninety_days(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3407)

    default_ttl = _issue(admin_client, [server_id])
    over_the_cap = _issue(admin_client, [server_id], expires_in_days=READ_ONLY_MAX_TTL_DAYS + 1)
    at_the_cap = _issue(admin_client, [server_id], expires_in_days=READ_ONLY_MAX_TTL_DAYS)

    assert default_ttl.status_code == 201, default_ttl.text
    assert (
        READ_ONLY_MAX_TTL_DAYS - 1
        < _remaining_days(default_ttl.json()["data"]["id"])
        <= READ_ONLY_MAX_TTL_DAYS
    )
    assert over_the_cap.status_code == 422, over_the_cap.text
    assert _code(over_the_cap) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    assert at_the_cap.status_code == 201, at_the_cap.text


def test_a_token_with_a_write_scope_defaults_to_and_is_capped_at_thirty_days(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3408)

    default_ttl = _issue(admin_client, [server_id], scopes=[READ_SCOPE, WRITE_SCOPE])
    over_the_cap = _issue(
        admin_client,
        [server_id],
        scopes=[READ_SCOPE, WRITE_SCOPE],
        expires_in_days=WRITE_MAX_TTL_DAYS + 1,
    )
    at_the_cap = _issue(
        admin_client,
        [server_id],
        scopes=[WRITE_SCOPE],
        expires_in_days=WRITE_MAX_TTL_DAYS,
    )

    assert default_ttl.status_code == 201, default_ttl.text
    assert (
        WRITE_MAX_TTL_DAYS - 1
        < _remaining_days(default_ttl.json()["data"]["id"])
        <= WRITE_MAX_TTL_DAYS
    )
    assert over_the_cap.status_code == 422, over_the_cap.text
    assert _code(over_the_cap) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    assert at_the_cap.status_code == 201, at_the_cap.text


def test_an_edit_that_adds_a_write_scope_to_a_longer_lived_token_is_refused_and_changes_nothing(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3409)
    token_pk = _issue(admin_client, [server_id], expires_in_days=READ_ONLY_MAX_TTL_DAYS).json()[
        "data"
    ]["id"]
    expiry_before = _row(token_pk).expires_at

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE, WRITE_SCOPE]}
    )

    assert edited.status_code == 422, edited.text
    assert _code(edited) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    row_after = _row(token_pk)
    # No clamp: shortening the expiry behind the caller's back would break a pipeline silently.
    assert row_after.expires_at == expiry_before
    assert row_after.scopes == READ_SCOPE


def test_an_edit_that_adds_a_write_scope_to_a_token_within_the_write_cap_is_accepted(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3410)
    token_pk = _issue(admin_client, [server_id], expires_in_days=WRITE_MAX_TTL_DAYS).json()["data"][
        "id"
    ]

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE, WRITE_SCOPE]}
    )

    assert edited.status_code == 200, edited.text
    assert sorted(edited.json()["data"]["scopes"]) == sorted([READ_SCOPE, WRITE_SCOPE])


@pytest.fixture()
def non_expiring_tokens_enabled(monkeypatch):
    """The deployment opt-in for tokens without expiration."""
    import app.controllers.integration_token_controller as token_controller

    monkeypatch.setattr(token_controller, "INTEGRATION_ALLOW_NON_EXPIRING_TOKENS", True)


def test_a_non_expiring_token_is_rejected_when_the_deployment_does_not_allow_it(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3420)

    response = _issue(admin_client, [server_id], never_expires=True)

    assert response.status_code == 422, response.text
    assert _code(response) == CODE_INTEGRATION_TOKEN_NON_EXPIRING_NOT_ALLOWED


def test_a_read_only_and_a_write_token_can_be_issued_without_expiration_when_allowed(
    admin_client, integration_enabled, non_expiring_tokens_enabled
):
    server_id = _server(admin_client, 3421)

    read_only = _issue(admin_client, [server_id], never_expires=True)
    with_write = _issue(
        admin_client, [server_id], scopes=[READ_SCOPE, WRITE_SCOPE], never_expires=True
    )

    assert read_only.status_code == 201, read_only.text
    assert with_write.status_code == 201, with_write.text
    for response in (read_only, with_write):
        data = response.json()["data"]
        assert data["expires_at"] is None
        assert data["active"] is True
        assert _row(data["id"]).expires_at is None


def test_a_non_expiring_token_cannot_carry_a_destructive_scope(
    admin_client, integration_enabled, non_expiring_tokens_enabled
):
    server_id = _server(admin_client, 3422)
    blueprint_id = _blueprint(admin_client, "non-expiring-destructive")

    response = _destructive_issue(admin_client, server_id, blueprint_id, never_expires=True)

    assert response.status_code == 422, response.text
    assert _code(response) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    assert response.json()["detail"]["public_context"]["max_days"] == DESTRUCTIVE_MAX_TTL_DAYS


def test_never_expires_and_expires_in_days_are_mutually_exclusive(
    admin_client, integration_enabled, non_expiring_tokens_enabled
):
    server_id = _server(admin_client, 3423)

    response = _issue(admin_client, [server_id], never_expires=True, expires_in_days=10)

    assert response.status_code == 422, response.text


def test_a_non_expiring_token_may_gain_a_write_scope_but_not_a_destructive_one(
    admin_client, integration_enabled, non_expiring_tokens_enabled
):
    server_id = _server(admin_client, 3424)
    blueprint_id = _blueprint(admin_client, "non-expiring-edit")
    token_pk = _issue(admin_client, [server_id], never_expires=True).json()["data"]["id"]

    with_write = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE, WRITE_SCOPE]}
    )
    with_destructive = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}",
        json={"scopes": [READ_SCOPE, ROLLBACK_SCOPE], "blueprint_ids": [blueprint_id]},
    )

    assert with_write.status_code == 200, with_write.text
    assert with_destructive.status_code == 422, with_destructive.text
    assert _code(with_destructive) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    assert _row(token_pk).expires_at is None


def test_the_ceiling_tells_the_caller_whether_non_expiring_tokens_are_allowed(
    admin_client, integration_enabled, non_expiring_tokens_enabled
):
    response = admin_client.get(CEILING_PATH)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["allow_non_expiring"] is True


# --------------------------------------------------------------------------- #
# Step-up                                                                      #
# --------------------------------------------------------------------------- #


def test_issuing_a_token_with_a_write_scope_needs_a_fresh_step_up(
    admin_client, integration_enabled, expire_step_up
):
    server_id = _server(admin_client, 3411)
    expire_step_up(admin_client)

    created = _issue(admin_client, [server_id], scopes=[WRITE_SCOPE])

    assert created.status_code == 403, created.text
    assert _code(created) == CODE_STEP_UP_REQUIRED


def test_adding_a_write_scope_to_a_token_needs_a_fresh_step_up(
    admin_client, integration_enabled, expire_step_up
):
    server_id = _server(admin_client, 3412)
    token_pk = _issue(admin_client, [server_id], expires_in_days=WRITE_MAX_TTL_DAYS).json()["data"][
        "id"
    ]
    expire_step_up(admin_client)

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE, WRITE_SCOPE]}
    )

    assert edited.status_code == 403, edited.text
    assert _code(edited) == CODE_STEP_UP_REQUIRED
    assert _row(token_pk).scopes == READ_SCOPE


# --------------------------------------------------------------------------- #
# Scope vocabulary and the issuer ceiling                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "forged_scope",
    ["access.admin", "data.read", "blueprints.apply", "servers.delete", "no-such-scope", "*"],
)
def test_a_scope_outside_the_closed_vocabulary_is_a_422_on_create_and_edit(
    admin_client, integration_enabled, forged_scope
):
    server_id = _server(admin_client, 3413)
    token_pk = _issue(admin_client, [server_id]).json()["data"]["id"]

    created = _issue(admin_client, [server_id], scopes=[READ_SCOPE, forged_scope])
    edited = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"scopes": [forged_scope]})

    for response in (created, edited):
        assert response.status_code == 422, response.text
        assert _code(response) == CODE_INTEGRATION_TOKEN_UNKNOWN_SCOPE
    assert _row(token_pk).scopes == READ_SCOPE


def test_a_viewer_cannot_give_a_token_a_scope_its_role_does_not_hold(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3414)
    viewer, _ = _person(admin_client, "viewer-it-ceiling", "viewer")

    created = _issue(viewer, [server_id], scopes=[READ_SCOPE, WRITE_SCOPE])

    assert created.status_code == 403, created.text
    assert _code(created) == CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED
    assert created.json()["detail"]["public_context"] == {
        "code": CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED
    }


def test_an_operator_can_hold_write_scopes_but_not_the_owner_only_ones(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3415)
    operator, _ = _person(admin_client, "operator-it", "operator")

    operator_scopes = _issue(operator, [server_id], scopes=[WRITE_SCOPE, SECOND_WRITE_SCOPE])
    owner_only_scope = _issue(
        operator, [server_id], scopes=[IntegrationScope.MIGRATIONS_APPLY_FORWARD.value]
    )

    assert operator_scopes.status_code == 201, operator_scopes.text
    assert owner_only_scope.status_code == 403, owner_only_scope.text
    assert _code(owner_only_scope) == CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED


def test_the_ceiling_omits_the_scopes_the_caller_does_not_hold_without_any_hint(
    admin_client, integration_enabled
):
    viewer, _ = _person(admin_client, "viewer-it-picker", "viewer")

    viewer_ceiling = viewer.get(CEILING_PATH)
    owner_ceiling = admin_client.get(CEILING_PATH)

    assert viewer_ceiling.status_code == 200, viewer_ceiling.text
    viewer_data = viewer_ceiling.json()["data"]
    viewer_scopes = {entry["scope"] for entry in viewer_data["scopes"]}
    assert READ_SCOPE in viewer_scopes
    assert WRITE_SCOPE not in viewer_scopes
    assert all(entry["mutates"] is False for entry in viewer_data["scopes"])
    assert all(entry["tier"] == "read" for entry in viewer_data["scopes"])
    # Not a flag, not a count, not a name: the unheld scopes do not exist for this caller.
    assert WRITE_SCOPE not in viewer_ceiling.text
    assert set(viewer_data) == {
        "enabled",
        "scopes",
        "max_ttl_days",
        "max_write_ttl_days",
        "max_destructive_ttl_days",
        "allow_non_expiring",
    }
    assert set(viewer_data["scopes"][0]) == {"scope", "label", "mutates", "tier"}
    assert viewer_data["max_ttl_days"] == READ_ONLY_MAX_TTL_DAYS
    assert viewer_data["max_write_ttl_days"] == WRITE_MAX_TTL_DAYS
    owner_entries = {entry["scope"]: entry for entry in owner_ceiling.json()["data"]["scopes"]}
    assert owner_entries[WRITE_SCOPE]["mutates"] is True
    assert owner_entries[WRITE_SCOPE]["tier"] == "write"


def test_the_ceiling_reports_the_kill_switch_state(admin_client, monkeypatch):
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", False)
    disabled = admin_client.get(CEILING_PATH)
    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", True)
    enabled = admin_client.get(CEILING_PATH)

    assert disabled.json()["data"]["enabled"] is False
    assert enabled.json()["data"]["enabled"] is True


# --------------------------------------------------------------------------- #
# Suspended scopes                                                             #
# --------------------------------------------------------------------------- #


def test_a_scope_the_issuer_lost_is_suspended_not_re_addable_but_removable(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3416)
    operator, operator_id = _person(admin_client, "operator-it-suspended", "operator")
    token_pk = _issue(
        operator, [server_id], scopes=[READ_SCOPE, WRITE_SCOPE], expires_in_days=WRITE_MAX_TTL_DAYS
    ).json()["data"]["id"]
    _set_user_role(operator_id, "viewer")

    listed = operator.get(TOKENS_PATH)

    token_view = listed.json()["data"][0]
    assert token_view["scopes"] == [READ_SCOPE]
    assert token_view["suspended_scopes"] == [WRITE_SCOPE]
    # The stored row keeps it: promoting the issuer back reactivates it without re-issuing.
    assert WRITE_SCOPE in _row(token_pk).scopes

    re_added = operator.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE, WRITE_SCOPE]}
    )
    removed = operator.patch(f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE]})

    assert re_added.status_code == 403, re_added.text
    assert _code(re_added) == CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["suspended_scopes"] == []
    assert _row(token_pk).scopes == READ_SCOPE


def test_a_suspended_scope_recovers_when_the_issuer_regains_the_role(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3417)
    operator, operator_id = _person(admin_client, "operator-it-recovers", "operator")
    _issue(operator, [server_id], scopes=[READ_SCOPE, WRITE_SCOPE])
    _set_user_role(operator_id, "viewer")
    assert operator.get(TOKENS_PATH).json()["data"][0]["suspended_scopes"] == [WRITE_SCOPE]

    _set_user_role(operator_id, "operator")

    recovered = operator.get(TOKENS_PATH).json()["data"][0]
    assert recovered["suspended_scopes"] == []
    assert sorted(recovered["scopes"]) == sorted([READ_SCOPE, WRITE_SCOPE])


def test_an_out_of_vocabulary_stored_scope_is_reported_as_suspended_and_never_effective(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3418)
    token_pk = _issue(admin_client, [server_id]).json()["data"]["id"]
    with Database().engine.begin() as connection:
        connection.execute(
            text("UPDATE integration_tokens SET scopes = :scopes WHERE id = :id"),
            {"scopes": f"{READ_SCOPE},legacy.scope", "id": token_pk},
        )

    listed = admin_client.get(TOKENS_PATH).json()["data"][0]

    assert listed["scopes"] == [READ_SCOPE]
    assert listed["suspended_scopes"] == ["legacy.scope"]


# --------------------------------------------------------------------------- #
# Allowlists                                                                   #
# --------------------------------------------------------------------------- #


def test_an_empty_server_allowlist_is_rejected_on_create_and_edit(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3419)
    token_pk = _issue(admin_client, [server_id]).json()["data"]["id"]

    created = _issue(admin_client, [])
    edited = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"server_ids": []})

    for response in (created, edited):
        assert response.status_code == 422, response.text
        assert _code(response) == CODE_INTEGRATION_TOKEN_SERVER_ALLOWLIST_REQUIRED
    assert _stored_server_ids(token_pk) == [server_id]


def test_an_allowlisted_server_or_blueprint_that_does_not_exist_is_a_422(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3420)
    missing_id = 424242

    unknown_server = _issue(admin_client, [server_id, missing_id])
    unknown_blueprint = _issue(admin_client, [server_id], blueprint_ids=[missing_id])

    assert unknown_server.status_code == 422, unknown_server.text
    assert _code(unknown_server) == CODE_INTEGRATION_TOKEN_SERVER_NOT_FOUND
    assert unknown_blueprint.status_code == 422, unknown_blueprint.text
    assert _code(unknown_blueprint) == CODE_INTEGRATION_TOKEN_BLUEPRINT_NOT_FOUND


def test_an_edit_replaces_name_note_and_allowlists_and_keeps_the_secret(
    admin_client, integration_enabled
):
    first_server = _server(admin_client, 3421)
    second_server = _server(admin_client, 3422)
    blueprint_id = _blueprint(admin_client, "bp-it-edit")
    created = _issue(admin_client, [first_server]).json()["data"]
    secret_hmac_before = _row(created["id"]).secret_hmac

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{created['id']}",
        json={
            "name": "pipeline-renombrado",
            "note": "nota nueva",
            "server_ids": [second_server],
            "blueprint_ids": [blueprint_id],
        },
    )

    assert edited.status_code == 200, edited.text
    data = edited.json()["data"]
    assert data["name"] == "pipeline-renombrado"
    assert data["note"] == "nota nueva"
    assert data["server_ids"] == [second_server]
    assert data["blueprint_ids"] == [blueprint_id]
    assert "token" not in data
    assert _row(created["id"]).secret_hmac == secret_hmac_before
    assert _stored_server_ids(created["id"]) == [second_server]
    assert _stored_blueprint_ids(created["id"]) == [blueprint_id]


def test_an_edit_cannot_carry_unknown_fields(admin_client, integration_enabled):
    server_id = _server(admin_client, 3423)
    token_pk = _issue(admin_client, [server_id]).json()["data"]["id"]

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"expires_in_days": 1, "secret": "x"}
    )

    assert edited.status_code == 422, edited.text


# --------------------------------------------------------------------------- #
# Revocation                                                                   #
# --------------------------------------------------------------------------- #


def test_revoking_twice_is_a_409_and_a_revoked_token_cannot_be_edited(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3424)
    token_pk = _issue(admin_client, [server_id]).json()["data"]["id"]

    first_revoke = admin_client.delete(f"{TOKENS_PATH}/{token_pk}")
    second_revoke = admin_client.delete(f"{TOKENS_PATH}/{token_pk}")
    edit_after_revoke = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"name": "zombie"})

    assert first_revoke.status_code == 200, first_revoke.text
    assert second_revoke.status_code == 409, second_revoke.text
    assert _code(second_revoke) == CODE_INTEGRATION_TOKEN_ALREADY_REVOKED
    assert edit_after_revoke.status_code == 409, edit_after_revoke.text
    assert _code(edit_after_revoke) == CODE_INTEGRATION_TOKEN_ALREADY_REVOKED


# --------------------------------------------------------------------------- #
# Kill switch                                                                  #
# --------------------------------------------------------------------------- #


def test_with_the_kill_switch_off_create_and_edit_are_503_but_list_and_revoke_still_work(
    admin_client, monkeypatch
):
    import app.core.integration_auth as integration_auth

    server_id = _server(admin_client, 3425)
    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", True)
    token_pk = _issue(admin_client, [server_id]).json()["data"]["id"]
    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", False)

    created = _issue(admin_client, [server_id])
    edited = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"name": "nuevo-nombre"})
    listed = admin_client.get(TOKENS_PATH)
    revoked = admin_client.delete(f"{TOKENS_PATH}/{token_pk}")

    for response in (created, edited):
        assert response.status_code == 503, response.text
        assert _code(response) == CODE_INTEGRATION_DISABLED
    assert listed.status_code == 200, listed.text
    assert revoked.status_code == 200, revoked.text
    assert _row(token_pk).name == "ci-pipeline"


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #


def test_create_edit_and_revoke_are_audited_with_the_human_actor(admin_client, integration_enabled):
    server_id = _server(admin_client, 3426)
    created = _issue(admin_client, [server_id]).json()["data"]
    token_pk = created["id"]
    admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"name": "renombrado"})
    admin_client.delete(f"{TOKENS_PATH}/{token_pk}")

    for action in (
        "integration_token.create",
        "integration_token.update",
        "integration_token.revoke",
    ):
        rows = [row for row in _audit_rows(action) if row.target_id == token_pk]
        assert len(rows) == 1, action
        audit_row = rows[0]
        assert audit_row.admin_username == "admin"
        assert audit_row.actor_type != "integration"
        assert audit_row.integration_token_id is None
        assert audit_row.target_type == "integration_token"
        assert created["token_id"] in (audit_row.detail or "")
        assert created["token"].split(".", 2)[2] not in (audit_row.detail or "")


def test_the_update_audit_records_the_scopes_before_and_after(admin_client, integration_enabled):
    server_id = _server(admin_client, 3427)
    token_pk = _issue(admin_client, [server_id], expires_in_days=WRITE_MAX_TTL_DAYS).json()["data"][
        "id"
    ]

    admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE, WRITE_SCOPE]})

    detail = [row for row in _audit_rows("integration_token.update") if row.target_id == token_pk][
        0
    ].detail
    assert READ_SCOPE in detail
    assert WRITE_SCOPE in detail
    assert json.dumps(detail)  # a plain string, never a serialized secret


def test_a_forbidden_creation_leaves_no_token_behind(admin_client, integration_enabled):
    server_id = _server(admin_client, 3428)
    viewer, _ = _person(admin_client, "viewer-it-nothing", "viewer")

    refused = _issue(viewer, [server_id], scopes=[WRITE_SCOPE])

    assert refused.status_code == 403, refused.text
    assert CODE_FORBIDDEN != _code(refused)  # a domain code, not the generic one
    session = Database().get_declarative_base_session()
    try:
        assert session.query(IntegrationToken).count() == 0
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Destructive tier (CR-1, D17): rollback and stamp                             #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def step_up_spy(monkeypatch):
    """
    Records every ``assert_step_up`` the controller performs by itself.

    The route guard already asks for the ``access.admin`` step-up on every non-safe method, and both
    share ONE freshness window, so an expired window cannot tell the guard and the controller
    apart. The explicit D17 check is therefore pinned by observing the call.
    """
    import app.controllers.integration_token_controller as token_controller

    recorded_calls: list[dict[str, Any]] = []
    real_assert_step_up = token_controller.assert_step_up

    def _spy(actor, capability, *, method=None):
        recorded_calls.append({"capability": capability, "method": method})
        return real_assert_step_up(actor, capability, method=method)

    monkeypatch.setattr(token_controller, "assert_step_up", _spy)
    return recorded_calls


def _destructive_issue(admin_client, server_id: int, blueprint_id: int, **overrides: Any):
    payload: dict[str, Any] = {
        "scopes": [ROLLBACK_SCOPE],
        "blueprint_ids": [blueprint_id],
    }
    payload.update(overrides)
    return _issue(admin_client, [server_id], **payload)


def test_a_destructive_token_defaults_to_and_is_capped_at_seven_days(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3501)
    blueprint_id = _blueprint(admin_client, "destructive-ttl")

    default_ttl = _destructive_issue(admin_client, server_id, blueprint_id)
    over_the_cap = _destructive_issue(
        admin_client, server_id, blueprint_id, expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS + 1
    )
    at_the_cap = _destructive_issue(
        admin_client,
        server_id,
        blueprint_id,
        scopes=[STAMP_SCOPE],
        expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS,
    )

    assert default_ttl.status_code == 201, default_ttl.text
    remaining_days = _remaining_days(default_ttl.json()["data"]["id"])
    assert DESTRUCTIVE_MAX_TTL_DAYS - 1 < remaining_days <= DESTRUCTIVE_MAX_TTL_DAYS
    assert over_the_cap.status_code == 422, over_the_cap.text
    assert _code(over_the_cap) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    assert over_the_cap.json()["detail"]["public_context"]["max_days"] == DESTRUCTIVE_MAX_TTL_DAYS
    assert at_the_cap.status_code == 201, at_the_cap.text


@pytest.mark.parametrize("destructive_scope", [ROLLBACK_SCOPE, STAMP_SCOPE])
def test_a_destructive_scope_without_a_blueprint_allowlist_is_a_422_and_creates_nothing(
    admin_client, integration_enabled, destructive_scope
):
    server_id = _server(admin_client, 3502)

    created = _issue(admin_client, [server_id], scopes=[destructive_scope])

    assert created.status_code == 422, created.text
    assert _code(created) == CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED
    session = Database().get_declarative_base_session()
    try:
        assert session.query(IntegrationToken).count() == 0
    finally:
        session.close()


def test_a_write_only_token_still_needs_no_blueprint_allowlist(admin_client, integration_enabled):
    server_id = _server(admin_client, 3503)

    created = _issue(admin_client, [server_id], scopes=[APPLY_SCOPE])

    assert created.status_code == 201, created.text


def test_issuing_a_destructive_token_asks_the_issuer_for_a_fresh_step_up_on_blueprints_apply(
    admin_client, integration_enabled, step_up_spy
):
    from app.services.capability_catalog import Capability

    server_id = _server(admin_client, 3504)
    blueprint_id = _blueprint(admin_client, "destructive-stepup-create")

    created = _destructive_issue(admin_client, server_id, blueprint_id)

    assert created.status_code == 201, created.text
    assert {"capability": Capability.BLUEPRINTS_APPLY, "method": "POST"} in step_up_spy


def test_an_expired_step_up_blocks_issuing_a_destructive_token(
    admin_client, integration_enabled, expire_step_up
):
    server_id = _server(admin_client, 3505)
    blueprint_id = _blueprint(admin_client, "destructive-stepup-expired")
    expire_step_up(admin_client)

    created = _destructive_issue(admin_client, server_id, blueprint_id)

    assert created.status_code == 403, created.text
    assert _code(created) == CODE_STEP_UP_REQUIRED


def test_adding_a_destructive_scope_asks_for_step_up_even_when_apply_forward_is_already_held(
    admin_client, integration_enabled, step_up_spy
):
    from app.services.capability_catalog import Capability

    server_id = _server(admin_client, 3506)
    blueprint_id = _blueprint(admin_client, "destructive-stepup-edit")
    token_pk = _issue(
        admin_client,
        [server_id],
        scopes=[APPLY_SCOPE],
        blueprint_ids=[blueprint_id],
        expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS,
    ).json()["data"]["id"]
    step_up_spy.clear()

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [APPLY_SCOPE, ROLLBACK_SCOPE]}
    )

    assert edited.status_code == 200, edited.text
    assert {"capability": Capability.BLUEPRINTS_APPLY, "method": "POST"} in step_up_spy


def test_an_edit_that_adds_no_destructive_scope_does_not_repeat_the_explicit_step_up(
    admin_client, integration_enabled, step_up_spy
):
    server_id = _server(admin_client, 3507)
    blueprint_id = _blueprint(admin_client, "destructive-stepup-not-added")
    token_pk = _destructive_issue(admin_client, server_id, blueprint_id).json()["data"]["id"]
    step_up_spy.clear()

    renamed = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"name": "renombrado"})
    same_scopes = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"scopes": [ROLLBACK_SCOPE]})

    assert renamed.status_code == 200, renamed.text
    assert same_scopes.status_code == 200, same_scopes.text
    assert step_up_spy == []


def test_adding_a_destructive_scope_with_an_expired_step_up_changes_nothing(
    admin_client, integration_enabled, expire_step_up
):
    server_id = _server(admin_client, 3508)
    blueprint_id = _blueprint(admin_client, "destructive-stepup-edit-expired")
    token_pk = _issue(
        admin_client,
        [server_id],
        scopes=[APPLY_SCOPE],
        blueprint_ids=[blueprint_id],
        expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS,
    ).json()["data"]["id"]
    expire_step_up(admin_client)

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [APPLY_SCOPE, STAMP_SCOPE]}
    )

    assert edited.status_code == 403, edited.text
    assert _code(edited) == CODE_STEP_UP_REQUIRED
    assert _row(token_pk).scopes == APPLY_SCOPE


def test_an_edit_adding_a_destructive_scope_to_a_token_without_blueprint_allowlist_is_a_422(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3509)
    token_pk = _issue(
        admin_client, [server_id], scopes=[APPLY_SCOPE], expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS
    ).json()["data"]["id"]

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [APPLY_SCOPE, ROLLBACK_SCOPE]}
    )

    assert edited.status_code == 422, edited.text
    assert _code(edited) == CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED
    assert _row(token_pk).scopes == APPLY_SCOPE


def test_an_edit_adding_a_destructive_scope_together_with_an_allowlist_is_accepted(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3510)
    blueprint_id = _blueprint(admin_client, "destructive-edit-with-allowlist")
    token_pk = _issue(
        admin_client, [server_id], scopes=[APPLY_SCOPE], expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS
    ).json()["data"]["id"]

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}",
        json={"scopes": [APPLY_SCOPE, ROLLBACK_SCOPE], "blueprint_ids": [blueprint_id]},
    )

    assert edited.status_code == 200, edited.text
    assert _stored_blueprint_ids(token_pk) == [blueprint_id]


def test_clearing_the_blueprint_allowlist_of_a_destructive_token_is_a_422_and_changes_nothing(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3511)
    blueprint_id = _blueprint(admin_client, "destructive-clear-allowlist")
    token_pk = _destructive_issue(admin_client, server_id, blueprint_id).json()["data"]["id"]

    cleared = admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"blueprint_ids": []})

    assert cleared.status_code == 422, cleared.text
    assert _code(cleared) == CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED
    assert _stored_blueprint_ids(token_pk) == [blueprint_id]


def test_clearing_the_allowlist_while_dropping_the_destructive_scopes_is_accepted(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3512)
    blueprint_id = _blueprint(admin_client, "destructive-drop-then-clear")
    token_pk = _destructive_issue(admin_client, server_id, blueprint_id).json()["data"]["id"]

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [READ_SCOPE], "blueprint_ids": []}
    )

    assert edited.status_code == 200, edited.text
    assert _stored_blueprint_ids(token_pk) == []


def test_adding_a_destructive_scope_to_a_token_that_outlives_seven_days_is_refused_untouched(
    admin_client, integration_enabled
):
    server_id = _server(admin_client, 3513)
    blueprint_id = _blueprint(admin_client, "destructive-d9")
    token_pk = _issue(
        admin_client,
        [server_id],
        scopes=[APPLY_SCOPE],
        blueprint_ids=[blueprint_id],
        expires_in_days=WRITE_MAX_TTL_DAYS,
    ).json()["data"]["id"]
    expiry_before = _row(token_pk).expires_at

    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [APPLY_SCOPE, ROLLBACK_SCOPE]}
    )

    assert edited.status_code == 422, edited.text
    assert _code(edited) == CODE_INTEGRATION_TOKEN_TTL_TOO_LONG
    assert edited.json()["detail"]["public_context"]["max_days"] == DESTRUCTIVE_MAX_TTL_DAYS
    row_after = _row(token_pk)
    assert row_after.expires_at == expiry_before
    assert row_after.scopes == APPLY_SCOPE


def test_an_operator_cannot_hold_destructive_scopes(admin_client, integration_enabled):
    server_id = _server(admin_client, 3514)
    blueprint_id = _blueprint(admin_client, "destructive-operator")
    operator, _ = _person(admin_client, "operator-it-destructive", "operator")

    created = _issue(operator, [server_id], scopes=[ROLLBACK_SCOPE], blueprint_ids=[blueprint_id])

    assert created.status_code == 403, created.text
    assert _code(created) == CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED


def test_the_ceiling_carries_the_tier_of_each_scope_and_the_destructive_ttl(
    admin_client, integration_enabled
):
    ceiling = admin_client.get(CEILING_PATH)

    assert ceiling.status_code == 200, ceiling.text
    data = ceiling.json()["data"]
    tier_by_scope = {entry["scope"]: entry["tier"] for entry in data["scopes"]}
    assert tier_by_scope[ROLLBACK_SCOPE] == "destructive"
    assert tier_by_scope[STAMP_SCOPE] == "destructive"
    assert tier_by_scope[APPLY_SCOPE] == "write"
    assert tier_by_scope[READ_SCOPE] == "read"
    assert len(tier_by_scope) == 12
    assert data["max_destructive_ttl_days"] == DESTRUCTIVE_MAX_TTL_DAYS


def test_the_update_audit_names_the_added_destructive_scopes(admin_client, integration_enabled):
    server_id = _server(admin_client, 3515)
    blueprint_id = _blueprint(admin_client, "destructive-audit")
    token_pk = _issue(
        admin_client,
        [server_id],
        scopes=[APPLY_SCOPE],
        blueprint_ids=[blueprint_id],
        expires_in_days=DESTRUCTIVE_MAX_TTL_DAYS,
    ).json()["data"]["id"]

    admin_client.patch(f"{TOKENS_PATH}/{token_pk}", json={"scopes": [APPLY_SCOPE, STAMP_SCOPE]})

    update_detail = _audit_rows("integration_token.update")[-1].detail or ""
    assert "added_destructive=[" + STAMP_SCOPE + "]" in update_detail


def test_the_create_audit_names_the_destructive_scopes(admin_client, integration_enabled):
    server_id = _server(admin_client, 3516)
    blueprint_id = _blueprint(admin_client, "destructive-audit-create")

    _destructive_issue(admin_client, server_id, blueprint_id, scopes=[ROLLBACK_SCOPE, STAMP_SCOPE])

    create_detail = _audit_rows("integration_token.create")[-1].detail or ""
    assert ROLLBACK_SCOPE in create_detail
    assert STAMP_SCOPE in create_detail


def test_with_the_kill_switch_off_issuing_or_editing_a_destructive_token_is_503(
    admin_client, monkeypatch
):
    import app.core.integration_auth as integration_auth

    server_id = _server(admin_client, 3517)
    blueprint_id = _blueprint(admin_client, "destructive-switch")
    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", True)
    token_pk = _destructive_issue(admin_client, server_id, blueprint_id).json()["data"]["id"]
    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", False)

    created = _destructive_issue(admin_client, server_id, blueprint_id)
    edited = admin_client.patch(
        f"{TOKENS_PATH}/{token_pk}", json={"scopes": [ROLLBACK_SCOPE, STAMP_SCOPE]}
    )

    for response in (created, edited):
        assert response.status_code == 503, response.text
        assert _code(response) == CODE_INTEGRATION_DISABLED
