"""
SQL REAL del aprovisionamiento de la cuenta de solo lectura, sin motor.

A diferencia de ``test_readonly_provision.py`` (que fakea el adapter entero), acá corren los
métodos de verdad de ``MySQLAdapter``, ``MariaDBAdapter`` y ``PostgresAdapter``. Lo único falso es
la conexión: las lecturas del preflight salen de una conexión guionada que además REGISTRA cada
sentencia, y las mutaciones (``_execute_server`` / ``_execute_database``) se capturan en una lista
sin ejecutarse. Eso permite afirmar lo que más importa: qué sentencias salen, en qué orden, y que
ante una precondición fallida NO sale ninguna que mute.
"""

import pytest
from sqlalchemy.exc import OperationalError

from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services.db_admin import mysql_adapter as mysql_mod
from app.services.db_admin import postgres_adapter as pg_mod
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.identifiers import quote_identifier, quote_string_literal
from app.services.db_admin.mysql_adapter import MariaDBAdapter, MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter
from app.services.db_admin.readonly_probe import (
    MYSQL_SHOW_ROUTINE_MIN_VERSION,
    ReadonlyPreflight,
    mysql_global_grants_for_version,
    mysql_grant_violations,
    mysql_has_unrecognized_grants,
)

USER = "mcp_ro"
HOST = "10.0.%"
PWD = "pa'ss\\w0rd"


# --------------------------------------------------------------------------- #
# Dobles                                                                       #
# --------------------------------------------------------------------------- #


class _Res:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar

    def __iter__(self):
        return iter(self._rows)


class _Conn:
    """Conexión guionada: responde las lecturas del preflight y registra TODO lo que recibe."""

    def __init__(self, *, exists=False, version="8.0.36", grants=(), version_fails=False):
        self.log: list[str] = []
        self.exists = exists
        self.version = version
        self.grants = list(grants)
        self.version_fails = version_fails

    def execute(self, clause, params=None):
        sql = str(clause)
        self.log.append(sql)
        if sql.startswith("SELECT 1 FROM mysql.user") or sql.startswith(
            "SELECT 1 FROM pg_roles"
        ):
            return _Res(rows=[(1,)] if self.exists else [])
        if sql.startswith("SELECT VERSION()"):
            if self.version_fails:
                raise OperationalError("SELECT VERSION()", {}, Exception("boom"))
            return _Res(scalar=self.version)
        if sql.startswith("SHOW GRANTS FOR"):
            return _Res(rows=[(g,) for g in self.grants])
        raise AssertionError(f"el preflight emitió una sentencia inesperada: {sql}")


class _ConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __call__(self, _target):
        return self

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _target(dialect: str) -> ServerTarget:
    return ServerTarget(
        server_id=1, dialect=dialect, host="127.0.0.1", port=3306,
        admin_user="root", admin_password="x",
    )


def _mysql(monkeypatch, cls=MySQLAdapter, **conn_kwargs):
    """Adapter real de la familia MySQL + (conexión guionada, sentencias que mutan)."""
    conn = _Conn(**conn_kwargs)
    monkeypatch.setattr(mysql_mod, "server_connection", _ConnCtx(conn))
    adapter = cls(_target("mariadb" if cls is MariaDBAdapter else "mysql"))
    mutations: list[str] = []
    monkeypatch.setattr(
        adapter, "_execute_server", lambda stmts, **kw: mutations.extend(stmts)
    )
    return adapter, conn, mutations


def _pg(monkeypatch, **conn_kwargs):
    conn = _Conn(**conn_kwargs)
    monkeypatch.setattr(pg_mod, "server_connection", _ConnCtx(conn))
    adapter = PostgresAdapter(_target("postgresql"))
    mutations: list[tuple[str, str | None, str]] = []  # (nivel, base, sentencia)
    monkeypatch.setattr(
        adapter,
        "_execute_server",
        lambda stmts, **kw: mutations.extend(("server", None, s) for s in stmts),
    )
    monkeypatch.setattr(
        adapter,
        "_execute_database",
        lambda db, stmts, **kw: mutations.extend(("database", db, s) for s in stmts),
    )
    return adapter, conn, mutations


