"""
Tests del ORDEN DE EJECUCIÓN y del grafo de dependencias del diff estructural.

Cada test corresponde a un error REAL del motor que el orden anterior producía. El orden
se decidía por ``(fase, object_type, object_name)`` — alfabético dentro de la fase — y eso
alcanzaba para romper la migración en los casos de abajo. Son tests 100% PUROS (sin motor,
sin BD): el ordenador de ``schema_diff`` es la única capa donde este problema se puede
verificar de forma determinística.

Referencia de los errores que cubren:
  - ``1146 / 42P01``  objeto inexistente (vista sobre vista, matview sobre vista)
  - ``3813 / 42703``  columna inexistente en un CHECK
  - ``errno 150``     FK sin índice/UNIQUE/PK de respaldo en la tabla referida
  - ``1828``          DROP de una columna usada por una FK todavía viva
  - ``1553``          DROP de un índice que respalda una FK todavía viva
  - ``1451 / 2BP01``  DROP de una tabla padre antes que su hija
  - ``1061 / 42P07``  crear un objeto que ya existe (grupo atómico partido)
"""

import pytest

from app.services.db_admin.dtos import (
    CheckConstraintInfo,
    ColumnInfo,
    ForeignKeyInfo,
    IndexInfo,
    RoutineInfo,
    SchemaSnapshot,
    TableSchema,
    TriggerInfo,
    UniqueConstraintInfo,
    ViewInfo,
)
from app.services.db_admin.plan_integrity import (
    PlanItem,
    blocking,
    check_closure,
    expand_selection,
    prune_unsatisfied,
    validate_statement_plan,
)
from app.services.db_admin.schema_diff import (
    _table_dep_order,
    build_dependency_graph,
    diff_snapshots,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def col(name, type_="int", nullable=True, **kw):
    return ColumnInfo(name=name, type=type_, nullable=nullable, **kw)


def tbl(name, cols, pk=None, fks=None, ix=None, uq=None, ck=None):
    return TableSchema(
        database="d", table=name, columns=cols, primary_key=pk or [],
        foreign_keys=fks or [], indexes=ix or [],
        unique_constraints=uq or [], check_constraints=ck or [],
    )


def snap(tables=(), views=(), routines=(), triggers=(), engine="mysql"):
    return SchemaSnapshot(
        database="d", source_engine=engine, tables=list(tables), views=list(views),
        routines=list(routines), triggers=list(triggers),
    )


def index_of(diff, object_type, object_name):
    """Posición de un ítem en el orden de ejecución (-1 si no está)."""
    for i, item in enumerate(diff.items):
        if item.object_type == object_type and item.object_name == object_name:
            return i
    return -1


def assert_before(diff, first, second):
    """``first`` debe ejecutarse antes que ``second``; ambos son (tipo, nombre)."""
    a, b = index_of(diff, *first), index_of(diff, *second)
    assert a >= 0, f"no se encontró {first} en {[(i.object_type, i.object_name) for i in diff.items]}"
    assert b >= 0, f"no se encontró {second} en {[(i.object_type, i.object_name) for i in diff.items]}"
    assert a < b, (
        f"{first} debe ir ANTES de {second}; orden real: "
        f"{[(i.object_type, i.change_type, i.object_name) for i in diff.items]}"
    )


def as_plan(diff):
    """``DiffItem`` -> ``PlanItem`` (un ítem = una "sentencia", suficiente para el linter)."""
    return [
        PlanItem(
            id=i, seq=i, op_group=item.op_key(), depends_on=tuple(item.depends_on),
            object_type=item.object_type, object_name=item.object_name,
            change_type=item.change_type, has_down_sql=True,
            destructive=item.risk.destructive,
        )
        for i, item in enumerate(diff.items)
    ]


T = tbl("t", [col("id"), col("v")], pk=["id"])


# --------------------------------------------------------------------------- #
# Objetos con cuerpo: dependencias por referencia                              #
# --------------------------------------------------------------------------- #
def test_view_on_view_is_created_after_its_dependency():
    """``v_alpha`` lee de ``v_zeta``: alfabéticamente iba primero y fallaba con 1146."""
    diff = diff_snapshots(
        snap([T], [
            ViewInfo(name="v_alpha", definition="select * from `v_zeta` where v > 0"),
            ViewInfo(name="v_zeta", definition="select * from `t`"),
        ]),
        snap([T]),
    )
    assert_before(diff, ("view", "v_zeta"), ("view", "v_alpha"))


def test_materialized_view_after_the_view_it_reads():
    """PostgreSQL: ``materialized_view`` < ``view`` alfabéticamente -> 42P01."""
    diff = diff_snapshots(
        snap([T], [
            ViewInfo(name="v_base", definition="select * from t"),
            ViewInfo(name="mv_agg", is_materialized=True, definition="select * from v_base"),
        ], engine="postgresql"),
        snap([T], engine="postgresql"),
    )
    assert_before(diff, ("view", "v_base"), ("materialized_view", "mv_agg"))


def test_trigger_after_the_routine_it_calls():
    """PostgreSQL valida la función al crear el trigger."""
    diff = diff_snapshots(
        snap(
            [T],
            routines=[RoutineInfo(name="fn_audit", kind="FUNCTION",
                                  body="CREATE FUNCTION fn_audit() RETURNS trigger ...")],
            triggers=[TriggerInfo(name="trg_t", table="t",
                                  action="CREATE TRIGGER trg_t ... EXECUTE FUNCTION fn_audit()")],
        ),
        snap([T]),
    )
    assert_before(diff, ("routine", "FUNCTION:fn_audit"), ("trigger", "trg_t"))


def test_dropped_view_falls_before_the_view_it_depends_on():
    """Al borrar, la arista se invierte: el dependiente cae primero (PostgreSQL lo exige)."""
    views = [
        ViewInfo(name="v_dep", definition="select * from `v_base_z`"),
        ViewInfo(name="v_base_z", definition="select * from `t`"),
    ]
    diff = diff_snapshots(snap([T]), snap([T], views))
    assert_before(diff, ("view", "v_dep"), ("view", "v_base_z"))


# --------------------------------------------------------------------------- #
# Sub-objetos de tabla                                                         #
# --------------------------------------------------------------------------- #
def test_check_constraint_after_the_new_column_it_references():
    """``check_constraint`` < ``column`` alfabéticamente -> 3813 / 42703."""
    before = tbl("t", [col("id")], pk=["id"])
    after = tbl(
        "t", [col("id"), col("edad")], pk=["id"],
        ck=[CheckConstraintInfo(name="ck_edad", sqltext="edad >= 0")],
    )
    diff = diff_snapshots(snap([after]), snap([before]))
    assert_before(diff, ("column", "t.edad"), ("check_constraint", "t.ck_edad"))


def test_foreign_key_after_the_unique_constraint_it_needs():
    """``foreign_key`` < ``unique_constraint`` alfabéticamente -> errno 150."""
    parent_before = tbl("parent", [col("id"), col("code", "varchar(20)")], pk=["id"])
    parent_after = tbl(
        "parent", [col("id"), col("code", "varchar(20)")], pk=["id"],
        uq=[UniqueConstraintInfo(name="uq_code", columns=["code"])],
    )
    child_before = tbl("child", [col("id"), col("pcode", "varchar(20)")], pk=["id"])
    child_after = tbl(
        "child", [col("id"), col("pcode", "varchar(20)")], pk=["id"],
        fks=[ForeignKeyInfo(name="fk_c_p", columns=["pcode"],
                            referred_table="parent", referred_columns=["code"])],
    )
    diff = diff_snapshots(
        snap([parent_after, child_after]), snap([parent_before, child_before])
    )
    assert_before(
        diff, ("unique_constraint", "parent.uq_code"), ("foreign_key", "child.fk_c_p")
    )


def test_foreign_key_after_the_primary_key_added_in_the_same_diff():
    """La FK está en fase 3 y la PK en fase 4: ordenar por fase la ponía primero."""
    diff = diff_snapshots(
        snap([
            tbl("parent", [col("id")], pk=["id"]),
            tbl("child", [col("id"), col("pid")], pk=["id"],
                fks=[ForeignKeyInfo(name="fk2", columns=["pid"],
                                    referred_table="parent", referred_columns=["id"])]),
        ]),
        snap([
            tbl("parent", [col("id")]),  # sin PK
            tbl("child", [col("id"), col("pid")], pk=["id"]),
        ]),
    )
    assert_before(
        diff, ("primary_key", "parent.PRIMARY"), ("foreign_key", "child.fk2")
    )


def test_dropped_foreign_key_before_the_column_and_index_it_uses():
    """``column`` < ``foreign_key`` alfabéticamente -> 1828 (y 1553 para el índice)."""
    target_child = tbl(
        "child", [col("id"), col("pid")], pk=["id"],
        fks=[ForeignKeyInfo(name="fk5", columns=["pid"],
                            referred_table="parent", referred_columns=["id"])],
        ix=[IndexInfo(name="ix_pid", columns=["pid"], unique=False)],
    )
    parent = tbl("parent", [col("id")], pk=["id"])
    diff = diff_snapshots(
        snap([tbl("child", [col("id")], pk=["id"]), parent]),
        snap([target_child, parent]),
    )
    assert_before(diff, ("foreign_key", "child.fk5"), ("column", "child.pid"))
    assert_before(diff, ("foreign_key", "child.fk5"), ("index", "child.ix_pid"))


# --------------------------------------------------------------------------- #
# Tablas                                                                       #
# --------------------------------------------------------------------------- #
def test_referred_table_created_before_the_referring_one():
    """El nombre alfabético no debe ganarle a la dependencia por FK."""
    diff = diff_snapshots(
        snap([
            tbl("zebra", [col("id")], pk=["id"]),
            tbl("alpha", [col("id"), col("zid")], pk=["id"],
                fks=[ForeignKeyInfo(name="fk_a_z", columns=["zid"],
                                    referred_table="zebra", referred_columns=["id"])]),
        ]),
        snap([]),
    )
    assert_before(diff, ("table", "zebra"), ("table", "alpha"))


def test_child_table_dropped_before_its_parent():
    """
    Regresión de ``_table_dep_order``: agregaba a ``placed`` DENTRO de la pasada, así que
    una hija visitada después de su padre heredaba su nivel. Con ambas en nivel 0, el
    orden inverso de los DROP quedaba decidido por el nombre y borraba la PADRE primero
    -> ``1451 Cannot delete or update a parent row``.
    """
    diff = diff_snapshots(snap([]), snap([
        tbl("aaa_parent", [col("id")], pk=["id"]),
        tbl("zzz_child", [col("id"), col("pid")], pk=["id"],
            fks=[ForeignKeyInfo(name="fk_z", columns=["pid"],
                                referred_table="aaa_parent", referred_columns=["id"])]),
    ]))
    assert_before(diff, ("table", "zzz_child"), ("table", "aaa_parent"))


def test_table_dep_order_assigns_real_topological_levels():
    """Nivel por pasada, no por orden de visita dentro de la pasada."""
    tables = {
        "a": tbl("a", [col("id")], pk=["id"]),
        "b": tbl("b", [col("id"), col("aid")], pk=["id"],
                 fks=[ForeignKeyInfo(columns=["aid"], referred_table="a",
                                     referred_columns=["id"])]),
        "c": tbl("c", [col("id"), col("bid")], pk=["id"],
                 fks=[ForeignKeyInfo(columns=["bid"], referred_table="b",
                                     referred_columns=["id"])]),
    }
    rank = _table_dep_order(["a", "b", "c"], tables)
    assert rank == {"a": 0, "b": 1, "c": 2}, rank


def test_table_dep_order_ignores_dependencies_outside_the_batch():
    """Una FK a una tabla que YA existe en el destino no debe arruinar el orden del lote."""
    tables = {
        "nueva": tbl("nueva", [col("id"), col("eid")], pk=["id"],
                     fks=[ForeignKeyInfo(columns=["eid"], referred_table="existente",
                                         referred_columns=["id"])]),
        "existente": tbl("existente", [col("id")], pk=["id"]),
    }
    # Solo se ordena 'nueva': la FK apunta afuera del conjunto -> nivel 0, no bucket de ciclos.
    assert _table_dep_order(["nueva"], tables) == {"nueva": 0}


def test_cycle_does_not_raise_and_stays_deterministic():
    """Un ciclo de FKs no debe abortar el diff: queda al final, estable."""
    tables = {
        "x": tbl("x", [col("id"), col("yid")], pk=["id"],
                 fks=[ForeignKeyInfo(columns=["yid"], referred_table="y",
                                     referred_columns=["id"])]),
        "y": tbl("y", [col("id"), col("xid")], pk=["id"],
                 fks=[ForeignKeyInfo(columns=["xid"], referred_table="x",
                                     referred_columns=["id"])]),
    }
    rank = _table_dep_order(["x", "y"], tables)
    assert set(rank) == {"x", "y"}
    assert rank["x"] == rank["y"]  # mismo bucket, orden alfabético estable


# --------------------------------------------------------------------------- #
# Invariante de orden: el linter debe estar de acuerdo con el ordenador         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "diff_factory",
    [
        pytest.param(
            lambda: diff_snapshots(
                snap([T], [ViewInfo(name="v_alpha", definition="select * from `v_zeta`"),
                           ViewInfo(name="v_zeta", definition="select * from `t`")]),
                snap([T]),
            ),
            id="view-on-view",
        ),
        pytest.param(
            lambda: diff_snapshots(
                snap([tbl("t", [col("id"), col("edad")], pk=["id"],
                          ck=[CheckConstraintInfo(name="ck", sqltext="edad >= 0")])]),
                snap([tbl("t", [col("id")], pk=["id"])]),
            ),
            id="check-on-new-column",
        ),
        pytest.param(
            lambda: diff_snapshots(snap([]), snap([
                tbl("aaa_parent", [col("id")], pk=["id"]),
                tbl("zzz_child", [col("id"), col("pid")], pk=["id"],
                    fks=[ForeignKeyInfo(name="fk", columns=["pid"],
                                        referred_table="aaa_parent",
                                        referred_columns=["id"])]),
            ])),
            id="drop-child-before-parent",
        ),
    ],
)
def test_plan_satisfies_the_ordering_invariant(diff_factory):
    """El linter NO debe encontrar dependencias fuera de orden en un plan completo."""
    findings = blocking(validate_statement_plan(as_plan(diff_factory())))
    assert not findings, [f.message for f in findings]


