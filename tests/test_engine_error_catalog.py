"""
Saneado de errores del motor (``engine_error_catalog``) y su uso en el historial de migraciones.

El texto nativo de MySQL/MariaDB/PostgreSQL incrusta VALORES de filas. El historial se lee con
``blueprints.read`` (viewer y agentes), así que lo que se persiste y se devuelve es un código del
vocabulario cerrado + el mensaje saneado; el crudo queda solo en el log con el Request ID.
"""

import logging
from datetime import datetime

import psycopg
import pymysql
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.services import engine_error_catalog as cat
from app.services.db_admin import migrations as mig
from app.services.db_admin.migrations import MigrationResult, MigrationRunner, MigrationSpec

EMAIL = "alice@x.com"


def _mysql(errno: int, msg: str) -> IntegrityError:
    return IntegrityError("INSERT INTO users …", {}, pymysql.err.IntegrityError(errno, msg))


def _pg(cls, msg: str) -> IntegrityError:
    return IntegrityError("INSERT INTO users …", {}, cls(msg))


# --------------------------------------------------------------------------- #
# Unit: saneado de strings reales                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, code, expected",
    [
        # MySQL/MariaDB, tal como lo guardaba ``str(exc.orig)`` (repr de la tupla de pymysql).
        (
            f'(1062, "Duplicate entry \'{EMAIL}\' for key \'users.email\'")',
            cat.CODE_DUPLICATE_KEY,
            "(1062) Duplicate entry '?' for key 'users.email'",
        ),
        (
            "(1265, \"Data truncated for column 'status' at row 12\")",
            cat.CODE_DATA_TRUNCATED,
            "(1265) Data truncated for column 'status' at row ?",
        ),
        (
            "(1366, \"Incorrect integer value: 'abc' for column 'age' at row 3\")",
            cat.CODE_INVALID_DATA,
            "(1366) Incorrect integer value: '?' for column 'age' at row ?",
        ),
        (
            "(1452, 'Cannot add or update a child row: a foreign key constraint fails "
            "(`app`.`orders`, CONSTRAINT `fk_user` FOREIGN KEY (`user_id`) REFERENCES "
            "`users` (`id`))')",
            cat.CODE_FK_VIOLATION,
            "(1452) Cannot add or update a child row: a foreign key constraint fails "
            "(`app`.`orders`, CONSTRAINT `fk_user` FOREIGN KEY (`user_id`) REFERENCES "
            "`users` (`id`))",
        ),
        (
            "(1064, \"You have an error in your SQL syntax; check the manual for the right "
            f"syntax to use near 'VALUES ('{EMAIL}', 'hunter2')' at line 1\")",
            cat.CODE_SYNTAX_ERROR,
            "(1064) You have an error in your SQL syntax; check the manual for the right "
            "syntax to use near '?' at line 1",
        ),
        (
            "(1045, \"Access denied for user 'gw'@'10.0.0.9' (using password: YES)\")",
            cat.CODE_PERMISSION_DENIED,
            "(1045) Access denied for user '?'@'?' (using password: YES)",
        ),
        (
            "(1205, 'Lock wait timeout exceeded; try restarting transaction')",
            cat.CODE_LOCK_TIMEOUT,
            "(1205) Lock wait timeout exceeded; try restarting transaction",
        ),
        # PostgreSQL: ``str()`` de psycopg NO trae SQLSTATE; la fila vieja se clasifica por texto.
        (
            'duplicate key value violates unique constraint "users_email_key"\n'
            f"DETAIL:  Key (email)=({EMAIL}) already exists.",
            cat.CODE_DUPLICATE_KEY,
            'duplicate key value violates unique constraint "users_email_key"',
        ),
        (
            'insert or update on table "orders" violates foreign key constraint '
            '"orders_user_id_fkey"\nDETAIL:  Key (user_id)=(42) is not present in table "users".',
            cat.CODE_FK_VIOLATION,
            'insert or update on table "orders" violates foreign key constraint '
            '"orders_user_id_fkey"',
        ),
        (
            'new row for relation "users" violates check constraint "age_pos"\n'
            f"DETAIL:  Failing row contains (1, {EMAIL}, -3).",
            cat.CODE_CONSTRAINT_VIOLATION,
            'new row for relation "users" violates check constraint "age_pos"',
        ),
        (
            'invalid input syntax for type integer: "abc"\nLINE 1: INSERT INTO t VALUES (\'abc\')',
            cat.CODE_INVALID_DATA,
            'invalid input syntax for type integer: "?"',
        ),
        (
            'value "99999999999" is out of range for type integer',
            cat.CODE_DATA_TRUNCATED,
            'value "?" is out of range for type integer',
        ),
    ],
)
def test_from_text_sanitizes_real_engine_messages(raw, code, expected):
    pub = cat.from_text(raw)
    assert pub.code == code
    assert pub.message == expected
    assert EMAIL not in pub.message
    assert pub.code in cat.ERROR_CODES


