"""
Helpers de tests para las ELEVACIONES con segundo aprobador (C3). Sin efectos al importarse.

POR QUÉ EXISTE
--------------
Desde C3, dar ``owner``, una global o un ``sod_override`` responde ``202 access.elevation_pending``
y no se aplica hasta que OTRO ``access_admin`` lo apruebe. ``admin_client`` es el ÚNICO
``access_admin`` de una BD de test recién creada, así que cientos de tests que creaban un
``owner`` o un ``security_officer`` con él para probar OTRA cosa quedarían con la cuenta a medias.

La salida NO es apagar los cuatro ojos para toda la suite (eso dejaría sin probar justo la
barrera que reemplazó al techo de otorgamiento), sino ``settle(r)``: si la respuesta es ``202``,
un segundo ``access_admin`` de test (``APPROVER``) la aprueba por HTTP —por la misma ruta y las
mismas reglas que en producción— y se devuelve la cuenta ya elevada.

EL APROBADOR ES EFÍMERO
-----------------------
Se crea (o reactiva) justo antes de aprobar y se DESACTIVA justo después. Activo, contaría para
el invariante del último administrador y cambiaría lo que miden los tests de F-24 ("el admin
sembrado es el último ``access_admin``"); desactivado, no cuenta para nada y la siguiente
aprobación lo reactiva.
"""

from __future__ import annotations

from sqlalchemy import text

APPROVER = "aprobador-tests"
APPROVER_PASSWORD = "AprobadorDeTests-123"
_PASSWORD = "ContraseñaLarga123"


def _sql(sql: str, **params) -> None:
    from app.core.database import Database

    with Database().engine.begin() as conn:
        conn.execute(text(sql), params)


def _login(username: str, password: str):
    from fastapi.testclient import TestClient

    from main import app
    from tests.csrf_helpers import attach_csrf

    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    attach_csrf(c)
    return c


def approver_client():
    """Un ``access_admin`` ``viewer`` APARTE, activo y logueado. Llamar a ``release_approver`` después."""
    from app.models.user_model import UserModel
    from app.utils.security import hash_password

    um = UserModel()
    if not um.find_by_username(APPROVER):
        um.create(
            {
                "username": APPROVER,
                "email": f"{APPROVER}@gateway.local",
                "hashed_password": hash_password(APPROVER_PASSWORD),
                "full_name": "Segundo aprobador (tests)",
                "notes": None,
                "is_active": True,
                "gateway_role": "viewer",
            }
        )
        um.grant_global_capabilities(APPROVER, ["access_admin"])
    else:
        _sql("UPDATE users SET is_active = 1 WHERE username = :u", u=APPROVER)
    return _login(APPROVER, APPROVER_PASSWORD)


def release_approver() -> None:
    _sql("UPDATE users SET is_active = 0 WHERE username = :u", u=APPROVER)


def approve(request_id: int):
    """Aprueba ``request_id`` con el aprobador efímero y devuelve la respuesta HTTP."""
    c = approver_client()
    try:
        return c.post(f"/api/v1/access-requests/{request_id}/approve", json={})
    finally:
        release_approver()


def settle(r) -> dict:
    """
    ``data`` de un escritor de ``/gateway-users`` con la elevación YA APLICADA.

    ``200``/``201``: tal cual. ``202``: la aprueba el segundo ``access_admin`` y devuelve la cuenta
    releída (con ``invite_token``/``invite_expires_at`` si era un alta).
    """
    if r.status_code != 202:
        return r.json()["data"]
    data = r.json()["data"]
    assert data["code"] == "access.elevation_pending", data
    ar = approve(data["pending_request"]["id"])
    assert ar.status_code == 200, ar.text
    from app.controllers.gateway_user_controller import GatewayUserController

    out = GatewayUserController().get_user(data["id"])
    for k in ("invite_token", "invite_expires_at"):
        if k in data:
            out[k] = data[k]
    return out


def create_user(admin_client, username: str, **extra) -> dict:
    """Alta por HTTP con la elevación (si la hay) aprobada. Devuelve ``data`` con la invitación."""
    payload = {"username": username, "full_name": "Persona de test", **extra}
    r = admin_client.post("/api/v1/gateway-users", json=payload)
    assert r.status_code in (201, 202), r.text
    return settle(r)


def client_as(datos: dict, username: str, password: str = _PASSWORD):
    """Acepta la invitación de ``datos`` y devuelve un client APARTE logueado como esa cuenta."""
    from fastapi.testclient import TestClient

    from main import app
    from tests.csrf_helpers import attach_csrf

    c = TestClient(app)
    r = c.post(
        "/api/v1/gateway-users/invite/accept",
        json={"token": datos["invite_token"], "password": password},
    )
    assert r.status_code == 200, r.text
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    attach_csrf(c)
    return c
