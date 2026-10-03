"""
Tools que reciben SQL de un agente. Hoy una sola: ``draft_query``, que **no ejecuta nada**.

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
