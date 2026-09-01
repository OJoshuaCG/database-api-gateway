"""
Tests del módulo de copia de datos (``data_copy``).

Dado que la copia real necesita DOS motores vivos, aquí probamos SIN motor real todo lo
que se puede aislar: generación de SQL INSERT/upsert por dialecto, orden por PK, forzado
de INSERT simple sin PK, adaptación de valores cross-engine y los dataclasses. El
round-trip de la lógica de lotes/cancelación/aislamiento se ejerce con DOS BDs SQLite
(monkeypatch de la factoría de conexiones). El comportamiento específico de MySQL/MariaDB/
PostgreSQL (FK off real, tipos exóticos) queda para el script e2e.

Convención de estilo: funciones pytest planas, sin clases (igual que test_schema_diff.py).
"""

import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.core.remote_engine import ServerTarget
from app.services.db_admin import data_copy as dc
from app.services.db_admin.data_copy import (
    TableCopyResult,
    TableCopySpec,
    _adapt_value,
    _build_insert,
    _build_insert_from_staging,
    _build_select,
    _escape_mysql_field,
    _render_mysql_field,
    _FilasPerdidas,
    _staging_name,
    _verificar_filas_cargadas,
    copy_tables,
    render_time_for_reinsert,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _spec(table="widget", columns=None, pk=None, upsert=False):
    return TableCopySpec(
        table=table,
        columns=columns if columns is not None else ["id", "name"],
        primary_key=pk if pk is not None else ["id"],
        upsert=upsert,
    )


_TARGET = ServerTarget(
    server_id=1,
    dialect="postgresql",
    host="db.internal",
    port=5432,
    admin_user="root",
    admin_password="secret",
)


# --------------------------------------------------------------------------- #
# Dataclasses                                                                  #
# --------------------------------------------------------------------------- #
# ── Staging: la condición es "PK o UNIQUE", no solo PK ───────────────────────────
# Una tabla SIN PK pero CON un índice UNIQUE secundario sí puede conflictuar. Cargándola
# directo a la tabla final, el IGNORE implícito de LOAD DATA LOCAL descartaba la fila en
# silencio; el INSERT legacy, en cambio, falla con 1062. Era una divergencia entre los dos
# writers del mismo módulo, no solo una optimización de más.


def test_spec_has_unique_key_defaults_to_false():
    spec = TableCopySpec(table="t", columns=["a"], primary_key=[])
    assert spec.has_unique_key is False


def test_staging_condition_covers_unique_without_pk():
    # Réplica de la condición de `_copy_writer_mysql`: sin PK pero con UNIQUE => staging.
    sin_claves = TableCopySpec(table="t", columns=["a"], primary_key=[])
    solo_pk = TableCopySpec(table="t", columns=["a"], primary_key=["a"])
    solo_unique = TableCopySpec(
        table="t", columns=["a"], primary_key=[], has_unique_key=True
    )
    assert (bool(sin_claves.primary_key) or sin_claves.has_unique_key) is False
    assert (bool(solo_pk.primary_key) or solo_pk.has_unique_key) is True
    assert (bool(solo_unique.primary_key) or solo_unique.has_unique_key) is True


def test_insert_from_staging_without_pk_is_plain_and_therefore_fail_closed():
    # Sin PK, `_compose_insert` degrada a INSERT plano incluso con upsert=True. Eso es lo
    # que hace que el conflicto contra filas preexistentes ABORTE (1062) en vez de saltearse:
    # el statement final es la red de seguridad que la carga directa no tenía.
    spec = TableCopySpec(
        table="t", columns=["a", "b"], primary_key=[], upsert=True, has_unique_key=True
    )
    sql = _build_insert_from_staging("mysql", spec, "stg_x")
    assert sql.startswith("INSERT INTO `t` (`a`, `b`) SELECT")
    assert "ON DUPLICATE KEY UPDATE" not in sql
    assert "IGNORE" not in sql


def test_spec_defaults():
    s = TableCopySpec(table="t", columns=["a"], primary_key=[])
    assert s.upsert is False
    assert s.primary_key == []


def test_result_defaults():
    r = TableCopyResult(table="t", status="applied")
    assert r.rows_copied == 0
    assert r.error is None
    assert r.duration_ms == 0


# --------------------------------------------------------------------------- #
# _adapt_value (cross-engine)                                                  #
# --------------------------------------------------------------------------- #
def test_adapt_value_dict_and_list_to_json():
    assert _adapt_value({"a": 1}) == '{"a": 1}'
    assert _adapt_value([1, 2]) == "[1, 2]"


def test_adapt_value_bytearray_and_memoryview_to_bytes():
    assert _adapt_value(bytearray(b"xy")) == b"xy"
    assert _adapt_value(memoryview(b"xy")) == b"xy"


def test_adapt_value_passthrough_scalars():
    now = datetime(2026, 1, 2, 3, 4, 5)
    for v in (None, True, False, 42, 3.5, Decimal("1.50"), "hi", b"raw", now):
        assert _adapt_value(v) is v


# --------------------------------------------------------------------------- #
# _build_select                                                                #
# --------------------------------------------------------------------------- #
def test_select_orders_by_pk_mysql():
    sql = _build_select("mysql", _spec(pk=["id"]))
    assert sql == "SELECT `id`, `name` FROM `widget` ORDER BY `id`"


def test_select_composite_pk_order():
    sql = _build_select("postgresql", _spec(columns=["a", "b", "c"], pk=["a", "b"]))
    assert sql == 'SELECT "a", "b", "c" FROM "widget" ORDER BY "a", "b"'


def test_select_no_pk_has_no_order_by():
    sql = _build_select("mysql", _spec(pk=[]))
    assert "ORDER BY" not in sql


# --------------------------------------------------------------------------- #
# _build_insert (por dialecto)                                                 #
# --------------------------------------------------------------------------- #
def test_insert_plain_mysql():
    sql = _build_insert("mysql", _spec(upsert=False))
    assert sql == "INSERT INTO `widget` (`id`, `name`) VALUES (:p0, :p1)"


def test_insert_plain_postgres():
    sql = _build_insert("postgresql", _spec(upsert=False))
    assert sql == 'INSERT INTO "widget" ("id", "name") VALUES (:p0, :p1)'


def test_upsert_mysql_on_duplicate_key():
    sql = _build_insert("mysql", _spec(upsert=True))
    assert sql.endswith("ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)")


def test_upsert_mariadb_uses_backticks():
    sql = _build_insert("mariadb", _spec(upsert=True))
    assert "`name` = VALUES(`name`)" in sql
    assert sql.startswith("INSERT INTO `widget`")


def test_upsert_postgres_on_conflict_do_update():
    sql = _build_insert("postgresql", _spec(upsert=True))
    assert sql.endswith('ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name"')


def test_upsert_postgres_pk_only_do_nothing():
    sql = _build_insert("postgresql", _spec(columns=["id"], pk=["id"], upsert=True))
    assert sql.endswith('ON CONFLICT ("id") DO NOTHING')


def test_upsert_mysql_pk_only_insert_ignore():
    sql = _build_insert("mysql", _spec(columns=["id"], pk=["id"], upsert=True))
    assert sql.startswith("INSERT IGNORE INTO `widget`")


def test_upsert_without_pk_forces_plain_insert():
    # Sin PK, upsert=True => INSERT simple (no ON CONFLICT / ON DUPLICATE).
    for engine in ("mysql", "mariadb", "postgresql"):
        sql = _build_insert(engine, _spec(pk=[], upsert=True))
        assert "ON CONFLICT" not in sql
        assert "ON DUPLICATE KEY" not in sql
        assert "IGNORE" not in sql
        assert sql.startswith("INSERT INTO")


def test_composite_pk_upsert_postgres():
    sql = _build_insert(
        "postgresql", _spec(columns=["a", "b", "v"], pk=["a", "b"], upsert=True)
    )
    assert 'ON CONFLICT ("a", "b") DO UPDATE SET "v" = EXCLUDED."v"' in sql


# --------------------------------------------------------------------------- #
# Round-trip con dos BDs SQLite (lógica de lotes / cancelación / aislamiento)  #
# --------------------------------------------------------------------------- #
def _setup_sqlite_env(monkeypatch, tmp_path, source_rows, *, create_dest_rows=None):
    """Crea src/dst SQLite y monkeypatchea la factoría de conexiones de data_copy.

    Usamos engine_type='postgresql' en las llamadas: comillas dobles + ``ON CONFLICT``
    son válidos en SQLite, así que el round-trip ejercita el mismo código que PostgreSQL.
    """
    src_engine = create_engine(f"sqlite:///{tmp_path}/src.db")
    dst_engine = create_engine(f"sqlite:///{tmp_path}/dst.db")
    ddl = 'CREATE TABLE "widget" ("id" INTEGER PRIMARY KEY, "name" TEXT)'
    with src_engine.begin() as c:
        c.exec_driver_sql(ddl)
        for row in source_rows:
            c.exec_driver_sql('INSERT INTO "widget" ("id", "name") VALUES (?, ?)', row)
    with dst_engine.begin() as c:
        c.exec_driver_sql(ddl)
        for row in create_dest_rows or []:
            c.exec_driver_sql('INSERT INTO "widget" ("id", "name") VALUES (?, ?)', row)

    engines = {"srcdb": src_engine, "dstdb": dst_engine}

    @contextmanager
    def fake_conn(target, database, *, bulk=False, mysql_local_infile=False):
        conn = engines[database].connect()
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(dc, "database_connection", fake_conn)
    return engines


def _dest_rows(engines):
    with engines["dstdb"].connect() as c:
        return c.exec_driver_sql('SELECT "id", "name" FROM "widget" ORDER BY "id"').fetchall()


def _copy(**kw):
    defaults = dict(
        source_target=_TARGET,
        source_db="srcdb",
        source_engine="postgresql",
        dest_target=_TARGET,
        dest_db="dstdb",
        dest_engine="postgresql",
        batch_rows=2,
    )
    defaults.update(kw)
    return copy_tables(**defaults)


def test_roundtrip_copies_all_rows_batched(monkeypatch, tmp_path):
    rows = [(i, f"n{i}") for i in range(1, 6)]  # 5 filas, batch=2 => 3 lotes
    engines = _setup_sqlite_env(monkeypatch, tmp_path, rows)

    progress = []
    results = _copy(
        specs=[_spec()],
        progress_cb=lambda t, n: progress.append((t, n)),
    )

    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].rows_copied == 5
    assert _dest_rows(engines) == rows
    # progreso reportado por lote (crece monótono, último == total).
    assert progress[-1] == ("widget", 5)
    assert [n for _, n in progress] == [2, 4, 5]


