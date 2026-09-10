"""
La sub-app del MCP, montada en ``/mcp``.

POR QUÉ NO SE REUSA ``create_versioned_app()`` TAL CUAL
------------------------------------------------------
Esa factory monta lo que necesita la SPA y **nada de eso aplica acá**, con dos piezas que además
serían activamente dañinas:

- **``SessionMiddleware``**: un endpoint que se autentica con bearer no debe aceptar cookies. Si
  las aceptara, un navegador con la sesión del admin abierta podría llamar al MCP desde cualquier
  página — y la exención de CSRF que tienen los agentes, correcta porque un bearer no es
  ambiental, se volvería el bypass.
- **``CORSMiddleware`` con credenciales**: un cliente MCP no es un navegador. Publicar CORS acá
  solo habilita que uno lo llame.

Lo que sí se conserva: los handlers de excepción, para que una ``AppHttpException`` no se escape
como un 500 pelado, y el ``ContextMiddleware``, porque el Request ID es lo que ata el log al
rastro de auditoría.

LO QUE FALTABA, Y ERA OMISIÓN Y NO DECISIÓN
-------------------------------------------
La primera versión de esta sub-app tenía **un solo** middleware, y el docstring justificaba por
qué se habían quitado ``SessionMiddleware`` y ``CORSMiddleware`` — pero no decía nada de los
otros dos, porque nadie los había pensado. Una auditoría lo midió:

- **Sin tope de cuerpo**, un POST de 20 MB a ``/mcp/`` respondía ``200``; el mismo cuerpo a
  ``/api/v1/*`` daba ``413``. ``await request.json()`` bufferea en memoria lo que le manden.
- **Sin límite de tasa**, 50 requests seguidas eran 50 respuestas, y 60 bearers inválidos eran
  60 rechazos sin throttle. Con una consulta que cuesta cientos de milisegundos de CPU y un
  request de 120 bytes, la amplificación satura el proceso **y con él toda la SPA**, porque
  comparten la BD de metadatos.

La clave del límite es el **token**, no la IP: un agente en CI comparte IP con todos los demás
jobs, así que por IP el límite sería colectivo y el primero en gastarlo dejaría afuera al resto.
"""

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.core.actor import Actor
from app.core.mcp_auth import authenticate_agent
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.exceptions import (
    AppHttpException,
    app_exception_handler,
    generic_exception_handler,
    rate_limit_handler,
)
from app.mcp import jsonrpc, protocol
from app.mcp.dispatch import handle
from app.core.environments import MCP_MAX_BODY_KIB
from app.core.limiter import mcp_limiter
from app.middleware.ContextMiddleware import ContextMiddleware
from app.middleware.RequestSizeMiddleware import RequestSizeMiddleware


def _agente(request: Request) -> Actor:
    return authenticate_agent(request)


def _origen_permitido(origin: str) -> bool:
    """
    Reusa el mismo comparador de origen que el guard de CSRF.

    Una segunda implementación del "¿es un origen nuestro?" es una segunda que se relaja: el
    día que alguien agregue un origen a la config, tiene que valer para los dos caminos.
    """
    from app.core.csrf import _origin_permitido

    return _origin_permitido(origin)


