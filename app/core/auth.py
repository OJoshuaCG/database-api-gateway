"""
Autenticación del gateway: sesión firmada + administrador único.

El gateway es una herramienta interna; no gestiona múltiples usuarios. La sesión
se guarda en una cookie httpOnly firmada (Starlette SessionMiddleware, backend
itsdangerous). Toda la lógica de "quién está autenticado" pasa por `authenticated_user`, de modo que
migrar a OIDC/SSO en el futuro no requiere tocar los endpoints.

**Acá NO hay ninguna dependencia inyectable.** La que había —`AdminDep`, que solo verificaba
sesión— se retiró al terminar el swap a capacidades, y se retiró en vez de deprecarse
justamente para que un endpoint nuevo copiado de uno viejo **falle al importar** en lugar de
nacer autenticado y sin autorizar. Lo inyectable vive en `app/core/authz.py`, y ahí todo alias
lleva capacidad.
"""

from fastapi import Request

from app.core.environments import ADMIN_PASSWORD, ADMIN_USERNAME
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.services.capability_catalog import GatewayRole, GlobalCapability
from app.core import session_store
from app.models.user_model import UserModel
from app.services import audit
from app.utils.security import hash_password

logger = get_logger(__name__)

#: Mensaje por motivo de rechazo. Distingue "se venció" de "no estás autenticado" porque son
#: acciones distintas para quien lo recibe: volver a entrar vs. entender que lo echaron. Ninguno
#: revela si el `sid` existió: `unknown` y `missing` comparten texto a propósito.
_MENSAJE_401 = {
    "missing": "No autenticado.",
    "unknown": "No autenticado.",
    session_store.REASON_ABSOLUTE: "La sesión alcanzó su duración máxima. Volvé a iniciar sesión.",
    session_store.REASON_IDLE: "La sesión expiró por inactividad. Volvé a iniciar sesión.",
    session_store.REASON_LOGOUT: "La sesión se cerró. Volvé a iniciar sesión.",
    session_store.REASON_PASSWORD_CHANGE: "La contraseña cambió: las sesiones se cerraron.",
    session_store.REASON_ROLE_CHANGE: "Tus permisos cambiaron: volvé a iniciar sesión.",
    session_store.REASON_ADMIN_REVOKED: "La sesión fue revocada.",
}

#: La ÚNICA clave que viaja en la cookie. El resto —id, username, rol, capacidades— se resuelve
#: server-side contra `gateway_sessions` y `users`.
#:
#: Que sea una sola no es minimalismo: un rol en la cookie es un rol que **no se puede
#: revocar**, y la cookie se re-firma en cada respuesta. `tests/test_session_lifecycle.py`
#: decodifica el payload sin la firma y afirma que las claves son exactamente estas.
SESSION_SID = "sid"


