"""
Credencial de DATOS por base: adapters (SQL real, conexión falsa) y controller/rutas (adapter falso).

Dos niveles, igual que ``test_readonly_provision_sql.py`` + ``test_readonly_provision.py``:

- Adapters: corren los métodos de verdad de ``MySQLAdapter``/``MariaDBAdapter``/``PostgresAdapter``;
  lo único falso es la conexión, y las mutaciones se capturan sin ejecutarse. Se afirma lo que más
  importa: UN solo ``GRANT``, ``SELECT`` y nada más, escapado, sobre UNA base.
- Controller: el adapter es un fake que registra qué se le pidió y en qué orden respecto de la
  auditoría y de la persistencia (409, lock, intención antes del motor, credencial antes del motor).
"""

import re
from types import SimpleNamespace

import pytest

from app.core.crypto import decrypt
from app.core.database import Database
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.models.managed_database import ManagedDatabase
from app.models.managed_database_data_credential import ManagedDatabaseDataCredential
from app.services import audit as audit_mod
from app.services.db_admin import postgres_adapter as pg_mod
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.identifiers import quote_string_literal
from app.services.db_admin.mysql_adapter import (
    DATA_ACCOUNT_MAX_USER_CONNECTIONS,
    MariaDBAdapter,
    MySQLAdapter,
)
from app.services.db_admin.postgres_adapter import PostgresAdapter
from app.services.db_admin.readonly_probe import ReadonlyPreflight

USER = "mcp_d_7"
HOST = "10.0.%"
PWD = "pa'ss\\w0rd"


# --------------------------------------------------------------------------- #
# Adapters: SQL real                                                           #
# --------------------------------------------------------------------------- #


class _Res:
    def __init__(self, rows=None):
        self._rows = rows or []

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _Conn:
    """Conexión guionada para el preflight de PostgreSQL (``pg_roles``) y su registro."""

    def __init__(self, exists=False):
        self.exists = exists
        self.log: list[str] = []

    def execute(self, clause, params=None):
        sql = str(clause)
        self.log.append(sql)
        if sql.startswith("SELECT 1 FROM pg_roles"):
            return _Res(rows=[(1,)] if self.exists else [])
        raise AssertionError(f"sentencia inesperada: {sql}")


class _ConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __call__(self, _target):
        return self

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _target(dialect):
    return ServerTarget(
        server_id=1, dialect=dialect, host="127.0.0.1", port=3306,
        admin_user="root", admin_password="x",
    )


def _mysql(monkeypatch, cls=MySQLAdapter):
    adapter = cls(_target("mariadb" if cls is MariaDBAdapter else "mysql"))
    muts: list[str] = []
    monkeypatch.setattr(adapter, "_execute_server", lambda stmts, **kw: muts.extend(stmts))
    return adapter, muts


def _pg(monkeypatch, *, exists=False, schemas=("public", "ventas")):
    conn = _Conn(exists=exists)
    monkeypatch.setattr(pg_mod, "server_connection", _ConnCtx(conn))
    adapter = PostgresAdapter(_target("postgresql"))
    monkeypatch.setattr(adapter, "_data_schemas", lambda database: list(schemas))
    muts: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(
        adapter, "_execute_server", lambda stmts, **kw: muts.extend(("server", None, s) for s in stmts)
    )
    monkeypatch.setattr(
        adapter,
        "_execute_database",
        lambda db, stmts, **kw: muts.extend(("database", db, s) for s in stmts),
    )
    return adapter, conn, muts


@pytest.mark.parametrize("cls", [MySQLAdapter, MariaDBAdapter])
def test_mysql_family_grants_exactly_one_select_on_one_database(monkeypatch, cls):
    adapter, muts = _mysql(monkeypatch, cls)
    adapter.provision_data_account(USER, PWD, HOST, "app_prod", ReadonlyPreflight(exists=False))
    grants = [m for m in muts if m.startswith("GRANT ")]
    assert grants == [f"GRANT SELECT ON `app\\_prod`.* TO {muts[0].split(' IF NOT EXISTS ')[1].split(' IDENTIFIED')[0]}"]
    assert muts[0].startswith("CREATE USER IF NOT EXISTS ")
    assert muts[1].startswith("ALTER USER ")
    assert muts[2].startswith("REVOKE ALL PRIVILEGES, GRANT OPTION FROM ")
    assert muts[3] == grants[0]  # el GRANT es lo último: la cuenta queda EXACTAMENTE con SELECT


