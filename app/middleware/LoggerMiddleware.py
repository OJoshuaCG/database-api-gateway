import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.context import current_http_identifier
from app.core.environments import (
    LOGGER_MIDDLEWARE_ERRORS_ONLY,
    LOGGER_MIDDLEWARE_SHOW_BODY,
    LOGGER_MIDDLEWARE_SHOW_HEADERS,
    LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS,
    LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS,
)
from app.core.logger import get_logger
from app.utils.dict_utils import _sanitize_dict

logger = get_logger()

# Rutas cuyo body se oculta por completo (son íntegramente credenciales).
_SENSITIVE_PATHS = {"/api/v1/auth/login"}


class LoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        unique_id = current_http_identifier.get()
        start_time = time.time()

        method = request.method
        path = request.url.path
        query_string = request.url.query or None
        headers = dict(request.headers)
        client_ip = request.client.host if request.client else "unknown"

        try:
            body = await request.json()
        except Exception:
            body = "<no body>"

        response = await call_next(request)
        process_time = round(time.time() - start_time, 3)

        if LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS:
            display_path = path
        else:
            route = request.scope.get("route")
            display_path = route.path if route else path

        is_error = response.status_code >= 400

        if LOGGER_MIDDLEWARE_ERRORS_ONLY and not is_error:
            return response

        # REQUEST
        request_parts = [
            str(unique_id),
            f"Host: {client_ip}",
            f"Request: {method} {display_path}",
        ]
        if LOGGER_MIDDLEWARE_SHOW_BODY:
            if path in _SENSITIVE_PATHS:
                safe_body = "<cannot show>"
            else:
                # Enmascara campos sensibles (password, root_password, token, ...)
                safe_body = _sanitize_dict(body) if isinstance(body, dict) else body
            request_parts.append(f"Body: {safe_body}")
        if LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS:
            request_parts.append(
                f"Query: {query_string if query_string else '<no parameters>'}"
            )
        if LOGGER_MIDDLEWARE_SHOW_HEADERS:
            request_parts.append(f"Headers: {headers}")

        logger.info(" | ".join(request_parts))

        # ERROR (solo cuando hay error)
        if is_error:
            error_parts = [
                str(unique_id),
                f"Host: {client_ip}",
                f"Error: {method} {display_path}",
                f"Status: {response.status_code}",
            ]
            if response.status_code >= 500:
                logger.error(" | ".join(error_parts))
            else:
                logger.warning(" | ".join(error_parts))

        # RESPONSE
        response_parts = [
            str(unique_id),
            f"Host: {client_ip}",
            f"Response: {method} {display_path}",
            f"Status: {response.status_code}",
            f"Duration: {process_time}s",
        ]
        logger.info(" | ".join(response_parts))

        return response