def test_roundtrip_upsert_updates_existing(monkeypatch, tmp_path):
    engines = _setup_sqlite_env(
        monkeypatch,
        tmp_path,
        source_rows=[(1, "new")],
        create_dest_rows=[(1, "old")],
    )
    results = _copy(specs=[_spec(upsert=True)])
    assert results[0].status == "applied"
    assert _dest_rows(engines) == [(1, "new")]


def test_plain_insert_conflict_marks_table_failed(monkeypatch, tmp_path):
    # upsert=False + PK duplicada => el INSERT choca => tabla failed (aislada).
    engines = _setup_sqlite_env(
        monkeypatch,
        tmp_path,
        source_rows=[(1, "x")],
        create_dest_rows=[(1, "orig")],
    )
    results = _copy(specs=[_spec(upsert=False)])
    assert results[0].status == "failed"
    assert results[0].error
    assert _dest_rows(engines) == [(1, "orig")]  # sin cambios


def test_failing_table_isolated_next_continues(monkeypatch, tmp_path):
    engines = _setup_sqlite_env(monkeypatch, tmp_path, source_rows=[(1, "a")])
    missing = _spec(table="does_not_exist")
    good = _spec()
    results = _copy(specs=[missing, good], batch_rows=10)

    assert results[0].table == "does_not_exist"
    assert results[0].status == "failed"
    assert results[1].table == "widget"
    assert results[1].status == "applied"
    assert _dest_rows(engines) == [(1, "a")]


