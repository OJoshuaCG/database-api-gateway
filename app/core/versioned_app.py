import secrets

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.core.environments import (
    APP_NAME,
    CORS_ORIGINS,
    DOCS_ENABLED,
    DOCS_PASSWORD,
    DOCS_PASSWORD_ENABLED,
    DOCS_USER,
    LOGGER_MIDDLEWARE_ENABLED,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE,
    SESSION_SECRET,
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


def cors_allow_credentials(origins: list[str]) -> bool:
    """
    Las cookies de sesión NO pueden viajar con CORS comodín: los navegadores rechazan
    ``Access-Control-Allow-Credentials: true`` junto con ``Access-Control-Allow-Origin: *``,
    y reflejar el origin sería un vector de CSRF asistido por CORS. Solo permitimos
    credenciales cuando hay una lista EXPLÍCITA de orígenes.
    """
    return bool(origins) and "*" not in origins


class PathScopedCORSMiddleware:
    """
    Aplica ``CORSMiddleware`` SOLO a paths que empiecen con ``path_prefix``; para
    cualquier otro path, pasa la request sin tocar (passthrough total, ni siquiera mira
    el ``Origin``).

    Existe porque el app PRINCIPAL monta las sub-apps versionadas
    (``app.mount("/api/v1", v1_app)``), y un middleware agregado con
    ``app.add_middleware()`` en el app principal envuelve TAMBIÉN esas sub-apps montadas
    (el middleware del ASGI externo siempre corre antes que el routing interno que
    resuelve el mount). Un ``CORSMiddleware`` "global" ahí interceptaría el preflight de
    *cualquier* ruta de ``/api/v1/*`` ANTES de que llegue al ``CORSMiddleware`` propio de
    esa sub-app (que tiene su propia config de ``allow_methods``/``allow_headers``/
    ``allow_credentials``) — y si la config del principal es más restrictiva (p. ej.
    ``allow_methods=["GET"]``, pensada solo para ``/health``), el preflight de un POST a
    ``/api/v1/auth/login`` se rechaza con ``400 Disallowed CORS method`` aunque la
    sub-app v1 sí lo permita. Bug real detectado por el frontend (login roto en CORS)
    tras agregar CORS al app principal para /health sin acotarlo por path.

    Con este wrapper, el CORS del app principal queda estrictamente acotado a sus
    propias rutas no versionadas (``/health``) y nunca interfiere con las sub-apps
    montadas, que se resuelven con SU PROPIO stack de middlewares intacto — coherente
    con que cada sub-app versionada es autocontenida (ver docstring de
    ``create_versioned_app``).
    """

    def __init__(self, app, path_prefix: str, **cors_kwargs) -> None:
        self._plain_app = app
        self._cors_app = CORSMiddleware(app, **cors_kwargs)
        self._path_prefix = path_prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self._path_prefix):
            await self._cors_app(scope, receive, send)
        else:
            await self._plain_app(scope, receive, send)


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
        # Solo se permiten credenciales (cookie de sesión) con orígenes explícitos.
        # Con CORS_ORIGINS="*" esto queda en False para no exponer la sesión cross-origin.
        allow_credentials=cors_allow_credentials(CORS_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    versioned.add_middleware(
        RequestSizeMiddleware,
        excluded_paths=excluded_request_size_paths or [],
    )
    # SessionMiddleware: cookie de sesión firmada (httpOnly) para la autenticación
    # del admin. Se añade al final para quedar como capa más externa.
    versioned.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        session_cookie="gw_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=SESSION_COOKIE_SECURE,
    )

    # === Rate limiter
    versioned.state.limiter = limiter

    # === Exception handlers
    versioned.add_exception_handler(AppHttpException, app_exception_handler)
    versioned.add_exception_handler(RequestValidationError, validation_exception_handler)
    versioned.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    versioned.add_exception_handler(Exception, generic_exception_handler)

    return versioned
