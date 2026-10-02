"""
Endpoints de los usuarios DEL GATEWAY (``/gateway-users``).

Es el módulo que hace **usable** el modelo de capacidades: hasta acá había un solo usuario, así
que roles y alcances existían sin nadie a quien aplicarlos.

Todo detrás de ``gateway.admin`` —que solo tienen las capacidades globales ``access_admin`` y
``security_officer``, no el rol ``owner``— **menos uno**: aceptar la invitación es público,
porque quien la usa todavía no puede autenticarse. Eso es justamente el punto del diseño.

OJO CON EL NOMBRE: acá se administran los usuarios que se autentican **contra el gateway**. Los
usuarios del MOTOR viven en ``/server-users`` y ``/servers/{id}/users``.
"""

from typing import Literal

from fastapi import APIRouter, Query, Request

from app.controllers.authz_controller import AuthzController
from app.controllers.capability_grant_controller import CapabilityGrantController
from app.controllers.gateway_user_controller import GatewayUserController
from app.core.limiter import client_address, limiter
from app.core.authz import GatewayAdmin
from app.schemas.authz import EffectiveAccessOut
from app.schemas.capability_grant import CapabilityGrantCreate, CapabilityGrantOut
from app.schemas.gateway_user import (
    AcceptInviteIn,
    AcceptInviteOut,
    GatewayUserAccessIn,
    GatewayUserCreate,
    GatewayUserCreatedOut,
    GatewayUserOut,
    GatewayUserUpdate,
    InviteOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/gateway-users", tags=["Gateway Users"])


@router.get("", response_model=ApiResponse[list[GatewayUserOut]])
def list_gateway_users(actor: GatewayAdmin, pagination: PaginationDep):
    """Usuarios del gateway con su acceso resuelto. Cero conexiones al motor."""
    items, total = GatewayUserController().list_users(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[GatewayUserCreatedOut], status_code=201)
def create_gateway_user(actor: GatewayAdmin, payload: GatewayUserCreate):
    """
    Crea la cuenta **sin credencial** y devuelve el token de invitación.

    **No hay campo de password en el payload, y no es un olvido.** Si quien crea la cuenta
    tipeara la password inicial conocería una credencial funcional de esa identidad, y con eso
    **toda fila de auditoría atribuida a esa persona sería repudiable** — para un sistema cuyo
    valor central es el rastro, eso es fatal. Y "cambio forzado en el primer login" no lo
    arregla: quien la puso pudo haber entrado antes.

    Con ``access_admin`` en el modelo no es solo repudio: sería la vía de escalada — crear una
    identidad ``owner``, conocer su password y operar producción con la cara de otro.
    """
    return success(
        data=GatewayUserController().create_user(payload.model_dump(), admin=actor),
        message="Usuario creado. Entregale el token de invitación a la persona.",
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
def get_gateway_user(actor: GatewayAdmin, user_id: int):
    return success(data=GatewayUserController().get_user(user_id))


@router.patch("/{user_id}", response_model=ApiResponse[GatewayUserOut])
def update_gateway_user(actor: GatewayAdmin, user_id: int, payload: GatewayUserUpdate):
    """
    Cambia rol, estado y datos de contacto.

    Desactivar al **último** ``access_admin`` activo devuelve 409
    ``access.last_admin_protected``: sin ese guard, dos administradores pueden dejar al gateway
    sin nadie que pueda repararlo, y la única salida es SQL a mano contra la BD de metadatos en
    plena incidencia.

    Un cambio de rol o una desactivación **tachan las sesiones** de esa persona. El rol ya se
    relee por request, así que el efecto era inmediato igual; tacharlas es para que el corte
    quede con motivo y la persona entienda por qué volvió al login.
    """
    return success(
        data=GatewayUserController().update_user(
            user_id, payload.model_dump(exclude_unset=True), admin=actor
        ),
        message="Usuario actualizado.",
    )


@router.put("/{user_id}/access", response_model=ApiResponse[GatewayUserOut])
def set_gateway_user_access(actor: GatewayAdmin, user_id: int, payload: GatewayUserAccessIn):
    """
    Reemplaza el acceso COMPLETO de la persona: globales y alcances.

    Un PUT y no N POSTs por grant porque la pregunta que responde una pantalla de accesos es
    *"qué acceso tiene"*, y con endpoints por grant el estado final depende del orden de N
    llamadas — y una que falle a mitad deja un acceso que nadie pidió.

    **Recordá que un grant REEMPLAZA al rol base en su alcance, no se suma**: `base=operator`
    con un grant `viewer` sobre producción es lector ahí y operador en el resto. Y antes de
    otorgar el primero conviene mirar ``GET /authz/scope-readiness``: una BD sin entorno se
    trata como el entorno más protegido.
    """
    return success(
        data=GatewayUserController().set_access(user_id, payload.model_dump(), admin=actor),
        message="Acceso actualizado.",
    )


@router.post("/{user_id}/invite", response_model=ApiResponse[InviteOut])
def reinvite_gateway_user(actor: GatewayAdmin, user_id: int):
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
    actor: GatewayAdmin,
    user_id: int,
    status: Literal["pending", "active", "rejected", "expired", "cancelled", "revoked"]
    | None = Query(None, description="Filtra por estado"),
):
    """
    Capacidades puntuales de la persona, de todos los estados (historial incluido). Solo
    ``access_admin``: ``security_officer`` recibe 403 aunque tenga ``gateway.admin``.
    """
    return success(data=CapabilityGrantController().list_for_user(user_id, actor, status))


@router.post(
    "/{user_id}/capability-grants",
    response_model=ApiResponse[CapabilityGrantOut],
    status_code=201,
)
def create_capability_grant(actor: GatewayAdmin, user_id: int, payload: CapabilityGrantCreate):
    """
    Otorga una capacidad puntual sobre un entorno o servidor. **Suma** al rol de la persona.

    Las 7 capacidades sensibles nacen ``pending`` (necesitan un segundo access_admin y no surten
    efecto hasta entonces); el resto nace ``active``. Errores: 403 ``access.forbidden``, 409
    ``access.self_modification_forbidden`` / ``access.grant_user_inactive`` /
    ``access.grant_ceiling_exceeded`` / ``access.grant_duplicate``, 422
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
def revoke_capability_grant(actor: GatewayAdmin, user_id: int, grant_id: int):
    """
    Revoca una capacidad activa (``revoked``) o cancela una pendiente (``cancelled``). Un solo
    access_admin alcanza y el efecto es inmediato. No se borra la fila: queda como historial.
    """
    return success(
        data=CapabilityGrantController().revoke(user_id, grant_id, actor),
        message="Capacidad puntual revocada.",
    )


@router.get("/{user_id}/effective-access", response_model=ApiResponse[EffectiveAccessOut])
def get_effective_access(actor: GatewayAdmin, user_id: int):
    """
    Acceso efectivo de la persona CON procedencia: cada capacidad dice si viene del rol base, de
    un rol por alcance, de una global o de una capacidad puntual (``grant_id``). Lo calcula el
    MISMO resolvedor que hace cumplir ``require()``. Solo ``access_admin`` (403 opaco si no).
    """
    return success(data=AuthzController().effective_access(user_id, actor))
