"""
Tests del MigrationRunner: building blocks + ciclo Alembic real contra SQLite.

Las rutas públicas (apply/rollback/stamp) usan remote_engine (solo MySQL/MariaDB/
PostgreSQL), por lo que se prueban contra motores reales en integración (CI). Aquí
se cubren: selección de SQL por motor, compute_pending, version_table_name, la
generación de archivos de revisión y un ciclo upgrade/downgrade/stamp completo
ejecutando el mismo ``env.py`` y ``command.*`` que usa el runner, sobre SQLite.
"""

import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from app.models.enums import EngineType
from app.services.db_admin import migration_progress
from app.services.db_admin.migrations import (
    MigrationRunner,
    MigrationSpec,
    version_table_name,
)


def _spec(version, up_sql, *, mysql=None, pg=None, down=None):
    return MigrationSpec(
        id=int(version), version=version, name=f"m{version}", up_sql=up_sql,
        up_sql_mysql=mysql, up_sql_postgresql=pg, down_sql=down, checksum="x",
    )


# ``_write_revision_files`` recibe el id de la BD gestionada para consultar el checkpoint
# de sentencia (``migration_progress``). Estos tests solo verifican el CODEGEN de los
# archivos de revisión y el ciclo Alembic contra SQLite, así que se pasa un id inexistente.
_NO_MANAGED_DB = -1


@contextmanager
def _no_checkpoint():
    """
    Neutraliza el checkpoint de sentencia, que vive en la BD de METADATOS del gateway.

    Sin esto, estos tests —que no tocan ningún motor remoto y corren contra SQLite— exigen
    una conexión viva a la BD del gateway: ``_write_revision_files`` consulta el checkpoint
    (``get_progress``) y el código generado llama a ``record_statement`` en cada sentencia.
    Se stubean ambos: ``get_progress`` a None (no hay aplicación parcial previa, el codegen
    sale completo) y ``record_statement`` a un no-op.
    """
    with (
        mock.patch.object(migration_progress, "get_progress", return_value=None),
        mock.patch.object(migration_progress, "record_statement", lambda *a, **k: None),
    ):
        yield


# --------------------------------------------------------------------------- #
# Building blocks                                                              #
# --------------------------------------------------------------------------- #
def test_version_table_name_sanitizes_slug():
    assert version_table_name("whatsapp") == "_gw_v_whatsapp"
    assert version_table_name("my-model") == "_gw_v_my_model"
    assert version_table_name("UP-CASE") == "_gw_v_up_case"


def test_select_up_sql_prefers_override():
    r = MigrationRunner()
    s = _spec("0001", "CREATE TABLE a (id INT)", mysql="MYSQL_OVERRIDE", pg="PG_OVERRIDE")
    assert r.select_up_sql(s, EngineType.mysql) == "MYSQL_OVERRIDE"
    assert r.select_up_sql(s, EngineType.mariadb) == "MYSQL_OVERRIDE"
    assert r.select_up_sql(s, EngineType.postgresql) == "PG_OVERRIDE"


def test_select_up_sql_translates_when_no_override():
    r = MigrationRunner()
    s = _spec("0001", "CREATE TABLE a (id INT AUTO_INCREMENT PRIMARY KEY)")
    assert r.select_up_sql(s, EngineType.mysql) == s.up_sql  # passthrough
    pg = r.select_up_sql(s, EngineType.postgresql)
    assert "AUTO_INCREMENT" not in pg


def test_select_down_sql_none_when_absent():
    r = MigrationRunner()
    s = _spec("0001", "CREATE TABLE a (id INT)")
    assert r.select_down_sql(s, EngineType.postgresql) is None


