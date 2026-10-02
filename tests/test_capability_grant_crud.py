"""
Capacidades puntuales: alta, listado y revocación (``/gateway-users/{id}/capability-grants``).

Lo que se mide son las reglas de negocio: solo ``access_admin`` actúa, nadie se otorga ni se
revoca a sí mismo, las globales no se otorgan, asigna la FUNCIÓN (no lo que tiene quien otorga), las 11
sensibles nacen ``pending`` sin efecto, revocar es de un solo administrador, y ``PUT /access``
no toca las capacidades puntuales.
"""

import json

import pytest
from sqlalchemy import text

from app.core.database import Database
from app.models.audit_log import AuditLog
from app.models.user_model import UserModel
from tests.scope_helpers import env_id
from tests.test_api_gateway_users import _cliente_como, _code, _crear

DEV = "development"
PROD = "production"


# --------------------------------------------------------------------------- #
# Arnés                                                                       #
# --------------------------------------------------------------------------- #


def _uid(username: str) -> int:
    return UserModel().find_by_username(username)["id"]


def _grant(client, user_id, capability="databases.write", scope_type="environment",
           scope_id=None, **extra):
    body = {"capability": capability, "scope_type": scope_type,
            "scope_id": scope_id if scope_id is not None else env_id(DEV), **extra}
    return client.post(f"/api/v1/gateway-users/{user_id}/capability-grants", json=body)


def _insert_cg(user_id, capability, scope_type, scope_id, status="active"):
    live = 1 if status in ("pending", "active") else None
    with Database().engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO capability_grants (user_id, capability, scope_type, scope_id, "
                "status, live_key, requested_at, created_at, updated_at) VALUES "
                "(:u, :c, :t, :i, :s, :l, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": user_id, "c": capability, "t": scope_type, "i": scope_id, "s": status, "l": live},
        )


def _row(grant_id: int) -> dict:
    with Database().engine.begin() as conn:
        r = conn.execute(
            text("SELECT status, live_key, decided_by FROM capability_grants WHERE id = :i"),
            {"i": grant_id},
        ).mappings().fetchone()
    return dict(r)


def _audits(action: str) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        rows = s.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
        s.expunge_all()
        return rows
    finally:
        s.close()


@pytest.fixture()
def target(admin_client):
    """La persona que recibe las capacidades."""
    return _crear(admin_client, "destino")["id"]


def _admin_como(admin_client, username, role="operator", extra=("access_admin",)):
    datos = _crear(admin_client, username, gateway_role=role, global_capabilities=list(extra))
    return datos["id"], _cliente_como(datos, username)


# --------------------------------------------------------------------------- #
# R1 / R2: alta                                                               #
# --------------------------------------------------------------------------- #


def test_a_plain_capability_is_created_active_and_audited(admin_client, target):
    r = _grant(admin_client, target, "blueprints.write", scope_id=env_id(DEV),
               reason="release del viernes")
    assert r.status_code == 201, r.text
    g = r.json()["data"]
    assert g["status"] == "active"
    assert g["sensitive"] is False
    assert g["username"] == "destino"
    assert g["scope_name"]
    assert g["requested_by"] == {"id": _uid("admin"), "username": "admin"}
    assert g["decided_by"] is None and g["expires_at"] is None
    assert g["request_reason"] == "release del viernes"
    assert g["implies"] == ["blueprints.read"]

    fila = _row(g["id"])
    assert fila["live_key"] == 1

    (a,) = _audits("capability_grant.created")
    assert a.status == "success"
    assert (a.grantee, a.privilege, a.object_level) == ("destino", "blueprints.write", "environment")
    assert a.object_name == f"environment:{env_id(DEV)}"
    detail = json.loads(a.detail)
    assert detail["before"] == {"status": None} and detail["after"] == {"status": "active"}
    assert detail["grant_id"] == g["id"]


def test_the_new_capability_is_active_for_the_grantee(admin_client, target):
    _grant(admin_client, target, "databases.write")
    cgs = UserModel().find_access_context(target)["capability_grants"]
    assert [(c["capability"], c["scope_type"]) for c in cgs] == [("databases.write", "environment")]


@pytest.mark.parametrize(
    "capability",
    ["engine_users.secrets", "engine_users.credentials", "blueprints.captures",
     "clones.execute", "exports.download", "sql_console.execute", "engine_users.drop",
     "databases.drop"],
)
def test_the_eight_sensitive_capabilities_are_created_pending_without_effect(
    admin_client, target, capability
):
    r = _grant(admin_client, target, capability)
    assert r.status_code == 201, r.text
    g = r.json()["data"]
    assert g["status"] == "pending"
    assert g["sensitive"] is True
    assert g["expires_at"] is not None
    assert _row(g["id"])["live_key"] == 1
    # Pendiente: ningún efecto en la capa 1 ni en la 2 (el lector solo carga las activas).
    assert UserModel().find_access_context(target)["capability_grants"] == []

    (a,) = _audits("capability_grant.requested")
    assert json.loads(a.detail)["after"] == {"status": "pending"}


