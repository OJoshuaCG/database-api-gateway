"""
Decisión masiva de capacidades puntuales (``POST /capability-grants/decisions``).

Lo que se mide: MEJOR ESFUERZO (un ítem bloqueado no frena ni revierte a los demás), las reglas de
segundo aprobador son las del endpoint individual (``_block_reason``, no una copia), el orden y los
códigos por ítem, la auditoría por ítem con ``bulk_id`` común más una fila agregada, y que un error
inesperado nunca filtre el texto de la excepción.
"""

import json

import pytest
from sqlalchemy import text

from app.controllers.capability_grant_controller import CapabilityGrantController
from app.core.database import Database
from tests.scope_helpers import env_id
from tests.test_api_gateway_users import _cliente_como, _code, _crear
from tests.test_capability_grant_crud import DEV, _admin_como, _audits, _grant, _row

# Sensibles (nacen pending): sirven para armar varias solicitudes sobre el mismo destino.
SENSITIVE_CAPABILITIES = [
    "exports.download",
    "databases.drop",
    "clones.execute",
    "sql_console.execute",
]
URL = "/api/v1/capability-grants/decisions"


def _decide(client, decision, ids, **extra):
    return client.post(URL, json={"decision": decision, "ids": ids, **extra})


def _status(grant_id: int) -> str:
    with Database().engine.begin() as conn:
        return conn.execute(
            text("SELECT status FROM capability_grants WHERE id = :i"), {"i": grant_id}
        ).scalar()


def _request_pending(client, user_id: int, capability: str) -> int:
    response = _grant(client, user_id, capability, scope_id=env_id(DEV))
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "pending"
    return response.json()["data"]["id"]


@pytest.fixture()
def target(admin_client):
    return _crear(admin_client, "destino")["id"]


@pytest.fixture()
def second(admin_client):
    """Un segundo access_admin que decide lo que pidió ``admin``."""
    return _admin_como(admin_client, "aprobador", role="owner")


@pytest.fixture()
def pendings(admin_client, target):
    return [_request_pending(admin_client, target, c) for c in SENSITIVE_CAPABILITIES]


def _results_by_id(response) -> dict:
    return {item["id"]: item for item in response.json()["data"]["results"]}


# --------------------------------------------------------------------------- #
# Aprobar                                                                     #
# --------------------------------------------------------------------------- #


def test_bulk_approve_activates_every_request_in_the_requested_order(second, pendings):
    _, approver = second
    ordered = list(reversed(pendings))

    r = _decide(approver, "approve", ordered, reason="lote del viernes")

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["requested"], data["succeeded"], data["failed"]) == (4, 4, 0)
    assert [item["id"] for item in data["results"]] == ordered
    for item in data["results"]:
        assert item["ok"] is True
        assert item["grant"]["status"] == "active"
        assert item["grant"]["decision_reason"] == "lote del viernes"
        assert item["grant"]["decided_by"]["username"] == "aprobador"
        assert item.get("code") is None and item.get("message") is None
    assert {_status(i) for i in pendings} == {"active"}


def test_bulk_approve_audits_each_item_with_a_shared_bulk_id_plus_one_aggregate(
    second, pendings
):
    _, approver = second

    _decide(approver, "approve", pendings)

    per_item = _audits("capability_grant.approved")
    assert len(per_item) == len(pendings)
    bulk_ids = {json.loads(a.detail)["bulk_id"] for a in per_item}
    assert len(bulk_ids) == 1

    (aggregate,) = _audits("capability_grant.bulk_decided")
    detail = json.loads(aggregate.detail)
    assert detail == {
        "bulk_id": bulk_ids.pop(),
        "decision": "approve",
        "requested": 4,
        "succeeded": 4,
        "failed": 0,
    }
    assert aggregate.status == "success" and aggregate.admin_username == "aprobador"


def test_single_endpoint_audit_rows_carry_no_bulk_id(second, pendings):
    _, approver = second

    r = approver.post(f"/api/v1/capability-grants/{pendings[0]}/approve")

    assert r.status_code == 200, r.text
    (row,) = _audits("capability_grant.approved")
    assert "bulk_id" not in json.loads(row.detail)
    assert _audits("capability_grant.bulk_decided") == []