def test_mysql_never_grants_structure_global_or_grant_option(monkeypatch):
    adapter, muts = _mysql(monkeypatch)
    # Un preflight con SHOW_ROUTINE global NO se aplica: la cuenta de datos no lleva *.*.
    pre = ReadonlyPreflight(exists=True, global_grants=("SHOW_ROUTINE",))
    adapter.provision_data_account(USER, PWD, HOST, "app", pre)
    text_all = "\n".join(muts).upper()
    assert " ON *.* " not in text_all
    for forbidden in ("SHOW VIEW", "TRIGGER", "EVENT", "SHOW_ROUTINE", "WITH GRANT OPTION"):
        assert forbidden not in text_all.replace("REVOKE ALL PRIVILEGES, GRANT OPTION", "")


def test_mysql_connection_cap_and_mariadb_statement_time(monkeypatch):
    mysql, m1 = _mysql(monkeypatch, MySQLAdapter)
    mysql.provision_data_account(USER, PWD, HOST, "app", ReadonlyPreflight(exists=False))
    cap = DATA_ACCOUNT_MAX_USER_CONNECTIONS
    assert m1[1].endswith(f"WITH MAX_USER_CONNECTIONS {cap}")
    maria, m2 = _mysql(monkeypatch, MariaDBAdapter)
    maria.provision_data_account(USER, PWD, HOST, "app", ReadonlyPreflight(exists=False))
    assert m2[1].endswith(f"WITH MAX_USER_CONNECTIONS {cap} MAX_STATEMENT_TIME 30")


def test_mysql_quotes_user_host_and_password_with_the_existing_helpers(monkeypatch):
    adapter, muts = _mysql(monkeypatch)
    adapter.provision_data_account(USER, PWD, HOST, "app", ReadonlyPreflight(exists=False))
    who = f"{quote_string_literal(USER, 'mysql')}@{quote_string_literal(HOST, 'mysql')}"
    pwd = quote_string_literal(PWD, "mysql")
    assert muts[0] == f"CREATE USER IF NOT EXISTS {who} IDENTIFIED BY {pwd}"
    assert muts[3] == f"GRANT SELECT ON `app`.* TO {who}"
    assert "pa'ss" not in muts[0].replace("pa''ss", "")


def test_mysql_rerun_issues_the_same_idempotent_statements(monkeypatch):
    """S28: re-correr converge (CREATE IF NOT EXISTS + ALTER + REVOKE ALL + GRANT) sin chocar."""
    a1, first = _mysql(monkeypatch)
    a1.provision_data_account(USER, PWD, HOST, "app", ReadonlyPreflight(exists=False))
    a2, second = _mysql(monkeypatch)
    a2.provision_data_account(USER, PWD, HOST, "app", ReadonlyPreflight(exists=True))
    assert first == second


def test_a_database_name_with_a_backtick_never_reaches_the_sql(monkeypatch):
    adapter, muts = _mysql(monkeypatch)
    with pytest.raises(AppHttpException) as exc:
        adapter.provision_data_account(
            USER, PWD, HOST, "a`b; DROP USER x", ReadonlyPreflight(exists=False)
        )
    assert exc.value.status_code == 422
    assert muts == []


def test_mysql_revoke_is_a_drop_user_if_exists(monkeypatch):
    adapter, muts = _mysql(monkeypatch)
    adapter.revoke_data_account(USER, HOST, "app")
    assert len(muts) == 1 and muts[0].startswith("DROP USER IF EXISTS ")


def test_postgres_role_attributes_limits_and_per_schema_select(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch)
    adapter.provision_data_account(USER, PWD, "%", "app_prod", ReadonlyPreflight(exists=False))
    server = [s for lvl, _db, s in muts if lvl == "server"]
    assert server[0].startswith('CREATE ROLE "mcp_d_7" WITH LOGIN PASSWORD ')
    for attr in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOINHERIT", "NOREPLICATION",
                 "NOBYPASSRLS", "CONNECTION LIMIT 3"):
        assert attr in server[0]
    assert 'ALTER ROLE "mcp_d_7" SET default_transaction_read_only = on' in server
    assert "ALTER ROLE \"mcp_d_7\" SET statement_timeout = '30s'" in server
    assert 'GRANT CONNECT ON DATABASE "app_prod" TO "mcp_d_7"' in server
    dbl = [(db, s) for lvl, db, s in muts if lvl == "database"]
    assert {db for db, _s in dbl} == {"app_prod"}
    stmts = [s for _db, s in dbl]
    for sch in ('"public"', '"ventas"'):
        assert f'GRANT USAGE ON SCHEMA {sch} TO "mcp_d_7"' in stmts
        assert f'GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO "mcp_d_7"' in stmts


