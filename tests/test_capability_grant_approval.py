"""
Capacidades puntuales: aprobación, rechazo, bandeja, vencimiento y cancelación (B4).

Se mide lo que decide el negocio: aprueba OTRO access_admin (ni el solicitante ni el destinatario),
solo cuenta el techo de quien aprueba, una pendiente vence a los 7 días, aprobar dos veces o
fuera de término da ``access.grant_not_pending`` y el ganador es uno solo (compare-and-set).
"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.controllers.capability_grant_controller import CapabilityGrantController
from app.core.database import Database
from app.models.capability_grant_model import utcnow
from app.models.user_model import UserModel
from tests.scope_helpers import env_id
from tests.test_api_gateway_users import _code, _crear
from tests.test_capability_grant_crud import (
    DEV,
    _admin_como,
    _audits,
    _grant,
    _row,
    _uid,
)

SENSITIVE = "exports.download"


def _approve(client, gid, **body):
    return client.post(f"/api/v1/capability-grants/{gid}/approve", json=body or None)


def _reject(client, gid, **body):
    return client.post(f"/api/v1/capability-grants/{gid}/reject", json=body or None)



def _full(gid) -> dict:
    with Database().engine.begin() as conn:
        r = conn.execute(
            text("SELECT * FROM capability_grants WHERE id = :i"), {"i": gid}
        ).mappings().fetchone()
    return dict(r)


def _set_expiry(gid, delta):
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE capability_grants SET expires_at = :e WHERE id = :i"),
            {"e": utcnow() + delta, "i": gid},
        )


@pytest.fixture()
def target(admin_client):
    return _crear(admin_client, "destino")["id"]


@pytest.fixture()
def second(admin_client):
    """Un segundo access_admin con rol operativo suficiente (owner)."""
    return _admin_como(admin_client, "aprobador", role="owner")


@pytest.fixture()
def pending(admin_client, target):
    r = _grant(admin_client, target, SENSITIVE, scope_id=env_id(DEV))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# Aprobar                                                                     #
# --------------------------------------------------------------------------- #


def test_a_second_access_admin_approves_and_the_grant_takes_effect(
    admin_client, second, target, pending
):
    _, client = second
    assert UserModel().find_access_context(target)["capability_grants"] == []

    r = _approve(client, pending, reason="ok por ticket 42")
    assert r.status_code == 200, r.text
    g = r.json()["data"]
    assert g["status"] == "active"
    assert g["decided_by"]["username"] == "aprobador"
    assert g["decision_reason"] == "ok por ticket 42"
    # Al aprobar se borra el vencimiento: si no, el lector la descartaría a los 7 días.
    assert g["expires_at"] is None and _row(pending)["live_key"] == 1

    cgs = UserModel().find_access_context(target)["capability_grants"]
    assert [c["capability"] for c in cgs] == [SENSITIVE]

    (a,) = _audits("capability_grant.approved")
    assert a.status == "success" and a.admin_username == "aprobador"
    d = json.loads(a.detail)
    assert d["before"] == {"status": "pending"} and d["after"] == {"status": "active"}


def test_the_requester_cannot_approve_their_own_request(admin_client, pending):
    r = _approve(admin_client, pending)
    assert (r.status_code, _code(r)) == (409, "access.self_approval_forbidden")
    assert _full(pending)["status"] == "pending"
    (a,) = _audits("capability_grant.approved")
    assert a.status == "failure"


def test_the_grantee_cannot_approve_their_own_capability(admin_client, second):
    """El destinatario es access_admin: pide otro, aprueba él → no puede."""
    grantee_id, grantee_client = _admin_como(admin_client, "destinatario", role="owner")
    gid = _grant(admin_client, grantee_id, SENSITIVE).json()["data"]["id"]
    r = _approve(grantee_client, gid)
    assert (r.status_code, _code(r)) == (409, "access.self_modification_forbidden")
    assert _full(gid)["status"] == "pending"


def test_only_the_approvers_ceiling_is_checked(admin_client, target, pending):
    """Un access_admin viewer no tiene la capacidad en ese alcance: no puede aprobarla."""
    _, weak = _admin_como(admin_client, "debil", role="viewer")
    r = _approve(weak, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_ceiling_exceeded")
    assert _full(pending)["status"] == "pending"


def test_a_non_access_admin_gets_an_opaque_403(admin_client, pending):
    datos = _crear(admin_client, "sin-admin", gateway_role="owner")
    from tests.test_api_gateway_users import _cliente_como

    c = _cliente_como(datos, "sin-admin")
    for call in (_approve(c, pending), _reject(c, pending),
                 c.get("/api/v1/capability-grants/pending")):
        assert call.status_code == 403
    assert _full(pending)["status"] == "pending"


def test_approving_an_unknown_grant_is_404(second):
    _, client = second
    r = _approve(client, 99999)
    assert (r.status_code, _code(r)) == (404, "access.grant_not_found")


def test_approving_twice_is_grant_not_pending(admin_client, second, pending):
    _, client = second
    assert _approve(client, pending).status_code == 200
    r = _approve(client, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")


def test_approving_an_expired_request_is_grant_not_pending_and_audits_expiry(
    admin_client, second, pending
):
    _, client = second
    _set_expiry(pending, timedelta(minutes=-1))
    r = _approve(client, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")
    f = _full(pending)
    assert f["status"] == "expired" and f["live_key"] is None
    (a,) = _audits("capability_grant.expired")
    assert a.actor_type == "system" and a.admin_username is None
    assert json.loads(a.detail)["after"] == {"status": "expired"}


def test_approving_an_inactive_grantee_is_rejected(admin_client, second, target, pending):
    assert admin_client.patch(f"/api/v1/gateway-users/{target}", json={"is_active": False}
                              ).status_code == 200
    _, client = second
    r = _approve(client, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_user_inactive")


def test_approve_race_only_one_wins(admin_client, second, pending, monkeypatch):
    """
    Un rival decide ENTRE el chequeo de reglas y el UPDATE: el compare-and-set hace perder a
    quien llega segundo, que recibe ``grant_not_pending`` y no pisa la decisión del primero.
    """
    rival_id = _uid("admin")
    original = CapabilityGrantController._block_reason

    def racing(self, *a, **kw):
        out = original(self, *a, **kw)
        assert self.grants.decide_pending(pending, approve=True, decided_by=rival_id, reason="rival")
        return out

    monkeypatch.setattr(CapabilityGrantController, "_block_reason", racing)
    _, client = second
    r = _approve(client, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")
    f = _full(pending)
    assert f["status"] == "active" and f["decided_by"] == rival_id
    assert f["decision_reason"] == "rival"


def test_decide_pending_is_a_compare_and_set_and_respects_expiry(admin_client, pending):
    m = CapabilityGrantController().grants
    _set_expiry(pending, timedelta(seconds=-1))
    assert m.decide_pending(pending, approve=True, decided_by=None, reason=None) is False
    _set_expiry(pending, timedelta(days=1))
    assert m.decide_pending(pending, approve=True, decided_by=None, reason=None) is True
    assert m.decide_pending(pending, approve=False, decided_by=None, reason=None) is False


# --------------------------------------------------------------------------- #
# Rechazar                                                                    #
# --------------------------------------------------------------------------- #


def test_reject_closes_the_request_and_frees_live_key(admin_client, second, target, pending):
    _, client = second
    r = _reject(client, pending, reason="no corresponde")
    assert r.status_code == 200, r.text
    g = r.json()["data"]
    assert (g["status"], g["decision_reason"]) == ("rejected", "no corresponde")
    assert _row(pending)["live_key"] is None
    assert UserModel().find_access_context(target)["capability_grants"] == []
    (a,) = _audits("capability_grant.rejected")
    assert json.loads(a.detail)["reason"] == "no corresponde"
    # Libera el UNIQUE: se puede volver a pedir.
    assert _grant(admin_client, target, SENSITIVE).status_code == 201
    # Y ya no se puede decidir de nuevo.
    r = _reject(client, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")
    r = _approve(client, pending)
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")


def test_reject_works_without_a_body(admin_client, second, pending):
    _, client = second
    assert client.post(f"/api/v1/capability-grants/{pending}/reject").status_code == 200


# --------------------------------------------------------------------------- #
# Bandeja                                                                     #
# --------------------------------------------------------------------------- #


def test_pending_inbox_flags_can_decide_per_actor(admin_client, second, target, pending):
    _, client = second
    # Para el solicitante: bloqueada por auto-aprobación.
    (mine,) = admin_client.get("/api/v1/capability-grants/pending").json()["data"]
    assert mine["id"] == pending
    assert mine["can_decide"] is False
    assert mine["blocked_reason"] == "access.self_approval_forbidden"
    assert mine["username"] == "destino" and mine["sensitive"] is True
    # Para el segundo admin: puede decidir.
    (theirs,) = client.get("/api/v1/capability-grants/pending").json()["data"]
    assert theirs["can_decide"] is True and theirs["blocked_reason"] is None


def test_pending_inbox_blocks_a_weak_approver_by_ceiling(admin_client, pending):
    _, weak = _admin_como(admin_client, "debil", role="viewer")
    (row,) = weak.get("/api/v1/capability-grants/pending").json()["data"]
    assert (row["can_decide"], row["blocked_reason"]) == (False, "access.grant_ceiling_exceeded")


def test_pending_inbox_sweeps_expired_and_hides_decided(admin_client, second, target, pending):
    other = _grant(admin_client, target, "databases.drop").json()["data"]["id"]
    _, client = second
    _set_expiry(pending, timedelta(minutes=-5))
    _approve(client, other)
    assert client.get("/api/v1/capability-grants/pending").json()["data"] == []
    assert _full(pending)["status"] == "expired"


def test_user_grant_list_sweeps_expired_rows(admin_client, target, pending):
    _set_expiry(pending, timedelta(minutes=-1))
    rows = admin_client.get(f"/api/v1/gateway-users/{target}/capability-grants").json()["data"]
    assert [r["status"] for r in rows] == ["expired"]


# --------------------------------------------------------------------------- #
# Vencimiento                                                                 #
# --------------------------------------------------------------------------- #


def test_expire_overdue_only_touches_overdue_pending(admin_client, target, pending):
    plain = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    fresh = _grant(admin_client, target, "databases.drop").json()["data"]["id"]
    _set_expiry(pending, timedelta(seconds=-1))
    c = CapabilityGrantController()
    assert c.expire_overdue() == 1
    assert c.expire_overdue() == 0
    assert _full(pending)["status"] == "expired"
    assert _full(plain)["status"] == "active" and _full(fresh)["status"] == "pending"


def test_pending_expires_after_seven_days(admin_client, pending):
    f = CapabilityGrantController().grants.get(pending)
    delta = f["expires_at"] - f["requested_at"]
    assert timedelta(days=7) - timedelta(seconds=5) <= delta <= timedelta(days=7, seconds=5)


def test_expire_overdue_failure_never_blocks_startup(monkeypatch):
    """El barrido del arranque va en try/except: una tabla sin migrar no impide arrancar."""
    import asyncio

    import main

    def boom(self):
        raise RuntimeError("no such table")

    monkeypatch.setattr(CapabilityGrantController, "expire_overdue", boom)

    async def run():
        async with main.lifespan(main.app):
            pass

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Cancelación                                                                 #
# --------------------------------------------------------------------------- #


def test_losing_access_admin_cancels_the_requesters_pending_requests(admin_client, target):
    req_id, req_client = _admin_como(admin_client, "solicitante", role="owner")
    gid = _grant(req_client, target, SENSITIVE).json()["data"]["id"]
    active = _grant(req_client, target, "databases.write").json()["data"]["id"]
    r = admin_client.put(
        f"/api/v1/gateway-users/{req_id}/access",
        json={"global_capabilities": [], "scope_grants": []},
    )
    assert r.status_code == 200, r.text
    f = _full(gid)
    assert f["status"] == "cancelled" and f["live_key"] is None
    assert _full(active)["status"] == "active"  # lo activo no depende del solicitante
    (a,) = _audits("capability_grant.cancelled")
    assert a.actor_type == "system" and json.loads(a.detail)["reason"] == "requester_lost_access"


def test_deactivating_the_requester_cancels_their_pending_requests(admin_client, target):
    req_id, req_client = _admin_como(admin_client, "solicitante", role="owner")
    gid = _grant(req_client, target, SENSITIVE).json()["data"]["id"]
    assert admin_client.patch(f"/api/v1/gateway-users/{req_id}", json={"is_active": False}
                              ).status_code == 200
    assert _full(gid)["status"] == "cancelled"


def test_approval_cancels_when_the_requester_drifted_without_the_hooks(
    admin_client, second, target
):
    """Respaldo de D7: si el solicitante perdió el rol por fuera de los hooks, no se aprueba."""
    req_id, req_client = _admin_como(admin_client, "solicitante", role="owner")
    gid = _grant(req_client, target, SENSITIVE).json()["data"]["id"]
    with Database().engine.begin() as conn:
        conn.execute(text("DELETE FROM user_global_capabilities WHERE user_id = :u"),
                     {"u": req_id})
    _, client = second
    r = _approve(client, gid)
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")
    assert _full(gid)["status"] == "cancelled"
    assert _audits("capability_grant.cancelled")[-1].actor_type == "system"


def test_inactive_user_keeps_active_grants_when_deactivated(admin_client, target):
    gid = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    admin_client.patch(f"/api/v1/gateway-users/{target}", json={"is_active": False})
    assert _full(gid)["status"] == "active"
