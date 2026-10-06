"""
``get_table_stats``: estadísticas de ALMACENAMIENTO de tablas pedidas por nombre.

LA TOOL NO ACEPTA SQL Y NO LEE FILAS
------------------------------------
Recibe ``{database_id, tables: [nombre]}``: identificadores, nunca texto SQL. Existe para que el
agente pueda saber qué tan grande es una tabla (bytes de datos y de índices, motor, collation,
fechas) SIN abrir ``information_schema`` a SQL libre: ``information_schema`` sigue bloqueado como
esquema de sistema en el validador de ``run_select``, y esta tool es la vía acotada y de solo
lectura hacia el mismo dato. La consulta la arma el adapter con parámetros enlazados, a partir de
nombres que ya están en el índice de la base.

DOS NIVELES DE SALIDA, SEGÚN EL SCOPE DEL TOKEN
-----------------------------------------------
Vive bajo ``databases.read`` (estructura). ``row_estimate`` y ``auto_increment`` aproximan lo que da
``count_rows`` (scope ``data.read``), así que SOLO salen si el token además tiene ``data.read`` con su
kill switch encendido. Para los demás NO existen como claves (``TableStatsOut``) y la respuesta lo
dice en ``row_estimates_omitted_reason``: un ``null`` sería ambiguo con "el motor no lo sabe". La
decisión la toma ``target_resolution.read_table_stats`` ANTES de leer el motor, y el adapter ni
siquiera selecciona esas columnas cuando no corresponde.

EL MAPEADOR ES LA LISTA BLANCA
------------------------------
``_map_table_stats`` arma el modelo de salida campo por campo desde ``TableStatsRead``. Los nombres de
tabla son texto de terceros: pasan por ``clean`` como en el resto del paquete.
"""

from __future__ import annotations

from app.core.environments import MCP_MAX_OBJECTS_PER_CALL
from app.exceptions import AppHttpException
from app.mcp.context import ToolContext
from app.mcp.tools._envelope import Tracker, clean, iso
from app.mcp.tools.catalog import _envelope, _warnings
from app.mcp.tools.definitions import _SessionFacts
from app.schemas import mcp as out
from app.services import mcp_catalog as codes

#: Tope de tablas por llamada. Es el mismo tope que ``get_schema`` (una tabla pedida cuesta una
#: consulta de catálogo en el peor caso) y se evalúa ANTES de conectar. ``read_table_stats`` lo
#: vuelve a exigir con el valor de ``environments`` por si otro llamador salta este handler. Es un
#: nombre de módulo para que el schema publicado y el handler lean el mismo valor.
MAX_TABLES_PER_CALL = MCP_MAX_OBJECTS_PER_CALL
_NAME_MAX = 128


def _invalid(message: str) -> AppHttpException:
    return AppHttpException(
        message=message, status_code=422, public_context={"code": codes.CODE_INVALID_ARGUMENT}
    )


def _database_id(params: dict) -> int:
    database_id = params.get("database_id")
    if not isinstance(database_id, int) or isinstance(database_id, bool):
        raise _invalid("'database_id' tiene que ser un entero (el id que devuelve list_databases).")
    return database_id


def _requested_tables(params: dict) -> list[str]:
    """
    Valida ``tables`` ANTES de abrir ninguna conexión. El dispatcher solo valida las claves de primer
    nivel; el tipo de cada elemento y el tope se hacen cumplir acá.

    NO se rechazan nombres por sus caracteres: uno con comilla, punto y coma o prefijo de otra base
    es un nombre que no está en el índice y vuelve en ``missing``, sin que se emita SQL con ese
    texto. Se colapsan los duplicados conservando el orden del pedido.
    """
    raw_tables = params.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise _invalid("'tables' es obligatorio: una lista de nombres de tabla (no hay 'dame todo').")
    if len(raw_tables) > MAX_TABLES_PER_CALL:
        raise AppHttpException(
            message=(
                f"Se pidieron {len(raw_tables)} tablas y el tope por llamada es "
                f"{MAX_TABLES_PER_CALL}. Partí el pedido en lotes."
            ),
            status_code=413,
            public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
        )
    requested: list[str] = []
    for table_name in raw_tables:
        if not isinstance(table_name, str) or not table_name or len(table_name) > _NAME_MAX:
            raise _invalid(f"Cada tabla es una cadena de 1 a {_NAME_MAX} caracteres.")
        if table_name not in requested:
            requested.append(table_name)
    return requested


def _map_table_stats(read, *, with_estimates: bool):
    """
    ``TableStatsRead`` -> modelo de salida, campo por campo. Con ``with_estimates=False`` se
    construye ``TableStatsOut``, que no tiene ``row_estimate`` ni ``auto_increment``: aunque el
    adapter hubiera devuelto valores, no tienen dónde caer.
    """
    common_fields = {
        "name": clean(read.table),
        "engine": clean(read.engine),
        "collation": clean(read.collation),
        "data_bytes": read.data_bytes,
        "index_bytes": read.index_bytes,
        "created_at": iso(read.created_at),
        "updated_at": iso(read.updated_at),
    }
    if with_estimates:
        return out.TableStatsWithEstimatesOut(
            **common_fields, row_estimate=read.row_estimate, auto_increment=read.auto_increment
        )
    return out.TableStatsOut(**common_fields)


def get_table_stats(ctx: ToolContext, params: dict) -> dict:
    """
    Estadísticas de almacenamiento de las tablas pedidas, con ``missing`` explícito.

    Lo que no es una tabla del índice de la base vuelve en ``missing`` y NO se consulta. Nunca hay un
    éxito vacío: o hay tablas, o hay ``missing``, o la llamada falló con su código.
    """
    database_id = _database_id(params)
    requested = _requested_tables(params)

    batch = ctx.get_table_stats(database_id, requested)

    tracker = Tracker()
    mapped = [
        _map_table_stats(read, with_estimates=batch.row_estimates_included)
        for read in batch.results
    ]
    data = out.TableStatsListOut(
        tables=mapped,
        missing=[clean(name) for name in batch.missing],
        row_estimates_included=batch.row_estimates_included,
        row_estimates_omitted_reason=(
            None if batch.row_estimates_included else "requires_data_read_scope"
        ),
    )
    warnings = _warnings(
        batch.database,
        _SessionFacts(
            consistent_structure=batch.consistent_structure, warnings=batch.session_warnings
        ),
        bodies_requested=False,
    )
    return _envelope(data, resuelta=batch.database, tracker=tracker, warnings=warnings)
