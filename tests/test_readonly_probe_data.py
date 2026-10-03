"""
Sonda de la credencial de DATOS por base (design D18, spec S29-S32).

Tres niveles:

- Evaluadores PUROS de ``readonly_probe`` con líneas de ``SHOW GRANTS`` y hechos de PostgreSQL
  escritos a mano: acá se decide qué es "SELECT sobre exactamente una base".
- Mapeo a códigos públicos (``mcp_catalog.public_probe_reason``): cerrado y fail-closed.
- Adapters con conexión falsa: leen los hechos correctos, sin motor.
"""

from datetime import datetime, timedelta

import pytest

from app.core.remote_engine import ServerTarget
from app.services import mcp_catalog
from app.services.db_admin import mysql_adapter as my_mod
from app.services.db_admin import postgres_adapter as pg_mod
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.mysql_adapter import MariaDBAdapter, MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter
from app.services.db_admin.readonly_probe import (
    data_credential_probe,
    data_probe_is_fresh,
    mysql_data_grant_violations,
    postgres_data_role_violations,
)

DB = "app_prod"
USAGE = "GRANT USAGE ON *.* TO `mcp_d_7`@`%`"
SELECT_DB = "GRANT SELECT ON `app\\_prod`.* TO `mcp_d_7`@`%`"


def viol(lines, database=DB, lctn=0):
    return mysql_data_grant_violations(lines, database=database, lower_case_table_names=lctn)


# --------------------------------------------------------------------------- #
# MySQL/MariaDB: grants                                                        #
# --------------------------------------------------------------------------- #


def test_the_provisioned_shape_is_green():
    assert viol([USAGE, SELECT_DB]) == []


def test_table_and_column_level_select_on_the_target_count_as_select():
    escaped = "GRANT SELECT ON `app\\_prod`.`t` TO `u`@`%`"
    assert viol([USAGE, escaped]) == []
    assert viol([USAGE, "GRANT SELECT (a, b) ON `app\\_prod`.`t` TO `u`@`%`"]) == []
    # Sin escapar, el `_` es comodín aunque el grant sea de tabla.
    unescaped = "GRANT SELECT ON `app_prod`.`t` TO `u`@`%`"
    assert viol([USAGE, unescaped]) == ["wildcard_database_pattern", "missing_select_on_database"]


def test_s30_select_on_two_databases_is_too_broad():
    out = viol([USAGE, SELECT_DB, "GRANT SELECT ON `other`.* TO `u`@`%`"])
    assert out == ["select_outside_database"]
    assert mcp_catalog.public_probe_reasons(out) == ["CREDENTIAL_TOO_BROAD"]


def test_select_on_all_databases_is_too_broad():
    out = viol([SELECT_DB, "GRANT SELECT ON *.* TO `u`@`%`"])
    assert out == ["global_privilege:select"]


def test_s31_insert_is_a_write_privilege():
    out = viol([USAGE, SELECT_DB, "GRANT INSERT ON `app\\_prod`.* TO `u`@`%`"])
    assert out == ["privilege:insert"]
    assert mcp_catalog.public_probe_reasons(out) == ["WRITE_PRIVILEGE_PRESENT"]


@pytest.mark.parametrize("priv", ["UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRIGGER", "EVENT", "FILE"])
def test_every_non_select_privilege_is_a_write_violation(priv):
    out = viol([USAGE, SELECT_DB, f"GRANT {priv} ON `app\\_prod`.* TO `u`@`%`"])
    assert out == [f"privilege:{priv.lower()}"]


def test_all_privileges_is_a_write_violation():
    out = viol(["GRANT ALL PRIVILEGES ON `app\\_prod`.* TO `u`@`%`"])
    assert "all_privileges" in out
    assert "WRITE_PRIVILEGE_PRESENT" in mcp_catalog.public_probe_reasons(out)


def test_structure_reads_are_excess_not_writes():
    out = viol([USAGE, "GRANT SELECT, SHOW VIEW ON `app\\_prod`.* TO `u`@`%`"])
    assert out == ["extra_privilege:show_view"]
    assert mcp_catalog.public_probe_reasons(out) == ["CREDENTIAL_TOO_BROAD"]


def test_global_non_usage_privilege_is_flagged_twice_over():
    out = viol([SELECT_DB, "GRANT INSERT ON *.* TO `u`@`%`"])
    assert out == ["global_privilege:insert", "privilege:insert"]


