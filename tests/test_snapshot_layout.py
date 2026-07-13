"""
Tests unitarios PUROS del snapshot selectivo (sin cliente ni motor):

- ``snapshot_layout``: orden topológico por FK, orden canónico por clase, construcción
  de versiones (single/by_class), y validación del layout manual (todas las violaciones).
- ``snapshot_data``: render seguro de literales por motor, upsert idempotente, rollback
  por PK (simple y compuesta), guardrails y fail-closed de tipos no soportados.
"""

import pytest

from app.models.enums import EngineType
from app.services.db_admin import snapshot_data as sd
from app.services.db_admin import snapshot_layout as sl
from app.services.db_admin.dtos import DumpStatement, SeedResult
from app.services.db_admin.migrations import MigrationRunner, MigrationSpec


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _tbl(name, deps=None):
    return DumpStatement(
        object_type="table",
        name=name,
        ddl=f"CREATE TABLE {name} (id INT PRIMARY KEY)",
        depends_on=deps or [],
    )


def _obj(object_type, name, deps=None):
    return DumpStatement(
        object_type=object_type, name=name, ddl=f"CREATE {object_type} {name} ...",
        depends_on=deps or [],
    )


def _seed(table, pk=("id",)):
    return SeedResult(
        table=table, included=True, row_count=1, primary_key=list(pk),
        up_sql=f"INSERT INTO {table} ...;", down_sql=f"DELETE FROM {table} ...;",
    )


def _bucket(objects=None, data_tables=None, name=None):
    return {
        "name": name,
        "objects": [{"object_type": o[0], "name": o[1]} for o in (objects or [])],
        "data_tables": list(data_tables or []),
    }


# --------------------------------------------------------------------------- #
# Orden                                                                        #
# --------------------------------------------------------------------------- #
def test_topo_sort_places_fk_parent_before_child():
    # child (alfabéticamente antes) depende de parent → parent debe ir primero.
    child = _tbl("a_child", deps=["z_parent"])
    parent = _tbl("z_parent")
    ordered = sl.order_statements([child, parent])
    names = [s.name for s in ordered]
    assert names.index("z_parent") < names.index("a_child")


def test_order_statements_respects_class_order():
    ordered = sl.order_statements([
        _obj("routine", "r"),
        _obj("view", "v"),
        _tbl("t"),
    ])
    assert [s.object_type for s in ordered] == ["table", "view", "routine"]


# --------------------------------------------------------------------------- #
# build_versions                                                               #
# --------------------------------------------------------------------------- #
def test_build_versions_single_is_one_schema_version():
    plans = sl.build_versions(
        layout="single",
        selected=[_tbl("t"), _obj("view", "v")],
        seeds=[],
        baseline_name="Base",
        source_engine="mysql",
    )
    assert len(plans) == 1
    assert plans[0].kind == "schema"
    assert "CREATE TABLE t" in plans[0].up_sql


def test_build_versions_by_class_splits_by_object_class():
    plans = sl.build_versions(
        layout="by_class",
        selected=[_tbl("t"), _obj("view", "v"), _obj("routine", "r")],
        seeds=[],
        baseline_name="Estructura",
        source_engine="mysql",
    )
    assert len(plans) == 3
    assert plans[0].object_counts.get("table") == 1
    assert plans[1].object_counts.get("view") == 1
    assert plans[2].object_counts.get("routine") == 1
    # Rutinas → no portable en su versión.
    assert plans[2].has_non_portable is True
    assert plans[0].has_non_portable is False


def test_build_versions_data_always_last_one_per_table():
    plans = sl.build_versions(
        layout="single",
        selected=[_tbl("cat")],
        seeds=[_seed("cat")],
        baseline_name="B",
        source_engine="mysql",
    )
    assert len(plans) == 2
    assert plans[0].kind == "schema"
    assert plans[-1].kind == "data"
    assert plans[-1].down_sql_suggested == "DELETE FROM cat ...;"


# --------------------------------------------------------------------------- #
# validate_manual_layout                                                       #
# --------------------------------------------------------------------------- #
def test_manual_valid_layout_has_no_violations():
    stmts = [_tbl("parent"), _tbl("child", deps=["parent"])]
    buckets = [_bucket(objects=[("table", "parent"), ("table", "child")])]
    assert sl.validate_manual_layout(stmts, {}, buckets) == []


def test_manual_fk_dependency_in_later_version_is_flagged():
    stmts = [_tbl("parent"), _tbl("child", deps=["parent"])]
    buckets = [
        _bucket(objects=[("table", "child")]),
        _bucket(objects=[("table", "parent")]),
    ]
    reasons = {v["reason"] for v in sl.validate_manual_layout(stmts, {}, buckets)}
    assert "dependency_in_later_version" in reasons


