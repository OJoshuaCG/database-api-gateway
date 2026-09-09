"""Endpoints de autenticación: login, logout, me."""

from fastapi import APIRouter, Request

from app.controllers.auth_controller import AuthController
from app.core import session_store
from app.core.auth import SESSION_SID, login_session, logout_session
from app.core.authz import SelfRead
from app.core.limiter import limiter
from app.controllers.authz_controller import AuthzController
from app.schemas.auth import AdminOut, LoginIn, RevokeOthersOut, SessionOut
from app.schemas.authz import MeOut
from app.services import audit
from app.utils.response import ApiResponse, empty, success

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=ApiResponse[AdminOut])
@limiter.limit("5/minute")
def login(request: Request, credentials: LoginIn):
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
