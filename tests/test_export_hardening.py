"""
Tests de regresión de los endurecimientos de la revisión de seguridad del módulo 10.

Acá viven solo los que son PUROS (sin cliente HTTP ni BD): el ciclo de vida de la sesión de
lectura y la decisión de "un solo uso" de la descarga. Los que necesitan la API están en
``tests/test_api_database_exports.py``.
"""

from __future__ import annotations

import pytest

from app.routes.v1.database_exports import _range_covers_whole_file
from app.services.db_admin.export_session import ExportSession

# --------------------------------------------------------------------------- #
# R3 — ``stream_results`` no puede quedar pegado a la conexión del job         #
# --------------------------------------------------------------------------- #
# ``Connection.execution_options()`` muta la conexión IN-PLACE (a diferencia de
# ``Engine.execution_options()``, que devuelve una copia). Esta conexión vive el job entero,
# así que fijarle ``stream_results`` ahí se lo dejaba pegado al re-snapshot final de drift y
# a ``counter_value``, que pasaban a ejecutarse por un cursor con nombre — el modo de fallo
# que documenta ``query_runner``: en psycopg un cursor con nombre se compone como
# ``DECLARE … CURSOR FOR <sentencia>``, gramática que solo acepta consultas.


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def __iter__(self):
        return iter(self._rows)

    def close(self):
        self.closed = True

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeConn:
    """Conexión doble que registra si le fijaron opciones A ELLA o a la sentencia."""

    def __init__(self):
        self.connection_options: list[dict] = []
        self.executed: list = []

    def execution_options(self, **kw):
        self.connection_options.append(kw)
        return self

    def execute(self, statement, params=None):
        self.executed.append(statement)
        return _FakeResult([(1,), (2,)])


def _session(conn) -> ExportSession:
    return ExportSession(
        conn=conn, engine="postgresql", database="tienda", deadline_monotonic=None
    )


def test_iter_rows_no_muta_la_conexion_del_job():
    conn = _FakeConn()
    assert list(_session(conn).iter_rows("SELECT 1")) == [(1,), (2,)]
    assert conn.connection_options == []


def test_iter_rows_pone_las_opciones_en_la_sentencia():
    conn = _FakeConn()
    list(_session(conn).iter_rows("SELECT 1", batch_rows=250))
    opts = conn.executed[0].get_execution_options()
    assert opts["stream_results"] is True
    assert opts["yield_per"] == 250


def test_scalar_no_hereda_el_cursor_de_streaming():
    """
    Es el síntoma concreto: el re-snapshot de drift y ``counter_value`` corren DESPUÉS de
    volcar filas, sobre la misma conexión.
    """
    conn = _FakeConn()
    session = _session(conn)
    list(session.iter_rows("SELECT 1"))
    session.scalar("SELECT max(id) FROM t")
    assert conn.connection_options == []
    assert conn.executed[-1].get_execution_options() == {}


# --------------------------------------------------------------------------- #
# R1 — un ``Range`` no puede anular el "un solo uso"                           #
# --------------------------------------------------------------------------- #
# Antes bastaba la PRESENCIA de la cabecera para no consumir el artefacto: un
# ``Range: bytes=0-`` bajaba el archivo entero y lo dejaba disponible para la próxima.

SIZE = 100
ETAG = "abc123"


@pytest.mark.parametrize(
    "rng",
    [None, "bytes=0-", "bytes=0-99", "bytes=0-500", "bytes=-100", "bytes=-999"],
)
def test_un_rango_que_cubre_todo_el_archivo_consume(rng):
    assert _range_covers_whole_file(rng, None, SIZE, ETAG) is True


@pytest.mark.parametrize(
    "rng",
    [
        "bytes=0-9",  # reanudación legítima
        "bytes=50-99",
        "bytes=-10",
        "bytes=0-9,20-29",  # multi-rango: no se interpreta ⇒ parcial
        "items=0-",  # unidad desconocida
        "bytes=abc",
        "bytes=",
    ],
)
def test_un_rango_parcial_o_ininteligible_no_consume(rng):
    """Fail-closed hacia NO borrar: cortarle la reanudación a un cliente legítimo obliga
    a rehacer la exportación entera."""
    assert _range_covers_whole_file(rng, None, SIZE, ETAG) is False


def test_un_if_range_que_no_valida_significa_respuesta_completa():
    """Con ``If-Range`` desactualizado el servidor ignora el rango y manda todo el cuerpo."""
    assert _range_covers_whole_file("bytes=0-9", '"otro-etag"', SIZE, ETAG) is True
    assert _range_covers_whole_file("bytes=0-9", f'"{ETAG}"', SIZE, ETAG) is False
