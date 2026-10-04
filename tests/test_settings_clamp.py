"""
Topes de las lecturas de datos del agente al CARGAR la configuración (D17, S17, S19).

``resolve_query_limits`` es PURA (recibe el mapeo de variables), así que se prueba con dicts: no hace
falta recargar ``app.core.environments`` ni tocar el entorno del proceso. El módulo la llama con
``os.environ`` y publica las constantes; el test de cierre verifica que lo publicado respeta los techos.
"""

import pytest

from app.core import environments as env
from app.core.environments import resolve_query_limits


def test_the_defaults_are_100_200_20s():
    limits, warnings = resolve_query_limits({})
    assert limits["MCP_QUERY_DEFAULT_ROWS"] == 100
    assert limits["MCP_QUERY_MAX_ROWS"] == 200
    assert limits["MCP_QUERY_TIMEOUT_MS"] == 20_000
    assert limits["MCP_QUERY_MAX_OFFSET"] == 10_000
    assert limits["MCP_QUERY_MAX_SQL_BYTES"] == 16_384
    assert limits["MCP_DATA_MAX_RESULT_BYTES"] == 131_072
    assert warnings == []


def test_s17_max_rows_900_is_clamped_to_the_500_ceiling_with_one_warning():
    limits, warnings = resolve_query_limits({"MCP_QUERY_MAX_ROWS": "900"})
    assert limits["MCP_QUERY_MAX_ROWS"] == 500
    assert len(warnings) == 1 and "MCP_QUERY_MAX_ROWS=900" in warnings[0]


def test_s19_timeout_60000_is_clamped_to_30000_with_one_warning():
    limits, warnings = resolve_query_limits({"MCP_QUERY_TIMEOUT_MS": "60000"})
    assert limits["MCP_QUERY_TIMEOUT_MS"] == 30_000
    assert len(warnings) == 1 and "MCP_QUERY_TIMEOUT_MS=60000" in warnings[0]


def test_the_default_rows_never_exceed_the_effective_max():
    limits, warnings = resolve_query_limits(
        {"MCP_QUERY_DEFAULT_ROWS": "400", "MCP_QUERY_MAX_ROWS": "150"}
    )
    assert limits["MCP_QUERY_DEFAULT_ROWS"] == 150 and limits["MCP_QUERY_MAX_ROWS"] == 150
    assert len(warnings) == 1


def test_a_default_above_the_ceiling_is_clamped_through_the_max():
    """900 / 900: el máximo cae a 500 y el default lo sigue: dos recortes, dos avisos."""
    limits, warnings = resolve_query_limits(
        {"MCP_QUERY_DEFAULT_ROWS": "900", "MCP_QUERY_MAX_ROWS": "900"}
    )
    assert limits["MCP_QUERY_MAX_ROWS"] == 500 and limits["MCP_QUERY_DEFAULT_ROWS"] == 500
    assert len(warnings) == 2


def test_values_at_or_below_the_ceiling_are_kept_and_never_raised():
    limits, warnings = resolve_query_limits(
        {"MCP_QUERY_MAX_ROWS": "500", "MCP_QUERY_TIMEOUT_MS": "30000", "MCP_QUERY_DEFAULT_ROWS": "5"}
    )
    assert limits["MCP_QUERY_MAX_ROWS"] == 500
    assert limits["MCP_QUERY_TIMEOUT_MS"] == 30_000
    assert limits["MCP_QUERY_DEFAULT_ROWS"] == 5
    assert warnings == []
    low, _ = resolve_query_limits({"MCP_QUERY_TIMEOUT_MS": "1500", "MCP_QUERY_MAX_ROWS": "10"})
    assert low["MCP_QUERY_TIMEOUT_MS"] == 1500 and low["MCP_QUERY_MAX_ROWS"] == 10


@pytest.mark.parametrize(
    "nombre", ["MCP_QUERY_MAX_ROWS", "MCP_QUERY_TIMEOUT_MS", "MCP_QUERY_DEFAULT_ROWS",
               "MCP_QUERY_MAX_OFFSET", "MCP_QUERY_MAX_SQL_BYTES", "MCP_DATA_MAX_RESULT_BYTES"]
)
@pytest.mark.parametrize("malo", ["0", "-5", "abc", "1.5"])
def test_a_non_positive_or_non_integer_value_refuses_to_start(nombre, malo):
    """``0`` en un timeout significaría «sin límite»: no es un valor, es un error de configuración."""
    with pytest.raises(ValueError) as exc:
        resolve_query_limits({nombre: malo})
    assert nombre in str(exc.value)


def test_blank_values_fall_back_to_the_default():
    limits, warnings = resolve_query_limits({"MCP_QUERY_MAX_ROWS": "  ", "MCP_QUERY_TIMEOUT_MS": ""})
    assert limits["MCP_QUERY_MAX_ROWS"] == 200 and limits["MCP_QUERY_TIMEOUT_MS"] == 20_000
    assert warnings == []


def test_what_the_module_publishes_respects_the_ceilings():
    assert 1 <= env.MCP_QUERY_MAX_ROWS <= env.MCP_QUERY_ROWS_CEILING == 500
    assert 1 <= env.MCP_QUERY_DEFAULT_ROWS <= env.MCP_QUERY_MAX_ROWS
    assert 1 <= env.MCP_QUERY_TIMEOUT_MS <= env.MCP_QUERY_TIMEOUT_CEILING_MS == 30_000


def test_the_result_budget_leaves_room_under_the_dispatch_hard_cap():
    """Se afirma al importar el despacho; acá se fija el valor y la relación."""
    from app.mcp import dispatch

    assert env.MCP_DATA_MAX_RESULT_BYTES <= dispatch.MAX_RESULT_BYTES // 2


def test_every_new_variable_is_documented_in_env_example():
    import pathlib

    ejemplo = (pathlib.Path(env.__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    for nombre in (
        "MCP_QUERY_DEFAULT_ROWS",
        "MCP_QUERY_MAX_ROWS",
        "MCP_QUERY_TIMEOUT_MS",
        "MCP_QUERY_MAX_OFFSET",
        "MCP_QUERY_MAX_SQL_BYTES",
        "MCP_DATA_MAX_RESULT_BYTES",
    ):
        assert f"\n{nombre}=" in ejemplo, nombre