def test_linter_detects_a_dependency_out_of_order():
    """Barrera de último recurso: si el orden se rompiera, el linter debe bloquear."""
    plan = [
        PlanItem(id=1, seq=0, op_group="view|v|new", depends_on=("table|t|new",),
                 object_type="view", object_name="v", change_type="new"),
        PlanItem(id=2, seq=1, op_group="table|t|new", object_type="table",
                 object_name="t", change_type="new"),
    ]
    findings = blocking(validate_statement_plan(plan))
    assert [f.code for f in findings] == ["dependency_out_of_order"]


def test_linter_detects_a_split_atomic_group():
    """Las sentencias de una redefinición no pueden quedar intercaladas con otras."""
    plan = [
        PlanItem(id=1, seq=0, op_group="index|t.ix|modified", object_type="index",
                 object_name="t.ix", change_type="modified"),
        PlanItem(id=2, seq=1, op_group="table|otra|new", object_type="table",
                 object_name="otra", change_type="new"),
        PlanItem(id=3, seq=2, op_group="index|t.ix|modified", object_type="index",
                 object_name="t.ix", change_type="modified"),
    ]
    assert "atomic_group_not_contiguous" in [
        f.code for f in blocking(validate_statement_plan(plan))
    ]


def test_linter_detects_duplicate_creation():
    plan = [
        PlanItem(id=1, seq=0, op_group="index|t.ix|new", object_type="index",
                 object_name="t.ix", change_type="new"),
        PlanItem(id=2, seq=1, op_group="index|t.ix|new|dup", object_type="index",
                 object_name="t.ix", change_type="new"),
    ]
    assert "duplicate_creation" in [
        f.code for f in blocking(validate_statement_plan(plan))
    ]