def test_escape_percent_doubles_literal_percent():
    """
    Los 3 motores (pymysql y psycopg, ambos paramstyle pyformat/format) parsean la
    sentencia buscando placeholders ``%s``/``%(name)s`` en cuanto ``cursor.execute``
    recibe params no ``None`` — y SQLAlchemy distila un ``parameters`` ausente a ``()``
    (no ``None``) antes de llegar al DBAPI, así que SIEMPRE entra a ese parseo. Un ``%``
    literal en el DDL (columna GENERATED con módulo, LIKE '%...%', DATE_FORMAT con
    '%Y-%m-%d', etc.) revienta al ejecutarse via ``exec_driver_sql`` en CUALQUIERA de los
    3 motores — debe escaparse a ``%%`` incondicionalmente.
    """
    stmt = "ALTER TABLE t ADD COLUMN r int GENERATED ALWAYS AS (id % 10) STORED"
    assert MigrationRunner._escape_percent(stmt) == stmt.replace("%", "%%")


def test_escape_percent_breaks_postgres_placeholder_parser_if_not_escaped():
    """
    Prueba de regresión contra el parser REAL de psycopg (no un mock): confirma que un
    ``%`` sin escapar efectivamente revienta (LIKE '%...%' y el operador módulo), y que
    escaparlo con ``_escape_percent`` lo vuelve inofensivo para ``cursor.execute``.
    """
    from psycopg._queries import _query2pg

    for raw in [
        "CHECK (id % 10 = 0)",
        "CHECK (name NOT LIKE '%bad%')",
    ]:
        with pytest.raises(Exception):
            _query2pg(raw.encode(), "utf-8")
        escaped = MigrationRunner._escape_percent(raw)
        _query2pg(escaped.encode(), "utf-8")  # no debe lanzar


class _RecordingConn:
    """Conexión falsa que registra cada ``exec_driver_sql`` (sin motor real)."""

    def __init__(self, *, fail_on: str | None = None):
        self.executed: list[str] = []
        self.fail_on = fail_on

    def exec_driver_sql(self, sql):
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise SQLAlchemyError("boom")


def test_toggle_fk_checks_mysql_family():
    conn = _RecordingConn()
    MigrationRunner._toggle_fk_checks(conn, EngineType.mysql, enabled=False)
    MigrationRunner._toggle_fk_checks(conn, EngineType.mariadb, enabled=True)
    assert conn.executed == ["SET FOREIGN_KEY_CHECKS=0", "SET FOREIGN_KEY_CHECKS=1"]


def test_toggle_fk_checks_postgresql():
    conn = _RecordingConn()
    MigrationRunner._toggle_fk_checks(conn, EngineType.postgresql, enabled=False)
    MigrationRunner._toggle_fk_checks(conn, EngineType.postgresql, enabled=True)
    assert conn.executed == [
        "SET session_replication_role = 'replica'",
        "SET session_replication_role = 'origin'",
    ]


def test_toggle_fk_checks_is_best_effort():
    """Si el SET falla (motor sin soporte, o el pseudo-root sin permiso), se ignora."""
    conn = _RecordingConn(fail_on="FOREIGN_KEY_CHECKS")
    MigrationRunner._toggle_fk_checks(conn, EngineType.mysql, enabled=False)  # no debe lanzar
    assert conn.executed == ["SET FOREIGN_KEY_CHECKS=0"]


def test_compute_pending():
    r = MigrationRunner()
    specs = [_spec("0001", "x"), _spec("0002", "y"), _spec("0003", "z")]
    assert [s.version for s in r.compute_pending(None, specs)] == ["0001", "0002", "0003"]
    assert [s.version for s in r.compute_pending("0001", specs)] == ["0002", "0003"]
    assert [s.version for s in r.compute_pending("0001", specs, up_to_version="0002")] == ["0002"]
    assert r.compute_pending("0003", specs) == []


def test_compute_pending_numeric_not_lexicographic():
    """Regresión P3: cruzar de 4 a 5 dígitos no debe saltar la migración nueva."""
    r = MigrationRunner()
    specs = [_spec("9999", "a"), _spec("10000", "b")]
    # current=9999 → 10000 está PENDIENTE (lexicográficamente "10000" < "9999").
    assert [s.version for s in r.compute_pending("9999", specs)] == ["10000"]
    # Orden de aplicación numérico.
    assert [s.version for s in r.compute_pending(None, specs)] == ["9999", "10000"]
    # Ancho mixto: 0099 (99) < 00100 (100).
    mixed = [_spec("00100", "x"), _spec("0099", "y")]
    assert [s.version for s in r.compute_pending(None, mixed)] == ["0099", "00100"]


