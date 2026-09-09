"""
JSON-RPC 2.0: la capa de PROTOCOLO, y solo eso.

LA DISTINCIÓN QUE ESTE MÓDULO EXISTE PARA MANTENER
--------------------------------------------------
Un error de **protocolo** (JSON inválido, método desconocido, parámetros mal formados) va en el
campo ``error`` de la respuesta JSON-RPC. Un error de **tool** (no tenés acceso a esa base, la
base no existe) va en el ``result`` con ``isError: true``.

Mezclarlos rompe el cliente de dos maneras distintas: un error de tool en ``error`` hace que el
agente crea que el servidor está roto y reintente; y un error de protocolo en ``result`` hace que
lo interprete como contenido y se lo muestre al usuario como si fuera una respuesta.
"""

from typing import Any

#: Códigos estándar de JSON-RPC 2.0. Se declaran en vez de usarse como literales para que el
#: valor y su significado vivan juntos.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def ok(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict:
    """
    Error de PROTOCOLO.

    El ``message`` es corto y no lleva detalle del sistema: un cliente MCP lo muestra tal cual,
    y este canal habla con un agente que después le repite todo a una persona.
    """
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_result(request_id: Any, payload: dict) -> dict:
    """
    Resultado de una tool que salió bien.

    El contenido va como un bloque de texto con el JSON serializado, que es la forma que el
    protocolo define y que todo cliente sabe renderizar. El objeto estructurado va **además**
    en ``structuredContent``, para los clientes que lo soportan.
    """
    import json

    return ok(
        request_id,
        {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}
            ],
            "structuredContent": payload,
            "isError": False,
        },
    )


def tool_error(request_id: Any, code: str, message: str) -> dict:
    """
    Error de TOOL: va en ``result`` con ``isError: true``, no en ``error``.

    Lleva el ``code`` del vocabulario cerrado además del mensaje, porque el agente puede
    reaccionar a un código y no a una frase — y porque el mensaje está en español para el
    operador que lo va a leer.
    """
    import json

    payload = {"error": {"code": code, "message": message}}
    return ok(
        request_id,
        {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
            ],
            "structuredContent": payload,
            "isError": True,
        },
    )
