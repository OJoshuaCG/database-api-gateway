"""
Tests unitarios PUROS (conexión falsa, sin motor) de ``read_definition`` en los adapters y de la
fachada ``ReadonlyIntrospector.definition()`` / ``server_version()`` (slice S2 de
``mcp-schema-definitions``).

Cubre:
- MySQL/MariaDB: ``SHOW CREATE`` + metadatos por ``information_schema`` con parámetros enlazados;
  cuerpo NULL o consulta denegada/1305 -> ``body is None`` + ``insufficient_privilege`` (jamás
  ``""``); el DEFINER no llega al ``DefinitionRead``; rutinas PROCEDURE/FUNCTION con el mismo nombre.
- PostgreSQL: ``pg_get_*`` con el nombre SIEMPRE como parámetro enlazado; sobrecargas y triggers en
  varias tablas devuelven una lectura cada uno; event es ``engine_unsupported`` sin tocar el motor.
- Identificadores peligrosos y tipos inválidos fallan ANTES de ejecutar cualquier SQL.

Lo que NO se verifica acá: el comportamiento contra un MariaDB/PostgreSQL reales (NULL vs 1305 según
motor y privilegios), que queda para la verificación en staging.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_definition_read_definition``
"""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.exceptions import AppHttpException
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import DefinitionRead
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter
from app.services.db_admin.readonly_introspector import ReadonlyIntrospector

_MYSQL_PERMISSION_DENIED_CODE = 1142
_MYSQL_UNKNOWN_ROUTINE_CODE = 1305


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

    def scalar(self):
        return self._rows[0][0] if self._rows else None


def driver_error(code: int) -> OperationalError:
    """Error de driver con el errno que ``extract_driver_error_code`` sabe leer."""
    return OperationalError("statement", {}, Exception(code, "engine text never exposed"))


class FakeConnection:
    """
    Responde cada ``execute`` con la primera respuesta cuyo fragmento esté en el SQL. Si la
    respuesta es una excepción, la lanza (privilegio denegado, 1305). Registra SQL y parámetros.
    """

    def __init__(self, responses: list[tuple[str, object]]):
        self._responses = responses
        self.executed_sql: list[str] = []
        self.executed_params: list[dict | None] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        for fragment, outcome in self._responses:
            if fragment in sql:
                if isinstance(outcome, Exception):
                    raise outcome
                return FakeResult(outcome)
        return FakeResult([])


@pytest.fixture
def mysql_adapter() -> MySQLAdapter:
    return MySQLAdapter(target=None)


@pytest.fixture
def postgres_adapter() -> PostgresAdapter:
    return PostgresAdapter(target=None)


def _create_view_row(body: str | None) -> FakeRow:
    return FakeRow(
        {
            "View": "v_ventas",
            "Create View": body,
            "character_set_client": "utf8mb4",
            "collation_connection": "utf8mb4_general_ci",
        }
    )


# --------------------------------------------------------------------------- #
# MySQL / MariaDB — vistas                                                     #
# --------------------------------------------------------------------------- #
def test_mysql_vista_entrega_cuerpo_sin_definer_y_metadatos(mysql_adapter):
    create_body = (
        "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`%` SQL SECURITY DEFINER "
        "VIEW `v_ventas` AS select 1"
    )
    conn = FakeConnection(
        [
            ("SHOW CREATE VIEW", [_create_view_row(create_body)]),
            ("information_schema.VIEWS", [("CASCADED", "DEFINER")]),
        ]
    )

    reads = mysql_adapter.read_definition(conn, "db", "db", "view", "v_ventas")

    assert len(reads) == 1
    read = reads[0]
    assert read.kind == "view"
    assert read.name == "v_ventas"
    assert read.body is not None
    assert "DEFINER=" not in read.body
    assert "`root`@`%`" not in read.body
    assert read.unavailable_reason is None
    assert read.security == "definer"
    assert read.check_option == "CASCADED"


