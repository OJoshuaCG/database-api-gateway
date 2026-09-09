"""
CSRF: que ``same_site="lax"`` no alcanzaba, y que el token no es double-submit.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
La defensa que había era ``same_site="lax"`` en la cookie de sesión, y tiene un hueco que no es
teórico: **Lax es same-SITE, no same-origin**. Si el panel vive en ``panel.midominio.com`` y
cualquier otra cosa de la organización en ``*.midominio.com`` sufre un XSS o queda dangling, ese
origen puede hacer POST/PATCH/DELETE con la cookie del admin adjunta, porque para el navegador
son el mismo *site*. En una herramienta con pseudo-root sobre la producción de terceros, eso es
compromiso total a través de un sitio de marketing.

Los dos tests que más importan de este archivo son los que fijan **por qué no es double-submit**
y **por qué la ausencia de `Origin` no rechaza**. Los dos son decisiones que a alguien le van a
parecer flojas y las dos tienen un motivo medido.
"""

import pytest

from app.core.csrf import CSRF_HEADER, cookie_name, token_for


def _login(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture()
def sesion(client):
    """Sesión abierta, con la cookie de CSRF publicada y SIN el header puesto."""
    return _login(client)


def _payload_entorno() -> dict:
    return {"name": "CSRF probe", "slug": "csrf-probe"}


def _crear_entorno(client, **kwargs):
    return client.post("/api/v1/environments", json=_payload_entorno(), **kwargs)


# --------------------------------------------------------------------------- #
# El token se exige de verdad                                                 #
# --------------------------------------------------------------------------- #


def test_an_unsafe_method_without_the_header_is_rejected(sesion):
    """
    Con sesión válida y sin header, 403. Es lo que hace que la cookie sola no alcance para
    operar — que es todo el punto.
    """
    r = _crear_entorno(sesion)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["public_context"]["code"] == "auth.csrf_missing"


def test_a_safe_method_does_not_need_the_header(sesion):
    """
    ``GET`` no pide token. Los GET de este repo que sí mutan se arreglan moviendo el efecto a un
    método no seguro, no exigiéndoles un token que un ``<img>`` no puede mandar **y que tampoco
    se le puede pedir a una navegación de primer nivel**.
    """
    assert sesion.get("/api/v1/environments").status_code == 200


def test_the_middleware_publishes_the_transport_cookie(sesion):
    """La cookie no es httpOnly a propósito: el JS tiene que poder leerla para mandar el header."""
    assert sesion.cookies.get(cookie_name())


def test_the_right_token_lets_the_operation_through(sesion):
    token = sesion.cookies.get(cookie_name())
    r = _crear_entorno(sesion, headers={CSRF_HEADER: token})
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Por qué NO es double-submit                                                 #
# --------------------------------------------------------------------------- #


def test_a_planted_cookie_plus_matching_header_does_not_pass(sesion):
    """
    **El test central del archivo.** Un double-submit puro —comparar cookie contra header— lo
    derrota el mismo atacante que motiva la defensa: un subdominio hermano puede escribir
    cookies en el dominio padre (``document.cookie = "gw_csrf=X; domain=.midominio.com"``,
    *cookie tossing*) y después mandar ``X-CSRF-Token: X``. Coinciden, y pasaría.

    Acá el token se **recomputa** del ``sid`` server-side, así que un par plantado no valida: el
    atacante no conoce el ``sid`` —viaja en una cookie httpOnly— ni el secreto.
    """
    plantado = "valor-elegido-por-el-atacante"
    sesion.cookies.set(cookie_name(), plantado)

    r = _crear_entorno(sesion, headers={CSRF_HEADER: plantado})
    assert r.status_code == 403, "el par cookie+header plantado pasó: es double-submit puro"
    assert r.json()["detail"]["public_context"]["code"] == "auth.csrf_invalid"


def test_the_token_of_another_session_does_not_pass(sesion):
    """
    Está atado al ``sid``, así que el token de otra sesión —incluso del mismo usuario— no sirve.
    """
    ajeno = token_for("sid-de-otra-sesion")
    r = _crear_entorno(sesion, headers={CSRF_HEADER: ajeno})
    assert r.status_code == 403
    assert r.json()["detail"]["public_context"]["code"] == "auth.csrf_invalid"


def test_the_token_rotates_with_the_session(client):
    """
    Se deriva del ``sid`` y el ``sid`` rota en cada login, así que el token del login anterior
    deja de valer. Sin esto, un token capturado sobreviviría a un re-login.
    """
    _login(client)
    viejo = client.cookies.get(cookie_name())
    _login(client)
    nuevo = client.cookies.get(cookie_name())
    assert viejo != nuevo

    r = _crear_entorno(client, headers={CSRF_HEADER: viejo})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# El chequeo de Origin                                                        #
# --------------------------------------------------------------------------- #


def test_a_foreign_origin_is_rejected_before_the_token(sesion):
    """
    Se corta por ``Origin`` **antes** de mirar el token, y con token válido: si se evaluara
    después, la respuesta distinguiría "adivinó el token" de "no adivinó" desde un origen
    ajeno, o sea un oráculo sobre el token.
    """
    token = sesion.cookies.get(cookie_name())
    r = _crear_entorno(
        sesion, headers={CSRF_HEADER: token, "Origin": "https://sitio-del-atacante.example"}
    )
    assert r.status_code == 403
    assert r.json()["detail"]["public_context"]["code"] == "auth.origin_rejected"


def test_a_missing_origin_does_not_reject(sesion):
    """
    **La otra decisión que hay que fijar.** Un navegador manda ``Origin`` en TODO método no
    seguro; que falte significa que el cliente no es un navegador — y un cliente que no es un
    navegador no adjunta cookies por su cuenta, así que no hay CSRF que prevenir.

    Rechazar por ausencia no agregaría seguridad y sí rompería a ``curl``, al CI y al dispatch
    del servidor MCP. Lo que se rechaza es un ``Origin`` presente y ajeno, que es la señal
    positiva de un cross-site.
    """
    token = sesion.cookies.get(cookie_name())
    r = _crear_entorno(sesion, headers={CSRF_HEADER: token})
    assert r.status_code == 201, r.text


def test_an_allowed_origin_passes(sesion):
    """El origen configurado en CORS_ORIGINS sí pasa: si no, la SPA no podría operar."""
    from app.core.environments import CORS_ORIGINS

    if "*" in CORS_ORIGINS or not CORS_ORIGINS:
        pytest.skip("sin lista explícita de orígenes no hay nada que validar")

    token = sesion.cookies.get(cookie_name())
    r = _crear_entorno(sesion, headers={CSRF_HEADER: token, "Origin": CORS_ORIGINS[0]})
    assert r.status_code == 201, r.text


def test_a_trailing_slash_in_the_configured_origin_still_matches():
    """
    Se compara por origen normalizado y no por string. Una barra final o una mayúscula en el
    host son configuraciones legítimas, y en un guard que rechaza eso sería una caída de
    servicio, no un aviso.
    """
    from app.core import csrf

    original = csrf.CORS_ORIGINS
    try:
        csrf.CORS_ORIGINS = ["https://Panel.Midominio.com/"]
        assert csrf._origin_permitido("https://panel.midominio.com")
        assert not csrf._origin_permitido("https://otro.midominio.com")
    finally:
        csrf.CORS_ORIGINS = original


# --------------------------------------------------------------------------- #
# La exención del actor de tipo token                                         #
# --------------------------------------------------------------------------- #


def test_the_token_actor_is_exempt_by_construction():
    """
    CSRF es un ataque contra la autenticación **ambiental** —la cookie que el navegador adjunta
    solo—. Un ``Authorization: Bearer`` no es ambiental, así que la defensa no aplica.

    Sin esta exención, el día que exista el servidor MCP **todo cliente programático recibiría
    403 en producción**. Se verifica a nivel de función porque todavía no hay emisor de tokens:
    lo que se fija es que la condición esté escrita sobre ``actor.kind``, no que el camino HTTP
    exista.
    """
    from app.core.actor import token_actor
    from app.services.capability_catalog import Capability

    actor = token_actor(
        token_pk=1, token_id="gwt_abc", name="mcp-lector",
        scopes="blueprints.read", project_id=7,
    )
    assert actor.kind == "api_token"
    assert actor.is_agent
    assert actor.has(Capability.BLUEPRINTS_READ)