def test_cancel_before_start_marks_all_canceled(monkeypatch, tmp_path):
    engines = _setup_sqlite_env(monkeypatch, tmp_path, source_rows=[(1, "a")])
    results = _copy(specs=[_spec(), _spec(table="widget")], cancel_cb=lambda: True)
    assert [r.status for r in results] == ["canceled", "canceled"]
    assert _dest_rows(engines) == []  # nada escrito


def test_cancel_between_batches_stops_and_marks_remaining(monkeypatch, tmp_path):
    rows = [(i, f"n{i}") for i in range(1, 6)]
    engines = _setup_sqlite_env(monkeypatch, tmp_path, rows)

    # Cancela tras el primer chequeo (que ocurre al llenar el primer lote de 2).
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] >= 2  # deja pasar el chequeo del loop, corta en el 1er lote

    second = _spec(table="widget")
    results = _copy(specs=[_spec(), second], batch_rows=2, cancel_cb=cancel)

    assert results[0].status == "canceled"
    assert results[1].status == "canceled"  # tabla restante marcada
    # Se canceló antes de escribir el primer lote => destino vacío.
    assert _dest_rows(engines) == []


def test_no_pk_copies_without_order(monkeypatch, tmp_path):
    engines = _setup_sqlite_env(monkeypatch, tmp_path, source_rows=[(1, "a"), (2, "b")])
    results = _copy(specs=[_spec(pk=[])], batch_rows=10)
    assert results[0].status == "applied"
    assert results[0].rows_copied == 2
    assert sorted(_dest_rows(engines)) == [(1, "a"), (2, "b")]


# --------------------------------------------------------------------------- #
# _resolve_writer (dispatch por dialecto real + capability-probe local_infile)#
# --------------------------------------------------------------------------- #
class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeShowVariablesResult:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        if self._value is None:
            return None
        return ("local_infile", self._value)


class _FakeResolveConn:
    """``dest_conn`` mínimo para ``_resolve_writer``: solo dialecto + probe de MySQL."""

    def __init__(self, dialect_name, local_infile="ON"):
        self.dialect = _FakeDialect(dialect_name)
        self.local_infile = local_infile
        self.exec_calls: list[str] = []

    def exec_driver_sql(self, sql):
        self.exec_calls.append(sql)
        return _FakeShowVariablesResult(self.local_infile)


def test_resolve_writer_postgres_dialect_uses_copy_writer():
    conn = _FakeResolveConn("postgresql")
    assert dc._resolve_writer(conn) is dc._copy_writer_postgres


def test_resolve_writer_sqlite_dialect_falls_back_to_insert():
    conn = _FakeResolveConn("sqlite")
    assert dc._resolve_writer(conn) is dc._copy_writer_insert


def test_resolve_writer_unmapped_dialect_falls_back_to_insert():
    conn = _FakeResolveConn("oracle")
    assert dc._resolve_writer(conn) is dc._copy_writer_insert


def test_resolve_writer_mysql_local_infile_on_uses_load_data():
    conn = _FakeResolveConn("mysql", local_infile="ON")
    assert dc._resolve_writer(conn) is dc._copy_writer_mysql
    assert any("local_infile" in c for c in conn.exec_calls)


def test_resolve_writer_mysql_local_infile_off_falls_back_to_insert():
    conn = _FakeResolveConn("mysql", local_infile="OFF")
    assert dc._resolve_writer(conn) is dc._copy_writer_insert


def test_resolve_writer_mysql_local_infile_fetchone_none_falls_back_to_insert():
    # SHOW VARIABLES sin filas (motor/permiso raro): probe fail-closed -> legacy.
    conn = _FakeResolveConn("mysql", local_infile=None)
    assert dc._resolve_writer(conn) is dc._copy_writer_insert


def test_resolve_writer_mysql_probe_error_falls_back_to_insert():
    class _ErrorConn:
        dialect = _FakeDialect("mysql")

        def exec_driver_sql(self, sql):
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("boom")

    assert dc._resolve_writer(_ErrorConn()) is dc._copy_writer_insert


# --------------------------------------------------------------------------- #
# Kill switch CLONE_BULK_COPY_ENABLED=False                                   #
# --------------------------------------------------------------------------- #
def test_kill_switch_disabled_skips_probe_and_forces_insert(monkeypatch):
    monkeypatch.setattr(dc, "CLONE_BULK_COPY_ENABLED", False)

    class _ExplodingConn:
        dialect = _FakeDialect("mysql")

        def exec_driver_sql(self, sql):
            raise AssertionError("no debe probar local_infile con el kill switch off")

    assert dc._resolve_writer(_ExplodingConn()) is dc._copy_writer_insert


def test_kill_switch_disabled_forces_insert_for_postgres_too(monkeypatch):
    monkeypatch.setattr(dc, "CLONE_BULK_COPY_ENABLED", False)
    conn = _FakeResolveConn("postgresql")
    assert dc._resolve_writer(conn) is dc._copy_writer_insert


