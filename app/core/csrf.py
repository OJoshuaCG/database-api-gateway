"""
CSRF: token derivado del ``sid``, recomputado del lado del servidor.

POR QUÉ ``same_site="lax"`` NO ALCANZA
--------------------------------------
Dos huecos, los dos verificados contra la configuración de este repo:

**(a) Lax es same-SITE, no same-origin.** Si el panel vive en ``panel.midominio.com`` y cualquier
otra cosa de la organización en ``*.midominio.com`` sufre un XSS o queda dangling, **ese origen
puede hacer POST/PATCH/DELETE con la cookie del admin adjunta**: para el navegador son el mismo
*site*. Para una herramienta con pseudo-root sobre la producción de terceros, eso es compromiso
total a través de un sitio de marketing.

**(b) Hay GETs que MUTAN.** ``GET /database-exports/{id}/download`` con
``EXPORT_SINGLE_USE_DOWNLOAD`` consume y borra el artefacto, y Lax **sí** manda la cookie en una
navegación GET de primer nivel. Eso NO lo arregla un token: lo arregla mover el consumo a un
método no seguro, y va aparte.

POR QUÉ NO ES DOUBLE-SUBMIT
---------------------------
Comparar una cookie contra un header lo derrota **el mismo atacante que motiva la defensa**: un
subdominio hermano puede escribir cookies en el dominio padre
(``document.cookie = "gw_csrf=X; domain=.midominio.com"``, *cookie tossing*) y después mandar
``X-CSRF-Token: X``. Coinciden, y pasa.

Acá el token es ``HMAC(SESSION_SECRET, sid)`` y se **recomputa** en cada request a partir del
``sid`` de la sesión. Un valor plantado no valida: el atacante no conoce el ``sid`` —viaja en una
cookie httpOnly— ni el secreto. La cookie ``__Host-gw_csrf`` (no httpOnly, ``SameSite=Strict``,
``Secure``) es **solo transporte** para que el JS lo lea, y el prefijo ``__Host-`` impide el
tossing de entrada. **Las dos cosas, no una.**

POR QUÉ SOLO PARA EL ACTOR DE TIPO ``admin``
--------------------------------------------
CSRF es un ataque contra la autenticación **ambiental**: la cookie que el navegador adjunta solo,
sin que el código del atacante tenga que leerla. Un ``Authorization: Bearer`` no es ambiental, así
que las defensas CSRF **no aplican y no deben aplicarse** a esos requests. Sin esa condición, el
día que se enciende **todo cliente programático recibe 403 en producción**: el CI, ``curl``, y el
dispatch del servidor MCP del plan 12.

POR QUÉ LA AUSENCIA DE ``Origin`` NO RECHAZA
--------------------------------------------
Un navegador manda ``Origin`` en **todo** método no seguro; que falte significa que el cliente no
es un navegador, y un cliente que no es un navegador no adjunta cookies por su cuenta. Rechazar
por ausencia no agregaría seguridad y sí rompería a `curl`, al CI y a cualquier script. Lo que se
rechaza es un ``Origin`` **presente y ajeno**, que es la señal positiva de un cross-site.
"""

from hmac import compare_digest, new as hmac_new
from hashlib import sha256
from urllib.parse import urlsplit

from fastapi import Request

from app.core.environments import APP_ENV, CORS_ORIGINS, SESSION_COOKIE_SECURE, SESSION_SECRET
from app.exceptions import AppHttpException

#: Métodos que no mutan. ``GET`` está acá por convención HTTP; los GET de este repo que sí mutan
#: se arreglan moviendo el efecto a un POST, no exigiéndoles token — un token en un `<img>` no se
#: puede mandar, pero tampoco se puede pedir en una navegación de primer nivel.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Nombre del header que la SPA manda. Un header custom ya obliga a un preflight CORS para
#: cualquier origen ajeno, que es una segunda barrera independiente del token.
CSRF_HEADER = "X-CSRF-Token"

CODE_CSRF_MISSING = "auth.csrf_missing"
CODE_CSRF_INVALID = "auth.csrf_invalid"
CODE_ORIGIN_REJECTED = "auth.origin_rejected"


