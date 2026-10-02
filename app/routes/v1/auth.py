"""Endpoints de autenticación: login, logout, me."""

from fastapi import APIRouter, Request

from app.controllers.auth_controller import AuthController
from app.core import session_store
from app.core.auth import SESSION_SID, login_session, logout_session
from app.core.authz import SelfRead
from app.core.limiter import (
    LOGIN_IP_RATE_LIMIT,
    client_address,
    enforce_login_limits,
    enforce_password_change_limits,
    enforce_step_up_limits,
    limiter,
)
from app.controllers.authz_controller import AuthzController
from app.schemas.auth import (
    AdminOut,
    LoginIn,
    PasswordChangeIn,
    PasswordChangeOut,
    RevokeOthersOut,
    SessionOut,
    StepUpIn,
    StepUpOut,
)
from app.exceptions import AppHttpException
from app.schemas.authz import MeOut
from app.services import audit
from app.utils.response import ApiResponse, empty, success

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=ApiResponse[AdminOut])
@limiter.limit(LOGIN_IP_RATE_LIMIT, key_func=client_address)
def login(request: Request, credentials: LoginIn):
    """
    Inicia sesión.

    **El límite NO usa el eje de sesión** (``session_or_address``) y no es un descuido: ese eje
    lee el ``sid`` de la cookie sin verificar que siga vivo, y en un endpoint público eso es un
    valor que elige el atacante — con N cookies juntadas tenía 5·N intentos por minuto contra
    cualquier cuenta. Acá se limita por IP (decorador) y por IP+usuario y usuario
    (``enforce_login_limits``), antes de verificar la password.
    """
    enforce_login_limits(request, credentials.username)
    admin = AuthController().authenticate(credentials.username, credentials.password)
    login_session(request, admin)
    return success(data=admin, message="Sesión iniciada.")


@router.post("/logout", response_model=ApiResponse[None])
def logout(request: Request, actor: SelfRead):
    """
    Cierra la sesión.

    **Hoy borra la cookie del cliente y nada más**: mientras la sesión viva en una cookie
    firmada, quien tenga una copia sigue autenticado. Eso lo arregla la sesión server-side, no
    este endpoint. Se audita igual —y desde ahora, porque no había ninguna acción ``auth.*``—
    para que el registro tenga los dos extremos de cada sesión y no solo el inicio.
    """
    audit.record(
        "auth.logout",
        admin=actor,
        target_type="user",
        target_id=actor.id,
        touched_engine=False,
    )
    logout_session(request)
    return empty("Sesión cerrada.")


@router.get("/me", response_model=ApiResponse[MeOut])
def me(actor: SelfRead):
    """
    Identidad y capacidades EFECTIVAS del actor.

    Aditivo: `id` y `username` siguen ahí, así que la SPA de hoy no se rompe. Lo que se agrega
    —`capabilities`, `role`, `scope_roles`— sale del MISMO predicado que hace cumplir
    `require()`, no de una lista paralela.

    **Es una pista de UI. Decide el servidor, siempre.**
    """
    return success(data=AuthzController().me(actor))


@router.get("/sessions", response_model=ApiResponse[list[SessionOut]])
def list_own_sessions(request: Request, actor: SelfRead):
    """
    Las sesiones VIVAS del propio usuario.

    Existe porque la tabla de sesiones lo hace posible por primera vez: con la sesión en la
    cookie firmada no había nada que listar ni forma de saber cuántas había. Es la pantalla que
    le permite a alguien ver una sesión que no abrió — detección que no depende de que otro lea
    el ``audit_log``.

    Detrás de ``self.read`` y **acotado al propio usuario**, sin parámetro para mirar las de
    otro: eso es administración de accesos y va con ``gateway.admin``, no acá.
    """
    return success(
        data=session_store.list_for_user(
            actor.id, current_sid=request.session.get(SESSION_SID)
        )
    )


