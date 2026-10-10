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


def _reset_schema_and_state() -> None:
    """Esquema fresco (drop+create) y estado de proceso limpio, sin sembrar nada."""
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
    # Mismo motivo para la API de integración: su limitador y su agregador son estado del proceso.
    from app.core.integration_auth import reset_integration_auth_state

    reset_integration_auth_state()
    # Mismo motivo para el agregador de denegaciones (`access.denied`): sin esto, el 403 de un
    # test se come la fila del mismo (actor, código, ruta) en el siguiente.
    from app.core.denial_audit import reset_denial_state

    reset_denial_state()
    # Y el reporte de separación de deberes: "una vez por arranque" es un set del proceso, y acá
    # cada test es un arranque con la BD recién creada (los ids se repiten).
    from app.services.sod_service import reset_sod_report_state

    reset_sod_report_state()


@pytest.fixture()
def client():
    """
    TestClient con esquema fresco y la cuenta ``admin`` de una INSTALACIÓN EXISTENTE: ``owner`` +
    ``access_admin`` + ``security_officer`` heredada y la ventana de arranque cerrada. La siembra
    de producción NO crea eso (desde C4 es ``viewer`` + ``access_admin``): se pre-siembra antes del
    ``lifespan``, que entonces no hace nada. Ver ``tests/bootstrap_helpers.py``.
    Rate limiting desactivado para evitar 429 entre pruebas.
    """
    _reset_schema_and_state()
    from tests.bootstrap_helpers import seed_existing_install_admin

    seed_existing_install_admin()

    import main

    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def fresh_client():
    """
    TestClient sobre una instalación NUEVA: esquema vacío y el ``lifespan`` de producción tal
    cual (``bootstrap_admin`` siembra ``viewer`` + ``access_admin`` y abre la ventana).
    """
    _reset_schema_and_state()

    import main

    with TestClient(main.app) as c:
        yield c


# ``attach_csrf`` vive en un módulo SIN efectos al importarse: los tests que lo necesitan
# importaban ``tests.conftest`` y eso creaba una segunda instancia de este archivo, con su
# propio tmpdir y otro DB_NAME (ver test_hay_una_sola_instancia_de_conftest).
from tests.csrf_helpers import attach_csrf  # noqa: E402
from tests.step_up_helpers import expire_step_up as _expire_step_up  # noqa: E402


@pytest.fixture()
def expire_step_up():
    """``expire_step_up(client)``: cierra la ventana de step-up de esa sesión (ver el helper)."""
    return _expire_step_up


@pytest.fixture()
def admin_client(client):
    """Client ya autenticado como admin (cookie de sesión + header CSRF)."""
    resp = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert resp.status_code == 200, resp.text
    attach_csrf(client)
    return client


# --------------------------------------------------------------------------- #
# Identidades de la separación de deberes (C3)                                 #
# --------------------------------------------------------------------------- #
# `admin_client` es la cuenta COMBINADA de una instalación existente (owner + access_admin +
# security_officer, heredada; pre-sembrada por `client`, NO por la siembra de producción) y el
# único access_admin de la BD. Estas fixtures dan cuentas de UNA función, creadas
# por HTTP con su elevación aprobada por un segundo access_admin (`tests/access_request_helpers`).


@pytest.fixture()
def aa_client(admin_client):
    """Un SEGUNDO access_admin activo (viewer + access_admin): aprueba lo que pide el admin."""
    from tests.access_request_helpers import client_as, create_user

    datos = create_user(admin_client, "aa-segundo", global_capabilities=["access_admin"])
    return client_as(datos, "aa-segundo")


@pytest.fixture()
def owner_client(admin_client):
    """Un `owner` sin globales: opera, no administra accesos ni política."""
    from tests.access_request_helpers import client_as, create_user

    datos = create_user(admin_client, "owner-solo", gateway_role="owner")
    return client_as(datos, "owner-solo")


@pytest.fixture()
def so_client(admin_client):
    """Un `security_officer` viewer: política, sin operar ni administrar accesos."""
    from tests.access_request_helpers import client_as, create_user

    datos = create_user(admin_client, "so-solo-fx", global_capabilities=["security_officer"])
    return client_as(datos, "so-solo-fx")


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
