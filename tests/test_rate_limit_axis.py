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
