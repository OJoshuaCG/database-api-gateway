"""
``GET /authz/scope-readiness`` y la limitación F-17.

La resolución a nivel servidor mira solo las BDs inventariadas; el reporte lo declara con UN campo
booleano de primer nivel, siempre true, y sigue sin abrir una sola conexión al motor.
"""

import app.core.remote_engine as remote_engine
import app.services.db_admin.factory as factory
from tests.scope_helpers import env_id, sembrar_bd

_URL = "/api/v1/authz/scope-readiness"


def test_readiness_flags_inventory_only_resolution(admin_client):
    r = admin_client.get(_URL)
    assert r.status_code == 200, r.text
    datos = r.json()["data"]
    assert datos["server_resolution_inventory_only"] is True


def test_the_flag_is_always_true_regardless_of_the_inventory(admin_client):
    sembrar_bd(environment_id=env_id("development"))
    antes = admin_client.get(_URL).json()["data"]
    sembrar_bd(environment_id=None, name="sin_entorno")
    despues = admin_client.get(_URL).json()["data"]
    assert antes["ready"] is True
    assert despues["ready"] is False
    assert antes["server_resolution_inventory_only"] is True
    assert despues["server_resolution_inventory_only"] is True


def test_readiness_opens_zero_engine_connections(admin_client, monkeypatch):
    """Listar el motor al autorizar está descartado (decisión R2-1): el reporte no conecta."""

    def _prohibido(*args, **kwargs):
        raise AssertionError("scope-readiness no debe abrir conexiones al motor")

    monkeypatch.setattr(remote_engine, "get_engine", _prohibido)
    monkeypatch.setattr(factory, "get_adapter", _prohibido)
    sembrar_bd(environment_id=env_id("production"))
    r = admin_client.get(_URL)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["server_resolution_inventory_only"] is True


def test_the_flag_is_the_only_new_top_level_field(admin_client):
    datos = admin_client.get(_URL).json()["data"]
    assert set(datos) == {
        "total_databases",
        "unclassified_databases",
        "ready",
        "fallback_environment_slug",
        "servers",
        "server_resolution_inventory_only",
    }
