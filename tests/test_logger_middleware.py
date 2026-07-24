"""
Tests de `LoggerMiddleware`: confirma que el trío REQUEST/ERROR/RESPONSE se loguea tanto
para errores CONTROLADOS (`AppHttpException`, resueltos por `ExceptionMiddleware`, que
queda POR DENTRO de este middleware) como para excepciones NO controladas (cualquier otra,
resueltas por `ServerErrorMiddleware`, que queda POR FUERA de este middleware y por eso
antes NO dejaba ningún log del request/response, solo la línea suelta del propio
`generic_exception_handler`).

Arnés: app FastAPI mínima con SOLO `LoggerMiddleware` + los exception handlers reales,
mismo patrón que `test_exception_handler_logging.py`. El logger del middleware se
monkeypatchea con un fake que registra llamadas (sin depender de `caplog`, ya que el
logger real tiene `propagate = False`).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.middleware.LoggerMiddleware as LM
from app.exceptions import AppHttpException, app_exception_handler, generic_exception_handler
from app.middleware.LoggerMiddleware import LoggerMiddleware


class _FakeLogger:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def info(self, msg):
        self.calls.append(("INFO", msg))

    def warning(self, msg):
        self.calls.append(("WARNING", msg))

    def error(self, msg):
        self.calls.append(("ERROR", msg))


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LoggerMiddleware)
    app.add_exception_handler(AppHttpException, app_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    @app.get("/ok")
    def _ok():
        return {"ok": True}

    @app.get("/controlled")
    def _controlled():
        raise AppHttpException("no encontrado", 404)

    @app.get("/boom")
    def _boom():
        raise RuntimeError("explota sin control")

    return app


def _client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=False)


def _setup(monkeypatch, *, errors_only=False):
    fake = _FakeLogger()
    monkeypatch.setattr(LM, "logger", fake)
    monkeypatch.setattr(LM, "LOGGER_MIDDLEWARE_ERRORS_ONLY", errors_only)
    monkeypatch.setattr(LM, "LOGGER_MIDDLEWARE_SHOW_BODY", False)
    monkeypatch.setattr(LM, "LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS", False)
    monkeypatch.setattr(LM, "LOGGER_MIDDLEWARE_SHOW_HEADERS", False)
    return fake


def test_ok_request_logs_request_and_response_only(monkeypatch):
    fake = _setup(monkeypatch)
    with _client() as c:
        resp = c.get("/ok")
    assert resp.status_code == 200
    levels = [lvl for lvl, _ in fake.calls]
    assert levels == ["INFO", "INFO"]  # REQUEST, RESPONSE (sin ERROR: no es error)
    assert "Request: GET" in fake.calls[0][1]
    assert "Response: GET" in fake.calls[1][1] and "Status: 200" in fake.calls[1][1]


def test_controlled_error_logs_request_error_response(monkeypatch):
    """AppHttpException: ExceptionMiddleware (mas adentro) ya la resuelve a Response ->
    call_next() retorna normal -> el camino EXISTENTE (no el nuevo except) es el que loguea."""
    fake = _setup(monkeypatch)
    with _client() as c:
        resp = c.get("/controlled")
    assert resp.status_code == 404
    levels = [lvl for lvl, _ in fake.calls]
    assert levels == ["INFO", "WARNING", "INFO"]  # REQUEST, ERROR(<500=warning), RESPONSE
    assert "Error: GET" in fake.calls[1][1] and "Status: 404" in fake.calls[1][1]
    assert "no controlado" not in fake.calls[1][1]
    assert "Response: GET" in fake.calls[2][1] and "Status: 404" in fake.calls[2][1]


def test_uncontrolled_exception_logs_request_error_response(monkeypatch):
    """Antes del fix: call_next() lanzaba, y ninguna linea de este middleware se logueaba.
    Ahora: el except la atrapa, loguea el trio, y vuelve a lanzar (re-raise) para que
    ServerErrorMiddleware/generic_exception_handler sigan generando la respuesta al cliente
    exactamente igual que antes."""
    fake = _setup(monkeypatch)
    with _client() as c:
        resp = c.get("/boom")
    assert resp.status_code == 500  # el cliente recibe la MISMA respuesta que antes del fix
    levels = [lvl for lvl, _ in fake.calls]
    assert levels == ["INFO", "ERROR", "INFO"]  # REQUEST, ERROR, RESPONSE
    assert "Request: GET" in fake.calls[0][1]
    assert "Error: GET" in fake.calls[1][1]
    assert "no controlado" in fake.calls[1][1]
    assert "Response: GET" in fake.calls[2][1] and "Status: 500" in fake.calls[2][1]


def test_uncontrolled_exception_logs_even_with_errors_only(monkeypatch):
    """LOGGER_MIDDLEWARE_ERRORS_ONLY solo suprime el caso SIN error; un 500 no controlado
    siempre debe quedar logueado (igual que ya pasa hoy con los errores controlados)."""
    fake = _setup(monkeypatch, errors_only=True)
    with _client() as c:
        resp = c.get("/boom")
    assert resp.status_code == 500
    levels = [lvl for lvl, _ in fake.calls]
    assert levels == ["INFO", "ERROR", "INFO"]


def test_errors_only_suppresses_successful_request_logs(monkeypatch):
    fake = _setup(monkeypatch, errors_only=True)
    with _client() as c:
        resp = c.get("/ok")
    assert resp.status_code == 200
    assert fake.calls == []
