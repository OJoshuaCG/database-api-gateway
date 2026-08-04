"""
Orquestación de ``query_runner.run_statements``/``estimate_impact`` con una conexión
SIMULADA (sin motor real). Antes de este archivo, este comportamiento solo se había
verificado con un script suelto fuera de la suite; estos tests lo llevan a ``tests/``.

Por qué un doble de conexión y no SQLite real (a diferencia de ``test_data_copy.py``,
que sí usa SQLite real): las sentencias de sesión que ``query_runner`` emite
(``START TRANSACTION READ ONLY`` / ``SET TRANSACTION READ ONLY`` / ``SET ROLE`` /
``SET SESSION max_execution_time``) son sintaxis de MySQL/PostgreSQL que SQLite no
entiende, y lo que estos tests verifican es precisamente el ORDEN y el CONTENIDO de esas
sentencias — no el resultado de ejecutar SQL de negocio. Un doble que graba las llamadas
a ``exec_driver_sql`` es más simple y menos frágil que forzar un dialecto ajeno sobre
SQLite (mismo criterio que el fake de ``test_migration_reconcile_partial.py``).

Lo que NO se puede probar con este doble (queda para el script e2e):
- que el motor REAL rechace una escritura dentro de la transacción de solo lectura,
- el mensaje nativo exacto de un rechazo de permisos,
- que ``stream_results`` realmente evite traer la tabla entera por red.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services.db_admin import query_policy as qp
from app.services.db_admin import query_runner as qr


# --------------------------------------------------------------------------- #
# Doble de conexión                                                            #
# --------------------------------------------------------------------------- #
class _FakeResult:
    """Simula un ``CursorResult`` de SQLAlchemy."""

    def __init__(self, columns=None, rows=None, rowcount=None, scalar=None):
        self._columns = columns or []
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self._scalar = scalar

    @property
    def returns_rows(self):
        return bool(self._columns)

    def keys(self):
        return self._columns

    def fetchmany(self, n):
        got, self._rows = self._rows[:n], self._rows[n:]
        return got

    def fetchone(self):
        return (self._scalar,) if self._scalar is not None else None

    def close(self):
        pass


class _FakeConn:
    """
    Graba cada ``exec_driver_sql`` y delega en ``handler(sql) -> _FakeResult`` (o
    ``handler`` lanza). ``handler`` por defecto devuelve un resultado vacío sin filas.
    """

    def __init__(self, handler=None):
        self.calls: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.savepoints = 0
        self._handler = handler or (lambda sql: _FakeResult())

    def execution_options(self, **kw):
        return self

    def begin_nested(self):
        # SAVEPOINT por conteo: sin esto, en PostgreSQL el primer COUNT que falla
        # aborta la transacción y los siguientes devuelven 25P02.
        conn = self

        class _Savepoint:
            def __enter__(self):
                conn.savepoints += 1
                return self

            def __exit__(self, *exc):
                return False

        return _Savepoint()

    def exec_driver_sql(self, sql):
        self.calls.append(sql)
        return self._handler(sql)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _install_conn(monkeypatch, conn, *, raise_on_enter: Exception | None = None):
    """Monkeypatchea ``query_runner.database_connection`` para devolver ``conn``."""

    @contextmanager
    def _fake_database_connection(target, database, **kw):
        if raise_on_enter is not None:
            raise raise_on_enter
        yield conn

    monkeypatch.setattr(qr, "database_connection", _fake_database_connection)


def _target(dialect="mysql") -> ServerTarget:
    return ServerTarget(
        server_id=1, dialect=dialect, host="127.0.0.1", port=3306,
        admin_user="root", admin_password="secret",
    )


def _cred(mode=qr.MODE_ADMIN, username="root", **kw) -> qr.QueryCredential:
    return qr.QueryCredential(mode=mode, username=username, **kw)


def _plan(sql, danger=qp.READ, kind="select", seq=0) -> qp.StatementPlan:
    return qp.StatementPlan(seq=seq, sql=sql, kind=kind, danger=danger)


def _run(monkeypatch, conn, *, statements, engine="mysql", read_only=False,
          dry_run=False, credential=None, timeout_ms=0, **kw):
    _install_conn(monkeypatch, conn)
    return qr.run_statements(
        _target(engine),
        database="db",
        engine=engine,
        statements=statements,
        credential=credential or _cred(),
        read_only=read_only,
        dry_run=dry_run,
        max_rows=1000,
        max_cell_chars=4096,
        timeout_ms=timeout_ms,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Orden de preparación de la sesión                                            #
# --------------------------------------------------------------------------- #
def test_mysql_pone_el_timeout_antes_que_la_transaccion_de_solo_lectura(monkeypatch):
    """``START TRANSACTION`` cerraría la transacción implícita que abre un ``SET``."""
    conn = _FakeConn()
    _run(monkeypatch, conn, statements=[_plan("SELECT 1")], engine="mysql",
         read_only=True, timeout_ms=5000)
    # Se emiten los timeouts de AMBOS motores (cada uno es no-op en el otro) y recién
    # después se abre la transacción de solo lectura; la sentencia va última.
    assert "SET SESSION max_execution_time = 5000" in conn.calls
    assert conn.calls.index("SET SESSION max_execution_time = 5000") < conn.calls.index(
        "START TRANSACTION READ ONLY"
    )
    assert conn.calls.index("START TRANSACTION READ ONLY") < conn.calls.index("SELECT 1")
    # Sin lock_wait_timeout, un ALTER esperando un metadata lock no tiene techo real:
    # el default de MySQL es de UN AÑO.
    assert "SET SESSION lock_wait_timeout = 5" in conn.calls
    assert "SET SESSION innodb_lock_wait_timeout = 5" in conn.calls


def test_postgres_impersonate_pone_set_role_despues_del_read_only(monkeypatch):
    """``SET TRANSACTION READ ONLY`` deja de ser válido tras la primera consulta."""
    conn = _FakeConn()
    cred = _cred(mode=qr.MODE_IMPERSONATE, username="reportes", impersonate_role="reportes")
    _run(monkeypatch, conn, statements=[_plan("SELECT 1")], engine="postgresql",
         read_only=True, credential=cred)
    assert conn.calls[0] == "SET TRANSACTION READ ONLY"
    assert conn.calls[1] == 'SET ROLE "reportes"'
    assert conn.calls[2] == "SELECT 1"


def test_postgres_sin_timeout_no_emite_set_session(monkeypatch):
    """En PostgreSQL el timeout viaja en los parámetros de conexión, no por SET."""
    conn = _FakeConn()
    _run(monkeypatch, conn, statements=[_plan("SELECT 1")], engine="postgresql",
         timeout_ms=5000)
    assert conn.calls == ["SELECT 1"]


def test_mariadb_usa_max_statement_time_en_segundos(monkeypatch):
    conn = _FakeConn()
    _run(monkeypatch, conn, statements=[_plan("SELECT 1")], engine="mariadb",
         timeout_ms=2500)
    assert "SET SESSION max_statement_time = 2.5" in conn.calls


def test_timeout_cero_no_emite_set_session(monkeypatch):
    conn = _FakeConn()
    _run(monkeypatch, conn, statements=[_plan("SELECT 1")], engine="mysql", timeout_ms=0)
    assert conn.calls == ["SELECT 1"]


# --------------------------------------------------------------------------- #
# Stop-on-error                                                                #
# --------------------------------------------------------------------------- #
def test_se_detiene_en_el_primer_error_y_marca_el_resto_no_ejecutado(monkeypatch):
    def handler(sql):
        if sql == "BOOM":
            exc = SQLAlchemyError("boom")
            exc.orig = RuntimeError("syntax error")
            raise exc
        return _FakeResult()

    conn = _FakeConn(handler)
    outcome = _run(
        monkeypatch, conn,
        statements=[_plan("SELECT 1", seq=0), _plan("BOOM", seq=1), _plan("SELECT 2", seq=2)],
    )
    s0, s1, s2 = outcome.statements
    assert s0.success and s0.executed
    assert not s1.success and s1.executed and s1.error is not None
    assert not s2.success and not s2.executed
    assert outcome.success is False
    assert outcome.rolled_back is True
    assert outcome.committed is False
    assert conn.rolled_back is True
    assert conn.committed is False


# --------------------------------------------------------------------------- #
# Commit vs rollback                                                           #
# --------------------------------------------------------------------------- #
def test_commitea_cuando_todo_sale_bien_y_no_es_solo_lectura(monkeypatch):
    conn = _FakeConn()
    outcome = _run(monkeypatch, conn, statements=[_plan("UPDATE t SET a=1", danger=qp.WRITE)],
                   read_only=False, dry_run=False)
    assert outcome.committed is True
    assert outcome.rolled_back is False
    assert conn.committed is True
    assert conn.rolled_back is False


def test_read_only_siempre_revierte_aunque_todo_salga_bien(monkeypatch):
    conn = _FakeConn()
    outcome = _run(monkeypatch, conn, statements=[_plan("SELECT 1")], read_only=True)
    assert outcome.committed is False
    assert outcome.rolled_back is True
    assert conn.committed is False


def test_dry_run_siempre_revierte_aunque_todo_salga_bien(monkeypatch):
    conn = _FakeConn()
    outcome = _run(
        monkeypatch, conn,
        statements=[_plan("UPDATE t SET a=1", danger=qp.WRITE)],
        read_only=False, dry_run=True,
    )
    assert outcome.committed is False
    assert outcome.rolled_back is True


def test_dry_run_con_ddl_en_mysql_avisa_que_el_commit_implicito_no_se_revierte(monkeypatch):
    conn = _FakeConn()
    outcome = _run(
        monkeypatch, conn,
        statements=[_plan("ALTER TABLE t ADD COLUMN c INT", danger=qp.DDL, kind="alter")],
        engine="mysql", dry_run=True,
    )
    assert any("COMMIT implícito" in w for w in outcome.warnings)


def test_dry_run_con_ddl_en_postgres_no_avisa_porque_su_ddl_es_transaccional(monkeypatch):
    conn = _FakeConn()
    outcome = _run(
        monkeypatch, conn,
        statements=[_plan("ALTER TABLE t ADD COLUMN c INT", danger=qp.DDL, kind="alter")],
        engine="postgresql", dry_run=True,
    )
    assert outcome.warnings == []


# --------------------------------------------------------------------------- #
# ``%`` literal escapado antes de llegar al driver                            #
# --------------------------------------------------------------------------- #
def test_el_porcentaje_literal_se_escapa_en_la_sentencia_ejecutada(monkeypatch):
    conn = _FakeConn()
    _run(monkeypatch, conn, statements=[_plan("SELECT * FROM t WHERE x LIKE '%a%'")])
    assert conn.calls[-1] == "SELECT * FROM t WHERE x LIKE '%%a%%'"


# --------------------------------------------------------------------------- #
# Errores de conexión: auth-like vs infraestructura                           #
# --------------------------------------------------------------------------- #
def _auth_error(sqlstate: str) -> SQLAlchemyError:
    exc = SQLAlchemyError("auth failed")
    orig = RuntimeError("denied")
    orig.sqlstate = sqlstate
    exc.orig = orig
    return exc


def test_error_de_credencial_al_conectar_se_devuelve_como_resultado(monkeypatch):
    """Un rechazo de la credencial QUE SE ESTÁ PROBANDO es un RESULTADO, no un 5xx."""
    _install_conn(monkeypatch, _FakeConn(), raise_on_enter=_auth_error("28P01"))
    outcome = qr.run_statements(
        _target("postgresql"), database="db", engine="postgresql",
        statements=[_plan("SELECT 1")],
        credential=_cred(mode=qr.MODE_PROVIDED, username="app_ro"), read_only=True,
        max_rows=10, max_cell_chars=100, timeout_ms=0,
    )
    assert outcome.success is False
    assert outcome.statements == []
    assert outcome.connection_error.sqlstate == "28P01"


def test_credencial_rechazada_en_modo_admin_es_un_error_del_gateway(monkeypatch):
    """
    El MISMO código con la credencial pseudo-root NO es el resultado de ninguna prueba:
    en modo ``admin``/``impersonate`` la conexión la hace el gateway con su propia
    credencial, así que un "access denied" ahí es el gateway mal configurado y debe
    salir como error de la API, no como un 200 con ``success=false``.
    """
    _install_conn(monkeypatch, _FakeConn(), raise_on_enter=_auth_error("28P01"))
    with pytest.raises(AppHttpException):
        qr.run_statements(
            _target("postgresql"), database="db", engine="postgresql",
            statements=[_plan("SELECT 1")], credential=_cred(mode=qr.MODE_ADMIN),
            read_only=True, max_rows=10, max_cell_chars=100, timeout_ms=0,
        )


def test_error_de_infraestructura_al_conectar_se_propaga_como_apphttpexception(monkeypatch):
    """Host inalcanzable / timeout de conexión SÍ es un problema de la API."""
    _install_conn(monkeypatch, _FakeConn(), raise_on_enter=_auth_error("08006"))
    with pytest.raises(AppHttpException) as exc:
        qr.run_statements(
            _target("postgresql"), database="db", engine="postgresql",
            statements=[_plan("SELECT 1")], credential=_cred(), read_only=True,
            max_rows=10, max_cell_chars=100, timeout_ms=0,
        )
    assert exc.value.status_code == 502


# --------------------------------------------------------------------------- #
# Truncado de filas                                                            #
# --------------------------------------------------------------------------- #
def test_una_fila_de_mas_marca_truncado(monkeypatch):
    def handler(sql):
        if sql == "SELECT * FROM t":
            return _FakeResult(columns=["id"], rows=[(i,) for i in range(5)])
        return _FakeResult()

    conn = _FakeConn(handler)
    _install_conn(monkeypatch, conn)
    outcome = qr.run_statements(
        _target(), database="db", engine="mysql",
        statements=[_plan("SELECT * FROM t")], credential=_cred(), read_only=True,
        max_rows=3, max_cell_chars=100, timeout_ms=0,
    )
    result = outcome.statements[0]
    assert result.truncated is True
    assert result.row_count == 3


def test_exactamente_el_tope_no_marca_truncado(monkeypatch):
    def handler(sql):
        if sql == "SELECT * FROM t":
            return _FakeResult(columns=["id"], rows=[(i,) for i in range(3)])
        return _FakeResult()

    conn = _FakeConn(handler)
    _install_conn(monkeypatch, conn)
    outcome = qr.run_statements(
        _target(), database="db", engine="mysql",
        statements=[_plan("SELECT * FROM t")], credential=_cred(), read_only=True,
        max_rows=3, max_cell_chars=100, timeout_ms=0,
    )
    result = outcome.statements[0]
    assert result.truncated is False
    assert result.row_count == 3


# --------------------------------------------------------------------------- #
# estimate_impact                                                              #
# --------------------------------------------------------------------------- #
def test_estimate_impact_vacio_no_abre_conexion(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("no debería conectar sin queries de impacto")

    monkeypatch.setattr(qr, "database_connection", _boom)
    assert qr.estimate_impact(
        _target(), database="db", engine="mysql", credential=_cred(),
        impact_queries=[], timeout_ms=0,
    ) == {}


def test_estimate_impact_devuelve_none_para_conteos_que_fallan_sin_romper_el_resto(monkeypatch):
    def handler(sql):
        if "FROM u" in sql:
            raise SQLAlchemyError("sin permiso")
        return _FakeResult(scalar=42)

    conn = _FakeConn(handler)
    _install_conn(monkeypatch, conn)
    results = qr.estimate_impact(
        _target(), database="db", engine="mysql", credential=_cred(),
        impact_queries=[(0, "SELECT COUNT(*) FROM t"), (1, "SELECT COUNT(*) FROM u")],
        timeout_ms=0,
    )
    assert results == {0: 42, 1: None}
    # Un SAVEPOINT por conteo: en PostgreSQL, sin esto, el COUNT que falla aborta la
    # transacción y TODOS los siguientes devuelven null — justo la cifra que se mira
    # antes de confirmar algo destructivo.
    assert conn.savepoints == 2
    # Siempre corre en solo lectura, sin importar qué vaya a ejecutar el UPDATE/DELETE.
    assert conn.calls[0] == "START TRANSACTION READ ONLY"
    assert conn.rolled_back is True
    assert conn.committed is False


def test_estimate_impact_no_rompe_si_no_puede_conectar(monkeypatch):
    """Un fallo al abrir la conexión de estimación NUNCA debe tirar el preview."""
    _install_conn(monkeypatch, _FakeConn(), raise_on_enter=_auth_error("28000"))
    results = qr.estimate_impact(
        _target(), database="db", engine="mysql", credential=_cred(),
        impact_queries=[(0, "SELECT COUNT(*) FROM t")], timeout_ms=0,
    )
    assert results == {0: None}