def test_kill_switch_enabled_by_default_uses_bulk_writer_for_postgres():
    # Sanity: sin monkeypatch, el kill switch está en su default (True) y no interfiere.
    assert dc.CLONE_BULK_COPY_ENABLED is True
    conn = _FakeResolveConn("postgresql")
    assert dc._resolve_writer(conn) is dc._copy_writer_postgres


# --------------------------------------------------------------------------- #
# _build_insert_from_staging (INSERT ... SELECT FROM staging, por dialecto)   #
# --------------------------------------------------------------------------- #
def test_staging_insert_plain_mysql():
    sql = _build_insert_from_staging("mysql", _spec(upsert=False), "_gw_stg_abc123")
    assert sql == "INSERT INTO `widget` (`id`, `name`) SELECT `id`, `name` FROM `_gw_stg_abc123`"


def test_staging_insert_plain_postgres():
    sql = _build_insert_from_staging("postgresql", _spec(upsert=False), "_gw_stg_abc123")
    assert sql == (
        'INSERT INTO "widget" ("id", "name") SELECT "id", "name" FROM "_gw_stg_abc123"'
    )


def test_staging_insert_upsert_mysql_on_duplicate_key():
    sql = _build_insert_from_staging("mysql", _spec(upsert=True), "_gw_stg_abc123")
    assert sql.startswith(
        "INSERT INTO `widget` (`id`, `name`) SELECT `id`, `name` FROM `_gw_stg_abc123`"
    )
    assert sql.endswith("ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)")


def test_staging_insert_upsert_mariadb_uses_backticks():
    sql = _build_insert_from_staging("mariadb", _spec(upsert=True), "_gw_stg_abc123")
    assert "`name` = VALUES(`name`)" in sql
    assert sql.startswith("INSERT INTO `widget`")


def test_staging_insert_upsert_postgres_on_conflict_do_update():
    sql = _build_insert_from_staging("postgresql", _spec(upsert=True), "_gw_stg_abc123")
    assert sql.endswith('ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name"')


def test_staging_insert_upsert_postgres_pk_only_do_nothing():
    sql = _build_insert_from_staging(
        "postgresql", _spec(columns=["id"], pk=["id"], upsert=True), "_gw_stg_abc123"
    )
    assert sql.endswith('ON CONFLICT ("id") DO NOTHING')


def test_staging_insert_upsert_mysql_pk_only_insert_ignore():
    sql = _build_insert_from_staging(
        "mysql", _spec(columns=["id"], pk=["id"], upsert=True), "_gw_stg_abc123"
    )
    assert sql.startswith("INSERT IGNORE INTO `widget`")
    assert "SELECT `id` FROM `_gw_stg_abc123`" in sql


def test_staging_insert_without_pk_forces_plain_select():
    for engine in ("mysql", "mariadb", "postgresql"):
        sql = _build_insert_from_staging(engine, _spec(pk=[], upsert=True), "_gw_stg_abc123")
        assert "ON CONFLICT" not in sql
        assert "ON DUPLICATE KEY" not in sql
        assert "IGNORE" not in sql
        assert sql.startswith("INSERT INTO")
        assert "SELECT" in sql


def test_staging_insert_composite_pk_upsert_postgres():
    sql = _build_insert_from_staging(
        "postgresql", _spec(columns=["a", "b", "v"], pk=["a", "b"], upsert=True), "_gw_stg_xyz"
    )
    assert 'ON CONFLICT ("a", "b") DO UPDATE SET "v" = EXCLUDED."v"' in sql
    assert 'SELECT "a", "b", "v" FROM "_gw_stg_xyz"' in sql


# --------------------------------------------------------------------------- #
# _render_mysql_field / _escape_mysql_field (serialización LOAD DATA)         #
# --------------------------------------------------------------------------- #
def test_render_mysql_field_none_is_literal_null():
    assert _render_mysql_field(None) == b"\\N"


def test_render_mysql_field_bool_as_0_or_1():
    assert _render_mysql_field(True) == b"1"
    assert _render_mysql_field(False) == b"0"


def test_render_mysql_field_scalars_use_str_repr():
    assert _render_mysql_field(42) == b"42"
    assert _render_mysql_field(3.5) == b"3.5"


def test_render_mysql_field_str_escapes_tab_newline_cr():
    assert _render_mysql_field("a\tb\nc\rd") == b"a\\tb\\nc\\rd"


def test_render_mysql_field_backslash_escaped_before_tab_no_double_escape():
    # Un backslash Y un tab REALES en el mismo valor: cada uno se escapa una sola vez
    # (si el orden fuera al revés, el tab ya escapado a "\t" literal se re-escaparía
    # el backslash recién insertado, corrompiendo el campo).
    assert _render_mysql_field("a\\b\tc") == b"a\\\\b\\tc"


def test_render_mysql_field_literal_backslash_t_sequence_not_read_as_tab():
    # La secuencia de 2 caracteres backslash+"t" (NO un tab real, value as-is) debe
    # quedar como backslash-escapado + "t" literal, nunca colapsar a un solo "\t".
    assert _render_mysql_field("\\t") == b"\\\\t"


def test_render_mysql_field_bytes_raw_passthrough_when_no_special_chars():
    assert _render_mysql_field(b"\x00\x01\x02") == b"\x00\x01\x02"


def test_render_mysql_field_bytearray_and_memoryview_escaped():
    assert _render_mysql_field(bytearray(b"a\tb")) == b"a\\tb"
    assert _render_mysql_field(memoryview(b"a\nb")) == b"a\\nb"


