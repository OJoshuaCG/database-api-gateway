"""
Tests unitarios PUROS del resolver de dependencias para clonación (``clone_dependencies``):
sin cliente ni motor, con fixtures ``SchemaSnapshot`` en memoria.

Cubre: cierre autoritativo por FK (transitivo y direccional), trigger→tabla, sugerencias
advisory por escaneo de cuerpos (no se agregan al cierre), orden topológico y avisos.
"""

from app.services.db_admin import clone_dependencies as cd
from app.services.db_admin.dtos import (
    ForeignKeyInfo,
    RoutineInfo,
    SchemaSnapshot,
    TableSchema,
    TriggerInfo,
    ViewInfo,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _tbl(name, *, fks=None, database="db"):
    return TableSchema(
        database=database, table=name, columns=[], primary_key=[],
        foreign_keys=fks or [], indexes=[],
    )


def _fk(referred, cols=("x",)):
    return ForeignKeyInfo(columns=list(cols), referred_table=referred, referred_columns=["id"])


def _snap(*, tables=None, views=None, routines=None, triggers=None, engine="mysql"):
    return SchemaSnapshot(
        database="db", source_engine=engine, tables=tables or [], views=views or [],
        routines=routines or [], triggers=triggers or [],
    )


def _ref(object_type, name):
    return cd.ObjectRef(object_type=object_type, name=name)


def _keys(refs):
    return {(r.object_type, r.name) for r in refs}


# =========================================================================== #
# Cierre autoritativo por FK                                                   #
# =========================================================================== #
def test_child_table_pulls_parent_via_fk():
    parent = _tbl("parent")
    child = _tbl("child", fks=[_fk("parent")])
    snap = _snap(tables=[parent, child])
    res = cd.resolve_closure(snap, [_ref("table", "child")])
    assert ("table", "parent") in _keys(res.added)
    assert ("table", "parent") in _keys(res.closure)
    assert ("table", "child") in _keys(res.closure)


def test_parent_table_does_not_pull_child():
    # La dependencia es direccional: el hijo depende del padre, no al revés.
    parent = _tbl("parent")
    child = _tbl("child", fks=[_fk("parent")])
    snap = _snap(tables=[parent, child])
    res = cd.resolve_closure(snap, [_ref("table", "parent")])
    assert ("table", "child") not in _keys(res.closure)
    assert res.added == []


def test_transitive_fk_closure():
    a = _tbl("a")
    b = _tbl("b", fks=[_fk("a")])
    c = _tbl("c", fks=[_fk("b")])
    snap = _snap(tables=[a, b, c])
    res = cd.resolve_closure(snap, [_ref("table", "c")])
    assert _keys(res.closure) >= {("table", "a"), ("table", "b"), ("table", "c")}


def test_unrelated_tables_stay_separate():
    a = _tbl("a")
    b = _tbl("b")
    snap = _snap(tables=[a, b])
    res = cd.resolve_closure(snap, [_ref("table", "a")])
    assert ("table", "b") not in _keys(res.closure)
    assert res.added == []


def test_table_order_is_topological_parent_before_child():
    parent = _tbl("parent")
    child = _tbl("child", fks=[_fk("parent")])
    snap = _snap(tables=[parent, child])
    res = cd.resolve_closure(snap, [_ref("table", "child")])
    assert res.table_order.index("parent") < res.table_order.index("child")


# =========================================================================== #
# Trigger → tabla (autoritativo)                                               #
# =========================================================================== #
def test_trigger_pulls_its_table():
    t = _tbl("users")
    tg = TriggerInfo(name="tg_users", table="users", action="BEGIN END")
    snap = _snap(tables=[t], triggers=[tg])
    res = cd.resolve_closure(snap, [_ref("trigger", "tg_users")])
    assert ("table", "users") in _keys(res.added)
    assert any(e.reason == "trigger_table" for e in res.edges)


# =========================================================================== #
# Advisory (best-effort, NO se agrega al cierre)                               #
# =========================================================================== #
def test_view_referencing_table_is_advisory_not_added():
    users = _tbl("users")
    v = ViewInfo(name="v_active_users", definition="SELECT id FROM users WHERE active = 1")
    snap = _snap(tables=[users], views=[v])
    res = cd.resolve_closure(snap, [_ref("view", "v_active_users")])
    # 'users' NO se agrega al cierre autoritativo (los cuerpos no son fiables)...
    assert ("table", "users") not in _keys(res.closure)
    # ...pero SÍ aparece como sugerencia advisory.
    assert any(
        e.to_type == "table" and e.to_name == "users" and e.reason == "body_reference"
        for e in res.advisory
    )


def test_advisory_not_emitted_when_target_already_in_closure():
    users = _tbl("users")
    v = ViewInfo(name="v_users", definition="SELECT id FROM users")
    snap = _snap(tables=[users], views=[v])
    res = cd.resolve_closure(snap, [_ref("view", "v_users"), _ref("table", "users")])
    # 'users' ya está en el cierre → no debe aparecer como advisory.
    assert not any(e.to_name == "users" for e in res.advisory)


def test_routine_body_reference_is_advisory():
    t = _tbl("orders")
    r = RoutineInfo(name="sp_report", kind="PROCEDURE",
                    body="CREATE PROCEDURE sp_report() BEGIN SELECT * FROM orders; END")
    snap = _snap(tables=[t], routines=[r])
    res = cd.resolve_closure(snap, [_ref("routine", "sp_report")])
    assert any(e.to_name == "orders" and e.reason == "body_reference" for e in res.advisory)


def test_body_scan_no_false_positive_on_substring():
    # 'orders' NO debe matchear dentro de 'orders_archive' (límite de palabra).
    t = _tbl("orders_archive")
    v = ViewInfo(name="v_x", definition="SELECT * FROM orders_archive")
    snap = _snap(tables=[t], views=[v])
    res = cd.resolve_closure(snap, [_ref("view", "v_x")])
    # Solo debe sugerir orders_archive, nunca un 'orders' inexistente.
    assert all(e.to_name == "orders_archive" for e in res.advisory)


# =========================================================================== #
# Avisos                                                                       #
# =========================================================================== #
def test_nonexistent_selection_produces_warning():
    snap = _snap(tables=[_tbl("a")])
    res = cd.resolve_closure(snap, [_ref("table", "ghost")])
    assert res.warnings
    assert ("table", "ghost") not in _keys(res.closure)


def test_build_graph_returns_authoritative_and_advisory():
    parent = _tbl("parent")
    child = _tbl("child", fks=[_fk("parent")])
    v = ViewInfo(name="v", definition="SELECT 1 FROM parent")
    snap = _snap(tables=[parent, child], views=[v])
    auth, advisory = cd.build_graph(snap)
    assert any(e.reason == "foreign_key" for e in auth)
    assert any(e.reason == "body_reference" for e in advisory)
