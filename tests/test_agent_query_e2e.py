"""
E2E de las lecturas de datos del agente contra motores REALES (S18, S28). Escrito, NUNCA ejecutado por
quien lo implementó: se SALTA solo si no hay motores alcanzables.

Marcado ``@pytest.mark.integration``. Mismos contenedores y puertos que
``tests/test_grants_integration.py`` (overridables con ``GW_IT_<ENGINE>_PORT/_USER/_PW``)::

    docker run -d --rm --name gw_it_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_it_maria -e MARIADB_ROOT_PASSWORD=rootpw \\
        -e MARIADB_ROOT_HOST=% -p 13400:3306 mariadb:11
    docker run -d --rm --name gw_it_pg -e POSTGRES_PASSWORD=rootpw -p 15499:5432 postgres:16
    .venv/bin/python scripts/run_tests_direct.py tests.test_agent_query_e2e

QUÉ SE FIJA, Y SOLO UN MOTOR REAL LO PRUEBA
-------------------------------------------
- **S28 (aislamiento por base):** la cuenta de datos de UNA base no lee otra base del mismo servidor.
- **El límite real es el motor:** un ``INSERT`` por la misma cuenta, dentro de la transacción READ ONLY,
  falla y no deja ninguna fila.
- **S18 (timeout):** una consulta legal pero costosa vence con ``QUERY_TIMEOUT`` y la sentencia deja de
  correr EN EL MOTOR (se consulta ``processlist`` / ``pg_stat_activity`` como root).
- **S18 (respaldo):** con el timeout de SESIÓN apagado a propósito, el cliente vence primero por el
  timeout de socket y el vigilante mata la sentencia desde una segunda conexión (``KILL QUERY``).

No usa el gateway ni su BD de metadatos: llama al servicio con una auditoría falsa. La parte del gate
(opt-in, sonda) está cubierta en ``tests/test_mcp_data_tools.py``.
"""

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.controllers.target_resolution import ReachableDatabase
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services.db_admin import agent_query as aq
from app.services.db_admin import agent_sql_policy as policy
from app.services.db_admin import query_policy as qp
from app.services.db_admin import query_runner as qr
from tests.test_grants_integration import _db_engine, _reachable, _root_engine, _spec

pytestmark = pytest.mark.integration

_USER = "aq_user"
_PW = "aqpw1"
_DB = "it_aq"
_OTHER = "it_aq_other"
_ENGINES = ["mysql", "mariadb", "postgresql"]
_HEAVY = {
    "mysql": "SELECT COUNT(*) FROM `big` a CROSS JOIN `big` b CROSS JOIN `big` c",
    "mariadb": "SELECT COUNT(*) FROM `big` a CROSS JOIN `big` b CROSS JOIN `big` c",
    "postgresql": 'SELECT COUNT(*) FROM "big" a CROSS JOIN "big" b CROSS JOIN "big" c',
}


class _Audit:
    def record_intent(self, action, **kw):
        pass

    def record(self, action, **kw):
        pass


def _setup(engine_key: str) -> dict:
    spec = _spec(engine_key)
    if not _reachable(spec):
        pytest.skip(f"Motor {engine_key} no alcanzable en 127.0.0.1:{spec['port']}")
    pg = engine_key == "postgresql"
    root = _root_engine(engine_key, spec)
    with root.connect() as c:
        for db in (_DB, _OTHER):
            c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
        if pg:
            c.execute(text(f"DROP ROLE IF EXISTS {_USER}"))
            c.execute(text(f"CREATE ROLE {_USER} LOGIN PASSWORD '{_PW}' NOSUPERUSER CONNECTION LIMIT 3"))
            for db in (_DB, _OTHER):
                c.execute(text(f"CREATE DATABASE {db}"))
                c.execute(text(f"REVOKE CONNECT ON DATABASE {db} FROM PUBLIC"))
            c.execute(text(f"GRANT CONNECT ON DATABASE {_DB} TO {_USER}"))
        else:
            c.execute(text(f"DROP USER IF EXISTS '{_USER}'@'%'"))
            c.execute(text(f"CREATE USER '{_USER}'@'%' IDENTIFIED BY '{_PW}'"))
            for db in (_DB, _OTHER):
                c.execute(text(f"CREATE DATABASE {db}"))
            c.execute(text(f"GRANT SELECT ON `{_DB}`.* TO '{_USER}'@'%'"))
    with _db_engine(engine_key, spec, _DB).connect() as c:
        c.execute(text("CREATE TABLE items (id INT PRIMARY KEY, name VARCHAR(50))"))
        c.execute(text("INSERT INTO items VALUES (1,'uno'),(2,'dos'),(3,'tres')"))
        c.execute(text("CREATE TABLE big (id INT)"))
        c.execute(text("INSERT INTO big VALUES (:i)"), [{"i": i} for i in range(3000)])
        if pg:
            c.execute(text(f"GRANT USAGE ON SCHEMA public TO {_USER}"))
            c.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_USER}"))
    with _db_engine(engine_key, spec, _OTHER).connect() as c:
        c.execute(text("CREATE TABLE secrets (id INT)"))
        c.execute(text("INSERT INTO secrets VALUES (1)"))
    return {"spec": spec, "engine": engine_key, "pg": pg}


def _teardown(ctx: dict) -> None:
    try:
        with _root_engine(ctx["engine"], ctx["spec"]).connect() as c:
            for db in (_DB, _OTHER):
                c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            if ctx["pg"]:
                c.execute(text(f"DROP ROLE IF EXISTS {_USER}"))
            else:
                c.execute(text(f"DROP USER IF EXISTS '{_USER}'@'%'"))
    except Exception:  # noqa: BLE001 — limpieza best-effort
        pass


