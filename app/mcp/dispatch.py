"""
Despacho JSON-RPC: ``initialize`` | ``tools/list`` | ``tools/call``.

Es "la ruta" del paquete. Tres responsabilidades y ninguna más:

1. **Traducir ``AppHttpException`` a error de TOOL**, no de protocolo. Un "no tenés acceso a esa
   base" tiene que llegar como contenido para que el agente lo entienda y no como una falla del
   servidor que reintente en loop.
2. **Auditar cada invocación**, con el token y nunca el secreto.
3. **Aplicar el presupuesto de bytes** después de serializar.

LO QUE NO HACE: autenticar. Eso pasa antes, en la dependencia — así el kill switch y la
verificación del bearer quedan en un choke point único.
"""

import json
from typing import Any

from app.core.actor import Actor
from app.exceptions import AppHttpException
from app.mcp import jsonrpc
from app.mcp.context import ToolContext
from app.mcp.registry import BY_NAME, TOOLS
from app.services import audit

#: Versión del protocolo que este servidor habla. Se declara y no se refleja lo que mande el
#: cliente: reflejarlo es cómo un servidor "soporta" una versión que no implementó.
PROTOCOL_VERSION = "2024-11-05"

#: Tope de bytes de la respuesta de una tool, **después** de serializar. El tope de objetos
#: (antes de consultar) es responsabilidad de cada tool; éste es la red de abajo, para el caso
#: en que N objetos chicos sumen una respuesta enorme.
#:
#: Se corta con un ERROR y **nunca truncando**: un JSON truncado que el agente parsea a medias
#: es peor que un fallo, porque le hace creer que el esquema es más chico de lo que es.
MAX_RESULT_BYTES = 512 * 1024


def _server_info() -> dict:
    from app.core.environments import APP_NAME

    return {"name": f"{APP_NAME} MCP", "version": "1"}


def _tool_descriptor(spec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": spec.input_schema,
    }


def handle(payload: Any, actor: Actor) -> dict:
    """
    Procesa un mensaje JSON-RPC. Devuelve el dict de respuesta.

    Nunca levanta: todo termina en una respuesta JSON-RPC bien formada. Un 500 en este canal es
    un cliente MCP que se cuelga sin decir por qué.
    """
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "Mensaje JSON-RPC 2.0 inválido.")

    rid = payload.get("id")
    metodo = payload.get("method")

    if metodo == "initialize":
        return jsonrpc.ok(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                # Solo `tools`. No se declara `resources` ni `prompts` porque no están
                # implementados, y declarar una capability vacía hace que el cliente la
                # consulte y reciba un método desconocido.
                "capabilities": {"tools": {}},
                "serverInfo": _server_info(),
            },
        )

    if metodo == "tools/list":
        return jsonrpc.ok(rid, {"tools": [_tool_descriptor(t) for t in TOOLS]})

    if metodo != "tools/call":
        return jsonrpc.error(rid, jsonrpc.METHOD_NOT_FOUND, f"Método desconocido: {metodo!r}.")

    params = payload.get("params") or {}
    nombre = params.get("name")
    spec = BY_NAME.get(nombre)
    if spec is None:
        # Método desconocido es PROTOCOLO; una tool desconocida también, porque el agente pidió
        # algo que `tools/list` no publica: es un bug del cliente, no una negación de acceso.
        return jsonrpc.error(rid, jsonrpc.METHOD_NOT_FOUND, f"Tool desconocida: {nombre!r}.")

    argumentos = params.get("arguments") or {}
    if not isinstance(argumentos, dict):
        return jsonrpc.error(rid, jsonrpc.INVALID_PARAMS, "'arguments' tiene que ser un objeto.")

    try:
        resultado = spec.handler(ToolContext(actor=actor), argumentos)
    except AppHttpException as exc:
        # LA traducción que este módulo existe para hacer: la negación del gate viaja como
        # contenido, con su código del vocabulario cerrado.
        codigo = (exc.public_context or {}).get("code") or "mcp.error"
        _audit(spec.name, actor, ok=False, detail=f"denegado: {codigo}")
        return jsonrpc.tool_error(rid, codigo, exc.message)
    except Exception:  # noqa: BLE001 — ver el comentario
        # Cualquier otra cosa NO puede salir con detalle: un traceback o un `str(exc)` del
        # motor por este canal termina en el contexto de un modelo y de ahí en la pantalla de
        # cualquiera. El detalle va al log con el Request ID, que es la regla del repo.
        from app.core.logger import get_logger

        get_logger(__name__).exception("Fallo no esperado en la tool %s", spec.name)
        _audit(spec.name, actor, ok=False, detail="fallo interno")
        return jsonrpc.error(rid, jsonrpc.INTERNAL_ERROR, "Fallo interno del servidor MCP.")

    serializado = json.dumps(resultado, ensure_ascii=False, default=str)
    if len(serializado.encode("utf-8")) > MAX_RESULT_BYTES:
        _audit(spec.name, actor, ok=False, detail="excedió el presupuesto de bytes")
        return jsonrpc.tool_error(
            rid,
            "mcp.result_too_large",
            (
                "El resultado supera el tope de la respuesta. Acotá la consulta: no se trunca "
                "a propósito, porque un JSON cortado haría creer que el esquema es más chico."
            ),
        )

    _audit(spec.name, actor, ok=True, detail=f"{len(serializado)} bytes")
    return jsonrpc.tool_result(rid, resultado)


def _audit(tool: str, actor: Actor, *, ok: bool, detail: str) -> None:
    """
    Una fila por invocación. **El secreto del token nunca se audita** — solo su ``token_id``,
    que es la parte pública, y el id de la fila.

    Best-effort: un fallo al auditar no puede tirar abajo la respuesta de una tool de solo
    lectura. Lo fail-closed está reservado a lo que divulga datos, y esto no lo hace todavía.
    """
    audit.record(
        f"mcp.{tool}",
        status="success" if ok else "failure",
        admin=actor,
        target_type="api_token",
        target_id=actor.id,
        touched_engine=False,
        detail=f"token={actor.token_id} proyecto={actor.project_id} {detail}",
    )