# ── TIME (timedelta): NINGÚN driver lo serializa bien, y son dos defectos distintos ──
# El TIME de MySQL/MariaDB llega al driver como ``timedelta`` y admite negativos y valores
# mayores a 24 h. Con ``str()`` sale "1 day, 2:00:00" / "-1 day, 23:00:00", que NO son
# literales TIME válidos — y como la fase de datos relaja STRICT_TRANS_TABLES a propósito, el
# motor los coercionaba EN SILENCIO y la tabla se reportaba `applied`.
#
# El primer arreglo pasó a ``format_timedelta``, y quedó CORTO: esa función hace
# ``int(total_seconds())``, o sea que tiraba los microsegundos. Cualquier columna TIME(3) o
# TIME(6) se copiaba truncada, y ``-00:00:00.500000`` se copiaba como ``00:00:00``, perdiendo
# el valor Y el signo. Por eso existe ``render_time_for_reinsert``, que es de REINSERCIÓN y no
# de presentación como ``format_timedelta`` (que sigue truncando a propósito para la consola
# SQL y los otros dos consumidores de pantalla).
#
# Y el camino legacy tampoco estaba sano: ``pymysql.escape_timedelta`` aplica el signo solo a
# las HORAS, así que reinserta ``-01:30:00`` como ``-2:30:00``. Por eso ``_adapt_value``
# normaliza el ``timedelta`` a str ANTES de bifurcar hacia cualquiera de los dos writers: es la
# única forma de que despachar una tabla a uno o al otro no cambie lo que queda escrito.


def test_render_mysql_field_time_under_24h():
    assert _render_mysql_field(timedelta(hours=2)) == b"02:00:00"
    assert _render_mysql_field(timedelta(hours=1, minutes=2, seconds=3)) == b"01:02:03"


def test_render_mysql_field_time_over_24h_does_not_say_day():
    # str(timedelta(days=1, hours=2)) == "1 day, 2:00:00" — inválido como literal TIME.
    assert _render_mysql_field(timedelta(days=1, hours=2)) == b"26:00:00"


def test_render_mysql_field_time_negative_keeps_sign():
    # str(timedelta(hours=-1)) == "-1 day, 23:00:00": el signo se perdía y el valor cambiaba.
    assert _render_mysql_field(timedelta(hours=-1)) == b"-01:00:00"


def test_render_mysql_field_time_does_not_normalize_to_24h():
    # 838:00:00 es el máximo LEGAL del tipo TIME: no se puede normalizar a días.
    assert _render_mysql_field(timedelta(hours=838)) == b"838:00:00"


def test_render_mysql_field_time_zero():
    assert _render_mysql_field(timedelta(0)) == b"00:00:00"


def test_render_mysql_field_time_never_needs_escaping():
    # El formato es [-]HH:MM:SS — solo dígitos, dos puntos y un signo. Ninguno de los cuatro
    # caracteres que LOAD DATA escapa puede aparecer, así que el valor sale tal cual.
    for value in (timedelta(hours=5), timedelta(hours=-5), timedelta(hours=900)):
        rendered = _render_mysql_field(value)
        assert b"\\" not in rendered
        assert rendered == rendered.replace(b"\\", b"")


def test_escape_mysql_field_order_backslash_before_tab():
    raw = b"\\" + b"\t"  # un backslash real seguido de un tab real
    assert _escape_mysql_field(raw) == b"\\" + b"\\" + b"\\" + b"t"


# --------------------------------------------------------------------------- #
# _staging_name                                                               #
# --------------------------------------------------------------------------- #
def test_staging_name_passes_validate_identifier_mysql_and_postgres():
    from app.services.db_admin.identifiers import validate_identifier

    name = _staging_name()
    validate_identifier(name, "mysql")
    validate_identifier(name, "postgresql")


def test_staging_name_generates_distinct_names():
    assert _staging_name() != _staging_name()


# --------------------------------------------------------------------------- #
# LOAD DATA vía FIFO: coordinación real de hilos, SIN servidor MySQL           #
# --------------------------------------------------------------------------- #
# Nota de alcance: estos fakes simulan el LADO LECTOR del FIFO (lo que en producción
# hace pymysql dentro de ``cur.execute(LOAD DATA ...)``) abriéndolo ellos mismos, para
# ejercer la coordinación de hilos real de ``_load_data_via_fifo``/``_copy_writer_mysql``
# (open bloqueante en escritura hasta que este lado abre en lectura). Se mantienen
# SIMPLIFICADOS a propósito: los valores usados en estos tests no llevan
# backslash/tab/newline, así que decodificar utf-8 directo alcanza para reconstruir las
# filas (el unescape byte a byte completo ya está cubierto arriba por los tests puros de
# ``_render_mysql_field``/``_escape_mysql_field``). NO se ejercita contra un motor MySQL
# real (fuera del alcance de este entorno sin Docker) -- ver script e2e de clonado para
# esa verificación.
def test_load_data_via_fifo_transmits_rows_in_order_across_threads():
    received: list[list[bytes]] = []

    class _FifoReaderConn:
        def exec_driver_sql(self, sql):
            m = re.search(r"LOAD DATA LOCAL INFILE '([^']+)'", sql)
            assert m, f"SQL inesperado: {sql}"
            path = m.group(1)
            with open(path, "rb") as fh:
                for line in fh:
                    received.append(line.rstrip(b"\n").split(b"\t"))
            return None

    rows = [(1, "a"), (2, "b"), (3, "c")]
    dc._load_data_via_fifo(
        dest_conn=_FifoReaderConn(),
        load_target="`widget`",
        cols_q="`id`, `name`",
        table="widget",
        rows_iter=iter(rows),
        batch_rows=1000,
        progress_cb=None,
        cancel_cb=None,
        counter=[0],
    )

    assert received == [[b"1", b"a"], [b"2", b"b"], [b"3", b"c"]]