# --------------------------------------------------------------------------- #
# Grafo de dependencias y cierre de selección                                  #
# --------------------------------------------------------------------------- #
def test_dependency_graph_links_view_to_its_new_table():
    diff = diff_snapshots(
        snap([tbl("nueva", [col("id")], pk=["id"])],
             [ViewInfo(name="v_n", definition="select * from `nueva`")]),
        snap([]),
    )
    graph = build_dependency_graph(diff.items)
    assert "table|nueva|new" in graph["view|v_n|new"]


def test_selection_of_a_view_without_its_table_is_reported_as_incomplete():
    diff = diff_snapshots(
        snap([tbl("nueva", [col("id")], pk=["id"])],
             [ViewInfo(name="v_n", definition="select * from `nueva`")]),
        snap([]),
    )
    plan = as_plan(diff)
    view_ids = [p.id for p in plan if p.object_type == "view"]
    table_ids = [p.id for p in plan if p.object_type == "table"]

    assert check_closure(plan, view_ids), "debía faltar la tabla"

    closure = expand_selection(plan, view_ids)
    assert set(table_ids) <= set(closure.item_ids)
    # El cierre sale en ORDEN DE EJECUCIÓN, no en el orden pedido.
    assert closure.item_ids.index(table_ids[0]) < closure.item_ids.index(view_ids[0])
    assert not check_closure(plan, list(closure.item_ids))


