"""
Elevaciones con segundo aprobador (C3): la política de asignación y ``/access-requests``.

Se mide lo que decide el negocio: un ``access_admin`` ``viewer`` PIDE ``owner`` o
``security_officer`` y otro lo aprueba; nadie aprueba lo que pidió ni su propia elevación; una
solicitud vieja (el acceso cambió) da 409 ``access.request_stale``; las bajas se aplican en el
acto y un payload mixto se parte; las pendientes vencen; quien pierde ``access_admin`` pierde sus
pendientes; y con ``ACCESS_FOUR_EYES=False`` un único administrador eleva solo, auditado.
"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.database import Database
from app.models.access_change_request_model import utcnow
from app.models.audit_log import AuditLog
from app.models.user_model import UserModel
from tests.access_request_helpers import client_as, create_user, settle
from tests.scope_helpers import env_id

_USERS = "/api/v1/gateway-users"
_REQ = "/api/v1/access-requests"
DEV = "development"
PROD = "production"
_RAZON = "Incidente 4711: no hay otro security_officer disponible esta semana"


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


def _uid(username: str) -> int:
    return UserModel().find_by_username(username)["id"]


def _ctx(user_id: int) -> dict:
    return UserModel().find_access_context(user_id)


def _audits(action: str) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        rows = s.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
        s.expunge_all()
        return rows
    finally:
        s.close()


def _sql(sql: str, **params) -> None:
    with Database().engine.begin() as conn:
        conn.execute(text(sql), params)


def _status(request_id: int) -> tuple:
    with Database().engine.begin() as conn:
        r = conn.execute(
            text("SELECT status, reason, decided_by FROM access_change_requests WHERE id = :i"),
            {"i": request_id},
        ).fetchone()
    return tuple(r)


def _pending(r) -> dict:
    assert r.status_code == 202, r.text
    d = r.json()["data"]
    assert d["code"] == "access.elevation_pending"
    return d["pending_request"]


@pytest.fixture()
def aa(admin_client):
    """Un access_admin ``viewer`` que PIDE (``aa-pide``) — no es el admin sembrado."""
    datos = create_user(admin_client, "aa-pide", global_capabilities=["access_admin"])
    return client_as(datos, "aa-pide")


@pytest.fixture()
def target(admin_client):
    return create_user(admin_client, "destino")["id"]


# --------------------------------------------------------------------------- #
# El camino feliz: pedir y aprobar                                             #
# --------------------------------------------------------------------------- #


def test_a_viewer_access_admin_requests_owner_and_a_second_one_approves(admin_client, aa, target):
    r = aa.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"})
    req = _pending(r)
    assert r.json()["data"]["gateway_role"] == "viewer", "la elevación no se aplica sola"
    assert req["status"] == "pending" and req["origin"] == "update"
    assert req["desired"]["gateway_role"] == "owner"
    assert req["elevations"] == [{"kind": "base_role", "role": "owner"}]
    assert req["requested_by"]["username"] == "aa-pide"
    assert UserModel().find_by_id(target)["gateway_role"] == "viewer"

    r = admin_client.post(f"{_REQ}/{req['id']}/approve", json={"reason": "ticket 42"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["status"] == "applied" and d["decided_by"]["username"] == "admin"
    assert d["reason"] == "ticket 42"
    assert UserModel().find_by_id(target)["gateway_role"] == "owner"

    # Las fixtures también aprueban (el alta de `aa-pide`): se mira la de ESTA solicitud.
    (ok,) = [a for a in _audits("access_request.approved")
             if a.status == "success" and a.target_id == req["id"]]
    assert ok.admin_username == "admin" and ok.grantee == "destino"
    efecto = json.loads(_audits("gateway_user.access_set")[-1].detail)
    assert efecto["request_id"] == req["id"] and efecto["approved_by"] == _uid("admin")
    assert (efecto["before"]["gateway_role"], efecto["after"]["gateway_role"]) == ("viewer", "owner")


def test_requesting_security_officer_and_approving_it(admin_client, aa, target):
    r = aa.put(f"{_USERS}/{target}/access",
               json={"global_capabilities": ["security_officer"], "scope_grants": []})
    req = _pending(r)
    assert _ctx(target)["globals"] == []
    assert admin_client.post(f"{_REQ}/{req['id']}/approve", json={}).status_code == 200
    assert _ctx(target)["globals"] == ["security_officer"]


def test_create_user_with_owner_is_born_viewer_with_a_pending_request(admin_client, aa):
    r = aa.post(_USERS, json={"username": "nace", "gateway_role": "owner",
                              "global_capabilities": ["access_admin"]})
    req = _pending(r)
    d = r.json()["data"]
    assert (d["gateway_role"], d["global_capabilities"]) == ("viewer", [])
    assert d["invite_token"] and d["invite_expires_at"]
    assert req["origin"] == "create"
    assert {e["kind"] for e in req["elevations"]} == {"base_role", "global_capability"}

    assert admin_client.post(f"{_REQ}/{req['id']}/approve", json={}).status_code == 200
    fila = UserModel().find_by_username("nace")
    assert fila["gateway_role"] == "owner"
    assert _ctx(fila["id"])["globals"] == ["access_admin"]


def test_approving_revokes_the_targets_sessions(admin_client, aa):
    from app.models.gateway_session import GatewaySession

    datos = create_user(admin_client, "logueada")
    client_as(datos, "logueada")
    req = _pending(aa.patch(f"{_USERS}/{datos['id']}", json={"gateway_role": "owner"}))
    assert admin_client.post(f"{_REQ}/{req['id']}/approve", json={}).status_code == 200

    s = Database().get_declarative_base_session()
    try:
        filas = s.query(GatewaySession).filter(GatewaySession.user_id == datos["id"]).all()
        assert filas and all(f.revoked_reason == "role_change" for f in filas)
    finally:
        s.close()


def test_a_pure_non_elevating_change_keeps_returning_200(admin_client, target):
    r = admin_client.put(
        f"{_USERS}/{target}/access",
        json={"global_capabilities": [],
              "scope_grants": [{"scope_type": "environment", "scope_id": env_id(DEV),
                                "role": "operator"}]},
    )
    assert r.status_code == 200, r.text
    assert "code" not in r.json()["data"] and "pending_request" not in r.json()["data"]
    r = admin_client.post(_USERS, json={"username": "plana", "gateway_role": "operator"})
    assert r.status_code == 201 and "pending_request" not in r.json()["data"]


# --------------------------------------------------------------------------- #
# Quién no puede aprobar                                                       #
# --------------------------------------------------------------------------- #


def test_the_requester_cannot_approve_their_own_request(admin_client, target):
    req = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    r = admin_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.self_approval_forbidden")
    assert _status(req["id"])[0] == "pending"
    assert [a.status for a in _audits("access_request.approved")] == ["failure"]


def test_the_target_cannot_approve_their_own_elevation(admin_client, aa_client):
    """El destino es access_admin: pide el admin, aprueba el destino → no puede."""
    target = _uid("aa-segundo")
    req = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    r = aa_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.self_modification_forbidden")
    assert UserModel().find_by_id(target)["gateway_role"] == "viewer"


def test_a_non_access_admin_gets_an_opaque_403(admin_client, owner_client, target):
    req = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    for r in (owner_client.get(f"{_REQ}/pending"),
              owner_client.post(f"{_REQ}/{req['id']}/approve", json={}),
              owner_client.post(f"{_REQ}/{req['id']}/reject", json={})):
        assert (r.status_code, _code(r)) == (403, "access.forbidden")


def test_the_inbox_flags_can_decide_per_actor(admin_client, aa_client, target):
    req = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    (mine,) = admin_client.get(f"{_REQ}/pending").json()["data"]
    assert mine["id"] == req["id"]
    assert (mine["can_decide"], mine["blocked_reason"]) == (False, "access.self_approval_forbidden")
    (theirs,) = aa_client.get(f"{_REQ}/pending").json()["data"]
    assert (theirs["can_decide"], theirs["blocked_reason"]) == (True, None)
    assert theirs["target"]["username"] == "destino"


# --------------------------------------------------------------------------- #
# Vieja, partida, baja                                                         #
# --------------------------------------------------------------------------- #


def test_a_stale_request_is_409_and_gets_cancelled(admin_client, aa, target):
    req = _pending(aa.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    # El acceso cambia entretanto (un alcance nuevo que no eleva).
    r = admin_client.put(
        f"{_USERS}/{target}/access",
        json={"global_capabilities": [],
              "scope_grants": [{"scope_type": "environment", "scope_id": env_id(DEV),
                                "role": "operator"}]},
    )
    assert r.status_code == 200, r.text

    (row,) = admin_client.get(f"{_REQ}/pending").json()["data"]
    assert row["blocked_reason"] == "access.request_stale"
    r = admin_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.request_stale")
    assert _status(req["id"])[:2] == ("cancelled", "stale")
    assert UserModel().find_by_id(target)["gateway_role"] == "viewer"


def test_a_mixed_payload_applies_the_safe_part_and_parks_the_elevation(admin_client, aa_client):
    """
    Una persona con ``operator`` en DEV y ``viewer`` en PROD. El PUT saca PROD, agrega
    ``operator`` en staging, pide ``owner`` en DEV y ``access_admin``. La baja y lo que no eleva se
    aplican ya; ``owner`` y la global, al aprobarse, y el estado final es exactamente el pedido.
    """
    uid = create_user(admin_client, "mixta")["id"]
    r = admin_client.put(
        f"{_USERS}/{uid}/access",
        json={"global_capabilities": [],
              "scope_grants": [
                  {"scope_type": "environment", "scope_id": env_id(DEV), "role": "operator"},
                  {"scope_type": "environment", "scope_id": env_id(PROD), "role": "viewer"},
              ]},
    )
    assert r.status_code == 200, r.text

    deseado = {"global_capabilities": ["access_admin"],
               "scope_grants": [
                   {"scope_type": "environment", "scope_id": env_id(DEV), "role": "owner"},
                   {"scope_type": "environment", "scope_id": env_id("staging"), "role": "operator"},
               ]}
    r = admin_client.put(f"{_USERS}/{uid}/access", json=deseado)
    req = _pending(r)
    ctx = _ctx(uid)
    assert ctx["globals"] == []
    assert sorted(ctx["grants"]) == sorted([
        ("environment", env_id(DEV), "operator"),        # queda como estaba hasta aprobar
        ("environment", env_id("staging"), "operator"),  # alta que no eleva: ya
    ]), "PROD se quitó en el acto"
    assert r.json()["data"]["scope_grants"] == [
        {"scope_type": t, "scope_id": i, "role": ro} for (t, i, ro) in ctx["grants"]
    ]
    assert {(e["kind"], e.get("role") or e.get("global_capability")) for e in req["elevations"]} == {
        ("scope_grant", "owner"), ("global_capability", "access_admin")}

    assert aa_client.post(f"{_REQ}/{req['id']}/approve", json={}).status_code == 200
    ctx = _ctx(uid)
    assert ctx["globals"] == ["access_admin"]
    assert sorted(ctx["grants"]) == sorted([
        ("environment", env_id(DEV), "owner"),
        ("environment", env_id("staging"), "operator"),
    ])


def test_mixed_payload_then_approval_leaves_the_requested_final_state(admin_client, aa_client):
    uid = create_user(admin_client, "mixta2", global_capabilities=["access_admin"])["id"]
    deseado = {"global_capabilities": [],  # baja de access_admin: inmediata
               "scope_grants": [{"scope_type": "environment", "scope_id": env_id(DEV),
                                 "role": "owner"}]}
    req = _pending(admin_client.put(f"{_USERS}/{uid}/access", json=deseado))
    assert _ctx(uid)["globals"] == [] and _ctx(uid)["grants"] == []
    assert aa_client.post(f"{_REQ}/{req['id']}/approve", json={}).status_code == 200
    ctx = _ctx(uid)
    assert ctx["globals"] == [] and ctx["grants"] == [("environment", env_id(DEV), "owner")]


def test_demotions_apply_immediately(admin_client):
    uid = create_user(admin_client, "baja", gateway_role="owner",
                      global_capabilities=["access_admin"])["id"]
    r = admin_client.patch(f"{_USERS}/{uid}", json={"gateway_role": "viewer"})
    assert r.status_code == 200 and r.json()["data"]["gateway_role"] == "viewer"
    r = admin_client.put(f"{_USERS}/{uid}/access",
                         json={"global_capabilities": [], "scope_grants": []})
    assert r.status_code == 200 and r.json()["data"]["global_capabilities"] == []
    assert admin_client.get(f"{_REQ}/pending").json()["data"] == []


def test_a_new_request_supersedes_the_pending_one(admin_client, target):
    viejo = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    nuevo = _pending(admin_client.put(
        f"{_USERS}/{target}/access",
        json={"global_capabilities": ["security_officer"], "scope_grants": []}))
    assert _status(viejo["id"])[:2] == ("cancelled", "superseded")
    assert [r["id"] for r in admin_client.get(f"{_REQ}/pending").json()["data"]] == [nuevo["id"]]


# --------------------------------------------------------------------------- #
# Vencimiento, rechazo, cancelación, pérdida del rol                           #
# --------------------------------------------------------------------------- #


def test_requests_expire_after_seven_days(admin_client, aa_client, target):
    req = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    vence = req["expires_at"]
    assert vence
    _sql("UPDATE access_change_requests SET expires_at = :t WHERE id = :i",
         t=utcnow() - timedelta(minutes=1), i=req["id"])
    assert aa_client.get(f"{_REQ}/pending").json()["data"] == []
    assert _status(req["id"])[:2] == ("expired", "expired")
    (a,) = _audits("access_request.expired")
    assert a.actor_type == "system"
    r = aa_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.request_not_pending")


def test_the_startup_sweep_expires_overdue_requests(admin_client, target):
    from app.controllers.access_request_controller import AccessRequestController

    req = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    _sql("UPDATE access_change_requests SET expires_at = :t WHERE id = :i",
         t=utcnow() - timedelta(minutes=1), i=req["id"])
    assert AccessRequestController().expire_overdue() == 1
    assert AccessRequestController().expire_overdue() == 0


def test_reject_and_cancel(admin_client, aa_client, target):
    a = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    r = aa_client.post(f"{_REQ}/{a['id']}/reject", json={"reason": "no corresponde"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "rejected"
    assert r.json()["data"]["reason"] == "no corresponde"
    assert UserModel().find_by_id(target)["gateway_role"] == "viewer"

    b = _pending(admin_client.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    r = aa_client.post(f"{_REQ}/{b['id']}/cancel", json={})
    assert (r.status_code, _code(r)) == (409, "access.request_not_requester")
    r = admin_client.post(f"{_REQ}/{b['id']}/cancel", json={})
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    r = admin_client.post(f"{_REQ}/{b['id']}/cancel", json={})
    assert (r.status_code, _code(r)) == (409, "access.request_not_pending")
    assert admin_client.get(f"{_REQ}/9999").status_code == 404
    assert _code(admin_client.post(f"{_REQ}/9999/approve", json={})) == "access.request_not_found"


def test_the_requester_losing_access_admin_cancels_their_requests(admin_client, aa, target):
    req = _pending(aa.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    r = admin_client.put(f"{_USERS}/{_uid('aa-pide')}/access",
                         json={"global_capabilities": [], "scope_grants": []})
    assert r.status_code == 200, r.text
    assert _status(req["id"])[:2] == ("cancelled", "requester_lost_access")


def test_approve_rechecks_the_requester_even_if_the_hook_was_bypassed(admin_client, aa, target):
    req = _pending(aa.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    _sql("DELETE FROM user_global_capabilities WHERE user_id = :u", u=_uid("aa-pide"))
    r = admin_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.request_not_pending")
    assert _status(req["id"])[:2] == ("cancelled", "requester_lost_access")


def test_an_inactive_target_cannot_be_elevated(admin_client, aa, target):
    req = _pending(aa.patch(f"{_USERS}/{target}", json={"gateway_role": "owner"}))
    _sql("UPDATE users SET is_active = 0 WHERE id = :u", u=target)
    r = admin_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.grant_user_inactive")


# --------------------------------------------------------------------------- #
# Separación de deberes al aprobar                                             #
# --------------------------------------------------------------------------- #


def test_approval_rechecks_separation_of_duties(admin_client, aa_client):
    """
    Una cuenta combinada (owner + security_officer) con override vivo pide un ``owner`` por
    alcance: la excepción la cubre y se puede pedir. Si el override vence antes de aprobar, la
    aprobación da 409 ``access.sod_conflict`` (el ``before_hash`` no lo ve: no cambió el acceso).
    """
    r = admin_client.post(_USERS, json={
        "username": "combinada", "gateway_role": "owner",
        "global_capabilities": ["security_officer"], "sod_override": {"reason": _RAZON}})
    uid = settle(r)["id"]
    req = _pending(admin_client.put(
        f"{_USERS}/{uid}/access",
        json={"global_capabilities": ["security_officer"],
              "scope_grants": [{"scope_type": "environment", "scope_id": env_id(DEV),
                                "role": "owner"}]}))
    _sql("UPDATE sod_exceptions SET expires_at = :t WHERE user_id = :u",
         t=utcnow() - timedelta(minutes=1), u=uid)
    r = aa_client.post(f"{_REQ}/{req['id']}/approve", json={})
    assert (r.status_code, _code(r)) == (409, "access.sod_conflict")
    assert _status(req["id"])[0] == "pending"


def test_the_sod_override_travels_with_the_request(admin_client, aa_client):
    r = admin_client.post(_USERS, json={
        "username": "conover", "gateway_role": "owner",
        "global_capabilities": ["security_officer"], "sod_override": {"reason": _RAZON}})
    req = _pending(r)
    assert req["sod_override"]["reason"] == _RAZON
    uid = r.json()["data"]["id"]
    with Database().engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM sod_exceptions WHERE user_id = :u"),
                            {"u": uid}).scalar() == 0
    assert aa_client.post(f"{_REQ}/{req['id']}/approve", json={}).status_code == 200
    with Database().engine.begin() as conn:
        fila = conn.execute(text("SELECT requested_by, approved_by FROM sod_exceptions "
                                 "WHERE user_id = :u"), {"u": uid}).fetchone()
    assert tuple(fila) == (_uid("admin"), _uid("aa-segundo"))


# --------------------------------------------------------------------------- #
# ACCESS_FOUR_EYES=False                                                       #
# --------------------------------------------------------------------------- #


def test_without_four_eyes_a_lone_admin_elevates_alone_and_it_is_audited(admin_client, monkeypatch):
    from app.controllers import access_request_controller

    monkeypatch.setattr(access_request_controller, "ACCESS_FOUR_EYES", False)
    r = admin_client.post(_USERS, json={"username": "solo", "gateway_role": "owner",
                                        "global_capabilities": ["access_admin"]})
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["gateway_role"] == "owner" and d["global_capabilities"] == ["access_admin"]
    assert "pending_request" not in d

    r = admin_client.put(f"{_USERS}/{d['id']}/access",
                         json={"global_capabilities": ["access_admin"],
                               "scope_grants": [{"scope_type": "environment",
                                                 "scope_id": env_id(DEV), "role": "owner"}]})
    assert r.status_code == 200, r.text

    r = admin_client.post(f"{_USERS}/{d['id']}/capability-grants",
                          json={"capability": "exports.download", "scope_type": "environment",
                                "scope_id": env_id(PROD)})
    assert r.status_code == 201 and r.json()["data"]["status"] == "active"

    filas = _audits("access.elevation_unapproved")
    assert [json.loads(a.detail)["origin"] for a in filas] == [
        "create", "set_access", "capability_grant"]
    assert all(a.admin_username == "admin" for a in filas)
    assert admin_client.get(f"{_REQ}/pending").json()["data"] == []


def test_four_eyes_is_on_by_default():
    from app.controllers.access_request_controller import four_eyes
    from app.core.environments import ACCESS_FOUR_EYES

    assert ACCESS_FOUR_EYES is True and four_eyes() is True
