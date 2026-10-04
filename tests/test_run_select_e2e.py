"""
E2E de ``run_select`` contra motores REALES (S24, S25, S27). Escrito, NUNCA ejecutado por quien lo
implementó: se SALTA solo si no hay motores alcanzables (mismos contenedores y puertos que
``tests/test_agent_query_e2e.py``, cuyo arnés reusa).

QUÉ SE FIJA, Y SOLO UN MOTOR REAL LO PRUEBA
-------------------------------------------
- **El motor ejecuta el RENDER de sqlglot del árbol verificado** (``executed_sql``), no el texto del
  agente: formas legales (CTE, ``UNION``, subconsulta, ``OFFSET``, ``LIMIT`` propio) devuelven las filas
  esperadas en MySQL, MariaDB y PostgreSQL. Es la única verificación de que el render es válido para el
  motor real y significa lo mismo que lo que escribió el agente.
- **Un ``LIMIT`` propio ``<=`` tope se respeta; uno mayor se reemplaza por el tope + 1** (acotado de
  verdad en el motor).
- **Los hostiles no llegan al motor**: el validador los rechaza ANTES; el E2E lo confirma sobre el motor
  real (la tabla sigue intacta y no queda una sentencia viva).
- **El límite real sigue siendo el motor**: ver ``test_the_engine_itself_refuses_a_write_from_the_data_account``
  en ``tests/test_agent_query_e2e.py`` (mismo ``GRANT``, misma transacción READ ONLY).

El gate (kill switch, scope, opt-in, sonda) se cubre en ``tests/test_mcp_run_select.py``.
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import pytest
from sqlalchemy import text

from app.services.db_admin import agent_sql_policy as policy
from tests.test_agent_query_e2e import (  # noqa: F401
    _DB,
    _ENGINES,
    _db_engine,
    _ejecutar,
    _setup,
    _sin_auditoria,
    _teardown,
)

pytestmark = pytest.mark.integration

_FORMAS = [
    ("cte", "WITH c AS (SELECT id FROM items WHERE id < 3) SELECT id FROM c ORDER BY id", [[1], [2]]),
    ("union", "SELECT id FROM items WHERE id = 1 UNION SELECT id FROM items WHERE id = 3 ORDER BY id",
     [[1], [3]]),
    ("derived", "SELECT d.id FROM (SELECT id FROM items) AS d WHERE d.id >= 2 ORDER BY d.id",
     [[2], [3]]),
    ("offset", "SELECT id FROM items ORDER BY id LIMIT 2 OFFSET 1", [[2], [3]]),
    ("own_limit", "SELECT id FROM items ORDER BY id LIMIT 1", [[1]]),
    ("scalar_functions", "SELECT UPPER(name), LENGTH(name) FROM items WHERE id = 1", [["UNO", 3]]),
]


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_the_engine_executes_the_canonical_render_of_each_accepted_shape(engine_key):
    ctx = _setup(engine_key)
    try:
        for nombre, sql, esperado in _FORMAS:
            veredicto = policy.validate_agent_select(sql, engine=engine_key, database=_DB)
            assert veredicto.accepted, f"{nombre}: {veredicto.reasons}"
            sobre = _ejecutar(ctx, sql)
            assert sobre["data"]["rows"] == esperado, nombre
            assert sobre["executed_sql"] == veredicto.executed_sql, nombre
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_s24_a_select_without_limit_is_capped_in_the_engine_with_a_human_query(engine_key):
    ctx = _setup(engine_key)
    try:
        sobre = _ejecutar(ctx, "SELECT id FROM big ORDER BY id", max_rows=50)
        assert sobre["row_count"] == 50 and sobre["truncated"] is True
        assert sobre["executed_sql"].rstrip().endswith("LIMIT 51")
        assert "LIMIT" not in sobre["human_query"].upper()
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_an_own_limit_above_the_cap_is_replaced_by_the_cap_in_the_engine(engine_key):
    ctx = _setup(engine_key)
    try:
        sobre = _ejecutar(ctx, "SELECT id FROM big ORDER BY id LIMIT 2000", max_rows=20)
        assert sobre["row_count"] == 20 and sobre["truncated"] is True
        assert sobre["executed_sql"].rstrip().endswith("LIMIT 21")
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_s25_hostile_texts_are_rejected_before_the_engine_and_leave_it_untouched(engine_key):
    ctx = _setup(engine_key)
    try:
        hostiles = [
            "WITH d AS (DELETE FROM items RETURNING *) SELECT * FROM d",
            "SELECT 1; DELETE FROM items",
            "DELETE FROM items",
            "SELECT * FROM information_schema.tables",
            "SELECT 1 /*!50000 , 2 */",
        ]
        for sql in hostiles:
            veredicto = policy.validate_agent_select(sql, engine=engine_key, database=_DB)
            assert not veredicto.accepted and veredicto.executed_sql is None, sql
        with _db_engine(engine_key, ctx["spec"], _DB).connect() as c:
            assert c.execute(text("SELECT count(*) FROM items")).scalar() == 3
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_s27_a_huge_offset_is_refused_by_the_validator_with_no_scan(engine_key):
    veredicto = policy.validate_agent_select(
        "SELECT id FROM big LIMIT 10 OFFSET 10000000", engine=engine_key, database=_DB
    )
    assert not veredicto.accepted and "OFFSET_TOO_HIGH" in veredicto.reasons