def test_load_data_via_fifo_engine_error_propagates_and_cleans_up_fifo(tmp_path):
    class _FailingConn:
        def exec_driver_sql(self, sql):
            raise RuntimeError("1146: Table 'x.does_not_exist' doesn't exist")

    # Se fotografía el directorio ANTES: la aserción tiene que ser sobre el FIFO de ESTE test,
    # no sobre el estado global de /dev/shm. La versión original comparaba contra la lista
    # vacía, así que un FIFO filtrado por CUALQUIER otra cosa —una corrida anterior que el
    # sistema mató antes del `finally`, por ejemplo— hacía fallar este test para siempre y
    # apuntaba al código equivocado. Costó un rato de depuración averiguarlo.
    previos = {p for p in os.listdir(dc._fifo_dir()) if p.startswith("gw_clone_")}

    with pytest.raises(RuntimeError, match="does_not_exist"):
        dc._load_data_via_fifo(
            dest_conn=_FailingConn(),
            load_target="`does_not_exist`",
            cols_q="`id`, `name`",
            table="does_not_exist",
            rows_iter=iter([(1, "a")]),
            batch_rows=1000,
            progress_cb=None,
            cancel_cb=None,
            counter=[0],
        )
    # El escritor bloqueado en open('wb') se desbloquea y el FIFO se limpia (no queda
    # huérfano en /dev/shm ni en el tmpdir).
    ahora = {p for p in os.listdir(dc._fifo_dir()) if p.startswith("gw_clone_")}
    assert ahora - previos == set()


# --------------------------------------------------------------------------- #
# Regresión: staging con PK aunque upsert=False (conflicto detectado en FINAL) #
# --------------------------------------------------------------------------- #
class _FakeMySQLDestConn:
    """
    Fake mínimo que ejecuta ``_copy_writer_mysql`` completo (CREATE TEMPORARY TABLE,
    LOAD DATA LOCAL INFILE vía FIFO -- incluyendo el lado lector, como pymysql --,
    INSERT ... SELECT, DROP TEMPORARY TABLE) contra tablas en memoria. Simplificado
    igual que los fakes de arriba: sin backslash/tab/newline en los valores de prueba.
    """

    def __init__(self, final_rows, *, pk_index=0):
        self.tables: dict[str, list[tuple]] = {"widget": list(final_rows)}
        self._pk_index = pk_index

    def exec_driver_sql(self, sql):
        m = re.match(r"CREATE TEMPORARY TABLE `([^`]+)` LIKE `([^`]+)`", sql)
        if m:
            self.tables[m.group(1)] = []
            return None

        m = re.match(r"DROP TEMPORARY TABLE IF EXISTS `([^`]+)`", sql)
        if m:
            self.tables.pop(m.group(1), None)
            return None

        m = re.match(
            r"LOAD DATA LOCAL INFILE '([^']+)' INTO TABLE `([^`]+)` FIELDS.*\(([^)]+)\)$",
            sql,
        )
        if m:
            path, target = m.group(1), m.group(2)
            self.tables.setdefault(target, [])
            with open(path, "rb") as fh:
                for line in fh:
                    fields = line.rstrip(b"\n").split(b"\t")
                    row = tuple(
                        None if f == b"\\N" else f.decode("utf-8") for f in fields
                    )
                    self.tables[target].append(row)
            return None

        m = re.match(
            r"^INSERT (?:IGNORE )?INTO `([^`]+)` \([^)]+\) SELECT [^)]+ FROM `([^`]+)`", sql
        )
        if m:
            final, stg = m.group(1), m.group(2)
            has_on_duplicate_update = "ON DUPLICATE KEY UPDATE" in sql
            is_ignore = sql.startswith("INSERT IGNORE")
            for row in self.tables.get(stg, []):
                rows = self.tables[final]
                conflict_idx = next(
                    (i for i, r in enumerate(rows) if r[self._pk_index] == row[self._pk_index]),
                    None,
                )
                if conflict_idx is None:
                    rows.append(row)
                elif has_on_duplicate_update:
                    rows[conflict_idx] = row
                elif is_ignore:
                    pass  # INSERT IGNORE: conflicto silenciado, fila descartada
                else:
                    raise RuntimeError(
                        f"1062: Duplicate entry '{row[self._pk_index]}' for key 'PRIMARY'"
                    )
            return None

        raise AssertionError(f"SQL no reconocido por _FakeMySQLDestConn: {sql!r}")


def test_mysql_writer_stages_with_pk_even_without_upsert_and_fails_on_final_conflict():
    """
    Regresión del fix ``use_staging = bool(spec.primary_key)`` (antes solo se activaba
    con ``upsert=True``): con PK y upsert=False, una fila que colisiona en la tabla
    FINAL (nunca en la staging, que siempre nace vacía) debe hacer fallar el
    ``INSERT ... SELECT`` final. Antes del fix, cargar directo a la final con LOAD DATA
    LOCAL habría hecho IGNORE silencioso del duplicado (comportamiento fijo de LOCAL en
    MySQL, sin sintaxis para abortar a mitad de archivo) -- divergiendo del INSERT
    legacy, que sí falla con ER_DUP_ENTRY.
    """
    conn = _FakeMySQLDestConn(final_rows=[("1", "orig")])
    spec = _spec(upsert=False)  # primary_key=["id"], upsert=False

    with pytest.raises(RuntimeError, match="Duplicate entry"):
        dc._copy_writer_mysql(
            conn, "mysql", spec, iter([("1", "new")]), 1000, None, None, [0]
        )

    # La final no cambió (el conflicto abortó el INSERT...SELECT) y la staging se
    # limpió en el finally (no queda ninguna tabla temporal huérfana).
    assert conn.tables["widget"] == [("1", "orig")]
    assert not any(name.startswith("_gw_stg_") for name in conn.tables)


