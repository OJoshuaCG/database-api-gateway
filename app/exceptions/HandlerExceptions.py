import sys
import traceback
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.core.context import current_http_identifier
from app.core.environments import APP_ENV, LOGGER_EXCEPTIONS_ENABLED, ROOT_DIR
from app.core.logger import get_logger
from app.exceptions import AppHttpException

# Se define SIEMPRE (no solo si LOGGER_EXCEPTIONS_ENABLED): definirlo condicionalmente
# provocaba NameError si el flag se activaba en runtime sin estar activo al importar. El
# `if LOGGER_EXCEPTIONS_ENABLED:` de cada handler ya controla si realmente se loguea.
logger = get_logger(level="WARNING")


async def app_exception_handler(request: Request, exc: AppHttpException):
    detail_error = {
        "msg": exc.message,
        "type": exc.__class__.__name__,
    }

    if LOGGER_EXCEPTIONS_ENABLED:
        logger_warning_exception = [
            current_http_identifier.get(),
            f"Exception: {exc.__class__.__name__}",
            f"Message: {exc.message}",
            f"Status Code: {exc.status_code}",
            f"Context: {getattr(exc, 'context', None)}",
            f"Loc: {exc.loc}",
        ]
        logger.warning(" | ".join(logger_warning_exception))

    if APP_ENV == "development":
        if getattr(exc, "context", None):
            detail_error["context"] = exc.context

        if getattr(exc, "loc", None):
            detail_error["loc"] = exc.loc

    return JSONResponse(status_code=exc.status_code, content={"detail": detail_error})


async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    detail_error = {
        "msg": f"Demasiadas solicitudes. Límite: {exc.detail}",
        "type": "RateLimitExceeded",
    }

    if LOGGER_EXCEPTIONS_ENABLED:
        logger_params = [
            str(current_http_identifier.get()),
            "Exception: RateLimitExceeded",
            f"Limit: {exc.detail}",
            f"IP: {request.client.host if request.client else 'unknown'}",
        ]
        logger.warning(" | ".join(logger_params))

    return JSONResponse(status_code=429, content={"detail": detail_error})


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    _LOCATION_PREFIXES = {"body", "query", "path", "header"}

    fields_with_errors = []
    for error in exc.errors():
        loc = error.get("loc", ())
        field_parts = [str(p) for p in loc if p not in _LOCATION_PREFIXES]
        field = ".".join(field_parts) if field_parts else str(loc)
        fields_with_errors.append({"field": field, "msg": error.get("msg", "")})

    field_names = [f["field"] for f in fields_with_errors]
    msg = (
        f"Error de validación en: {', '.join(field_names)}"
        if field_names
        else "Error de validación en los datos enviados"
    )

    detail_error = {"msg": msg, "type": "RequestValidationError"}

    if APP_ENV == "development":
        detail_error["context"] = fields_with_errors

    if LOGGER_EXCEPTIONS_ENABLED:
        logger_params = [
            str(current_http_identifier.get()),
            "Exception: RequestValidationError",
            f"Fields: {', '.join(field_names)}",
        ]
        logger.warning(" | ".join(logger_params))

    return JSONResponse(status_code=422, content={"detail": detail_error})


