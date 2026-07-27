"""
Tests del falso positivo de VIEW/rutina/trigger/evento al diffear una BD contra su CLON.

Fallo REAL reportado en MariaDB (2026-07-27): tras clonar una BD y diffear origen vs clon,
el diff reportaba una vista como ``modified`` aunque manualmente ambas eran idénticas.

Causa: MySQL/MariaDB guardan la definición con el esquema CALIFICADO.
``information_schema.VIEWS.VIEW_DEFINITION`` devuelve siempre
``select `midb`.`t`.`col` from `midb`.`t```, así que cada BD lleva SU PROPIO nombre dentro
del cuerpo y dos objetos lógicamente idénticos tienen texto distinto. ``normalize_body`` no
lo cubre: colapsa whitespace y quita el ``DEFINER``, pero el nombre de la BD es parte del
texto de la consulta.

El fix (``sql_dialect.strip_self_schema_qualifier``) quita de cada lado el calificador de su
propia base ANTES de comparar. Lo importante es que NO enmascare diferencias reales: una
referencia a OTRA base se conserva en ambos lados.
"""

from app.services.db_admin.dtos import (
    ColumnInfo,
    EventInfo,
    RoutineInfo,
    SchemaSnapshot,
    TableSchema,
    TriggerInfo,
    ViewInfo,
)
from app.services.db_admin.schema_diff import diff_snapshots
from app.services.db_admin.sql_dialect import strip_self_schema_qualifier


def _table(db):
    return TableSchema(
        database=db, table="invoices",
        columns=[ColumnInfo(name="id", type="int", nullable=False)],
        primary_key=["id"], foreign_keys=[], indexes=[],
    )


def _snap(db, *, views=(), routines=(), triggers=(), events=(), engine="mariadb"):
    return SchemaSnapshot(
        database=db, source_engine=engine, tables=[_table(db)],
        views=list(views), routines=list(routines),
        triggers=list(triggers), events=list(events),
    )


def _view(db, extra=""):
    """Vista tal como la devuelve MariaDB: con el esquema propio calificado."""
    return ViewInfo(
        name="v_totales",
        definition=f"select `{db}`.`invoices`.`id` AS `id` from `{db}`.`invoices`{extra}",
        columns=["id"], security_definer=True,
    )


# --------------------------------------------------------------------------- #
# El falso positivo                                                            #
# --------------------------------------------------------------------------- #
def test_view_identical_in_a_clone_is_not_reported_as_modified():
    """El caso exacto reportado: BD vs su clon, misma vista, distinto nombre de BD."""
    diff = diff_snapshots(
        _snap("cirox_origen", views=[_view("cirox_origen")]),
        _snap("cirox_clon", views=[_view("cirox_clon")]),
    )
    assert diff.items == [], [
        (i.object_type, i.change_type, i.object_name) for i in diff.items
    ]


def test_routine_trigger_and_event_have_the_same_normalization():
    """El cuerpo de rutinas/triggers/eventos también lleva el esquema calificado."""
    def bundle(db):
        return {
            "routines": [RoutineInfo(
                name="sp_x", kind="PROCEDURE",
                body=f"CREATE PROCEDURE `sp_x`() BEGIN SELECT 1 FROM `{db}`.`invoices`; END",
            )],
            "triggers": [TriggerInfo(
                name="trg_x", table="invoices", timing="BEFORE", events=["INSERT"],
                action=f"CREATE TRIGGER `trg_x` BEFORE INSERT ON `{db}`.`invoices` "
                       f"FOR EACH ROW SET NEW.`id` = NEW.`id`",
            )],
            "events": [EventInfo(
                name="ev_x", schedule="EVERY 1 DAY",
                body=f"CREATE EVENT `ev_x` ON SCHEDULE EVERY 1 DAY DO "
                     f"DELETE FROM `{db}`.`invoices` WHERE `id` < 0",
            )],
        }

    diff = diff_snapshots(
        _snap("origen", **bundle("origen")), _snap("clon", **bundle("clon"))
    )
    assert diff.items == [], [
        (i.object_type, i.change_type, i.object_name) for i in diff.items
    ]


