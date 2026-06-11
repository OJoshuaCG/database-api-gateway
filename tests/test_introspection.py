"""
Valida la lógica de introspección de `ServerAdapter` (columnas, PK, FK, índices,
detección de tabla inexistente) usando un `Inspector` REAL sobre SQLite.

No cubre el SQL específico de cada dialecto (list_databases/list_users) ni la
semántica de schema de MySQL/PostgreSQL: eso requiere un motor real.
"""

from contextlib import contextmanager

import pytest
import sqlalchemy as sa

import app.services.db_admin.base_adapter as base_adapter
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services.db_admin.base_adapter import ServerAdapter


class _SqliteTestAdapter(ServerAdapter):
    """Adapter mínimo para ejercitar la introspección de la base contra SQLite."""

    dialect = "sqlite"

    def _version_sql(self):
        return "SELECT sqlite_version()"

    def _inspect_schema(self, database):
        return None  # SQLite no usa schema

    def list_databases(self):
        return []

    def list_users(self):
        return []

    def create_database(self, *a, **k): ...
    def drop_database(self, *a, **k): ...
    def create_user(self, *a, **k): ...
    def drop_user(self, *a, **k): ...
    def change_password(self, *a, **k): ...
    def grant_database(self, *a, **k): ...
    def revoke_database(self, *a, **k): ...


@pytest.fixture()
def sqlite_adapter(tmp_path, monkeypatch):
    db_file = tmp_path / "introspect.db"
    engine = sa.create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE child ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " parent_id INTEGER NOT NULL,"
            " note TEXT,"
            " FOREIGN KEY(parent_id) REFERENCES parent(id))"
        )
        conn.exec_driver_sql("CREATE INDEX ix_child_note ON child(note)")

    @contextmanager
    def fake_db_conn(target, database):
        conn = engine.connect()
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(base_adapter, "database_connection", fake_db_conn)
    return _SqliteTestAdapter(ServerTarget(1, "sqlite", "h", 1, "u", "p"))


def test_list_tables(sqlite_adapter):
    assert sqlite_adapter.list_tables("main") == ["child", "parent"]


def test_get_table_schema_columns_and_pk(sqlite_adapter):
    schema = sqlite_adapter.get_table_schema("main", "child")
    assert schema.table == "child"
    names = {c.name for c in schema.columns}
    assert names == {"id", "parent_id", "note"}
    assert schema.primary_key == ["id"]
    id_col = next(c for c in schema.columns if c.name == "id")
    assert id_col.primary_key is True
    note_col = next(c for c in schema.columns if c.name == "note")
    assert note_col.nullable is True
    parent_col = next(c for c in schema.columns if c.name == "parent_id")
    assert parent_col.nullable is False


def test_get_table_schema_foreign_keys(sqlite_adapter):
    schema = sqlite_adapter.get_table_schema("main", "child")
    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.referred_table == "parent"
    assert fk.columns == ["parent_id"]
    assert fk.referred_columns == ["id"]


def test_get_table_schema_indexes(sqlite_adapter):
    schema = sqlite_adapter.get_table_schema("main", "child")
    index_names = {ix.name for ix in schema.indexes}
    assert "ix_child_note" in index_names


def test_get_table_schema_missing_table_404(sqlite_adapter):
    with pytest.raises(AppHttpException) as exc:
        sqlite_adapter.get_table_schema("main", "nope")
    assert exc.value.status_code == 404


def test_get_table_schema_rejects_bad_identifier(sqlite_adapter):
    with pytest.raises(AppHttpException) as exc:
        sqlite_adapter.get_table_schema("main", "bad;name")
    assert exc.value.status_code == 422