async def generic_exception_handler(request: Request, exc: Exception):
    detail_error = {"msg": "Error interno del servidor", "type": "InternalServerError"}

    trace_info = _get_full_traceback_info(exc, ROOT_DIR)

    if APP_ENV == "development":
        detail_error["context"] = {
            "type_error": exc.__class__.__name__,
            "exception": str(exc),
        }
        detail_error["loc"] = trace_info["origin"]

    if LOGGER_EXCEPTIONS_ENABLED:
        logger_warning_exception_params = [
            # Identifier "libre" (primer campo, sin etiqueta), como en los demás handlers;
            # el f-string solo garantiza str. Ya nunca es None: el ContextMiddleware no
            # resetea el ContextVar, así que sigue disponible aquí (handler de 500).
            f"{current_http_identifier.get()}",
            f"Exception: {exc.__class__.__name__}",
            f'Message: UNHANDLED EXC. "{str(exc)}"',
            f"File: {trace_info['origin']['file']}",
            f"Function: {trace_info['origin']['function']}",
            f"Line: {trace_info['origin']['line']}",
            f'Code: "{trace_info["origin"]["code"]}"',
        ]
        # Coerción defensiva a str: el handler de errores NUNCA debe romperse al loguear
        # (un elemento None —p. ej. el RequestID sin contexto— hacía fallar el join).
        logger.error(" | ".join(str(p) for p in logger_warning_exception_params))

    return JSONResponse(status_code=500, content={"detail": detail_error})


def _get_full_traceback_info(
    exc: Exception, project_root: Path | None = None
) -> dict[str, Any]:
    """
    Obtiene el traceback completo de la excepción
    """
    tb_list = traceback.extract_tb(sys.exc_info()[2])

    # Convertir cada frame del traceback
    trace_frames = []
    for frame in tb_list:
        absolute_path = Path(frame.filename)

        # Calcular ruta relativa
        if project_root:
            try:
                relative_path = absolute_path.relative_to(project_root)
                file_path = str(relative_path).replace("\\", "/")
            except ValueError:
                file_path = absolute_path.name
        else:
            file_path = absolute_path.name

        trace_frames.append(
            {
                "file": file_path,
                "function": frame.name,
                "line": frame.lineno,
                "code": frame.line,
            }
        )

    # El ultimo frame es donde ocurrio el error
    origin = (
        trace_frames[-1]
        if trace_frames
        else {"file": "unknown", "function": "unknown", "line": 0, "code": None}
    )

    return {
        "origin": origin,  # Donde ocurrio el error
        "full_trace": trace_frames,  # Traceback completo
    }


def _get_full_traceback(exc: Exception, project_root: Path | None = None) -> list[dict]:
    """Obtiene el traceback completo"""
    tb_list = traceback.extract_tb(sys.exc_info()[2])

    frames = []
    for frame in tb_list:
        absolute_path = Path(frame.filename)

        if project_root:
            try:
                relative_path = absolute_path.relative_to(project_root)
                file_path = str(relative_path).replace("\\", "/")
            except ValueError:
                file_path = absolute_path.name
        else:
            file_path = absolute_path.name

        frames.append(
            {
                "file": file_path,
                "function": frame.name,
                "line": frame.lineno,
                "code": frame.line,
            }
        )

    return frames


def _get_exception_info(
    exc: Exception, project_root: Path | None = None, depth: int = 2
) -> dict[str, Any]:
    """
    Obtiene informacion detallada de donde se origino la excepción
    """
    # Obtener el traceback
    tb = sys.exc_info()[2]

    if tb is None:
        return {"file": "unknown", "function": "unknown", "line": 0, "code": None}

    # Ir al ultimo frame del traceback (donde ocurrio el error)
    while tb.tb_next is not None:
        tb = tb.tb_next

    frame = tb.tb_frame
    absolute_path = Path(frame.f_code.co_filename)

    # Calcular ruta relativa
    if project_root:
        try:
            relative_path = absolute_path.relative_to(project_root)
            file_path = str(relative_path).replace("\\", "/")
        except ValueError:
            parts = absolute_path.parts[-depth:]
            file_path = "/".join(parts)
    else:
        parts = absolute_path.parts[-depth:]
        file_path = "/".join(parts)

    # Obtener el codigo que causo el error
    try:
        import linecache

        code_line = linecache.getline(str(absolute_path), frame.f_lineno).strip()
    except Exception:
        code_line = None

    return {
        "file": file_path,
        "function": frame.f_code.co_name,
        "line": frame.f_lineno,
        "code": code_line,
    }
