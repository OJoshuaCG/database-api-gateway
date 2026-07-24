"""
Tests unitarios (sin motor real) de la detección de FKs cross-database:

``ServerAdapter.external_fk_dependents`` — usada por el clon para advertir, ANTES de
limpiar/dropear el destino (``clean_mode='objects'``/``'drop_database'``), si alguna
tabla de OTRA base de datos del mismo servidor tiene una FK hacia el destino. El
snapshot estructural es de una sola BD y nunca puede ver esto — es el candidato más
probable ante un ``DROP TABLE``/``DROP DATABASE`` que revienta con
``(1451, 'Cannot delete or update a parent row...')`` sin motivo aparente.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import app.services.db_admin.mysql_adapter as mysql_adapter_module
from app.controllers.clone_controller import CloneController
from app.services.db_admin.dtos import ExternalFkDependent
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter


class _FakeRow:
    def __init__(self, **kw):
        self._mapping = kw
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return _FakeResult(self.rows)


def _mysql_adapter():
    a = MySQLAdapter.__new__(MySQLAdapter)
    a.dialect = "mysql"
    a.target = object()
    return a


def _patch_server_connection(monkeypatch, fake_conn):
    @contextmanager
    def fake_server_connection(target):
        yield fake_conn

    monkeypatch.setattr(mysql_adapter_module, "server_connection", fake_server_connection)


def test_external_fk_dependents_maps_rows(monkeypatch):
    rows = [
        _FakeRow(
            schema_name="control_panel", table_name="accounts", column_name="server_id",
            constraint_name="fk_srv", referenced_table="db_servers", referenced_column="id",
        ),
    ]
    fake_conn = _FakeConn(rows)
    _patch_server_connection(monkeypatch, fake_conn)

    deps = _mysql_adapter().external_fk_dependents("target_db")

    assert len(deps) == 1
    d = deps[0]
    assert d.schema_name == "control_panel"
    assert d.table == "accounts"
    assert d.column == "server_id"
    assert d.constraint == "fk_srv"
    assert d.referenced_table == "db_servers"
    assert d.referenced_column == "id"

    # La consulta filtra parametrizado (:db), no por interpolación de string.
    sql, params = fake_conn.calls[0]
    assert params == {"db": "target_db"}
    assert "REFERENCED_TABLE_SCHEMA" in sql
    assert "TABLE_SCHEMA <> :db" in sql


def test_external_fk_dependents_empty_when_no_rows(monkeypatch):
    _patch_server_connection(monkeypatch, _FakeConn([]))
    assert _mysql_adapter().external_fk_dependents("target_db") == []


def test_base_adapter_default_has_no_cross_database_fks():
    """
    PostgreSQL no soporta FKs cross-database por arquitectura (una BD no puede
    referenciar tablas de otra) — hereda el default de ServerAdapter sin sobreescribirlo.
    """
    a = PostgresAdapter.__new__(PostgresAdapter)
    assert a.external_fk_dependents("any_db") == []


# --------------------------------------------------------------------------- #
# CloneController._external_fk_warnings                                       #
# --------------------------------------------------------------------------- #
class _FakeAdapterWithDeps:
    def __init__(self, deps):
        self.deps = deps

    def external_fk_dependents(self, database):
        return self.deps


def _job(*, target_mode="existing", clean_mode="objects", target_database_name="dst_db"):
    return SimpleNamespace(
        target_mode=target_mode, clean_mode=clean_mode,
        target_database_name=target_database_name,
    )


def _dep(schema="control_panel", table="accounts"):
    return ExternalFkDependent(
        schema_name=schema, table=table, column="server_id", constraint="fk_srv",
        referenced_table="db_servers", referenced_column="id",
    )


def test_external_fk_warning_emitted_for_clean_objects_on_existing_target():
    warnings = CloneController._external_fk_warnings(
        _FakeAdapterWithDeps([_dep()]), _job(target_mode="existing", clean_mode="objects"),
    )
    assert len(warnings) == 1
    assert "control_panel" in warnings[0]
    assert "db_servers" in warnings[0]
    assert "dst_db" in warnings[0]


def test_external_fk_warning_emitted_for_drop_database_on_existing_target():
    warnings = CloneController._external_fk_warnings(
        _FakeAdapterWithDeps([_dep()]), _job(target_mode="existing", clean_mode="drop_database"),
    )
    assert len(warnings) == 1


def test_external_fk_warning_empty_when_no_dependents():
    warnings = CloneController._external_fk_warnings(
        _FakeAdapterWithDeps([]), _job(target_mode="existing", clean_mode="objects"),
    )
    assert warnings == []


def test_external_fk_warning_skipped_for_new_target():
    """Un target 'new' no puede tener dependents (la BD todavía no existe)."""
    warnings = CloneController._external_fk_warnings(
        _FakeAdapterWithDeps([_dep()]), _job(target_mode="new", clean_mode="objects"),
    )
    assert warnings == []


def test_external_fk_warning_skipped_when_clean_mode_none():
    """clean_mode='none' no dropea nada — no hace falta advertir."""
    warnings = CloneController._external_fk_warnings(
        _FakeAdapterWithDeps([_dep()]), _job(target_mode="existing", clean_mode="none"),
    )
    assert warnings == []


def test_external_fk_warning_truncates_long_list():
    deps = [_dep(table=f"t{i}") for i in range(8)]
    warnings = CloneController._external_fk_warnings(
        _FakeAdapterWithDeps(deps), _job(target_mode="existing", clean_mode="objects"),
    )
    assert len(warnings) == 1
    assert "8 columna(s)" in warnings[0]
    assert "+3 más" in warnings[0]
