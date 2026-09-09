"""
Ciclo de vida de la sesión: vida absoluta, inactividad, logout real y qué viaja en la cookie.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Los cuatro controles que se prueban acá **eran inimplementables** hasta que la sesión pasó al
servidor, y la razón está medida: el ``SessionMiddleware`` de Starlette re-firma la cookie con
timestamp nuevo en CADA respuesta, así que ``SESSION_MAX_AGE`` era timeout de inactividad puro
—con actividad continua la sesión no expiraba nunca— y ``session.clear()`` borraba la cookie del
cliente sin invalidar nada.

Ninguno de esos cuatro se podía afirmar con un test antes, porque no había nada que afirmar. El
test de la cookie es el más importante de todos: es lo que impide que alguien "optimice" una
consulta guardando el rol ahí y con eso vuelva a un rol irrevocable.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.database import Database
from app.models.gateway_session import GatewaySession


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _login(client) -> None:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text


def _filas() -> list[GatewaySession]:
    s = Database().get_declarative_base_session()
    try:
        return s.query(GatewaySession).order_by(GatewaySession.created_at).all()
    finally:
        s.close()


def _envejecer(columna: str, delta: timedelta) -> None:
    """Mueve un timestamp de TODAS las sesiones hacia atrás. El reloj no se puede mover."""
    with Database().engine.begin() as conn:
        conn.execute(
            text(f"UPDATE gateway_sessions SET {columna} = :t"),
            {"t": _utcnow() - delta},
        )


# --------------------------------------------------------------------------- #
# Lo que viaja en la cookie                                                   #
# --------------------------------------------------------------------------- #


def test_the_cookie_carries_only_the_sid(client):
    """
    Guard ESTRUCTURAL: se decodifica el payload de la cookie **sin verificar la firma** y se
    afirma que las claves son exactamente ``{"sid"}``.

    Es el test que protege la propiedad de la que dependen todas las demás: si mañana alguien
    guarda el rol o las capacidades ahí "para ahorrar una consulta", vuelve a existir un rol que
    no se puede revocar — porque la cookie se re-firma en cada respuesta y una sesión activa no
    caduca sola.
    """
    import base64
    import json

    _login(client)
    cruda = client.cookies.get("gw_session")
    assert cruda, "no se seteó la cookie de sesión"

    # itsdangerous: <payload-b64>.<timestamp>.<firma>. Solo interesa el payload.
    payload_b64 = cruda.split(".")[0]
    relleno = "=" * (-len(payload_b64) % 4)
    datos = json.loads(base64.urlsafe_b64decode(payload_b64 + relleno))

    assert set(datos) == {"sid"}, f"la cookie lleva más que el sid: {sorted(datos)}"
    assert isinstance(datos["sid"], str) and len(datos["sid"]) >= 20


# --------------------------------------------------------------------------- #
# Logout de verdad                                                            #
# --------------------------------------------------------------------------- #


def test_logout_revokes_the_row_not_just_the_cookie(client):
    """
    El control que antes no existía: ``session.clear()`` borraba la cookie **del cliente**, así
    que quien tuviera una copia seguía autenticado.
    """
    _login(client)
    sid = _filas()[-1].sid

    assert client.post("/api/v1/auth/logout").status_code == 200

    s = Database().get_declarative_base_session()
    try:
        fila = s.get(GatewaySession, sid)
        assert fila.revoked_at is not None, "el logout no tachó la fila"
        assert fila.revoked_reason == "logout"
    finally:
        s.close()


def test_a_copied_cookie_stops_working_after_logout(client):
    """
    La prueba de que el logout sirve **contra una copia**, que es el escenario real: se guarda
    la cookie, se cierra sesión, se vuelve a poner la cookie guardada.
    """
    _login(client)
    copia = client.cookies.get("gw_session")

    assert client.post("/api/v1/auth/logout").status_code == 200

    client.cookies.set("gw_session", copia)
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401, "la cookie copiada sigue autenticando después del logout"


# --------------------------------------------------------------------------- #
# Los dos vencimientos                                                        #
# --------------------------------------------------------------------------- #


def test_the_absolute_lifetime_expires_even_with_activity(client):
    """
    El control que la cookie firmada hacía imposible: con actividad continua **no expiraba
    nunca**. Se envejece ``created_at`` sin tocar ``last_seen_at``, que es exactamente el caso
    de una sesión que se estuvo usando.
    """
    _login(client)
    assert client.get("/api/v1/auth/me").status_code == 200

    _envejecer("created_at", timedelta(hours=13))
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["detail"]["public_context"]["code"] == "auth.session_absolute"


def test_the_idle_timeout_expires(client):
    _login(client)
    _envejecer("last_seen_at", timedelta(minutes=61))
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["detail"]["public_context"]["code"] == "auth.session_idle"


def test_an_expired_session_is_revoked_with_its_reason(client):
    """
    Vencer **tacha la fila**, no solo devuelve 401. Si no se tachara, el mismo ``sid`` volvería
    a intentarlo en cada request y el motivo del corte no quedaría en ninguna parte.
    """
    _login(client)
    sid = _filas()[-1].sid
    _envejecer("last_seen_at", timedelta(minutes=61))
    assert client.get("/api/v1/auth/me").status_code == 401

    s = Database().get_declarative_base_session()
    try:
        assert s.get(GatewaySession, sid).revoked_reason == "idle"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Rotación y aislamiento entre sesiones                                       #
# --------------------------------------------------------------------------- #


def test_each_login_rotates_the_sid(client):
    """
    Contra session fixation: un ``sid`` que un atacante haya fijado antes del login deja de
    servir en el instante en que la víctima se autentica, porque la cookie pasa a llevar otro.
    """
    _login(client)
    primero = client.cookies.get("gw_session")
    _login(client)
    segundo = client.cookies.get("gw_session")
    assert primero != segundo
    assert len(_filas()) == 2, "el segundo login reutilizó la fila en vez de crear una nueva"


def test_deactivating_the_user_revokes_the_session(client):
    """
    ``is_active`` ya era el único kill switch real y se releía por request. Lo que se suma es
    que ahora el corte **deja motivo** en vez de repetirse en silencio.
    """
    _login(client)
    sid = _filas()[-1].sid
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE username = 'admin'"))

    assert client.get("/api/v1/auth/me").status_code == 401

    s = Database().get_declarative_base_session()
    try:
        assert s.get(GatewaySession, sid).revoked_at is not None
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Los dos endpoints que la tabla habilita                                     #
# --------------------------------------------------------------------------- #


def test_listing_own_sessions_never_returns_a_full_sid(admin_client):
    """
    El ``sid`` **es** la credencial de sesión: en un cuerpo de respuesta termina en los logs del
    proxy y al alcance de cualquier XSS. Solo viaja un prefijo.
    """
    r = admin_client.get("/api/v1/auth/sessions")
    assert r.status_code == 200, r.text
    filas = r.json()["data"]
    assert len(filas) == 1
    assert filas[0]["current"] is True
    assert len(filas[0]["sid_prefix"]) == 8

    sid_real = _filas()[-1].sid
    assert sid_real not in r.text, "el sid completo viajó en la respuesta"


def test_revoke_others_keeps_the_current_session(client):
    """
    No echarse a sí mismo no es comodidad: quien pide esto está reaccionando a algo que vio en
    el listado, y perder la sesión actual lo deja sin poder seguir.
    """
    _login(client)
    vieja = client.cookies.get("gw_session")
    _login(client)  # la actual

    r = client.post("/api/v1/auth/sessions/revoke-others")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["revoked"] == 1

    # La actual sigue sirviendo…
    assert client.get("/api/v1/auth/me").status_code == 200
    # …y la otra no.
    client.cookies.set("gw_session", vieja)
    assert client.get("/api/v1/auth/me").status_code == 401
