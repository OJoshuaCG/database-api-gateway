"""
Tests unitarios PUROS (sin motor) de la slice S1 de ``mcp-schema-definitions``: los adapters
dejan de fingir "objeto vacío" cuando el motor no entregó el cuerpo.

Cubre:
- S1.1/S1.2: cuerpo NULL de vista/rutina/trigger/event en MySQL/MariaDB -> sin ``TypeError``,
  cuerpo ``""`` en el DTO interno (el diff/export no cambia de contrato) Y un WARNING en el log.
- S1.3: las vistas de PostgreSQL salen de ``pg_class`` + ``pg_get_viewdef`` y NO de
  ``information_schema.views`` (que devuelve NULL a quien no es dueño de la vista).
- S1.4: ``ReadonlyIntrospector.events()``, ``EventInfo.schedule`` armado por el gateway y la clave
  ``event`` en el índice barato.

El motor se reemplaza por una conexión falsa que responde según un fragmento del SQL. Lo que NO
se verifica acá: el comportamiento contra un MariaDB/PostgreSQL reales (NULL vs error 1305 según
motor y privilegios), que queda para la verificación en staging.
"""

from types import SimpleNamespace

import pytest

from app.services.db_admin import base_adapter as base_adapter_module
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import EventInfo
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter
from app.services.db_admin.readonly_introspector import ReadonlyIntrospector


# --------------------------------------------------------------------------- #
# Dobles de prueba                                                             #
# --------------------------------------------------------------------------- #
class FakeRow(tuple):
    """Fila que se comporta como la de SQLAlchemy: indexable y con ``_mapping``."""

    def __new__(cls, mapping: dict):
        instance = super().__new__(cls, tuple(mapping.values()))
        instance._mapping = mapping
        return instance


class FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    """Responde cada ``execute`` con las filas del primer fragmento de SQL que coincida."""

    def __init__(self, responses: list[tuple[str, list]]):
        self._responses = responses
        self.executed_sql: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed_sql.append(sql)
        for fragment, rows in self._responses:
            if fragment in sql:
                return FakeResult(rows)
        return FakeResult([])


class WarningRecorder:
    def __init__(self):
        self.messages: list[str] = []

    def warning(self, message, *args, **kwargs):
        self.messages.append(message % args if args else message)

    def debug(self, *args, **kwargs):
        pass


@pytest.fixture
def warnings_recorded(monkeypatch) -> WarningRecorder:
    recorder = WarningRecorder()
    monkeypatch.setattr(base_adapter_module, "logger", recorder)
    return recorder


@pytest.fixture
def mysql_adapter() -> MySQLAdapter:
    return MySQLAdapter(target=None)


@pytest.fixture
def postgres_adapter() -> PostgresAdapter:
    return PostgresAdapter(target=None)


# --------------------------------------------------------------------------- #
# S1.1 / S1.2 — MySQL/MariaDB: cuerpo NULL                                     #
# --------------------------------------------------------------------------- #
def test_strip_definer_clause_acepta_none():
    assert ServerAdapter._strip_definer_clause(None) == ""


def test_strip_definer_clause_sigue_quitando_el_definer():
    ddl = "CREATE DEFINER=`root`@`%` PROCEDURE `p`() SELECT 1"
    assert "DEFINER" not in ServerAdapter._strip_definer_clause(ddl)


@pytest.mark.parametrize("raw_view_definition", [None, "", "   "])
def test_vista_mysql_sin_cuerpo_no_revienta_y_deja_advertencia(
    mysql_adapter, warnings_recorded, raw_view_definition
):
    conn = FakeConnection(
        [
            (
                "information_schema.VIEWS",
                [("v_ventas", raw_view_definition, "NONE", "DEFINER")],
            ),
            ("information_schema.COLUMNS", [("v_ventas", "id")]),
        ]
    )

    views = mysql_adapter._snapshot_views(conn, "db", "db")

    assert [v.name for v in views] == ["v_ventas"]
    assert views[0].definition == ""
    assert len(warnings_recorded.messages) == 1
    assert "v_ventas" in warnings_recorded.messages[0]


def test_vista_mysql_con_cuerpo_no_deja_advertencia(mysql_adapter, warnings_recorded):
    conn = FakeConnection(
        [
            ("information_schema.VIEWS", [("v", "select 1 AS `a`", "CASCADED", "INVOKER")]),
            ("information_schema.COLUMNS", [("v", "a")]),
        ]
    )

    views = mysql_adapter._snapshot_views(conn, "db", "db")

    assert views[0].definition == "select 1 AS `a`"
    assert views[0].check_option == "CASCADED"
    assert warnings_recorded.messages == []