def cookie_name() -> str:
    """
    ``__Host-gw_csrf`` con TLS, ``gw_csrf`` sin él.

    Mismo criterio que la cookie de sesión: el prefijo ``__Host-`` exige ``Secure``, así que sin
    TLS el navegador **descartaría** la cookie y el JS no tendría de dónde leer el token.
    """
    return "__Host-gw_csrf" if SESSION_COOKIE_SECURE else "gw_csrf"


def token_for(sid: str) -> str:
    """
    ``HMAC-SHA256(SESSION_SECRET, sid)`` en hex.

    Derivado y no aleatorio a propósito: al ser una función del ``sid``, **no hay estado que
    guardar** —ni una tabla, ni una entrada en la sesión— y se recomputa idéntico en cualquier
    worker. Un token aleatorio por sesión habría necesitado su propia fila y su propia
    invalidación.
    """
    return hmac_new(SESSION_SECRET.encode(), sid.encode(), sha256).hexdigest()


def _origin_permitido(origin: str) -> bool:
    """
    ``True`` si el origin está en ``CORS_ORIGINS``.

    Compara por origen normalizado (esquema + host + puerto) y no por string: ``CORS_ORIGINS``
    puede traer una barra final o mayúsculas en el host, y una comparación literal rechazaría un
    origen legítimo — que en un guard fail-closed es una caída de servicio, no un aviso.
    """
    if "*" in CORS_ORIGINS:
        # Con comodín no hay lista contra la que validar. En producción el arranque ya lo
        # rechaza; en desarrollo esto no puede ser el guard que decida.
        return True

    def norm(u: str) -> tuple[str, str, str]:
        p = urlsplit(u.strip().rstrip("/"))
        return (p.scheme.lower(), p.hostname or "", str(p.port or ""))

    objetivo = norm(origin)
    return any(norm(permitido) == objetivo for permitido in CORS_ORIGINS)


def enforce(request: Request, sid: str) -> None:
    """
    Exige token y ``Origin`` aceptable en un método no seguro. Llamar SOLO con actor ``admin``.

    El orden es deliberado: primero ``Origin`` y después el token. Un cross-site detectado por
    origen no debería llegar a producir un mensaje distinto según si adivinó o no el token —eso
    sería un oráculo sobre el token—, así que se corta antes.
    """
    if request.method.upper() in SAFE_METHODS:
        return
    enforce_regardless_of_method(request, sid)


def enforce_regardless_of_method(request: Request, sid: str) -> None:
    """
    Lo mismo, **sin la exención por método**. Para un GET que muta y que el cliente sí puede
    llamar con un header.

    Existe porque ``GET`` está exento por convención HTTP, no por seguridad, y en este repo hay
    endpoints que la violan: ``GET /database-exports/{id}/content`` **consume y borra el
    artefacto**. La SPA lo pide con ``fetch`` (necesita el cuerpo para el portapapeles), así que
    puede mandar el header y la exención no le hace falta.

    Su hermano ``/download`` NO puede usar esto: se abre como navegación, y a una navegación de
    primer nivel no se le puede pedir un header. Ése se arregla con un ticket.
    """
    origin = request.headers.get("origin")
    if origin and not _origin_permitido(origin):
        # `context` (solo en desarrollo) lleva el origin; `public_context` NO, porque
        # devolverle al atacante qué origen mandó no le dice nada que no sepa, pero
        # devolvérselo a un log de terceros sí.
        raise AppHttpException(
            message="Origen no permitido para esta operación.",
            status_code=403,
            public_context={"code": CODE_ORIGIN_REJECTED},
            context={"origin": origin, "app_env": APP_ENV},
        )

    enviado = request.headers.get(CSRF_HEADER)
    if not enviado:
        raise AppHttpException(
            message=(
                "Falta el token CSRF. Se lee de la cookie de CSRF y se manda en el header "
                f"{CSRF_HEADER}."
            ),
            status_code=403,
            public_context={"code": CODE_CSRF_MISSING},
        )

    # `compare_digest` y no `==`: la comparación de strings corta en el primer byte distinto y
    # eso es un oráculo de timing sobre el token. Es el mismo criterio que el del login.
    if not compare_digest(enviado, token_for(sid)):
        raise AppHttpException(
            message="Token CSRF inválido.",
            status_code=403,
            public_context={"code": CODE_CSRF_INVALID},
        )
