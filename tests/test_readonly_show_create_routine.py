"""
S5 (mcp-schema-definitions): ``SHOW CREATE ROUTINE`` por base en MariaDB >= 11.3.

Sin motor: corren los métodos reales de los adapters con la conexión guionada de
``test_readonly_provision_sql``. NADA de esto prueba el comportamiento de un servidor 11.3+ real:
el nombre exacto del privilegio y la sintaxis del ``GRANT`` están pendientes de confirmar en
staging (ver ``MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE``). Estos tests fijan la REGLA del gateway
(a quién se otorga, a qué nivel, qué acepta la sonda), no la del motor.
"""

import pytest

from app.services.db_admin.mysql_adapter import MariaDBAdapter, MySQLAdapter
from app.services.db_admin.readonly_probe import (
    MARIADB_READONLY_DB_EXTRA_GRANTS,
    MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE,
    MYSQL_ALLOWED_PRIVILEGES,
    MYSQL_READONLY_DB_GRANTS,
    MYSQL_READONLY_GLOBAL_GRANTS,
    ReadonlyPreflight,
    mariadb_routine_grants_for_version,
    mysql_grant_violations,
)
from tests.test_readonly_provision_sql import _mysql, _pg, _provision

_PRIVILEGE = MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE
_DB_GRANT_PREFIX_BASE = "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT"
_DB_GRANT_PREFIX_WITH_ROUTINE = f"{_DB_GRANT_PREFIX_BASE}, {_PRIVILEGE}"

#: Privilegios que escriben o administran: la lista fija nunca puede incluir ninguno.
_WRITE_PRIVILEGES = (
    "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "ALL", "GRANT OPTION",
    "EXECUTE", "FILE", "PROCESS", "SUPER", "CREATE ROUTINE", "ALTER ROUTINE",
)


def _db_grants(muts: list[str]) -> list[str]:
    return [m for m in muts if m.startswith("GRANT ") and " ON *.* " not in m]


# --------------------------------------------------------------------------- #
# Matriz de versiones                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls,version,esperado",
    [
        (MariaDBAdapter, "10.6.12-MariaDB", False),
        (MariaDBAdapter, "10.11.6-MariaDB", False),
        (MariaDBAdapter, "5.5.5-10.11.6-MariaDB", False),
        (MariaDBAdapter, "11.2.4-MariaDB", False),
        (MariaDBAdapter, "11.3.0-MariaDB", True),
        (MariaDBAdapter, "11.4.2-MariaDB-log", True),
        (MariaDBAdapter, "5.5.5-11.4.2-MariaDB", True),
        # VERSION() sin el sufijo: el dialecto registrado alcanza para saber que es MariaDB.
        (MariaDBAdapter, "11.4.2", True),
        (MariaDBAdapter, "10.11.6", False),
        # Versión ilegible: no se otorga (un GRANT desconocido falla después del REVOKE ALL).
        (MariaDBAdapter, "not-a-version", False),
        (MariaDBAdapter, None, False),
        # MySQL y servidores registrados como mysql: igual que hoy.
        (MySQLAdapter, "5.7.44", False),
        (MySQLAdapter, "8.0.19", False),
        (MySQLAdapter, "8.0.20", False),
        (MySQLAdapter, "8.0.36", False),
        (MySQLAdapter, "9.1.0", False),
        # Registrado como mysql pero el motor dice MariaDB: se trata como MariaDB.
        (MySQLAdapter, "11.4.2-MariaDB", True),
        (MySQLAdapter, "10.11.6-MariaDB", False),
    ],
)
def test_show_create_routine_is_granted_per_database_only_on_mariadb_11_3_or_later(
    monkeypatch, cls, version, esperado
):
    adapter, _conn, muts = _mysql(monkeypatch, cls=cls, version=version)
    _provision(adapter, ["app_prod", "otra"])
    grants_por_base = _db_grants(muts)
    assert len(grants_por_base) == 2
    for grant in grants_por_base:
        assert (f", {_PRIVILEGE} ON " in grant) is esperado


def test_the_per_database_grant_is_scoped_to_each_database_and_never_global(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, cls=MariaDBAdapter, version="11.4.2-MariaDB")
    _provision(adapter, ["app_prod", "otra"])
    grants = _db_grants(muts)
    assert grants[0].startswith(f"{_DB_GRANT_PREFIX_WITH_ROUTINE} ON `app\\_prod`.* TO ")
    assert grants[1].startswith(f"{_DB_GRANT_PREFIX_WITH_ROUTINE} ON `otra`.* TO ")
    for statement in muts:
        if _PRIVILEGE in statement:
            assert " ON *.* " not in statement
            assert "mysql." not in statement.lower()
    # Ninguna sentencia global lo lleva: el único global posible sigue siendo SHOW_ROUTINE.
    assert not any(_PRIVILEGE in m for m in muts if " ON *.* " in m)