def test_mixed_batch_reports_each_block_with_its_code_and_decides_the_rest(
    admin_client, second, target, pendings
):
    approver_id, approver = second
    # Pedida por el propio aprobador: auto-aprobación.
    own_request = _grant(approver, target, "engine_users.drop", scope_id=env_id(DEV))
    assert own_request.status_code == 201, own_request.text
    self_requested = own_request.json()["data"]["id"]
    # Para el aprobador como destinatario: auto-modificación.
    about_approver = _request_pending(admin_client, approver_id, "engine_users.secrets")
    # Ya decidida: no pendiente.
    already_decided = pendings[3]
    assert approver.post(f"/api/v1/capability-grants/{already_decided}/approve").status_code == 200
    # Destinatario desactivado.
    inactive_user = _crear(admin_client, "inactivo")["id"]
    for_inactive = _request_pending(admin_client, inactive_user, "exports.download")
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE id = :i"), {"i": inactive_user})

    ids = [
        pendings[0],
        self_requested,
        about_approver,
        already_decided,
        for_inactive,
        999999,
        pendings[1],
    ]
    r = _decide(approver, "approve", ids)

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["requested"], data["succeeded"], data["failed"]) == (7, 2, 5)
    assert [item["id"] for item in data["results"]] == ids
    by_id = _results_by_id(r)
    assert by_id[pendings[0]]["ok"] and by_id[pendings[1]]["ok"]
    assert by_id[self_requested]["code"] == "access.self_approval_forbidden"
    assert by_id[about_approver]["code"] == "access.self_modification_forbidden"
    assert by_id[already_decided]["code"] == "access.grant_not_pending"
    assert by_id[for_inactive]["code"] == "access.grant_user_inactive"
    assert by_id[999999]["code"] == "access.grant_not_found"
    for blocked in (self_requested, about_approver, already_decided, for_inactive, 999999):
        assert by_id[blocked]["ok"] is False
        assert by_id[blocked]["grant"] is None
        assert by_id[blocked]["message"]
    # Los bloqueados no cambiaron y los demás sí.
    assert _status(self_requested) == "pending"
    assert _status(about_approver) == "pending"
    assert _status(pendings[0]) == "active" and _status(pendings[1]) == "active"


def test_blocked_items_keep_their_failure_audit_row_with_the_bulk_id(
    admin_client, second, target
):
    _, approver = second
    own = _grant(approver, target, "engine_users.drop", scope_id=env_id(DEV)).json()["data"]["id"]

    _decide(approver, "approve", [own])

    failures = [a for a in _audits("capability_grant.approved") if a.status == "failure"]
    assert len(failures) == 1
    detail = json.loads(failures[0].detail)
    assert detail["reason"] == "access.self_approval_forbidden"
    assert detail["bulk_id"]
    (aggregate,) = _audits("capability_grant.bulk_decided")
    assert json.loads(aggregate.detail)["failed"] == 1


def test_a_grantee_that_became_conflicting_is_reported_as_sod_conflict(
    admin_client, second, target, pendings
):
    """``_block_reason`` re-chequea la separación de deberes: el lote lo respeta ítem por ítem."""
    _, approver = second
    # El destino pasa a ser ``security_officer`` DESPUÉS de pedir una exclusiva de owner.
    with Database().engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_global_capabilities (user_id, capability, created_at, "
                "updated_at) VALUES (:u, 'security_officer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": target},
        )

    r = _decide(approver, "approve", [pendings[0]])

    assert r.status_code == 200, r.text
    item = r.json()["data"]["results"][0]
    assert item["ok"] is False
    assert item["code"] == "access.sod_conflict"
    assert _status(pendings[0]) == "pending"


# --------------------------------------------------------------------------- #
# Rechazar, duplicados e idempotencia                                         #
# --------------------------------------------------------------------------- #


def test_bulk_reject_rejects_every_request_and_audits_them(second, pendings):
    _, approver = second

    r = _decide(approver, "reject", pendings, reason="fuera de alcance")

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["succeeded"], data["failed"]) == (4, 0)
    assert {item["grant"]["status"] for item in data["results"]} == {"rejected"}
    assert {item["grant"]["decision_reason"] for item in data["results"]} == {"fuera de alcance"}
    assert {_status(i) for i in pendings} == {"rejected"}
    rejected_rows = _audits("capability_grant.rejected")
    assert len(rejected_rows) == 4
    assert len({json.loads(a.detail)["bulk_id"] for a in rejected_rows}) == 1
    (aggregate,) = _audits("capability_grant.bulk_decided")
    assert json.loads(aggregate.detail)["decision"] == "reject"


