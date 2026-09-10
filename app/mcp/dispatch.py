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
from app.mcp import jsonrpc, protocol
from app.mcp.context import ToolContext
from app.mcp.registry import BY_NAME, TOOLS
from app.services import audit

#: Tope de bytes de la respuesta de una tool, **después** de serializar. El tope de objetos
#: (antes de consultar) es responsabilidad de cada tool; éste es la red de abajo, para el caso
#: en que N objetos chicos sumen una respuesta enorme.
#:
#: Se corta con un ERROR y **nunca truncando**: un JSON truncado que el agente parsea a medias
#: es peor que un fallo, porque le hace creer que el esquema es más chico de lo que es.
MAX_RESULT_BYTES = 512 * 1024


def server_info() -> dict:
    from app.core.environments import APP_NAME

    return {"name": f"{APP_NAME} MCP", "version": "1"}


def _tool_descriptor(spec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": spec.input_schema,
    }


def _ok(rid: Any, result: dict) -> protocol.Respuesta:
    """200 con el ``result`` completado (``resultType`` + ``_meta.serverInfo``)."""
    return protocol.Respuesta(
        200, jsonrpc.ok(rid, protocol.envolver_result(result, server_info=server_info()))
    )


def handle(payload: Any, actor: Actor, headers: dict[str, str]) -> protocol.Respuesta:
    """
    Procesa un mensaje JSON-RPC y devuelve **status HTTP y cuerpo**.

    El status es parte del contrato en esta revisión, no un detalle: una notificación es ``202``
    sin cuerpo, un método desconocido es ``404``, una validación de headers es ``400``. Un
    servidor que contestara ``200`` a todo sería inválido incluso con el JSON correcto.

    Nunca levanta: todo termina en una respuesta bien formada. Un 500 en este canal es un
    cliente MCP que se cuelga sin decir por qué.
    """
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return protocol.Respuesta(
            400,
            jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "Mensaje JSON-RPC 2.0 inválido."),
        )

    stateless = protocol.es_stateless(headers)

    # UNA NOTIFICACIÓN NO SE RESPONDE. JSON-RPC define una notificación como un request SIN
    # `id` y prohíbe contestarla; sobre HTTP, la spec pide **202 sin cuerpo**.
    #
    # Es la regla que este servidor violaba de la forma más visible: todo cliente de la era del
    # handshake manda `notifications/initialized` inmediatamente después de `initialize`, y
    # recibía un `-32601` con `id: null`. Un cliente estricto lo lee como servidor roto en el
    # primer intercambio.
    #
    # Vale para TODA notificación, conocida o no: las que vengan en versiones futuras tienen que
    # poder ignorarse en silencio.
    if "id" not in payload:
        return protocol.Respuesta(202)

    rid = payload.get("id")
    # `id` nulo está prohibido en MCP, a diferencia de JSON-RPC pelado.
    if rid is None:
        return protocol.Respuesta(
            400, jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "El 'id' no puede ser nulo.")
        )

    metodo = payload.get("method")

    if stateless:
        rechazo = protocol.validar(payload, headers)
        if rechazo is not None:
            return rechazo

    # `initialize` y `notifications/initialized` NO existen en la era stateless. Aceptarlos ahí
    # sería anunciar un handshake que el protocolo retiró, y un cliente moderno que por error lo
    # llame tiene que enterarse.
    if metodo == "initialize":
        if stateless:
            return protocol.Respuesta(
                404,
                jsonrpc.error(
                    rid,
                    jsonrpc.METHOD_NOT_FOUND,
                    (
                        "Esta revisión del protocolo no tiene handshake: el contexto viaja en "
                        "_meta de cada request."
                    ),
                ),
            )
        pedida = (payload.get("params") or {}).get("protocolVersion")
        acordada = pedida if pedida in protocol.SUPPORTED_VERSIONS else protocol.LATEST_VERSION
        return _ok(
            rid,
            {
                "protocolVersion": acordada,
                # Solo `tools`. No se declara `resources` ni `prompts` porque no están
                # implementados, y declarar una capability vacía hace que el cliente la
                # consulte y reciba un método desconocido.
                "capabilities": {"tools": {}},
                "serverInfo": server_info(),
            },
        )

    if metodo == "ping":
        # Utilidad del protocolo que cualquiera de los dos lados puede mandar y que los clientes
        # usan como keepalive. Sin implementarla, un `-32601` le dice al cliente que la conexión
        # está rota. Responde un result VACÍO, que es lo que la spec define.
        return _ok(rid, {})

    if metodo == "tools/list":
        return _ok(rid, {"tools": [_tool_descriptor(t) for t in TOOLS]})

    if metodo != "tools/call":
        # 404 y no 200: es lo que la spec pide para un método que el servidor no implementa, y
        # es lo que le permite a un cliente moderno distinguir este caso de un 404 de un
        # servidor legado que no hospeda el endpoint — el cuerpo lleva un error reconocible.
        return protocol.Respuesta(
            404,
            jsonrpc.error(rid, jsonrpc.METHOD_NOT_FOUND, f"Método desconocido: {metodo!r}."),
        )

    params = payload.get("params")
    # `isinstance` y no `or {}`: un `params` truthy que no sea dict —una cadena, un número, una
    # lista— pasaba el `or` y reventaba en el `.get()` de la línea siguiente con un 500. El
    # docstring de esta función promete "nunca levanta" y no era cierto: `params: "x"` daba
    # `AttributeError` y, con `APP_ENV=development`, el handler genérico devolvía archivo,
    # función, línea Y la línea de código al agente — o sea al contexto de un modelo.
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return protocol.Respuesta(
            200, jsonrpc.error(rid, jsonrpc.INVALID_PARAMS, "'params' tiene que ser un objeto.")
        )

    nombre = params.get("name")
    # Y el nombre tiene que ser un STRING antes de tocar el dict: con una lista o un dict,
    # `BY_NAME.get(nombre)` levantaba `TypeError: unhashable type`. Mismo 500, otra puerta.
    if not isinstance(nombre, str):
        return protocol.Respuesta(
            200,
            jsonrpc.error(
                rid, jsonrpc.INVALID_PARAMS, "'params.name' tiene que ser una cadena."
            ),
        )

    spec = BY_NAME.get(nombre)
    if spec is None:
        # `-32602` y no `-32601`: la spec clasifica "tool desconocida" como problema de
        # PARÁMETROS, no de método — el método `tools/call` existe. Y sigue siendo error de
        # protocolo y no de tool, porque el agente pidió algo que `tools/list` no publica: es un
        # bug del cliente, no una negación de acceso.
        return protocol.Respuesta(
            200, jsonrpc.error(rid, jsonrpc.INVALID_PARAMS, f"Tool desconocida: {nombre!r}.")
        )

    argumentos = params.get("arguments") or {}
    if not isinstance(argumentos, dict):
        return protocol.Respuesta(
            200,
            jsonrpc.error(rid, jsonrpc.INVALID_PARAMS, "'arguments' tiene que ser un objeto."),
        )

    # El schema de entrada es CERRADO y hay que hacerlo cumplir, no solo publicarlo: un
    # `additionalProperties: false` que el servidor no valida es una promesa que el cliente lee
    # y el servidor no sostiene — y la diferencia entre lo que el operador cree que pidió y lo
    # que se ejecutó vive exactamente ahí.
    permitidas = set((spec.input_schema.get("properties") or {}).keys())
    if spec.input_schema.get("additionalProperties") is False:
        sobrantes = sorted(set(argumentos) - permitidas)
        if sobrantes:
            return protocol.Respuesta(
                200,
                jsonrpc.error(
                    rid,
                    jsonrpc.INVALID_PARAMS,
                    f"Argumentos no declarados en el schema de {spec.name!r}: {sobrantes}.",
                ),
            )

    try:
        resultado = spec.handler(ToolContext(actor=actor), argumentos)
    except AppHttpException as exc:
        # LA traducción que este módulo existe para hacer: la negación del gate viaja como
        # contenido de TOOL, con su código del vocabulario cerrado. Un error de tool en el campo
        # `error` haría que el agente crea que el servidor está roto y reintente.
        codigo = (exc.public_context or {}).get("code") or "mcp.error"
        _audit(spec.name, actor, ok=False, detail=f"denegado: {codigo}")
        return _ok(rid, jsonrpc.tool_error_result(codigo, exc.message))
    except Exception:  # noqa: BLE001 — ver el comentario
        # Cualquier otra cosa NO puede salir con detalle: un traceback o un `str(exc)` del motor
        # por este canal termina en el contexto de un modelo y de ahí en la pantalla de
        # cualquiera. El detalle va al log con el Request ID, que es la regla del repo.
        from app.core.logger import get_logger

        get_logger(__name__).exception("Fallo no esperado en la tool %s", spec.name)
        _audit(spec.name, actor, ok=False, detail="fallo interno")
        return protocol.Respuesta(
            200, jsonrpc.error(rid, jsonrpc.INTERNAL_ERROR, "Fallo interno del servidor MCP.")
        )

    payload_tool = jsonrpc.tool_result_payload(resultado)
    serializado = json.dumps(payload_tool, ensure_ascii=False, default=str)
    if len(serializado.encode("utf-8")) > MAX_RESULT_BYTES:
        _audit(spec.name, actor, ok=False, detail="excedió el presupuesto de bytes")
        return _ok(
            rid,
            jsonrpc.tool_error_result(
                "mcp.result_too_large",
                (
                    "El resultado supera el tope de la respuesta. Acotá la consulta: no se "
                    "trunca a propósito, porque un JSON cortado haría creer que el esquema es "
                    "más chico."
                ),
            ),
        )

    _audit(spec.name, actor, ok=True, detail=f"{len(serializado)} bytes")
    return _ok(rid, payload_tool)


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
