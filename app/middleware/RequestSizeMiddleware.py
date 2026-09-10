from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.environments import REQUEST_MAX_SIZE_MB

# Métodos HTTP que pueden llevar body
_METHODS_WITH_BODY = {"POST", "PUT", "PATCH"}


class RequestSizeMiddleware(BaseHTTPMiddleware):
    """
    Middleware que rechaza requests cuyo body supere REQUEST_MAX_SIZE_MB.

    Estrategia de validación:
      1. Lee el header Content-Length (rápido, sin consumir el body).
      2. Si el header no está presente, lee el body completo como fallback.
         El body queda cacheado en request._body para que el endpoint pueda leerlo.

    Solo aplica a métodos con body: POST, PUT, PATCH.

    Args:
        excluded_paths: Lista de rutas exactas que omiten la validación.
                        Se configura a nivel de código, no por variables de entorno.

    Uso en versioned_app.py o main.py:
        app.add_middleware(
            RequestSizeMiddleware,
            excluded_paths=["/api/v1/special-upload"],
        )
    """

    def __init__(
        self,
        app,
        excluded_paths: list[str] | None = None,
        max_size_mb: float | None = None,
    ):
        """
        ``max_size_mb`` permite un tope POR INSTANCIA, con default al global.

        Existe porque el endpoint MCP necesita uno mucho más chico que la API: un mensaje
        JSON-RPC legítimo son kilobytes, mientras la API sube artefactos. Un parámetro con
        default al valor global es más barato que una segunda implementación de "rechazá un
        cuerpo grande" — y una segunda implementación es la que se olvida de la estrategia del
        `Content-Length` ausente.
        """
        super().__init__(app)
        self.max_size_mb: float = (
            REQUEST_MAX_SIZE_MB if max_size_mb is None else max_size_mb
        )
        self.max_bytes: float = self.max_size_mb * 1024 * 1024
        self.excluded_paths: list[str] = excluded_paths or []

    async def dispatch(self, request: Request, call_next):
        if request.method not in _METHODS_WITH_BODY:
            return await call_next(request)

        if request.url.path in self.excluded_paths:
            return await call_next(request)

        # — Estrategia 1: Content-Length header
        content_length = request.headers.get("content-length")
        if content_length is not None:
            if int(content_length) > self.max_bytes:
                return self._reject(request)
            return await call_next(request)

        # — Estrategia 2: leer body como fallback (Content-Length ausente)
        # request.body() cachea el resultado en request._body,
        # por lo que el endpoint puede releerlo sin problema.
        body = await request.body()
        if len(body) > self.max_bytes:
            return self._reject(request)

        return await call_next(request)

    def _reject(self, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "detail": {
                    # El límite EFECTIVO de esta instancia, no el global: con dos topes
                    # distintos en el proceso, el mensaje tiene que decir cuál se aplicó.
                    "msg": (
                        "El cuerpo de la solicitud supera el límite permitido de "
                        f"{self.max_size_mb}MB"
                    ),
                    "type": "RequestTooLarge",
                }
            },
        )