def test_atomic_group_cannot_be_selected_partially():
    """Elegir el ADD de un índice redefinido sin su DROP -> 1061 Duplicate key name."""
    plan = [
        PlanItem(id=1, seq=0, op_group="index|t.ix|modified", object_type="index",
                 object_name="t.ix", change_type="modified"),
        PlanItem(id=2, seq=1, op_group="index|t.ix|modified", object_type="index",
                 object_name="t.ix", change_type="modified"),
    ]
    assert check_closure(plan, [2]), "un grupo atómico partido debe reportarse"
    assert set(expand_selection(plan, [2]).item_ids) == {1, 2}


def test_automatic_mode_prunes_orphans_instead_of_failing():
    """
    ``all_except_destructive`` excluye la tabla marcada ``possible_rename_of`` (destructiva)
    pero NO su índice (aditivo): ejecutar el índice solo daría "tabla inexistente".
    """
    plan = [
        PlanItem(id=1, seq=0, op_group="table|nueva|new", object_type="table",
                 object_name="nueva", change_type="new", destructive=True),
        PlanItem(id=2, seq=1, op_group="index|nueva.ix|new", depends_on=("table|nueva|new",),
                 object_type="index", object_name="nueva.ix", change_type="new"),
    ]
    kept, pruned = prune_unsatisfied(plan, [p.id for p in plan if not p.destructive])
    assert kept == []
    assert pruned == ["index|nueva.ix|new"]


