"""
Configuración de pytest.

Fija las variables de entorno ANTES de importar la app (environments.py las lee
al import), usando una BD SQLite temporal como BD de metadatos del gateway.
"""

import os
import tempfile

# --- Entorno de test (debe fijarse antes de importar cualquier módulo de app) ---
_TMPDIR = tempfile.mkdtemp(prefix="gw_test_")
_DB_PATH = os.path.join(_TMPDIR, "test_gateway.db")

os.environ.update(
    {
        "DB_ENGINE": "sqlite",
        "DB_NAME": _DB_PATH,
        "SECRET_KEY": "test-secret-key-fixed",
        "CRYPTO_KEY_SALT": "test-salt",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "admin123",
        "APP_ENV": "development",
        # Lista EXPLÍCITA de orígenes, igual que en producción. Sin fijarla, CORS_ORIGINS cae
        # al default "*" de environments.py (o a lo que traiga el .env local de cada uno, que
        # load_dotenv no pisa sobre os.environ), y con "*" el chequeo de Origin del CSRF y del
        # MCP no tiene lista contra la que validar: los tests de "origen ajeno → 403" quedaban
        # verdes o rojos según la máquina. localhost:5173 es la SPA en desarrollo.
        "CORS_ORIGINS": "http://localhost:5173",
        "LOGGER_MIDDLEWARE_ENABLED": "False",
        "LOGGER_EXCEPTIONS_ENABLED": "False",
        # Los tests registran servidores con 127.0.0.1 como dummy; el guard anti-SSRF
        # se prueba aparte (tests/test_ssrf_guard.py) activándolo explícitamente.
        "REMOTE_SSRF_GUARD_ENABLED": "False",
        # Los artefactos de exportación van al directorio temporal de la corrida y no al
        # /app/exports del contenedor: ningún test debe poder escribir (ni borrar) en la
        # ruta de producción.
        "EXPORT_ARTIFACT_DIR": os.path.join(_TMPDIR, "exports"),
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    """
    TestClient con esquema fresco (drop+create) y admin sembrado por el lifespan.
    Rate limiting desactivado para evitar 429 entre pruebas.
    """
    from app.core import crypto
    from app.core.database import Database
    from app.core.limiter import limiter
    from app.models import Base

    db = Database()
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)

    # Esquema fresco → invalidar la DEK cacheada para aislar los tests entre sí
    # (evita arrastrar una DEK rotada en un test previo).
    crypto.reset_dek_cache()

    limiter.enabled = False
    # El `mcp_limiter` queda PRENDIDO (los tests del MCP prueban sus límites), así que se vacían
    # sus cupos y el agregador de rechazos: el storage en memoria vive lo que el proceso, y sin
    # esto los 401 de un test le gastan el tope de rechazos por IP a los siguientes.
    from app.core.mcp_auth import reset_rejection_state

    reset_rejection_state()
    # Mismo motivo para el agregador de denegaciones (`access.denied`): sin esto, el 403 de un
    # test se come la fila del mismo (actor, código, ruta) en el siguiente.
    from app.core.denial_audit import reset_denial_state

    reset_denial_state()

    import main

    with TestClient(main.app) as c:
        yield c


# ``attach_csrf`` vive en un módulo SIN efectos al importarse: los tests que lo necesitan
# importaban ``tests.conftest`` y eso creaba una segunda instancia de este archivo, con su
# propio tmpdir y otro DB_NAME (ver test_hay_una_sola_instancia_de_conftest).
from tests.csrf_helpers import attach_csrf  # noqa: E402


@pytest.fixture()
def admin_client(client):
    """Client ya autenticado como admin (cookie de sesión + header CSRF)."""
    resp = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert resp.status_code == 200, resp.text
    attach_csrf(client)
    return client


@pytest.fixture()
def server_payload():
    """Devuelve un builder de payloads de Server con overrides."""

    def _make(**overrides) -> dict:
        base = {
            "name": "srv-test",
            "host": "127.0.0.1",
            "port": 3399,
            "engine": "mysql",
            "root_username": "root",
            "root_password": "supersecret",
        }
        base.update(overrides)
        return base

    return _make
