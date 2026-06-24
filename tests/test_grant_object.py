"""
Tests del DCL generado por grant_object/revoke_object (sin conexión real).

Captura los statements monkeypatcheando _execute_server/_execute_database y verifica
el SQL exacto por nivel y motor, la seguridad de identificadores, la validación contra
el catálogo cerrado y el ruteo servidor-vs-BD en PostgreSQL.
"""

import pytest

from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services.db_admin.dtos import EngineUserInfo, GrantLevel, ObjectRef, RoutineRef
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter

U = EngineUserInfo(username="u", host="%")
R = EngineUserInfo(username="r")  # PostgreSQL: rol sin host


def _adapter(cls, dialect, port):
    target = ServerTarget(
        server_id=1, dialect=dialect, host="h", port=port,
        admin_user="root", admin_password="x",
    )
    adapter = cls(target)
    captured = {"server": [], "database": []}
    adapter._execute_server = lambda stmts, **k: captured["server"].extend(stmts)
    adapter._execute_database = lambda db, stmts, **k: captured["database"].extend(
        (db, s) for s in stmts
    )
    return adapter, captured


def _mysql():
    return _adapter(MySQLAdapter, "mysql", 3306)


def _pg():
    return _adapter(PostgresAdapter, "postgresql", 5432)


# --------------------------------- MySQL ------------------------------------- #
def test_mysql_grant_database():
    a, cap = _mysql()
    a.grant_object(U, GrantLevel.DATABASE, ObjectRef(database="app"), ["SELECT", "INSERT"])
    assert cap["server"] == ["GRANT SELECT, INSERT ON `app`.* TO 'u'@'%'"]


def test_mysql_grant_table():
    a, cap = _mysql()
    a.grant_object(U, GrantLevel.TABLE, ObjectRef(database="app", table="users"), ["SELECT"])
    assert cap["server"] == ["GRANT SELECT ON `app`.`users` TO 'u'@'%'"]


def test_mysql_grant_column_applies_cols_to_each_priv():
    a, cap = _mysql()
    a.grant_object(
        U, GrantLevel.COLUMN,
        ObjectRef(database="app", table="users", columns=["email", "name"]),
        ["SELECT", "UPDATE"],
    )
    assert cap["server"] == [
        "GRANT SELECT (`email`, `name`), UPDATE (`email`, `name`) ON `app`.`users` TO 'u'@'%'"
    ]


def test_mysql_grant_routine():
    a, cap = _mysql()
    a.grant_object(
        U, GrantLevel.ROUTINE,
        ObjectRef(database="app", routine=RoutineRef(kind="function", name="calc")),
        ["EXECUTE"],
    )
    assert cap["server"] == ["GRANT EXECUTE ON FUNCTION `app`.`calc` TO 'u'@'%'"]


def test_mysql_with_grant_option():
    a, cap = _mysql()
    a.grant_object(U, GrantLevel.DATABASE, ObjectRef(database="app"), ["SELECT"], with_grant_option=True)
    assert cap["server"] == ["GRANT SELECT ON `app`.* TO 'u'@'%' WITH GRANT OPTION"]


def test_mysql_grant_option_token_becomes_clause():
    a, cap = _mysql()
    # "GRANT OPTION" se traduce a WITH GRANT OPTION (no a `GRANT GRANT OPTION`).
    a.grant_object(U, GrantLevel.TABLE, ObjectRef(database="app", table="t"), ["SELECT", "GRANT OPTION"])
    assert cap["server"] == ["GRANT SELECT ON `app`.`t` TO 'u'@'%' WITH GRANT OPTION"]


def test_mysql_grant_option_only_uses_usage():
    a, cap = _mysql()
    a.grant_object(U, GrantLevel.TABLE, ObjectRef(database="app", table="t"), ["GRANT OPTION"])
    assert cap["server"] == ["GRANT USAGE ON `app`.`t` TO 'u'@'%' WITH GRANT OPTION"]


def test_mysql_revoke_table():
    a, cap = _mysql()
    a.revoke_object(U, GrantLevel.TABLE, ObjectRef(database="app", table="t"), ["SELECT", "UPDATE"])
    assert cap["server"] == ["REVOKE SELECT, UPDATE ON `app`.`t` FROM 'u'@'%'"]