def test_rutina_mysql_con_cuerpo_null_no_lanza_typeerror(mysql_adapter, warnings_recorded):
    show_create_row = FakeRow({"Procedure": "p_cierre", "sql_mode": "", "Create Procedure": None})
    conn = FakeConnection(
        [
            (
                "information_schema.ROUTINES",
                [("p_cierre", "PROCEDURE", None, "NO", "DEFINER")],
            ),
            ("information_schema.PARAMETERS", []),
            ("SHOW CREATE PROCEDURE", [show_create_row]),
        ]
    )

    routines = mysql_adapter._snapshot_routines(conn, "db", "db")

    assert [(r.name, r.kind, r.body) for r in routines] == [("p_cierre", "PROCEDURE", "")]
    assert any("p_cierre" in message for message in warnings_recorded.messages)


def test_rutina_mysql_sin_fila_de_show_create_no_lanza(mysql_adapter, warnings_recorded):
    conn = FakeConnection(
        [
            ("information_schema.ROUTINES", [("f_total", "FUNCTION", "int", "YES", "INVOKER")]),
            ("information_schema.PARAMETERS", []),
        ]
    )

    routines = mysql_adapter._snapshot_routines(conn, "db", "db")

    assert routines[0].body == ""
    assert any("f_total" in message for message in warnings_recorded.messages)


def test_trigger_mysql_con_cuerpo_null_no_lanza(mysql_adapter, warnings_recorded):
    show_create_row = FakeRow(
        {"Trigger": "trg", "sql_mode": "", "SQL Original Statement": None}
    )
    conn = FakeConnection(
        [
            (
                "information_schema.TRIGGERS",
                [("trg", "pedidos", "BEFORE", "INSERT", "ROW")],
            ),
            ("SHOW CREATE TRIGGER", [show_create_row]),
        ]
    )

    triggers = mysql_adapter._snapshot_triggers(conn, "db", "db")

    assert [(t.name, t.table, t.action) for t in triggers] == [("trg", "pedidos", "")]
    assert any("trg" in message for message in warnings_recorded.messages)


def test_rutina_mysql_con_cuerpo_se_devuelve_sin_definer(mysql_adapter, warnings_recorded):
    ddl = "CREATE DEFINER=`root`@`%` PROCEDURE `p`() SELECT 1"
    show_create_row = FakeRow({"Procedure": "p", "sql_mode": "", "Create Procedure": ddl})
    conn = FakeConnection(
        [
            ("information_schema.ROUTINES", [("p", "PROCEDURE", None, "NO", "DEFINER")]),
            ("information_schema.PARAMETERS", []),
            ("SHOW CREATE PROCEDURE", [show_create_row]),
        ]
    )

    routines = mysql_adapter._snapshot_routines(conn, "db", "db")

    assert routines[0].body == "CREATE PROCEDURE `p`() SELECT 1"
    assert warnings_recorded.messages == []


# --------------------------------------------------------------------------- #
# S1.3 — PostgreSQL: vistas por pg_class + pg_get_viewdef                      #
# --------------------------------------------------------------------------- #
def test_vistas_postgres_usan_pg_class_y_no_information_schema_views(postgres_adapter):
    conn = FakeConnection(
        [
            ("pg_get_viewdef", [("v_ventas", " SELECT 1 AS a;", None)]),
            ("information_schema.columns", [("a",)]),
        ]
    )

    views = postgres_adapter._snapshot_views(conn, "db", "public")

    all_sql = "\n".join(conn.executed_sql)
    assert "pg_class" in all_sql
    assert "pg_get_viewdef" in all_sql
    assert "information_schema.views" not in all_sql
    assert views[0].name == "v_ventas"
    assert views[0].definition == " SELECT 1 AS a;"
    assert views[0].is_materialized is False


def test_vista_postgres_de_otro_dueno_devuelve_cuerpo(postgres_adapter, warnings_recorded):
    """El caso del bug: un no-dueño recibía NULL de information_schema.views y ``""``."""
    conn = FakeConnection(
        [
            ("pg_get_viewdef", [("v_ajena", " SELECT id FROM t;", None)]),
            ("information_schema.columns", [("id",)]),
        ]
    )

    views = postgres_adapter._snapshot_views(conn, "db", "public")

    assert views[0].definition == " SELECT id FROM t;"
    assert warnings_recorded.messages == []


@pytest.mark.parametrize(
    "reloptions, expected_check_option",
    [
        (None, None),
        ([], None),
        (["security_barrier=true"], None),
        (["check_option=cascaded"], "CASCADED"),
        (["security_barrier=true", "check_option=local"], "LOCAL"),
    ],
)
def test_check_option_sale_de_reloptions(postgres_adapter, reloptions, expected_check_option):
    conn = FakeConnection(
        [
            ("pg_get_viewdef", [("v", " SELECT 1;", reloptions)]),
            ("information_schema.columns", []),
        ]
    )

    views = postgres_adapter._snapshot_views(conn, "db", "public")

    assert views[0].check_option == expected_check_option