def test_fk_drop_is_hoisted_before_a_column_type_change():
    """
    MySQL/MariaDB rechazan ``MODIFY COLUMN`` mientras una FK use esa columna
    (``1832``). Si el diff elimina la FK igual, hay que soltarla ANTES del ALTER, no en la
    fase destructiva (que va mucho después).
    """
    parent = tbl("parent", [col("id", "bigint")], pk=["id"])
    target_child = tbl(
        "child", [col("id"), col("pid", "int")], pk=["id"],
        fks=[ForeignKeyInfo(name="fk_pid", columns=["pid"],
                            referred_table="parent", referred_columns=["id"])],
    )
    source_child = tbl("child", [col("id"), col("pid", "bigint")], pk=["id"])
    diff = diff_snapshots(snap([parent, source_child]), snap([parent, target_child]))
    assert_before(diff, ("foreign_key", "child.fk_pid"), ("column", "child.pid"))


def test_unrelated_fk_drop_is_not_hoisted():
    """El adelanto es quirúrgico: una FK que no toca la columna retipada sigue al final."""
    parent = tbl("parent", [col("id")], pk=["id"])
    target_child = tbl(
        "child", [col("id"), col("pid"), col("nota", "varchar(10)")], pk=["id"],
        fks=[ForeignKeyInfo(name="fk_pid", columns=["pid"],
                            referred_table="parent", referred_columns=["id"])],
    )
    source_child = tbl(
        "child", [col("id"), col("pid"), col("nota", "varchar(50)")], pk=["id"]
    )
    diff = diff_snapshots(snap([parent, source_child]), snap([parent, target_child]))
    # 'nota' cambia de tipo pero la FK es sobre 'pid': el DROP queda en su paso destructivo.
    assert_before(diff, ("column", "child.nota"), ("foreign_key", "child.fk_pid"))