@pytest.mark.parametrize(
    "pattern", ["app_prod", "app%", "%", "_", "app\\_prod_", "app\\\\_prod"]
)
def test_unescaped_wildcards_are_flagged_whatever_the_name(pattern):
    out = viol([f"GRANT SELECT ON `{pattern}`.* TO `u`@`%`"])
    assert "wildcard_database_pattern" in out
    assert "missing_select_on_database" in out


def test_escaped_wildcards_are_literal_characters():
    # `app\_prod` es la base `app_prod`; `appXprod` NO es la base objetivo.
    assert viol([SELECT_DB]) == []
    out = viol(["GRANT SELECT ON `appXprod`.* TO `u`@`%`"])
    assert out == ["select_outside_database", "missing_select_on_database"]


def test_database_with_percent_and_underscore_in_its_name():
    grant = "GRANT SELECT ON `a\\%b\\_c`.* TO `u`@`%`"
    assert viol([grant], database="a%b_c") == []
    assert "select_outside_database" in viol([grant], database="aXbXc")


def test_escaped_backslash_is_a_literal_backslash():
    assert viol(["GRANT SELECT ON `a\\\\b`.* TO `u`@`%`"], database="a\\b") == []


def test_name_case_rule_follows_lower_case_table_names():
    upper = "GRANT SELECT ON `APP\\_PROD`.* TO `u`@`%`"
    assert viol([upper], lctn=0) == ["select_outside_database", "missing_select_on_database"]
    assert viol([upper], lctn=1) == []
    assert viol([upper], lctn=2) == []
    # Ausente (None) = estricto.
    assert "select_outside_database" in viol([upper], lctn=None)


def test_a_missing_select_is_reported():
    assert viol([USAGE]) == ["missing_select_on_database"]
    assert viol([]) == ["missing_select_on_database"]


def test_grant_option_roles_proxy_and_unknown_lines_are_violations():
    out = viol([USAGE, SELECT_DB + " WITH GRANT OPTION"])
    assert out == ["grant_option"]
    assert "unrecognized_grant" in viol([USAGE, SELECT_DB, "GRANT `app_rw`@`%` TO `u`@`%`"])
    assert "unrecognized_grant" in viol([USAGE, SELECT_DB, "GRANT PROXY ON ''@'' TO `u`@`%`"])
    assert "unrecognized_grant" in viol([USAGE, SELECT_DB, "GRANT SOMETHING NEW"])
    assert "unrecognized_grant" in viol(
        [USAGE, SELECT_DB, "GRANT EXECUTE ON PROCEDURE `app\\_prod`.`p` TO `u`@`%`"]
    )
    for line in ("GRANT PROXY ON ''@'' TO `u`@`%`", "GRANT `r`@`%` TO `u`@`%`"):
        out = viol([USAGE, SELECT_DB, line])
        assert mcp_catalog.public_probe_reasons(out) == ["CREDENTIAL_TOO_BROAD"]


def test_malformed_object_is_unrecognized_not_ignored():
    assert "unrecognized_grant" in viol([USAGE, SELECT_DB, "GRANT SELECT ON `broken TO `u`@`%`"])
    assert "unrecognized_grant" in viol([USAGE, SELECT_DB, "GRANT SELECT ON `a`.`b`.`c` TO `u`@`%`"])
    assert "unrecognized_grant" in viol([USAGE, SELECT_DB, "GRANT SELECT ON *.t TO `u`@`%`"])


def test_a_quoted_star_database_is_a_name_not_the_wildcard():
    out = viol([USAGE, SELECT_DB, "GRANT SELECT ON `*`.* TO `u`@`%`"])
    assert out == ["select_outside_database"]


def test_violations_carry_no_grant_text_and_do_not_repeat():
    lines = [USAGE, "GRANT INSERT ON `x`.* TO `secret_user`@`10.1.2.3`"] * 2
    out = viol(lines)
    assert len(out) == len(set(out))
    assert not any("secret_user" in v or "10.1.2.3" in v for v in out)


# --------------------------------------------------------------------------- #
# Veredicto por dialecto                                                       #
# --------------------------------------------------------------------------- #