@_no_checkpoint()
def test_write_revision_files_chains_down_revision():
    r = MigrationRunner()
    specs = [_spec("0001", "CREATE TABLE a (id INT)"),
             _spec("0002", "ALTER TABLE a ADD COLUMN b INT", down="ALTER TABLE a DROP COLUMN b")]
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql, _NO_MANAGED_DB)
        rev1 = (vdir / "rev_0001.py").read_text()
        rev2 = (vdir / "rev_0002.py").read_text()
        assert "down_revision = None" in rev1
        assert "down_revision = '0001'" in rev2
        # Sin down_sql confirmado => el downgrade levanta NotImplementedError.
        assert "NotImplementedError" in rev1
        assert "op.get_bind().exec_driver_sql('ALTER TABLE a DROP COLUMN b')" in rev2


@_no_checkpoint()
def test_render_does_not_treat_colon_as_bind_param():
    """
    Regresión del bug de producción: un ``:`` LITERAL en el DDL (un JSON de ejemplo dentro
    de un COMMENT, ``{"discount_pct":15}``) hacía que ``op.execute(str)`` -> ``text()``
    interpretara ``:15`` como bind param y abortara con "A value is required for bind
    parameter '15'" ANTES de tocar el motor. El codegen debe emitir ``exec_driver_sql`` (sin
    ``text()``) y escapar ``%``->``%%`` para los drivers pyformat/format.
    """
    r = MigrationRunner()
    ddl = (
        "CREATE TABLE t (c TEXT) "
        "COMMENT 'ej {\"discount_pct\":15} — 50% off'"
    )
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, [_spec("0001", ddl)], EngineType.mysql, _NO_MANAGED_DB)
        body = (vdir / "rev_0001.py").read_text()
    assert "op.get_bind().exec_driver_sql(" in body
    assert "op.execute(" not in body
    # El ``:15`` sobrevive LITERAL (no se convierte en un placeholder ``%(15)s``).
    assert '"discount_pct":15' in body
    assert "%(15)s" not in body
    # El ``%`` literal quedó escapado a ``%%`` (exec_driver_sql lo requiere).
    assert "50%% off" in body


# --------------------------------------------------------------------------- #
# Ciclo Alembic real contra SQLite (env.py compartido + command.*)            #
# --------------------------------------------------------------------------- #
@_no_checkpoint()
def test_upgrade_applies_ddl_with_colon_against_sqlite():
    """
    End-to-end del fix por el camino Alembic real (``command.upgrade``): un ``:`` dentro de
    un literal de string del DDL rompía con el viejo ``op.execute`` (bind param). SQLite no
    soporta COMMENT de columna estilo MySQL, así que el ``:15`` va en un DEFAULT — mismo
    disparador (``text()`` lo tomaría como bind).
    """
    r = MigrationRunner()
    specs = [_spec("0001", "CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT DEFAULT 'ratio 1:15')",
                   down="DROP TABLE t")]
    dbfile = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{dbfile}")
    vt = version_table_name("colon")
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql, _NO_MANAGED_DB)
        with engine.connect() as conn:
            cfg = r._make_config(vdir, conn, vt)
            command.upgrade(cfg, "0001")  # antes del fix: InvalidRequestError bind param '15'
            assert r._read_current(conn, vt) == "0001"
            assert "t" in inspect(conn).get_table_names()


@_no_checkpoint()
def test_full_upgrade_downgrade_stamp_cycle_sqlite():
    r = MigrationRunner()
    specs = [
        _spec("0001", "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(50))",
              down="DROP TABLE users"),
        _spec("0002", "ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
              down="ALTER TABLE users DROP COLUMN phone"),
    ]
    dbfile = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{dbfile}")
    vt = version_table_name("whatsapp")

    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql, _NO_MANAGED_DB)

        with engine.connect() as conn:
            cfg = r._make_config(vdir, conn, vt)
            assert r._read_current(conn, vt) is None

            command.upgrade(cfg, "0001")
            assert r._read_current(conn, vt) == "0001"
            assert "users" in inspect(conn).get_table_names()

            command.upgrade(cfg, "0002")
            assert r._read_current(conn, vt) == "0002"
            assert "phone" in [c["name"] for c in inspect(conn).get_columns("users")]

            command.downgrade(cfg, "-1")
            assert r._read_current(conn, vt) == "0001"
            assert "phone" not in [c["name"] for c in inspect(conn).get_columns("users")]

    # stamp: marca versión sin ejecutar SQL.
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql, _NO_MANAGED_DB)
        with engine.connect() as conn:
            cfg = r._make_config(vdir, conn, vt)
            command.stamp(cfg, "0002")
            assert r._read_current(conn, vt) == "0002"


