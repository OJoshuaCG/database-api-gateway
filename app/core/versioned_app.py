import secrets

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.environments import (
    APP_NAME,
    CORS_ORIGINS,
    DOCS_ENABLED,
    DOCS_PASSWORD,
    DOCS_PASSWORD_ENABLED,
    DOCS_USER,
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

_http_basic = HTTPBasic()


def _verify_docs_credentials(credentials: HTTPBasicCredentials = Depends(_http_basic)):
    """Verifica usuario y contraseña para acceder a la documentación."""
    valid_user = secrets.compare_digest(credentials.username.encode(), DOCS_USER.encode())
    valid_pass = secrets.compare_digest(credentials.password.encode(), DOCS_PASSWORD.encode())
    if not (valid_user and valid_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def _register_protected_docs(app: FastAPI, version: str) -> None:
    """
    Registra /docs, /redoc y /openapi.json protegidos con HTTP Basic Auth.
    Solo se llama cuando DOCS_ENABLED=True y DOCS_PASSWORD_ENABLED=True.
    """
    title = f"{APP_NAME} {version.upper()}"

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(_verify_docs_credentials)])
    def get_openapi_schema():
        return app.openapi()

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(_verify_docs_credentials)])
    def get_swagger():
        return get_swagger_ui_html(openapi_url="openapi.json", title=f"{title} - Swagger")

    @app.get("/redoc", include_in_schema=False, dependencies=[Depends(_verify_docs_credentials)])
    def get_redoc():
        return get_redoc_html(openapi_url="openapi.json", title=f"{title} - ReDoc")


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

    Comportamiento de la documentación según variables de entorno:
    - DOCS_ENABLED=False                          → sin documentación
    - DOCS_ENABLED=True, DOCS_PASSWORD_ENABLED=False → documentación pública
    - DOCS_ENABLED=True, DOCS_PASSWORD_ENABLED=True  → documentación con contraseña (HTTP Basic)

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
    docs_protected = DOCS_ENABLED and DOCS_PASSWORD_ENABLED

    versioned = FastAPI(
        title=f"{APP_NAME} {version.upper()}",
        version=version,
        docs_url="/docs" if DOCS_ENABLED and not docs_protected else None,
        redoc_url="/redoc" if DOCS_ENABLED and not docs_protected else None,
        openapi_url="/openapi.json" if DOCS_ENABLED and not docs_protected else None,
    )

    if docs_protected:
        _register_protected_docs(versioned, version)

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
