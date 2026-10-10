"""
Presupuesto de bytes de la respuesta de una tool, medido en UN solo lugar.

POR QUÉ ESTÁ APARTE DEL DESPACHADOR
-----------------------------------
El despachador corta con ``mcp.result_too_large`` lo que supera el tope, sin saber cuánto de eso es
SQL. Una tool que devuelve un cuerpo que no se puede recortar (``get_blueprint_migration``) necesita
medir ANTES de responder, para avisar con un código propio y con los tres números que el agente
necesita (``sql_bytes``, ``response_bytes``, ``max_response_bytes``). Si cada lado midiera con su
propia fórmula, una respuesta que la tool da por buena podría ser rechazada por el despachador, o a
la inversa. Las dos medidas salen de ``serialized_result_bytes``.

El despachador importa ``MAX_RESULT_BYTES`` desde acá y lo reexporta: ``dispatch.MAX_RESULT_BYTES``
sigue siendo el nombre con el que lo leen los tests y el assert de ``MCP_DATA_MAX_RESULT_BYTES``.
"""

import json

from app.mcp import jsonrpc

#: Tope de bytes de la respuesta de una tool, **después** de serializar. El tope de objetos
#: (antes de consultar) es responsabilidad de cada tool; éste es la red de abajo, para el caso
#: en que N objetos chicos sumen una respuesta enorme.
#:
#: Se corta con un ERROR y **nunca truncando**: un JSON truncado que el agente parsea a medias
#: es peor que un fallo, porque le hace creer que el esquema es más chico de lo que es.
MAX_RESULT_BYTES = 512 * 1024


def serialized_result_bytes(tool_result: dict) -> int:
    """
    Bytes UTF-8 que ocupa la respuesta de una tool que salió bien, tal como la mide el despachador:
    el ``result`` envuelto por ``jsonrpc.tool_result_payload`` (el JSON va dos veces, en el bloque de
    texto y en ``structuredContent``) y serializado con ``ensure_ascii=False``.
    """
    payload_tool = jsonrpc.tool_result_payload(tool_result)
    serialized = json.dumps(payload_tool, ensure_ascii=False, default=str)
    return len(serialized.encode("utf-8"))
