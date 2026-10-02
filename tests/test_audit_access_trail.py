"""
El rastro de autorización: cambios de acceso reconstruibles, agentes bien atribuidos y
denegaciones que dejan huella.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Tres huecos del mismo registro, y en los tres el log "funcionaba" —había fila— pero no
respondía la pregunta del incidente:

- **F-29.** ``gateway_user.update`` guardaba solo los NOMBRES de los campos y
  ``gateway_user.access_set`` las globales DESPUÉS y la CANTIDAD de alcances. "¿Quién le dio
  ``owner`` en producción, y qué tenía antes?" no tenía respuesta en ninguna tabla.
- **F-31.** Las filas de un token iban con el PK del token en ``admin_id`` (mismo espacio que los
  ids de usuario) y su ``name`` libre en ``admin_username``; ``mcp.auth`` iba con ``admin=None`` y
  leía como un admin anónimo.
- **F-30.** Los 403 de capacidad, alcance y CSRF no dejaban fila: un sondeo era invisible.
"""

import json

import pytest

from app.core.database import Database
from app.models.audit_log import AuditLog
from app.models.user_model import UserModel


def _rows(action: str) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        return s.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
    finally:
        s.close()


def _crear(admin_client, username, **extra):
    r = admin_client.post("/api/v1/gateway-users", json={"username": username, **extra})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _servidor(admin_client, server_payload, name="srv-trail") -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(name=name))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _cliente_como(datos: dict, username: str, password: str = "ContraseñaLarga123"):
    """Un client APARTE autenticado como otra identidad (``admin_client`` es ``client``)."""
    from fastapi.testclient import TestClient

    from main import app
    from tests.csrf_helpers import attach_csrf

    otra = TestClient(app)
    r = otra.post(
        "/api/v1/gateway-users/invite/accept",
        json={"token": datos["invite_token"], "password": password},
    )
    assert r.status_code == 200, r.text
    r = otra.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    attach_csrf(otra)
    return otra


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


# --------------------------------------------------------------------------- #
# F-29 — antes/después reconstruible                                          #
# --------------------------------------------------------------------------- #