def test_s32_amended_a_federated_table_blocks():
    facts = {"grants": [USAGE, SELECT_DB], "foreign_engine_tables": 1}
    out, warnings = data_credential_probe("mysql", facts, database=DB)
    assert out == ["foreign_engine_table"] and warnings == []
    assert mcp_catalog.public_probe_reasons(out) == ["FEDERATED_TABLE_PRESENT"]


def test_mysql_view_findings_are_warnings_only():
    facts = {
        "grants": [USAGE, SELECT_DB],
        "foreign_engine_tables": 0,
        "cross_schema_views": 2,
        "definer_views": 1,
    }
    out, warnings = data_credential_probe("mariadb", facts, database=DB)
    assert out == []
    assert warnings == ["cross_schema_view_reference", "definer_views_present"]


def test_mysql_green_verdict():
    facts = {"grants": [USAGE, SELECT_DB], "lower_case_table_names": 0}
    assert data_credential_probe("mysql", facts, database=DB) == ([], [])


def test_engine_without_a_probe_never_verifies():
    assert data_credential_probe("mysql", {"engine_unsupported": True}, database=DB)[0] == [
        "engine_unsupported"
    ]
    assert data_credential_probe("oracle", {}, database=DB)[0] == ["engine_unsupported"]
    assert mcp_catalog.public_probe_reasons(["engine_unsupported"]) == ["PROBE_NOT_GREEN"]


# --------------------------------------------------------------------------- #
# PostgreSQL: hechos del rol                                                   #
# --------------------------------------------------------------------------- #

GREEN_PG = {
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolreplication": False,
    "rolbypassrls": False,
    "rolconnlimit": 3,
    "default_transaction_read_only": "on",
    "statement_timeout": "30s",
    "can_create_in_database": False,
    "can_create_in_public": False,
    "write_roles": [],
    "role_memberships": 0,
    "table_write_privileges": 0,
    "foreign_access_extensions": [],
    "explicit_connect_other_databases": 0,
    "public_connect_other_databases": 0,
    "temp_write_succeeded": False,
}


def pg(**over):
    return postgres_data_role_violations({**GREEN_PG, **over})


def test_pg_green():
    assert pg() == ([], [])


@pytest.mark.parametrize(
    "attr", ["rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"]
)
def test_pg_role_attributes_are_too_broad(attr):
    out, _ = pg(**{attr: True})
    assert out == [f"role_attribute:{attr}"]
    assert mcp_catalog.public_probe_reasons(out) == ["CREDENTIAL_TOO_BROAD"]


def test_pg_write_signals_map_to_write_privilege_present():
    for over, code in (
        ({"table_write_privileges": 2}, "table_write_privileges"),
        ({"default_transaction_read_only": "off"}, "default_transaction_read_only_off"),
        ({"temp_write_succeeded": True}, "write_attempt_succeeded"),
    ):
        out, _ = pg(**over)
        assert out == [code]
        assert mcp_catalog.public_probe_reasons(out) == ["WRITE_PRIVILEGE_PRESENT"]


def test_pg_memberships_and_write_roles_are_too_broad():
    out, _ = pg(role_memberships=1, write_roles=["pg_write_all_data"])
    assert out == ["member_of:pg_write_all_data", "member_of_role"]
    assert mcp_catalog.public_probe_reasons(out) == ["CREDENTIAL_TOO_BROAD"]


def test_pg_foreign_extension_blocks_as_federated():
    out, _ = pg(foreign_access_extensions=["dblink"])
    assert out == ["foreign_access_extension"]
    assert mcp_catalog.public_probe_reasons(out) == ["FEDERATED_TABLE_PRESENT"]


def test_pg_explicit_connect_elsewhere_is_too_broad_but_public_connect_only_warns():
    out, _ = pg(explicit_connect_other_databases=1)
    assert out == ["select_outside_database"]
    out, warnings = pg(public_connect_other_databases=4)
    assert out == [] and warnings == ["public_connect_other_databases"]


def test_pg_create_privileges_are_warnings_because_pg14_public_has_them():
    out, warnings = pg(can_create_in_database=True, can_create_in_public=True)
    assert out == [] and warnings == ["create_on_database", "create_on_schema_public"]


@pytest.mark.parametrize("limit", [None, -1, 0, 4, 100])
def test_pg_connection_limit_must_be_between_1_and_3(limit):
    out, _ = pg(rolconnlimit=limit)
    assert out == ["connection_limit"]
    assert mcp_catalog.public_probe_reasons(out) == ["PROBE_NOT_GREEN"]


