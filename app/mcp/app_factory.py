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
"""

from fastapi import Depends, FastAPI, Request

from app.core.actor import Actor
from app.core.mcp_auth import authenticate_agent
from app.exceptions import (
    AppHttpException,
    app_exception_handler,
    generic_exception_handler,
)
from app.mcp import jsonrpc
from app.mcp.dispatch import handle
from app.middleware.ContextMiddleware import ContextMiddleware


def _agente(request: Request) -> Actor:
    return authenticate_agent(request)


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
    mcp.add_exception_handler(AppHttpException, app_exception_handler)
    mcp.add_exception_handler(Exception, generic_exception_handler)

    @mcp.post("/")
    async def rpc(request: Request, actor: Actor = Depends(_agente)):
        """
        El único endpoint. Streamable HTTP: un POST por mensaje JSON-RPC.

        El cuerpo se parsea acá y no con un modelo Pydantic a propósito: un JSON inválido tiene
        que salir como ``PARSE_ERROR`` de JSON-RPC, y un ``RequestValidationError`` de FastAPI
        saldría como 422 — que para un cliente MCP es un servidor roto, no un mensaje mal
        formado.
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — cualquier cuerpo no-JSON es un PARSE_ERROR
            return jsonrpc.error(None, jsonrpc.PARSE_ERROR, "El cuerpo no es JSON válido.")

        # Un batch (lista) NO se soporta: multiplicaría el presupuesto de bytes por N sin que el
        # tope lo vea, y ningún cliente MCP lo necesita. Se responde como request inválido, que
        # es lo que es para este servidor.
        if isinstance(payload, list):
            return jsonrpc.error(
                None, jsonrpc.INVALID_REQUEST, "Este servidor no acepta batches JSON-RPC."
            )

        return handle(payload, actor)

    return mcp
