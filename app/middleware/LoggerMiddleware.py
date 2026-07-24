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

        def _display_path() -> str:
            if LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS:
                return path
            route = request.scope.get("route")
            return route.path if route else path

        def _log_request(display_path: str) -> None:
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

        def _log_response(display_path: str, status_code: int, duration: float) -> None:
            response_parts = [
                str(unique_id),
                f"Host: {client_ip}",
                f"Response: {method} {display_path}",
                f"Status: {status_code}",
                f"Duration: {duration}s",
            ]
            logger.info(" | ".join(response_parts))

        try:
            response = await call_next(request)
        except Exception:
            # Excepcion NO controlada. AppHttpException/RequestValidationError/
            # RateLimitExceeded jamas llegan aca: Starlette las resuelve en
            # ExceptionMiddleware, que queda POR DENTRO de este middleware (ya vuelven como
            # Response normal). Lo unico que atraviesa este ``call_next`` como excepcion es
            # lo que no tiene handler especifico y termina en ServerErrorMiddleware (POR
            # FUERA de este middleware) -> generic_exception_handler, que siempre responde
            # 500. Sin este except, ese caso no dejaba NINGUN log de REQUEST/ERROR/RESPONSE
            # (solo la linea suelta que loguea el propio generic_exception_handler).
            display_path = _display_path()
            duration = round(time.time() - start_time, 3)
            _log_request(display_path)
            logger.error(
                " | ".join(
                    [
                        str(unique_id),
                        f"Host: {client_ip}",
                        f"Error: {method} {display_path}",
                        "Status: 500 (no controlado)",
                    ]
                )
            )
            _log_response(display_path, 500, duration)
            raise

        process_time = round(time.time() - start_time, 3)
        display_path = _display_path()
        is_error = response.status_code >= 400

        if LOGGER_MIDDLEWARE_ERRORS_ONLY and not is_error:
            return response

        _log_request(display_path)

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

        _log_response(display_path, response.status_code, process_time)

        return response