@pytest.mark.parametrize("value", [None, "", "0", "0ms"])
def test_pg_statement_timeout_must_be_set(value):
    assert pg(statement_timeout=value)[0] == ["statement_timeout_unset"]


def test_pg_an_absent_protective_fact_counts_as_a_violation():
    out, _ = postgres_data_role_violations({})
    for code in (
        "role_attribute:rolsuper",
        "default_transaction_read_only_off",
        "write_attempt_succeeded",
        "table_write_privileges",
        "member_of_role",
        "select_outside_database",
        "connection_limit",
        "statement_timeout_unset",
    ):
        assert code in out


def test_pg_dispatch():
    assert data_credential_probe("postgresql", GREEN_PG, database=DB) == ([], [])


# --------------------------------------------------------------------------- #
# Mapeo público y frescura                                                     #
# --------------------------------------------------------------------------- #


def test_unknown_probe_reason_fails_closed_to_too_broad():
    assert mcp_catalog.public_probe_reason("algo_nuevo") == "CREDENTIAL_TOO_BROAD"


def test_every_probe_reason_maps_into_the_closed_public_vocabulary():
    emitted = set()
    for lines in (
        [USAGE],
        [SELECT_DB, "GRANT INSERT ON *.* TO `u`@`%`", "GRANT `r` TO `u`"],
        ["GRANT ALL PRIVILEGES ON `x`.* TO `u`@`%` WITH GRANT OPTION"],
        ["GRANT SELECT, SHOW VIEW ON `a_b`.* TO `u`@`%`"],
    ):
        emitted |= set(viol(lines))
    emitted |= set(postgres_data_role_violations({})[0])
    emitted |= {"foreign_engine_table", "foreign_access_extension", "engine_unsupported"}
    for code in emitted:
        assert mcp_catalog.public_probe_reason(code) in mcp_catalog.REASON_CODES


def test_freshness_window():
    now = datetime(2026, 10, 10, 12, 0, 0)
    assert data_probe_is_fresh(now - timedelta(days=6, hours=23), now=now, max_age_days=7)
    assert not data_probe_is_fresh(now - timedelta(days=7, seconds=1), now=now, max_age_days=7)
    assert not data_probe_is_fresh(None, now=now, max_age_days=7)
    assert not data_probe_is_fresh(now + timedelta(hours=1), now=now, max_age_days=7)


# --------------------------------------------------------------------------- #
# Adapters con conexión falsa                                                  #
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _ScriptedConn:
    """Responde por fragmento de SQL (el primero que aparezca) y registra todo."""

    def __init__(self, script):
        self.script = script
        self.log: list[str] = []
        self.rollbacks = 0

    def execute(self, clause, params=None):
        sql = str(clause)
        self.log.append(sql)
        for fragment, rows in self.script:
            if fragment in sql:
                if isinstance(rows, Exception):
                    raise rows
                return _Result(rows)
        raise AssertionError(f"sentencia inesperada: {sql}")

    def rollback(self):
        self.rollbacks += 1


class _Ctx:
    def __init__(self, conn):
        self.conn = conn
        self.opened: list[tuple] = []

    def __call__(self, target, database):
        self.opened.append((target.admin_user, database))
        return self

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _target(dialect, user="mcp_d_7"):
    return ServerTarget(
        server_id=1, dialect=dialect, host="127.0.0.1", port=3306,
        admin_user=user, admin_password="x",
    )


@pytest.mark.parametrize("cls,dialect", [(MySQLAdapter, "mysql"), (MariaDBAdapter, "mariadb")])
def test_mysql_facts_read_grants_case_rule_and_foreign_engines(monkeypatch, cls, dialect):
    conn = _ScriptedConn(
        [
            ("SHOW GRANTS", [(USAGE,), (SELECT_DB,)]),
            ("@@lower_case_table_names", [(1,)]),
            ("information_schema.TABLES", [(2,)]),
            ("information_schema.VIEW_TABLE_USAGE", [(3,)]),
            ("information_schema.VIEWS", [(1,)]),
        ]
    )
    ctx = _Ctx(conn)
    monkeypatch.setattr(my_mod, "database_connection", ctx)
    facts = cls(_target(dialect)).data_credential_facts(DB)
    assert ctx.opened == [("mcp_d_7", DB)]  # con la credencial de datos, en ESA base
    assert facts == {
        "grants": [USAGE, SELECT_DB],
        "lower_case_table_names": 1,
        "foreign_engine_tables": 2,
        "definer_views": 1,
        "cross_schema_views": 3,
    }
    foreign = next(s for s in conn.log if "information_schema.TABLES" in s)
    for engine in ("FEDERATED", "CONNECT", "SPIDER"):
        assert f"'{engine}'" in foreign