def test_manual_view_before_all_tables_is_flagged():
    stmts = [_tbl("t"), _obj("view", "v")]
    buckets = [
        _bucket(objects=[("view", "v")]),
        _bucket(objects=[("table", "t")]),
    ]
    reasons = {v["reason"] for v in sl.validate_manual_layout(stmts, {}, buckets)}
    assert "must_be_after_all_tables" in reasons


def test_manual_unassigned_object_is_flagged():
    stmts = [_tbl("a"), _tbl("b")]
    buckets = [_bucket(objects=[("table", "a")])]
    v = sl.validate_manual_layout(stmts, {}, buckets)
    assert any(x["reason"] == "unassigned_object" and x["object"] == "b" for x in v)


def test_manual_data_before_schema_is_flagged():
    stmts = [_tbl("cat")]
    buckets = [
        _bucket(data_tables=["cat"]),          # datos primero (inválido)
        _bucket(objects=[("table", "cat")]),   # estructura después
    ]
    reasons = {v["reason"] for v in sl.validate_manual_layout(stmts, {"cat": _seed("cat")}, buckets)}
    assert "schema_after_data" in reasons or "data_before_table_structure" in reasons


def test_manual_skipped_data_table_in_own_bucket_is_not_a_violation():
    """
    Una tabla pedida en 'data_tables' pero omitida por un guardrail (vacía, sin PK, etc.)
    referenciada en su propio bucket NO debe bloquear la creación: se omite en silencio
    (igual que en single/by_class), no es un error de layout.
    """
    stmts = [_tbl("cat"), _tbl("empty_cat")]
    buckets = [
        _bucket(objects=[("table", "cat"), ("table", "empty_cat")]),
        _bucket(data_tables=["cat"]),
        _bucket(data_tables=["empty_cat"]),  # 'empty_cat' nunca se extrajo (vacía)
    ]
    violations = sl.validate_manual_layout(
        stmts, {"cat": _seed("cat")}, buckets, skipped_data_tables={"empty_cat"}
    )
    assert violations == []


def test_manual_data_table_never_requested_is_still_a_violation():
    """
    Si el bucket referencia una tabla que NUNCA se pidió en 'data_tables' (ni extraída ni
    en skipped_data_tables), sigue siendo un error real (typo/omisión del usuario).
    """
    stmts = [_tbl("cat")]
    buckets = [
        _bucket(objects=[("table", "cat")]),
        _bucket(data_tables=["cat_typo"]),
    ]
    violations = sl.validate_manual_layout(stmts, {}, buckets, skipped_data_tables=set())
    assert any(v["reason"] == "unknown_data_table" and v["object"] == "cat_typo" for v in violations)


# --------------------------------------------------------------------------- #
# snapshot_data.render_value                                                   #
# --------------------------------------------------------------------------- #
def test_render_value_mysql_basic_types():
    assert sd.render_value(None, "mysql") == "NULL"
    assert sd.render_value(7, "mysql") == "7"
    assert sd.render_value(True, "mysql") == "1"
    assert sd.render_value(False, "mysql") == "0"
    assert sd.render_value("O'Brien", "mysql") == "'O''Brien'"
    assert sd.render_value(b"\x00\xff", "mysql") == "x'00ff'"


def test_render_value_postgres_bool_and_bytea():
    assert sd.render_value(True, "postgresql") == "TRUE"
    assert sd.render_value(b"\xde\xad", "postgresql") == "decode('dead', 'hex')"


def test_render_value_backslash_is_escaped_mysql():
    # Inyección vía backslash: se dobla (protege NO_BACKSLASH_ESCAPES y default).
    assert sd.render_value("a\\b", "mysql") == "'a\\\\b'"


def test_render_value_unsupported_type_raises():
    with pytest.raises(sd.UnsupportedValueError):
        sd.render_value(object(), "mysql")


# --------------------------------------------------------------------------- #
# snapshot_data.build_seed                                                     #
# --------------------------------------------------------------------------- #
def test_build_seed_mysql_upsert_and_reverse_delete():
    res = sd.build_seed(
        dialect="mysql", table="cat", columns=["id", "name"], pk=["id"],
        rows=[(1, "a"), (2, "b")], mode="upsert", batch_rows=500, max_rows=1000, max_bytes=100000,
    )
    assert res.included and res.row_count == 2
    assert "INSERT INTO `cat` (`id`, `name`)" in res.up_sql
    assert "ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)" in res.up_sql
    # Rollback por PK en orden inverso al insert.
    assert "DELETE FROM `cat` WHERE `id` IN (2, 1);" in res.down_sql


