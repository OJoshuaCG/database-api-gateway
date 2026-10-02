"""
Enmascarado de literales del historial de la consola SQL (``sql_masking.mask_literals``).

Módulo puro: sin BD ni motor. Lo que se mide es que NINGÚN dato del lote sobreviva y que la
estructura (palabras clave, identificadores, funciones) sí.
"""

import logging

import pytest

from app.services.db_admin import sql_masking
from app.services.db_admin.sql_masking import _scan_mask, mask_literals


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "postgresql"])
def test_insert_values_con_email_enmascara_cada_valor(engine):
    out = mask_literals(
        "INSERT INTO clientes (email, edad) VALUES ('alice@x.com', 34), ('bob@y.org', 51)",
        engine,
    )
    assert "alice" not in out and "bob" not in out and "34" not in out and "51" not in out
    assert "INSERT INTO clientes" in out
    assert "VALUES (?, ?), (?, ?)" in out


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "postgresql"])
def test_update_set_con_numeros(engine):
    out = mask_literals(
        "UPDATE cuentas SET saldo = 1500.25, nivel = -3 WHERE id = 42", engine
    )
    assert out == "UPDATE cuentas SET saldo = ?, nivel = -? WHERE id = ?"


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "postgresql"])
def test_where_in_list(engine):
    out = mask_literals(
        "SELECT nombre FROM personas WHERE dni IN ('30111222', '27999000', 5)", engine
    )
    assert out == "SELECT nombre FROM personas WHERE dni IN (?, ?, ?)"


def test_string_con_comilla_escapada_mysql():
    out = mask_literals("SELECT * FROM t WHERE apellido = 'O\\'Brien' OR x = 'it''s'", "mysql")
    assert "Brien" not in out
    assert out == "SELECT * FROM t WHERE apellido = ? OR x = ?"


def test_string_con_comilla_escapada_postgres():
    out = mask_literals("SELECT * FROM t WHERE apellido = 'O''Brien' OR b = E'a\\'b'", "postgresql")
    assert "Brien" not in out
    assert out == "SELECT * FROM t WHERE apellido = ? OR b = ?"


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_comentarios_con_datos_se_descartan(engine):
    out = mask_literals(
        "-- cliente juan@x.com, dni 30111222\n"
        "UPDATE t SET a = 1 /* tarjeta 4111111111111111 */ WHERE id = 2",
        engine,
    )
    assert "juan" not in out and "30111222" not in out and "4111" not in out
    assert out == "UPDATE t SET a = ? WHERE id = ?"


def test_hex_bit_national_e_introducer_mysql():
    out = mask_literals(
        "SELECT x'1F', 0x1F, b'101', N'abc', _utf8mb4'z' FROM t1 LIMIT 10", "mysql"
    )
    assert out == "SELECT ?, ?, ?, ?, ? FROM t1 LIMIT ?"


def test_postgres_dollar_quoting_fechas_y_parametros_posicionales():
    out = mask_literals(
        "UPDATE t SET b = $$secreto$$, e = DATE '2020-01-01' WHERE p = $1", "postgresql"
    )
    assert "secreto" not in out and "2020" not in out
    # ``$1`` es un marcador posicional, no un dato.
    assert out == "UPDATE t SET b = ?, e = CAST(? AS DATE) WHERE p = $1"


def test_identificadores_y_parametros_de_tipo_se_conservan():
    out = mask_literals(
        "CREATE TABLE `t1` (a VARCHAR(255) DEFAULT 'zz', b DECIMAL(10,2))", "mysql"
    )
    assert out == "CREATE TABLE `t1` (a VARCHAR(255) DEFAULT ?, b DECIMAL(10, 2))"


def test_postgres_identificadores_entre_comillas_dobles_se_conservan():
    out = mask_literals('SELECT "Email" FROM "Clientes" WHERE "Email" = \'a@b.c\'', "postgresql")
    assert out == 'SELECT "Email" FROM "Clientes" WHERE "Email" = ?'


def test_mysql_comillas_dobles_son_strings():
    out = mask_literals('SELECT * FROM t WHERE email = "alice@x.com"', "mysql")
    assert "alice" not in out
    assert out == "SELECT * FROM t WHERE email = ?"


@pytest.mark.parametrize(
    "sql",
    [
        # Truncado al tope del historial: la comilla no cierra.
        "SELECT * FROM t WHERE email = 'alice@x.com AND dni = 3011",
        # Varias sentencias unidas por salto de línea, sin ``;`` (así las guarda el historial).
        "SELECT 1\nINSERT INTO t VALUES ('alice@x.com', 99)",
        # Basura que sqlglot no modela.
        "FROBNICATE 'alice@x.com' WITH 12345 # nota 777",
        # sqlglot lo parsea como ``exp.Command`` (cuerpo opaco).
        "ALTER USER 'alice'@'%' IDENTIFIED BY '***'",
    ],
)
def test_sql_no_parseable_cae_al_fallback_y_nunca_devuelve_el_crudo(sql):
    out = mask_literals(sql, "mysql")
    assert out != sql
    for dato in ("alice", "3011", "99", "12345", "777"):
        assert dato not in out


def test_fallback_conserva_estructura_y_descarta_comentarios():
    out = _scan_mask(
        "INSERT INTO t1 VALUES ('it''s', \"a\\\"b\", 12.5e3, 0xFF) # c 9\n/* x 8 */ `c1`",
        postgres=False,
    )
    assert out.split() == ["INSERT", "INTO", "t1", "VALUES", "(?,", "?,", "?,", "?)", "`c1`"]


def test_fallback_postgres_dollar_y_prefijos():
    out = _scan_mask(
        "SELECT \"Col1\", $tag$ pii ; 'x' $tag$, E'y', X'1F' FROM t2 WHERE a=1", postgres=True
    )
    assert out == 'SELECT "Col1", ?, ?, ? FROM t2 WHERE a=?'


def test_un_fallo_inesperado_devuelve_marcador_y_no_el_crudo(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(sql_masking, "_mask_with_sqlglot", _boom)
    assert mask_literals("SELECT 'alice@x.com'", "mysql") == "?"


def test_no_loguea_la_sentencia_al_enmascarar(caplog):
    # sqlglot avisa "contains unsupported syntax" con la sentencia COMPLETA; leer el historial
    # no puede llevar esos literales al log del gateway.
    with caplog.at_level(logging.WARNING):
        mask_literals("ALTER USER 'alice'@'%' IDENTIFIED BY '***'", "mysql")
    assert "alice" not in caplog.text


def test_vacio():
    assert mask_literals("", "mysql") == ""
    assert mask_literals(None, "mysql") == ""