def test_postgres_grants_no_write_privilege_and_no_default_privileges(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch)
    adapter.provision_data_account(USER, PWD, "%", "app", ReadonlyPreflight(exists=True))
    allsql = [s for _l, _d, s in muts]
    assert any(s.startswith("ALTER ROLE") and "WITH LOGIN" in s for s in allsql)  # exists -> ALTER
    for s in allsql:
        assert "ALTER DEFAULT PRIVILEGES" not in s
        if s.startswith("GRANT "):
            assert re.search(r"INSERT|UPDATE|DELETE|TRUNCATE|REFERENCES|TRIGGER|CREATE", s) is None
            assert "ALL PRIVILEGES" not in s


def test_postgres_revoke_is_idempotent_and_drops_owned_before_the_role(monkeypatch):
    adapter, _conn, muts = _pg(monkeypatch, exists=False)
    adapter.revoke_data_account(USER, "%", "app")
    assert muts == []  # sin rol no hay nada que revocar
    adapter, _conn, muts = _pg(monkeypatch, exists=True)
    adapter.revoke_data_account(USER, "%", "app")
    assert muts == [
        ("database", "app", 'DROP OWNED BY "mcp_d_7"'),
        ("server", None, 'DROP ROLE IF EXISTS "mcp_d_7"'),
    ]


def test_base_adapter_default_is_fail_closed_422():
    fake = SimpleNamespace(dialect="sqlite")
    with pytest.raises(AppHttpException) as exc:
        ServerAdapter.provision_data_account(fake, "u", "p", "%", "d", ReadonlyPreflight(False))
    assert exc.value.status_code == 422
    with pytest.raises(AppHttpException) as exc:
        ServerAdapter.revoke_data_account(fake, "u", "%", "d")
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# Controller y rutas                                                           #
# --------------------------------------------------------------------------- #

PROVISION = "/api/v1/managed-databases/{db}/data-credential/provision"
CLEAR = "/api/v1/managed-databases/{db}/data-credential"


def _server(admin_client, port=3399) -> int:
    payload = {
        "name": f"srv{port}",
        "host": "10.0.0.9",
        "port": port,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    return admin_client.post("/api/v1/servers", json=payload).json()["data"]["id"]


def _database(admin_client, name="app_prod", port=3399, active=True) -> int:
    sid = _server(admin_client, port)
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": f"own{port}"}
    ).json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases", json={"server_id": sid, "owner_id": oid, "name": name}
    )
    assert r.status_code == 201, r.text
    db_id = r.json()["data"]["id"]
    if active:
        s = Database().get_declarative_base_session()
        try:
            s.get(ManagedDatabase, db_id).status = "active"
            s.commit()
        finally:
            s.close()
    return db_id


def _row(db_id):
    s = Database().get_declarative_base_session()
    try:
        row = (
            s.query(ManagedDatabaseDataCredential)
            .filter(ManagedDatabaseDataCredential.managed_database_id == db_id)
            .first()
        )
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


def _set_opt_in(db_id):
    s = Database().get_declarative_base_session()
    try:
        row = (
            s.query(ManagedDatabaseDataCredential)
            .filter(ManagedDatabaseDataCredential.managed_database_id == db_id)
            .first()
        )
        row.data_access_allowed = True
        s.commit()
    finally:
        s.close()


def _audit_rows(prefix="managed_database.data_credential."):
    s = Database().get_declarative_base_session()
    try:
        return [
            (a.action, a.status) for a in s.query(AuditLog).all() if a.action.startswith(prefix)
        ]
    finally:
        s.close()


class _Motor:
    def __init__(self):
        self.events: list[str] = []
        self.provisioned = []  # (usuario, password, host, base)
        self.revoked = []  # (usuario, host, base)
        self.preflights = []
        self.exists = False
        self.fail_provision = False
        self.fail_revoke = False
        self.stored_at_engine_time = None  # (existe fila, descifra == password, verified_at)

    def adapter(self, target):
        return _Adapter(self)