def test_mysql_facts_tolerate_an_engine_without_view_table_usage(monkeypatch):
    from sqlalchemy.exc import OperationalError

    conn = _ScriptedConn(
        [
            ("SHOW GRANTS", [(USAGE,), (SELECT_DB,)]),
            ("@@lower_case_table_names", [(0,)]),
            ("information_schema.TABLES", [(0,)]),
            ("information_schema.VIEW_TABLE_USAGE", OperationalError("s", {}, Exception("x"))),
            ("information_schema.VIEWS", [(0,)]),
        ]
    )
    monkeypatch.setattr(my_mod, "database_connection", _Ctx(conn))
    facts = MySQLAdapter(_target("mysql")).data_credential_facts(DB)
    assert facts["cross_schema_views"] == 0
    assert data_credential_probe("mysql", facts, database=DB) == ([], [])


def test_mysql_facts_never_interpolate_the_database_name(monkeypatch):
    conn = _ScriptedConn(
        [
            ("SHOW GRANTS", []),
            ("@@lower_case_table_names", [(0,)]),
            ("information_schema", [(0,)]),
        ]
    )
    monkeypatch.setattr(my_mod, "database_connection", _Ctx(conn))
    MySQLAdapter(_target("mysql")).data_credential_facts("x'; DROP TABLE t; --")
    assert not any("DROP TABLE" in s for s in conn.log)


def test_postgres_facts_cover_every_input_of_the_evaluator(monkeypatch):
    role_row = {
        "rolsuper": False, "rolcreatedb": False, "rolcreaterole": False,
        "rolreplication": False, "rolbypassrls": False, "rolconnlimit": 3,
    }
    conn = _ScriptedConn(
        [
            ("rolsuper", [role_row]),
            ("current_setting", [("on",)]),
            ("has_database_privilege", [(False,)]),
            ("has_schema_privilege", [(False,)]),
            ("pg_has_role", [(False,)]),
            ("pg_auth_members", [(0,)]),
            ("has_table_privilege", [(0,)]),
            ("pg_extension", [("dblink",)]),
            ("a.grantee = 0", [(5,)]),
            ("aclexplode", [(0,)]),
            ("CREATE TEMP TABLE", [(None,)]),
        ]
    )
    ctx = _Ctx(conn)
    monkeypatch.setattr(pg_mod, "database_connection", ctx)
    facts = PostgresAdapter(_target("postgresql")).data_credential_facts(DB)
    assert ctx.opened == [("mcp_d_7", DB)]
    assert facts["rolconnlimit"] == 3 and facts["foreign_access_extensions"] == ["dblink"]
    assert facts["public_connect_other_databases"] == 5
    assert facts["explicit_connect_other_databases"] == 0
    assert facts["temp_write_succeeded"] is True  # el fake no rechazó la escritura
    assert conn.rollbacks >= 2  # el intento de escritura se revierte SIEMPRE
    out, warnings = data_credential_probe("postgresql", facts, database=DB)
    assert "foreign_access_extension" in out and "write_attempt_succeeded" in out
    assert warnings == ["public_connect_other_databases"]
    ext = next(s for s in conn.log if "pg_extension" in s)
    for name in ("dblink", "postgres_fdw", "mysql_fdw", "file_fdw"):
        assert f"'{name}'" in ext


def test_postgres_temp_write_rejected_by_the_engine_is_green(monkeypatch):
    from sqlalchemy.exc import OperationalError

    conn = _ScriptedConn([("CREATE TEMP TABLE", OperationalError("s", {}, Exception("25006")))])
    assert PostgresAdapter._temp_write_succeeded(conn) is False
    assert conn.rollbacks == 2


def test_base_adapter_probe_is_fail_closed():
    class _Fake:
        pass

    assert ServerAdapter.data_credential_facts(_Fake(), DB) == {"engine_unsupported": True}
