"""
Alta masiva con VARIAS capacidades (``POST /gateway-users/{id}/capability-grants/bulk``).

Lo que se mide: ``capability`` (una) sigue igual, ``capabilities`` (varias) es excluyente con ella,
el tope se cuenta en PARES, todo sigue siendo todo o nada, y el estado de nacimiento (pending/active)
se decide por capacidad con un solo ``bulk_id`` para el pedido.
"""

import json

import pytest
from sqlalchemy import text

from app.controllers import access_request_controller as access_requests
from app.core.database import Database
from tests.scope_helpers import env_id
from tests.test_api_gateway_users import _code, _crear
from tests.test_capability_grant_crud import DEV, PROD, _admin_como, _audits, _grant

PLAIN = "databases.write"
PLAIN_TWO = "blueprints.write"
SENSITIVE = "exports.download"


@pytest.fixture()
def target(admin_client):
    return _crear(admin_client, "destino")["id"]


def _bulk(client, user_id, scope_ids, **capability_fields):
    body = {"scope_type": "environment", "scope_ids": scope_ids, **capability_fields}
    return client.post(f"/api/v1/gateway-users/{user_id}/capability-grants/bulk", json=body)


def _count(user_id: int) -> int:
    with Database().engine.begin() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM capability_grants WHERE user_id = :u"), {"u": user_id}
        ).scalar()


def _failures(response) -> list[dict]:
    return response.json()["detail"]["public_context"]["failures"]


def test_mixed_capabilities_are_born_pending_or_active_per_capability(admin_client, target):
    scopes = [env_id(DEV), env_id(PROD)]

    r = _bulk(admin_client, target, scopes, capabilities=[PLAIN, SENSITIVE, PLAIN_TWO])

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert (data["count"], data["pending"]) == (6, True)
    assert [(g["capability"], g["scope_id"]) for g in data["grants"]] == [
        (PLAIN, scopes[0]), (PLAIN, scopes[1]),
        (SENSITIVE, scopes[0]), (SENSITIVE, scopes[1]),
        (PLAIN_TWO, scopes[0]), (PLAIN_TWO, scopes[1]),
    ]
    status_by_capability = {g["capability"]: g["status"] for g in data["grants"]}
    assert status_by_capability == {PLAIN: "active", SENSITIVE: "pending", PLAIN_TWO: "active"}
    pending_row = next(g for g in data["grants"] if g["capability"] == SENSITIVE)
    assert pending_row["expires_at"] is not None
    active_row = next(g for g in data["grants"] if g["capability"] == PLAIN)
    assert active_row["expires_at"] is None


def test_one_bulk_id_covers_every_audit_row_and_actions_follow_each_capability(
    admin_client, target
):
    _bulk(admin_client, target, [env_id(DEV), env_id(PROD)], capabilities=[PLAIN, SENSITIVE])

    created = _audits("capability_grant.created")
    requested = _audits("capability_grant.requested")
    assert {a.privilege for a in created} == {PLAIN} and len(created) == 2
    assert {a.privilege for a in requested} == {SENSITIVE} and len(requested) == 2
    bulk_ids = {json.loads(a.detail)["bulk_id"] for a in created + requested}
    assert len(bulk_ids) == 1


def test_only_non_sensitive_capabilities_report_pending_false(admin_client, target):
    r = _bulk(admin_client, target, [env_id(DEV)], capabilities=[PLAIN, PLAIN_TWO])

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert (data["count"], data["pending"]) == (2, False)
    assert {g["status"] for g in data["grants"]} == {"active"}


def test_without_four_eyes_a_sensitive_capability_is_active_and_audited_once(
    admin_client, target, monkeypatch
):
    monkeypatch.setattr(access_requests, "four_eyes", lambda: False)

    r = _bulk(admin_client, target, [env_id(DEV), env_id(PROD)], capabilities=[PLAIN, SENSITIVE])

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["pending"] is False
    assert {g["status"] for g in data["grants"]} == {"active"}
    assert len(_audits("access.elevation_unapproved")) == 1


def test_all_or_nothing_lists_every_failing_pair_with_its_capability(admin_client, target):
    assert _grant(admin_client, target, PLAIN, scope_id=env_id(PROD)).status_code == 201
    before = _count(target)

    r = _bulk(admin_client, target, [env_id(DEV), env_id(PROD), 999],
              capabilities=[PLAIN, SENSITIVE])

    assert (r.status_code, _code(r)) == (409, "access.grant_bulk_failed")
    assert {(f["capability"], f["scope_id"], f["code"]) for f in _failures(r)} == {
        (PLAIN, env_id(PROD), "access.grant_duplicate"),
        (PLAIN, 999, "access.grant_scope_not_found"),
        (SENSITIVE, 999, "access.grant_scope_not_found"),
    }
    for failure in _failures(r):
        assert failure["message"]
    # Nada se insertó, ni los pares válidos.
    assert _count(target) == before


