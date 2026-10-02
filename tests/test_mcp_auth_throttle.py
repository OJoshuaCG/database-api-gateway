"""
Los rechazos del MCP: tope por IP y auditoría agregada.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
El límite de tasa del MCP es por ``token_id``, leído del header ANTES de verificar el HMAC. Eso
acota a quien tiene un token real, y a nadie más: un ``token_id`` inventado distinto por request
estrena un cupo completo cada vez. Y cada rechazo pagaba una lectura a la BD de metadatos, un HMAC
y **una fila en ``audit_log``** — escritura gratis e ilimitada para cualquiera sin credencial.

Lo que se fija acá: los rechazos consumen un cupo POR IP que, agotado, corta con 429 antes de la
BD; la auditoría escribe como mucho una fila por IP por ventana, con la cuenta de lo agregado; y
los requests autenticados no gastan ese cupo.
"""

import secrets

import pytest

from app.core.database import Database


@pytest.fixture()
def mcp_on(monkeypatch):
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", True)


@pytest.fixture()
def tope(monkeypatch):
    """Fija el tope de rechazos por IP; se importa por nombre, así que se parchea en `mcp_auth`."""
    import app.core.mcp_auth as auth_mod

    def _fijar(valor: str):
        monkeypatch.setattr(auth_mod, "MCP_AUTH_FAILURE_RATE_LIMIT", valor)

    return _fijar


def _rpc(client, bearer):
    return client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"Authorization": f"Bearer {bearer}"},
    )


def _basura() -> str:
    """Un bearer bien formado con un ``token_id`` NUEVO cada vez: el ataque que se cierra."""
    return f"dbgw.{secrets.token_urlsafe(18)}.{secrets.token_urlsafe(32)}"


def _filas_de_rechazo() -> list[str]:
    from app.models.audit_log import AuditLog

    s = Database().get_declarative_base_session()
    try:
        filas = (
            s.query(AuditLog)
            .filter(AuditLog.action == "mcp.auth", AuditLog.status == "failure")
            .order_by(AuditLog.id)
            .all()
        )
        return [f.detail or "" for f in filas]
    finally:
        s.close()


def _token_valido(admin_client) -> str:
    pid = admin_client.post("/api/v1/projects", json={"name": "Throttle"}).json()["data"]["id"]
    r = admin_client.post("/api/v1/api-tokens", json={"name": "agente", "project_id": pid})
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def test_rotating_invented_token_ids_hits_the_per_ip_cap(client, mcp_on, tope):
    """
    **El test que fija F-28.** Cada request trae un ``token_id`` distinto, así que el límite por
    token no los frena; el tope por IP sí, y a partir de ahí es 429.
    """
    tope("3/minute")
    codigos = [_rpc(client, _basura()).status_code for _ in range(6)]
    # Tres rechazos agotan el cupo; los siguientes son 429 y ni llegan a la BD.
    assert codigos == [401, 401, 401, 429, 429, 429], codigos


def test_rejections_are_audited_once_per_ip_per_window(client, mcp_on, tope):
    """Diez rechazos en la misma ventana: UNA fila, no diez."""
    tope("1000/minute")
    for _ in range(10):
        assert _rpc(client, _basura()).status_code == 401

    filas = _filas_de_rechazo()
    assert len(filas) == 1, filas
    assert "agregados=0" in filas[0]
    assert "ip=" in filas[0]


def test_the_next_window_row_carries_the_aggregated_count(client, mcp_on, tope, monkeypatch):
    """
    La fila que abre la ventana siguiente declara cuántos rechazos quedaron sin fila en la
    anterior — incluidos los cortados con 429. Sin esa cuenta, agregar sería esconder el volumen.
    """
    import app.core.mcp_auth as auth_mod

    tope("5/minute")
    for _ in range(8):  # 5 rechazos con 401 + 3 cortados con 429
        _rpc(client, _basura())
    assert len(_filas_de_rechazo()) == 1

    reloj = auth_mod.monotonic() + auth_mod._AUDIT_WINDOW_SECONDS + 1
    monkeypatch.setattr(auth_mod, "monotonic", lambda: reloj)
    _rpc(client, _basura())

    filas = _filas_de_rechazo()
    assert len(filas) == 2, filas
    assert "agregados=7" in filas[1], filas[1]


def test_authenticated_requests_do_not_spend_the_rejection_quota(client, admin_client, mcp_on, tope):
    """
    El tope es de RECHAZOS. Un agente legítimo que hace muchas llamadas sigue con su límite por
    token y nada más.
    """
    tope("2/minute")
    token = _token_valido(admin_client)
    assert [_rpc(client, token).status_code for _ in range(5)] == [200] * 5
    assert _filas_de_rechazo() == []


def test_the_kill_switch_rejection_is_aggregated_too(client):
    """Con el servidor apagado, una fila por request seguía siendo escritura gratis."""
    for _ in range(5):
        assert _rpc(client, _basura()).status_code == 503
    filas = _filas_de_rechazo()
    assert len(filas) == 1 and "rechazo=deshabilitado" in filas[0], filas


def test_the_aggregator_memory_is_bounded():
    """Un bloque IPv6 rotando no puede hacer crecer el agregador sin cota."""
    from app.core.audit_aggregator import WindowedAggregator

    agg = WindowedAggregator(window=60, max_keys=3)
    for i in range(10):
        assert agg.admit(f"2001:db8::{i}") == 0
    assert len(agg) == 3
