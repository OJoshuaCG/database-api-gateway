import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.context import current_http_identifier
from app.core.environments import (
    LOGGER_MIDDLEWARE_SHOW_BODY,
    LOGGER_MIDDLEWARE_SHOW_HEADERS,
    LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS,
    LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS,
)
from app.core.logger import get_logger

# Configuración del logger usando la función centralizada
logger = get_logger()


class LoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        unique_id = current_http_identifier.get()
        start_time = time.time()

        # Obtener datos de la solicitud
        method = request.method
        path = request.url.path
        query_string = request.url.query or None
        headers = dict(request.headers)
        client_ip = request.client.host if request.client else "unknown"

        try:
            body = await request.json()
        except Exception:
            body = "<no body>"

        # Procesar la solicitud
        response = await call_next(request)
        process_time = round(time.time() - start_time, 3)

        # Determinar el path a mostrar: ruta con template o URL real
        if LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS:
            display_path = path
        else:
            route = request.scope.get("route")
            display_path = route.path if route else path

        logger_info_request = [
            str(unique_id),
            f"Host: {client_ip}",
            f"Request: {method} {display_path}",
        ]

        if LOGGER_MIDDLEWARE_SHOW_BODY:
            logger_info_request.append(
                f"Body: {'<cannot show>' if path in ['/user/login'] else body}"
            )

        if LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS:
            logger_info_request.append(
                f"Query: {query_string if query_string else '<no parameters>'}"
            )

        if LOGGER_MIDDLEWARE_SHOW_HEADERS:
            logger_info_request.append(f"Headers: {headers}")

        logger.info(" | ".join(logger_info_request))

        # Registrar respuesta
        logger_info_response = [
            str(unique_id),
            f"Host: {client_ip}",
            f"Response: {method} {display_path}",
            f"Status: {response.status_code}",
            f"Duration: {process_time}s",
        ]
        logger.info(" | ".join(logger_info_response))

        return response