def test_mysql_vista_check_option_none_se_normaliza_a_none(mysql_adapter):
    conn = FakeConnection(
        [
            ("SHOW CREATE VIEW", [_create_view_row("CREATE VIEW `v_ventas` AS select 1")]),
            ("information_schema.VIEWS", [("NONE", "INVOKER")]),
        ]
    )

    read = mysql_adapter.read_definition(conn, "db", "db", "view", "v_ventas")[0]

    assert read.check_option is None
    assert read.security == "invoker"


def test_mysql_nombre_cuoteado_en_show_create_y_enlazado_en_metadatos(mysql_adapter):
    conn = FakeConnection(
        [
            ("SHOW CREATE VIEW", [_create_view_row("CREATE VIEW `v_ventas` AS select 1")]),
            ("information_schema.VIEWS", [("NONE", "DEFINER")]),
        ]
    )

    mysql_adapter.read_definition(conn, "mi_base", "mi_base", "view", "v_ventas")

    assert conn.executed_sql[0] == "SHOW CREATE VIEW `v_ventas`"
    assert "v_ventas" not in conn.executed_sql[1]
    assert conn.executed_params[1] == {"db": "mi_base", "name": "v_ventas"}


@pytest.mark.parametrize("null_or_blank_body", [None, "", "   "])
def test_mysql_vista_con_cuerpo_null_o_vacio_es_sin_cuerpo_y_no_cuerpo_vacio(
    mysql_adapter, null_or_blank_body
):
    conn = FakeConnection(
        [
            ("SHOW CREATE VIEW", [_create_view_row(null_or_blank_body)]),
            ("information_schema.VIEWS", [("NONE", "DEFINER")]),
        ]
    )

    read = mysql_adapter.read_definition(conn, "db", "db", "view", "v_ventas")[0]

    assert read.body is None
    assert read.unavailable_reason == "insufficient_privilege"


def test_mysql_vista_con_privilegio_denegado_es_sin_cuerpo(mysql_adapter):
    conn = FakeConnection(
        [
            ("SHOW CREATE VIEW", driver_error(_MYSQL_PERMISSION_DENIED_CODE)),
            ("information_schema.VIEWS", [("NONE", "DEFINER")]),
        ]
    )

    read = mysql_adapter.read_definition(conn, "db", "db", "view", "v_ventas")[0]

    assert read.body is None
    assert read.unavailable_reason == "insufficient_privilege"


# --------------------------------------------------------------------------- #
# MySQL / MariaDB — triggers y events                                          #
# --------------------------------------------------------------------------- #
def test_mysql_trigger_trae_tabla_momento_y_evento(mysql_adapter):
    show_create_row = FakeRow(
        {
            "Trigger": "trg_audit",
            "sql_mode": "",
            "SQL Original Statement": "CREATE DEFINER=`a`@`b` TRIGGER `trg_audit` BEFORE INSERT ON `ventas` FOR EACH ROW SET NEW.x = 1",
        }
    )
    conn = FakeConnection(
        [
            ("SHOW CREATE TRIGGER", [show_create_row]),
            ("information_schema.TRIGGERS", [("ventas", "BEFORE", "INSERT")]),
        ]
    )

    read = mysql_adapter.read_definition(conn, "db", "db", "trigger", "trg_audit")[0]

    assert read.kind == "trigger"
    assert read.body is not None and "`a`@`b`" not in read.body
    assert read.trigger_table == "ventas"
    assert read.trigger_timing == "BEFORE"
    assert read.trigger_events == ["INSERT"]
    assert read.security is None


