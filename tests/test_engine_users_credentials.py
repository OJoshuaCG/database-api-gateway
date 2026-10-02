"""
``engine_users.credentials``: elegir la contraseña de una cuenta del motor divulga.

Quien elige la credencial la conoce y entra al motor por fuera del gateway (sin export, consola,
step-up ni auditoría), así que la capacidad es ``discloses=True``: solo ``owner``, con step-up y
sensible si se otorga suelta. Lo que se mide:

- ``operator`` que manda una contraseña → 403 ``access.forbidden`` y el motor no se toca.
- ``operator`` sin contraseña (inventario, adopción, agregar host copiando el hash) → sigue.
- ``owner`` con la ventana fresca → pasa; con la ventana vencida → 403 ``access.step_up_required``.
- ``operator`` con la capacidad puntual APROBADA (es sensible) → pasa, y también pide step-up.
"""

import pytest

from app.services.capability_catalog import CODE_FORBIDDEN, CODE_STEP_UP_REQUIRED
from tests.test_api_engine_users import _FakeAdapter, _make_server, _patch
from tests.test_api_gateway_users import _cliente_como, _code, _crear
from tests.test_capability_grant_crud import _admin_como, _grant

LIVE = [("app", "%")]


def _inventory_row(admin_client, sid: int) -> int:
    """Una fila de inventario SIN contraseña: el alta que ``operator`` conserva."""
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "inv", "host": "%"}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _credential_requests(sid: int, uid: int) -> list[tuple[str, str, dict, int]]:
    """(método, url, body, status de éxito) de TODA operación donde el actor elige la contraseña."""
    base = f"/api/v1/servers/{sid}/users"
    return [
        ("POST", "/api/v1/server-users", {"server_id": sid, "username": "nuevo", "password": "x1"}, 201),
        ("POST", "/api/v1/server-users?provision=true",
         {"server_id": sid, "username": "prov", "password": "x1"}, 201),
        ("PATCH", f"/api/v1/server-users/{uid}", {"password": "x2"}, 200),
        ("POST", "/api/v1/server-users/provision",
         {"server_id": sid, "username": "full", "password": "x1"}, 201),
        ("POST", base, {"username": "eng", "password": "x1"}, 201),
        ("PATCH", f"{base}/password", {"username": "app", "new_password": "x3"}, 200),
        ("PATCH", f"{base}/password-all-hosts",
         {"username": "app", "new_password": "x3", "confirm_username": "app"}, 200),
        ("POST", f"{base}/define-password",
         {"username": "app", "known_password": "x4", "adopt_if_missing": True}, 200),
        ("POST", f"{base}/add-host",
         {"username": "app", "new_host": "10.0.0.9", "reuse_password": False,
          "new_password": "x5"}, 201),
        ("POST", f"{base}/adopt-all-hosts", {"username": "app", "known_password": "x6"}, 201),
    ]


_IDS = [
    "create-inventory", "create-provision", "patch-inventory", "provision", "create-engine",
    "password", "password-all-hosts", "define-password", "add-host-new", "adopt-all-known",
]


@pytest.fixture()
def setup(admin_client, monkeypatch):
    adapter = _FakeAdapter(LIVE)
    _patch(monkeypatch, adapter)
    sid = _make_server(admin_client)
    uid = _inventory_row(admin_client, sid)
    return adapter, sid, uid


@pytest.fixture()
def operator(admin_client):
    datos = _crear(admin_client, "opera", gateway_role="operator")
    return datos["id"], _cliente_como(datos, "opera")


def _send(client, method, url, body):
    return client.request(method, url, json=body)


@pytest.mark.parametrize("idx", range(10), ids=_IDS)
def test_operator_supplying_a_password_is_forbidden(setup, operator, idx):
    adapter, sid, uid = setup
    _, op = operator
    method, url, body, _ = _credential_requests(sid, uid)[idx]
    r = _send(op, method, url, body)
    assert r.status_code == 403, r.text
    assert _code(r) == CODE_FORBIDDEN
    assert "credentials" not in r.text
    assert adapter.calls == []


