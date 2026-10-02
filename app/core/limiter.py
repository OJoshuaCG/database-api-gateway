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

Lo que se pierde, declarado: **un usuario con N sesiones abiertas tiene N cupos.** El ``sid``
sale de la cookie firmada pero **no se verifica que siga viva**, así que una sesión revocada o
vencida sigue siendo una clave válida del limitador mientras su firma no expire. Es un hueco real
y mucho más chico que el de NAT —aquel es entre personas distintas, este es la misma persona—
pero **no está cerrado**: no existe todavía un tope de sesiones concurrentes por usuario.

LOS ENDPOINTS PÚBLICOS NO PUEDEN USAR ESTE EJE
----------------------------------------------
Justamente porque el ``sid`` no se verifica, en un endpoint que no exige sesión el ``sid`` es un
valor que el cliente elige: quien junte N cookies (cada login exitoso emite una nueva) obtiene N
cupos. Por eso ``/auth/login`` y ``/gateway-users/invite/accept`` se limitan con
``client_address`` y nunca con ``session_or_address``. Ver ``enforce_login_limits``.
"""

from hashlib import sha256

from fastapi import Request
from limits import parse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit

from app.core.environments import (
    LOGIN_USERNAME_RATE_LIMIT,
    MCP_RATE_LIMIT,
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_REDIS_ENABLED,
    RATE_LIMIT_REDIS_URL,
)


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


def client_address(request: Request) -> str:
    """
    ``ip:<ip>`` y nada más. El eje de los endpoints PÚBLICOS: ver el docstring del módulo.

    La IP es la del socket, o la de ``X-Forwarded-For`` solo cuando el request viene de un proxy
    listado en ``TRUSTED_PROXY_IPS`` (lo resuelve uvicorn antes de llegar acá).
    """
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=session_or_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri=RATE_LIMIT_REDIS_URL if RATE_LIMIT_REDIS_ENABLED else "memory://",
)


def hit_or_429(lim: Limiter, rate: str, *identifiers: str) -> None:
    """
    Consume un intento del cupo ``rate`` para la clave ``identifiers`` o levanta el 429 estándar.

    Para los límites que el decorador de SlowAPI no puede expresar porque su clave sale del
    CUERPO (el ``key_func`` corre sin el body parseado). ``hit`` es atómico —incrementa y compara
    en una operación del storage—, así que N requests concurrentes no pasan todas un ``test``
    previo: un "consultar y después contar" dejaría pasar una ráfaga del tamaño del threadpool.

    Levanta ``RateLimitExceeded`` y no un ``AppHttpException`` para que el 429 salga por el mismo
    handler y con el mismo cuerpo que el resto de los límites: el cliente no tiene que distinguir
    de qué capa vino.

    Respeta ``lim.enabled``: la suite apaga el limitador y un límite manual que lo ignorara
    devolvería 429 en tests que no tienen nada que ver.
    """
    if not lim.enabled:
        return
    item = parse(rate)
    if not lim.limiter.hit(item, *identifiers):
        raise RateLimitExceeded(
            Limit(
                item,
                key_func=lambda: "/".join(identifiers),
                scope=None,
                per_method=False,
                methods=None,
                error_message=None,
                exempt_when=None,
                cost=1,
                override_defaults=False,
            )
        )


#: Cupo de login por IP. Más holgado que el de IP+usuario a propósito: es el que comparten N
#: personas detrás de una salida NAT, y lo que frena la fuerza bruta contra UNA cuenta es el de
#: abajo, no este.
LOGIN_IP_RATE_LIMIT = "20/minute"
#: Cupo de login por (IP, usuario normalizado): el control principal contra adivinar la password
#: de una cuenta desde un origen.
LOGIN_IP_USERNAME_RATE_LIMIT = "5/minute"


def _username_key(username: str) -> str:
    """
    El username NORMALIZADO y hasheado, para usarlo como clave del limitador.

    **Normalizado** (``strip`` + ``casefold``) porque la comparación de la BD lo es: con la
    colación de MySQL/MariaDB ``Admin``, ``admin`` y ``admin `` son la misma fila, así que sin
    normalizar cada variante sería un cupo nuevo contra la misma cuenta.

    **Hasheado** porque la clave termina en Redis cuando está habilitado, y un listado de
    usernames intentados en un storage sin auditoría es justo lo que el login se cuida de no
    filtrar. Además neutraliza el ``/`` que ``limits`` usa como separador de clave.
    """
    return sha256(username.strip().casefold().encode("utf-8")).hexdigest()[:32]


def enforce_login_limits(request: Request, username: str) -> None:
    """
    Los límites de ``/auth/login`` que dependen del username. Se llama ANTES de verificar la
    password, así que un intento que excede no llega a Argon2 ni a la BD.

    Tres ejes, y ninguno es el ``sid`` (ver el docstring del módulo):

    1. **IP** — lo pone el decorador de la ruta (``LOGIN_IP_RATE_LIMIT``).
    2. **IP + usuario** (``LOGIN_IP_USERNAME_RATE_LIMIT``) — frena adivinar desde un origen.
    3. **Usuario, sin IP** (``LOGIN_USERNAME_RATE_LIMIT``) — lo único que frena un ataque
       DISTRIBUIDO, donde cada IP tiene su propio cupo de (1) y (2).

    Cuentan **todos los intentos**, no solo los fallidos: contar solo fallos exige consultar
    antes y contar después, y esa ventana deja pasar ráfagas concurrentes (ver ``hit_or_429``).
    Un usuario legítimo entra pocas veces por hora, así que el costo es nulo en la práctica.

    EL COSTO DE (3), DECLARADO: es un **DoS de bloqueo**. Quien conozca un username (``admin``
    es el del seed) puede gastarle el cupo con passwords basura y dejar a su dueño afuera hasta
    que la ventana se vacíe. Se acepta porque la alternativa —sin tope por cuenta— es fuerza
    bruta distribuida sin techo contra una cuenta que administra producción ajena, y porque el
    bloqueo se disuelve solo. Si el DoS está ocurriendo, ``LOGIN_USERNAME_RATE_LIMIT`` vacío lo
    apaga sin desplegar. No es un lockout de cuenta: no hay estado persistido ni desbloqueo
    manual, y esa decisión de producto queda abierta.
    """
    ip = get_remote_address(request)
    usuario = _username_key(username)
    hit_or_429(limiter, LOGIN_IP_USERNAME_RATE_LIMIT, "login", "ip_user", ip, usuario)
    if LOGIN_USERNAME_RATE_LIMIT:
        hit_or_429(limiter, LOGIN_USERNAME_RATE_LIMIT, "login", "user", usuario)


def agent_token_key(request) -> str:
    """
    Clave del límite de tasa del endpoint MCP: el ``token_id``, y la IP como último recurso.

    **Por token y no por IP**, y es una diferencia de comportamiento, no de estilo: un agente
    corriendo en CI comparte IP con todos los demás jobs del runner, así que un límite por IP
    sería colectivo — el primero en gastarlo deja afuera al resto, y el operador no tiene forma
    de entender por qué.

    Se lee del header y **sin verificar el HMAC**, porque el ``key_func`` corre antes de las
    dependencias. La consecuencia, que la versión anterior de este docstring negaba: **este
    límite solo acota a quien tiene un token real.** Un atacante que inventa un ``token_id``
    distinto por request estrena un cupo completo en cada uno, así que contra él este límite no
    hace nada. Lo que lo frena es el tope de rechazos POR IP de ``mcp_auth.authenticate_agent``
    (``MCP_AUTH_FAILURE_RATE_LIMIT``), que corre antes de tocar la BD.
    """
    crudo = request.headers.get("authorization") or ""
    if crudo.lower().startswith("bearer "):
        partes = crudo[7:].strip().split(".")
        if len(partes) == 3 and partes[0] == "dbgw" and partes[1]:
            return f"agent:{partes[1]}"
    return f"ip:{get_remote_address(request)}"


#: Limitador propio del endpoint MCP. Instancia aparte y no el `limiter` de la API porque el eje
#: es otro —el token en vez de la sesión— y compartir instancia haría que un agente y un humano
#: se pisaran el cupo.
mcp_limiter = Limiter(
    key_func=agent_token_key,
    default_limits=[MCP_RATE_LIMIT],
    storage_uri=RATE_LIMIT_REDIS_URL if RATE_LIMIT_REDIS_ENABLED else "memory://",
)
