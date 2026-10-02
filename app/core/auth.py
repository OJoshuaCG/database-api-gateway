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

from app.core.environments import ADMIN_PASSWORD, ADMIN_RECOVERY, ADMIN_USERNAME
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
    session_store.REASON_ACCESS_ADMIN_REVOKED: (
        "Un administrador de accesos cerró tus sesiones. Volvé a iniciar sesión; si no lo "
        "esperabas, consultalo con quien administra los accesos."
    ),
    session_store.REASON_STEP_UP_FAILED: (
        "La sesión se cerró tras varios intentos fallidos de confirmar la contraseña. "
        "Volvé a iniciar sesión."
    ),
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
    """La fila COMPLETA del usuario de la sesión, o 401: ``authenticated_session`` sin la sesión."""
    return authenticated_session(request)[0]


def authenticated_session(request: Request) -> tuple[dict, session_store.SessionInfo]:
    """
    ``(fila COMPLETA del usuario, sesión)``, o 401. Es la única resolución de sesión.

    La sesión viaja junto al usuario porque de ella sale la ventana de step-up
    (``SessionInfo.step_up_at``), leída en la MISMA consulta que valida la sesión: ningún SELECT
    extra por request.

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
    info, motivo = session_store.resolve_session(sid or "")
    if info is None:
        # La cookie se limpia SIEMPRE, incluso cuando la sesión ya estaba tachada: dejarla
        # puesta hace que el navegador reintente con un `sid` muerto en cada request y que el
        # usuario vea 401 sin entender por qué.
        request.session.clear()
        raise AppHttpException(
            message=_MENSAJE_401.get(motivo, "No autenticado."),
            status_code=401,
            public_context={"code": f"auth.session_{motivo}"} if motivo else None,
        )

    user = UserModel().find_by_id(info.user_id)
    if not user or not user.get("is_active"):
        # `is_active` se relee por request y ese es el kill switch que ya funcionaba. Ahora
        # además se tacha la sesión, para que el corte quede con motivo en vez de repetirse.
        session_store.revoke(sid, session_store.REASON_ADMIN_REVOKED)
        request.session.clear()
        raise AppHttpException(
            message="Sesión inválida o usuario inactivo.", status_code=401
        )
    return user, info


def bootstrap_admin() -> None:
    """
    Siembra la cuenta inicial en una instalación VACÍA y, solo con ``ADMIN_RECOVERY=1``, la
    recupera. Idempotente. Corre en el ``lifespan`` de cada pod.

    LA SIEMBRA ES ``viewer`` + ``access_admin``, NADA MÁS (C4)
    --------------------------------------------------------
    Antes sembraba ``owner`` + ``access_admin`` + ``security_officer``: una cuenta que junta los
    tres deberes que la separación de funciones vino a separar. Ahora la cuenta inicial solo
    administra accesos, y dentro de la VENTANA DE ARRANQUE (``app/services/bootstrap_window.py``)
    crea sola a quien opera (``owner``), a quien fija política (``security_officer``) y al segundo
    ``access_admin``, que al aceptar su invitación cierra la ventana. Las instalaciones que ya
    existían conservan su cuenta combinada, heredada (``sod_exceptions``, C2): esto no la parte.

    Se siembra **solo si la tabla ``users`` está vacía**. Con usuarios y sin ningún
    ``access_admin`` utilizable, el arranque NO crea uno: eso sería ganar privilegio cambiando
    una variable y reiniciando. Lo dice en el log y pide ``ADMIN_RECOVERY=1``.

    SIN ``ADMIN_RECOVERY`` NUNCA REVIVE NI RE-ELEVA
    ----------------------------------------------
    **Nunca toca la contraseña, el estado, el rol ni las globales de una cuenta existente.** Si
    el arranque reparara solo, desactivar al administrador o quitarle ``access_admin`` sería
    reversible por reinicio —la operación más común del mundo— y el control dejaría de existir
    sin que nadie lo note. La versión anterior reparaba sola cuando había cero ``access_admin``
    activos; ahora esa reparación exige el flag explícito.

    CON ``ADMIN_RECOVERY=1`` (F-24)
    ------------------------------
    Ver ``_recover``. El ancla de confianza es el ACCESO AL SERVIDOR: quien puede fijar variables
    de entorno y reiniciar el proceso ya controla el gateway (lee ``SECRET_KEY``, la BD de
    metadatos, las credenciales cifradas). El flag no le da nada que no tenga; lo que agrega es
    que la recuperación sea un acto explícito, acotado (solo ``access_admin``) y auditado.
    """
    user_model = UserModel()
    if ADMIN_RECOVERY:
        _recover(user_model)
        return

    if user_model.count_active_access_admins() > 0:
        # El invariante se cumple: no hay nada que hacer, ni siquiera si el username de la
        # variable de entorno no coincide con nadie. Que el administrador se llame distinto de
        # `ADMIN_USERNAME` es una situación NORMAL, no algo que el arranque deba "corregir".
        return

    if user_model.count() > 0:
        logger.error(
            "No hay ningún access_admin activo con credencial y el arranque NO repara cuentas "
            "existentes. Para recuperar el acceso, arrancar UNA vez con ADMIN_RECOVERY=1 "
            "(reactiva '%s' y le devuelve access_admin) y quitarlo después.",
            ADMIN_USERNAME,
        )
        return

    if not ADMIN_PASSWORD:
        logger.warning(
            "ADMIN_PASSWORD no está definido; no se sembró ningún administrador."
        )
        return

    # Instalación nueva. El rol se fija EXPLÍCITAMENTE en `viewer` y no se hereda del default de
    # la columna, aunque coincidan: la siembra dice qué cuenta crea, no confía en un default.
    # `access.admin` vive ÚNICAMENTE en la global `access_admin`.
    user_model.create(
        {
            "username": ADMIN_USERNAME,
            "email": f"{ADMIN_USERNAME}@gateway.local",
            "hashed_password": hash_password(ADMIN_PASSWORD),
            "full_name": "Administrador",
            "notes": None,
            "is_active": True,
            "gateway_role": GatewayRole.VIEWER.value,
        }
    )
    user_model.grant_global_capabilities(ADMIN_USERNAME, [GlobalCapability.ACCESS_ADMIN.value])
    _open_window("seed")
    logger.info("Administrador de accesos '%s' sembrado (viewer + access_admin).", ADMIN_USERNAME)


def _recover(user_model: UserModel) -> None:
    """
    ``ADMIN_RECOVERY=1``: reactiva la cuenta de ``ADMIN_USERNAME`` y le devuelve
    ``access_admin``; si no existe, la crea como la siembra (``viewer`` + ``access_admin``).
    Reabre la ventana de arranque con plazo nuevo y audita ``access.admin_recovery``.

    - **Solo ``access_admin``**: nunca agrega ``security_officer`` ni ``owner``. Tampoco QUITA lo
      que la cuenta ya tenía (una cuenta combinada heredada sigue combinada): la recuperación
      devuelve la capacidad de administrar accesos, no rehace la cuenta. Si la cuenta tenía
      ``security_officer`` sin una excepción que cubra ``access_admin`` + ``security_officer``,
      el lector le descarta ``security_officer`` (falla cerrado): reparar eso es una decisión de
      una persona, no del arranque.
    - **No toca la contraseña.** Recupera a quien la tiene; no es un reseteo de credencial.
    - Corre en CADA arranque con el flag puesto (cada uno reabre la ventana), por eso el aviso
      pide quitarlo. Si ya hay un segundo ``access_admin`` con credencial, la ventana se vuelve
      a cerrar sola en el mismo arranque (``bootstrap_window.startup``).
    """
    existente = user_model.find_by_username(ADMIN_USERNAME)
    if existente:
        user_model.update(existente["id"], {"is_active": True})
        user_model.grant_global_capabilities(
            ADMIN_USERNAME, [GlobalCapability.ACCESS_ADMIN.value]
        )
        user_id = existente["id"]
        detalle = (
            f"ADMIN_RECOVERY=1: se reactivó '{ADMIN_USERNAME}' y se le restauró access_admin "
            "(solo access_admin). La contraseña NO se modificó"
        )
        if not existente.get("hashed_password"):
            logger.warning(
                "ADMIN_RECOVERY: '%s' no tiene credencial (invitación pendiente); la "
                "recuperación no la fija.",
                ADMIN_USERNAME,
            )
    else:
        if not ADMIN_PASSWORD:
            logger.error(
                "ADMIN_RECOVERY=1 pero '%s' no existe y ADMIN_PASSWORD no está definido: no se "
                "recuperó nada.",
                ADMIN_USERNAME,
            )
            return
        user_model.create(
            {
                "username": ADMIN_USERNAME,
                "email": f"{ADMIN_USERNAME}@gateway.local",
                "hashed_password": hash_password(ADMIN_PASSWORD),
                "full_name": "Administrador",
                "notes": None,
                "is_active": True,
                "gateway_role": GatewayRole.VIEWER.value,
            }
        )
        user_model.grant_global_capabilities(
            ADMIN_USERNAME, [GlobalCapability.ACCESS_ADMIN.value]
        )
        # Releída por username y no por `lastrowid`, que no todos los motores devuelven.
        user_id = (user_model.find_by_username(ADMIN_USERNAME) or {}).get("id")
        detalle = (
            f"ADMIN_RECOVERY=1: '{ADMIN_USERNAME}' no existía; se creó como viewer + "
            "access_admin"
        )
    ventana = _open_window("admin_recovery")
    closes_at = (ventana or {}).get("closes_at")
    audit.record(
        "access.admin_recovery",
        admin=None,
        actor_type="system",
        target_type="user",
        target_id=user_id,
        touched_engine=False,
        detail=(
            f"{detalle}; ventana de arranque reabierta hasta "
            f"{closes_at.isoformat(timespec='seconds') if closes_at else '—'} UTC"
        ),
    )
    logger.warning(
        "ADMIN_RECOVERY=1: se recuperó '%s' (access_admin) y se reabrió la ventana de arranque. "
        "QUITAR el flag: cada arranque con él la vuelve a abrir.",
        ADMIN_USERNAME,
    )


def _open_window(reason: str) -> dict | None:
    """Reabre la ventana de arranque. Best-effort: sin ella, las elevaciones quedan pendientes."""
    try:
        from app.services import bootstrap_window

        return bootstrap_window.reopen(reason=reason)
    except Exception:  # noqa: BLE001 — la ventana no puede impedir el arranque
        logger.exception("No se pudo abrir la ventana de arranque (%s).", reason)
        return None