def test_mysql_writer_staging_upsert_resolves_final_conflict():
    # Mismo escenario que el anterior, pero con upsert=True: el ON DUPLICATE KEY UPDATE
    # debe absorber el conflicto en la final (actualizar) en vez de fallar la tabla.
    conn = _FakeMySQLDestConn(final_rows=[("1", "orig")])
    spec = _spec(upsert=True)

    dc._copy_writer_mysql(conn, "mysql", spec, iter([("1", "new")]), 1000, None, None, [0])

    assert conn.tables["widget"] == [("1", "new")]
    assert not any(name.startswith("_gw_stg_") for name in conn.tables)



# --------------------------------------------------------------------------- #
# B1 -- guard anti "rogue MySQL server" en LOAD DATA LOCAL INFILE             #
# --------------------------------------------------------------------------- #
# Añadido concurrentemente a este trabajo de QA (ver el guard nuevo en
# ``data_copy.py``): pymysql, con ``local_infile=True``, abre y envía CUALQUIER archivo
# que el SERVIDOR pida en respuesta al LOAD_LOCAL request -- no necesariamente el que el
# gateway puso en su SQL. Un servidor MySQL comprometido podría pedir `.env`/la clave
# Fernet. El guard parchea ``MySQLResult._read_load_local_packet`` para rechazar
# cualquier filename que no coincida EXACTO con el FIFO esperado (thread-local, armado
# solo durante la ventana del ``exec_driver_sql(LOAD DATA)``).
#
# El unit-test directo de ``_guarded_read_load_local_packet`` (rechazo sin ventana armada,
# rechazo por filename distinto, aceptación con match exacto, verificación del parche
# instalado en la clase) YA está cubierto en ``tests/test_load_local_guard.py`` -- no lo
# duplicamos aquí. Lo que SÍ agregamos, porque no está en ese archivo: (1) que el mensaje
# de error no filtra el path que pidió el "servidor" atacante, y (2) la integración real
# con ``_load_data_via_fifo`` -- que el thread-local queda ARMADO solo durante la ventana
# del ``exec_driver_sql`` y DESARMADO después, tanto en éxito como en fallo del motor.
def test_guard_error_message_does_not_leak_requested_path():
    from pymysql.err import OperationalError

    class _FakeLoadLocalPacket:
        def __init__(self, filename: bytes):
            self._filename = filename

        def is_load_local_packet(self):
            return True

        def get_all_data(self):
            return b"\xfb" + self._filename

    dc._expected_local_infile_path.path = "/dev/shm/gw_clone_expected.tsv"
    try:
        packet = _FakeLoadLocalPacket(b"/etc/shadow")
        with pytest.raises(OperationalError) as exc_info:
            dc._guarded_read_load_local_packet(None, packet)
        assert "/etc/shadow" not in str(exc_info.value)
    finally:
        dc._expected_local_infile_path.path = None


def test_load_data_via_fifo_arms_guard_only_during_engine_call():
    captured = {}

    class _CapturingConn:
        def exec_driver_sql(self, sql):
            captured["armed_path"] = getattr(dc._expected_local_infile_path, "path", None)
            # Simula lo que hace pymysql: abre el FIFO para lectura y lo drena hasta EOF.
            m = re.search(r"LOAD DATA LOCAL INFILE '([^']+)'", sql)
            with open(m.group(1), "rb") as fh:
                fh.read()
            return None

    dc._load_data_via_fifo(
        dest_conn=_CapturingConn(),
        load_target="`widget`",
        cols_q="`id`, `name`",
        table="widget",
        rows_iter=iter([(1, "a")]),
        batch_rows=1000,
        progress_cb=None,
        cancel_cb=None,
        counter=[0],
    )

    assert captured["armed_path"] is not None
    assert captured["armed_path"].startswith(dc._fifo_dir())
    # Fail-closed: desarmado fuera de la ventana del exec_driver_sql.
    assert getattr(dc._expected_local_infile_path, "path", None) is None


def test_load_data_via_fifo_disarms_guard_even_on_engine_error():
    class _FailingConn:
        def exec_driver_sql(self, sql):
            raise RuntimeError("1146: Table 'x.does_not_exist' doesn't exist")

    with pytest.raises(RuntimeError):
        dc._load_data_via_fifo(
            dest_conn=_FailingConn(),
            load_target="`widget`",
            cols_q="`id`, `name`",
            table="widget",
            rows_iter=iter([(1, "a")]),
            batch_rows=1000,
            progress_cb=None,
            cancel_cb=None,
            counter=[0],
        )
    assert getattr(dc._expected_local_infile_path, "path", None) is None


def test_mysql_writer_no_pk_skips_staging_entirely():
    # Sin PK no hay concepto de conflicto: carga directo a la final, sin staging.
    conn = _FakeMySQLDestConn(final_rows=[])
    spec = _spec(pk=[], upsert=False)

    dc._copy_writer_mysql(
        conn, "mysql", spec, iter([("1", "a"), ("2", "b")]), 1000, None, None, [0]
    )

    assert sorted(conn.tables["widget"]) == [("1", "a"), ("2", "b")]
    assert not any(name.startswith("_gw_stg_") for name in conn.tables)


# --------------------------------------------------------------------------- #
# Duración por tabla                                                           #
# --------------------------------------------------------------------------- #
# Nunca se había calculado: los ítems de la fase de datos llegaban al historial con
# `execution_ms` en NULL, así que el reporte sabía cuánto tardó cada CREATE TABLE y no cuánto
# tardó copiar una tabla — que es la pregunta que el operador realmente se hace.


