"""
El eje del límite de tasa: la sesión, no la IP.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
``get_remote_address`` era el eje, y con login multiusuario eso significa que **N personas detrás
de una salida NAT comparten los 3/min del ``DROP DATABASE``**: el límite que protege la operación
más destructiva del gateway lo consume el compañero de escritorio, y el afectado no tiene forma de
saber por qué.

Los tests corren con el limitador HABILITADO a mano, porque ``conftest`` lo apaga para toda la
suite (si no, los tests se pisarían entre sí con 429). Se vuelve a apagar en el ``finally``: si un
test deja el limitador prendido, los que corran después fallan de formas que no tienen nada que
ver con lo que prueban.
"""

import pytest

from app.core.limiter import limiter, session_or_address


class _Pedido:
    """Request mínimo: lo que el ``key_func`` mira y nada más."""

    def __init__(self, session, host="203.0.113.7"):
        self._session = session
        self.client = type("C", (), {"host": host})()
        self.headers = {}
        self.scope = {"client": (host, 0)}

    @property
    def session(self):
        if self._session is None:
            raise AssertionError("SessionMiddleware must be installed")
        return self._session


# --------------------------------------------------------------------------- #
# La clave                                                                    #
# --------------------------------------------------------------------------- #


def test_a_session_keys_by_sid():
    assert session_or_address(_Pedido({"sid": "abc123"})) == "sid:abc123"


def test_without_a_session_it_falls_back_to_the_address():
    """
    El login **no tiene sesión todavía** —es el request que la crea— así que su límite tiene que
    seguir siendo por IP. Lo mismo vale para cualquier cliente programático sin cookie.
    """
    assert session_or_address(_Pedido({})) == "ip:203.0.113.7"


def test_the_two_key_spaces_are_prefixed_apart():
    """
    Sin prefijo, un ``sid`` con forma de IP colisionaría con el cupo de una IP real — y una
    colisión en un limitador es un cupo compartido entre dos actores que no tienen nada que ver.
    """
    como_ip = session_or_address(_Pedido({"sid": "203.0.113.7"}))
    la_ip = session_or_address(_Pedido({}))
    assert como_ip != la_ip


def test_a_missing_session_middleware_does_not_break_the_request():
    """Un limitador no puede ser el motivo de un 500: se cae al eje de IP."""
    assert session_or_address(_Pedido(None)) == "ip:203.0.113.7"


# --------------------------------------------------------------------------- #
# El efecto extremo a extremo                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def con_limitador():
    """Enciende el limitador y lo vuelve a apagar SIEMPRE."""
    limiter.enabled = True
    limiter.reset()
    try:
        yield
    finally:
        limiter.enabled = False
        limiter.reset()


def test_two_sessions_do_not_share_the_quota(client, con_limitador):
    """
    **El test que fija el arreglo.** Las dos sesiones salen de la MISMA IP —es el mismo
    ``TestClient``— así que con el eje viejo la segunda habría heredado el cupo agotado de la
    primera. Es exactamente el caso de dos personas detrás de una NAT.
    """
    from fastapi.testclient import TestClient

    from main import app

    def sesion_nueva():
        # Sin el gestor de contexto a propósito: el lifespan ya lo corrió la fixture `client`,
        # y volver a levantarlo re-sembraría el esquema a mitad del test.
        c = TestClient(app)
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200, r.text
        return c

    primera = sesion_nueva()
    # `/auth/sessions` no tiene límite propio, así que cae en el default: se agota rápido
    # sin depender de un endpoint que toque el motor.
    agotado = False
    for _ in range(200):
        if primera.get("/api/v1/auth/sessions").status_code == 429:
            agotado = True
            break
    assert agotado, "no se pudo agotar el cupo de la primera sesión"

    segunda = sesion_nueva()
    r = segunda.get("/api/v1/auth/sessions")
    assert r.status_code != 429, "la segunda sesión heredó el cupo de la primera: el eje sigue siendo la IP"


# --------------------------------------------------------------------------- #
# El login: nunca por sid, siempre por IP, IP+usuario y usuario               #
# --------------------------------------------------------------------------- #

_LOGIN = "/api/v1/auth/login"


def _intento(c, username="admin", password="incorrecta", ip=None):
    headers = {"x-test-ip": ip} if ip else {}
    return c.post(_LOGIN, json={"username": username, "password": password}, headers=headers)