# --------------------------------------------------------------------------- #
# El OTRO camino de creación de versiones: baseline de snapshot                #
# --------------------------------------------------------------------------- #
def test_snapshot_layout_orders_bodies_by_dependency_not_alphabetically():
    """
    ``snapshot_layout.order_statements`` ordenaba vistas/matviews/rutinas por NOMBRE.
    Una ``v_alpha`` que lee de ``v_zeta`` salía primero y el baseline fallaba con
    ``1146``/``42P01``. Es el mismo bug que en el diff, en el otro camino.
    """
    from app.services.db_admin.dtos import DumpStatement
    from app.services.db_admin.snapshot_layout import order_statements

    ordered = order_statements([
        DumpStatement(object_type="view", name="v_alpha",
                      ddl="CREATE VIEW v_alpha AS SELECT * FROM v_zeta"),
        DumpStatement(object_type="view", name="v_zeta",
                      ddl="CREATE VIEW v_zeta AS SELECT * FROM t"),
        DumpStatement(object_type="table", name="t", ddl="CREATE TABLE t (id int)"),
    ])
    names = [s.name for s in ordered]
    assert names.index("t") < names.index("v_zeta") < names.index("v_alpha"), names


def test_snapshot_layout_puts_routines_before_views_and_indexes_after_matviews():
    """
    Dos reordenamientos de clase: una vista puede llamar a una función (PostgreSQL la valida
    al crear la vista) y ``pg_indexes`` incluye los índices de las MATVIEWS, que antes se
    emitían antes de la matview misma.
    """
    from app.services.db_admin.dtos import DumpStatement
    from app.services.db_admin.snapshot_layout import order_statements

    ordered = order_statements([
        DumpStatement(object_type="index", name="ix_mv",
                      ddl="CREATE INDEX ix_mv ON mv_agg (x)"),
        DumpStatement(object_type="materialized_view", name="mv_agg",
                      ddl="CREATE MATERIALIZED VIEW mv_agg AS SELECT 1"),
        DumpStatement(object_type="view", name="v", ddl="CREATE VIEW v AS SELECT fn_x()"),
        DumpStatement(object_type="routine", name="fn_x", ddl="CREATE FUNCTION fn_x() ..."),
    ])
    names = [s.name for s in ordered]
    assert names.index("fn_x") < names.index("v"), names
    assert names.index("mv_agg") < names.index("ix_mv"), names


def test_snapshot_layout_survives_a_view_cycle():
    """Un ciclo de vistas no debe abortar el layout: queda al final, determinista."""
    from app.services.db_admin.dtos import DumpStatement
    from app.services.db_admin.snapshot_layout import order_statements

    ordered = order_statements([
        DumpStatement(object_type="view", name="a", ddl="CREATE VIEW a AS SELECT * FROM b"),
        DumpStatement(object_type="view", name="b", ddl="CREATE VIEW b AS SELECT * FROM a"),
    ])
    assert {s.name for s in ordered} == {"a", "b"}


# --------------------------------------------------------------------------- #
# Dependencias de PostgreSQL: tipos ENUM y secuencias                          #
# --------------------------------------------------------------------------- #
def test_new_table_depends_on_the_enum_type_its_column_uses():
    """Elegir la tabla sin su ENUM daba "type does not exist" al ejecutar."""
    from app.services.db_admin.dtos import EnumTypeInfo

    src = SchemaSnapshot(
        database="d", source_engine="postgresql",
        tables=[tbl("t", [col("id"), col("estado", "mi_estado")], pk=["id"])],
        enum_types=[EnumTypeInfo(name="mi_estado", values=["a", "b"])],
    )
    diff = diff_snapshots(src, snap([], engine="postgresql"))
    graph = build_dependency_graph(diff.items)
    assert "enum_type|mi_estado|new" in graph["table|t|new"]
    assert_before(diff, ("enum_type", "mi_estado"), ("table", "t"))


