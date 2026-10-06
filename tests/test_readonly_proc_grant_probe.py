"""
S6 (mcp-schema-definitions): la sonda y el aprovisionamiento ante ``SELECT ON mysql.proc``.

``mysql.proc`` es SERVER-WIDE: expone el código de las rutinas de TODAS las bases del servidor.
Antes de S6 la sonda lo toleraba siempre; ahora solo lo tolera con la bandera
``servers.readonly_proc_grant`` encendida Y en un motor que lo necesita (MariaDB < 11.3 /
MySQL < 8.0). Cubre S6.1 y S6.2 y la regla de que NUNCA se acepta ``SELECT ON mysql.*``.

Sin motor: corren las funciones puras y los métodos reales de los adapters con la conexión
guionada de ``test_readonly_provision_sql``. NO prueba el comportamiento de un servidor real.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_readonly_proc_grant_probe``
"""

import dataclasses

import pytest

from app.services.db_admin.mysql_adapter import MariaDBAdapter, MySQLAdapter
from app.services.db_admin.readonly_probe import (
    MYSQL_PROC_TABLE,
    ReadonlyPreflight,
    mysql_grant_violations,
    proc_grant_supported,
)
from tests.test_readonly_provision_sql import _mysql, _pg

_PROC_LINE = "GRANT SELECT ON `mysql`.`proc` TO `mcp_ro`@`%`"
_USAGE_LINE = "GRANT USAGE ON *.* TO `mcp_ro`@`%`"
_DB_LINE = "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `app`.* TO `mcp_ro`@`%`"
_PROC_GRANT_SQL_PREFIX = "GRANT SELECT ON mysql.proc TO "


# --------------------------------------------------------------------------- #
# La regla pura                                                                #
# --------------------------------------------------------------------------- #


def test_the_literal_lives_in_one_place():
    assert MYSQL_PROC_TABLE == "mysql.proc"


def test_s6_1_without_the_flag_select_on_mysql_proc_is_a_violation():
    assert mysql_grant_violations([_USAGE_LINE, _PROC_LINE]) == ["select_on_mysql_schema"]
    assert mysql_grant_violations([_PROC_LINE], allow_mysql_proc=False) == [
        "select_on_mysql_schema"
    ]


def test_s6_2_with_the_flag_select_on_mysql_proc_is_accepted():
    assert mysql_grant_violations([_USAGE_LINE, _DB_LINE, _PROC_LINE], allow_mysql_proc=True) == []


@pytest.mark.parametrize("allow_mysql_proc", [False, True])
@pytest.mark.parametrize(
    "line",
    [
        "GRANT SELECT ON `mysql`.* TO `u`@`%`",
        "GRANT SELECT ON mysql.* TO `u`@`%`",
        "GRANT SELECT ON `mysql`.`user` TO `u`@`%`",
        "GRANT SELECT ON `mysql`.`servers` TO `u`@`%`",
        "GRANT SELECT ON `mysql`.`procs_priv` TO `u`@`%`",
        "GRANT SELECT ON `mysql`.`proc_extra` TO `u`@`%`",
        "GRANT SELECT (Host, User) ON `mysql`.`user` TO `u`@`%`",
    ],
)
def test_select_on_any_other_mysql_object_is_a_violation_with_or_without_the_flag(
    line, allow_mysql_proc
):
    assert "select_on_mysql_schema" in mysql_grant_violations(
        [line], allow_mysql_proc=allow_mysql_proc
    )


@pytest.mark.parametrize("allow_mysql_proc", [False, True])
def test_the_flag_does_not_excuse_global_select_or_write_privileges(allow_mysql_proc):
    violations = mysql_grant_violations(
        [
            "GRANT SELECT ON *.* TO `u`@`%`",
            "GRANT INSERT ON `mysql`.`proc` TO `u`@`%`",
            "GRANT ALL PRIVILEGES ON `mysql`.* TO `u`@`%`",
        ],
        allow_mysql_proc=allow_mysql_proc,
    )
    assert "global_privilege:select" in violations
    assert "privilege:insert" in violations
    assert "all_privileges" in violations


def test_the_flag_tolerates_proc_but_the_same_line_set_with_mysql_user_still_fails():
    violations = mysql_grant_violations(
        [_PROC_LINE, "GRANT SELECT ON `mysql`.`user` TO `u`@`%`"], allow_mysql_proc=True
    )
    assert violations == ["select_on_mysql_schema"]


# --------------------------------------------------------------------------- #
# ¿Qué motores necesitan el grant? (flag x motor)                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "dialect, version, expected",
    [
        ("mysql", "5.7.44", True),
        ("mysql", "5.6.51", True),
        ("mysql", "8.0.19", False),
        ("mysql", "8.0.36", False),
        ("mysql", "9.1.0", False),
        ("mariadb", "10.6.12-MariaDB", True),
        ("mariadb", "11.2.4-MariaDB", True),
        ("mariadb", "11.3.0-MariaDB", False),
        ("mariadb", "11.4.2-MariaDB", False),
        ("mysql", "10.11.6-MariaDB", True),
        ("mysql", "11.4.2-MariaDB", False),
        ("postgresql", "16.2", False),
        ("mysql", None, False),
        ("mysql", "not-a-version", False),
    ],
)
def test_proc_grant_is_supported_only_on_old_mariadb_and_mysql_5_7(dialect, version, expected):
    assert proc_grant_supported(version, dialect) is expected


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize(
    "cls, version, needs_proc",
    [
        (MySQLAdapter, "5.7.44", True),
        (MariaDBAdapter, "10.11.6-MariaDB", True),
        (MySQLAdapter, "8.0.36", False),
        (MariaDBAdapter, "11.4.2-MariaDB", False),
    ],
)
def test_the_adapter_probe_tolerates_proc_only_with_the_flag_and_an_engine_that_needs_it(
    monkeypatch, cls, version, needs_proc, flag
):
    adapter, _conn, _muts = _mysql(
        monkeypatch, cls=cls, version=version, grants=[_USAGE_LINE, _PROC_LINE]
    )
    violations = adapter.readonly_violations(allow_mysql_proc=flag)
    tolerated = flag and needs_proc
    assert (violations == []) is tolerated
    if not tolerated:
        assert violations == ["select_on_mysql_schema"]