def test_copy_reports_duration_for_applied_table(monkeypatch, tmp_path):
    _setup_sqlite_env(monkeypatch, tmp_path, [(1, "a"), (2, "b")])
    results = _copy(specs=[_spec()])
    assert results[0].status == "applied"
    assert isinstance(results[0].duration_ms, int)
    assert results[0].duration_ms >= 0


def test_copy_reports_duration_even_when_the_table_fails(monkeypatch, tmp_path):
    """
    Una tabla que tardó tres minutos ANTES de reventar es un dato de diagnóstico. Descartar la
    duración en el camino de fallo dejaría el reporte ciego justo en el caso que más se
    investiga.
    """
    # Una fila que choca contra la PK del destino => INSERT plano => la tabla falla.
    _setup_sqlite_env(monkeypatch, tmp_path, [(1, "a")], create_dest_rows=[(1, "ya estaba")])
    results = _copy(specs=[_spec()])
    assert results[0].status == "failed"
    assert isinstance(results[0].duration_ms, int)
    assert results[0].duration_ms >= 0



# ── TIME con fracción de segundo: el defecto que el primer arreglo dejó pasar ──────
def test_render_time_preserva_la_fraccion_de_segundo():
    """
    ``TIME(3)``/``TIME(6)`` tienen microsegundos y hay que copiarlos.

    ``format_timedelta`` —el criterio de PRESENTACIÓN del proyecto— hace
    ``int(total_seconds())`` y los tira. Usarlo para reinsertar convertía ``01:02:03.123456``
    en ``01:02:03`` sin que nada fallara, porque la fase relaja el sql_mode estricto.
    """
    assert render_time_for_reinsert(
        timedelta(hours=1, minutes=2, seconds=3, microseconds=123456)
    ) == "01:02:03.123456"


def test_render_time_negativo_con_fraccion_conserva_valor_y_signo():
    """El peor caso del truncado: ``-00:00:00.5`` se volvía ``00:00:00``, sin valor ni signo."""
    assert render_time_for_reinsert(timedelta(microseconds=-500000)) == "-00:00:00.500000"


def test_render_time_sin_fraccion_no_agrega_ceros():
    """Un TIME(0) no debe salir con ``.000000`` de más: sería ruido en el destino."""
    assert render_time_for_reinsert(timedelta(hours=2)) == "02:00:00"
    assert "." not in render_time_for_reinsert(timedelta(hours=838))


def test_render_time_aplica_el_signo_al_TOTAL_no_a_las_horas():
    """
    Es exactamente el defecto de ``pymysql.escape_timedelta``, que rendea ``-01:30:00`` como
    ``-2:30:00`` y ``-00:00:01`` como ``-1:59:59``. Valores válidos, silenciosos y distintos
    del original.
    """
    assert render_time_for_reinsert(timedelta(hours=-1, minutes=-30)) == "-01:30:00"
    assert render_time_for_reinsert(timedelta(seconds=-1)) == "-00:00:01"
    assert render_time_for_reinsert(
        -(timedelta(hours=838, minutes=59, seconds=59))
    ) == "-838:59:59"


def test_adapt_value_normaliza_timedelta_para_los_DOS_writers():
    """
    La normalización vive en ``_adapt_value`` y no en cada writer.

    Si cada camino serializara por su cuenta, despachar una tabla al writer bulk o al legacy
    cambiaría lo que queda escrito en el destino — que es justamente la clase de divergencia
    silenciosa que ya nos costó dos bugs en este mismo tipo.
    """
    td = timedelta(hours=1, minutes=2, seconds=3, microseconds=123456)
    adaptado = _adapt_value(td)
    assert adaptado == "01:02:03.123456", "el legacy recibe el str ya normalizado"
    assert _render_mysql_field(adaptado) == b"01:02:03.123456", "y el FIFO lo pasa tal cual"
    # Y por el camino directo (defensa si alguien llama al render sin adaptar) da lo mismo.
    assert _render_mysql_field(td) == b"01:02:03.123456"


# ── Contabilidad de filas: que una tabla que pierde filas no diga `applied` ────────
def test_verificar_filas_acepta_el_conteo_exacto():
    _verificar_filas_cargadas("t", enviadas=10, cargadas=10)


def test_verificar_filas_falla_si_el_motor_registro_menos():
    """
    El caso real: `LOAD DATA LOCAL` se comporta SIEMPRE como IGNORE y la fase relaja
    STRICT_TRANS_TABLES, así que un truncado o una colisión de clave única descartan filas
    sin error. Antes esto se reportaba `applied`.
    """
    with pytest.raises(_FilasPerdidas) as exc:
        _verificar_filas_cargadas("clientes", enviadas=100, cargadas=98)
    assert "100" in str(exc.value) and "98" in str(exc.value) and "clientes" in str(exc.value)


def test_verificar_filas_con_upsert_solo_exige_que_no_falten():
    """
    Con `ON DUPLICATE KEY UPDATE` MySQL cuenta 2 por fila actualizada, así que exigir
    igualdad daría falsos positivos y fallaría copias buenas.
    """
    _verificar_filas_cargadas("t", enviadas=10, cargadas=20, solo_minimo=True)
    with pytest.raises(_FilasPerdidas):
        _verificar_filas_cargadas("t", enviadas=10, cargadas=9, solo_minimo=True)


def test_verificar_filas_sin_rowcount_no_falla():
    """
    Si el driver no expone `rowcount` se prefiere no verificar antes que fallar una copia
    buena por una limitación del driver. `None` no es cero.
    """
    _verificar_filas_cargadas("t", enviadas=10, cargadas=None)


def test_verificar_filas_cero_enviadas_cero_cargadas():
    """Una tabla vacía es un caso legítimo, no un faltante."""
    _verificar_filas_cargadas("t", enviadas=0, cargadas=0)