def test_access_set_records_the_full_before_and_after(admin_client, server_payload):
    uid = _crear(admin_client, "reconstruible")["id"]
    sid = _servidor(admin_client, server_payload)

    r = admin_client.put(
        f"/api/v1/gateway-users/{uid}/access",
        json={
            "global_capabilities": [],
            "scope_grants": [
                {"scope_type": "environment", "scope_id": 1, "role": "operator"},
                {"scope_type": "server", "scope_id": sid, "role": "viewer"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    r = admin_client.put(
        f"/api/v1/gateway-users/{uid}/access",
        json={
            "global_capabilities": ["security_officer"],
            "scope_grants": [{"scope_type": "environment", "scope_id": 1, "role": "owner"}],
        },
    )
    assert r.status_code == 200, r.text

    fila = _rows("gateway_user.access_set")[-1]
    d = json.loads(fila.detail)
    assert d["username"] == "reconstruible"
    assert d["before"]["global_capabilities"] == []
    assert d["before"]["scope_grants"] == [
        {"scope_type": "environment", "scope_id": 1, "role": "operator"},
        {"scope_type": "server", "scope_id": sid, "role": "viewer"},
    ]
    assert d["after"]["global_capabilities"] == ["security_officer"]
    assert d["after"]["scope_grants"] == [
        {"scope_type": "environment", "scope_id": 1, "role": "owner"}
    ]
    assert d["after"]["scope_grants_total"] == 1
    assert d["before"]["gateway_role"] == d["after"]["gateway_role"] == "viewer"
    assert d["before"]["is_active"] is True


def test_update_records_role_and_active_before_and_after(admin_client):
    uid = _crear(admin_client, "degradable", gateway_role="operator")["id"]

    r = admin_client.patch(
        f"/api/v1/gateway-users/{uid}",
        json={"gateway_role": "viewer", "is_active": False, "notes": "dato personal"},
    )
    assert r.status_code == 200, r.text

    d = json.loads(_rows("gateway_user.update")[-1].detail)
    assert d["changed"] == ["gateway_role", "is_active", "notes"]
    assert (d["before"]["gateway_role"], d["after"]["gateway_role"]) == ("operator", "viewer")
    # La desactivación también queda con su antes/después.
    assert (d["before"]["is_active"], d["after"]["is_active"]) == (True, False)
    assert "scope_grants" in d["before"] and "scope_grants" in d["after"]
    # Los valores de contacto no se copian: se nombran.
    assert "dato personal" not in _rows("gateway_user.update")[-1].detail


def test_the_snapshot_is_bounded(admin_client, monkeypatch):
    """Una persona con miles de alcances no puede hacer crecer una fila de auditoría sin cota."""
    import app.controllers.gateway_user_controller as guc

    monkeypatch.setattr(guc, "AUDIT_MAX_GRANTS", 2)
    ctrl = guc.GatewayUserController()
    ctx = {
        "grants": [("server", i, "viewer") for i in range(5)],
        "globals": [],
    }
    snap = ctrl._access_snapshot({"id": 1, "gateway_role": "viewer", "is_active": 1}, ctx)
    assert len(snap["scope_grants"]) == 2
    assert snap["scope_grants_total"] == 5
    assert snap["scope_grants_truncated"] is True


# --------------------------------------------------------------------------- #
# F-31 — atribución de agentes                                                #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def mcp_on(monkeypatch):
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", True)


def _token(admin_client) -> dict:
    pid = admin_client.post("/api/v1/projects", json={"name": "Trail"}).json()["data"]["id"]
    # Un nombre que IMITA a un usuario real: es el caso que la atribución vieja confundía.
    r = admin_client.post("/api/v1/api-tokens", json={"name": "admin", "project_id": pid})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _rpc(client, bearer, method="initialize", params=None):
    cuerpo = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        cuerpo["params"] = params
    return client.post("/mcp/", json=cuerpo, headers={"Authorization": f"Bearer {bearer}"})


def test_token_rows_never_use_the_user_id_namespace(client, admin_client, mcp_on):
    datos = _token(admin_client)
    assert _rpc(client, datos["token"]).status_code == 200
    _rpc(client, datos["token"], "tools/call", {"name": "list_databases", "arguments": {}})

    for action in ("mcp.auth", "mcp.list_databases"):
        fila = [f for f in _rows(action) if f.status == "success"][-1]
        assert fila.actor_type == "api_token", action
        assert fila.api_token_id == datos["id"], action
        assert fila.admin_id is None, f"{action}: el PK del token cayó en admin_id"
        assert fila.admin_username == f"token:{datos['token_id']}", action


def test_a_failed_agent_authentication_is_anonymous(client, mcp_on):
    assert _rpc(client, "dbgw.noexiste.secreto").status_code == 401
    fila = [f for f in _rows("mcp.auth") if f.status == "failure"][-1]
    assert fila.actor_type == "anonymous"
    assert fila.admin_id is None and fila.api_token_id is None


# --------------------------------------------------------------------------- #
# F-30 — las denegaciones dejan rastro, agregado                              #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def viewer(admin_client):
    datos = _crear(admin_client, "mirona")
    return _cliente_como(datos, "mirona")


def test_a_viewer_hitting_a_drop_route_leaves_one_opaque_denial(viewer):
    r = viewer.delete("/api/v1/managed-databases/999?drop_remote=true&confirm_name=x")
    assert r.status_code == 403
    assert _code(r) == "access.forbidden"
    # La capacidad que faltó NO viaja en la respuesta.
    assert "databases." not in r.text

    filas = _rows("access.denied")
    assert len(filas) == 1, [f.detail for f in filas]
    fila = filas[0]
    uid = UserModel().find_by_username("mirona")["id"]
    assert fila.status == "failure"
    assert (fila.admin_id, fila.admin_username, fila.actor_type) == (uid, "mirona", "admin")
    d = json.loads(fila.detail)
    assert d["code"] == "access.forbidden"
    assert d["check"] == "capability"
    assert d["capability"] == "databases.write"
    assert d["method"] == "DELETE"
    assert d["route"] == "/api/v1/managed-databases/{id}"
    assert d["path"] == "/api/v1/managed-databases/999"
    assert d["aggregated"] == 0


def test_repeated_denials_are_aggregated_per_window(viewer, monkeypatch):
    import app.core.denial_audit as da

    # Ids distintos: la ruta normalizada es la misma clave, así que no estrenan fila.
    for i in range(50):
        assert viewer.delete(f"/api/v1/managed-databases/{1000 + i}").status_code == 403
    assert len(_rows("access.denied")) == 1

    # La ventana siguiente abre con UNA fila que declara las 49 que quedaron sin fila.
    reloj = da._denials._clock() + da.DENIAL_AUDIT_WINDOW_SECONDS + 1
    monkeypatch.setattr(da._denials, "_clock", lambda: reloj)
    assert viewer.delete("/api/v1/managed-databases/1").status_code == 403

    filas = _rows("access.denied")
    assert len(filas) == 2
    assert json.loads(filas[1].detail)["aggregated"] == 49


def test_a_csrf_rejection_leaves_a_row(admin_client):
    from app.core.csrf import CSRF_HEADER

    token = admin_client.headers.pop(CSRF_HEADER)
    try:
        r = admin_client.post("/api/v1/projects", json={"name": "SinToken"})
    finally:
        admin_client.headers[CSRF_HEADER] = token
    assert r.status_code == 403 and _code(r) == "auth.csrf_missing"

    r = admin_client.post(
        "/api/v1/projects", json={"name": "Ajeno"}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403 and _code(r) == "auth.origin_rejected"

    detalles = [json.loads(f.detail) for f in _rows("access.denied")]
    codigos = {d["code"]: d for d in detalles}
    assert codigos["auth.csrf_missing"]["check"] == "csrf"
    assert codigos["auth.origin_rejected"]["origin"] == "https://evil.example"
    assert all(f.admin_username == "admin" for f in _rows("access.denied"))


def test_a_scope_denial_leaves_a_row(client):
    from app.core.scope import assert_scope
    from app.exceptions import AppHttpException
    from app.services.capability_catalog import Capability, GatewayRole
    from tests.scope_helpers import actor_con

    with pytest.raises(AppHttpException) as exc:
        assert_scope(
            actor_con(GatewayRole.VIEWER),
            Capability.DATABASES_DROP,
            server_id=None,
            managed_database_id=None,
        )
    assert exc.value.status_code == 403
    d = json.loads(_rows("access.denied")[-1].detail)
    assert (d["check"], d["capability"]) == ("scope", "databases.drop")


def test_an_audit_failure_does_not_change_the_403(viewer, monkeypatch):
    from app.services import audit

    def _rompe(*a, **k):
        raise RuntimeError("audit_log caído")

    monkeypatch.setattr(audit, "record", _rompe)
    r = viewer.delete("/api/v1/managed-databases/999?drop_remote=true&confirm_name=x")
    assert r.status_code == 403
    assert _code(r) == "access.forbidden"


def test_token_denials_are_left_to_the_mcp_dispatch(client):
    """El dispatch ya audita la denegación de la tool; acá se duplicaría."""
    from app.core.actor import token_actor
    from app.core.authz import assert_capability
    from app.exceptions import AppHttpException
    from app.services.capability_catalog import Capability

    actor = token_actor(token_pk=1, token_id="t", name="n", scopes="", project_id=1)
    with pytest.raises(AppHttpException):
        assert_capability(actor, Capability.DATABASES_DROP)
    assert _rows("access.denied") == []


def test_route_normalization():
    from app.core.denial_audit import normalize_route

    assert normalize_route("/api/v1/servers/3/users/12") == "/api/v1/servers/{id}/users/{id}"
    assert normalize_route("/api/v1/managed-databases") == "/api/v1/managed-databases"
    assert normalize_route(None) is None