def test_a_live_duplicate_is_rejected_whether_active_or_pending(admin_client, target):
    assert _grant(admin_client, target, "databases.write").status_code == 201
    r = _grant(admin_client, target, "databases.write")
    assert (r.status_code, _code(r)) == (409, "access.grant_duplicate")

    assert _grant(admin_client, target, "exports.download").status_code == 201
    r = _grant(admin_client, target, "exports.download")
    assert (r.status_code, _code(r)) == (409, "access.grant_duplicate")

    # Otro alcance no es duplicado.
    assert _grant(admin_client, target, "databases.write", scope_id=env_id(PROD)).status_code == 201


def test_the_unique_constraint_backs_up_the_precheck(admin_client, target):
    """Si dos altas pasan el chequeo previo, el ``UNIQUE`` hace perder a la segunda."""
    from app.controllers.capability_grant_controller import CapabilityGrantController
    from app.exceptions import AppHttpException

    c = CapabilityGrantController()
    c.grants.insert(user_id=target, capability="databases.write", scope_type="environment",
                    scope_id=env_id(DEV), requested_by=1, pending=False, reason=None)
    with pytest.raises(AppHttpException) as exc:
        c.grants.insert(user_id=target, capability="databases.write", scope_type="environment",
                        scope_id=env_id(DEV), requested_by=1, pending=False, reason=None)
    assert exc.value.status_code == 409
    assert exc.value.public_context["code"] == "access.grant_duplicate"


@pytest.mark.parametrize(
    "capability",
    ["servers.admin", "catalogs.write", "environments.write", "access.admin", "policy.admin",
     "gateway.admin", "self.read", "access_admin", "security_officer", "no.existe"],
)
def test_global_or_unknown_capabilities_are_not_grantable(admin_client, target, capability):
    r = _grant(admin_client, target, capability)
    assert (r.status_code, _code(r)) == (422, "access.capability_not_grantable")


def test_global_scope_is_not_accepted(admin_client, target):
    r = admin_client.post(
        f"/api/v1/gateway-users/{target}/capability-grants",
        json={"capability": "databases.write", "scope_type": "global", "scope_id": 1},
    )
    assert r.status_code == 422


def test_a_missing_scope_target_is_404(admin_client, target):
    r = _grant(admin_client, target, "databases.write", scope_id=999)
    assert (r.status_code, _code(r)) == (404, "access.grant_scope_not_found")
    r = _grant(admin_client, target, "databases.write", scope_type="server", scope_id=999)
    assert (r.status_code, _code(r)) == (404, "access.grant_scope_not_found")


def test_a_server_scope_grant_resolves_its_name(admin_client, target, server_payload):
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    r = _grant(admin_client, target, "databases.write", scope_type="server", scope_id=sid)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["scope_name"] == "srv-test"


def test_an_unknown_grantee_is_404(admin_client):
    r = _grant(admin_client, 9999, "databases.write")
    assert r.status_code == 404


def test_an_inactive_grantee_is_rejected(admin_client, target):
    assert admin_client.patch(
        f"/api/v1/gateway-users/{target}", json={"is_active": False}
    ).status_code == 200
    r = _grant(admin_client, target, "databases.write")
    assert (r.status_code, _code(r)) == (409, "access.grant_user_inactive")


def test_nobody_grants_capabilities_to_themselves(admin_client):
    r = _grant(admin_client, _uid("admin"), "databases.write")
    assert (r.status_code, _code(r)) == (409, "access.self_modification_forbidden")
    (a,) = _audits("capability_grant.created")
    assert a.status == "failure"


def test_only_access_admin_may_create(admin_client, target):
    """
    Un operador y un security_officer reciben 403 opaco. Lo da ``require(access.admin)`` en la
    ruta: ``security_officer`` ya no tiene la capacidad (antes compartían ``gateway.admin`` y el
    controller re-exigía la global).
    """
    _, oper = _admin_como(admin_client, "operadora", extra=())
    r = _grant(oper, target)
    assert (r.status_code, _code(r)) == (403, "access.forbidden")

    _, oficial = _admin_como(admin_client, "oficial", extra=("security_officer",))
    r = _grant(oficial, target)
    assert (r.status_code, _code(r)) == (403, "access.forbidden")
    # Opaco: el cuerpo no nombra la capacidad que falta.
    assert "access_admin" not in json.dumps(r.json()["detail"]["public_context"])