def _provision(adapter, databases, host=HOST, password=PWD):
    pre = adapter.preflight_readonly_account(USER, host)
    adapter.provision_readonly_account(USER, password, host, databases, pre)
    return pre


# --------------------------------------------------------------------------- #
# MySQL / MariaDB: orden y forma de las sentencias                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", [MySQLAdapter, MariaDBAdapter])
def test_statement_order_is_create_alter_revoke_then_grants(monkeypatch, cls):
    adapter, _conn, muts = _mysql(monkeypatch, cls=cls, version="8.0.36")
    _provision(adapter, ["app_prod", "otra"])
    assert muts[0].startswith("CREATE USER IF NOT EXISTS ")
    assert muts[1].startswith("ALTER USER ")
    assert muts[2].startswith("REVOKE ALL PRIVILEGES, GRANT OPTION FROM ")
    grants = muts[3:]
    assert grants[0].startswith("GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `app\\_prod`.* TO ")
    assert grants[1].startswith("GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `otra`.* TO ")
    assert all(g.startswith("GRANT ") for g in grants)


def test_username_host_and_password_are_quoted_with_the_existing_helpers(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch)
    _provision(adapter, ["app"])
    who = f"{quote_string_literal(USER, 'mysql')}@{quote_string_literal(HOST, 'mysql')}"
    pwd = quote_string_literal(PWD, "mysql")
    assert muts[0] == f"CREATE USER IF NOT EXISTS {who} IDENTIFIED BY {pwd}"
    assert muts[1] == f"ALTER USER {who} IDENTIFIED BY {pwd}"
    assert muts[2] == f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {who}"
    assert all(m.endswith(f"TO {who}") for m in muts[3:])
    # La comilla y el backslash de la contraseña salen escapados, nunca crudos.
    assert "pa''ss\\\\w0rd" in muts[0]
    assert "pa'ss" not in muts[0].replace("pa''ss", "")


def test_underscore_and_percent_in_database_names_are_escaped():
    from app.services.db_admin.mysql_adapter import MySQLAdapter as A

    assert A._db_grant_pattern("`app_prod`") == "`app\\_prod`"
    assert A._db_grant_pattern("`50%_x`") == "`50\\%\\_x`"
    # Los backticks se duplican en el quoting del identificador (y un nombre que los trae ni
    # siquiera llega: la whitelist lo rechaza antes, ver el test siguiente).
    assert quote_identifier("a`b", "mysql") == "`a``b`"


def test_a_database_name_with_a_backtick_never_reaches_the_sql(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch)
    pre = adapter.preflight_readonly_account(USER, HOST)
    with pytest.raises(AppHttpException) as exc:
        adapter.provision_readonly_account(USER, PWD, HOST, ["a`b; DROP USER x"], pre)
    assert exc.value.status_code == 422
    assert muts == []


def test_the_grants_never_include_grant_option_global_select_or_mysql_schema(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, version="8.0.36")
    _provision(adapter, ["app_prod", "otra"])
    grants = [m for m in muts if m.startswith("GRANT ")]
    assert grants
    for g in grants:
        assert "WITH GRANT OPTION" not in g.upper()
        assert " ON *.* " not in g or g.startswith("GRANT SHOW_ROUTINE ON *.* ")
        assert "mysql." not in g.lower()
    # Y la sonda acepta exactamente lo que se otorgó.
    assert mysql_grant_violations(grants) == []