class _Adapter:
    def __init__(self, motor):
        self._m = motor

    def preflight_readonly_account(self, username, host):
        self._m.preflights.append((username, host))
        return ReadonlyPreflight(exists=self._m.exists)

    def is_privileged_role(self, username):
        return False

    def provision_data_account(self, username, password, host, database, preflight):
        self._m.events.append("engine")
        row = _row(int(username.rsplit("_", 1)[1]))
        self._m.stored_at_engine_time = (
            row is not None and decrypt(row.password_encrypted) == password,
            None if row is None else row.verified_at,
        )
        if self._m.fail_provision:
            raise AppHttpException(message="El motor rechazó la operación.", status_code=502)
        self._m.provisioned.append((username, password, host, database))
        self._m.exists = True

    def revoke_data_account(self, username, host, database):
        if self._m.fail_revoke:
            raise AppHttpException(message="El motor no contesta.", status_code=502)
        self._m.revoked.append((username, host, database))
        self._m.exists = False


@pytest.fixture()
def motor(monkeypatch):
    import app.controllers.managed_database_controller as mdc

    m = _Motor()
    monkeypatch.setattr(mdc, "get_adapter", m.adapter)
    original = audit_mod.record_intent

    def spy(action, **kw):
        if action.startswith("managed_database.data_credential."):
            m.events.append("intent")
        return original(action, **kw)

    monkeypatch.setattr(audit_mod, "record_intent", spy)
    # La base de metadatos del gateway "co-alojada": nunca es elegible.
    monkeypatch.setattr(mdc, "DB_NAME", "datum_meta")
    monkeypatch.setattr(mdc, "DB_HOST", "10.0.0.9")
    monkeypatch.setattr(mdc, "DB_PORT", 3399)
    return m


def test_requires_auth(client):
    assert client.post(PROVISION.format(db=1)).status_code == 401
    assert client.delete(CLEAR.format(db=1)).status_code == 401


def test_provision_creates_stores_encrypted_and_leaves_it_unverified(
    admin_client, motor
):
    db_id = _database(admin_client)
    r = admin_client.post(PROVISION.format(db=db_id))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_data_credential"] is True
    assert data["verified_at"] is None  # la sonda es de la slice siguiente
    assert data["data_access_allowed"] is False
    assert not ({"username", "password", "password_encrypted"} & set(data))

    usuario, password, host, base = motor.provisioned[0]
    assert usuario == f"mcp_d_{db_id}" and len(usuario) <= 32
    assert len(password) >= 32 and host == "%" and base == "app_prod"
    row = _row(db_id)
    assert row.username == usuario
    assert row.password_encrypted != password
    assert decrypt(row.password_encrypted) == password
    assert row.verified_at is None


def test_the_request_body_is_not_accepted_as_input(admin_client, motor):
    db_id = _database(admin_client)
    r = admin_client.post(
        PROVISION.format(db=db_id),
        json={"password": "elegida", "username": "root", "grants": ["ALL PRIVILEGES"]},
    )
    assert r.status_code == 200, r.text
    usuario, password, _h, _b = motor.provisioned[0]
    assert usuario == f"mcp_d_{db_id}" and password != "elegida"


def test_audit_intent_and_the_stored_credential_come_before_the_engine_change(
    admin_client, motor
):
    db_id = _database(admin_client)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    assert motor.events == ["intent", "engine"]
    stored_ok, verified = motor.stored_at_engine_time
    assert stored_ok is True and verified is None  # ya guardada, cifrada y sin verificar
    assert ("managed_database.data_credential.provision", "attempt") in _audit_rows()
    assert ("managed_database.data_credential.provision", "success") in _audit_rows()


def test_running_it_twice_converges_and_keeps_the_opt_in(admin_client, motor):
    db_id = _database(admin_client)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    primera = decrypt(_row(db_id).password_encrypted)
    _set_opt_in(db_id)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    segunda = decrypt(_row(db_id).password_encrypted)
    assert primera != segunda and motor.provisioned[-1][1] == segunda
    assert _row(db_id).data_access_allowed is True  # re-aprovisionar no revoca una decisión humana


def test_an_existing_account_that_is_not_ours_is_refused_without_changes(admin_client, motor):
    db_id = _database(admin_client)
    motor.exists = True  # 'mcp_d_<id>' existe en el motor y el gateway no la tiene guardada
    r = admin_client.post(PROVISION.format(db=db_id))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "data_credential.account_already_exists"
    assert motor.provisioned == [] and motor.events == []  # ni intención ni motor
    assert _row(db_id) is None
    assert not [e for e in _audit_rows() if e[0].endswith(".provision")]


