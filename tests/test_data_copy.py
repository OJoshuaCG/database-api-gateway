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

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine

from app.core.remote_engine import ServerTarget
from app.services.db_admin import data_copy as dc
from app.services.db_admin.data_copy import (
    TableCopyResult,
    TableCopySpec,
    _adapt_value,
    _build_insert,
    _build_select,
    copy_tables,
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
def test_spec_defaults():
    s = TableCopySpec(table="t", columns=["a"], primary_key=[])
    assert s.upsert is False
    assert s.primary_key == []


def test_result_defaults():
    r = TableCopyResult(table="t", status="applied")
    assert r.rows_copied == 0
    assert r.error is None


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
    def fake_conn(target, database):
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