def test_mysql_event_trae_programacion_armada_por_el_gateway_y_estado(mysql_adapter):
    show_create_row = FakeRow(
        {
            "Event": "ev_limpieza",
            "sql_mode": "",
            "time_zone": "SYSTEM",
            "Create Event": "CREATE DEFINER=`a`@`b` EVENT `ev_limpieza` ON SCHEDULE EVERY 1 DAY DO DELETE FROM x",
        }
    )
    conn = FakeConnection(
        [
            ("SHOW CREATE EVENT", [show_create_row]),
            ("information_schema.EVENTS", [("RECURRING", None, 1, "day", "ENABLED")]),
        ]
    )

    read = mysql_adapter.read_definition(conn, "db", "db", "event", "ev_limpieza")[0]

    assert read.kind == "event"
    assert read.body is not None and "`a`@`b`" not in read.body
    assert read.event_schedule == "EVERY 1 DAY"
    assert read.event_status == "ENABLED"


# --------------------------------------------------------------------------- #
# MySQL / MariaDB — rutinas                                                    #
# --------------------------------------------------------------------------- #
def _create_routine_row(kind: str, body: str | None) -> FakeRow:
    return FakeRow({kind.capitalize(): "r", "sql_mode": "", f"Create {kind.capitalize()}": body})


def test_mysql_rutina_sin_routine_kind_prueba_ambos_tipos_y_omite_el_ausente(mysql_adapter):
    conn = FakeConnection(
        [
            (
                "SHOW CREATE PROCEDURE",
                [_create_routine_row("PROCEDURE", "CREATE DEFINER=`a`@`b` PROCEDURE `r`() SELECT 1")],
            ),
            ("SHOW CREATE FUNCTION", driver_error(_MYSQL_UNKNOWN_ROUTINE_CODE)),
            ("information_schema.ROUTINES", [("DEFINER",)]),
        ]
    )

    reads = mysql_adapter.read_definition(conn, "db", "db", "routine", "r")

    assert len(reads) == 1
    assert reads[0].routine_kind == "PROCEDURE"
    assert reads[0].body is not None and "`a`@`b`" not in reads[0].body
    assert reads[0].security == "definer"


def test_mysql_rutina_procedure_y_function_con_el_mismo_nombre_devuelven_dos_lecturas(mysql_adapter):
    conn = FakeConnection(
        [
            ("SHOW CREATE PROCEDURE", [_create_routine_row("PROCEDURE", "CREATE PROCEDURE `r`() SELECT 1")]),
            ("SHOW CREATE FUNCTION", [_create_routine_row("FUNCTION", "CREATE FUNCTION `r`() RETURNS int RETURN 1")]),
            ("information_schema.ROUTINES", [("INVOKER",)]),
        ]
    )

    reads = mysql_adapter.read_definition(conn, "db", "db", "routine", "r")

    assert [read.routine_kind for read in reads] == ["PROCEDURE", "FUNCTION"]
    assert all(read.body for read in reads)


def test_mysql_rutina_con_routine_kind_solo_consulta_ese_tipo(mysql_adapter):
    conn = FakeConnection(
        [
            ("SHOW CREATE FUNCTION", [_create_routine_row("FUNCTION", "CREATE FUNCTION `r`() RETURNS int RETURN 1")]),
            ("information_schema.ROUTINES", [("INVOKER",)]),
        ]
    )

    reads = mysql_adapter.read_definition(conn, "db", "db", "routine", "r", "FUNCTION")

    assert [read.routine_kind for read in reads] == ["FUNCTION"]
    assert all("SHOW CREATE PROCEDURE" not in sql for sql in conn.executed_sql)
    assert reads[0].security == "invoker"


def test_mysql_rutina_con_cuerpo_null_es_sin_cuerpo_con_su_tipo(mysql_adapter):
    # MariaDB sin privilegio: la fila existe pero la columna del cuerpo viene NULL.
    conn = FakeConnection(
        [
            ("SHOW CREATE PROCEDURE", [_create_routine_row("PROCEDURE", None)]),
            ("SHOW CREATE FUNCTION", driver_error(_MYSQL_UNKNOWN_ROUTINE_CODE)),
            ("information_schema.ROUTINES", [("DEFINER",)]),
        ]
    )

    reads = mysql_adapter.read_definition(conn, "db", "db", "routine", "r")

    assert len(reads) == 1
    assert reads[0].routine_kind == "PROCEDURE"
    assert reads[0].body is None
    assert reads[0].unavailable_reason == "insufficient_privilege"