def test_vista_postgres_con_cuerpo_null_deja_advertencia(postgres_adapter, warnings_recorded):
    conn = FakeConnection(
        [
            ("pg_get_viewdef", [("v_rara", None, None)]),
            ("information_schema.columns", []),
        ]
    )

    views = postgres_adapter._snapshot_views(conn, "db", "public")

    assert views[0].definition == ""
    assert any("v_rara" in message for message in warnings_recorded.messages)


# --------------------------------------------------------------------------- #
# S1.4 — events: schedule, índice barato y façade                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "event_type, execute_at, interval_value, interval_field, expected_schedule",
    [
        ("ONE TIME", "2026-12-31 23:00:00", None, None, "AT 2026-12-31 23:00:00"),
        ("RECURRING", None, "1", "day", "EVERY 1 DAY"),
        ("RECURRING", None, None, None, None),
        ("ONE TIME", None, None, None, None),
        (None, None, None, None, None),
    ],
)
def test_schedule_del_event_lo_arma_el_gateway(
    event_type, execute_at, interval_value, interval_field, expected_schedule
):
    schedule = MySQLAdapter._build_event_schedule(
        event_type, execute_at, interval_value, interval_field
    )

    assert schedule == expected_schedule


def test_snapshot_events_llena_nombre_schedule_y_cuerpo(mysql_adapter, warnings_recorded):
    show_create_row = FakeRow(
        {
            "Event": "ev_limpieza",
            "sql_mode": "",
            "time_zone": "SYSTEM",
            "Create Event": "CREATE DEFINER=`root`@`%` EVENT `ev_limpieza` ON SCHEDULE EVERY 1 DAY DO SELECT 1",
        }
    )
    conn = FakeConnection(
        [
            ("information_schema.EVENTS", [("ev_limpieza", "RECURRING", None, "1", "DAY")]),
            ("SHOW CREATE EVENT", [show_create_row]),
        ]
    )

    events = mysql_adapter._snapshot_events(conn, "db", "db")

    assert events == [
        EventInfo(
            name="ev_limpieza",
            schedule="EVERY 1 DAY",
            body="CREATE EVENT `ev_limpieza` ON SCHEDULE EVERY 1 DAY DO SELECT 1",
        )
    ]
    assert warnings_recorded.messages == []


def test_snapshot_events_con_cuerpo_null_no_lanza(mysql_adapter, warnings_recorded):
    show_create_row = FakeRow({"Event": "ev", "sql_mode": "", "time_zone": "", "Create Event": None})
    conn = FakeConnection(
        [
            ("information_schema.EVENTS", [("ev", "ONE TIME", "2030-01-01 00:00:00", None, None)]),
            ("SHOW CREATE EVENT", [show_create_row]),
        ]
    )

    events = mysql_adapter._snapshot_events(conn, "db", "db")

    assert (events[0].name, events[0].schedule, events[0].body) == (
        "ev",
        "AT 2030-01-01 00:00:00",
        "",
    )
    assert any("ev" in message for message in warnings_recorded.messages)


def test_list_event_names_no_hace_show_create(mysql_adapter):
    conn = FakeConnection([("information_schema.EVENTS", [("ev_b",), ("ev_a",)])])

    names = mysql_adapter.list_event_names(conn, "db")

    assert names == ["ev_b", "ev_a"]
    assert all("SHOW CREATE" not in sql for sql in conn.executed_sql)


def test_list_event_names_en_postgres_es_vacio(postgres_adapter):
    conn = FakeConnection([])

    assert postgres_adapter.list_event_names(conn, "db") == []
    assert conn.executed_sql == []


class FakeSession:
    """Lo mínimo de ``ExportSession`` que usa el façade."""

    def __init__(self, conn):
        self.conn = conn
        self.deadline_checks = 0

    def check_deadline(self) -> None:
        self.deadline_checks += 1


def test_facade_events_delega_en_el_snapshot_del_adapter():
    expected_events = [EventInfo(name="ev", schedule="EVERY 1 DAY", body="CREATE EVENT ev")]
    adapter = SimpleNamespace(
        _inspect_schema=lambda database: "inventory",
        _snapshot_events=lambda conn, database, schema: (
            expected_events if (database, schema) == ("inventory", "inventory") else []
        ),
    )
    session = FakeSession(conn=object())
    facade = ReadonlyIntrospector(adapter, session, "inventory")

    assert facade.events() == expected_events
    assert session.deadline_checks == 1
