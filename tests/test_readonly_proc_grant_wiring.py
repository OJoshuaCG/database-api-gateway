"""
S6 (mcp-schema-definitions): la columna real ``servers.readonly_proc_grant`` llega a ``list_objects``.

Antes de S6 ``resolve_agent_database`` leía la bandera con ``getattr(..., False)`` y la razón
``flag_off`` era constante. Ahora sale de la fila: con la bandera encendida, las rutinas de
MariaDB < 11.3 / MySQL 5.7 dejan de reportar ``flag_off``; apagada, siguen reportándolo.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_readonly_proc_grant_wiring``
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import pytest

from app.core.database import Database
from app.models.server import Server
from tests.test_mcp_catalog_tools import _server_de, mcp_on, motor_falso  # noqa: F401
from tests.test_mcp_get_definition import _escenario, _facade
from tests.test_mcp_list_objects_definitions import _INDICE_CON_TODO, _ok, _por_nombre


def _fijar_bandera(server_id: int, valor: bool) -> None:
    session = Database().get_declarative_base_session()
    try:
        session.get(Server, server_id).readonly_proc_grant = valor
        session.commit()
    finally:
        session.close()


@pytest.mark.parametrize("version", ["5.7.44", "10.6.12-MariaDB"])
def test_flag_off_follows_the_server_column(admin_client, monkeypatch, motor_falso, version):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor=version)

    apagada = _por_nombre(_ok(actor, database_id))["calcular"]
    assert apagada["unavailable_reason"] == "flag_off"
    assert apagada["body_available"] is False

    _fijar_bandera(_server_de(database_id), True)
    encendida = _por_nombre(_ok(actor, database_id))["calcular"]
    assert encendida["unavailable_reason"] is None
    assert encendida["body_available"] is True

    _fijar_bandera(_server_de(database_id), False)
    assert _por_nombre(_ok(actor, database_id))["calcular"]["unavailable_reason"] == "flag_off"


def test_the_flag_does_not_change_engines_that_never_had_a_flag_off(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor="8.0.19")
    _fijar_bandera(_server_de(database_id), True)
    # MySQL 8.0.0 a 8.0.19: ninguna bandera lo arregla.
    assert (
        _por_nombre(_ok(actor, database_id))["calcular"]["unavailable_reason"]
        == "engine_unsupported"
    )