@pytest.fixture()
def ip_por_header(monkeypatch):
    """
    El ``TestClient`` sale siempre de la misma IP. Para simular orígenes distintos se lee la IP
    de un header de test — en el MÓDULO del limitador, que es de donde la toman tanto el
    decorador como ``enforce_login_limits``.
    """
    import app.core.limiter as limiter_mod

    real = limiter_mod.get_remote_address
    monkeypatch.setattr(
        limiter_mod,
        "get_remote_address",
        lambda request: request.headers.get("x-test-ip") or real(request),
    )


def test_rotating_session_cookies_does_not_multiply_login_attempts(client):
    """
    **El test que fija F-27.** Antes el login se limitaba con ``session_or_address``, que lee el
    ``sid`` de la cookie sin verificarlo: con N cookies (cada login exitoso emite una) se tenían
    5·N intentos por minuto contra cualquier cuenta. Ahora la cookie no cuenta para nada.
    """
    from fastapi.testclient import TestClient

    from main import app

    # Las cookies se juntan con el limitador APAGADO: juntar no es lo que se prueba.
    clientes = []
    for _ in range(4):
        c = TestClient(app)
        assert _intento(c, password="admin123").status_code == 200
        clientes.append(c)

    limiter.enabled = True
    limiter.reset()
    try:
        respuestas = [_intento(clientes[i % 4]).status_code for i in range(8)]
    finally:
        limiter.enabled = False
        limiter.reset()

    assert respuestas[:5] == [401] * 5
    assert respuestas[5:] == [429] * 3, f"las cookies multiplicaron el cupo: {respuestas}"


def test_login_is_limited_per_ip_and_username(client, con_limitador):
    """
    El cupo de IP+usuario es por CUENTA: agotarlo contra ``admin`` no le cierra el login a otra
    cuenta desde la misma IP (la NAT de una oficina), y las variantes de mayúsculas y espacios
    son la misma cuenta, porque para la colación de la BD lo son.
    """
    variantes = ["admin", "ADMIN", " Admin ", "admin", "aDmIn"]
    assert [_intento(client, u).status_code for u in variantes] == [401] * 5
    r = _intento(client, "admin")
    assert r.status_code == 429, r.text
    # Ni la password correcta pasa: el límite corre ANTES de verificarla.
    assert _intento(client, "admin", "admin123").status_code == 429

    assert _intento(client, "otra-cuenta").status_code == 401


def test_login_is_limited_per_username_across_ips(client, con_limitador, ip_por_header, monkeypatch):
    """
    El ataque DISTRIBUIDO: cada IP tiene su propio cupo de IP e IP+usuario, así que lo único que
    lo frena es el tope por cuenta, sin IP.
    """
    import app.core.limiter as limiter_mod

    monkeypatch.setattr(limiter_mod, "LOGIN_USERNAME_RATE_LIMIT", "3/hour")
    codigos = [_intento(client, ip=f"198.51.100.{i}").status_code for i in range(4)]
    assert codigos == [401, 401, 401, 429], codigos
    # Otra cuenta no comparte ese tope.
    assert _intento(client, "otra-cuenta", ip="198.51.100.9").status_code == 401


def test_the_per_username_limit_can_be_turned_off(client, con_limitador, ip_por_header, monkeypatch):
    """La válvula para cuando el DoS de bloqueo que trae el tope por cuenta esté ocurriendo."""
    import app.core.limiter as limiter_mod

    monkeypatch.setattr(limiter_mod, "LOGIN_USERNAME_RATE_LIMIT", "")
    codigos = [_intento(client, ip=f"198.51.100.{i}").status_code for i in range(6)]
    assert codigos == [401] * 6, codigos


def test_login_is_limited_per_ip_across_usernames(client, con_limitador, monkeypatch):
    """Desde una misma IP, rotar usernames no da cupo infinito: el decorador lo corta por IP."""
    from app.core.limiter import LOGIN_IP_RATE_LIMIT

    tope = int(LOGIN_IP_RATE_LIMIT.split("/")[0])
    codigos = [_intento(client, f"usuario{i}").status_code for i in range(tope + 1)]
    assert codigos[:tope] == [401] * tope
    assert codigos[tope] == 429