# --------------------------------------------------------------------------- #
# Lo que NO se debe enmascarar (el riesgo del fix)                             #
# --------------------------------------------------------------------------- #
def test_a_real_body_difference_is_still_detected():
    """Quitar el calificador propio no puede volver ciego al diff."""
    diff = diff_snapshots(
        _snap("origen", views=[_view("origen", extra=" where `id` > 0")]),
        _snap("clon", views=[_view("clon")]),
    )
    assert [(i.object_type, i.change_type) for i in diff.items] == [("view", "modified")]


def test_a_cross_database_reference_is_still_compared():
    """
    Una referencia a OTRA base es intencional y sí es una diferencia real: solo se quita el
    calificador de la base PROPIA de cada lado, nunca el de una tercera.
    """
    # El origen lee de una tercera BD; el clon lee de su propia BD -> diferencia REAL.
    src = ViewInfo(
        name="v_ext", definition="select `x` from `warehouse`.`ventas`", columns=["x"],
    )
    tgt = ViewInfo(
        name="v_ext", definition="select `x` from `clon`.`ventas`", columns=["x"],
    )
    diff = diff_snapshots(_snap("origen", views=[src]), _snap("clon", views=[tgt]))
    assert [(i.object_type, i.change_type) for i in diff.items] == [("view", "modified")]


def test_the_same_external_reference_on_both_sides_is_equal():
    """Si AMBOS lados apuntan a la misma BD externa, no hay diferencia."""
    ext = "select `x` from `warehouse`.`ventas`"
    diff = diff_snapshots(
        _snap("origen", views=[ViewInfo(name="v", definition=ext, columns=["x"])]),
        _snap("clon", views=[ViewInfo(name="v", definition=ext, columns=["x"])]),
    )
    assert diff.items == []


def test_other_view_attributes_still_differentiate():
    """El fix toca SOLO el cuerpo: check_option/security/columnas siguen comparándose."""
    diff = diff_snapshots(
        _snap("origen", views=[ViewInfo(
            name="v", definition="select `id` from `origen`.`invoices`",
            columns=["id"], check_option="CASCADED", security_definer=True)]),
        _snap("clon", views=[ViewInfo(
            name="v", definition="select `id` from `clon`.`invoices`",
            columns=["id"], check_option=None, security_definer=True)]),
    )
    assert [(i.object_type, i.change_type) for i in diff.items] == [("view", "modified")]


# --------------------------------------------------------------------------- #
# El helper, aislado                                                           #
# --------------------------------------------------------------------------- #
def test_strip_only_applies_to_the_mysql_family():
    """PostgreSQL no califica por nombre de BD (usa el schema `public`): no se toca."""
    body = 'select "x" from "midb"."t"'
    assert strip_self_schema_qualifier(body, "midb", "postgresql") == body


def test_strip_removes_only_the_backticked_own_qualifier():
    body = "select `a` from `midb`.`t` join `otra`.`u` on 1=1"
    assert strip_self_schema_qualifier(body, "midb", "mariadb") == (
        "select `a` from `t` join `otra`.`u` on 1=1"
    )


def test_strip_is_a_noop_without_database_or_body():
    assert strip_self_schema_qualifier("", "db", "mysql") == ""
    assert strip_self_schema_qualifier("select 1", "", "mysql") == "select 1"


def test_a_string_literal_that_looks_like_the_db_name_is_untouched():
    """Solo se quita la forma `db`. (backtick + punto), no un literal de cadena."""
    body = "select 'midb' as origen from `midb`.`t`"
    assert strip_self_schema_qualifier(body, "midb", "mysql") == (
        "select 'midb' as origen from `t`"
    )
