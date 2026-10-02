"""
``POST /api/v1/auth/password``: cambio de la password propia (F-26).

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Antes no había forma de rotar una credencial filtrada: el único escritor de ``hashed_password``
era aceptar la invitación, y ``REASON_PASSWORD_CHANGE`` era vocabulario muerto. Lo que se fija
acá es el contrato completo: la password vieja deja de servir, la nueva sirve, TODAS las
sesiones anteriores mueren (la actual rota a un ``sid`` nuevo), y nada de eso es alcanzable sin
sesión por cookie, sin CSRF o sin la password actual.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.csrf import CSRF_HEADER, cookie_name
from app.core.database import Database
from app.core.limiter import limiter
from app.models.audit_log import AuditLog
from app.models.gateway_session import GatewaySession
from tests.csrf_helpers import attach_csrf

URL = "/api/v1/auth/password"
NUEVA = "una-password-nueva-larga"


def _login(c, password="admin123"):
    return c.post("/api/v1/auth/login", json={"username": "admin", "password": password})


def _cambiar(c, actual="admin123", nueva=NUEVA):
    return c.post(URL, json={"current_password": actual, "new_password": nueva})


def _code(resp) -> str | None:
    return ((resp.json().get("detail") or {}).get("public_context") or {}).get("code")


def _rows(action: str) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        return s.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
    finally:
        s.close()


def _sesiones() -> list[GatewaySession]:
    s = Database().get_declarative_base_session()
    try:
        return s.query(GatewaySession).order_by(GatewaySession.created_at).all()
    finally:
        s.close()


def _otro_cliente() -> TestClient:
    # Sin gestor de contexto: el lifespan ya corrió en la fixture `client`.
    from main import app

    c = TestClient(app)
    assert _login(c).status_code == 200
    attach_csrf(c)
    return c


# --------------------------------------------------------------------------- #
# Éxito                                                                       #
# --------------------------------------------------------------------------- #


def test_success_rotates_the_credential(client, admin_client):
    r = _cambiar(admin_client)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"revoked_sessions": 0}

    fresco = TestClient(admin_client.app)
    assert _login(fresco, "admin123").status_code == 401, "la password vieja sigue sirviendo"
    assert _login(fresco, NUEVA).status_code == 200, "la password nueva no sirve"


def test_success_revokes_every_other_session(client, admin_client):
    otra = _otro_cliente()
    assert otra.get("/api/v1/auth/me").status_code == 200

    r = _cambiar(admin_client)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["revoked_sessions"] == 1

    r = otra.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert _code(r) == "auth.session_password_change"


def test_the_current_session_survives_on_a_rotated_sid(client, admin_client):
    """
    La sesión desde la que se cambió sigue funcionando, pero con OTRO ``sid``: la vieja queda
    tachada con ``password_change``, y el token CSRF nuevo llega en la misma respuesta.
    """
    csrf_viejo = admin_client.cookies.get(cookie_name())
    r = _cambiar(admin_client)
    assert r.status_code == 200, r.text

    vivas = [s for s in _sesiones() if s.revoked_at is None]
    tachadas = [s for s in _sesiones() if s.revoked_at is not None]
    assert len(vivas) == 1
    assert len(tachadas) == 1
    assert tachadas[0].revoked_reason == "password_change"

    csrf_nuevo = admin_client.cookies.get(cookie_name())
    assert csrf_nuevo and csrf_nuevo != csrf_viejo, "no se re-emitió el token CSRF"

    assert admin_client.get("/api/v1/auth/me").status_code == 200
    # Con el header CSRF viejo un método no seguro ya no valida; con el nuevo, sí.
    assert admin_client.post("/api/v1/auth/sessions/revoke-others").status_code == 403
    attach_csrf(admin_client)
    assert admin_client.post("/api/v1/auth/sessions/revoke-others").status_code == 200


def test_success_is_audited_without_any_password(client, admin_client):
    assert _cambiar(admin_client).status_code == 200

    filas = _rows("auth.password_changed")
    assert len(filas) == 1
    fila = filas[0]
    assert fila.admin_id == 1
    assert fila.status == "success"
    volcado = " ".join(str(getattr(fila, c.key)) for c in AuditLog.__table__.columns)
    assert "admin123" not in volcado
    assert NUEVA not in volcado


# --------------------------------------------------------------------------- #
# Rechazos                                                                    #
# --------------------------------------------------------------------------- #


def test_wrong_current_password_is_422_and_changes_nothing(client, admin_client):
    r = _cambiar(admin_client, actual="no-es-esta")
    assert r.status_code == 422
    assert _code(r) == "auth.invalid_current_password"

    assert admin_client.get("/api/v1/auth/me").status_code == 200, "un fallo no cierra la sesión"
    fresco = TestClient(admin_client.app)
    assert _login(fresco, "admin123").status_code == 200

    filas = _rows("auth.password_change_failed")
    assert len(filas) == 1 and filas[0].status == "failure"
    volcado = " ".join(str(getattr(filas[0], c.key)) for c in AuditLog.__table__.columns)
    assert "no-es-esta" not in volcado and NUEVA not in volcado
    assert _rows("auth.password_changed") == []


def test_a_weak_new_password_uses_the_invite_policy_code(client, admin_client):
    from app.utils.security import PASSWORD_MIN_LENGTH

    r = _cambiar(admin_client, nueva="corta")
    assert r.status_code == 422
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "gateway_user.weak_password"
    assert pc["min_length"] == PASSWORD_MIN_LENGTH


def test_the_new_password_must_differ_from_the_current(client, admin_client):
    # La password del seed es más corta que el mínimo, así que se fija una válida primero.
    assert _cambiar(admin_client).status_code == 200
    attach_csrf(admin_client)

    r = _cambiar(admin_client, actual=NUEVA, nueva=NUEVA)
    assert r.status_code == 422
    assert _code(r) == "auth.password_unchanged"


def test_without_a_session_it_is_401(client):
    r = _cambiar(client)
    assert r.status_code == 401


def test_an_agent_token_is_rejected(client, admin_client):
    pid = admin_client.post("/api/v1/projects", json={"name": "Pw"}).json()["data"]["id"]
    r = admin_client.post("/api/v1/api-tokens", json={"name": "agente", "project_id": pid})
    assert r.status_code == 201, r.text
    token = r.json()["data"]["token"]

    sin_cookie = TestClient(admin_client.app)
    r = sin_cookie.post(
        URL,
        json={"current_password": "admin123", "new_password": NUEVA},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401
    assert _login(TestClient(admin_client.app), "admin123").status_code == 200


def test_missing_csrf_token_is_403(client, admin_client):
    del admin_client.headers[CSRF_HEADER]
    r = _cambiar(admin_client)
    assert r.status_code == 403
    assert _code(r) == "auth.csrf_missing"
    assert _login(TestClient(admin_client.app), "admin123").status_code == 200


# --------------------------------------------------------------------------- #
# Límite de tasa                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def con_limitador():
    limiter.enabled = True
    limiter.reset()
    try:
        yield
    finally:
        limiter.enabled = False
        limiter.reset()


def test_guessing_the_current_password_is_rate_limited(client, admin_client, con_limitador):
    for _ in range(5):
        assert _cambiar(admin_client, actual="adivinanza").status_code == 422
    r = _cambiar(admin_client, actual="adivinanza")
    assert r.status_code == 429


def test_the_limit_is_per_user_not_per_session(client, admin_client, con_limitador):
    """Una sesión nueva del mismo usuario NO estrena cupo: la clave es el user_id verificado."""
    for _ in range(5):
        assert _cambiar(admin_client, actual="adivinanza").status_code == 422
    otra = _otro_cliente()
    assert _cambiar(otra, actual="adivinanza").status_code == 429
