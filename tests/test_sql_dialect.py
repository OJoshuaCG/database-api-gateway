"""Tests unitarios de split_sql_statements, SqlTranslator y RollbackGenerator."""

from app.models.enums import EngineType
from app.services.db_admin.sql_dialect import (
    RollbackGenerator,
    SqlTranslator,
    split_sql_statements,
)


# --------------------------------------------------------------------------- #
# split_sql_statements                                                         #
# --------------------------------------------------------------------------- #
def test_split_basic_two_statements():
    parts = split_sql_statements("CREATE TABLE a (id INT); CREATE TABLE b (id INT)")
    assert parts == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]


def test_split_ignores_semicolon_in_string_literal():
    parts = split_sql_statements("INSERT INTO t VALUES ('a;b'); SELECT 1")
    assert parts == ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]


def test_split_respects_block_comment():
    parts = split_sql_statements("CREATE TABLE a (id INT /* x; y */); SELECT 1")
    assert len(parts) == 2
    assert parts[0] == "CREATE TABLE a (id INT /* x; y */)"


def test_split_respects_pg_dollar_quoting():
    sql = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql; SELECT 1"
    parts = split_sql_statements(sql)
    assert len(parts) == 2
    assert "BEGIN RETURN 1; END;" in parts[0]


def test_split_trailing_semicolon_and_empty():
    assert split_sql_statements("CREATE TABLE a (id INT);") == ["CREATE TABLE a (id INT)"]
    assert split_sql_statements("   ;  ;  ") == []


# --------------------------------------------------------------------------- #
# SqlTranslator                                                                #
# --------------------------------------------------------------------------- #
def test_translate_mysql_is_passthrough():
    t = SqlTranslator()
    sql = "CREATE TABLE x (id INT AUTO_INCREMENT PRIMARY KEY)"
    assert t.translate(sql, EngineType.mysql) == sql
    assert t.translate(sql, EngineType.mariadb) == sql


def test_translate_to_postgres_converts_autoincrement():
    t = SqlTranslator()
    out = t.translate("CREATE TABLE x (id INT AUTO_INCREMENT PRIMARY KEY)", EngineType.postgresql)
    assert out is not None
    assert "AUTO_INCREMENT" not in out
    assert "IDENTITY" in out or "SERIAL" in out


def test_translate_all_includes_both_engines():
    out = SqlTranslator().translate_all("ALTER TABLE x ADD COLUMN y VARCHAR(10)")
    assert set(out) == {"mysql", "postgresql"}


def test_translate_invalid_sql_returns_none():
    assert SqlTranslator().translate("THIS IS NOT SQL @@@", EngineType.postgresql) is None


# --------------------------------------------------------------------------- #
# RollbackGenerator                                                            #
# --------------------------------------------------------------------------- #
def test_rollback_create_table():
    assert RollbackGenerator().generate("CREATE TABLE users (id INT)") == \
        "DROP TABLE IF EXISTS users;"


def test_rollback_add_column():
    assert RollbackGenerator().generate("ALTER TABLE users ADD COLUMN phone VARCHAR(20)") == \
        "ALTER TABLE users DROP COLUMN phone;"


def test_rollback_create_index_includes_table():
    out = RollbackGenerator().generate("CREATE INDEX idx_total ON orders(total)")
    assert out == "DROP INDEX idx_total ON orders;"


def test_rollback_multi_statement_reversed_order():
    out = RollbackGenerator().generate(
        "CREATE TABLE a (id INT); CREATE INDEX i ON a(id)"
    )
    # El rollback invierte el orden: primero el índice, luego la tabla.
    assert out == "DROP INDEX i ON a;\nDROP TABLE IF EXISTS a;"


def test_rollback_none_for_destructive():
    g = RollbackGenerator()
    assert g.generate("DROP TABLE x") is None
    assert g.generate("DELETE FROM t WHERE id=1") is None
    assert g.generate("UPDATE t SET a=1") is None
    assert g.generate("INSERT INTO t VALUES (1)") is None


def test_rollback_none_if_any_statement_irreversible():
    # Una aditiva + una destructiva => None (no rollback parcial).
    assert RollbackGenerator().generate(
        "CREATE TABLE a (id INT); DROP TABLE b"
    ) is None