def test_new_column_depends_on_the_sequence_in_its_default():
    from app.services.db_admin.dtos import SequenceInfo

    base = tbl("t", [col("id")], pk=["id"])
    src = SchemaSnapshot(
        database="d", source_engine="postgresql",
        tables=[tbl("t", [col("id"),
                          col("n", "integer", default="nextval('mi_seq'::regclass)")],
                    pk=["id"])],
        sequences=[SequenceInfo(name="mi_seq")],
    )
    diff = diff_snapshots(src, snap([base], engine="postgresql"))
    graph = build_dependency_graph(diff.items)
    assert "sequence|mi_seq|new" in graph["column|t.n|new"]


# --------------------------------------------------------------------------- #
# Rutinas invocadas por DDL de tabla (PostgreSQL: DEFAULT/CHECK/GENERATED)     #
# --------------------------------------------------------------------------- #
def test_routine_called_by_a_table_default_is_created_before_the_table():
    """
    PostgreSQL valida al CREATE TABLE las funciones de un DEFAULT (``next_id()``): con las
    rutinas en el paso 80 (después de las tablas) el DDL moría con 42883. La rutina
    referenciada se adelanta y la arista alimenta el cierre de selección.
    """
    from app.services.db_admin.dtos import RoutineInfo

    src = SchemaSnapshot(
        database="d", source_engine="postgresql",
        tables=[tbl("t", [col("id", "bigint", nullable=False, default="next_id()")],
                    pk=["id"])],
        routines=[RoutineInfo(name="next_id", kind="FUNCTION",
                              body="CREATE FUNCTION next_id() RETURNS bigint AS $$ "
                                   "SELECT 1 $$ LANGUAGE sql")],
    )
    diff = diff_snapshots(src, snap([], engine="postgresql"))
    assert_before(diff, ("routine", "FUNCTION:next_id"), ("table", "t"))
    graph = build_dependency_graph(diff.items)
    assert "routine|FUNCTION:next_id|new" in graph["table|t|new"]


def test_routine_that_reads_a_diff_table_is_not_hoisted():
    """
    Dependencia MUTUA (la función consulta una tabla del diff): adelantarla rompería la
    validación de una función SQL-language. Fail-closed: orden actual, sin arista.
    """
    from app.services.db_admin.dtos import RoutineInfo

    src = SchemaSnapshot(
        database="d", source_engine="postgresql",
        tables=[tbl("contadores", [col("id"), col("n")], pk=["id"])],
        routines=[RoutineInfo(name="next_id", kind="FUNCTION",
                              body="CREATE FUNCTION next_id() RETURNS bigint AS $$ "
                                   "UPDATE contadores SET n = n + 1 RETURNING n $$ "
                                   "LANGUAGE sql")],
    )
    diff = diff_snapshots(src, snap([], engine="postgresql"))
    # La tabla va primero (orden normal) y NO hay arista tabla->rutina.
    assert_before(diff, ("table", "contadores"), ("routine", "FUNCTION:next_id"))
    graph = build_dependency_graph(diff.items)
    assert "routine|FUNCTION:next_id|new" not in graph.get("table|contadores|new", set())


def test_snapshot_layout_hoists_routines_used_by_table_ddl():
    """Mismo caso en el camino del snapshot: el DEFAULT next_id() exige la función antes."""
    from app.services.db_admin.dtos import DumpStatement
    from app.services.db_admin.snapshot_layout import order_statements

    ordered = order_statements([
        DumpStatement(object_type="table", name="t",
                      ddl='CREATE TABLE t (id bigint DEFAULT next_id() NOT NULL)'),
        DumpStatement(object_type="routine", name="next_id",
                      ddl="CREATE FUNCTION next_id() RETURNS bigint ..."),
        DumpStatement(object_type="routine", name="fn_reporte",
                      ddl="CREATE FUNCTION fn_reporte() ... SELECT count(*) FROM t ..."),
    ])
    names = [s.name for s in ordered]
    # next_id (prerrequisito) antes de la tabla; fn_reporte (consulta la tabla) después.
    assert names.index("next_id") < names.index("t") < names.index("fn_reporte"), names