def test_a_viewer_access_admin_grants_by_duty_not_by_holdings(admin_client, target):
    """
    Política de asignación (C3): lo que cuenta es la FUNCIÓN (``access_admin``), no lo que tiene
    quien otorga. Un access_admin ``viewer`` otorga ``databases.write`` en el acto; una exclusiva de
    owner (``blueprints.apply``) también la puede pedir, pero nace pendiente del segundo aprobador.
    Antes el techo por tenencia rechazaba las dos con ``access.grant_ceiling_exceeded``.
    """
    _, vera = _admin_como(admin_client, "vera", role="viewer")
    r = _grant(vera, target, "databases.write")
    assert r.status_code == 201, r.text
    assert r.json()["data"]["status"] == "active"
    r = _grant(vera, target, "blueprints.apply", scope_id=env_id(PROD))
    assert r.status_code == 201, r.text
    assert r.json()["data"]["status"] == "pending" and r.json()["data"]["sensitive"] is True
    assert not [a for a in _audits("capability_grant.created") if a.status == "failure"]


def test_a_scoped_role_does_not_limit_what_an_access_admin_assigns(admin_client, target):
    """operator@dev y viewer@prod: asigna igual en prod. El alcance del actor ya no es un techo."""
    datos = _crear(admin_client, "mixta", gateway_role="viewer", global_capabilities=["access_admin"])
    r = admin_client.put(
        f"/api/v1/gateway-users/{datos['id']}/access",
        json={"global_capabilities": ["access_admin"],
              "scope_grants": [{"scope_type": "environment", "scope_id": env_id(DEV),
                                "role": "operator"}]},
    )
    assert r.status_code == 200, r.text
    mixta = _cliente_como(datos, "mixta")
    assert _grant(mixta, target, "databases.write", scope_id=env_id(DEV)).status_code == 201
    assert _grant(mixta, target, "databases.write", scope_id=env_id(PROD)).status_code == 201


def test_a_caller_without_the_duty_is_not_assignable():
    """Fail-closed en el controller: un actor sin ``access_admin`` (llamador interno) no asigna."""
    from app.controllers.capability_grant_controller import CapabilityGrantController

    assert CapabilityGrantController._assignable(None, "databases.write") is False


def test_unauthenticated_requests_are_401(client):
    assert client.get("/api/v1/gateway-users/1/capability-grants").status_code == 401
    assert client.post("/api/v1/gateway-users/1/capability-grants", json={}).status_code in (401, 403)
    assert client.delete("/api/v1/gateway-users/1/capability-grants/1").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# R4: revocación                                                              #
# --------------------------------------------------------------------------- #


def test_revoking_an_active_grant_is_immediate_and_keeps_history(admin_client, target):
    gid = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    r = admin_client.delete(f"/api/v1/gateway-users/{target}/capability-grants/{gid}")
    assert r.status_code == 200, r.text
    g = r.json()["data"]
    assert g["status"] == "revoked"
    assert g["decided_by"]["username"] == "admin" and g["decided_at"] is not None
    assert _row(gid) == {"status": "revoked", "live_key": None, "decided_by": _uid("admin")}
    assert UserModel().find_access_context(target)["capability_grants"] == []

    (a,) = _audits("capability_grant.revoked")
    d = json.loads(a.detail)
    assert d["before"] == {"status": "active"} and d["after"] == {"status": "revoked"}

    # El historial no bloquea volver a otorgar la misma tupla.
    assert _grant(admin_client, target, "databases.write").status_code == 201