def test_mariadb_never_receives_the_global_show_routine_even_with_the_new_privilege(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, cls=MariaDBAdapter, version="11.4.2-MariaDB")
    _provision(adapter, ["app"])
    assert not any("SHOW_ROUTINE" in m for m in muts)
    assert not any(" ON *.* " in m and m.startswith("GRANT ") for m in muts)


def test_mysql_8_0_20_keeps_exactly_the_grants_it_gets_today(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, cls=MySQLAdapter, version="8.0.36")
    _provision(adapter, ["app"])
    grants = [m for m in muts if m.startswith("GRANT ")]
    assert grants[0].startswith(f"{_DB_GRANT_PREFIX_BASE} ON `app`.* TO ")
    assert _PRIVILEGE not in grants[0]
    assert grants[1].startswith("GRANT SHOW_ROUTINE ON *.* TO ")
    assert len(grants) == 2


def test_postgres_provisioning_is_unchanged(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch, exists=False)
    pre = _provision(adapter, ["Mi_Base"], host="ignorado")
    assert pre.db_extra_grants == ()
    assert not any(_PRIVILEGE in statement for _n, _d, statement in muts)
    assert len(muts) == 4


def test_the_preflight_reports_the_extra_grants_before_any_mutation(monkeypatch):
    adapter, conn, muts = _mysql(monkeypatch, cls=MariaDBAdapter, version="11.4.2-MariaDB")
    preflight = adapter.preflight_readonly_account("mcp_ro", "10.0.%")
    assert preflight.db_extra_grants == (_PRIVILEGE,)
    assert preflight.global_grants == ()
    assert preflight.note is None
    assert muts == []
    assert any(s.startswith("SELECT VERSION()") for s in conn.log)


def test_the_preflight_explains_why_the_privilege_was_not_granted_on_old_mariadb(monkeypatch):
    adapter, _conn, _muts = _mysql(monkeypatch, cls=MariaDBAdapter, version="10.11.6-MariaDB")
    preflight = adapter.preflight_readonly_account("mcp_ro", "10.0.%")
    assert preflight.db_extra_grants == ()
    assert preflight.note and "11.3" in preflight.note


def test_the_preflight_defaults_to_no_extra_grants_for_existing_callers():
    assert ReadonlyPreflight(False).db_extra_grants == ()
    assert ReadonlyPreflight(True, ("SHOW_ROUTINE",), "n").db_extra_grants == ()


def test_reprovisioning_is_idempotent_and_converges_to_the_same_statements(monkeypatch):
    adapter, _conn, muts_first = _mysql(
        monkeypatch, cls=MariaDBAdapter, version="11.4.2-MariaDB", exists=False
    )
    _provision(adapter, ["app"])
    adapter_again, _conn_again, muts_second = _mysql(
        monkeypatch, cls=MariaDBAdapter, version="11.4.2-MariaDB", exists=True,
        grants=["GRANT USAGE ON *.* TO `mcp_ro`@`10.0.%`"],
    )
    _provision(adapter_again, ["app"])
    # Misma lista fija tras REVOKE ALL, exista o no la cuenta.
    assert muts_first[2:] == muts_second[2:]
    assert muts_second[0].startswith("CREATE USER IF NOT EXISTS ")
    assert muts_second[2].startswith("REVOKE ALL PRIVILEGES, GRANT OPTION FROM ")


# --------------------------------------------------------------------------- #
# Pureza del helper de versión                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "version,dialect,esperado",
    [
        ("11.3.0-MariaDB", "mysql", True),
        ("11.3.0", "mariadb", True),
        ("11.2.9", "mariadb", False),
        ("11.4.2", "mysql", False),  # sin sufijo y dialecto mysql: no es MariaDB
        ("8.0.36", "mariadb", False),
        (None, "mariadb", False),
        ("11.4.2-MariaDB", "postgresql", True),  # la cadena de versión manda sobre el dialecto
    ],
)
def test_mariadb_routine_grants_for_version_honours_the_registered_dialect(
    version, dialect, esperado
):
    grants, note = mariadb_routine_grants_for_version(version, dialect)
    assert grants == ((_PRIVILEGE,) if esperado else ())
    assert (note is None) is esperado


# --------------------------------------------------------------------------- #
# Lista fija vs allowlist de la sonda                                           #
# --------------------------------------------------------------------------- #


def test_the_extra_grants_are_in_the_allowlist_and_are_not_write_privileges():
    assert set(MARIADB_READONLY_DB_EXTRA_GRANTS) == {_PRIVILEGE}
    assert set(MARIADB_READONLY_DB_EXTRA_GRANTS) <= MYSQL_ALLOWED_PRIVILEGES
    todo_lo_que_se_puede_otorgar = {
        *MYSQL_READONLY_DB_GRANTS,
        *MYSQL_READONLY_GLOBAL_GRANTS,
        *MARIADB_READONLY_DB_EXTRA_GRANTS,
    }
    for privilegio in _WRITE_PRIVILEGES:
        assert privilegio not in todo_lo_que_se_puede_otorgar


