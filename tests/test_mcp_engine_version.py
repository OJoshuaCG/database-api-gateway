"""
``database.engine_version`` de las tools MCP: la versión LIMPIA del motor, jamás la cadena cruda.

QUÉ SE VERIFICA
---------------
- ``public_engine_version`` (función pura): tabla de cadenas reales de MariaDB, MySQL y PostgreSQL.
- Cada tool que ya abre la sesión de lectura devuelve ``11.8.3`` cuando el façade falso entrega
  ``11.8.3-MariaDB-0+deb13u1 from Debian``, y el JSON serializado NO contiene ``deb13u1`` ni
  ``Debian`` en ningún lado.
- ``get_definition`` sin objetos presentes no lee la versión (cero lecturas al motor) y no filtra.
- El modelo de salida rechaza todo valor que no sea ``mayor.menor[.parche]``.

QUÉ NO SE VERIFICA: el ``VERSION()`` de un motor real (queda para staging).

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_mcp_engine_version``
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import json
import re

import pytest
from pydantic import ValidationError

from app.mcp import registry
from app.schemas import mcp as out
from app.services.db_admin.dtos import SchemaSnapshot
from app.services.db_admin.readonly_probe import public_engine_version
from tests.test_mcp_catalog_tools import (  # noqa: F401
    _dos_bases,
    _escenario,
    _FacadeFalso,
    _tabla,
    mcp_on,
    motor_falso,
)
from tests.test_mcp_get_definition import (
    _escenario as _escenario_definiciones,
)
from tests.test_mcp_get_definition import (
    _facade as _facade_definiciones,
)
from tests.test_mcp_get_definition import (
    _lectura_vista,
    _objetos,
)
from tests.test_mcp_get_definition import (
    _llamar as _llamar_definicion,
)
from tests.test_mcp_get_definition import (
    _ok as _ok_definicion,
)
from tests.test_mcp_server import _contenido, _rpc
from tests.test_mcp_table_stats import _lectura

RAW_MARIADB_DEBIAN = "11.8.3-MariaDB-0+deb13u1 from Debian"
CLEAN_VERSION = "11.8.3"
#: Lo que NO puede aparecer en ninguna respuesta: delata distribución y nivel de parche del paquete.
LEAK_MARKERS = ("deb13u1", "debian", "0+deb")
VERSION_SHAPE = re.compile(r"^\d+(\.\d+){1,2}$")


# --------------------------------------------------------------------------- #
# La función pura                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("11.8.3-MariaDB-0+deb13u1 from Debian", "11.8.3"),
        # Prefijo de compatibilidad de replicación de MariaDB: la versión real va DESPUÉS.
        ("5.5.5-10.11.6-MariaDB", "10.11.6"),
        ("10.6.12-MariaDB-1:10.6.12+maria~ubu2004", "10.6.12"),
        ("8.0.36", "8.0.36"),
        ("8.0.36-0ubuntu0.22.04.1", "8.0.36"),
        ("5.7.44-log", "5.7.44"),
        ("16.3", "16.3"),
        ("16.3 (Debian 16.3-1.pgdg120+1)", "16.3"),
        ("  8.0.36", "8.0.36"),
        (None, None),
        ("", None),
        ("   ", None),
        ("garbage", None),
        ("MariaDB", None),
        ("16", None),
        ("v8.0.36", None),
        ("1234567890.1.1", None),
    ],
)
def test_public_engine_version_table(raw, expected):
    assert public_engine_version(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        RAW_MARIADB_DEBIAN,
        "5.5.5-10.11.6-MariaDB",
        "16.3 (Debian 16.3-1.pgdg120+1)",
        "8.0.36-0ubuntu0.22.04.1",
        "8.0.36.99-extra",
    ],
)
def test_public_engine_version_output_is_always_digits_and_dots(raw):
    result = public_engine_version(raw)
    assert result is not None and VERSION_SHAPE.fullmatch(result)


# --------------------------------------------------------------------------- #
# El modelo de salida es la segunda barrera                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("valid", [None, "11.8", "11.8.3", "8.0.36"])
def test_database_ref_accepts_clean_versions_and_none(valid):
    ref = out.DatabaseRefOut(database_id=1, engine="mariadb", engine_version=valid)
    assert ref.engine_version == valid


@pytest.mark.parametrize(
    "leaky",
    [
        RAW_MARIADB_DEBIAN,
        "11.8.3-MariaDB",
        "11",
        "11.8.3.4",
        "11.8.",
        "v11.8",
        "",
        "11.8.3\n",
        "١١.٨",  # dígitos arábigo-índicos: ``\d`` Unicode los aceptaría.
    ],
)
def test_database_ref_rejects_anything_that_is_not_a_clean_version(leaky):
    with pytest.raises(ValidationError):
        out.DatabaseRefOut(database_id=1, engine="mariadb", engine_version=leaky)


def test_database_ref_defaults_to_no_version():
    assert out.DatabaseRefOut(database_id=1, engine="mysql").engine_version is None


# --------------------------------------------------------------------------- #
# Cada tool, por el despachador real, con un façade que entrega la cadena cruda #
# --------------------------------------------------------------------------- #
def _con_version_cruda(facade, raw=RAW_MARIADB_DEBIAN):
    """El façade falso responde la cadena CRUDA: la limpieza es de quien arma la respuesta."""
    facade.server_version = lambda: raw
    return facade


def _assert_clean(response_json: dict) -> dict:
    """La versión limpia está en el bloque ``database`` y nada de la cadena cruda sale."""
    serialized = json.dumps(response_json).lower()
    for marker in LEAK_MARKERS:
        assert marker not in serialized, marker
    structured = response_json["result"]["structuredContent"]
    assert structured["database"]["engine_version"] == CLEAN_VERSION
    return structured


def _call(client, token, name, arguments):
    return _rpc(client, token, "tools/call", {"name": name, "arguments": arguments})


def test_list_objects_returns_the_clean_version(client, admin_client, mcp_on, motor_falso):
    token, database_id = _escenario(admin_client)
    motor_falso.por_base["core_cliente1"] = _con_version_cruda(_FacadeFalso(motor_falso.registro))

    response = _call(client, token, "list_objects", {"database_id": database_id})

    _assert_clean(response.json())


def test_get_schema_returns_the_clean_version(client, admin_client, mcp_on, motor_falso):
    token, database_id = _escenario(admin_client)
    motor_falso.por_base["core_cliente1"] = _con_version_cruda(_FacadeFalso(motor_falso.registro))

    response = _call(
        client,
        token,
        "get_schema",
        {"database_id": database_id, "objects": [{"kind": "table", "name": "clientes"}]},
    )

    _assert_clean(response.json())


def test_search_schema_returns_the_clean_version(client, admin_client, mcp_on, motor_falso):
    token, database_id = _escenario(admin_client)
    motor_falso.por_base["core_cliente1"] = _con_version_cruda(_FacadeFalso(motor_falso.registro))

    response = _call(client, token, "search_schema", {"database_id": database_id, "query": "cli"})

    _assert_clean(response.json())


def test_check_freshness_returns_the_clean_version(client, admin_client, mcp_on, motor_falso):
    token, database_id = _escenario(admin_client)
    motor_falso.por_base["core_cliente1"] = _con_version_cruda(_FacadeFalso(motor_falso.registro))

    response = _call(client, token, "check_freshness", {"database_id": database_id})

    _assert_clean(response.json())


def test_get_table_stats_returns_the_clean_version(client, admin_client, mcp_on, motor_falso):
    token, database_id = _escenario(admin_client)
    facade = _con_version_cruda(_FacadeFalso(motor_falso.registro))
    facade.consistent_structure = True
    facade.table_stats = lambda tables, *, include_row_estimates: [
        _lectura(name) for name in tables
    ]
    motor_falso.por_base["core_cliente1"] = facade

    response = _call(
        client, token, "get_table_stats", {"database_id": database_id, "tables": ["clientes"]}
    )

    _assert_clean(response.json())


def test_diff_schemas_returns_the_version_of_the_source_side(
    client, admin_client, mcp_on, motor_falso
):
    token, source_id, target_id = _dos_bases(admin_client)
    for name, raw in (
        ("core_cliente1", RAW_MARIADB_DEBIAN),
        ("core_cliente2", "5.7.44-log-OTHER-SIDE-SECRET"),
    ):
        motor_falso.por_base[name] = _con_version_cruda(
            _FacadeFalso(
                motor_falso.registro,
                snapshot=SchemaSnapshot(database=name, source_engine="mysql", tables=[_tabla()]),
            ),
            raw,
        )

    response = _call(
        client,
        token,
        "diff_schemas",
        {"source_database_id": source_id, "target_database_id": target_id},
    )

    structured = _assert_clean(response.json())
    assert "other-side-secret" not in json.dumps(structured).lower()


def test_get_definition_returns_the_clean_version(admin_client, monkeypatch, motor_falso):
    tools = registry._build(definitions_enabled=True)
    monkeypatch.setattr(registry, "TOOLS", tools)
    for tool in tools:
        monkeypatch.setitem(registry.BY_NAME, tool.name, tool)
    actor, database_id = _escenario_definiciones(admin_client, monkeypatch)
    _facade_definiciones(
        motor_falso,
        definiciones={("view", "v_activos"): [_lectura_vista()]},
        version_motor=RAW_MARIADB_DEBIAN,
    )

    body = _llamar_definicion(
        actor, {"database_id": database_id, "objects": _objetos(("view", "v_activos"))}
    )

    structured = _ok_definicion(body)
    serialized = json.dumps(body).lower()
    for marker in LEAK_MARKERS:
        assert marker not in serialized, marker
    assert structured["database"]["engine_version"] == CLEAN_VERSION


def test_get_definition_without_present_objects_reads_no_version_and_leaks_nothing(
    admin_client, monkeypatch, motor_falso
):
    tools = registry._build(definitions_enabled=True)
    monkeypatch.setattr(registry, "TOOLS", tools)
    for tool in tools:
        monkeypatch.setitem(registry.BY_NAME, tool.name, tool)
    actor, database_id = _escenario_definiciones(admin_client, monkeypatch)
    facade = _facade_definiciones(motor_falso, version_motor=RAW_MARIADB_DEBIAN)

    body = _llamar_definicion(
        actor, {"database_id": database_id, "objects": _objetos(("view", "no_existe"))}
    )

    structured = _ok_definicion(body)
    assert facade.llamadas_version == 0, "sin nada que leer no hay lectura de versión"
    assert structured["database"]["engine_version"] is None
    serialized = json.dumps(body).lower()
    for marker in LEAK_MARKERS:
        assert marker not in serialized, marker


def test_a_tool_without_an_engine_connection_has_no_version_and_no_leak(
    client, admin_client, mcp_on, motor_falso
):
    token, _database_id = _escenario(admin_client)

    response = _call(client, token, "list_environments", {})

    assert motor_falso.abiertas == [], "list_environments no abre ninguna sesión de lectura"
    serialized = json.dumps(response.json()).lower()
    assert "engine_version" not in serialized
    for marker in LEAK_MARKERS:
        assert marker not in serialized, marker


def test_an_unreadable_version_comes_back_as_null_not_as_the_raw_text(
    client, admin_client, mcp_on, motor_falso
):
    token, database_id = _escenario(admin_client)
    motor_falso.por_base["core_cliente1"] = _con_version_cruda(
        _FacadeFalso(motor_falso.registro), "build-from-Debian-deb13u1"
    )

    response = _call(client, token, "check_freshness", {"database_id": database_id})

    structured = _contenido(response)
    assert structured["database"]["engine_version"] is None
    serialized = json.dumps(response.json()).lower()
    for marker in LEAK_MARKERS:
        assert marker not in serialized, marker


class _FacadeQueFallaAlLeerLaVersion:
    """Un façade cuya lectura de ``VERSION()`` falla con un mensaje que no debe salir a ningún lado."""

    def server_version(self):
        raise RuntimeError("fallo del motor en host-interno-db01 con usuario mcp_ro")


def test_a_failing_version_read_returns_none_and_never_logs_the_engine_message(monkeypatch):
    from app.controllers import target_resolution

    registros = []
    monkeypatch.setattr(
        target_resolution.logger,
        "warning",
        lambda mensaje, *args: registros.append(mensaje % args),
    )

    resultado = target_resolution.read_engine_version(_FacadeQueFallaAlLeerLaVersion())

    assert resultado is None
    assert len(registros) == 1
    assert "RuntimeError" in registros[0]
    # El mensaje del motor puede traer host, usuario o sentencia: solo se registra el TIPO.
    assert "host-interno-db01" not in registros[0]
    assert "mcp_ro" not in registros[0]