def test_unescaped_quote_inside_a_mysql_value_does_not_leak_the_tail():
    # MySQL no escapa la comilla interna: un barrido ingenuo cortaría en ``'O'`` y dejaría
    # pasar ``Brien-7``.
    pub = cat.from_text("(1062, \"Duplicate entry 'O'Brien-7' for key 'PRIMARY'\")")
    assert pub.message == "(1062) Duplicate entry '?' for key 'PRIMARY'"
    assert "Brien" not in pub.message


def test_a_truncated_row_with_an_unclosed_quote_masks_to_the_end():
    # Filas viejas truncadas a 500 caracteres: la comilla del valor nunca cierra.
    pub = cat.from_text("(1062, \"Duplicate entry 'alice-the-very-long-value")
    assert pub.code == cat.CODE_DUPLICATE_KEY
    assert "alice" not in pub.message


def test_unquoted_email_and_ip_are_masked_as_a_safety_net():
    pub = cat.from_text(f"could not connect to server at 10.1.2.3 as {EMAIL}")
    assert "10.1.2.3" not in pub.message
    assert EMAIL not in pub.message
    assert pub.code == cat.CODE_CONNECTION_LOST


@pytest.mark.parametrize(
    "raw",
    [
        f'(1062, "Duplicate entry \'{EMAIL}\' for key \'users.email\'")',
        'duplicate key value violates unique constraint "k"\nDETAIL:  Key (a)=(1) already exists.',
        "boom",
    ],
)
def test_from_text_is_idempotent(raw):
    once = cat.from_text(raw)
    assert cat.from_text(once.message) == once


def test_empty_text_has_no_public_error():
    assert cat.from_text(None) is None
    assert cat.from_text("  ") is None


def test_unknown_message_falls_back_to_unknown_code():
    assert cat.from_text("boom").code == cat.CODE_UNKNOWN


# --------------------------------------------------------------------------- #
# Unit: desde la excepción del driver                                          #
# --------------------------------------------------------------------------- #
def test_from_exception_mysql_uses_the_errno():
    pub = cat.from_exception(
        _mysql(1062, f"Duplicate entry '{EMAIL}' for key 'users.email'")
    )
    assert pub.code == cat.CODE_DUPLICATE_KEY
    assert pub.native_code == "1062"
    assert pub.message == "(1062) Duplicate entry '?' for key 'users.email'"


def test_from_exception_postgres_uses_the_sqlstate():
    pub = cat.from_exception(
        _pg(
            psycopg.errors.UniqueViolation,
            'duplicate key value violates unique constraint "users_email_key"\n'
            f"DETAIL:  Key (email)=({EMAIL}) already exists.",
        )
    )
    assert pub.code == cat.CODE_DUPLICATE_KEY
    assert pub.native_code == "23505"
    assert pub.message == '(23505) duplicate key value violates unique constraint "users_email_key"'


def test_from_exception_ignores_a_short_word_that_is_not_an_errno():
    # ``extract_driver_error_code`` acepta cualquier ``args[0]`` alfanumérico de <=5.
    exc = OperationalError("SELECT 1", {}, Exception("boom"))
    pub = cat.from_exception(exc)
    assert pub.native_code is None
    assert pub.code == cat.CODE_UNKNOWN


# --------------------------------------------------------------------------- #
# Runner: el fallo de un apply sale saneado y el crudo va al log               #
# --------------------------------------------------------------------------- #
class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def captured_log(monkeypatch):
    # ``get_logger`` pone ``propagate=False``: ``caplog`` no ve nada. Se intercepta el logger.
    handler = _ListHandler()
    logger = logging.getLogger("test.engine_error_catalog")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers = [handler]
    monkeypatch.setattr(mig, "logger", logger)
    return handler


def _spec():
    return MigrationSpec(
        id=1, version="0001", name="m", up_sql="INSERT INTO users VALUES (1)",
        up_sql_mysql=None, up_sql_postgresql=None, down_sql=None, checksum="x",
    )


def _run_failing_apply(monkeypatch, exc) -> MigrationResult:
    def boom(cfg, version):
        raise exc

    monkeypatch.setattr(mig.command, "upgrade", boom)
    monkeypatch.setattr(mig.migration_results, "begin", lambda *a, **k: None)
    monkeypatch.setattr(mig.migration_results, "finalize", lambda *a, **k: 0)
    monkeypatch.setattr(mig.migration_progress, "get_progress", lambda *a, **k: None)
    return MigrationRunner()._apply_one(
        cfg=None, spec=_spec(), managed_db_id=1, statement_total=1
    )


