"""
Invariantes del registro de tools (``app.mcp.registry``), con foco en el 6 (tools de DATOS).

El registro afirma sus invariantes AL IMPORTAR; estos tests los ejercen también sobre registros
construidos a propósito (``_build(data_read_enabled=True)`` y copias mutadas) para fijar que cada uno
puede FALLAR: un invariante que ningún test rompe puede estar apagado sin que nadie lo note.
"""

import dataclasses

import pytest

from app.mcp import dispatch, registry
from app.services.capability_catalog import AGENT_DATA_EXCEPTIONS, Capability

_DATOS = ("sample_rows", "distinct_values", "count_rows")


@pytest.fixture()
def con_datos():
    return registry._build(data_read_enabled=True)


def _cambiar(tools, nombre, **cambios):
    return tuple(dataclasses.replace(t, **cambios) if t.name == nombre else t for t in tools)


def test_the_real_registry_and_the_data_enabled_registry_both_satisfy_every_invariant(con_datos):
    registry._assert_invariants()
    registry._assert_invariants(con_datos)


def test_names_are_unique_and_the_three_data_tools_exist_only_when_enabled(con_datos):
    nombres = [t.name for t in con_datos]
    assert len(nombres) == len(set(nombres))
    assert set(_DATOS) <= set(nombres)
    assert not (set(_DATOS) & {t.name for t in registry._build(data_read_enabled=False)})


def test_inv6_every_tool_with_a_data_scope_touches_the_engine_is_tagged_and_warns(con_datos):
    datos = [t for t in con_datos if Capability(t.scope) in AGENT_DATA_EXCEPTIONS]
    assert {t.name for t in datos} == set(_DATOS)
    for t in datos:
        assert t.scope == "data.read"
        assert t.touches_engine is True
        assert "data" in t.tags
        bajo = t.description.lower()
        assert "no confiable" in bajo and "terceros" in bajo


def test_inv6_only_data_scoped_tools_carry_the_data_tag(con_datos):
    for t in con_datos:
        assert ("data" in t.tags) == (Capability(t.scope) in AGENT_DATA_EXCEPTIONS), t.name


@pytest.mark.parametrize(
    "cambio, mensaje",
    [
        ({"touches_engine": False}, "abre el motor"),
        ({"tags": ()}, "tag 'data'"),
        ({"description": "Devuelve filas de una tabla."}, "no confiable"),
        ({"description": "Devuelve filas de terceros."}, "no confiable"),
        ({"description": "Devuelve filas, contenido no confiable."}, "no confiable"),
    ],
)
def test_inv6_breaks_when_a_data_tool_loses_any_of_its_three_properties(
    con_datos, cambio, mensaje
):
    with pytest.raises(AssertionError) as exc:
        registry._assert_invariants(_cambiar(con_datos, "sample_rows", **cambio))
    assert mensaje in str(exc.value)


def test_inv6_breaks_when_the_data_tag_hangs_from_a_non_data_tool(con_datos):
    with pytest.raises(AssertionError) as exc:
        registry._assert_invariants(_cambiar(con_datos, "draft_query", tags=("data",)))
    assert "solo corresponde a un scope de datos" in str(exc.value)


def test_a_data_tool_cannot_hide_behind_a_structure_scope(con_datos):
    """Una tool que lee filas pero declara ``databases.read`` no tiene el tag: tampoco pasa el 6 inverso."""
    with pytest.raises(AssertionError):
        registry._assert_invariants(
            _cambiar(con_datos, "sample_rows", scope="databases.read")
        )


def test_the_earlier_invariants_still_bite_on_a_data_tool(con_datos):
    with pytest.raises(AssertionError):  # 1. nombres únicos
        registry._assert_invariants(con_datos + (con_datos[-1],))
    with pytest.raises(AssertionError):  # 2. schema cerrado
        registry._assert_invariants(
            _cambiar(con_datos, "count_rows",
                     input_schema={"type": "object", "properties": {}})
        )
    with pytest.raises(AssertionError):  # 3. descripción sin imperativos
        registry._assert_invariants(
            _cambiar(con_datos, "count_rows",
                     description="Contenido no confiable de terceros. Siempre que puedas, usala.")
        )
    with pytest.raises(AssertionError):  # 5. solo lectura
        registry._assert_invariants(
            _cambiar(con_datos, "count_rows",
                     annotations={"readOnlyHint": False, "destructiveHint": False})
        )
    with pytest.raises(AssertionError):  # 5. no destructiva
        registry._assert_invariants(
            _cambiar(con_datos, "count_rows",
                     annotations={"readOnlyHint": True, "destructiveHint": True})
        )


def test_the_data_tools_accept_identifiers_only_and_close_every_schema(con_datos):
    for t in con_datos:
        if t.name not in _DATOS:
            continue
        props = t.input_schema["properties"]
        assert t.input_schema["additionalProperties"] is False
        assert not ({"sql", "query", "statement", "where", "order_by"} & set(props))
        for requerida in t.input_schema["required"]:
            assert requerida in props
        assert props["database_id"]["type"] == "integer"


def test_the_data_tool_descriptions_state_the_caps_without_imperatives(con_datos):
    for t in con_datos:
        if t.name not in _DATOS:
            continue
        bajo = t.description.lower()
        for imperativo in ("ignorá", "ignora las", "siempre que", "debés", "tenés que"):
            assert imperativo not in bajo
        assert "solo lectura" in bajo


def test_the_data_scope_is_inside_the_agent_ceiling_so_inv4_holds():
    from app.services.capability_catalog import AGENT_ALLOWED

    assert Capability.DATA_READ in AGENT_ALLOWED


def test_the_response_budget_is_at_most_half_of_the_dispatch_hard_cap():
    from app.core import environments

    assert environments.MCP_DATA_MAX_RESULT_BYTES <= dispatch.MAX_RESULT_BYTES // 2