def test_the_base_grant_list_did_not_change():
    assert MYSQL_READONLY_DB_GRANTS == ("SELECT", "SHOW VIEW", "TRIGGER", "EVENT")
    assert MYSQL_READONLY_GLOBAL_GRANTS == ("SHOW_ROUTINE",)


@pytest.mark.parametrize("version", ["11.3.0-MariaDB", "11.4.2-MariaDB", "12.0.1-MariaDB"])
def test_every_statement_provisioned_on_mariadb_passes_the_probe(monkeypatch, version):
    adapter, _conn, muts = _mysql(monkeypatch, cls=MariaDBAdapter, version=version)
    _provision(adapter, ["app_prod", "otra"])
    grants = [m for m in muts if m.startswith("GRANT ")]
    assert any(_PRIVILEGE in g for g in grants)
    assert mysql_grant_violations(grants, is_mariadb=True) == []


def test_every_statement_provisioned_never_includes_write_privileges(monkeypatch):
    for cls, version in (
        (MariaDBAdapter, "11.4.2-MariaDB"),
        (MariaDBAdapter, "10.11.6-MariaDB"),
        (MySQLAdapter, "8.0.36"),
        (MySQLAdapter, "5.7.44"),
    ):
        adapter, _conn, muts = _mysql(monkeypatch, cls=cls, version=version)
        _provision(adapter, ["app"])
        for grant in (m for m in muts if m.startswith("GRANT ")):
            privileges_text = grant[len("GRANT "):grant.index(" ON ")]
            granted = {p.strip().upper() for p in privileges_text.split(",")}
            assert not granted & set(_WRITE_PRIVILEGES), (cls.__name__, version, grant)
            assert "WITH GRANT OPTION" not in grant.upper()


# --------------------------------------------------------------------------- #
# Sonda                                                                        #
# --------------------------------------------------------------------------- #

_DB_LEVEL_LINE = f"GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, {_PRIVILEGE} ON `la_base`.* TO `u`@`%`"


def test_the_probe_accepts_the_privilege_at_database_level_on_mariadb():
    assert mysql_grant_violations([_DB_LEVEL_LINE], is_mariadb=True) == []


def test_the_probe_reports_the_privilege_when_the_engine_is_not_mariadb():
    assert "privilege:show_create_routine" in mysql_grant_violations([_DB_LEVEL_LINE])
    assert "privilege:show_create_routine" in mysql_grant_violations(
        [_DB_LEVEL_LINE], is_mariadb=False
    )


def test_the_probe_rejects_the_privilege_server_wide_even_on_mariadb():
    violations = mysql_grant_violations(
        [f"GRANT {_PRIVILEGE} ON *.* TO `u`@`%`"], is_mariadb=True
    )
    assert "global_privilege:show_create_routine" in violations


def test_the_probe_rejects_the_privilege_below_database_level_even_on_mariadb():
    violations = mysql_grant_violations(
        [f"GRANT {_PRIVILEGE} ON `la_base`.`t` TO `u`@`%`"], is_mariadb=True
    )
    assert "non_database_privilege:show_create_routine" in violations


@pytest.mark.parametrize("is_mariadb", [True, False])
@pytest.mark.parametrize(
    "line",
    [
        "GRANT SELECT ON `mysql`.* TO `u`@`%`",
        "GRANT SELECT ON `mysql`.`user` TO `u`@`%`",
        "GRANT SELECT ON `mysql`.`servers` TO `u`@`%`",
    ],
)
def test_select_on_the_mysql_schema_is_still_a_violation(line, is_mariadb):
    assert "select_on_mysql_schema" in mysql_grant_violations([line], is_mariadb=is_mariadb)


@pytest.mark.parametrize("is_mariadb", [True, False])
def test_write_privileges_are_still_violations_next_to_the_new_privilege(is_mariadb):
    violations = mysql_grant_violations(
        [f"GRANT INSERT, {_PRIVILEGE} ON `la_base`.* TO `u`@`%`"], is_mariadb=is_mariadb
    )
    assert "privilege:insert" in violations


def test_the_probe_still_rejects_grant_option_with_the_new_privilege():
    violations = mysql_grant_violations(
        [f"GRANT {_PRIVILEGE} ON `la_base`.* TO `u`@`%` WITH GRANT OPTION"], is_mariadb=True
    )
    assert "grant_option" in violations


def test_the_probe_keeps_its_default_strict_for_callers_that_do_not_pass_the_engine():
    # Mismo contrato de siempre para los grants que ya existían.
    assert mysql_grant_violations(
        [
            "GRANT USAGE ON *.* TO `mcp_ro`@`10.0.0.%`",
            "GRANT SHOW_ROUTINE ON *.* TO `mcp_ro`@`10.0.0.%`",
            "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `la_base`.* TO `mcp_ro`@`10.0.0.%`",
        ]
    ) == []