def test_the_adapter_probe_defaults_to_strict(monkeypatch):
    adapter, _conn, _muts = _mysql(monkeypatch, version="5.7.44", grants=[_PROC_LINE])
    assert adapter.readonly_violations() == ["select_on_mysql_schema"]


# --------------------------------------------------------------------------- #
# Aprovisionamiento: el grant solo con bandera Y soporte                       #
# --------------------------------------------------------------------------- #


def _proc_statements(muts: list[str]) -> list[str]:
    return [m for m in muts if "mysql." in m.lower() and m.startswith("GRANT ")]


def _provision_with(adapter, *, proc_grant: bool):
    preflight = adapter.preflight_readonly_account("mcp_ro", "10.0.%")
    preflight = dataclasses.replace(preflight, proc_grant=proc_grant)
    adapter.provision_readonly_account("mcp_ro", "pwd", "10.0.%", ["app"], preflight)
    return preflight


@pytest.mark.parametrize(
    "cls, version, proc_grant, granted",
    [
        (MySQLAdapter, "5.7.44", True, True),
        (MariaDBAdapter, "10.11.6-MariaDB", True, True),
        (MySQLAdapter, "5.7.44", False, False),
        (MariaDBAdapter, "10.11.6-MariaDB", False, False),
        # Bandera encendida en un motor que no lo necesita: NO se otorga.
        (MySQLAdapter, "8.0.36", True, False),
        (MariaDBAdapter, "11.4.2-MariaDB", True, False),
    ],
)
def test_select_on_mysql_proc_is_granted_only_with_the_flag_and_a_supporting_engine(
    monkeypatch, cls, version, proc_grant, granted
):
    adapter, _conn, muts = _mysql(monkeypatch, cls=cls, version=version)
    _provision_with(adapter, proc_grant=proc_grant)
    proc_grants = _proc_statements(muts)
    if granted:
        assert len(proc_grants) == 1
        assert proc_grants[0].startswith(_PROC_GRANT_SQL_PREFIX)
        assert " WITH GRANT OPTION" not in proc_grants[0].upper()
        # Después del REVOKE ALL: la secuencia converge a la lista fija MÁS este grant.
        assert muts.index(proc_grants[0]) > 2
    else:
        assert proc_grants == []


def test_with_the_flag_the_only_mysql_object_ever_granted_is_proc(monkeypatch):
    adapter, _conn, muts = _mysql(monkeypatch, version="5.7.44")
    _provision_with(adapter, proc_grant=True)
    for statement in muts:
        if statement.startswith("GRANT ") and "mysql" in statement.lower():
            assert statement.startswith(_PROC_GRANT_SQL_PREFIX), statement
    assert not any(m.startswith("GRANT SELECT") and " ON *.* " in m for m in muts)
    assert not any(" ON mysql.* " in m for m in muts)


def test_what_is_granted_with_the_flag_passes_the_probe_with_the_flag_and_fails_without(
    monkeypatch,
):
    adapter, _conn, muts = _mysql(monkeypatch, version="5.7.44")
    _provision_with(adapter, proc_grant=True)
    # ``SHOW GRANTS`` muestra el objeto entre backticks; el ``GRANT`` que emite el gateway no.
    shown = [
        statement.replace("mysql.proc", "`mysql`.`proc`")
        for statement in muts
        if statement.startswith("GRANT ")
    ]
    assert shown
    assert mysql_grant_violations(shown, allow_mysql_proc=True) == []
    assert "select_on_mysql_schema" in mysql_grant_violations(shown)


def test_the_real_preflight_reports_whether_the_engine_needs_the_grant(monkeypatch):
    for cls, version, expected in (
        (MySQLAdapter, "5.7.44", True),
        (MariaDBAdapter, "10.6.12-MariaDB", True),
        (MySQLAdapter, "8.0.36", False),
        (MariaDBAdapter, "11.4.2-MariaDB", False),
    ):
        adapter, _conn, muts = _mysql(monkeypatch, cls=cls, version=version)
        preflight = adapter.preflight_readonly_account("mcp_ro", "10.0.%")
        assert preflight.proc_grant_supported is expected, (cls, version)
        # El adapter informa el hecho; decidir la bandera es del controller.
        assert preflight.proc_grant is False
        assert muts == []


def test_postgres_never_gets_a_proc_grant_and_ignores_the_probe_flag(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch, exists=False)
    preflight = adapter.preflight_readonly_account("mcp_ro", "ignorado")
    assert preflight.proc_grant_supported is False
    assert preflight.proc_grant is False
    assert not _proc_statements([statement for _n, _d, statement in muts])


def test_a_default_preflight_never_asks_for_the_grant():
    preflight = ReadonlyPreflight(False)
    assert preflight.proc_grant is False
    assert preflight.proc_grant_supported is False
