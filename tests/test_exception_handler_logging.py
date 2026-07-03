"""
Regresión: el `generic_exception_handler` NUNCA debe romperse al construir el log.

Antes, el `RequestID` (ContextVar `current_http_identifier`, default None) se metía CRUDO
en la lista que se pasa a `" | ".join(...)`; sin contexto de request, `join([None, ...])`
lanzaba TypeError y enmascaraba el error original (devolviendo un 500 roto en vez del
manejado). El handler debe seguir devolviendo un 500 limpio aunque el RequestID sea None.
"""

import asyncio

from starlette.requests import Request

import app.exceptions.HandlerExceptions as H


def _req() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )


def test_generic_handler_does_not_crash_without_request_id(monkeypatch):
    # Logging de excepciones ACTIVO + sin request id en contexto (default None).
    monkeypatch.setattr(H, "LOGGER_EXCEPTIONS_ENABLED", True)
    resp = asyncio.run(H.generic_exception_handler(_req(), ValueError("boom")))
    assert resp.status_code == 500


def test_generic_handler_logs_inside_exception_context(monkeypatch):
    monkeypatch.setattr(H, "LOGGER_EXCEPTIONS_ENABLED", True)
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as exc:
        resp = asyncio.run(H.generic_exception_handler(_req(), exc))
    assert resp.status_code == 500


def test_request_id_present_in_500_handler_and_unique_per_request():
    """
    El request_id debe estar SIEMPRE disponible vía ContextVar incluso en el handler de
    error más externo (500), y ser ALEATORIO y DISTINTO en cada request. Regresión del
    reset prematuro del ContextMiddleware que lo dejaba en None en ese handler.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.responses import JSONResponse

    from app.core.context import current_http_identifier
    from app.middleware.ContextMiddleware import ContextMiddleware

    captured: list[str | None] = []
    app = FastAPI()
    app.add_middleware(ContextMiddleware)

    @app.get("/boom")
    def _boom():
        raise RuntimeError("explota")

    async def _handler(request, exc):
        captured.append(current_http_identifier.get())  # leído en el handler de 500
        return JSONResponse({"detail": "err"}, status_code=500)

    app.add_exception_handler(Exception, _handler)

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/boom").status_code == 500
        r2 = c.get("/boom")
        assert r2.status_code == 500

    # (1) Disponible (no None) en el handler de 500 · (2) 16 hex · (3) distinto por request.
    assert all(rid is not None and len(rid) == 16 for rid in captured), captured
    assert captured[0] != captured[1]


def test_request_id_unique_and_in_header_on_success():
    """El X-Request-ID de la respuesta es distinto en cada request exitosa."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.middleware.ContextMiddleware import ContextMiddleware

    app = FastAPI()
    app.add_middleware(ContextMiddleware)

    @app.get("/ok")
    def _ok():
        return {"ok": True}

    with TestClient(app) as c:
        h1 = c.get("/ok").headers.get("X-Request-ID")
        h2 = c.get("/ok").headers.get("X-Request-ID")
    assert h1 and h2 and len(h1) == 16 and h1 != h2