# --------------------------------------------------------------------------- #
# SHOW_ROUTINE: solo MySQL >= 8.0.20                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls,version,esperado",
    [
        (MySQLAdapter, "8.0.36", True),
        (MySQLAdapter, "8.0.20", True),
        (MySQLAdapter, "8.4.0", True),
        (MySQLAdapter, "9.1.0", True),
        (MySQLAdapter, "8.0.19", False),
        (MySQLAdapter, "5.7.44", False),
        (MySQLAdapter, "5.5.5-10.6.12-MariaDB", False),
        (MySQLAdapter, "not-a-version", False),
        (MariaDBAdapter, "10.11.6-MariaDB", False),
        (MariaDBAdapter, "11.4.2-MariaDB-log", False),
    ],
)
def test_show_routine_is_granted_only_on_mysql_8_0_20_or_later(
    monkeypatch, cls, version, esperado
):
    adapter, _conn, muts = _mysql(monkeypatch, cls=cls, version=version)
    _provision(adapter, ["app"])
    emitido = any("SHOW_ROUTINE" in m for m in muts)
    assert emitido is esperado
    if esperado:
        assert muts[-1].startswith("GRANT SHOW_ROUTINE ON *.* TO ")


def test_show_routine_is_not_granted_when_the_version_is_unknown_and_it_is_noted(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, version=None)
    pre = _provision(adapter, ["app"])
    assert not any("SHOW_ROUTINE" in m for m in muts)
    assert pre.note and "no determinada" in pre.note


def test_show_routine_is_not_granted_when_reading_the_version_fails(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, version_fails=True)
    pre = _provision(adapter, ["app"])
    assert not any("SHOW_ROUTINE" in m for m in muts)
    assert pre.note


def test_the_version_rule_is_pure_and_pins_the_threshold():
    assert MYSQL_SHOW_ROUTINE_MIN_VERSION == (8, 0, 20)
    assert mysql_global_grants_for_version("8.0.20", dialect="mysql")[0] == ("SHOW_ROUTINE",)
    assert mysql_global_grants_for_version("8.0.19", dialect="mysql")[0] == ()
    assert mysql_global_grants_for_version("8.0.36", dialect="mariadb")[0] == ()


# --------------------------------------------------------------------------- #
# Precondiciones: ninguna mutación si el preflight rechaza                      #
# --------------------------------------------------------------------------- #

_MUTANTES = ("CREATE", "ALTER", "REVOKE", "GRANT", "DROP", "SET", "INSERT", "UPDATE", "DELETE")


def _solo_lecturas(conn: _Conn) -> bool:
    return all(not s.lstrip().upper().startswith(_MUTANTES) for s in conn.log)


@pytest.mark.parametrize(
    "linea_extra",
    [
        "GRANT `app_role`@`%` TO `mcp_ro`@`10.0.%`",  # rol (MySQL)
        "GRANT app_role TO 'mcp_ro'@'10.0.%'",  # rol (MariaDB)
        "GRANT PROXY ON ''@'' TO `mcp_ro`@`10.0.%`",
    ],
)
def test_an_existing_account_with_roles_is_refused_before_any_mutation(monkeypatch, linea_extra):
    adapter, conn, muts = _mysql(
        monkeypatch,
        exists=True,
        grants=["GRANT USAGE ON *.* TO `mcp_ro`@`10.0.%`", linea_extra],
    )
    with pytest.raises(AppHttpException) as exc:
        adapter.preflight_readonly_account(USER, HOST)
    assert exc.value.status_code == 409
    assert exc.value.public_context["code"] == "readonly_account.has_roles"
    assert muts == []
    assert _solo_lecturas(conn)


def test_an_existing_account_with_only_plain_grants_passes_the_preflight(monkeypatch):
    adapter, conn, _muts = _mysql(
        monkeypatch,
        exists=True,
        grants=[
            "GRANT USAGE ON *.* TO `mcp_ro`@`10.0.%`",
            "GRANT SELECT, SHOW VIEW ON `app`.* TO `mcp_ro`@`10.0.%`",
            "GRANT INSERT ON `app`.* TO `mcp_ro`@`10.0.%`",  # lo quita el REVOKE ALL
        ],
    )
    pre = adapter.preflight_readonly_account(USER, HOST)
    assert pre.exists is True
    assert _solo_lecturas(conn)


def test_a_bad_host_is_rejected_before_opening_any_connection(monkeypatch):
    adapter, conn, muts = _mysql(monkeypatch)
    with pytest.raises(AppHttpException) as exc:
        adapter.preflight_readonly_account(USER, "bad host';--")
    assert exc.value.status_code == 422
    assert conn.log == [] and muts == []