@_no_checkpoint()
def test_sequential_downgrade_to_target_version_sqlite():
    """
    Mecánica del rollback secuencial: estando en 0004, bajar a 0001 en pasos
    ordenados (igual que rollback_to) deja la versión en 0001 y revierte el esquema.
    Prueba directamente que la versión SÍ se actualiza tras varios downgrades.
    """
    r = MigrationRunner()
    specs = [
        _spec("0001", "CREATE TABLE a (id INTEGER PRIMARY KEY)", down="DROP TABLE a"),
        _spec("0002", "CREATE TABLE b (id INTEGER PRIMARY KEY)", down="DROP TABLE b"),
        _spec("0003", "CREATE TABLE c (id INTEGER PRIMARY KEY)", down="DROP TABLE c"),
        _spec("0004", "CREATE TABLE d (id INTEGER PRIMARY KEY)", down="DROP TABLE d"),
    ]
    dbfile = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{dbfile}")
    vt = version_table_name("seqdown")

    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql, _NO_MANAGED_DB)
        with engine.connect() as conn:
            cfg = r._make_config(vdir, conn, vt)
            command.upgrade(cfg, "0004")
            assert r._read_current(conn, vt) == "0004"
            assert {"a", "b", "c", "d"}.issubset(set(inspect(conn).get_table_names()))

            # Bajar SECUENCIALMENTE hasta 0001 (revierte 0004, 0003, 0002).
            reverted = []
            current = r._read_current(conn, vt)
            from app.services.db_admin.migration_integrity import version_sort_key
            while current is not None and version_sort_key(current) > version_sort_key("0001"):
                command.downgrade(cfg, "-1")
                reverted.append(current)
                current = r._read_current(conn, vt)

            assert reverted == ["0004", "0003", "0002"]
            assert r._read_current(conn, vt) == "0001"          # versión actualizada
            tables = set(inspect(conn).get_table_names())
            assert "a" in tables and not {"b", "c", "d"} & tables  # esquema revertido


# =========================================================================== #
# Timeout de sentencia: va por OPERACIÓN, no por versión                      #
# =========================================================================== #
def _capture_connection_flags(monkeypatch) -> list[dict]:
    """
    Intercepta ``database_connection`` del runner y registra con qué flags se abre cada
    conexión. Devuelve la lista de llamadas; la conexión cedida es un doble inerte, así que
    estos tests miran SOLO los flags, no lo que se ejecuta.

    Además neutraliza el checkpoint por sentencia. NO es adorno: ``_write_revision_files``
    consulta ``migration_progress.get_progress`` para resolver desde dónde reanudar, y eso va a
    la BD del GATEWAY. Sin este doble, ``apply``/``rollback_to``/``stamp`` mueren en
    ``no such table: migration_statement_progress`` **antes** de abrir la conexión remota, y el
    test pasa o falla según si alguien creó el esquema — que es una dependencia que este archivo
    no declara y que los otros 17 tests no tienen (todos son autocontenidos, con su propio
    SQLite). El sujeto de estos tres tests es el FLAG de la conexión, no el checkpoint.
    """
    from app.services.db_admin import migration_progress as prog
    from app.services.db_admin import migrations as mig

    calls: list[dict] = []

    @contextmanager
    def fake_conn(target, db_name, *, bulk=False, **kw):
        calls.append({"db_name": db_name, "bulk": bulk})
        raise SQLAlchemyError("corte deliberado: solo interesa el flag de la conexión")
        yield  # pragma: no cover — inalcanzable, deja la función como generador

    monkeypatch.setattr(mig, "database_connection", fake_conn)
    monkeypatch.setattr(prog, "get_progress", lambda *a, **k: None)
    monkeypatch.setattr(mig.migration_progress, "get_progress", lambda *a, **k: None)
    return calls