def test_build_seed_postgres_on_conflict():
    res = sd.build_seed(
        dialect="postgresql", table="cat", columns=["id", "name"], pk=["id"],
        rows=[(1, "a")], mode="upsert", batch_rows=500, max_rows=1000, max_bytes=100000,
    )
    assert 'ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name"' in res.up_sql


def test_build_seed_insert_ignore_mode():
    res = sd.build_seed(
        dialect="mysql", table="c", columns=["id", "v"], pk=["id"],
        rows=[(1, "x")], mode="insert_ignore", batch_rows=500, max_rows=1000, max_bytes=100000,
    )
    assert "INSERT IGNORE INTO `c`" in res.up_sql
    assert "ON DUPLICATE KEY UPDATE" not in res.up_sql


def test_build_seed_composite_pk_delete_uses_tuples():
    res = sd.build_seed(
        dialect="mysql", table="m", columns=["a", "b", "v"], pk=["a", "b"],
        rows=[(1, 2, "x")], mode="upsert", batch_rows=500, max_rows=1000, max_bytes=100000,
    )
    assert "WHERE (`a`, `b`) IN ((1, 2));" in res.down_sql


def test_build_seed_oversize_bytes_skips():
    big = "x" * 1000
    res = sd.build_seed(
        dialect="mysql", table="t", columns=["id", "v"], pk=["id"],
        rows=[(1, big), (2, big)], mode="upsert", batch_rows=500, max_rows=1000, max_bytes=500,
    )
    assert not res.included and res.reason == "oversize_bytes"


def test_build_seed_accepts_iterator_not_just_list():
    # rows llega como resultado en streaming (iterador sin len): build_seed no debe len()-earlo.
    res = sd.build_seed(
        dialect="mysql", table="c", columns=["id"], pk=["id"],
        rows=iter([(1,), (2,), (3,)]), mode="upsert", batch_rows=500,
        max_rows=1000, max_bytes=100000,
    )
    assert res.included and res.row_count == 3


def test_build_seed_oversize_rows_skips():
    rows = [(i, f"n{i}") for i in range(10)]
    res = sd.build_seed(
        dialect="mysql", table="t", columns=["id", "v"], pk=["id"],
        rows=rows, mode="upsert", batch_rows=500, max_rows=5, max_bytes=100000,
    )
    assert not res.included and res.reason == "oversize_rows"


def test_build_seed_unsupported_type_skips_table():
    res = sd.build_seed(
        dialect="mysql", table="t", columns=["id", "v"], pk=["id"],
        rows=[(1, object())], mode="upsert", batch_rows=500, max_rows=1000, max_bytes=100000,
    )
    assert not res.included and res.reason.startswith("unsupported_type")


def test_build_seed_batches_multiple_statements():
    rows = [(i, f"n{i}") for i in range(5)]
    res = sd.build_seed(
        dialect="mysql", table="c", columns=["id", "name"], pk=["id"],
        rows=rows, mode="upsert", batch_rows=2, max_rows=1000, max_bytes=100000,
    )
    # 5 filas / lotes de 2 => 3 sentencias INSERT.
    assert res.up_sql.count("INSERT INTO `c`") == 3


# --------------------------------------------------------------------------- #
# Runner: una migración de DATOS NO se traduce cross-engine (regresión PG)      #
# --------------------------------------------------------------------------- #
def _data_spec(up, down):
    return MigrationSpec(
        id=1, version="0002", name="d", up_sql=up, up_sql_mysql=None,
        up_sql_postgresql=None, down_sql=down, checksum="x", kind="data",
    )


def test_runner_does_not_translate_data_up_sql_for_pg():
    # up_sql PG con identificadores entre comillas dobles: NO debe pasar por sqlglot
    # (lo leería como MySQL y "id" sería un literal de cadena).
    up = 'INSERT INTO "estado" ("id") VALUES (1) ON CONFLICT ("id") DO NOTHING'
    out = MigrationRunner().select_up_sql(_data_spec(up, None), EngineType.postgresql)
    assert out == up


def test_runner_does_not_translate_data_down_sql_for_pg():
    down = 'DELETE FROM "estado" WHERE "id" IN (2, 1);'
    out = MigrationRunner().select_down_sql(_data_spec("x", down), EngineType.postgresql)
    assert out == down  # verbatim, sin traducir
