"""
Limitador de tasa: el eje es la SESIÓN, con la IP como último recurso.

POR QUÉ NO ES LA IP
-------------------
``get_remote_address`` era el eje y tiene un modo de fallo concreto ahora que hay login
multiusuario en camino: **N personas detrás de una salida NAT comparten los 3/min del
``DROP DATABASE``**. O sea que el límite que protege la operación más destructiva del gateway lo
consume el compañero de escritorio, y el afectado no tiene forma de saber por qué.

POR QUÉ LA SESIÓN Y NO EL USUARIO
---------------------------------
El eje correcto sería el ``user_id``, y no se puede: ``key_func`` corre **antes** de que se
resuelvan las dependencias del endpoint, así que el ``Actor`` todavía no existe y resolverlo acá
significaría una lectura a la BD en el limitador **más** la que va a hacer la dependencia. El
``sid``, en cambio, sale de la cookie firmada sin tocar la BD.

Lo que se pierde, declarado: **un usuario con N sesiones abiertas tiene N cupos.** Es un hueco
real y mucho más chico que el de hoy —el compartido por NAT es entre personas distintas, este es
la misma persona— y se cierra con el tope de sesiones concurrentes, que la tabla
``gateway_sessions`` ya habilita.

El fallback a la IP no es decorativo: el login **no tiene sesión todavía** —es el request que la
crea— así que su límite tiene que seguir siendo por IP, y lo mismo vale para cualquier cliente
programático sin cookie.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.environments import RATE_LIMIT_DEFAULT, RATE_LIMIT_REDIS_ENABLED, RATE_LIMIT_REDIS_URL


def session_or_address(request: Request) -> str:
    """
    ``sid:<sid>`` si hay sesión, si no la IP remota.

    Los dos espacios de claves se prefijan distinto a propósito: sin prefijo, un ``sid`` con
    forma de IP —improbable pero no imposible con base64url— colisionaría con el cupo de una
    IP real, y una colisión en un limitador es un cupo compartido entre dos actores que no
    tienen nada que ver.

    Si el ``SessionMiddleware`` no está en el stack, ``request.session`` levanta: se cae al eje
    de IP en vez de romper el request. Un limitador no puede ser el motivo de un 500.
    """
    try:
        sid = request.session.get("sid")
    except (AssertionError, KeyError):
        sid = None
    if sid:
        return f"sid:{sid}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=session_or_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri=RATE_LIMIT_REDIS_URL if RATE_LIMIT_REDIS_ENABLED else "memory://",
)
