from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.environments import (
    APP_NAME,
    CORS_ORIGINS,
    DOCS_ENABLED,
    LOGGER_MIDDLEWARE_ENABLED,
)
from app.core.limiter import limiter
from app.exceptions import (
    AppHttpException,
    app_exception_handler,
    generic_exception_handler,
    rate_limit_handler,
    validation_exception_handler,
)
from app.middleware.ContextMiddleware import ContextMiddleware
from app.middleware.LoggerMiddleware import LoggerMiddleware
from app.middleware.RequestSizeMiddleware import RequestSizeMiddleware


def create_versioned_app(
    version: str,
    excluded_request_size_paths: list[str] | None = None,
) -> FastAPI:
    """
    Factory que crea una sub-aplicación FastAPI completamente configurada
    para una versión específica de la API.

    Cada versión tiene:
    - Su propia documentación en /docs y /redoc
    - Todos los middlewares (CORS, Context, Logger, RateLimit, RequestSize)
    - Todos los exception handlers

    Uso en main.py:
        v1_app = create_versioned_app("v1")
        v1_app.include_router(v1_router)
        app.mount("/api/v1", v1_app)

        # Con rutas excluidas del validador de tamaño:
        v1_app = create_versioned_app(
            "v1",
            excluded_request_size_paths=["/special-upload"],
        )

    Args:
        version:                      Etiqueta de versión (ej: "v1", "v2")
        excluded_request_size_paths:  Rutas exactas que omiten el middleware
                                      de validación de tamaño de request.
                                      Se define a nivel de código.
    """
    versioned = FastAPI(
        title=f"{APP_NAME} {version.upper()}",
        version=version,
        docs_url="/docs" if DOCS_ENABLED else None,
        redoc_url="/redoc" if DOCS_ENABLED else None,
        openapi_url="/openapi.json" if DOCS_ENABLED else None,
    )

    # === Middlewares (último en add_middleware = primero en ejecutarse)
    versioned.add_middleware(SlowAPIMiddleware)
    if LOGGER_MIDDLEWARE_ENABLED:
        versioned.add_middleware(LoggerMiddleware)
    versioned.add_middleware(ContextMiddleware)
    versioned.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    versioned.add_middleware(
        RequestSizeMiddleware,
        excluded_paths=excluded_request_size_paths or [],
    )

    # === Rate limiter
    versioned.state.limiter = limiter

    # === Exception handlers
    versioned.add_exception_handler(AppHttpException, app_exception_handler)
    versioned.add_exception_handler(RequestValidationError, validation_exception_handler)
    versioned.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    versioned.add_exception_handler(Exception, generic_exception_handler)

    return versioned
