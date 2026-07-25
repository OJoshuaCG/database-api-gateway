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
# Cuerpos procedurales con ``;`` internos (MySQL/MariaDB)                      #
# --------------------------------------------------------------------------- #
# Regresión: un CREATE PROCEDURE con BEGIN...END se cortaba en su primer ``;`` interno
# (normalmente el DECLARE), y el motor rechazaba el fragmento con
# (1064, "...syntax... near '' at line N").
_SP = """CREATE PROCEDURE `sp_access`(IN `p_type` ENUM('USER','API'))
    READS SQL DATA
    COMMENT 'dos result sets'
BEGIN
    -- comentario; con punto y coma
    DECLARE v_module_id INT DEFAULT NULL;
    SET v_module_id = (SELECT id FROM m WHERE r = 'x');
    SELECT CASE WHEN a = 1 THEN 1 ELSE 0 END AS flag FROM t;
    IF p_type = 'USER' THEN
        SELECT 1;
    ELSEIF p_type = 'API' THEN
        SELECT 2;
    ELSE
        SELECT 0;
    END IF;
    CASE p_type WHEN 'USER' THEN SELECT 'u'; ELSE SELECT 'x'; END CASE;
END"""


def test_split_keeps_mysql_procedure_body_intact():
    parts = split_sql_statements(f"ALTER TABLE `t` ADD COLUMN `c` INT;\n{_SP};\nSELECT 1")
    assert len(parts) == 3
    assert parts[1] == _SP
    # El fallo historico truncaba justo en el DECLARE.
    assert not parts[1].rstrip().endswith("DEFAULT NULL")
    assert parts[1].rstrip().endswith("END")


def test_split_keeps_function_trigger_and_event_bodies_intact():
    for sql in (
        "CREATE FUNCTION f() RETURNS INT DETERMINISTIC BEGIN DECLARE x INT; "
        "SET x = 1; RETURN x; END",
        "CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW BEGIN INSERT INTO l VALUES (1); "
        "UPDATE c SET n = n + 1; END",
        "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN DELETE FROM t; DELETE FROM u; END",
    ):
        parts = split_sql_statements(f"{sql};\nSELECT 1")
        assert parts == [sql, "SELECT 1"]


def test_split_handles_definer_clause():
    for definer in ("`root`@`localhost`", "'root'@'%'"):
        sql = f"CREATE DEFINER={definer} PROCEDURE p() BEGIN DECLARE v INT; SET v = 1; END"
        assert split_sql_statements(f"{sql};\nSELECT 1") == [sql, "SELECT 1"]


def test_split_handles_nested_blocks_and_loops():
    sql = (
        "CREATE PROCEDURE p() BEGIN DECLARE i INT DEFAULT 0; "
        "WHILE (i < 10) DO SET i = i + 1; END WHILE; "
        "lbl: LOOP SET i = i + 1; LEAVE lbl; END LOOP; "
        "REPEAT SET i = i - 1; UNTIL i = 0 END REPEAT; "
        "BEGIN DECLARE j INT; SET j = 1; END; END"
    )
    assert split_sql_statements(f"{sql};\nSELECT 1") == [sql, "SELECT 1"]


def test_split_does_not_treat_if_and_repeat_functions_as_blocks():
    """``IF(a,b,c)`` y ``REPEAT('x',3)`` son FUNCIONES, no aperturas de bloque."""
    sql = "CREATE PROCEDURE p() BEGIN SELECT IF(a = 1, 'x', 'y'), REPEAT('ab', 3) FROM t; END"
    assert split_sql_statements(f"{sql};\nSELECT 1") == [sql, "SELECT 1"]


def test_split_trigger_without_begin_block_ends_at_semicolon():
    sql = "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET NEW.a = 1"
    assert split_sql_statements(f"{sql};\nSELECT 1") == [sql, "SELECT 1"]


def test_split_transaction_begin_outside_routine_is_not_a_block():
    """Fuera de una rutina, ``BEGIN`` abre una TRANSACCIÓN: no debe pegar el script."""
    parts = split_sql_statements("BEGIN; UPDATE t SET a = 1; COMMIT; SELECT 1")
    assert parts == ["BEGIN", "UPDATE t SET a = 1", "COMMIT", "SELECT 1"]


def test_split_case_expression_outside_routine_is_not_a_block():
    parts = split_sql_statements("SELECT CASE WHEN a = 1 THEN 1 ELSE 0 END FROM t; SELECT 1")
    assert parts == ["SELECT CASE WHEN a = 1 THEN 1 ELSE 0 END FROM t", "SELECT 1"]


# --------------------------------------------------------------------------- #
# Directiva DELIMITER (dumps de mysqldump / export del gateway)                #
# --------------------------------------------------------------------------- #
def test_split_honors_delimiter_directive():
    sql = (
        "DELIMITER $$\n"
        "CREATE PROCEDURE p() BEGIN DECLARE v INT; SET v = 1; END$$\n"
        "DELIMITER ;\n"
        "SELECT 1"
    )
    parts = split_sql_statements(sql)
    assert len(parts) == 2
    # La directiva es del CLIENTE: nunca se envia al motor.
    assert not any("DELIMITER" in p for p in parts)
    assert parts[0].startswith("CREATE PROCEDURE")
    assert parts[1] == "SELECT 1"


def test_split_delimiter_with_several_routines():
    sql = (
        "DELIMITER //\n"
        "CREATE PROCEDURE a() BEGIN SET @x = 1; END//\n"
        "CREATE PROCEDURE b() BEGIN SET @y = 2; END//\n"
        "DELIMITER ;\n"
        "SELECT 1"
    )
    parts = split_sql_statements(sql)
    assert len(parts) == 3
    assert not any("DELIMITER" in p for p in parts)


# --------------------------------------------------------------------------- #
# PostgreSQL                                                                   #
# --------------------------------------------------------------------------- #
def test_split_respects_pg_dollar_quoting_with_tag():
    sql = (
        "CREATE FUNCTION f() RETURNS int AS $body$ BEGIN RETURN 1; END; $body$ "
        "LANGUAGE plpgsql"
    )
    assert split_sql_statements(f"{sql};\nSELECT 1") == [sql, "SELECT 1"]


def test_split_pg_begin_atomic_body_is_kept_intact():
    """
    SQL/PSM de PostgreSQL 14+: el cuerpo va en ``BEGIN ATOMIC … END`` SIN dollar-quoting,
    asi que antes tambien se partia mal (no era un problema exclusivo de MySQL).
    """
    sql = "CREATE FUNCTION f() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT 1; END"
    assert split_sql_statements(f"{sql};\nSELECT 1") == [sql, "SELECT 1"]


def test_split_pg_do_block():
    parts = split_sql_statements("DO $$ BEGIN PERFORM 1; END $$; SELECT 1")
    assert len(parts) == 2


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
