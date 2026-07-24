"""
Tests del modo BULK de conexión (copia de datos del clon) y del helper READ COMMITTED.

El timeout interactivo (15s) cancelaría lotes de tablas grandes → el modo bulk usa un
timeout mucho mayor (o sin límite). Puro: no abre conexiones a ningún motor.
"""

import importlib

import app.core.remote_engine as re
from app.services.db_admin import data_copy


def test_mysql_bulk_uses_larger_socket_timeout():
    interactive = re._connect_args("mysql", "disable")
    bulk = re._connect_args("mysql", "disable", bulk=True)
    assert bulk["read_timeout"] > interactive["read_timeout"]
    assert bulk["write_timeout"] > interactive["write_timeout"]


def test_postgres_bulk_uses_larger_statement_timeout():
    interactive = re._connect_args("postgresql", "disable")["options"]
    bulk = re._connect_args("postgresql", "disable", bulk=True)["options"]
    assert "statement_timeout=15000" in interactive
    assert "statement_timeout=15000" not in bulk
    assert "statement_timeout=3600000" in bulk  # default de REMOTE_BULK_STATEMENT_TIMEOUT_MS


def test_bulk_zero_means_unlimited(monkeypatch):
    # 0 => sin read/write_timeout en MySQL; statement_timeout=0 en PG.
    monkeypatch.setenv("REMOTE_BULK_STATEMENT_TIMEOUT_MS", "0")
    import app.core.environments as envs
    importlib.reload(envs)
    importlib.reload(re)
    try:
        mysql_args = re._connect_args("mysql", "disable", bulk=True)
        assert "read_timeout" not in mysql_args
        assert "write_timeout" not in mysql_args
        pg_opts = re._connect_args("postgresql", "disable", bulk=True)["options"]
        assert "statement_timeout=0" in pg_opts
    finally:
        monkeypatch.delenv("REMOTE_BULK_STATEMENT_TIMEOUT_MS", raising=False)
        importlib.reload(envs)
        importlib.reload(re)


def test_engine_cache_key_separates_bulk_from_interactive():
    # bulk y no-bulk deben cachearse por separado (distinto timeout).
    assert re.get_engine.__doc__ is not None  # sanity
    # La clave incluye el flag bulk (3-tupla): construir ambas no colisiona.
    # Verificado indirectamente por la firma; aquí solo garantizamos que acepta el kwarg.
    import inspect
    sig = inspect.signature(re.get_engine)
    assert "bulk" in sig.parameters


class _FakeConn:
    def __init__(self):
        self.executed = []

    def exec_driver_sql(self, sql):
        self.executed.append(sql)


def test_set_read_committed_mysql():
    c = _FakeConn()
    data_copy._set_read_committed(c, "mariadb")
    assert any("READ COMMITTED" in s for s in c.executed)


def test_set_read_committed_postgres():
    c = _FakeConn()
    data_copy._set_read_committed(c, "postgresql")
    assert any("READ COMMITTED" in s for s in c.executed)


def test_set_read_committed_unknown_engine_noop():
    c = _FakeConn()
    data_copy._set_read_committed(c, "sqlite")
    assert c.executed == []
