"""
Tests del guard anti-SSRF (app/core/net_guard.py).

El guard está DESACTIVADO en el fixture general (conftest) porque los tests usan
127.0.0.1 como dummy; aquí lo activamos explícitamente para probar su lógica.
"""

import ipaddress

import pytest

from app.core import environments, net_guard
from app.exceptions import AppHttpException


@pytest.fixture()
def guard_on(monkeypatch):
    monkeypatch.setattr(environments, "REMOTE_SSRF_GUARD_ENABLED", True)
    monkeypatch.setattr(environments, "REMOTE_ALLOWED_CIDRS", [])


def _blocked(host) -> int:
    with pytest.raises(AppHttpException) as exc:
        net_guard.validate_remote_host(host)
    return exc.value.status_code


# ------------------------------- denylist ------------------------------------ #
@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1", "localhost"],
)
def test_dangerous_hosts_blocked(guard_on, host):
    assert _blocked(host) == 422


def test_private_and_public_allowed_by_default(guard_on):
    # Sin allowlist, los privados (BD internas) y públicos pasan.
    net_guard.validate_remote_host("10.0.0.5")
    net_guard.validate_remote_host("192.168.1.20")
    net_guard.validate_remote_host("8.8.8.8")


# ------------------------------- allowlist ----------------------------------- #
def test_allowlist_enforced(monkeypatch):
    monkeypatch.setattr(environments, "REMOTE_SSRF_GUARD_ENABLED", True)
    monkeypatch.setattr(
        environments, "REMOTE_ALLOWED_CIDRS", [ipaddress.ip_network("10.0.0.0/8")]
    )
    net_guard.validate_remote_host("10.1.2.3")  # dentro → OK
    assert _blocked("8.8.8.8") == 422            # fuera → 422


# ------------------------------- disabled ------------------------------------ #
def test_guard_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(environments, "REMOTE_SSRF_GUARD_ENABLED", False)
    # No lanza aunque sea loopback.
    net_guard.validate_remote_host("127.0.0.1")


# ------------------------------- API ----------------------------------------- #
def test_create_server_blocks_loopback_via_api(admin_client, server_payload, monkeypatch):
    monkeypatch.setattr(environments, "REMOTE_SSRF_GUARD_ENABLED", True)
    # server_payload usa host=127.0.0.1 (loopback) → debe rechazarse.
    r = admin_client.post("/api/v1/servers", json=server_payload(port=3600))
    assert r.status_code == 422


# --------------------- R2: revalidación al CONECTAR --------------------------- #
def test_server_connection_blocks_dangerous_host(guard_on):
    """La conexión a nivel servidor revalida el host y bloquea ANTES de conectar."""
    from app.core.remote_engine import ServerTarget, server_connection

    t = ServerTarget(
        server_id=1, dialect="mysql", host="169.254.169.254", port=3306,
        admin_user="u", admin_password="p",
    )
    with pytest.raises(AppHttpException) as exc:
        with server_connection(t):
            pass
    assert exc.value.status_code == 422


def test_connect_time_revalidation_blocks_rebinding_via_api(
    admin_client, server_payload, monkeypatch
):
    """
    Anti-rebinding: registrar con el guard OFF un host que LUEGO es peligroso, activar el
    guard, y comprobar que la operación contra el motor (test-connection) lo bloquea al
    conectar — no solo en el registro.
    """
    # Registro con guard desactivado (conftest) → simula que en ese momento resolvía OK.
    r = admin_client.post(
        "/api/v1/servers", json=server_payload(host="169.254.169.254", port=3698)
    )
    assert r.status_code == 201, r.text
    sid = r.json()["data"]["id"]

    # Ahora el guard está activo: la conexión debe revalidar y rechazar (422).
    monkeypatch.setattr(environments, "REMOTE_SSRF_GUARD_ENABLED", True)
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection")
    assert r.status_code == 422, r.text