def test_mysql_rutina_que_ningun_tipo_resuelve_devuelve_una_lectura_sin_cuerpo(mysql_adapter):
    conn = FakeConnection(
        [
            ("SHOW CREATE PROCEDURE", driver_error(_MYSQL_UNKNOWN_ROUTINE_CODE)),
            ("SHOW CREATE FUNCTION", driver_error(_MYSQL_PERMISSION_DENIED_CODE)),
        ]
    )

    reads = mysql_adapter.read_definition(conn, "db", "db", "routine", "r")

    assert len(reads) == 1
    assert reads[0].routine_kind is None
    assert reads[0].body is None
    assert reads[0].unavailable_reason == "insufficient_privilege"


# --------------------------------------------------------------------------- #
# Validaciones previas a cualquier SQL                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dangerous_name",
    ["v`; DROP TABLE x; --", "v v", "v'", 'v"', "v\\", "", "v\x00"],
)
def test_mysql_nombre_peligroso_falla_antes_de_ejecutar_sql(mysql_adapter, dangerous_name):
    conn = FakeConnection([])

    with pytest.raises(AppHttpException) as raised:
        mysql_adapter.read_definition(conn, "db", "db", "view", dangerous_name)

    assert raised.value.status_code == 422
    assert conn.executed_sql == []


@pytest.mark.parametrize("dangerous_name", ["v`; DROP TABLE x; --", "v'", ""])
def test_postgres_nombre_peligroso_falla_antes_de_ejecutar_sql(postgres_adapter, dangerous_name):
    conn = FakeConnection([])

    with pytest.raises(AppHttpException):
        postgres_adapter.read_definition(conn, "db", "public", "view", dangerous_name)

    assert conn.executed_sql == []


@pytest.mark.parametrize("adapter_class", [MySQLAdapter, PostgresAdapter])
def test_tipo_de_objeto_invalido_es_422_sin_sql(adapter_class):
    adapter = adapter_class(target=None)
    conn = FakeConnection([])

    with pytest.raises(AppHttpException) as raised:
        adapter.read_definition(conn, "db", "public", "table", "t")

    assert raised.value.status_code == 422
    assert conn.executed_sql == []


@pytest.mark.parametrize("adapter_class", [MySQLAdapter, PostgresAdapter])
def test_routine_kind_invalido_es_422(adapter_class):
    adapter = adapter_class(target=None)
    conn = FakeConnection([])

    with pytest.raises(AppHttpException) as raised:
        adapter.read_definition(conn, "db", "public", "routine", "r", "AGGREGATE")

    assert raised.value.status_code == 422
    assert conn.executed_sql == []


# --------------------------------------------------------------------------- #
# PostgreSQL                                                                   #
# --------------------------------------------------------------------------- #
def test_postgres_vista_entrega_cuerpo_modo_security_y_check_option(postgres_adapter):
    conn = FakeConnection(
        [
            (
                "pg_get_viewdef",
                [("v", " SELECT 1;", ["check_option=local", "security_invoker=true"])],
            )
        ]
    )

    read = postgres_adapter.read_definition(conn, "db", "public", "view", "v_ventas")[0]

    assert read.body == " SELECT 1;"
    assert read.unavailable_reason is None
    assert read.security == "invoker"
    assert read.check_option == "LOCAL"


def test_postgres_vista_sin_security_invoker_corre_como_definer(postgres_adapter):
    conn = FakeConnection([("pg_get_viewdef", [("v", "SELECT 1", None)])])

    read = postgres_adapter.read_definition(conn, "db", "public", "view", "v_ventas")[0]

    assert read.security == "definer"
    assert read.check_option is None