def _run_and_swallow(fn):
    """Ejecuta y se traga el AppHttpException del corte deliberado del doble."""
    from app.exceptions import AppHttpException

    try:
        fn()
    except (AppHttpException, SQLAlchemyError):
        pass


def test_apply_and_rollback_use_the_bulk_timeout(monkeypatch):
    """
    `apply` y `rollback_to` ejecutan DDL del usuario, así que van con el timeout de volcado.

    El interactivo de 15 s NO era una protección que acá se pierda: en MySQL/MariaDB son
    `read_timeout`/`write_timeout` de socket DEL CLIENTE y no cancelan nada en el motor.
    Rompían la conexión mientras el servidor seguía ejecutando, el checkpoint no registraba la
    sentencia, el motor la completaba igual y el próximo `apply` la reejecutaba: un bucle que
    no termina, con la contabilidad desincronizada del plano físico.
    """
    from app.core.remote_engine import ServerTarget

    calls = _capture_connection_flags(monkeypatch)
    runner = MigrationRunner()
    target = ServerTarget(
        server_id=1, dialect="mysql", host="10.0.0.5", port=3306,
        admin_user="root", admin_password="pw",
    )
    specs = [_spec("0001", "ALTER TABLE t ADD COLUMN c INT")]

    _run_and_swallow(lambda: runner.apply(
        target, db_name="db", slug="s", engine=EngineType.mysql,
        managed_db_id=_NO_MANAGED_DB, specs=specs,
    ))
    _run_and_swallow(lambda: runner.rollback_to(
        target, db_name="db", slug="s", engine=EngineType.mysql,
        managed_db_id=_NO_MANAGED_DB, specs=specs, to_version=None,
    ))

    # Se fija el CONTEO, no solo "alguna": con `assert calls` a secas, si `apply` dejara de
    # abrir conexión (un return temprano nuevo) el test seguiría en verde por la de
    # `rollback_to`, y justamente `apply` es el camino que el fix venía a arreglar.
    assert len(calls) == 2, calls
    assert all(c["bulk"] is True for c in calls), calls


def test_stamp_keeps_the_interactive_timeout(monkeypatch):
    """
    `stamp` NO ejecuta SQL del usuario: solo escribe la tabla de versión. Un timeout largo
    ahí sería un defecto, no una mejora — una escritura de metadatos que tarda una hora tiene
    que fallar rápido.

    Es lo que fija que el eje sea la OPERACIÓN y no el runner entero.
    """
    from app.core.remote_engine import ServerTarget

    calls = _capture_connection_flags(monkeypatch)
    runner = MigrationRunner()
    target = ServerTarget(
        server_id=1, dialect="mysql", host="10.0.0.5", port=3306,
        admin_user="root", admin_password="pw",
    )
    specs = [_spec("0001", "ALTER TABLE t ADD COLUMN c INT")]

    _run_and_swallow(lambda: runner.stamp(
        target, db_name="db", slug="s", engine=EngineType.mysql,
        managed_db_id=_NO_MANAGED_DB, specs=specs, version="0001",
    ))

    assert len(calls) == 1, calls
    assert all(c["bulk"] is False for c in calls), calls


def test_reading_the_current_version_keeps_the_interactive_timeout(monkeypatch):
    """Leer la versión es un SELECT sobre la tabla de versión: no necesita el timeout largo."""
    from app.core.remote_engine import ServerTarget

    calls = _capture_connection_flags(monkeypatch)
    runner = MigrationRunner()
    target = ServerTarget(
        server_id=1, dialect="mysql", host="10.0.0.5", port=3306,
        admin_user="root", admin_password="pw",
    )

    _run_and_swallow(lambda: runner.get_current_version(target, "db", "s"))

    assert len(calls) == 1, calls
    assert all(c["bulk"] is False for c in calls), calls