def test_the_requester_can_reject_their_own_request_in_bulk(admin_client, pendings):
    """Rechazar nunca da acceso: la regla del endpoint individual no exige otra persona."""
    r = _decide(admin_client, "reject", pendings[:2])

    assert r.status_code == 200, r.text
    assert r.json()["data"]["succeeded"] == 2


def test_duplicate_ids_are_collapsed_keeping_the_first_occurrence(second, pendings):
    _, approver = second

    r = _decide(approver, "approve", [pendings[1], pendings[0], pendings[1]])

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["requested"] == 2
    assert [item["id"] for item in data["results"]] == [pendings[1], pendings[0]]


def test_repeating_a_bulk_is_idempotent_and_reports_not_pending(second, pendings):
    _, approver = second
    assert _decide(approver, "approve", pendings).json()["data"]["succeeded"] == 4

    again = _decide(approver, "approve", pendings)

    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert (data["succeeded"], data["failed"]) == (0, 4)
    assert {item["code"] for item in data["results"]} == {"access.grant_not_pending"}
    assert {_status(i) for i in pendings} == {"active"}


def test_an_unexpected_error_in_one_item_is_generic_and_does_not_stop_the_rest(
    second, pendings, monkeypatch
):
    _, approver = second
    original_approve = CapabilityGrantController.approve
    poisoned_id = pendings[1]

    def flaky_approve(self, grant_id, actor, reason=None, *, bulk_id=None):
        if grant_id == poisoned_id:
            raise RuntimeError("SELECT secret FROM host-interno.example password=hunter2")
        return original_approve(self, grant_id, actor, reason, bulk_id=bulk_id)

    monkeypatch.setattr(CapabilityGrantController, "approve", flaky_approve)

    r = _decide(approver, "approve", pendings)

    assert r.status_code == 200, r.text
    by_id = _results_by_id(r)
    poisoned = by_id[poisoned_id]
    assert poisoned["ok"] is False
    assert poisoned["code"] == "access.grant_decision_failed"
    assert "hunter2" not in r.text and "host-interno" not in r.text
    assert all(by_id[i]["ok"] for i in pendings if i != poisoned_id)
    assert _status(poisoned_id) == "pending"


# --------------------------------------------------------------------------- #
# Validación, autorización y step-up                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "approve", "ids": []},
        {"decision": "approve", "ids": list(range(1, 102))},
        {"decision": "approve", "ids": [0]},
        {"decision": "approve", "ids": [-3]},
        {"decision": "maybe", "ids": [1]},
        {"ids": [1]},
        {"decision": "approve"},
        {"decision": "approve", "ids": [1], "reason": "x" * 501},
    ],
)
def test_invalid_bodies_are_422(admin_client, body):
    assert admin_client.post(URL, json=body).status_code == 422


def test_exactly_one_hundred_ids_is_accepted(second):
    _, approver = second

    r = _decide(approver, "approve", list(range(900000, 900100)))

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["requested"], data["failed"]) == (100, 100)
    assert {item["code"] for item in data["results"]} == {"access.grant_not_found"}


def test_a_non_access_admin_gets_403_and_nothing_changes(admin_client, pendings):
    datos = _crear(admin_client, "sin-admin", gateway_role="owner")
    client = _cliente_como(datos, "sin-admin")

    r = _decide(client, "approve", pendings)

    assert r.status_code == 403
    assert {_status(i) for i in pendings} == {"pending"}
    assert _audits("capability_grant.bulk_decided") == []


def test_an_access_admin_without_a_fresh_step_up_is_blocked_before_any_effect(
    second, pendings, expire_step_up
):
    _, approver = second
    expire_step_up(approver)

    r = _decide(approver, "approve", pendings)

    assert r.status_code == 403
    assert _code(r) == "access.step_up_required"
    assert {_status(i) for i in pendings} == {"pending"}
    assert _audits("capability_grant.bulk_decided") == []


def test_row_state_is_untouched_for_blocked_items(admin_client, second, target):
    _, approver = second
    own = _grant(approver, target, "engine_users.drop", scope_id=env_id(DEV)).json()["data"]["id"]

    _decide(approver, "approve", [own])

    assert _row(own)["decided_by"] is None