@pytest.fixture(autouse=True)
def _sin_auditoria(monkeypatch):
    monkeypatch.setattr(aq, "audit", _Audit())


def _target(ctx) -> ServerTarget:
    return ServerTarget(
        server_id=1, dialect=ctx["engine"], host="127.0.0.1", port=ctx["spec"]["port"],
        admin_user=_USER, admin_password=_PW,
    )


def _resolved(ctx, database=_DB) -> ReachableDatabase:
    return ReachableDatabase(
        database_id=1, database=database, server_id=1, engine=ctx["engine"],
        environment_slug="development", model_id=None, model_slug=None, model_version=None,
    )


def _ejecutar(ctx, sql, *, database=_DB, max_rows=100, timeout_ms=None):
    verdict = policy.validate_agent_select(
        sql, engine=ctx["engine"], database=database, max_rows=max_rows
    )
    assert verdict.accepted, verdict.reasons
    return aq.run_agent_select(
        aq.AuditContext(actor=SimpleNamespace(token_id="e2e"), tool="e2e", database_id=1, server_id=1),
        resolved=_resolved(ctx, database),
        target=_target(ctx),
        credential=qr.QueryCredential(qr.MODE_STORED, _USER, _PW),
        verdict=verdict,
        max_rows=max_rows,
        timeout_ms=timeout_ms,
    )


def _activas(ctx) -> int:
    """Sentencias de la cuenta de datos que SIGUEN corriendo en el motor, vistas como root."""
    with _root_engine(ctx["engine"], ctx["spec"]).connect() as c:
        if ctx["pg"]:
            return c.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE usename = :u AND state = 'active'"),
                {"u": _USER},
            ).scalar()
        return c.execute(
            text(
                "SELECT count(*) FROM information_schema.processlist "
                "WHERE user = :u AND command = 'Query' AND id <> CONNECTION_ID() "
                "AND info LIKE '%CROSS JOIN%'"
            ),
            {"u": _USER},
        ).scalar()


def _espera_sin_activas(ctx, segundos=6.0) -> bool:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if _activas(ctx) == 0:
            return True
        time.sleep(0.25)
    return False


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_the_data_account_reads_its_own_database_with_the_row_cap(engine_key):
    ctx = _setup(engine_key)
    try:
        sobre = _ejecutar(ctx, "SELECT id, name FROM items ORDER BY id", max_rows=2)
        assert sobre["data"]["rows"] == [[1, "uno"], [2, "dos"]]
        assert sobre["truncated"] is True and sobre["truncation_reason"] == "row_cap"
        assert sobre["executed_sql"].rstrip().endswith("LIMIT 3")
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_s28_the_account_of_one_database_cannot_read_another(engine_key):
    ctx = _setup(engine_key)
    try:
        with pytest.raises(AppHttpException) as exc:
            _ejecutar(ctx, "SELECT id FROM secrets", database=_OTHER)
        assert exc.value.public_context["code"] in ("PROBE_NOT_GREEN", "QUERY_FAILED")
        # Y el motor lo confirma: la fila de la otra base no salió por ningún camino.
        with _db_engine(engine_key, ctx["spec"], _OTHER).connect() as c:
            assert c.execute(text("SELECT count(*) FROM secrets")).scalar() == 1
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_the_engine_itself_refuses_a_write_from_the_data_account(engine_key):
    """El límite real: aun saltándose al validador, la cuenta es SELECT-only y la transacción READ ONLY."""
    ctx = _setup(engine_key)
    try:
        salida = qr.run_statements(
            _target(ctx),
            database=_DB,
            engine=engine_key,
            statements=[qp.StatementPlan(seq=1, sql="INSERT INTO items VALUES (99, 'x')",
                                         kind="insert", danger=qp.WRITE)],
            credential=qr.QueryCredential(qr.MODE_STORED, _USER, _PW),
            read_only=True,
            max_rows=10,
            max_cell_chars=100,
            timeout_ms=5000,
        )
        assert salida.statements[0].success is False and salida.rolled_back is True
        with _db_engine(engine_key, ctx["spec"], _DB).connect() as c:
            assert c.execute(text("SELECT count(*) FROM items WHERE id = 99")).scalar() == 0
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", _ENGINES)
def test_s18_a_costly_query_times_out_and_stops_running_on_the_engine(engine_key):
    ctx = _setup(engine_key)
    try:
        inicio = time.monotonic()
        with pytest.raises(AppHttpException) as exc:
            _ejecutar(ctx, _HEAVY[engine_key], timeout_ms=1000)
        assert exc.value.public_context["code"] == "QUERY_TIMEOUT"
        assert time.monotonic() - inicio < 10
        assert _espera_sin_activas(ctx), "la sentencia sigue corriendo en el motor"
    finally:
        _teardown(ctx)


@pytest.mark.parametrize("engine_key", ["mysql", "mariadb"])
def test_s18_the_watchdog_kills_the_statement_when_the_session_timeout_is_not_honored(
    engine_key, monkeypatch
):
    """
    Sin variables de sesión (``max_execution_time``/``max_statement_time``) el servidor no corta solo:
    vence el SOCKET del cliente (1 s) y la sentencia quedaría corriendo. ``kill_now`` abre otra conexión
    y la mata con ``KILL QUERY``.
    """
    ctx = _setup(engine_key)
    try:
        monkeypatch.setattr(qr, "apply_session_timeouts", lambda conn, engine, timeout_ms: None)
        with pytest.raises(AppHttpException) as exc:
            _ejecutar(ctx, _HEAVY[engine_key], timeout_ms=1000)
        assert exc.value.public_context["code"] == "QUERY_TIMEOUT"
        assert _espera_sin_activas(ctx), "el KILL QUERY del vigilante no detuvo la sentencia"
    finally:
        _teardown(ctx)
