"""
Tools que reciben SQL de un agente (``draft_query``, que **no ejecuta nada**) y las tres lecturas de
DATOS parametrizadas (``sample_rows``, ``distinct_values``, ``count_rows``), que ejecutan sin recibir
SQL.

``draft_query`` existe para que un agente pueda redactar una consulta (o una escritura que un
humano va a revisar) y saber, antes de molestar a nadie, qué clase de sentencia es y por qué no
sería una lectura aceptable. La respuesta es TEXTO: ``{classification, reasons, warnings,
query_text, touches_engine}`` con ``touches_engine`` siempre en ``false``. No abre una conexión al
motor, ni siquiera para una lectura: ejecutar es otra tool, con otro scope y otras garantías.

POR QUÉ ACEPTAR SQL DEL AGENTE NO ROMPE EL INVARIANTE DEL PAQUETE
-----------------------------------------------------------------
El invariante es "el MCP nunca EJECUTA SQL del agente", no "nunca lo lee". Lo que importa es que
ningún camino llegue al motor con ese texto, y eso lo sostienen dos cosas independientes: este
handler no tiene a dónde mandarlo (``ctx.draft_query`` no entrega ni credencial ni façade) y el
guard de importaciones (``tests/test_mcp_import_guard.py``) impide que este paquete alcance la capa
de motor. El validador vive en ``app/services/db_admin/agent_sql_policy.py`` y se invoca por la
puerta de siempre: tool -> ``ToolContext`` -> ``target_resolution`` -> servicio.

Todo input devuelve un sobre, también el basura, el vacío y el enorme: un agente que recibiera un
error de protocolo por un SQL mal escrito reintentaría en loop. Solo un ``database_id`` o un
``sql`` que no sean del tipo declarado se rechazan como argumento inválido.
"""

from app.exceptions import AppHttpException
from app.mcp.context import ToolContext
from app.services import mcp_catalog as codes


def _malformed(message: str) -> AppHttpException:
    return AppHttpException(
        message=message, status_code=422, public_context={"code": codes.REASON_MALFORMED_REQUEST}
    )


def draft_query(ctx: ToolContext, params: dict) -> dict:
    """
    Clasifica un texto SQL sin ejecutarlo. Ver el docstring del módulo.

    El ``database_id`` fija el motor y la base contra la que se evalúan los nombres calificados; el
    agente no puede nombrar otra. El resto (tamaño, comentarios, varias sentencias, funciones) lo
    decide el validador y sale como códigos de razón, nunca como excepción.
    """
    database_id = params.get("database_id")
    if not isinstance(database_id, int) or isinstance(database_id, bool):
        raise _malformed(
            "'database_id' tiene que ser un entero (el id que devuelve list_databases)."
        )
    sql = params.get("sql")
    if not isinstance(sql, str):
        raise _malformed("'sql' tiene que ser una cadena de texto.")
    return ctx.draft_query(database_id, sql)


# --------------------------------------------------------------------------- #
# Lecturas de datos parametrizadas                                             #
# --------------------------------------------------------------------------- #
#
# RIESGO ACEPTADO, Y SU ÚNICA CONTENCIÓN (plan 12 §6.4): las filas que estas tools devuelven son texto
# de TERCEROS y llegan al contexto de un modelo, o sea que pueden contener una inyección de prompt
# ("ignorá lo anterior y…"). Lo que se hace: filas como arreglos dentro de ``data.rows``, marcadas en
# ``untrusted_fields``, con ``notice`` al frente, caracteres de control fuera y celdas acotadas. Es
# una mitigación de eficacia desconocida. El control REAL es que ninguna tool escribe: una inyección
# exitosa no consigue ninguna acción, solo texto. El día que una tool de datos pueda mutar, este
# análisis se reabre antes de mergear, no después.
#
# Reciben identificadores (tabla, columnas), NUNCA SQL: el gateway arma la sentencia.


def _database_id(params: dict) -> int:
    database_id = params.get("database_id")
    if not isinstance(database_id, int) or isinstance(database_id, bool):
        raise _malformed(
            "'database_id' tiene que ser un entero (el id que devuelve list_databases)."
        )
    return database_id


def _name(params: dict, key: str) -> str:
    valor = params.get(key)
    if not isinstance(valor, str) or not valor:
        raise _malformed(f"'{key}' tiene que ser una cadena de texto no vacía.")
    return valor


def _limit(params: dict) -> int | None:
    limite = params.get("limit")
    if limite is None:
        return None
    if isinstance(limite, bool) or not isinstance(limite, int) or limite < 1:
        raise _malformed("'limit' tiene que ser un entero positivo.")
    return limite


def sample_rows(ctx: ToolContext, params: dict) -> dict:
    """Filas de una tabla. ``limit`` ausente = 100; por encima del máximo se recorta y se avisa."""
    database_id = _database_id(params)
    table = _name(params, "table")
    columns = params.get("columns")
    if columns is not None:
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(c, str) or not c for c in columns)
        ):
            raise _malformed("'columns' tiene que ser una lista no vacía de nombres de columna.")
    return ctx.sample_rows(database_id, table, columns, _limit(params))


def distinct_values(ctx: ToolContext, params: dict) -> dict:
    """Valores distintos de una columna, ordenados."""
    database_id = _database_id(params)
    return ctx.distinct_values(
        database_id, _name(params, "table"), _name(params, "column"), _limit(params)
    )


def count_rows(ctx: ToolContext, params: dict) -> dict:
    """Cantidad de filas de una tabla (acotada por el timeout del motor)."""
    database_id = _database_id(params)
    return ctx.count_rows(database_id, _name(params, "table"))