def test_apply_failure_is_sanitized_and_raw_goes_to_the_log(monkeypatch, captured_log):
    from app.core.context import current_http_identifier

    token = current_http_identifier.set("req-abc123")
    try:
        result = _run_failing_apply(
            monkeypatch, _mysql(1062, f"Duplicate entry '{EMAIL}' for key 'users.email'")
        )
    finally:
        current_http_identifier.reset(token)

    assert result.status == "failed"
    assert result.error_code == cat.CODE_DUPLICATE_KEY
    assert result.error == "(1062) Duplicate entry '?' for key 'users.email'"
    assert EMAIL not in result.error
    # El crudo SÍ está en el log, correlacionable por Request ID.
    raw_lines = [m for m in captured_log.messages if "req-abc123" in m]
    assert raw_lines and any(EMAIL in m for m in raw_lines)
    assert any(cat.CODE_DUPLICATE_KEY in m for m in raw_lines)


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
def _server(admin_client, port) -> int:
    return admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}", "host": "10.0.0.9", "port": port,
            "engine": "mysql", "root_username": "root", "root_password": "rootpw",
        },
    ).json()["data"]["id"]


def _managed_db(admin_client, port, model_id=None) -> int:
    sid = _server(admin_client, port)
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner1"}
    ).json()["data"]["id"]
    payload = {"server_id": sid, "owner_id": oid, "name": "errdb"}
    if model_id is not None:
        payload["model_id"] = model_id
    r = admin_client.post("/api/v1/managed-databases", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _insert_raw_history(db_id, raw_error):
    from app.core.database import Database
    from app.models.database_migration_history import DatabaseMigrationHistory
    from app.models.enums import MigrationStatus

    s = Database().get_declarative_base_session()
    try:
        s.add(
            DatabaseMigrationHistory(
                managed_database_id=db_id, model_migration_id=None,
                applied_at=datetime.now(), status=MigrationStatus("failed"),
                error=raw_error, execution_ms=1, direction="up", applied_version="0001",
            )
        )
        s.commit()
    finally:
        s.close()


def test_history_sanitizes_a_row_stored_raw_before_the_fix(admin_client):
    db_id = _managed_db(admin_client, 5481)
    # Fila guardada con el ``str(exc.orig)[:500]`` de antes: NO se reescribe en BD.
    _insert_raw_history(
        db_id, f'(1062, "Duplicate entry \'{EMAIL}\' for key \'users.email\'")'
    )

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/history")
    assert r.status_code == 200, r.text
    assert EMAIL not in r.text
    (item,) = r.json()["data"]
    assert item["error"] == "(1062) Duplicate entry '?' for key 'users.email'"
    assert item["error_code"] == cat.CODE_DUPLICATE_KEY


def test_history_sanitizes_a_raw_postgres_row(admin_client):
    db_id = _managed_db(admin_client, 5482)
    _insert_raw_history(
        db_id,
        'duplicate key value violates unique constraint "users_email_key"\n'
        f"DETAIL:  Key (email)=({EMAIL}) already exists.",
    )
    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/history")
    assert r.status_code == 200, r.text
    assert EMAIL not in r.text
    (item,) = r.json()["data"]
    assert item["error_code"] == cat.CODE_DUPLICATE_KEY


def test_apply_failure_response_and_history_are_sanitized(admin_client, monkeypatch):
    r = admin_client.post("/api/v1/database-models", json={"name": "errbp", "slug": "errbp"})
    model_id = r.json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m", "up_sql": "CREATE TABLE t1 (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 201, r.text
    mig_id = r.json()["data"]["id"]
    db_id = _managed_db(admin_client, 5483, model_id=model_id)

    pub = cat.from_exception(
        _mysql(1062, f"Duplicate entry '{EMAIL}' for key 'users.email'")
    )
    failed = MigrationResult(
        migration_id=mig_id, version="0001", status="failed", error=pub.message,
        error_code=pub.code, execution_ms=3, applied_at=datetime(2026, 6, 29, 12, 0, 0),
    )
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: None)
    monkeypatch.setattr(MigrationRunner, "apply", lambda self, *a, **k: [failed])

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply?on_failure=leave"
    )
    assert r.status_code == 200, r.text
    assert EMAIL not in r.text
    (res,) = r.json()["data"]["results"]
    assert res["error_code"] == cat.CODE_DUPLICATE_KEY
    assert res["error"] == "(1062) Duplicate entry '?' for key 'users.email'"

    h = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/history")
    assert EMAIL not in h.text
    assert h.json()["data"][0]["error_code"] == cat.CODE_DUPLICATE_KEY