def test_operator_without_a_password_keeps_engine_users_write(setup, operator):
    adapter, sid, uid = setup
    _, op = operator
    base = f"/api/v1/servers/{sid}/users"

    r = op.post("/api/v1/server-users", json={"server_id": sid, "username": "solo_inv"})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["has_password"] is False

    r = op.patch(f"/api/v1/server-users/{uid}", json={"notes": "sin credencial"})
    assert r.status_code == 200, r.text

    r = op.post(f"{base}/adopt-all-hosts", json={"username": "app"})
    assert r.status_code == 201, r.text

    # Copiar el hash de la cuenta origen no le da al actor ninguna credencial.
    r = op.post(f"{base}/add-host", json={"username": "app", "new_host": "10.0.0.8"})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["password_mode"] == "reused"


@pytest.mark.parametrize("idx", range(10), ids=_IDS)
def test_owner_with_a_fresh_window_can_choose_the_password(admin_client, setup, idx):
    _, sid, uid = setup
    method, url, body, ok = _credential_requests(sid, uid)[idx]
    r = _send(admin_client, method, url, body)
    assert r.status_code == ok, r.text


@pytest.mark.parametrize("idx", range(10), ids=_IDS)
def test_owner_with_a_stale_window_needs_step_up(admin_client, setup, expire_step_up, idx):
    adapter, sid, uid = setup
    method, url, body, _ = _credential_requests(sid, uid)[idx]
    expire_step_up(admin_client)
    r = _send(admin_client, method, url, body)
    assert r.status_code == 403, r.text
    assert _code(r) == CODE_STEP_UP_REQUIRED
    assert adapter.calls == []


def test_owner_with_a_stale_window_is_not_prompted_without_a_password(
    admin_client, setup, expire_step_up
):
    """``engine_users.write`` no pide step-up: sin contraseña, nadie confirma la suya."""
    _, sid, uid = setup
    expire_step_up(admin_client)
    r = admin_client.post("/api/v1/server-users", json={"server_id": sid, "username": "sin_pw"})
    assert r.status_code == 201, r.text
    r = admin_client.patch(f"/api/v1/server-users/{uid}", json={"notes": "x"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("idx", range(10), ids=_IDS)
def test_operator_with_an_approved_grant_can_choose_the_password(
    admin_client, setup, operator, expire_step_up, idx
):
    """Sensible: nace ``pending`` y no tiene efecto hasta que OTRO ``access_admin`` la aprueba."""
    adapter, sid, uid = setup
    op_id, op = operator
    method, url, body, ok = _credential_requests(sid, uid)[idx]

    r = _grant(admin_client, op_id, "engine_users.credentials", scope_type="server", scope_id=sid)
    assert r.status_code == 201, r.text
    g = r.json()["data"]
    assert (g["status"], g["sensitive"]) == ("pending", True)
    assert g["implies"] == ["engine_users.read"]

    # Pendiente: sin efecto.
    assert _code(_send(op, method, url, body)) == CODE_FORBIDDEN

    _, aprobador = _admin_como(admin_client, "aprobador", role="owner")
    r = aprobador.post(f"/api/v1/capability-grants/{g['id']}/approve", json=None)
    assert r.status_code == 200, r.text

    # Con la ventana vencida, la capacidad puntual también pide step-up.
    expire_step_up(op)
    r = _send(op, method, url, body)
    assert (r.status_code, _code(r)) == (403, CODE_STEP_UP_REQUIRED), r.text
    assert adapter.calls == []

    r = op.post("/api/v1/auth/step-up", json={"password": "ContraseñaLarga123"})
    assert r.status_code == 200, r.text
    r = _send(op, method, url, body)
    assert r.status_code == ok, r.text