@router.post("/sessions/revoke-others", response_model=ApiResponse[RevokeOthersOut])
def revoke_other_sessions(request: Request, actor: SelfRead):
    """
    Cierra todas las sesiones del usuario MENOS la actual.

    La excepción de la actual no es una comodidad: quien pide esto está reaccionando a algo que
    vio en el listado, y echarlo de la sesión desde la que está actuando lo deja sin poder
    seguir. La revocación administrativa —que sí cierra todas— es otra cosa y va con
    ``gateway.admin``.

    Se audita porque es el rastro que explica por qué N sesiones terminaron a la misma hora.
    """
    actual = request.session.get(SESSION_SID)
    revocadas = session_store.revoke_all_for_user(
        actor.id, session_store.REASON_ADMIN_REVOKED, except_sid=actual
    )
    audit.record(
        "auth.sessions_revoked",
        admin=actor,
        target_type="user",
        target_id=actor.id,
        touched_engine=False,
        detail=f"{revocadas} sesión(es) cerrada(s) por el propio usuario",
    )
    return success(
        data={"revoked": revocadas},
        message=f"{revocadas} sesión(es) cerrada(s).",
    )


@router.post("/password", response_model=ApiResponse[PasswordChangeOut])
def change_own_password(request: Request, actor: SelfRead, payload: PasswordChangeIn):
    """
    Cambia la contraseña del PROPIO usuario. Sin parámetro para cambiar la de otro: eso es
    administración de accesos.

    Detrás de ``self.read`` como el resto de las rutas propias (``logout``,
    ``sessions/revoke-others``): la dependencia exige sesión por cookie y el CSRF de todo método
    no seguro. Además exige la password ACTUAL, que es lo que separa "tengo la cookie" de "soy
    la persona": sin ella, una sesión robada alcanzaría para apropiarse de la cuenta.

    Límite propio (``enforce_password_change_limits``): 5/min por usuario verificado + IP,
    antes de pagar Argon2. Nunca por ``sid``.

    Al terminar, TODAS las sesiones del usuario quedan cerradas con motivo
    ``password_change`` y esta respuesta trae la cookie de una sesión NUEVA (``sid`` rotado):
    el token CSRF cambia con ella, y el ``CsrfCookieMiddleware`` publica el nuevo en esta misma
    respuesta.
    """
    enforce_password_change_limits(request, actor.id)
    otras = AuthController().change_password(
        actor,
        current_password=payload.current_password,
        new_password=payload.new_password,
        current_sid=request.session.get(SESSION_SID) or "",
    )
    login_session(request, {"id": actor.id})
    return success(
        data={"revoked_sessions": otras},
        message="Contraseña cambiada. Se cerraron las demás sesiones.",
    )


@router.post("/step-up", response_model=ApiResponse[StepUpOut])
def step_up(request: Request, actor: SelfRead, payload: StepUpIn):
    """
    Confirma la contraseña y abre la ventana de step-up ("sudo mode") de ESTA sesión.

    Las capacidades con ``requires_step_up`` (``/auth/me`` → ``step_up_capabilities``) responden
    403 ``access.step_up_required`` cuando la ventana está cerrada; la SPA llama a esto y
    reintenta. El login ya abre la ventana. Ver ``app/core/step_up.py``.

    Detrás de ``self.read``: sesión por cookie + CSRF; un token de agente no llega acá. Límite
    propio (``enforce_step_up_limits``, 5/min por usuario + IP) antes de pagar Argon2.

    - 200: ``step_up_expires_at``. El ``sid`` NO rota.
    - 400 ``auth.step_up_failed`` (con ``attempts_remaining``): contraseña incorrecta. 400 y no
      401 para no disparar el logout de la SPA por un error de tipeo.
    - 401 ``auth.session_step_up_failed``: era el quinto fallo seguido y la sesión se cerró.
    """
    enforce_step_up_limits(request, actor.id)
    try:
        data = AuthController().step_up(
            actor,
            password=payload.password,
            sid=request.session.get(SESSION_SID) or "",
        )
    except AppHttpException as exc:
        if exc.status_code == 401:
            # Sesión tachada: la cookie se limpia igual que en `authenticated_user`.
            request.session.clear()
        raise
    return success(data=data, message="Contraseña confirmada.")
