"""Seguridad de identificadores SQL: validación y quoting anti-inyección."""

import pytest

from app.exceptions import AppHttpException
from app.services.db_admin import identifiers as ident


@pytest.mark.parametrize("name", ["db", "users", "_tmp", "a1_b2", "Whatsapp", "X" * 63])
def test_valid_identifiers_pass(name):
    assert ident.validate_identifier(name, "mysql") == name


@pytest.mark.parametrize(
    "name",
    [
        "",                  # vacío
        "1db",               # empieza con dígito
        "a b",               # espacio
        "db;DROP",           # punto y coma
        "db-name",           # guion
        "db`x",              # backtick
        'db"x',              # comilla
        "db'x",              # comilla simple
        "schema.table",      # punto
        "X" * 64,            # excede 63
        "dröp",              # no-ASCII
    ],
)
def test_invalid_identifiers_rejected(name):
    with pytest.raises(AppHttpException) as exc:
        ident.validate_identifier(name, "mysql")
    assert exc.value.status_code == 422
    # El valor crudo NO debe filtrarse en el contexto del error.
    if name:  # la cadena vacía está trivialmente "contenida" en cualquier string
        assert name not in str(exc.value.context)


def test_quote_identifier_mysql_escapes_backtick():
    assert ident.quote_identifier("db", "mysql") == "`db`"
    assert ident.quote_identifier("a`b", "mysql") == "`a``b`"


def test_quote_identifier_postgres_escapes_doublequote():
    assert ident.quote_identifier("Tbl", "postgresql") == '"Tbl"'
    assert ident.quote_identifier('a"b', "postgresql") == '"a""b"'


def test_quote_string_literal_mysql():
    assert ident.quote_string_literal("a'b", "mysql") == "'a\\'b'"
    assert ident.quote_string_literal("a\\b", "mysql") == "'a\\\\b'"


def test_quote_string_literal_postgres():
    assert ident.quote_string_literal("a'b", "postgresql") == "'a''b'"
    assert ident.quote_string_literal("a\\b", "postgresql").startswith("E'")


def test_quote_string_literal_rejects_null_byte():
    with pytest.raises(AppHttpException) as exc:
        ident.quote_string_literal("a\x00b", "mysql")
    assert exc.value.status_code == 422


@pytest.mark.parametrize("host", ["%", "localhost", "10.0.0.1", "db.example.com"])
def test_valid_hosts(host):
    assert ident.validate_host(host) == host


@pytest.mark.parametrize("host", ["a host", "a;b", "a'b", ""])
def test_invalid_hosts(host):
    with pytest.raises(AppHttpException):
        ident.validate_host(host)


def test_privileges_validation():
    assert ident.validate_privileges("all privileges") == "ALL PRIVILEGES"
    assert ident.validate_privileges("SELECT, INSERT") == "SELECT, INSERT"
    with pytest.raises(AppHttpException):
        ident.validate_privileges("SELECT; DROP")
