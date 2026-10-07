"""
``list_objects`` veraz (slice S4 de ``mcp-schema-definitions``): ``body_available`` por alcance del
llamador y motor/versión, el tipo ``event`` y la garantía de que el índice NO lee código.

Cubre S4.13 a S4.15 y la verdad por motor/versión del índice:

- Sin el scope ``data.definitions`` (o con el kill switch apagado): ``scope_disabled`` en todo lo que
  tiene cuerpo, y ni siquiera se consulta la versión.
- Con el scope: ``True`` salvo rutinas cuando motor/versión explican su ausencia (``flag_off``,
  ``engine_unsupported``), con el aviso ``mcp.warn.routines_not_visible``.
- ``get_schema`` nunca emite ``flag_off`` ni ``too_large`` en ``body_omitted_reason``.
- Los nombres de rutinas y triggers de MySQL salen de ``information_schema``, sin ``SHOW CREATE``.

QUÉ NO SE VERIFICA ACÁ: el motor real (qué rutinas oculta ``information_schema.ROUTINES`` según el
privilegio de la cuenta), que queda para la verificación en staging.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_mcp_list_objects_definitions``
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import typing
from types import SimpleNamespace

import pytest

from app.mcp import dispatch, registry
from app.services.db_admin.mysql_adapter import MySQLAdapter
from tests.test_mcp_catalog_tools import mcp_on, motor_falso  # noqa: F401
from tests.test_mcp_get_definition import _escenario, _facade

_INDICE_CON_TODO = {
    "table": ["clientes"],
    "view": ["v_activos"],
    "routine": ["calcular"],
    "trigger": ["trg_auditoria"],
    "sequence": [],
    "event": ["ev_limpieza"],
}
_SCOPES_SIN_DEFINICIONES = "databases.read"


def _list_objects(actor, database_id, **extra):
    respuesta = dispatch.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_objects",
                "arguments": {"database_id": database_id, **extra},
            },
        },
        actor,
        {},
    )
    assert respuesta.status == 200, respuesta.body
    resultado = respuesta.body["result"]
    return resultado["isError"], resultado["structuredContent"]


def _ok(actor, database_id, **extra) -> dict:
    es_error, sobre = _list_objects(actor, database_id, **extra)
    assert es_error is False, sobre
    return sobre


def _por_nombre(sobre) -> dict:
    return {objeto["name"]: objeto for objeto in sobre["data"]["objects"]}


# --------------------------------------------------------------------------- #
# S4.13: según el scope del llamador                                           #
# --------------------------------------------------------------------------- #
def test_s4_13_without_the_scope_every_body_is_scope_disabled_and_nothing_is_read(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, scopes=_SCOPES_SIN_DEFINICIONES)
    facade = _facade(motor_falso, indice=_INDICE_CON_TODO)

    sobre = _ok(actor, database_id)

    objetos = _por_nombre(sobre)
    for nombre in ("v_activos", "calcular", "trg_auditoria", "ev_limpieza"):
        assert objetos[nombre]["body_available"] is False, nombre
        assert objetos[nombre]["unavailable_reason"] == "scope_disabled", nombre
    assert objetos["clientes"]["body_available"] is None
    assert objetos["clientes"]["unavailable_reason"] is None
    assert facade.llamadas_definition == [], "el índice no lee código"
    assert facade.llamadas_version == 0, "sin scope no hace falta ni la versión"
    assert "mcp.warn.routines_not_visible" not in {w["code"] for w in sobre["warnings"]}


def test_s4_13_with_the_scope_and_a_supporting_engine_every_body_is_available(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    facade = _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor="8.0.36")

    sobre = _ok(actor, database_id)

    objetos = _por_nombre(sobre)
    for nombre in ("v_activos", "calcular", "trg_auditoria", "ev_limpieza"):
        assert objetos[nombre]["body_available"] is True, nombre
        assert objetos[nombre]["unavailable_reason"] is None, nombre
    assert facade.llamadas_definition == [], "zero SHOW CREATE: es el índice barato"
    assert facade.llamadas_version == 1
    assert "mcp.warn.routines_not_visible" not in {w["code"] for w in sobre["warnings"]}


def test_a_kill_switch_turned_off_after_issuing_the_token_closes_the_bodies(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch, switch=False)
    facade = _facade(motor_falso, indice=_INDICE_CON_TODO)

    sobre = _ok(actor, database_id)

    assert _por_nombre(sobre)["v_activos"]["unavailable_reason"] == "scope_disabled"
    assert facade.llamadas_version == 0


@pytest.mark.parametrize(
    "version, razon_rutina, avisa",
    [
        ("10.6.12-MariaDB", "flag_off", True),
        ("5.7.44", "flag_off", True),
        ("8.0.19", "engine_unsupported", True),
        ("8.0.36", None, False),
        ("11.4.2-MariaDB", None, False),
        (None, None, False),
    ],
)
def test_routines_follow_engine_and_version_while_other_kinds_stay_available(
    admin_client, monkeypatch, motor_falso, version, razon_rutina, avisa
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor=version)

    sobre = _ok(actor, database_id)

    objetos = _por_nombre(sobre)
    assert objetos["calcular"]["unavailable_reason"] == razon_rutina
    assert objetos["calcular"]["body_available"] is (razon_rutina is None)
    for nombre in ("v_activos", "trg_auditoria", "ev_limpieza"):
        assert objetos[nombre]["body_available"] is True, nombre
    codigos = {w["code"] for w in sobre["warnings"]}
    assert ("mcp.warn.routines_not_visible" in codigos) is avisa


# --------------------------------------------------------------------------- #
# S4.15: el tipo event                                                         #
# --------------------------------------------------------------------------- #
def test_s4_15_list_objects_includes_events_and_can_filter_by_them(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO)

    todos = _ok(actor, database_id)
    solo_events = _ok(actor, database_id, kinds=["event"])

    assert {"kind": "event", "name": "ev_limpieza"} in [
        {"kind": o["kind"], "name": o["name"]} for o in todos["data"]["objects"]
    ]
    assert [(o["kind"], o["name"]) for o in solo_events["data"]["objects"]] == [
        ("event", "ev_limpieza")
    ]
    assert "mcp.warn.objects_omitted_by_filter" in {w["code"] for w in solo_events["warnings"]}


def test_an_index_without_the_event_key_still_lists_the_other_kinds(
    admin_client, monkeypatch, motor_falso
):
    """PostgreSQL y los façades viejos no traen ``event``: no es un error, es ``[]``."""
    actor, database_id = _escenario(admin_client, monkeypatch)
    indice = {k: v for k, v in _INDICE_CON_TODO.items() if k != "event"}
    _facade(motor_falso, indice=indice)

    sobre = _ok(actor, database_id)

    assert "event" not in {o["kind"] for o in sobre["data"]["objects"]}
    assert "clientes" in _por_nombre(sobre)


def test_an_unknown_kind_is_still_rejected(admin_client, monkeypatch, motor_falso):
    actor, database_id = _escenario(admin_client, monkeypatch)

    es_error, sobre = _list_objects(actor, database_id, kinds=["column"])

    assert es_error is True and sobre["error"]["code"] == "mcp.invalid_argument"


def test_the_published_schemas_add_event_to_list_objects_only():
    por_nombre = {t.name: t for t in registry.TOOLS}
    enum_list_objects = por_nombre["list_objects"].input_schema["properties"]["kinds"]["items"]["enum"]
    enum_get_schema = por_nombre["get_schema"].input_schema["properties"]["objects"]["items"][
        "properties"
    ]["kind"]["enum"]
    assert "event" in enum_list_objects
    assert "event" not in enum_get_schema


# --------------------------------------------------------------------------- #
# S4.14: get_schema y flag_off / too_large                                     #
# --------------------------------------------------------------------------- #
def test_s4_14_get_schema_body_omitted_reason_can_only_be_scope_disabled():
    from app.schemas import mcp as out

    for modelo in (out.ViewOut, out.RoutineOut, out.TriggerOut):
        anotacion = modelo.model_fields["body_omitted_reason"].annotation
        assert typing.get_args(anotacion) == ("scope_disabled",), modelo.__name__


def test_s4_14_get_schema_never_emits_flag_off_or_too_large_even_with_the_scope(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor="10.6.12-MariaDB")
    objetos = [{"kind": "view", "name": "v_activos"}, {"kind": "routine", "name": "calcular"}]

    respuesta = dispatch.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_schema",
                "arguments": {"database_id": database_id, "objects": objetos},
            },
        },
        actor,
        {},
    )

    sobre = respuesta.body["result"]["structuredContent"]
    razones = {o["body_omitted_reason"] for o in sobre["data"]["objects"]}
    assert razones == {"scope_disabled"}
    assert "flag_off" not in str(sobre) and "too_large" not in str(sobre)


def test_get_schema_warning_points_to_get_definition(admin_client, monkeypatch, motor_falso):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO)

    respuesta = dispatch.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_schema",
                "arguments": {
                    "database_id": database_id,
                    "objects": [{"kind": "view", "name": "v_activos"}],
                },
            },
        },
        actor,
        {},
    )

    sobre = respuesta.body["result"]["structuredContent"]
    avisos = {w["code"]: w["message"] for w in sobre["warnings"]}
    assert "get_definition" in avisos["mcp.warn.bodies_unavailable"]


# --------------------------------------------------------------------------- #
# El índice de MySQL no lee código                                             #
# --------------------------------------------------------------------------- #
class _Resultado:
    def __init__(self, filas):
        self._filas = filas

    def fetchall(self):
        return list(self._filas)


class _ConexionQueRegistra:
    """Responde nombres y registra el SQL: ninguna sentencia puede ser un ``SHOW CREATE``."""

    def __init__(self, filas):
        self._filas = filas
        self.sql: list[str] = []
        self.params: list[dict | None] = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        self.params.append(params)
        return _Resultado(self._filas)


def test_mysql_routine_and_trigger_names_come_from_information_schema_without_show_create():
    adapter = MySQLAdapter(target=None)
    conn_rutinas = _ConexionQueRegistra([("calcular",), ("recalcular",)])
    conn_triggers = _ConexionQueRegistra([("trg_auditoria",)])

    rutinas = adapter.list_routine_names(conn_rutinas, "core_cliente1", "core_cliente1")
    triggers = adapter.list_trigger_names(conn_triggers, "core_cliente1", "core_cliente1")

    assert rutinas == ["calcular", "recalcular"] and triggers == ["trg_auditoria"]
    for conn in (conn_rutinas, conn_triggers):
        assert len(conn.sql) == 1
        assert "SHOW CREATE" not in conn.sql[0].upper()
        assert "information_schema" in conn.sql[0]
        assert conn.params == [{"db": "core_cliente1"}], "la base va como parámetro enlazado"
    assert "ROUTINES" in conn_rutinas.sql[0] and "TRIGGERS" in conn_triggers.sql[0]


def test_the_default_name_hooks_reuse_the_snapshot_of_engines_without_show_create():
    """
    PostgreSQL trae el cuerpo en la misma consulta del snapshot, así que el default de la base
    conserva los nombres del snapshot (no hay un ``SHOW CREATE`` por objeto que ahorrar).
    """
    from app.services.db_admin.base_adapter import ServerAdapter

    adapter_minimo = SimpleNamespace(
        _snapshot_routines=lambda conn, database, schema: [
            SimpleNamespace(name="f1"),
            SimpleNamespace(name="f2"),
        ],
        _snapshot_triggers=lambda conn, database, schema: [SimpleNamespace(name="t1")],
    )

    assert ServerAdapter.list_routine_names(adapter_minimo, None, "db", "db") == ["f1", "f2"]
    assert ServerAdapter.list_trigger_names(adapter_minimo, None, "db", "db") == ["t1"]


# --------------------------------------------------------------------------- #
# routines_not_visible: motor que puede ocultarlas O índice con cero rutinas   #
# --------------------------------------------------------------------------- #
_CODIGO_RUTINAS_NO_VISIBLES = "mcp.warn.routines_not_visible"
_INDICE_SIN_RUTINAS = {**_INDICE_CON_TODO, "routine": []}


def _avisos(sobre) -> dict:
    return {w["code"]: w["message"] for w in sobre["warnings"]}


def test_zero_routines_listed_on_a_modern_engine_warns_without_claiming_certainty(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_SIN_RUTINAS, version_motor="11.8.3-MariaDB")

    sobre = _ok(actor, database_id)

    mensaje = _avisos(sobre)[_CODIGO_RUTINAS_NO_VISIBLES]
    assert "Cero rutinas listadas" in mensaje and "puede que no existan" in mensaje


def test_zero_routines_warns_even_without_the_definitions_scope(
    admin_client, monkeypatch, motor_falso
):
    """El cero del índice no depende del scope: la cuenta sigue sin ver rutinas."""
    actor, database_id = _escenario(admin_client, monkeypatch, scopes=_SCOPES_SIN_DEFINICIONES)
    _facade(motor_falso, indice=_INDICE_SIN_RUTINAS)

    sobre = _ok(actor, database_id)

    assert _CODIGO_RUTINAS_NO_VISIBLES in _avisos(sobre)


def test_zero_routines_does_not_warn_when_kinds_leave_routines_out(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_SIN_RUTINAS)

    sobre = _ok(actor, database_id, kinds=["table", "view"])

    assert _CODIGO_RUTINAS_NO_VISIBLES not in _avisos(sobre)


def test_zero_routines_warns_when_kinds_include_routine(admin_client, monkeypatch, motor_falso):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_SIN_RUTINAS)

    sobre = _ok(actor, database_id, kinds=["routine"])

    assert _CODIGO_RUTINAS_NO_VISIBLES in _avisos(sobre)


def test_a_name_prefix_that_filters_out_every_routine_is_not_zero_routines(
    admin_client, monkeypatch, motor_falso
):
    """Con rutinas en el índice, un prefijo que no coincide no dispara el aviso del cero."""
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor="8.0.36")

    sobre = _ok(actor, database_id, name_prefix="zzz")

    assert _CODIGO_RUTINAS_NO_VISIBLES not in _avisos(sobre)


def test_an_engine_that_may_hide_routines_keeps_its_own_message_even_with_zero_listed(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_SIN_RUTINAS, version_motor="10.6.12-MariaDB")

    sobre = _ok(actor, database_id)

    avisos = [w for w in sobre["warnings"] if w["code"] == _CODIGO_RUTINAS_NO_VISIBLES]
    assert len(avisos) == 1
    assert "puede ocultar rutinas" in avisos[0]["message"]


def test_the_engine_hint_warns_for_a_filtered_listing_that_includes_routines(
    admin_client, monkeypatch, motor_falso
):
    actor, database_id = _escenario(admin_client, monkeypatch)
    _facade(motor_falso, indice=_INDICE_CON_TODO, version_motor="10.6.12-MariaDB")

    sobre = _ok(actor, database_id, kinds=["routine"])

    assert "puede ocultar rutinas" in _avisos(sobre)[_CODIGO_RUTINAS_NO_VISIBLES]


@pytest.mark.parametrize(
    "engine, kinds, oculta, cantidad, esperado",
    [
        ("postgresql", ("routine",), True, 0, False),
        ("mysql", ("table",), True, 0, False),
        ("mysql", ("routine",), False, 3, False),
        ("mysql", ("routine",), False, 0, True),
        ("mariadb", ("table", "routine"), True, 3, True),
    ],
)
def test_the_routines_not_visible_decision_table(engine, kinds, oculta, cantidad, esperado):
    from app.mcp.tools.catalog import _routines_not_visible_warning

    aviso = _routines_not_visible_warning(
        engine=engine,
        kinds=kinds,
        engine_may_hide_routines=oculta,
        routines_listed_count=cantidad,
    )

    assert (aviso is not None) is esperado