def test_the_preflight_of_a_missing_account_reads_only_and_reports_it_absent(monkeypatch):
    adapter, conn, muts = _mysql(monkeypatch, exists=False)
    pre = adapter.preflight_readonly_account(USER, HOST)
    assert pre.exists is False
    assert muts == []
    assert _solo_lecturas(conn)
    # Una cuenta inexistente no se consulta con SHOW GRANTS (fallaría con 1141).
    assert not any(s.startswith("SHOW GRANTS") for s in conn.log)


def test_has_unrecognized_grants_is_a_pure_classifier():
    assert mysql_has_unrecognized_grants([]) is False
    assert mysql_has_unrecognized_grants(["GRANT USAGE ON *.* TO `u`@`%`", ""]) is False
    assert mysql_has_unrecognized_grants(["GRANT `r`@`%` TO `u`@`%`"]) is True
    assert mysql_has_unrecognized_grants(["GRANT PROXY ON ''@'' TO `u`@`%`"]) is True


def test_the_base_adapter_does_not_provision_by_default():
    for nombre, args in (
        ("preflight_readonly_account", (USER, HOST)),
        ("provision_readonly_account", (USER, PWD, HOST, [], ReadonlyPreflight(False))),
    ):
        with pytest.raises(AppHttpException) as exc:
            getattr(ServerAdapter, nombre)(object.__new__(MySQLAdapter), *args)
        assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# PostgreSQL                                                                    #
# --------------------------------------------------------------------------- #


def test_postgres_creates_a_role_with_fixed_attributes_and_per_database_grants(monkeypatch):
    adapter, conn, muts = _pg(monkeypatch, exists=False)
    pre = _provision(adapter, ["Mi_Base", "otra"], host="ignorado")
    assert pre.exists is False and pre.global_grants == ()
    stmts = [s for _n, _d, s in muts]
    assert stmts[0].startswith('CREATE ROLE "mcp_ro" WITH LOGIN PASSWORD ')
    for attr in (
        "NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOINHERIT", "NOREPLICATION", "NOBYPASSRLS",
    ):
        assert attr in stmts[0]
    assert stmts[1] == 'ALTER ROLE "mcp_ro" SET default_transaction_read_only = on'
    # Por base: CONNECT a nivel servidor, USAGE sobre public conectado a esa base.
    assert muts[2] == ("server", None, 'GRANT CONNECT ON DATABASE "Mi_Base" TO "mcp_ro"')
    assert muts[3] == ("database", "Mi_Base", 'GRANT USAGE ON SCHEMA public TO "mcp_ro"')
    assert muts[4] == ("server", None, 'GRANT CONNECT ON DATABASE "otra" TO "mcp_ro"')
    assert muts[5] == ("database", "otra", 'GRANT USAGE ON SCHEMA public TO "mcp_ro"')
    assert len(muts) == 6
    # Nunca SELECT ni opciones de grant.
    assert not any("SELECT" in s.upper() or "GRANT OPTION" in s.upper() for s in stmts)
    assert _solo_lecturas(conn)


def test_postgres_alters_an_existing_role_and_quotes_the_password(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch, exists=True)
    _provision(adapter, [], host="x")
    first = muts[0][2]
    assert first.startswith('ALTER ROLE "mcp_ro" WITH LOGIN PASSWORD ')
    # La contraseña lleva backslash: literal E'' con el backslash y la comilla escapados.
    assert quote_string_literal(PWD, "postgresql") in first
    assert quote_string_literal(PWD, "postgresql").startswith("E'")


def test_postgres_rejects_a_bad_identifier_before_any_statement(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch)
    pre = adapter.preflight_readonly_account(USER, "x")
    with pytest.raises(AppHttpException) as exc:
        adapter.provision_readonly_account(USER, PWD, "x", ['b"d; DROP ROLE x'], pre)
    assert exc.value.status_code == 422
    # Se valida TODO antes de la primera sentencia: ni el rol se tocó.
    assert muts == []
