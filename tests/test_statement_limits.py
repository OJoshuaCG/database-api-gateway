"""
``statement_limits`` (timeouts de sesión extraídos de ``query_runner``) y el ``session_hook`` de
``run_statements``.

Lo que se fija: la extracción NO cambió lo que la consola emite (mismas sentencias, mismo orden), el
hook corre DESPUÉS de preparar la sesión (READ ONLY ya abierta) y ANTES de la primera sentencia, sin
hook nada cambia, y una excepción del hook corta la ejecución sin correr ninguna sentencia.
``tests/test_query_runner_execution.py`` (sin modificar) sigue siendo el contrato de la consola.
"""

import inspect

import pytest

from app.services.db_admin import query_policy as qp
from app.services.db_admin import query_runner as qr
from app.services.db_admin import statement_limits as sl
from tests.test_query_runner_execution import (  # noqa: F401 — dobles del arnés de la consola
    _FakeConn,
    _FakeResult,
    _install_conn,
    _target,
)


def _plan(sql="SELECT 1"):
    return qp.StatementPlan(seq=1, sql=sql, kind="select", danger=qp.READ)


def _run(conn, monkeypatch, *, engine="mysql", **kw):
    _install_conn(monkeypatch, conn)
    return qr.run_statements(
        _target(engine),
        database="app",
        engine=engine,
        statements=[_plan()],
        credential=qr.QueryCredential(qr.MODE_STORED, "u", "p"),
        read_only=True,
        max_rows=10,
        max_cell_chars=100,
        timeout_ms=5000,
        **kw,
    )


def test_the_runner_uses_the_extracted_function_and_no_longer_defines_its_own():
    assert qr.apply_session_timeouts is sl.apply_session_timeouts
    assert not hasattr(qr, "_apply_statement_timeout")
    assert "apply_session_timeouts(conn, engine, timeout_ms)" in inspect.getsource(
        qr._prepare_session
    )


def test_mysql_family_gets_the_four_session_variables_in_the_same_order():
    conn = _FakeConn()
    sl.apply_session_timeouts(conn, "mysql", 5000)
    assert conn.calls == [
        "SET SESSION max_execution_time = 5000",
        "SET SESSION max_statement_time = 5.0",
        "SET SESSION lock_wait_timeout = 5",
        "SET SESSION innodb_lock_wait_timeout = 5",
    ]


@pytest.mark.parametrize("engine, timeout", [("postgresql", 5000), ("mysql", 0), ("mariadb", -1)])
def test_nothing_is_emitted_for_postgres_or_a_non_positive_timeout(engine, timeout):
    conn = _FakeConn()
    sl.apply_session_timeouts(conn, engine, timeout)
    assert conn.calls == []


def test_an_engine_that_rejects_a_variable_does_not_stop_the_others():
    from sqlalchemy.exc import SQLAlchemyError

    def _handler(sql):
        if "max_statement_time" in sql:
            raise SQLAlchemyError("unknown variable")
        return _FakeResult()

    conn = _FakeConn(_handler)
    sl.apply_session_timeouts(conn, "mariadb", 3000)
    assert len(conn.calls) == 4


def test_the_postgres_helper_sets_the_exact_timeout():
    conn = _FakeConn()
    sl.apply_postgres_statement_timeout(conn, 18_000)
    assert conn.calls == ["SET statement_timeout = 18000"]
    sl.apply_postgres_statement_timeout(conn, 0)
    assert len(conn.calls) == 1


def test_the_hook_runs_after_the_read_only_start_and_before_the_first_statement(monkeypatch):
    visto = []

    def hook(conn):
        visto.append(list(conn.calls))

    conn = _FakeConn()
    out = _run(conn, monkeypatch, session_hook=hook)

    assert out.success and out.rolled_back
    assert len(visto) == 1
    assert visto[0][-1] == "START TRANSACTION READ ONLY"
    assert "SELECT 1" not in visto[0]
    assert conn.calls[-1] == "SELECT 1"


def test_without_a_hook_the_emitted_statements_are_exactly_the_console_ones(monkeypatch):
    sin_hook = _FakeConn()
    _run(sin_hook, monkeypatch)
    con_hook_nulo = _FakeConn()
    _run(con_hook_nulo, monkeypatch, session_hook=None)

    assert sin_hook.calls == con_hook_nulo.calls
    assert sin_hook.calls[-2:] == ["START TRANSACTION READ ONLY", "SELECT 1"]
    assert sin_hook.rolled_back and not sin_hook.committed


def test_the_hook_can_issue_statements_on_the_same_connection(monkeypatch):
    conn = _FakeConn()
    _run(conn, monkeypatch, session_hook=lambda c: c.exec_driver_sql("SELECT CONNECTION_ID()"))
    assert conn.calls.index("SELECT CONNECTION_ID()") == conn.calls.index("SELECT 1") - 1


def test_a_failing_hook_aborts_before_any_statement_and_propagates(monkeypatch):
    conn = _FakeConn()

    def hook(_conn):
        raise RuntimeError("hook roto")

    with pytest.raises(RuntimeError):
        _run(conn, monkeypatch, session_hook=hook)
    assert "SELECT 1" not in conn.calls
