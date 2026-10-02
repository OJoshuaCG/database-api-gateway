"""
Endpoints de los usuarios DEL GATEWAY (``/gateway-users``).

Es el módulo que hace **usable** el modelo de capacidades: hasta acá había un solo usuario, así
que roles y alcances existían sin nadie a quien aplicarlos.

Todo detrás de ``access.admin`` —que solo tiene la capacidad global ``access_admin``: ni el rol
``owner`` ni ``security_officer``— **menos uno**: aceptar la invitación es público,
porque quien la usa todavía no puede autenticarse. Eso es justamente el punto del diseño.

OJO CON EL NOMBRE: acá se administran los usuarios que se autentican **contra el gateway**. Los
usuarios del MOTOR viven en ``/server-users`` y ``/servers/{id}/users``.
"""

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.controllers.authz_controller import AuthzController
from app.controllers.capability_grant_controller import CapabilityGrantController
from app.controllers.gateway_user_controller import GatewayUserController
from app.core.limiter import client_address, limiter
from app.core.authz import AccessAdmin
from app.schemas.access_request import GatewayUserCreatedPendingOut, GatewayUserPendingOut
from app.schemas.authz import EffectiveAccessOut
from app.schemas.capability_grant import CapabilityGrantCreate, CapabilityGrantOut
from app.schemas.gateway_user import (
    AcceptInviteIn,
    AcceptInviteOut,
    GatewayUserAccessIn,
    GatewayUserCreate,
    GatewayUserCreatedOut,
    GatewayUserOut,
    GatewayUserSessionOut,
    GatewayUserSessionsRevokedOut,
    GatewayUserUpdate,
    InviteOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/gateway-users", tags=["Gateway Users"])

#: Mensaje del ``202``: la parte que eleva espera a OTRO access_admin.
_PENDING_MESSAGE = (
    "La elevación quedó pendiente: la tiene que aprobar otra persona con access_admin "
    "(POST /access-requests/{id}/approve). Lo que no eleva ya se aplicó."
)


def _respond(result: dict, *, pending_model, ok_message: str, ok_status: int = 200):
    """
    ``200``/``201`` de siempre si todo se aplicó; ``202 access.elevation_pending`` si una parte
    quedó pendiente de un segundo aprobador.

    El ``202`` se arma a mano (``JSONResponse``) porque el ``response_model`` de la ruta es el del
    caso normal y descartaría ``pending_request``. Se valida igual contra ``ApiResponse[...]``, así
    que la forma es la que declara ``responses``. El caso normal no cambia en nada: ni status ni
    campos nuevos.
    """
    if "pending_request" not in result:
        return success(data=result, message=ok_message)
    body = ApiResponse[pending_model](data=result, message=_PENDING_MESSAGE)
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"))


@router.get("", response_model=ApiResponse[list[GatewayUserOut]])
def list_gateway_users(actor: AccessAdmin, pagination: PaginationDep):
    """Usuarios del gateway con su acceso resuelto. Cero conexiones al motor."""
    items, total = GatewayUserController().list_users(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[GatewayUserCreatedOut],
    status_code=201,
    responses={202: {"model": ApiResponse[GatewayUserCreatedPendingOut],
                     "description": "Cuenta creada sin la elevación; elevación pendiente"}},
)
def create_gateway_user(actor: AccessAdmin, payload: GatewayUserCreate):
    """
    Crea la cuenta **sin credencial** y devuelve el token de invitación.

    **No hay campo de password en el payload, y no es un olvido.** Si quien crea la cuenta
    tipeara la password inicial conocería una credencial funcional de esa identidad, y con eso
    **toda fila de auditoría atribuida a esa persona sería repudiable** — para un sistema cuyo
    valor central es el rastro, eso es fatal. Y "cambio forzado en el primer login" no lo
    arregla: quien la puso pudo haber entrado antes.

    Con ``access_admin`` en el modelo no es solo repudio: sería la vía de escalada — crear una
    identidad ``owner``, conocer su password y operar producción con la cara de otro.

    **Elevaciones (C3).** Si pide ``owner`` o alguna global (o trae ``sod_override``), la cuenta
    nace con la parte que NO eleva (``viewer``/``operator``, sin globales), la invitación se emite
    igual y la respuesta es ``202`` con ``code: access.elevation_pending`` y ``pending_request``.
    """
    return _respond(
        GatewayUserController().create_user(payload.model_dump(), admin=actor),
        pending_model=GatewayUserCreatedPendingOut,
        ok_message="Usuario creado. Entregale el token de invitación a la persona.",
    )


@router.post("/invite/accept", response_model=ApiResponse[AcceptInviteOut])
# Por IP y no por sesión: es público, y el `sid` sin verificar de `session_or_address` sería un
# cupo nuevo por cada cookie que el cliente junte. Ver el docstring de `app/core/limiter.py`.
@limiter.limit("10/minute", key_func=client_address)
def accept_invite(request: Request, payload: AcceptInviteIn):
    """
    Fija la primera contraseña. **Público**, y es el único endpoint público que escribe.

    Se autoriza con el token y nada más porque quien lo usa **todavía no puede autenticarse**.
    El token es HMAC sobre ``(user_id, credential_epoch)`` con TTL de 48 h, y aceptar sube el
    epoch: es de **un solo uso**, sin necesidad de una tabla de tokens consumidos.

    El ``user_id`` viaja DENTRO del token firmado y no como parámetro: si viniera aparte habría
    que verificar que coincide, y ese es el chequeo que alguien olvida.

    Tiene rate limit propio porque es público y escribe. Y **no distingue** ningún motivo de
    fallo del token —firma inválida, vencido, ya usado, usuario inexistente—: todos responden el
    mismo 422 ``gateway_user.not_found`` con el mismo mensaje, para no convertirlo en un oráculo
    de qué invitaciones hay pendientes. Ver ``GatewayUserController._verify_invite``.
    """
    return success(
        data=GatewayUserController().accept_invite(payload.token, payload.password),
        message="Contraseña establecida. Ya podés iniciar sesión.",
    )


@router.get("/{user_id}", response_model=ApiResponse[GatewayUserOut])
def get_gateway_user(actor: AccessAdmin, user_id: int):
    return success(data=GatewayUserController().get_user(user_id))


@router.patch(
    "/{user_id}",
    response_model=ApiResponse[GatewayUserOut],
    responses={202: {"model": ApiResponse[GatewayUserPendingOut],
                     "description": "Cambio de rol a owner pendiente de un segundo aprobador"}},
)
def update_gateway_user(actor: AccessAdmin, user_id: int, payload: GatewayUserUpdate):
    """
    Cambia rol, estado y datos de contacto.

    Desactivar al **último** ``access_admin`` activo devuelve 409
    ``access.last_admin_protected``: sin ese guard, dos administradores pueden dejar al gateway
    sin nadie que pueda repararlo, y la única salida es SQL a mano contra la BD de metadatos en
    plena incidencia.

    Un cambio de rol o una desactivación **tachan las sesiones** de esa persona. El rol ya se
    relee por request, así que el efecto era inmediato igual; tacharlas es para que el corte
    quede con motivo y la persona entienda por qué volvió al login.

    Pasar a ``owner`` es una elevación (C3): el resto del PATCH se aplica y el rol queda pendiente
    de otro access_admin (``202 access.elevation_pending``). Bajar de rol se aplica siempre ya.
    """
    return _respond(
        GatewayUserController().update_user(
            user_id, payload.model_dump(exclude_unset=True), admin=actor
        ),
        pending_model=GatewayUserPendingOut,
        ok_message="Usuario actualizado.",
    )


@router.put(
    "/{user_id}/access",
    response_model=ApiResponse[GatewayUserOut],
    responses={202: {"model": ApiResponse[GatewayUserPendingOut],
                     "description": "Lo que no eleva, aplicado; la elevación, pendiente"}},
)
def set_gateway_user_access(actor: AccessAdmin, user_id: int, payload: GatewayUserAccessIn):
    """
    Reemplaza el acceso COMPLETO de la persona: globales y alcances.

    Un PUT y no N POSTs por grant porque la pregunta que responde una pantalla de accesos es
    *"qué acceso tiene"*, y con endpoints por grant el estado final depende del orden de N
    llamadas — y una que falle a mitad deja un acceso que nadie pidió.

    **Recordá que un grant REEMPLAZA al rol base en su alcance, no se suma**: `base=operator`
    con un grant `viewer` sobre producción es lector ahí y operador en el resto. Y antes de
    otorgar el primero conviene mirar ``GET /authz/scope-readiness``: una BD sin entorno se
    trata como el entorno más protegido.

    **Elevaciones (C3).** Agregar una global, un ``owner`` por alcance o un ``sod_override`` no se
    aplica solo: lo demás (bajas incluidas) se aplica ya y la elevación queda pendiente de otro
    access_admin (``202 access.elevation_pending`` con ``pending_request``).
    """
    return _respond(
        GatewayUserController().set_access(user_id, payload.model_dump(), admin=actor),
        pending_model=GatewayUserPendingOut,
        ok_message="Acceso actualizado.",
    )


@router.post("/{user_id}/invite", response_model=ApiResponse[InviteOut])
def reinvite_gateway_user(actor: AccessAdmin, user_id: int):
    """
    Reemite la invitación y **mata la anterior** subiendo el ``credential_epoch``.

    Es también la vía para revocar una invitación que se filtró: no hace falta un endpoint de
    revocación aparte, porque emitir invalida.
    """
    return success(
        data=GatewayUserController().reinvite(user_id, admin=actor),
        message="Invitación reemitida. La anterior quedó inválida.",
    )


@router.get(
    "/{user_id}/capability-grants",
    response_model=ApiResponse[list[CapabilityGrantOut]],
)
def list_capability_grants(
    actor: AccessAdmin,
    user_id: int,
    status: Literal["pending", "active", "rejected", "expired", "cancelled", "revoked"]
    | None = Query(None, description="Filtra por estado"),
):
    """
    Capacidades puntuales de la persona, de todos los estados (historial incluido). Solo
    ``access_admin`` (``access.admin``): ``security_officer`` recibe 403.
    """
    return success(data=CapabilityGrantController().list_for_user(user_id, actor, status))


@router.post(
    "/{user_id}/capability-grants",
    response_model=ApiResponse[CapabilityGrantOut],
    status_code=201,
)
def create_capability_grant(actor: AccessAdmin, user_id: int, payload: CapabilityGrantCreate):
    """
    Otorga una capacidad puntual sobre un entorno o servidor. **Suma** al rol de la persona.

    Las 7 capacidades sensibles nacen ``pending`` (necesitan un segundo access_admin y no surten
    efecto hasta entonces); el resto nace ``active``. Errores: 403 ``access.forbidden``, 409
    ``access.self_modification_forbidden`` / ``access.grant_user_inactive`` /
    ``access.not_assignable`` / ``access.grant_duplicate``, 422
    ``access.capability_not_grantable``, 404 ``access.grant_scope_not_found``.
    """
    return success(
        data=CapabilityGrantController().create(user_id, payload.model_dump(), actor),
        message="Capacidad puntual registrada.",
    )


@router.delete(
    "/{user_id}/capability-grants/{grant_id}",
    response_model=ApiResponse[CapabilityGrantOut],
)
def revoke_capability_grant(actor: AccessAdmin, user_id: int, grant_id: int):
    """
    Revoca una capacidad activa (``revoked``) o cancela una pendiente (``cancelled``). Un solo
    access_admin alcanza y el efecto es inmediato. No se borra la fila: queda como historial.
    """
    return success(
        data=CapabilityGrantController().revoke(user_id, grant_id, actor),
        message="Capacidad puntual revocada.",
    )


@router.get("/{user_id}/effective-access", response_model=ApiResponse[EffectiveAccessOut])
def get_effective_access(actor: AccessAdmin, user_id: int):
    """
    Acceso efectivo de la persona CON procedencia: cada capacidad dice si viene del rol base, de
    un rol por alcance, de una global o de una capacidad puntual (``grant_id``). Lo calcula el
    MISMO resolvedor que hace cumplir ``require()``. Solo ``access_admin`` (403 opaco si no).
    """
    return success(data=AuthzController().effective_access(user_id, actor))


@router.get("/{user_id}/sessions", response_model=ApiResponse[list[GatewayUserSessionOut]])
def list_gateway_user_sessions(actor: AccessAdmin, user_id: int):
    """
    Las sesiones VIVAS de la persona (sin tachar ni vencidas), la más reciente primero.

    **Sin ``sid`` ni prefijo**: el ``sid`` es la credencial de sesión. Para revocarlas no hace
    falta identificarlas, porque la revocación administrativa cierra todas. 404
    ``gateway_user.not_found`` si la cuenta no existe.
    """
    return success(data=GatewayUserController().list_sessions(user_id))


@router.post(
    "/{user_id}/sessions/revoke", response_model=ApiResponse[GatewayUserSessionsRevokedOut]
)
def revoke_gateway_user_sessions(actor: AccessAdmin, user_id: int):
    """
    Cierra TODAS las sesiones vivas de OTRA persona. Su próximo request responde ``401
    auth.session_access_admin_revoked``. No toca la contraseña ni el acceso.

    Pide step-up (``access.admin``, método no seguro). Sobre uno mismo: ``409
    access.self_modification_forbidden`` (lo propio es ``POST /auth/sessions/revoke-others``).
    Auditado ``gateway_user.sessions_revoked`` con la cantidad.
    """
    data = GatewayUserController().revoke_sessions(user_id, admin=actor)
    return success(data=data, message=f"{data['revoked']} sesión(es) cerrada(s).")