def login_session(request: Request, user: dict) -> None:
    """
    Abre una sesión server-side y deja su ``sid`` —y nada más— en la cookie.

    El ``clear()`` va PRIMERO y no es cosmético: sin él, cualquier clave que ya estuviera en la
    sesión sobrevive al login. Con la cookie llevando solo el ``sid`` el riesgo se achica, pero
    el orden se mantiene porque acá van a vivir el marcador de reautenticación y el flag de 2FA
    pendiente, y ahí un valor plantado por el dueño anterior pasaría al dueño nuevo.

    **Cada login crea una fila nueva**, o sea que el ``sid`` ROTA: un identificador fijado por
    un atacante antes del login (session fixation) deja de servir en el instante en que la
    víctima se autentica.
    """
    request.session.clear()
    request.session[SESSION_SID] = session_store.create(
        user["id"],
        ip=(request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    )


def logout_session(request: Request) -> None:
    """
    Cierra la sesión **de verdad**: tacha la fila y después borra la cookie.

    El orden importa. Si se borrara la cookie primero y la revocación fallara, el usuario
    quedaría "deslogueado" en su navegador con una sesión que sigue viva del lado del servidor —
    o sea el modo de fallo exacto que la sesión server-side vino a cerrar, disfrazado de éxito.
    """
    sid = request.session.get(SESSION_SID)
    if sid:
        session_store.revoke(sid, session_store.REASON_LOGOUT)
    request.session.clear()


def authenticated_user(request: Request) -> dict:
    """
    La fila COMPLETA del usuario de la sesión, o 401. Es la única resolución de sesión.

    Existe extraída y no duplicada porque de acá cuelga ``get_current_actor`` de
    ``app/core/authz.py`` y de acá va a colgar la autenticación por token del servidor MCP. Dos
    chequeos de sesión paralelos son exactamente cómo se termina con dos políticas que divergen
    en silencio: el riesgo está anotado en el plan 11 §9 y esto lo cierra por construcción.

    Relee la BD en CADA request, a propósito: es lo que hace que desactivar a alguien surta
    efecto de inmediato, y lo mismo va a valer para el rol.

    OJO: devuelve la fila entera, que incluye ``hashed_password``. Quien la consuma tiene que
    ESTRECHARLA: ``get_current_actor`` la reduce a los campos del ``Actor``, que no propaga el
    hash.
    """
    sid = request.session.get(SESSION_SID)
    user_id, motivo = session_store.resolve(sid or "")
    if user_id is None:
        # La cookie se limpia SIEMPRE, incluso cuando la sesión ya estaba tachada: dejarla
        # puesta hace que el navegador reintente con un `sid` muerto en cada request y que el
        # usuario vea 401 sin entender por qué.
        request.session.clear()
        raise AppHttpException(
            message=_MENSAJE_401.get(motivo, "No autenticado."),
            status_code=401,
            public_context={"code": f"auth.session_{motivo}"} if motivo else None,
        )

    user = UserModel().find_by_id(user_id)
    if not user or not user.get("is_active"):
        # `is_active` se relee por request y ese es el kill switch que ya funcionaba. Ahora
        # además se tacha la sesión, para que el corte quede con motivo en vez de repetirse.
        session_store.revoke(sid, session_store.REASON_ADMIN_REVOKED)
        request.session.clear()
        raise AppHttpException(
            message="Sesión inválida o usuario inactivo.", status_code=401
        )
    return user


def bootstrap_admin() -> None:
    """
    Garantiza que exista **al menos un administrador de accesos activo**. Idempotente.

    ANCLADO AL INVARIANTE, NO AL USERNAME
    -------------------------------------
    La versión anterior hacía ``if find_by_username(ADMIN_USERNAME): return``, y con eso **no
    reparaba nada** en el caso que importa: si al administrador se lo renombra o se lo
    desactiva, la fila con ese username ya no existe o no sirve, pero el seed encuentra *algo*
    —o no encuentra nada y siembra un duplicado— en vez de mirar la condición que hace
    funcionar al sistema.

    El criterio nuevo es la condición misma: **si hay CERO usuarios activos con
    ``access_admin``**, sembrar o reparar. Misma idempotencia, keyed en lo que importa.

    LO QUE NUNCA HACE, Y ES LO QUE LO SEPARA DE UN BYPASS
    ----------------------------------------------------
    **Nunca toca la password ni el rol de un administrador existente.** Ésa es la línea entre
    una reparación y "la variable de entorno siempre gana", que es lo que este diseño rechaza:
    si el arranque re-afirmara rol y password, **desactivar a alguien sería reversible por
    reinicio** — y el reinicio es la operación más común del mundo. El control dejaría de
    existir y nadie se daría cuenta.

    Por eso hay dos caminos y no uno: si el usuario de ``ADMIN_USERNAME`` **no existe**, se
    siembra completo; si existe pero el invariante está roto, se **reactiva y se le devuelven
    las globales**, sin tocar su credencial. Quien tenga la password sigue siendo quien la
    tenía.

    Y se audita como ``access.bootstrap_recovery`` cuando repara —no cuando siembra un
    despliegue nuevo, que no es una recuperación—, porque una reparación de privilegio hecha
    por el arranque es exactamente el evento que alguien tiene que poder ver después.
    """
    if not ADMIN_PASSWORD:
        logger.warning(
            "ADMIN_PASSWORD no está definido; no se sembró ningún administrador."
        )
        return

    user_model = UserModel()
    if user_model.count_active_access_admins() > 0:
        # El invariante se cumple: no hay nada que hacer, ni siquiera si el username de la
        # variable de entorno no coincide con nadie. Que el administrador se llame distinto de
        # `ADMIN_USERNAME` es una situación NORMAL, no algo que el arranque deba "corregir".
        return

    existente = user_model.find_by_username(ADMIN_USERNAME)
    globales = [
        GlobalCapability.ACCESS_ADMIN.value,
        GlobalCapability.SECURITY_OFFICER.value,
    ]

    if existente:
        # Reparación: se reactiva y se le devuelven las globales. La password NO se toca.
        user_model.update(existente["id"], {"is_active": True})
        user_model.grant_global_capabilities(ADMIN_USERNAME, globales)
        audit.record(
            "access.bootstrap_recovery",
            admin=None,
            target_type="user",
            target_id=existente["id"],
            touched_engine=False,
            detail=(
                f"invariante roto (0 access_admin activos): se reactivó '{ADMIN_USERNAME}' y "
                "se le restauraron las capacidades globales. La contraseña NO se modificó"
            ),
        )
        logger.warning(
            "Recuperación de arranque: no había ningún access_admin activo; se reparó '%s'.",
            ADMIN_USERNAME,
        )
        return

    # Despliegue nuevo. El rol y las globales se fijan EXPLÍCITAMENTE y no se heredan del
    # default de la columna: `users.gateway_role` tiene `server_default='viewer'` a propósito
    # —para que ninguna fila nazca con privilegio— así que sin este bloque se sembraría un
    # administrador que no puede administrar. Y `owner` no alcanza solo: `servers.admin`,
    # `catalogs.write` y `gateway.admin` viven ÚNICAMENTE en las capacidades globales.
    user_model.create(
        {
            "username": ADMIN_USERNAME,
            "email": f"{ADMIN_USERNAME}@gateway.local",
            "hashed_password": hash_password(ADMIN_PASSWORD),
            "full_name": "Administrador",
            "notes": None,
            "is_active": True,
            "gateway_role": GatewayRole.OWNER.value,
        }
    )
    user_model.grant_global_capabilities(ADMIN_USERNAME, globales)
    logger.info("Administrador '%s' sembrado.", ADMIN_USERNAME)
