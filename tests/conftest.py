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
        "LOGGER_MIDDLEWARE_ENABLED": "False",
        "LOGGER_EXCEPTIONS_ENABLED": "False",
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
    from app.core.database import Database
    from app.core.limiter import limiter
    from app.models import Base

    db = Database()
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)

    limiter.enabled = False

    import main

    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def admin_client(client):
    """Client ya autenticado como admin (cookie de sesión establecida)."""
    resp = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert resp.status_code == 200, resp.text
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