def test_postgres_nombre_viaja_como_parametro_enlazado_y_no_en_el_sql(postgres_adapter):
    conn = FakeConnection([("pg_get_viewdef", [("v", "SELECT 1", None)])])

    postgres_adapter.read_definition(conn, "db", "public", "view", "v_ventas")

    assert "v_ventas" not in conn.executed_sql[0]
    assert conn.executed_params[0] == {"schema": "public", "name": "v_ventas"}


@pytest.mark.parametrize("null_body", [None, ""])
def test_postgres_vista_con_cuerpo_null_o_vacio_es_sin_cuerpo(postgres_adapter, null_body):
    conn = FakeConnection([("pg_get_viewdef", [("v", null_body, None)])])

    read = postgres_adapter.read_definition(conn, "db", "public", "view", "v_ventas")[0]

    assert read.body is None
    assert read.unavailable_reason == "insufficient_privilege"


def test_postgres_vista_inexistente_para_el_catalogo_es_sin_cuerpo(postgres_adapter):
    conn = FakeConnection([("pg_get_viewdef", [])])

    read = postgres_adapter.read_definition(conn, "db", "public", "view", "v_ventas")[0]

    assert read.body is None
    assert read.unavailable_reason == "insufficient_privilege"


def test_postgres_trigger_con_el_mismo_nombre_en_dos_tablas_devuelve_dos_lecturas(postgres_adapter):
    conn = FakeConnection(
        [
            (
                "pg_get_triggerdef",
                [
                    ("ventas", "CREATE TRIGGER trg BEFORE INSERT OR UPDATE ON public.ventas FOR EACH ROW EXECUTE FUNCTION f()"),
                    ("pagos", "CREATE TRIGGER trg AFTER DELETE ON public.pagos FOR EACH ROW EXECUTE FUNCTION f()"),
                ],
            )
        ]
    )

    reads = postgres_adapter.read_definition(conn, "db", "public", "trigger", "trg")

    assert [read.trigger_table for read in reads] == ["ventas", "pagos"]
    assert (reads[0].trigger_timing, reads[0].trigger_events) == ("BEFORE", ["INSERT", "UPDATE"])
    assert (reads[1].trigger_timing, reads[1].trigger_events) == ("AFTER", ["DELETE"])
    assert all(read.body for read in reads)


def test_postgres_sobrecargas_devuelven_una_lectura_por_firma(postgres_adapter):
    conn = FakeConnection(
        [
            (
                "pg_get_functiondef",
                [
                    ("f", "integer", "CREATE OR REPLACE FUNCTION public.calc(integer) ...", False),
                    ("f", "integer, text", "CREATE OR REPLACE FUNCTION public.calc(integer, text) ...", True),
                ],
            )
        ]
    )

    reads = postgres_adapter.read_definition(conn, "db", "public", "routine", "calc")

    assert [read.identity_arguments for read in reads] == ["integer", "integer, text"]
    assert [read.security for read in reads] == ["invoker", "definer"]
    assert all(read.routine_kind == "FUNCTION" for read in reads)


def test_postgres_routine_kind_filtra_las_sobrecargas(postgres_adapter):
    conn = FakeConnection(
        [
            (
                "pg_get_functiondef",
                [
                    ("f", "integer", "CREATE FUNCTION a ...", False),
                    ("p", "", "CREATE PROCEDURE a ...", False),
                ],
            )
        ]
    )

    reads = postgres_adapter.read_definition(conn, "db", "public", "routine", "a", "PROCEDURE")

    assert [read.routine_kind for read in reads] == ["PROCEDURE"]


