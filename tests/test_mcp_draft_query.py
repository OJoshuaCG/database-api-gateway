"""
``draft_query``: clasifica el SQL de un agente SIN abrir nunca una conexión al motor (S1-S4, S33).

QUÉ SE VERIFICA ACÁ
-------------------
- El sobre exacto ``{classification, reasons, warnings, query_text, touches_engine}`` para TODO
  input, también el basura, el vacío y el enorme (S1-S4 y los bordes).
- **Cero conexiones**: ``remote_engine.database_connection``/``get_engine``/``server_connection``
  explotan si alguien las toca, y el façade de lectura del catálogo también se reemplaza por uno
  que registra aperturas. Un ``draft_query`` que conectara —aunque fuera para una lectura— rompe
  el invariante "el MCP nunca ejecuta SQL del agente".
- El gate de la base sigue valiendo (otro proyecto, sin credencial, sin scope) y el motor y la base
  fijada salen de la fila de inventario, nunca del agente.
- S33: el camino de la tool no importa ningún módulo de la capa de motor (AST, además del guard).

El motor se reemplaza igual que en ``tests/test_mcp_catalog_tools.py``: el gate corre de verdad.
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import ast
import inspect
import json
import pathlib

import pytest

from tests.test_mcp_catalog_tools import (  # noqa: F401 — fixtures y helpers del arnés del MCP
    _credencial_ro,
    _error,
    _escenario,
    _server_de,
    _token,
    mcp_on,
    motor_falso,
)
from tests.test_mcp_server import _bd_alcanzable, _contenido, _proyecto, _rpc

_ENVELOPE_KEYS = {"classification", "reasons", "warnings", "query_text", "touches_engine"}
_RAIZ = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def sin_motor(monkeypatch):
    """
    Cualquier conexión a un motor falla el test y queda registrada. ``llamadas`` tiene que
    terminar vacía: es la aserción de "cero conexiones" (S1-S4).

    Se ARMA con ``armar()`` DESPUÉS de sembrar el escenario: sembrar un servidor y una credencial
    pasa por la API de administración, que no es lo que se está midiendo.
    """
    import app.core.remote_engine as remote_engine

    estado = type("Estado", (), {})()
    estado.llamadas = []

    def _explota(*args, **kwargs):
        estado.llamadas.append(args)
        raise AssertionError("draft_query abrió una conexión al motor")

    def armar():
        for nombre in (
            "database_connection",
            "server_connection",
            "get_engine",
            "pooled_source_scope",
        ):
            monkeypatch.setattr(remote_engine, nombre, _explota)

    estado.armar = armar
    return estado


def _draft(client, token, database_id, sql):
    return _rpc(
        client,
        token,
        "tools/call",
        {"name": "draft_query", "arguments": {"database_id": database_id, "sql": sql}},
    )


def _sobre(client, token, database_id, sql) -> dict:
    resp = _draft(client, token, database_id, sql)
    assert resp.json()["result"]["isError"] is False, resp.text
    return _contenido(resp)


# --------------------------------------------------------------------------- #
# S1-S4                                                                        #
# --------------------------------------------------------------------------- #
def test_an_update_is_drafted_as_write_with_a_warning_and_no_engine_contact(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    """S1."""
    token, db_id = _escenario(admin_client)
    sin_motor.armar()
    sql = "UPDATE t SET a=1"

    sobre = _sobre(client, token, db_id, sql)

    assert sobre["classification"] == "write"
    assert sobre["query_text"] == sql
    assert sobre["warnings"] == ["WRITE_NOT_EXECUTED"]
    assert sobre["reasons"] == ["NOT_SELECT"]
    assert sobre["touches_engine"] is False
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_a_drop_is_drafted_as_ddl_with_the_same_envelope(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    """S2."""
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, "DROP TABLE t")

    assert sobre["classification"] == "ddl"
    assert sobre["query_text"] == "DROP TABLE t"
    assert sobre["warnings"] == ["DDL_NOT_EXECUTED"]
    assert sobre["touches_engine"] is False
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_an_unparseable_text_is_invalid_with_no_exception_text(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    """S3: sin respaldo por palabras clave y sin texto del parser en la respuesta."""
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    resp = _draft(client, token, db_id, "SELECT * FROM (")
    sobre = _contenido(resp)

    assert sobre["classification"] == "invalid"
    assert sobre["reasons"] == ["PARSE_FAILED"]
    assert sobre["warnings"] == []
    crudo = resp.text
    assert "ParseError" not in crudo and "Expecting" not in crudo and "Traceback" not in crudo
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_a_valid_select_is_a_read_and_still_does_not_touch_the_engine(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    """S4: redactar nunca ejecuta, ni siquiera una lectura. ``query_text`` es el render canónico."""
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, "select id   from clientes where id=1")

    assert sobre["classification"] == "read"
    assert sobre["reasons"] == [] and sobre["warnings"] == []
    assert sobre["query_text"] == "SELECT id FROM clientes WHERE id = 1"
    assert sobre["touches_engine"] is False
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


# --------------------------------------------------------------------------- #
# Todo input devuelve el sobre exacto                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        ";",
        "x" * 20_000,
        "SELECT 1",
        "DELETE FROM t",
        "SELECT 1; DROP TABLE t",
        "\x00\x01\x02",
        "SELECT '/*! DROP TABLE t */'",
        "ñandú 🚀 --",
    ],
)
def test_every_input_returns_exactly_the_five_key_envelope(
    client, admin_client, mcp_on, motor_falso, sin_motor, sql
):
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, sql)

    assert set(sobre) == _ENVELOPE_KEYS
    assert sobre["touches_engine"] is False
    assert sobre["classification"] in {"read", "write", "ddl", "blocked", "invalid"}
    assert isinstance(sobre["reasons"], list) and isinstance(sobre["warnings"], list)
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_an_empty_text_is_invalid(client, admin_client, mcp_on, motor_falso, sin_motor):
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, "")

    assert sobre["classification"] == "invalid" and sobre["reasons"] == ["PARSE_FAILED"]


def test_an_oversized_text_is_invalid_and_the_echo_is_clipped(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, "SELECT '" + "ñ" * 12_000 + "'")

    assert sobre["classification"] == "invalid"
    assert sobre["reasons"] == ["SQL_TOO_LARGE"]
    assert len(sobre["query_text"].encode("utf-8")) <= 16_384


def test_control_characters_are_stripped_from_the_echoed_text(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, "UPDATE t SET a = 'x\x07\x1b[31m'\x00")

    assert not any(ord(c) < 32 and c not in "\n\t" for c in sobre["query_text"])
    assert "\x7f" not in sobre["query_text"]
    assert sobre["classification"] == "invalid"


def test_the_pinned_database_comes_from_the_inventory_not_from_the_agent(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    """``core_cliente1`` es la base de la fila: otra se rechaza, la propia se acepta."""
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    ajena = _sobre(client, token, db_id, "SELECT * FROM otra_base.clientes")
    propia = _sobre(client, token, db_id, "SELECT * FROM core_cliente1.clientes")

    assert ajena["classification"] == "blocked" and "CROSS_DATABASE" in ajena["reasons"]
    assert propia["classification"] == "read" and propia["reasons"] == []


def test_a_select_with_a_deceptive_cte_is_never_drafted_as_read(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    sobre = _sobre(client, token, db_id, "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d")

    assert sobre["classification"] != "read"
    assert "DML_IN_CTE" in sobre["reasons"]
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


# --------------------------------------------------------------------------- #
# El gate de la base sigue valiendo                                            #
# --------------------------------------------------------------------------- #
def test_a_database_of_another_project_is_not_found(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    token, _ = _escenario(admin_client)
    sin_motor.armar()
    otro = _proyecto(admin_client, nombre="Ajeno")
    ajena = _bd_alcanzable(admin_client, project_id=otro)
    _credencial_ro(admin_client, _server_de(ajena))
    sin_motor.armar()

    resp = _draft(client, token, ajena, "SELECT 1")

    assert _error(resp)["code"] == "mcp.not_found"
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_the_scope_is_enforced_before_the_handler(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    pid = _proyecto(admin_client)
    token = _token(admin_client, pid, ["blueprints.read"])
    sin_motor.armar()

    resp = _draft(client, token, 1, "SELECT 1")

    assert _error(resp)["code"] == "mcp.scope_denied"
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_without_a_readonly_credential_the_gate_denies_and_nothing_is_opened(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    token, db_id = _escenario(admin_client, credencial=False)
    sin_motor.armar()

    resp = _draft(client, token, db_id, "SELECT 1")

    assert _error(resp)["code"] == "mcp.readonly_credential_missing"
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"database_id": "1", "sql": "SELECT 1"},
        {"database_id": True, "sql": "SELECT 1"},
        {"database_id": 1, "sql": 42},
        {"database_id": 1, "sql": None},
        {"database_id": 1},
        {"sql": "SELECT 1"},
    ],
)
def test_a_mistyped_argument_is_a_tool_error_with_a_closed_code(
    client, admin_client, mcp_on, motor_falso, sin_motor, arguments
):
    token, _ = _escenario(admin_client)
    sin_motor.armar()

    resp = _rpc(client, token, "tools/call", {"name": "draft_query", "arguments": arguments})

    assert _error(resp)["code"] == "MALFORMED_REQUEST"
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_an_undeclared_argument_is_a_protocol_error(
    client, admin_client, mcp_on, motor_falso, sin_motor
):
    token, db_id = _escenario(admin_client)
    sin_motor.armar()

    resp = _rpc(
        client,
        token,
        "tools/call",
        {"name": "draft_query", "arguments": {"database_id": db_id, "sql": "SELECT 1", "x": 1}},
    )

    assert resp.json()["error"]["code"] == -32602
    assert sin_motor.llamadas == [] and motor_falso.abiertas == []


def test_the_tool_is_published_to_a_token_with_databases_read_and_hidden_otherwise(
    client, admin_client, mcp_on
):
    pid = _proyecto(admin_client)
    con = _token(admin_client, pid, ["blueprints.read", "databases.read"])
    sin = _token(admin_client, pid, ["blueprints.read"])

    publicadas = {t["name"] for t in _rpc(client, con, "tools/list").json()["result"]["tools"]}
    ocultas = {t["name"] for t in _rpc(client, sin, "tools/list").json()["result"]["tools"]}

    assert "draft_query" in publicadas
    assert "draft_query" not in ocultas


# --------------------------------------------------------------------------- #
# Registro: scope, anotaciones, schema                                         #
# --------------------------------------------------------------------------- #
def test_the_registry_entry_is_read_only_and_does_not_touch_the_engine():
    from app.mcp.registry import BY_NAME

    spec = BY_NAME["draft_query"]

    assert spec.scope == "databases.read"
    assert spec.touches_engine is False
    assert spec.annotations["readOnlyHint"] is True
    assert spec.annotations["destructiveHint"] is False
    assert spec.input_schema["additionalProperties"] is False
    assert set(spec.input_schema["required"]) == {"database_id", "sql"}
    assert spec.input_schema["properties"]["sql"]["type"] == "string"
    # El texto SQL no tiene ``maxLength`` en el schema a propósito: un texto enorme tiene que
    # recibir un sobre ``invalid``/``SQL_TOO_LARGE`` y no un error de protocolo que invite a
    # reintentar.
    assert "maxLength" not in spec.input_schema["properties"]["sql"]
    assert "sin ejecut" in spec.description.lower()


def test_the_published_descriptor_carries_the_read_only_annotations(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    token = _token(admin_client, pid, ["blueprints.read", "databases.read"])

    tools = {t["name"]: t for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}

    assert tools["draft_query"]["annotations"]["readOnlyHint"] is True
    assert tools["draft_query"]["annotations"]["destructiveHint"] is False
    assert json.dumps(tools["draft_query"])  # serializable tal cual por el cable


# --------------------------------------------------------------------------- #
# S33: el camino no importa nada de la capa de motor                           #
# --------------------------------------------------------------------------- #
_ENGINE_MODULES = {
    "app.core.remote_engine",
    "app.core.database",
    "app.controllers.common",
    "app.services.db_admin.factory",
    "app.services.db_admin.query_runner",
}


def _imports(path: pathlib.Path) -> set[str]:
    from tests.test_mcp_import_guard import _imports_de

    return _imports_de(path)


@pytest.mark.parametrize(
    "relativa",
    [
        "app/mcp/tools/query.py",
        "app/services/db_admin/agent_sql_policy.py",
        "app/services/db_admin/sql_lexing.py",
    ],
)
def test_the_draft_path_modules_import_no_engine_module(relativa):
    assert not (_imports(_RAIZ / relativa) & _ENGINE_MODULES), relativa


def test_the_draft_controller_function_never_builds_a_target_or_opens_a_session():
    """S33. ``draft_agent_query`` resuelve el gate y nada más: ni target, ni façade, ni conexión."""
    from app.controllers import target_resolution

    arbol = ast.parse(inspect.getsource(target_resolution.draft_agent_query).lstrip())
    funcion = arbol.body[0]
    cuerpo = ast.Module(body=funcion.body[1:], type_ignores=[])  # sin el docstring
    nombres = {n.id for n in ast.walk(cuerpo) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(cuerpo) if isinstance(n, ast.Attribute)
    }
    importados = {
        a.name for n in ast.walk(cuerpo) if isinstance(n, ast.ImportFrom) for a in n.names
    } | {n.module for n in ast.walk(cuerpo) if isinstance(n, ast.ImportFrom)}

    assert not (
        nombres
        & {
            "_readonly_target",
            "open_readonly",
            "build_target",
            "get_adapter",
            "database_connection",
            "get_engine",
            "server_connection",
            "Database",
            "ServerTarget",
        }
    )
    assert importados == {"app.services.db_admin", "agent_sql_policy"}
    assert "resolve_agent_database" in nombres


def test_the_mcp_package_does_not_import_the_validator_directly():
    """La puerta es tool -> ToolContext -> target_resolution -> servicio, y solo esa."""
    for path in (_RAIZ / "app" / "mcp").rglob("*.py"):
        directos = {m for m in _imports(path) if m.startswith("app.services.db_admin")}
        assert not directos, f"{path}: {sorted(directos)}"


def test_the_context_method_delegates_to_the_allowlisted_controller(monkeypatch):
    """``ToolContext.draft_query`` no tiene lógica propia: pasa actor y capacidad del dispatcher."""
    from app.controllers import target_resolution
    from app.mcp.context import ToolContext
    from app.services.capability_catalog import Capability

    visto = {}

    def _falso(actor, database_id, sql, capability):
        visto.update(actor=actor, database_id=database_id, sql=sql, capability=capability)
        return {"ok": True}

    monkeypatch.setattr(target_resolution, "draft_agent_query", _falso)
    ctx = ToolContext(actor="el-actor", capability=Capability.DATABASES_READ)

    assert ctx.draft_query(7, "SELECT 1") == {"ok": True}
    assert visto == {
        "actor": "el-actor",
        "database_id": 7,
        "sql": "SELECT 1",
        "capability": Capability.DATABASES_READ,
    }
