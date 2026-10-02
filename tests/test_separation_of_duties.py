"""
Separación de deberes (C2): ``security_officer`` no convive con ``owner`` ni con ``access_admin``.

Se mide: el 409 ``access.sod_conflict`` en cada escritor (alta, PATCH, PUT de accesos y
capacidades puntuales) sobre el estado RESULTANTE; el break-glass (``sod_override``) que escribe
la excepción y la audita; la defensa al LEER que descarta ``security_officer`` sin excepción
viva; la herencia del administrador sembrado (que conserva todo); el reporte de arranque,
``GET /authz/sod-report`` y ``/auth/me.sod_warnings``.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.database import Database
from app.models.audit_log import AuditLog
from app.models.user_model import UserModel
from app.services.capability_catalog import (
    GLOBAL_CAPABILITIES,
    SOD_RULE_ACCESS_ADMIN,
    SOD_RULE_OWNER,
    Capability,
    GlobalCapability,
)
from tests.scope_helpers import env_id
from tests.test_api_gateway_users import _cliente_como, _code, _crear

DEV = "development"
_USERS = "/api/v1/gateway-users"
_RAZON = "Incidente 4711: no hay otro security_officer disponible esta semana"

_SO_CAPS = {c.value for c in GLOBAL_CAPABILITIES[GlobalCapability.SECURITY_OFFICER]}


# --------------------------------------------------------------------------- #
# Arnés                                                                       #
# --------------------------------------------------------------------------- #


def _pc(r) -> dict:
    return (r.json().get("detail") or {}).get("public_context") or {}


def _uid(username: str) -> int:
    return UserModel().find_by_username(username)["id"]


def _exceptions(user_id: int) -> list[dict]:
    with Database().engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT id, rule, reason, requested_by, approved_by, expires_at, closed_at, "
                    "closed_reason FROM sod_exceptions WHERE user_id = :u ORDER BY id"
                ),
                {"u": user_id},
            ).mappings()
        ]


def _audits(action: str, status: str | None = None) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        q = s.query(AuditLog).filter(AuditLog.action == action)
        if status:
            q = q.filter(AuditLog.status == status)
        rows = q.order_by(AuditLog.id).all()
        s.expunge_all()
        return rows
    finally:
        s.close()


def _sql(sql: str, **params) -> None:
    with Database().engine.begin() as conn:
        conn.execute(text(sql), params)


def _grant(client, user_id, capability, **extra):
    body = {"capability": capability, "scope_type": "environment", "scope_id": env_id(DEV),
            **extra}
    return client.post(f"{_USERS}/{user_id}/capability-grants", json=body)


def _assert_conflict(r, *rules: str) -> None:
    assert r.status_code == 409, r.text
    pc = _pc(r)
    assert pc["code"] == "access.sod_conflict"
    assert pc["rules"] == sorted(rules)
    assert [c["rule"] for c in pc["conflicts"]] == sorted(rules)
    assert pc["override"]["field"] == "sod_override"


# --------------------------------------------------------------------------- #
# Escritor: 409 sobre el estado resultante                                    #
# --------------------------------------------------------------------------- #


def test_create_owner_base_with_security_officer_is_409(admin_client):
    r = admin_client.post(_USERS, json={"username": "combo1", "gateway_role": "owner",
                                        "global_capabilities": ["security_officer"]})
    _assert_conflict(r, SOD_RULE_OWNER)
    assert _pc(r)["conflicts"][0]["sources"] == [{"kind": "base_role", "role": "owner"}]
    assert UserModel().find_by_username("combo1") is None


def test_create_access_admin_with_security_officer_is_409(admin_client):
    r = admin_client.post(_USERS, json={
        "username": "combo2", "global_capabilities": ["access_admin", "security_officer"]})
    _assert_conflict(r, SOD_RULE_ACCESS_ADMIN)
    assert UserModel().find_by_username("combo2") is None


def test_patch_role_to_owner_on_a_security_officer_is_409(admin_client):
    so = _crear(admin_client, "so1", global_capabilities=["security_officer"])
    r = admin_client.patch(f"{_USERS}/{so['id']}", json={"gateway_role": "owner"})
    _assert_conflict(r, SOD_RULE_OWNER)
    assert UserModel().find_by_id(so["id"])["gateway_role"] == "viewer"
    # Lo que no junta las dos funciones pasa.
    r = admin_client.patch(f"{_USERS}/{so['id']}", json={"gateway_role": "operator"})
    assert r.status_code == 200, r.text


def test_put_owner_scope_grant_on_a_security_officer_is_409(admin_client):
    so = _crear(admin_client, "so2", global_capabilities=["security_officer"])
    r = admin_client.put(f"{_USERS}/{so['id']}/access", json={
        "global_capabilities": ["security_officer"],
        "scope_grants": [{"scope_type": "environment", "scope_id": env_id(DEV), "role": "owner"}],
    })
    _assert_conflict(r, SOD_RULE_OWNER)
    assert _pc(r)["conflicts"][0]["sources"][0]["kind"] == "scope_grant"


def test_put_adding_security_officer_to_an_owner_or_access_admin_is_409(admin_client):
    owner = _crear(admin_client, "owner1", gateway_role="owner")
    r = admin_client.put(f"{_USERS}/{owner['id']}/access",
                         json={"global_capabilities": ["security_officer"], "scope_grants": []})
    _assert_conflict(r, SOD_RULE_OWNER)

    aa = _crear(admin_client, "aa1", global_capabilities=["access_admin"])
    r = admin_client.put(f"{_USERS}/{aa['id']}/access", json={
        "global_capabilities": ["access_admin", "security_officer"], "scope_grants": []})
    _assert_conflict(r, SOD_RULE_ACCESS_ADMIN)


def test_owner_only_capability_grant_to_a_security_officer_is_409(admin_client):
    so = _crear(admin_client, "so3", global_capabilities=["security_officer"])
    r = _grant(admin_client, so["id"], "blueprints.apply")
    _assert_conflict(r, SOD_RULE_OWNER)
    src = _pc(r)["conflicts"][0]["sources"][0]
    assert src["kind"] == "capability_grant" and src["capability"] == "blueprints.apply"
    assert [a.status for a in _audits("capability_grant.created")][-1] == "failure"
    # Una capacidad que operator ya tiene no es owner en sustancia.
    assert _grant(admin_client, so["id"], "databases.write").status_code == 201


def test_security_officer_on_top_of_a_live_owner_only_grant_is_409(admin_client):
    """Una puntual PENDIENTE cuenta: surte efecto en cuanto se aprueba."""
    u = _crear(admin_client, "conapply")
    assert _grant(admin_client, u["id"], "exports.download").json()["data"]["status"] == "pending"
    r = admin_client.put(f"{_USERS}/{u['id']}/access",
                         json={"global_capabilities": ["security_officer"], "scope_grants": []})
    _assert_conflict(r, SOD_RULE_OWNER)


# --------------------------------------------------------------------------- #
# Break-glass                                                                  #
# --------------------------------------------------------------------------- #


def test_override_writes_an_exception_row_and_audits_it(admin_client):
    r = admin_client.post(_USERS, json={
        "username": "breakglass", "gateway_role": "owner",
        "global_capabilities": ["security_officer"],
        "sod_override": {"reason": _RAZON, "expires_in_hours": 24},
    })
    assert r.status_code == 201, r.text
    uid = r.json()["data"]["id"]

    filas = _exceptions(uid)
    assert len(filas) == 1
    f = filas[0]
    assert f["rule"] == SOD_RULE_OWNER and f["reason"] == _RAZON
    assert f["requested_by"] == _uid("admin")
    assert f["approved_by"] is None  # C3: el segundo aprobador
    vence = f["expires_at"] if isinstance(f["expires_at"], datetime) else datetime.fromisoformat(
        str(f["expires_at"]))
    assert timedelta(hours=23) < vence - datetime.utcnow() <= timedelta(hours=24)

    assert len(_audits("access.sod_override", "attempt")) == 1
    ok = _audits("access.sod_override", "success")
    assert len(ok) == 1 and ok[0].target_id == uid
    d = json.loads(ok[0].detail)
    assert d["rules"] == [SOD_RULE_OWNER] and d["exception_ids"] == [f["id"]]
    assert d["reason"] == _RAZON

    # La excepción cubre la combinación al LEER: conserva security_officer.
    eff = admin_client.get(f"{_USERS}/{uid}/effective-access").json()["data"]
    assert "security_officer" in eff["global_capabilities"]
    assert _SO_CAPS <= {e["capability"] for e in eff["capabilities"]}


def test_override_validation_is_422_with_a_closed_code(admin_client):
    base = {"username": "corta", "gateway_role": "owner",
            "global_capabilities": ["security_officer"]}
    r = admin_client.post(_USERS, json={**base, "sod_override": {"reason": "porque sí"}})
    assert r.status_code == 422, r.text
    assert _code(r) == "access.sod_override_invalid"
    r = admin_client.post(_USERS, json={
        **base, "sod_override": {"reason": _RAZON, "expires_in_hours": 24 * 8}})
    assert r.status_code == 422 and _code(r) == "access.sod_override_invalid"
    assert UserModel().find_by_username("corta") is None
    assert not _audits("access.sod_override")


def test_override_on_a_capability_grant(admin_client):
    so = _crear(admin_client, "so4", global_capabilities=["security_officer"])
    r = _grant(admin_client, so["id"], "blueprints.apply", sod_override={"reason": _RAZON})
    assert r.status_code == 201, r.text
    assert [f["rule"] for f in _exceptions(so["id"])] == [SOD_RULE_OWNER]
    assert _audits("access.sod_override", "success")


def test_a_resolved_combination_closes_its_exception(admin_client):
    r = admin_client.post(_USERS, json={
        "username": "resuelve", "gateway_role": "owner",
        "global_capabilities": ["security_officer"], "sod_override": {"reason": _RAZON}})
    uid = r.json()["data"]["id"]
    r = admin_client.patch(f"{_USERS}/{uid}", json={"gateway_role": "viewer"})
    assert r.status_code == 200, r.text
    f = _exceptions(uid)[0]
    assert f["closed_at"] is not None and f["closed_reason"] == "resolved"
    # Ya no cubre nada: volver a juntarlas pide otro override.
    r = admin_client.patch(f"{_USERS}/{uid}", json={"gateway_role": "owner"})
    _assert_conflict(r, SOD_RULE_OWNER)


# --------------------------------------------------------------------------- #
# Lector: neutralización sin excepción viva                                   #
# --------------------------------------------------------------------------- #


def test_read_time_neutralization_without_exception(admin_client):
    datos = _crear(admin_client, "amano", global_capabilities=["security_officer"])
    so = _cliente_como(datos, "amano")
    assert "environments.write" in so.get("/api/v1/auth/me").json()["data"]["capabilities"]

    # La combinación entra por fuera del escritor (un UPDATE a mano).
    _sql("UPDATE users SET gateway_role = 'owner' WHERE id = :u", u=datos["id"])
    me = so.get("/api/v1/auth/me").json()["data"]
    assert not (_SO_CAPS & set(me["capabilities"])), "security_officer debía caerse"
    assert "security_officer" not in me["global_capabilities"]
    assert Capability.DATABASES_DROP.value in me["capabilities"]  # owner se conserva
    assert me["sod_warnings"] == [{"rule": SOD_RULE_OWNER, "status": "neutralized",
                                   "reason": None, "since": None, "expires_at": None}]
    r = so.post("/api/v1/environments", json={"name": "X", "slug": "x", "rank": 5})
    assert r.status_code == 403 and _code(r) == "access.forbidden"

    denegaciones = [json.loads(a.detail) for a in _audits("access.denied")]
    assert any(d["check"] == "sod" and d["rules"] == [SOD_RULE_OWNER] for d in denegaciones)

    # La vista de acceso efectivo dice lo mismo que se hace cumplir.
    eff = admin_client.get(f"{_USERS}/{datos['id']}/effective-access").json()["data"]
    assert "security_officer" not in eff["global_capabilities"]


def test_an_expired_override_stops_covering(admin_client):
    r = admin_client.post(_USERS, json={
        "username": "vencida", "gateway_role": "owner",
        "global_capabilities": ["security_officer"], "sod_override": {"reason": _RAZON}})
    uid = r.json()["data"]["id"]
    _sql("UPDATE sod_exceptions SET expires_at = :t WHERE user_id = :u",
         t=datetime.utcnow() - timedelta(minutes=1), u=uid)
    eff = admin_client.get(f"{_USERS}/{uid}/effective-access").json()["data"]
    assert "security_officer" not in eff["global_capabilities"]


# --------------------------------------------------------------------------- #
# El administrador sembrado: heredado, conserva todo                          #
# --------------------------------------------------------------------------- #


def test_grandfathered_seed_admin_keeps_every_capability(admin_client):
    admin_id = _uid("admin")
    filas = _exceptions(admin_id)
    assert sorted(f["rule"] for f in filas) == sorted([SOD_RULE_OWNER, SOD_RULE_ACCESS_ADMIN])
    assert all(f["reason"] == "grandfathered" and f["expires_at"] is None for f in filas)

    me = admin_client.get("/api/v1/auth/me").json()["data"]
    assert set(me["capabilities"]) == {c.value for c in Capability}
    assert {(w["rule"], w["status"]) for w in me["sod_warnings"]} == {
        (SOD_RULE_OWNER, "grandfathered"), (SOD_RULE_ACCESS_ADMIN, "grandfathered")}

    # Editable mientras el cambio no agregue una regla nueva: su excepción la cubre.
    r = admin_client.patch(f"{_USERS}/{admin_id}", json={"full_name": "Otra"})
    assert r.status_code == 200, r.text


def test_boot_report_audits_each_grandfathered_row_once(admin_client):
    filas = _audits("access.sod_grandfathered")
    assert sorted(json.loads(a.detail)["rule"] for a in filas) == sorted(
        [SOD_RULE_OWNER, SOD_RULE_ACCESS_ADMIN])
    assert all(a.actor_type == "system" for a in filas)

    from app.services.sod_service import report_sod_violations

    assert report_sod_violations() == 0  # mismo arranque: no se repite
    assert len(_audits("access.sod_grandfathered")) == 2


def test_plain_user_has_no_sod_warnings(admin_client):
    datos = _crear(admin_client, "normal", global_capabilities=["security_officer"])
    me = _cliente_como(datos, "normal").get("/api/v1/auth/me").json()["data"]
    assert me["sod_warnings"] == []


# --------------------------------------------------------------------------- #
# GET /authz/sod-report                                                       #
# --------------------------------------------------------------------------- #


def test_sod_report_lists_grandfathered_overrides_and_uncovered(admin_client):
    r = admin_client.post(_USERS, json={
        "username": "conover", "gateway_role": "owner",
        "global_capabilities": ["security_officer"], "sod_override": {"reason": _RAZON}})
    over_id = r.json()["data"]["id"]
    sin = _crear(admin_client, "sincubrir", global_capabilities=["security_officer"])
    _sql("UPDATE users SET gateway_role = 'owner' WHERE id = :u", u=sin["id"])

    r = admin_client.get("/api/v1/authz/sod-report")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    por_usuario = {(e["user"]["username"], e["rule"]): e for e in data["exceptions"]}
    g = por_usuario[("admin", SOD_RULE_OWNER)]
    assert g["kind"] == "grandfathered" and g["reason"] == "grandfathered"
    assert g["expires_at"] is None and g["still_violating"] is True and g["since"]
    assert ("admin", SOD_RULE_ACCESS_ADMIN) in por_usuario
    o = por_usuario[("conover", SOD_RULE_OWNER)]
    assert o["kind"] == "override" and o["reason"] == _RAZON and o["expires_at"]
    assert o["requested_by"]["username"] == "admin" and o["approved_by"] is None
    assert o["user"]["id"] == over_id
    assert data["uncovered"] == [
        {"user": {"id": sin["id"], "username": "sincubrir"}, "user_active": True,
         "rules": [SOD_RULE_OWNER]}
    ]


def test_sod_report_is_access_admin_only(admin_client):
    datos = _crear(admin_client, "so-rep", global_capabilities=["security_officer"])
    r = _cliente_como(datos, "so-rep").get("/api/v1/authz/sod-report")
    assert r.status_code == 403 and _code(r) == "access.forbidden"


# --------------------------------------------------------------------------- #
# Aprobación de capacidades puntuales: se re-chequea                          #
# --------------------------------------------------------------------------- #


def test_approving_an_owner_only_grant_rechecks_the_rule(admin_client):
    destino = _crear(admin_client, "destino-sod")
    g = _grant(admin_client, destino["id"], "exports.download").json()["data"]
    assert g["status"] == "pending"
    # Entre el alta y la aprobación, la persona recibe security_officer por fuera del escritor.
    _sql("INSERT INTO user_global_capabilities (user_id, capability, created_at, updated_at) "
         "VALUES (:u, 'security_officer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
         u=destino["id"])

    aprobador = _crear(admin_client, "aprob-sod", gateway_role="owner",
                       global_capabilities=["access_admin"])
    c = _cliente_como(aprobador, "aprob-sod")
    r = c.post(f"/api/v1/capability-grants/{g['id']}/approve", json={})
    _assert_conflict(r, SOD_RULE_OWNER)