def test_revoking_a_pending_grant_cancels_it(admin_client, target):
    gid = _grant(admin_client, target, "exports.download").json()["data"]["id"]
    r = admin_client.delete(f"/api/v1/gateway-users/{target}/capability-grants/{gid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "cancelled"
    assert _row(gid)["live_key"] is None
    assert len(_audits("capability_grant.cancelled")) == 1


def test_a_single_access_admin_can_revoke(admin_client, target):
    """Revocar es de un solo administrador, sin segundo aprobador: vera (viewer) revoca."""
    gid = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    _, vera = _admin_como(admin_client, "vera", role="viewer")
    r = vera.delete(f"/api/v1/gateway-users/{target}/capability-grants/{gid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "revoked"


def test_revoking_twice_or_a_foreign_or_unknown_grant(admin_client, target):
    otro = _crear(admin_client, "otro")["id"]
    gid = _grant(admin_client, target, "databases.write").json()["data"]["id"]

    r = admin_client.delete(f"/api/v1/gateway-users/{otro}/capability-grants/{gid}")
    assert (r.status_code, _code(r)) == (404, "access.grant_not_found")
    r = admin_client.delete(f"/api/v1/gateway-users/{target}/capability-grants/9999")
    assert (r.status_code, _code(r)) == (404, "access.grant_not_found")

    assert admin_client.delete(f"/api/v1/gateway-users/{target}/capability-grants/{gid}").status_code == 200
    r = admin_client.delete(f"/api/v1/gateway-users/{target}/capability-grants/{gid}")
    assert (r.status_code, _code(r)) == (409, "access.grant_not_pending")


def test_the_compare_and_set_loses_when_the_row_already_changed(admin_client, target):
    from app.controllers.capability_grant_controller import CapabilityGrantController

    gid = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    m = CapabilityGrantController().grants
    assert m.close_live(gid, expected_status="active", new_status="revoked", decided_by=1)
    assert not m.close_live(gid, expected_status="active", new_status="revoked", decided_by=1)


def test_nobody_revokes_their_own_capabilities(admin_client):
    alice_id, alice = _admin_como(admin_client, "alice")
    _insert_cg(alice_id, "databases.write", "environment", env_id(DEV))
    gid = admin_client.get(f"/api/v1/gateway-users/{alice_id}/capability-grants").json()["data"][0]["id"]
    r = alice.delete(f"/api/v1/gateway-users/{alice_id}/capability-grants/{gid}")
    assert (r.status_code, _code(r)) == (409, "access.self_modification_forbidden")
    assert _row(gid)["status"] == "active"


def test_only_access_admin_may_revoke(admin_client, target):
    gid = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    _, oper = _admin_como(admin_client, "operadora", extra=())
    r = oper.delete(f"/api/v1/gateway-users/{target}/capability-grants/{gid}")
    assert (r.status_code, _code(r)) == (403, "access.forbidden")
    assert _row(gid)["status"] == "active"


# --------------------------------------------------------------------------- #
# Listado                                                                     #
# --------------------------------------------------------------------------- #


def test_list_returns_every_status_and_filters(admin_client, target):
    a = _grant(admin_client, target, "databases.write").json()["data"]["id"]
    b = _grant(admin_client, target, "exports.download").json()["data"]["id"]
    admin_client.delete(f"/api/v1/gateway-users/{target}/capability-grants/{a}")

    todos = admin_client.get(f"/api/v1/gateway-users/{target}/capability-grants").json()["data"]
    assert {(g["id"], g["status"]) for g in todos} == {(a, "revoked"), (b, "pending")}

    f = admin_client.get(f"/api/v1/gateway-users/{target}/capability-grants?status=pending")
    assert [g["id"] for g in f.json()["data"]] == [b]
    assert admin_client.get(
        f"/api/v1/gateway-users/{target}/capability-grants?status=nope"
    ).status_code == 422


def test_list_is_scoped_to_the_user_and_admin_only(admin_client, target):
    otro = _crear(admin_client, "otro")["id"]
    _grant(admin_client, target, "databases.write")
    assert admin_client.get(f"/api/v1/gateway-users/{otro}/capability-grants").json()["data"] == []

    _, oper = _admin_como(admin_client, "operadora", extra=())
    r = oper.get(f"/api/v1/gateway-users/{target}/capability-grants")
    assert (r.status_code, _code(r)) == (403, "access.forbidden")
    assert admin_client.get("/api/v1/gateway-users/9999/capability-grants").status_code == 404


# --------------------------------------------------------------------------- #
# R6: PUT /access no toca las capacidades puntuales                           #
# --------------------------------------------------------------------------- #


def test_put_access_keeps_capability_grants(admin_client, target):
    ids = [
        _grant(admin_client, target, "databases.write").json()["data"]["id"],
        _grant(admin_client, target, "blueprints.write", scope_id=env_id(PROD)).json()["data"]["id"],
    ]
    r = admin_client.put(
        f"/api/v1/gateway-users/{target}/access",
        json={"global_capabilities": [],
              "scope_grants": [{"scope_type": "environment", "scope_id": env_id(PROD),
                                "role": "viewer"}]},
    )
    assert r.status_code == 200, r.text
    assert [_row(i)["status"] for i in ids] == ["active", "active"]
    assert len(UserModel().find_access_context(target)["capability_grants"]) == 2


def test_audit_record_accepts_an_actor_type_override(admin_client):
    """D11: expiración y cancelación automáticas se atribuyen al ``system``, no a una persona."""
    from app.services import audit

    audit.record("capability_grant.expired", admin=None, actor_type="system", touched_engine=False)
    (a,) = _audits("capability_grant.expired")
    assert a.actor_type == "system"
    audit.record("capability_grant.x", admin={"id": 1, "username": "admin"})
    assert _audits("capability_grant.x")[0].actor_type == "admin"