def create_mcp_app() -> FastAPI:
    mcp = FastAPI(
        title="Gateway MCP",
        version="1",
        # Sin documentación pública: el contrato de un servidor MCP lo publica `tools/list`, y
        # un Swagger acá solo expone la superficie a quien no tiene token.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    mcp.add_middleware(ContextMiddleware)
    # Tope de cuerpo propio y chico: un mensaje JSON-RPC legítimo son kilobytes.
    mcp.add_middleware(RequestSizeMiddleware, max_size_mb=MCP_MAX_BODY_KIB / 1024)
    # Límite de tasa por TOKEN. `state.limiter` es lo que SlowAPIMiddleware busca; sin esa
    # línea el middleware no hace nada y el límite es una decoración.
    mcp.state.limiter = mcp_limiter
    mcp.add_middleware(SlowAPIMiddleware)
    mcp.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    mcp.add_exception_handler(AppHttpException, app_exception_handler)
    mcp.add_exception_handler(Exception, generic_exception_handler)

    async def _rpc(request: Request, actor: Actor) -> Response:
        """
        El endpoint MCP. Un POST por mensaje JSON-RPC.

        **Valida ``Origin`` antes que nada.** La spec lo pone como MUST y el ataque es concreto:
        sin eso, una página cualquiera puede hacer *DNS rebinding* contra un servidor MCP que
        corre en la máquina del operador y hablarle como si fuera local. Un ``Origin`` presente
        y ajeno es ``403``; **ausente no rechaza**, porque un cliente que no es un navegador no
        lo manda y no está sujeto al ataque.
        """
        origin = request.headers.get("origin")
        if origin and not _origen_permitido(origin):
            return JSONResponse(
                status_code=403,
                content=protocol.error(
                    jsonrpc.INVALID_REQUEST, "Origen no permitido."
                ),
            )

        # El cuerpo se parsea acá y no con un modelo Pydantic a propósito: un JSON inválido
        # tiene que salir como `PARSE_ERROR` de JSON-RPC, y un `RequestValidationError` de
        # FastAPI saldría como 422 — que para un cliente MCP es un servidor roto, no un mensaje
        # mal formado.
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — cualquier cuerpo no-JSON es un PARSE_ERROR
            return JSONResponse(
                status_code=400,
                content=jsonrpc.error(None, jsonrpc.PARSE_ERROR, "El cuerpo no es JSON válido."),
            )

        # Un batch (lista) NO se soporta: la spec de esta revisión dice que el cuerpo tiene que
        # ser **un** request o notificación, y un batch multiplicaría el presupuesto de bytes
        # por N sin que el tope lo vea.
        if isinstance(payload, list):
            return JSONResponse(
                status_code=400,
                content=jsonrpc.error(
                    None, jsonrpc.INVALID_REQUEST, "El cuerpo tiene que ser un solo mensaje."
                ),
            )

        r = handle(payload, actor, dict(request.headers))
        if r.body is None:
            # 202 sin cuerpo, que es lo que la spec pide para una notificación aceptada.
            return Response(status_code=r.status)
        return JSONResponse(status_code=r.status, content=r.body)

    # UNA sola ruta. La versión anterior declaraba además `""` para atender `POST /mcp`, y era
    # **código muerto**: `app.mount("/mcp", sub)` no matchea el path pelado, así que el router
    # EXTERNO emite el 307 antes de que la sub-app vea nada. Se verificó con un experimento, no
    # se supuso. Lo que resuelve el caso es `McpPathNormalizer`, abajo.
    @mcp.post("/")
    async def rpc(request: Request, actor: Actor = Depends(_agente)) -> Response:
        return await _rpc(request, actor)

    # GET y DELETE **no se declaran**, y responden 405 igual: Starlette contesta
    # `405 Method Not Allowed` cuando el path coincide y el método no. Es exactamente lo que la
    # spec pide de un servidor de esta revisión —el stream por GET y la terminación de sesión
    # por DELETE se retiraron— así que declararlos a mano era código sin efecto, con el costo de
    # dos rutas más que el guard de cobertura tiene que clasificar.

    return mcp


class McpPathNormalizer:
    """
    Hace que ``POST /mcp`` y ``POST /mcp/`` sean el MISMO request, sin redirección.

    POR QUÉ HACE FALTA UN SHIM Y NO ALCANZA UNA RUTA MÁS
    ---------------------------------------------------
    ``app.mount("/mcp", sub)`` **no matchea el path pelado** ``/mcp``: el ``Mount`` de Starlette
    exige la barra, así que el router externo no encuentra ruta y su ``redirect_slashes`` emite
    un ``307`` antes de que la sub-app vea el request. Una ruta ``""`` declarada adentro es
    inalcanzable — se verificó con un experimento.

    POR QUÉ EL 307 NO ES ACEPTABLE
    ------------------------------
    La URL natural que alguien escribe en su ``.mcp.json`` es ``https://host/mcp``. Un ``307``
    obliga al cliente a reintentar el POST, y hay clientes HTTP que **no reenvían el header
    ``Authorization``** en un salto de redirección: el reintento llega sin credencial y el
    servidor contesta ``401``. El síntoma que ve el operador es "el token no funciona", que es
    lo más lejos posible de la causa.

    Se reescribe el ``scope`` en vez de redirigir porque el objetivo es que las dos URLs sean
    equivalentes, no que una mande a la otra.
    """

    def __init__(self, app, *, prefijo: str = "/mcp") -> None:
        self._app = app
        self._pelado = prefijo
        self._con_barra = prefijo + "/"

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("path") == self._pelado:
            scope = {**scope, "path": self._con_barra, "raw_path": self._con_barra.encode()}
        await self._app(scope, receive, send)