@pytest.mark.parametrize("privs", [["TRUNCATE"], ["SUPER"], ["BOGUS"]])
def test_mysql_invalid_privileges_rejected(privs):
    a, _ = _mysql()
    with pytest.raises(AppHttpException) as exc:
        a.grant_object(U, GrantLevel.TABLE, ObjectRef(database="app", table="t"), privs)
    assert exc.value.status_code == 422


def test_mysql_unsupported_level_rejected():
    a, _ = _mysql()
    with pytest.raises(AppHttpException) as exc:
        a.grant_object(U, GrantLevel.SCHEMA, ObjectRef(database="app", db_schema="s"), ["USAGE"])
    assert exc.value.status_code == 422


def test_mysql_injection_in_object_name_rejected():
    a, _ = _mysql()
    with pytest.raises(AppHttpException):
        a.grant_object(U, GrantLevel.TABLE, ObjectRef(database="bad`db", table="t"), ["SELECT"])


def test_mysql_missing_table_rejected():
    a, _ = _mysql()
    with pytest.raises(AppHttpException) as exc:
        a.grant_object(U, GrantLevel.TABLE, ObjectRef(database="app"), ["SELECT"])
    assert exc.value.status_code == 422


# ------------------------------- PostgreSQL ---------------------------------- #
def test_pg_grant_database_is_server_level():
    a, cap = _pg()
    a.grant_object(R, GrantLevel.DATABASE, ObjectRef(database="app"), ["CONNECT", "CREATE"])
    assert cap["server"] == ['GRANT CONNECT, CREATE ON DATABASE "app" TO "r"']
    assert cap["database"] == []  # DATABASE-level NO usa conexión a la BD


def test_pg_grant_schema_runs_in_database():
    a, cap = _pg()
    a.grant_object(R, GrantLevel.SCHEMA, ObjectRef(database="app", db_schema="public"), ["USAGE", "CREATE"])
    assert cap["server"] == []
    assert cap["database"] == [("app", 'GRANT USAGE, CREATE ON SCHEMA "public" TO "r"')]


def test_pg_grant_table_default_schema():
    a, cap = _pg()
    a.grant_object(R, GrantLevel.TABLE, ObjectRef(database="app", table="users"), ["SELECT", "TRUNCATE"])
    assert cap["database"] == [("app", 'GRANT SELECT, TRUNCATE ON TABLE "public"."users" TO "r"')]


def test_pg_grant_column():
    a, cap = _pg()
    a.grant_object(R, GrantLevel.COLUMN, ObjectRef(database="app", table="users", columns=["email"]), ["SELECT", "UPDATE"])
    assert cap["database"] == [("app", 'GRANT SELECT ("email"), UPDATE ("email") ON "public"."users" TO "r"')]


def test_pg_grant_sequence_and_routine():
    a, cap = _pg()
    a.grant_object(R, GrantLevel.SEQUENCE, ObjectRef(database="app", sequence="seq1"), ["USAGE"])
    a.grant_object(R, GrantLevel.ROUTINE, ObjectRef(database="app", routine=RoutineRef(kind="procedure", name="proc1")), ["EXECUTE"])
    assert cap["database"] == [
        ("app", 'GRANT USAGE ON SEQUENCE "public"."seq1" TO "r"'),
        ("app", 'GRANT EXECUTE ON PROCEDURE "public"."proc1" TO "r"'),
    ]


def test_pg_with_grant_option_and_revoke():
    a, cap = _pg()
    a.grant_object(R, GrantLevel.TABLE, ObjectRef(database="app", table="t"), ["SELECT"], with_grant_option=True)
    a.revoke_object(R, GrantLevel.TABLE, ObjectRef(database="app", table="t"), ["SELECT"])
    assert cap["database"] == [
        ("app", 'GRANT SELECT ON TABLE "public"."t" TO "r" WITH GRANT OPTION'),
        ("app", 'REVOKE SELECT ON TABLE "public"."t" FROM "r"'),
    ]


@pytest.mark.parametrize("privs", [["CREATE VIEW"], ["SUPERUSER"], ["EXECUTE"]])
def test_pg_invalid_privileges_for_table_rejected(privs):
    # CREATE VIEW (no existe en PG), SUPERUSER (DENY), EXECUTE (no aplica a tabla).
    a, _ = _pg()
    with pytest.raises(AppHttpException) as exc:
        a.grant_object(R, GrantLevel.TABLE, ObjectRef(database="app", table="t"), privs)
    assert exc.value.status_code == 422