def test_a_duplicate_in_one_capability_does_not_hide_the_other_capabilitys_duplicate(
    admin_client, target
):
    assert _grant(admin_client, target, SENSITIVE, scope_id=env_id(DEV)).status_code == 201
    assert _grant(admin_client, target, PLAIN, scope_id=env_id(DEV)).status_code == 201

    r = _bulk(admin_client, target, [env_id(DEV)], capabilities=[PLAIN, SENSITIVE])

    assert (r.status_code, _code(r)) == (409, "access.grant_bulk_failed")
    assert {f["capability"] for f in _failures(r)} == {PLAIN, SENSITIVE}


def test_the_legacy_single_capability_payload_is_unchanged(admin_client, target):
    ids = [env_id(DEV), env_id(PROD)]

    r = _bulk(admin_client, target, ids, capability=PLAIN, reason="rollout")

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert set(data) == {"count", "pending", "grants"}
    assert (data["count"], data["pending"]) == (2, False)
    assert [g["scope_id"] for g in data["grants"]] == ids
    assert {g["request_reason"] for g in data["grants"]} == {"rollout"}


def test_the_legacy_payload_failures_keep_their_fields_and_gain_the_capability(
    admin_client, target
):
    r = _bulk(admin_client, target, [999], capability=PLAIN)

    assert (r.status_code, _code(r)) == (409, "access.grant_bulk_failed")
    (failure,) = _failures(r)
    assert failure["scope_id"] == 999
    assert failure["code"] == "access.grant_scope_not_found"
    assert failure["message"]
    assert failure["capability"] == PLAIN


def test_a_single_element_capabilities_list_behaves_like_the_legacy_field(
    admin_client, target
):
    r = _bulk(admin_client, target, [env_id(DEV)], capabilities=[SENSITIVE])

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert (data["count"], data["pending"]) == (1, True)


def test_repeated_capabilities_are_collapsed(admin_client, target):
    r = _bulk(admin_client, target, [env_id(DEV)], capabilities=[PLAIN, PLAIN, PLAIN_TWO])

    assert r.status_code == 201, r.text
    assert r.json()["data"]["count"] == 2


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"capability": PLAIN, "capabilities": [PLAIN_TWO]},
        {"capabilities": []},
        {"capabilities": [""]},
        {"capabilities": ["x" * 65]},
        {"capability": ""},
    ],
)
def test_exactly_one_of_capability_or_capabilities_is_required(admin_client, target, fields):
    assert _bulk(admin_client, target, [env_id(DEV)], **fields).status_code == 422


def test_more_than_one_hundred_pairs_is_422_with_a_closed_code(admin_client, target):
    scope_ids = list(range(1, 52))

    r = _bulk(admin_client, target, scope_ids, capabilities=[PLAIN, PLAIN_TWO])

    assert (r.status_code, _code(r)) == (422, "access.grant_bulk_too_large")
    assert _count(target) == 0


def test_exactly_one_hundred_pairs_passes_the_cap(admin_client, target):
    """El tope es inclusivo: 100 pares llegan a la validación por par (acá, destinos inexistentes)."""
    scope_ids = list(range(900000, 900050))

    r = _bulk(admin_client, target, scope_ids, capabilities=[PLAIN, PLAIN_TWO])

    assert (r.status_code, _code(r)) == (409, "access.grant_bulk_failed")
    assert len(_failures(r)) == 100


def test_capability_level_errors_are_raised_directly(admin_client, target):
    r = _bulk(admin_client, target, [env_id(DEV)], capabilities=[PLAIN, "servers.admin"])
    assert (r.status_code, _code(r)) == (422, "access.capability_not_grantable")
    assert _count(target) == 0


def test_granting_to_yourself_is_still_forbidden(admin_client):
    from tests.test_capability_grant_crud import _uid

    r = _bulk(admin_client, _uid("admin"), [env_id(DEV)], capabilities=[PLAIN, SENSITIVE])

    assert (r.status_code, _code(r)) == (409, "access.self_modification_forbidden")


def test_multi_capability_bulk_requires_access_admin(admin_client, target):
    _, operador = _admin_como(admin_client, "operador2", extra=())

    r = _bulk(operador, target, [env_id(DEV)], capabilities=[PLAIN, SENSITIVE])

    assert r.status_code == 403
    assert _count(target) == 0