def test_a_retry_after_an_engine_failure_converges_because_the_account_is_ours(
    admin_client, motor
):
    db_id = _database(admin_client)
    motor.fail_provision = True
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 502
    assert _row(db_id) is not None and _row(db_id).verified_at is None  # propiedad registrada
    assert ("managed_database.data_credential.provision", "error") in _audit_rows()

    motor.exists = True  # la cuenta quedó creada antes de fallar
    motor.fail_provision = False
    r = admin_client.post(PROVISION.format(db=db_id))
    assert r.status_code == 200, r.text
    assert motor.provisioned[-1][1] == decrypt(_row(db_id).password_encrypted)


def test_a_provision_in_progress_returns_409_and_changes_nothing(admin_client, motor):
    import app.controllers.server_controller as sc

    db_id = _database(admin_client)
    assert sc._try_acquire_provision(("data", db_id)) is True
    try:
        r = admin_client.post(PROVISION.format(db=db_id))
        rc = admin_client.delete(CLEAR.format(db=db_id))
    finally:
        sc._release_provision(("data", db_id))
    assert r.status_code == 409 and rc.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "data_credential.provision_in_progress"
    assert motor.preflights == [] and motor.events == []
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200  # liberado: procede


def test_the_lock_is_per_database_and_released_after_a_failure(admin_client, motor):
    import app.controllers.server_controller as sc

    db_id = _database(admin_client)
    motor.fail_provision = True
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 502
    assert ("data", db_id) not in sc._PROVISIONING
    # El lock de datos no choca con el de un servidor del mismo id numérico.
    assert sc._try_acquire_provision(db_id) is True
    sc._release_provision(db_id)


def test_a_pending_database_is_not_eligible(admin_client, motor):
    db_id = _database(admin_client, active=False)
    r = admin_client.post(PROVISION.format(db=db_id))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "data_credential.database_not_eligible"
    assert motor.events == [] and _row(db_id) is None


def test_the_gateway_metadata_database_is_not_eligible(admin_client, motor):
    db_id = _database(admin_client, name="datum_meta")
    r = admin_client.post(PROVISION.format(db=db_id))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "data_credential.database_not_eligible"
    assert motor.events == []


def test_an_unknown_database_is_404(admin_client, motor):
    assert admin_client.post(PROVISION.format(db=987654)).status_code == 404
    assert admin_client.delete(CLEAR.format(db=987654)).status_code == 404


def test_clear_revokes_in_the_engine_and_deletes_the_row(admin_client, motor):
    db_id = _database(admin_client)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    _set_opt_in(db_id)
    r = admin_client.delete(CLEAR.format(db=db_id))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["has_data_credential"] is False
    assert motor.revoked == [(f"mcp_d_{db_id}", "%", "app_prod")]
    assert _row(db_id) is None
    assert ("managed_database.data_credential.clear", "success") in _audit_rows()


def test_clear_is_idempotent_and_never_drops_an_account_it_does_not_own(admin_client, motor):
    db_id = _database(admin_client)
    motor.exists = True  # una cuenta homónima ajena: sin fila, el gateway no la toca
    for _ in range(2):
        r = admin_client.delete(CLEAR.format(db=db_id))
        assert r.status_code == 200, r.text
        assert r.json()["data"]["has_data_credential"] is False
    assert motor.revoked == []


def test_clear_cuts_access_first_and_keeps_the_row_when_the_engine_fails(admin_client, motor):
    db_id = _database(admin_client)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    s = Database().get_declarative_base_session()
    try:
        from datetime import datetime

        row = (
            s.query(ManagedDatabaseDataCredential)
            .filter(ManagedDatabaseDataCredential.managed_database_id == db_id)
            .first()
        )
        row.verified_at = datetime(2026, 1, 1)
        row.data_access_allowed = True
        s.commit()
    finally:
        s.close()

    motor.fail_revoke = True
    assert admin_client.delete(CLEAR.format(db=db_id)).status_code == 502
    row = _row(db_id)
    assert row is not None  # queda para reintentar la revocación
    assert row.verified_at is None and row.data_access_allowed is False  # corte inmediato
    assert ("managed_database.data_credential.clear", "error") in _audit_rows()

    motor.fail_revoke = False
    assert admin_client.delete(CLEAR.format(db=db_id)).status_code == 200
    assert _row(db_id) is None and motor.revoked == [(f"mcp_d_{db_id}", "%", "app_prod")]
