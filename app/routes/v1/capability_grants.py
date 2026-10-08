"""
Aprobación de capacidades puntuales sensibles (``/capability-grants``).

La alta y la revocación viven bajo ``/gateway-users/{id}/capability-grants``; acá está lo que NO
tiene un usuario como recurso padre: la bandeja de pendientes y la decisión. Todas exigen
``access.admin``, que solo tiene la global ``access_admin`` (D10).
"""

from fastapi import APIRouter, Request

from app.controllers.capability_grant_controller import CapabilityGrantController
from app.core.authz import AccessAdmin
from app.core.limiter import limiter
from app.schemas.capability_grant import (
    CapabilityGrantDecision,
    CapabilityGrantDecisionBulk,
    CapabilityGrantDecisionBulkOut,
    CapabilityGrantOut,
    PendingCapabilityGrantOut,
)
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/capability-grants", tags=["Capability Grants"])


@router.get("/pending", response_model=ApiResponse[list[PendingCapabilityGrantOut]])
def list_pending_capability_grants(actor: AccessAdmin):
    """
    Solicitudes pendientes vigentes de TODAS las personas. Cada una trae ``can_decide`` y, si es
    ``false``, ``blocked_reason`` (código ``access.*``). Las vencidas se barren antes de listar.
    """
    return success(data=CapabilityGrantController().list_pending(actor))


@router.post(
    "/decisions",
    response_model=ApiResponse[CapabilityGrantDecisionBulkOut],
    summary="Aprobar o rechazar varias solicitudes pendientes en una llamada",
)
# 10/minute: cada ítem es una decisión individual (un UPDATE y una auditoría, sin conexión al
# motor), bastante más liviana que el fan-out de 5/minute de ``apply-profile/bulk``; y aprobar a
# 100 pedidos de una vez es el caso de uso, no un abuso. El tope por llamada (100) acota el costo.
@limiter.limit("10/minute")
def decide_capability_grants_bulk(
    request: Request, actor: AccessAdmin, payload: CapabilityGrantDecisionBulk
):
    """
    Aprueba o rechaza varias solicitudes (``ids``, de 1 a 100, sin repetidos) con la MISMA decisión.

    Mejor esfuerzo: cada ítem aplica las reglas del endpoint individual (segundo aprobador, SoD,
    etc.) y es independiente; uno bloqueado no frena ni revierte a los demás. Siempre 200:
    revisar ``data.results[]`` (``ok``, ``grant`` o ``code``/``message``) en el orden pedido.
    El step-up (ventana de 5 minutos) se exige una vez para toda la llamada.
    """
    result = CapabilityGrantController().decide_bulk(
        payload.decision, payload.ids, actor, payload.reason
    )
    verb = "aprobadas" if payload.decision == "approve" else "rechazadas"
    message = f"{result['succeeded']} de {result['requested']} solicitudes {verb}."
    if result["failed"]:
        message += f" {result['failed']} con error."
    return success(data=result, message=message)


@router.post("/{grant_id}/approve", response_model=ApiResponse[CapabilityGrantOut])
def approve_capability_grant(
    actor: AccessAdmin, grant_id: int, payload: CapabilityGrantDecision | None = None
):
    """
    Aprueba una solicitud pendiente (``pending`` → ``active``, efecto inmediato). La tiene que
    aprobar OTRO access_admin (basta con que su función la asigne: ya no hace falta que la tenga).
    Si la solicitud trae ``sod_override``, la excepción se escribe acá, con ``approved_by``.
    Errores: 403 ``access.forbidden``, 404 ``access.grant_not_found``, 409
    ``access.grant_not_pending`` / ``access.self_approval_forbidden`` /
    ``access.self_modification_forbidden`` / ``access.grant_user_inactive`` /
    ``access.not_assignable`` / ``access.sod_conflict``, 404 ``access.grant_scope_not_found``.
    """
    reason = payload.reason if payload else None
    return success(
        data=CapabilityGrantController().approve(grant_id, actor, reason),
        message="Capacidad puntual aprobada.",
    )


@router.post("/{grant_id}/reject", response_model=ApiResponse[CapabilityGrantOut])
def reject_capability_grant(
    actor: AccessAdmin, grant_id: int, payload: CapabilityGrantDecision | None = None
):
    """Rechaza una solicitud pendiente (``pending`` → ``rejected``), con motivo opcional."""
    reason = payload.reason if payload else None
    return success(
        data=CapabilityGrantController().reject(grant_id, actor, reason),
        message="Capacidad puntual rechazada.",
    )
