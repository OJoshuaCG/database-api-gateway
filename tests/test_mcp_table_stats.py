"""
Tool ``get_table_stats`` del MCP (slice S7 de ``mcp-schema-definitions``): registro, gate de
estructura, campos que dependen de ``data.read``, ``missing[]``, topes y las consultas por motor.

Dos capas, una por bloque del archivo:

- La TOOL completa: se llama a ``dispatch.handle`` con un actor de token real, de modo que el gate,
  el scope y la proyección corren de verdad sobre la BD de metadatos de test. El motor se reemplaza
  SOLO por el façade falso inyectado en ``readonly_introspection`` (``motor_falso``), que registra
  con qué nombres y con qué ``include_row_estimates`` se lo llamó.
- Los ADAPTERS (MySQL/MariaDB y PostgreSQL): una conexión falsa que registra el SQL y los
  parámetros enlazados, para afirmar que ningún nombre se interpola y que las columnas de estimado
  no se piden cuando el llamador no las puede ver.

QUÉ NO SE VERIFICA ACÁ
----------------------
Que un MariaDB/MySQL/PostgreSQL reales devuelvan estas columnas con estos tipos (staging), ni los
valores que el motor reporta en ``information_schema.TABLES`` / ``pg_class`` para una tabla real.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_mcp_table_stats``
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import ast
import json
import pathlib
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.controllers import target_resolution
from app.core import environments
from app.core.actor import admin_actor, token_actor
from app.exceptions import AppHttpException
from app.mcp import dispatch, registry
from app.mcp.context import ToolContext
from app.mcp.tools import table_stats as table_stats_tool
from app.services.capability_catalog import Capability, GatewayRole
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import TableStatsRead
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter
from app.services.db_admin.readonly_introspector import ReadonlyIntrospector
from tests.step_up_helpers import OPEN_WINDOW
from tests.test_mcp_catalog_tools import (  # noqa: F401
    _credencial_ro,
    _FacadeFalso,
    _server_de,
    mcp_on,
    motor_falso,
)
from tests.test_mcp_server import _bd_alcanzable, _proyecto

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_SCOPES_SOLO_ESTRUCTURA = "databases.read"
_SCOPES_CON_DATOS = "databases.read,data.read"
_CAMPOS_BASE = {
    "name",
    "engine",
    "collation",
    "data_bytes",
    "index_bytes",
    "created_at",
    "updated_at",
}
_CAMPOS_DE_ESTIMADO = {"row_estimate", "auto_increment"}
_PROHIBIDAS = ("confirm_token", "password", "encrypted", "host", "port")


# --------------------------------------------------------------------------- #
# Arnés de la tool                                                             #
# --------------------------------------------------------------------------- #
def _lectura(nombre, **extra) -> TableStatsRead:
    """Una lectura de adapter con estimados siempre presentes (el façade decide si se piden)."""
    valores = {
        "table": nombre,
        "engine": "InnoDB",
        "collation": "utf8mb4_general_ci",
        "data_bytes": 16384,
        "index_bytes": 8192,
        "created_at": datetime(2026, 1, 2, 3, 4, 5),
        "updated_at": datetime(2026, 2, 3, 4, 5, 6),
        "row_estimate": 1234,
        "auto_increment": 1235,
    }
    valores.update(extra)
    return TableStatsRead(**valores)


class _FacadeEstadisticas(_FacadeFalso):
    """
    ``_FacadeFalso`` más ``table_stats``. Registra cada llamada (nombres e
    ``include_row_estimates``) para afirmar qué se consultó y qué NO.
    """

    def __init__(self, registro, *, indice=None, lecturas=None):
        super().__init__(
            registro,
            indice=indice
            or {
                "table": ["clientes", "pedidos"],
                "view": ["v_activos"],
                "routine": [],
                "trigger": [],
                "sequence": [],
                "event": [],
            },
        )
        self.consistent_structure = True
        self.lecturas = lecturas if lecturas is not None else {}
        self.llamadas_stats: list[tuple[tuple[str, ...], bool]] = []

    def table_stats(self, tables, *, include_row_estimates):
        self.llamadas_stats.append((tuple(tables), include_row_estimates))
        return [self.lecturas[name] for name in tables if name in self.lecturas]


def _facade(motor_falso, **kwargs) -> _FacadeEstadisticas:
    facade = _FacadeEstadisticas(motor_falso.registro, **kwargs)
    motor_falso.por_base["core_cliente1"] = facade
    return facade


def _lecturas_por_defecto() -> dict:
    return {"clientes": _lectura("clientes"), "pedidos": _lectura("pedidos", engine="MyISAM")}


def _actor(project_id, scopes):
    """Actor de token cuyo emisor es owner con step-up abierto (``data.read`` exige las dos cosas)."""
    return token_actor(
        token_pk=1,
        token_id="t",
        name="agente",
        scopes=scopes,
        project_id=project_id,
        issuer=admin_actor(
            user_id=1, username="o", role=GatewayRole.OWNER, step_up_until=OPEN_WINDOW
        ),
    )


def _escenario(
    admin_client,
    monkeypatch,
    *,
    scopes=_SCOPES_SOLO_ESTRUCTURA,
    data_read_switch=True,
    credencial=True,
):
    """
    Base alcanzable, credencial de ESTRUCTURA y un actor de token. NO se siembra credencial de
    datos: ``get_table_stats`` va por el gate de estructura y el escenario lo prueba por ausencia.
    """
    # El actor se arma con el switch de datos ENCENDIDO (apagado, ``data.read`` sería inerte desde
    # el emisor) y recién después se fija el estado a medir: así se prueba "se apagó con el token
    # ya emitido".
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    project_id = _proyecto(admin_client)
    database_id = _bd_alcanzable(admin_client, project_id=project_id)
    if credencial:
        _credencial_ro(admin_client, _server_de(database_id))
    actor = _actor(project_id, scopes)
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", data_read_switch)
    return actor, database_id


def _responder(actor, args):
    return dispatch.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_table_stats", "arguments": args},
        },
        actor,
        {},
    )


def _llamar(actor, args):
    respuesta = _responder(actor, args)
    assert respuesta.status == 200, respuesta.body
    return respuesta.body


def _ok(body) -> dict:
    assert "result" in body, f"error JSON-RPC: {body.get('error')}"
    assert body["result"]["isError"] is False, body
    return body["result"]["structuredContent"]


def _codigo(body) -> str:
    assert "result" in body, f"error JSON-RPC: {body.get('error')}"
    assert body["result"]["isError"] is True, body
    return body["result"]["structuredContent"]["error"]["code"]


# --------------------------------------------------------------------------- #
# Registro: scope de estructura, sin SQL                                       #
# --------------------------------------------------------------------------- #
def test_the_tool_is_registered_under_the_structure_scope_without_the_data_tag():
    spec = registry.BY_NAME["get_table_stats"]
    assert spec.scope == "databases.read"
    assert spec.touches_engine is True
    assert "data" not in spec.tags
    assert spec.annotations["readOnlyHint"] is True and spec.annotations["destructiveHint"] is False


def test_the_schema_is_closed_bounded_and_takes_names_not_sql():
    spec = registry.BY_NAME["get_table_stats"]
    properties = spec.input_schema["properties"]
    assert spec.input_schema["additionalProperties"] is False
    assert spec.input_schema["required"] == ["database_id", "tables"]
    assert set(properties) == {"database_id", "tables"}
    assert not ({"sql", "query", "statement", "where", "schema", "database"} & set(properties))
    tables = properties["tables"]
    assert tables["minItems"] == 1
    assert tables["maxItems"] == table_stats_tool.MAX_TABLES_PER_CALL
    assert tables["items"]["type"] == "string" and tables["items"]["maxLength"] == 128


def test_the_description_states_the_data_read_condition_and_that_no_sql_is_accepted():
    descripcion = registry.BY_NAME["get_table_stats"].description.lower()
    assert "no sql" in descripcion
    assert "row_estimate" in descripcion and "data.read" in descripcion
    assert "missing" in descripcion


def test_the_registry_invariants_still_hold_with_the_new_tool():
    registry._assert_invariants()
    registry._assert_invariants(registry._build(data_read_enabled=True))


def test_the_tool_is_published_to_databases_read_and_hidden_without_it():
    con = _actor(1, _SCOPES_SOLO_ESTRUCTURA)
    sin = _actor(1, "blueprints.read")
    assert "get_table_stats" in {t.name for t in registry.tools_for(con)}
    assert "get_table_stats" not in {t.name for t in registry.tools_for(sin)}


def test_the_tool_module_never_imports_the_service_or_the_engine_layer():
    fuente = (_RAIZ / "app" / "mcp" / "tools" / "table_stats.py").read_text(encoding="utf-8")
    importados = set()
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.add(nodo.module)
        elif isinstance(nodo, ast.Import):
            importados |= {a.name for a in nodo.names}
    assert not any(
        "db_admin" in m or "remote_engine" in m or "target_resolution" in m for m in importados
    ), importados


# --------------------------------------------------------------------------- #
# Gate: estructura, no datos                                                   #
# --------------------------------------------------------------------------- #
def test_a_token_without_databases_read_is_denied_before_the_engine_is_opened(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, scopes="blueprints.read")

    respuesta = _llamar(actor, {"database_id": database_id, "tables": ["clientes"]})

    assert _codigo(respuesta) == "mcp.scope_denied"
    assert motor_falso.abiertas == []


def test_a_database_of_another_project_is_not_found_and_never_opened(
    admin_client, monkeypatch, motor_falso
):
    actor, _ = _escenario(admin_client, monkeypatch)
    otro_proyecto = _proyecto(admin_client, nombre="Ajeno")
    base_ajena = _bd_alcanzable(admin_client, project_id=otro_proyecto)
    _credencial_ro(admin_client, _server_de(base_ajena))

    respuesta = _llamar(actor, {"database_id": base_ajena, "tables": ["clientes"]})

    assert _codigo(respuesta) == "mcp.not_found"
    assert motor_falso.abiertas == []


def test_without_a_structure_credential_the_engine_is_never_opened(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, credencial=False)

    respuesta = _llamar(actor, {"database_id": database_id, "tables": ["clientes"]})

    assert _codigo(respuesta) == "mcp.readonly_credential_missing"
    assert motor_falso.abiertas == []


def test_the_read_needs_no_data_credential_and_does_not_depend_on_the_data_kill_switch(
    admin_client, monkeypatch, motor_falso
):
    """Solo estructura + switch de datos APAGADO: igual responde, porque no pasa por ``_data_gate``."""
    actor, database_id = _escenario(admin_client, monkeypatch, data_read_switch=False)
    _facade(motor_falso, lecturas=_lecturas_por_defecto())

    respuesta = _llamar(actor, {"database_id": database_id, "tables": ["clientes"]})

    contenido = _ok(respuesta)
    assert [t["name"] for t in contenido["data"]["tables"]] == ["clientes"]
    assert motor_falso.abiertas == [("core_cliente1", "mcp_ro")]


# --------------------------------------------------------------------------- #
# S7.1 / S7.2: row_estimate y auto_increment solo con data.read                #
# --------------------------------------------------------------------------- #
def test_s7_1_with_only_databases_read_the_estimate_keys_do_not_exist(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, scopes=_SCOPES_SOLO_ESTRUCTURA)
    facade = _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes", "pedidos"]}))

    datos = contenido["data"]
    for tabla in datos["tables"]:
        assert set(tabla) == _CAMPOS_BASE
        assert not (_CAMPOS_DE_ESTIMADO & set(tabla))
    assert datos["row_estimates_included"] is False
    assert datos["row_estimates_omitted_reason"] == "requires_data_read_scope"
    assert facade.llamadas_stats == [(("clientes", "pedidos"), False)]
    serializado = json.dumps(contenido)
    assert "1234" not in serializado and "1235" not in serializado


def test_s7_2_with_databases_read_and_data_read_both_estimates_are_present(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, scopes=_SCOPES_CON_DATOS)
    facade = _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes"]}))

    datos = contenido["data"]
    assert datos["row_estimates_included"] is True
    assert datos["row_estimates_omitted_reason"] is None
    (tabla,) = datos["tables"]
    assert set(tabla) == _CAMPOS_BASE | _CAMPOS_DE_ESTIMADO
    assert tabla["row_estimate"] == 1234 and tabla["auto_increment"] == 1235
    assert facade.llamadas_stats == [(("clientes",), True)]


def test_s7_2_a_data_read_token_loses_the_estimates_when_the_kill_switch_is_off_at_call_time(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(
        admin_client, monkeypatch, scopes=_SCOPES_CON_DATOS, data_read_switch=False
    )
    assert actor.has(Capability.DATA_READ), "el escenario tiene que emitir el token CON el scope"
    facade = _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes"]}))

    (tabla,) = contenido["data"]["tables"]
    assert not (_CAMPOS_DE_ESTIMADO & set(tabla))
    assert contenido["data"]["row_estimates_omitted_reason"] == "requires_data_read_scope"
    assert facade.llamadas_stats == [(("clientes",), False)]


def test_even_if_the_facade_returned_estimates_the_whitelist_mapper_drops_them():
    """Defensa en profundidad: ``TableStatsOut`` no tiene dónde guardar un estimado."""
    mapeada = table_stats_tool._map_table_stats(_lectura("clientes"), with_estimates=False)
    assert set(mapeada.model_dump()) == _CAMPOS_BASE


def test_caller_may_see_row_estimates_needs_the_scope_and_the_live_switch(monkeypatch):
    # El actor se arma con el switch encendido: apagado, ``data.read`` no llegaría al token.
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    con_scope = _actor(1, _SCOPES_CON_DATOS)
    sin_scope = _actor(1, _SCOPES_SOLO_ESTRUCTURA)
    assert con_scope.has(Capability.DATA_READ)
    assert target_resolution.caller_may_see_row_estimates(con_scope) is True
    assert target_resolution.caller_may_see_row_estimates(sin_scope) is False
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", False)
    assert target_resolution.caller_may_see_row_estimates(con_scope) is False


# --------------------------------------------------------------------------- #
# S7.3: lo que no está en el índice va a missing sin consultarse               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "nombre_pedido",
    [
        "no_existe",
        "_gw_v_clientes",
        "otra_base.clientes",
        "clientes'; DROP TABLE clientes; --",
        "v_activos",
        "CLIENTES",
    ],
)
def test_s7_3_a_name_that_is_not_an_indexed_table_goes_to_missing_with_zero_reads(
    admin_client, monkeypatch, motor_falso, nombre_pedido
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    facade = _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": [nombre_pedido]}))

    assert contenido["data"]["tables"] == []
    assert contenido["data"]["missing"] == [nombre_pedido]
    assert facade.llamadas_stats == [], "un nombre fuera del índice jamás llega al adapter"


def test_present_and_absent_names_are_split_and_only_the_present_ones_are_queried(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    facade = _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(
        _llamar(actor, {"database_id": database_id, "tables": ["pedidos", "fantasma", "clientes"]})
    )

    assert [t["name"] for t in contenido["data"]["tables"]] == ["pedidos", "clientes"]
    assert contenido["data"]["missing"] == ["fantasma"]
    assert facade.llamadas_stats == [(("pedidos", "clientes"), False)]


def test_duplicate_names_are_collapsed_keeping_the_request_order(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    facade = _facade(motor_falso, lecturas=_lecturas_por_defecto())

    _ok(_llamar(actor, {"database_id": database_id, "tables": ["pedidos", "clientes", "pedidos"]}))

    assert facade.llamadas_stats == [(("pedidos", "clientes"), False)]


def test_a_table_the_catalog_stops_returning_is_reported_missing_not_dropped(
    admin_client, monkeypatch, motor_falso
):
    """El índice la listó y ``table_stats`` ya no la devuelve (un ``DROP`` en el medio)."""
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, lecturas={"clientes": _lectura("clientes")})

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes", "pedidos"]}))

    assert [t["name"] for t in contenido["data"]["tables"]] == ["clientes"]
    assert contenido["data"]["missing"] == ["pedidos"]


# --------------------------------------------------------------------------- #
# Topes y validación: antes de conectar                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "args",
    [
        {"database_id": "x", "tables": ["t"]},
        {"database_id": True, "tables": ["t"]},
        {"database_id": 1},
        {"database_id": 1, "tables": []},
        {"database_id": 1, "tables": "clientes"},
        {"database_id": 1, "tables": [7]},
        {"database_id": 1, "tables": [""]},
        {"database_id": 1, "tables": ["x" * 129]},
        {"database_id": 1, "tables": [{"name": "t"}]},
    ],
)
def test_invalid_arguments_are_rejected_before_any_connection(
    admin_client, monkeypatch, motor_falso, args
):
    actor, _ = _escenario(admin_client, monkeypatch)

    respuesta = _llamar(actor, args)

    assert _codigo(respuesta) == "mcp.invalid_argument"
    assert motor_falso.abiertas == []


def test_more_tables_than_the_cap_is_a_413_before_any_connection(
    admin_client, monkeypatch, motor_falso
):
    monkeypatch.setattr(table_stats_tool, "MAX_TABLES_PER_CALL", 2)
    actor, database_id = _escenario(admin_client, monkeypatch)

    respuesta = _llamar(actor, {"database_id": database_id, "tables": ["a", "b", "c"]})

    assert _codigo(respuesta) == "mcp.too_many_objects"
    assert motor_falso.abiertas == []


def test_exactly_the_cap_is_accepted(admin_client, monkeypatch, motor_falso):
    monkeypatch.setattr(table_stats_tool, "MAX_TABLES_PER_CALL", 2)
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes", "pedidos"]}))

    assert len(contenido["data"]["tables"]) == 2


def test_the_service_repeats_the_cap_for_callers_that_skip_the_handler(
    admin_client, monkeypatch, motor_falso
):
    monkeypatch.setattr(environments, "MCP_MAX_OBJECTS_PER_CALL", 1)
    actor, database_id = _escenario(admin_client, monkeypatch)
    contexto = ToolContext(actor=actor, capability=Capability.DATABASES_READ)

    with pytest.raises(AppHttpException) as excepcion:
        contexto.get_table_stats(database_id, ["clientes", "pedidos"])
    assert excepcion.value.status_code == 413
    assert excepcion.value.public_context["code"] == "mcp.too_many_objects"
    assert motor_falso.abiertas == []

    with pytest.raises(AppHttpException) as vacia:
        contexto.get_table_stats(database_id, [])
    assert vacia.value.status_code == 413
    assert motor_falso.abiertas == []


# --------------------------------------------------------------------------- #
# La tool no acepta SQL                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("clave", ["sql", "query", "statement", "where", "schema", "database"])
def test_any_undeclared_argument_is_a_protocol_error_and_nothing_runs(
    admin_client, monkeypatch, motor_falso, clave
):
    actor, database_id = _escenario(admin_client, monkeypatch)

    respuesta = _llamar(
        actor, {"database_id": database_id, "tables": ["clientes"], clave: "SELECT 1"}
    )

    assert "error" in respuesta and respuesta["error"]["code"] == -32602
    assert motor_falso.abiertas == []


# --------------------------------------------------------------------------- #
# Salida: lista blanca y contenido no confiable                                #
# --------------------------------------------------------------------------- #
def test_the_response_maps_every_field_and_serializes_dates_as_iso_text(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes"]}))

    (tabla,) = contenido["data"]["tables"]
    assert tabla == {
        "name": "clientes",
        "engine": "InnoDB",
        "collation": "utf8mb4_general_ci",
        "data_bytes": 16384,
        "index_bytes": 8192,
        "created_at": "2026-01-02T03:04:05",
        "updated_at": "2026-02-03T04:05:06",
    }
    assert contenido["untrusted_content"] is True
    assert contenido["objects_omitted"] is False
    assert contenido["database"]["database_id"] == database_id


def test_a_postgres_style_read_comes_back_with_null_engine_collation_and_dates(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    lectura_pg = TableStatsRead(table="clientes", data_bytes=8192, index_bytes=16384)
    _facade(motor_falso, lecturas={"clientes": lectura_pg})

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes"]}))

    (tabla,) = contenido["data"]["tables"]
    assert tabla["engine"] is None and tabla["collation"] is None
    assert tabla["created_at"] is None and tabla["updated_at"] is None
    assert tabla["data_bytes"] == 8192 and tabla["index_bytes"] == 16384


def test_table_names_and_text_columns_are_cleaned_of_control_characters(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    nombre_hostil = "clientes\x07\x1b[31m"
    _facade(
        motor_falso,
        indice={"table": [nombre_hostil], "view": [], "routine": [], "trigger": [], "sequence": []},
        lecturas={nombre_hostil: _lectura(nombre_hostil, engine="Inno\x00DB")},
    )

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": [nombre_hostil]}))

    (tabla,) = contenido["data"]["tables"]
    assert "\x07" not in tabla["name"] and "\x1b" not in tabla["name"]
    assert tabla["engine"] == "InnoDB"


def test_the_response_contains_none_of_the_forbidden_substrings(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, scopes=_SCOPES_CON_DATOS)
    _facade(motor_falso, lecturas=_lecturas_por_defecto())

    contenido = _ok(_llamar(actor, {"database_id": database_id, "tables": ["clientes"]}))

    serializado = json.dumps(contenido).lower()
    for prohibida in _PROHIBIDAS:
        assert prohibida not in serializado, prohibida


def test_the_output_models_forbid_extra_fields_and_do_not_inherit_each_other():
    from app.schemas import mcp as out

    assert not issubclass(out.TableStatsOut, out.TableStatsWithEstimatesOut)
    assert not issubclass(out.TableStatsWithEstimatesOut, out.TableStatsOut)
    for modelo in (out.TableStatsOut, out.TableStatsWithEstimatesOut, out.TableStatsListOut):
        assert modelo.model_config.get("extra") == "forbid", modelo.__name__
    with pytest.raises(ValueError):
        out.TableStatsOut(
            name="t", engine=None, collation=None, data_bytes=None, index_bytes=None,
            created_at=None, updated_at=None, row_estimate=1,
        )


# --------------------------------------------------------------------------- #
# Façade                                                                       #
# --------------------------------------------------------------------------- #
class _SesionFalsa:
    def __init__(self, conn):
        self.conn = conn
        self.deadline_checks = 0

    def check_deadline(self) -> None:
        self.deadline_checks += 1


def test_the_facade_delegates_to_the_adapter_with_the_pinned_schema_and_checks_the_deadline():
    llamadas = []

    def _leer(conn, database, schema, tables, *, include_row_estimates):
        llamadas.append((conn, database, schema, tables, include_row_estimates))
        return [TableStatsRead(table="clientes")]

    adapter = SimpleNamespace(
        _inspect_schema=lambda database: "inventario", read_table_storage_stats=_leer
    )
    conexion = object()
    sesion = _SesionFalsa(conexion)
    facade = ReadonlyIntrospector(adapter, sesion, "inventario")

    resultado = facade.table_stats(("clientes",), include_row_estimates=True)

    assert resultado == [TableStatsRead(table="clientes")]
    assert llamadas == [(conexion, "inventario", "inventario", ["clientes"], True)]
    assert sesion.deadline_checks == 1


# --------------------------------------------------------------------------- #
# Adapters: SQL y parámetros enlazados                                         #
# --------------------------------------------------------------------------- #
class _ResultadoFalso:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _ConexionFalsa:
    """Registra cada ``(sql, params)`` y responde siempre con las mismas filas."""

    def __init__(self, rows):
        self._rows = rows
        self.ejecutadas: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        self.ejecutadas.append((str(statement), dict(params or {})))
        return _ResultadoFalso(self._rows)


_NOMBRE_HOSTIL = "t'; DROP TABLE clientes; --"


def _fila_mysql(nombre="clientes", **extra):
    valores = {
        "engine": "InnoDB",
        "collation": "utf8mb4_general_ci",
        "data_length": 16384,
        "index_length": 8192,
        "create_time": datetime(2026, 1, 2, 3, 4, 5),
        "update_time": None,
        "table_rows": 1234,
        "auto_increment": 1235,
    }
    valores.update(extra)
    return (nombre, *valores.values())


def test_mysql_filters_by_bound_schema_and_bound_names_and_never_interpolates_a_name():
    conexion = _ConexionFalsa([_fila_mysql("clientes")])
    adaptador = MySQLAdapter(target=None)

    adaptador.read_table_storage_stats(
        conexion, "core_cliente1", "core_cliente1", ["clientes", _NOMBRE_HOSTIL],
        include_row_estimates=True,
    )

    ((sql, params),) = conexion.ejecutadas
    assert "information_schema.TABLES" in sql
    assert "TABLE_SCHEMA = :db" in sql
    assert "TABLE_NAME IN (:t0, :t1)" in sql
    assert params == {"db": "core_cliente1", "t0": "clientes", "t1": _NOMBRE_HOSTIL}
    assert "clientes" not in sql.replace("information_schema", "")
    assert "DROP" not in sql and "'" not in sql and "core_cliente1" not in sql


def test_mysql_maps_every_column_and_keeps_null_as_none():
    conexion = _ConexionFalsa([_fila_mysql("clientes", update_time=None, auto_increment=None)])

    (lectura,) = MySQLAdapter(target=None).read_table_storage_stats(
        conexion, "db", "db", ["clientes"], include_row_estimates=True
    )

    assert lectura == TableStatsRead(
        table="clientes",
        engine="InnoDB",
        collation="utf8mb4_general_ci",
        data_bytes=16384,
        index_bytes=8192,
        created_at=datetime(2026, 1, 2, 3, 4, 5),
        updated_at=None,
        row_estimate=1234,
        auto_increment=None,
    )


def test_mysql_without_row_estimates_does_not_even_select_those_columns():
    conexion = _ConexionFalsa(
        [_fila_mysql("clientes", table_rows=None, auto_increment=None)]
    )

    (lectura,) = MySQLAdapter(target=None).read_table_storage_stats(
        conexion, "db", "db", ["clientes"], include_row_estimates=False
    )

    ((sql, _),) = conexion.ejecutadas
    assert "TABLE_ROWS" not in sql and "AUTO_INCREMENT" not in sql
    assert lectura.row_estimate is None and lectura.auto_increment is None


def test_mysql_with_row_estimates_selects_both_columns():
    conexion = _ConexionFalsa([_fila_mysql("clientes")])

    MySQLAdapter(target=None).read_table_storage_stats(
        conexion, "db", "db", ["clientes"], include_row_estimates=True
    )

    ((sql, _),) = conexion.ejecutadas
    assert "TABLE_ROWS" in sql and "AUTO_INCREMENT" in sql


def test_mysql_ignores_a_row_whose_name_was_not_requested_and_keeps_the_request_order():
    """Una collation insensible a mayúsculas puede devolver ``Clientes`` para el pedido ``clientes``."""
    conexion = _ConexionFalsa(
        [_fila_mysql("pedidos"), _fila_mysql("Clientes"), _fila_mysql("clientes")]
    )

    lecturas = MySQLAdapter(target=None).read_table_storage_stats(
        conexion, "db", "db", ["clientes", "pedidos"], include_row_estimates=False
    )

    assert [lectura.table for lectura in lecturas] == ["clientes", "pedidos"]


def test_mysql_with_no_tables_runs_no_query():
    conexion = _ConexionFalsa([])

    assert MySQLAdapter(target=None).read_table_storage_stats(
        conexion, "db", "db", [], include_row_estimates=True
    ) == []
    assert conexion.ejecutadas == []


def _fila_postgres(nombre="clientes", data_bytes=8192, index_bytes=16384, row_estimate=77):
    return (nombre, data_bytes, index_bytes, row_estimate)


def test_postgres_uses_pg_class_with_the_pinned_schema_and_bound_names():
    conexion = _ConexionFalsa([_fila_postgres("clientes")])
    adaptador = PostgresAdapter(target=None)

    adaptador.read_table_storage_stats(
        conexion, "core_cliente1", "public", ["clientes", _NOMBRE_HOSTIL],
        include_row_estimates=True,
    )

    ((sql, params),) = conexion.ejecutadas
    assert "pg_class" in sql and "pg_total_relation_size" in sql and "pg_indexes_size" in sql
    assert "n.nspname = :schema" in sql
    assert "c.relname IN (:t0, :t1)" in sql
    assert params == {"schema": "public", "t0": "clientes", "t1": _NOMBRE_HOSTIL}
    assert "DROP" not in sql and "core_cliente1" not in sql and "'public'" not in sql


def test_postgres_maps_sizes_and_leaves_engine_collation_and_dates_empty():
    conexion = _ConexionFalsa([_fila_postgres("clientes", 8192, 16384, 77)])

    (lectura,) = PostgresAdapter(target=None).read_table_storage_stats(
        conexion, "db", "public", ["clientes"], include_row_estimates=True
    )

    assert lectura == TableStatsRead(
        table="clientes", data_bytes=8192, index_bytes=16384, row_estimate=77
    )
    assert lectura.auto_increment is None


def test_postgres_without_row_estimates_does_not_select_reltuples():
    conexion = _ConexionFalsa([_fila_postgres("clientes", row_estimate=None)])

    (lectura,) = PostgresAdapter(target=None).read_table_storage_stats(
        conexion, "db", "public", ["clientes"], include_row_estimates=False
    )

    ((sql, _),) = conexion.ejecutadas
    assert "reltuples" not in sql
    assert lectura.row_estimate is None


def test_postgres_with_row_estimates_selects_reltuples_and_maps_never_analyzed_to_none():
    conexion = _ConexionFalsa([_fila_postgres("clientes", row_estimate=None)])

    (lectura,) = PostgresAdapter(target=None).read_table_storage_stats(
        conexion, "db", "public", ["clientes"], include_row_estimates=True
    )

    ((sql, _),) = conexion.ejecutadas
    assert "reltuples < 0 THEN NULL" in sql
    assert lectura.row_estimate is None


def test_postgres_ignores_unrequested_rows_and_runs_no_query_without_tables():
    conexion = _ConexionFalsa([_fila_postgres("otra")])
    adaptador = PostgresAdapter(target=None)

    assert adaptador.read_table_storage_stats(
        conexion, "db", "public", ["clientes"], include_row_estimates=False
    ) == []
    conexion_vacia = _ConexionFalsa([])
    assert adaptador.read_table_storage_stats(
        conexion_vacia, "db", "public", [], include_row_estimates=False
    ) == []
    assert conexion_vacia.ejecutadas == []


# --------------------------------------------------------------------------- #
# Contrato base                                                                #
# --------------------------------------------------------------------------- #
def test_an_engine_without_an_implementation_raises_instead_of_returning_an_empty_list():
    adaptador_sin_soporte = SimpleNamespace(dialect="motor_nuevo")

    with pytest.raises(AppHttpException) as excepcion:
        ServerAdapter.read_table_storage_stats(
            adaptador_sin_soporte, object(), "db", "db", ["t"], include_row_estimates=False
        )
    assert excepcion.value.status_code == 422


def test_the_bound_name_list_builds_only_placeholders_and_rejects_an_empty_list():
    sql, params = ServerAdapter._bound_name_list(["a", "b'; --", "c"])

    assert sql == "(:t0, :t1, :t2)"
    assert params == {"t0": "a", "t1": "b'; --", "t2": "c"}
    with pytest.raises(ValueError):
        ServerAdapter._bound_name_list([])