def test_postgres_rutina_sin_filas_es_una_lectura_sin_cuerpo(postgres_adapter):
    conn = FakeConnection([("pg_get_functiondef", [])])

    reads = postgres_adapter.read_definition(conn, "db", "public", "routine", "calc")

    assert len(reads) == 1
    assert reads[0].body is None
    assert reads[0].unavailable_reason == "insufficient_privilege"


def test_postgres_event_es_engine_unsupported_sin_tocar_el_motor(postgres_adapter):
    conn = FakeConnection([])

    reads = postgres_adapter.read_definition(conn, "db", "public", "event", "ev")

    assert len(reads) == 1
    assert reads[0].unavailable_reason == "engine_unsupported"
    assert reads[0].body is None
    assert conn.executed_sql == []


# --------------------------------------------------------------------------- #
# Base y versión                                                               #
# --------------------------------------------------------------------------- #
def test_base_read_definition_por_defecto_es_engine_unsupported(mysql_adapter):
    conn = FakeConnection([])

    reads = ServerAdapter.read_definition(mysql_adapter, conn, "db", "db", "view", "v")

    assert reads == [DefinitionRead(kind="view", name="v", unavailable_reason="engine_unsupported")]
    assert conn.executed_sql == []


def test_read_server_version_devuelve_la_cadena_del_motor(mysql_adapter):
    conn = FakeConnection([("VERSION()", [("10.11.6-MariaDB",)])])

    assert mysql_adapter.read_server_version(conn) == "10.11.6-MariaDB"


def test_read_server_version_sin_fila_o_con_error_es_none(mysql_adapter):
    assert mysql_adapter.read_server_version(FakeConnection([("VERSION()", [])])) is None
    denied = FakeConnection([("VERSION()", driver_error(_MYSQL_PERMISSION_DENIED_CODE))])
    assert mysql_adapter.read_server_version(denied) is None


# --------------------------------------------------------------------------- #
# Fachada de solo lectura                                                      #
# --------------------------------------------------------------------------- #
class FakeSession:
    """Lo mínimo de ``ExportSession`` que usa la fachada."""

    def __init__(self, conn):
        self.conn = conn
        self.deadline_checks = 0

    def check_deadline(self) -> None:
        self.deadline_checks += 1


def test_facade_definition_delega_en_el_adapter_con_sesion_base_y_schema():
    expected_reads = [DefinitionRead(kind="view", name="v", body="SELECT 1")]
    received_calls: list[tuple] = []

    def fake_read_definition(conn, database, schema, kind, name, routine_kind):
        received_calls.append((conn, database, schema, kind, name, routine_kind))
        return expected_reads

    adapter = SimpleNamespace(
        _inspect_schema=lambda database: "inventory",
        read_definition=fake_read_definition,
    )
    connection_marker = object()
    session = FakeSession(conn=connection_marker)
    facade = ReadonlyIntrospector(adapter, session, "inventory")

    reads = facade.definition("view", "v")

    assert reads == expected_reads
    assert received_calls == [(connection_marker, "inventory", "inventory", "view", "v", None)]
    assert session.deadline_checks == 1


def test_facade_definition_pasa_routine_kind():
    received_routine_kinds: list = []
    adapter = SimpleNamespace(
        _inspect_schema=lambda database: "inventory",
        read_definition=lambda conn, database, schema, kind, name, routine_kind: (
            received_routine_kinds.append(routine_kind) or []
        ),
    )
    facade = ReadonlyIntrospector(adapter, FakeSession(conn=object()), "inventory")

    facade.definition("routine", "r", "PROCEDURE")

    assert received_routine_kinds == ["PROCEDURE"]


def test_facade_server_version_delega_en_el_adapter():
    connection_marker = object()
    adapter = SimpleNamespace(
        read_server_version=lambda conn: "8.0.35" if conn is connection_marker else None
    )
    session = FakeSession(conn=connection_marker)
    facade = ReadonlyIntrospector(adapter, session, "inventory")

    assert facade.server_version() == "8.0.35"
    assert session.deadline_checks == 1
