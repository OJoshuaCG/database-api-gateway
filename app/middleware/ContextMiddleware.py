import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.context import (
    current_http_identifier,
    current_request_client_host,
    current_request_host,
    current_request_ip,
    current_request_method,
    current_request_route,
    current_request_user_agent,
)


class ContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 1. Generar un ID ÚNICO y ALEATORIO por solicitud (Correlation ID).
        #    secrets.token_hex(8) => 16 hex; nuevo y distinto en cada request.
        request_id = secrets.token_hex(8)

        # 2. Establecer las variables de contexto de la request.
        #    NO se resetean al terminar (ver nota abajo): cada request las vuelve a SET
        #    al entrar, así que no hay fuga entre requests, y resetearlas dejaba el
        #    request_id en None justo cuando se ejecuta el handler de error 500 (que vive
        #    en el ServerErrorMiddleware, MÁS EXTERNO que este middleware y por tanto
        #    DESPUÉS de un finally aquí). Sin reset, el identifier queda disponible vía
        #    ContextVar en TODOS los handlers (incluido el genérico de 500) y en tareas
        #    derivadas de la request.
        current_http_identifier.set(request_id)
        current_request_ip.set(request.client.host if request.client else "unknown")
        current_request_method.set(request.method)
        current_request_route.set(request.url.path)
        current_request_client_host.set(
            request.client.host if request.client else None
        )
        current_request_host.set(request.url.hostname)
        current_request_user_agent.set(request.headers.get("user-agent"))

        # Inyectar el ID en el request.state (acceso directo desde la Request, p. ej. en
        # handlers de error, independientemente del ContextVar).
        request.state.request_id = request_id

        response = await call_next(request)

        # 3. Header X-Request-ID en la respuesta para trazabilidad de extremo a extremo.
        response.headers["X-Request-ID"] = request_id

        return response
