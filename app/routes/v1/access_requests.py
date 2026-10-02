"""
Elevaciones de acceso pendientes de un segundo aprobador (``/access-requests``, C3).

Nacen en ``POST /gateway-users``, ``PATCH /gateway-users/{id}`` y ``PUT /gateway-users/{id}/access``
cuando el cambio eleva (``owner``, una global, un ``sod_override``): esos endpoints responden
``202 access.elevation_pending`` con la solicitud. Acá se listan y se deciden. Todo exige
``access.admin`` (que trae step-up por catálogo en los ``POST``).
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.controllers.access_request_controller import AccessRequestController
from app.core.actor import Actor
from app.core.authz import AccessAdmin, require
from app.services.capability_catalog import Capability
from app.schemas.access_request import (
    AccessRequestDecision,
    AccessRequestOut,
    PendingAccessRequestOut,
)
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/access-requests", tags=["Access Requests"])

# Cancelar NO pide step-up (``step_up=False``): retirar una elevación que uno mismo pidió nunca da
# acceso, y frenar algo no puede costar más que pedirlo. La capa de capacidad sigue valiendo. Ver
# ``STEP_UP_EXEMPT`` en ``scripts/check_route_capabilities.py``.
AccessAdminCancel = Annotated[
    Actor, Depends(require(Capability.ACCESS_ADMIN_CAP, step_up=False))
]


@router.get("/pending", response_model=ApiResponse[list[PendingAccessRequestOut]])
def list_pending_access_requests(actor: AccessAdmin):
    """
    Elevaciones pendientes vigentes de TODAS las personas. Cada una trae ``can_decide`` y, si es
    ``false``, ``blocked_reason`` (código ``access.*``). Las vencidas se barren antes de listar.
    """
    return success(data=AccessRequestController().list_pending(actor))


@router.get("/{request_id}", response_model=ApiResponse[AccessRequestOut])
def get_access_request(actor: AccessAdmin, request_id: int):
    """Una solicitud, en cualquier estado. 404 ``access.request_not_found``."""
    return success(data=AccessRequestController().get(request_id))


@router.post("/{request_id}/approve", response_model=ApiResponse[AccessRequestOut])
def approve_access_request(
    actor: AccessAdmin, request_id: int, payload: AccessRequestDecision | None = None
):
    """
    Aplica la elevación (``pending`` → ``applied``) y tacha las sesiones de la persona. La aprueba
    OTRO access_admin. Errores: 404 ``access.request_not_found``; 409
    ``access.request_not_pending`` / ``access.self_approval_forbidden`` /
    ``access.self_modification_forbidden`` / ``access.grant_user_inactive`` /
    ``access.request_stale`` / ``access.sod_conflict`` / ``access.last_admin_protected``.
    """
    reason = payload.reason if payload else None
    return success(
        data=AccessRequestController().approve(request_id, actor, reason),
        message="Elevación aprobada y aplicada.",
    )


@router.post("/{request_id}/reject", response_model=ApiResponse[AccessRequestOut])
def reject_access_request(
    actor: AccessAdmin, request_id: int, payload: AccessRequestDecision | None = None
):
    """``pending`` → ``rejected``, con motivo opcional. Cualquier access_admin."""
    reason = payload.reason if payload else None
    return success(
        data=AccessRequestController().reject(request_id, actor, reason),
        message="Elevación rechazada.",
    )


@router.post("/{request_id}/cancel", response_model=ApiResponse[AccessRequestOut])
def cancel_access_request(
    actor: AccessAdminCancel, request_id: int, payload: AccessRequestDecision | None = None
):
    """``pending`` → ``cancelled``. Solo quien la pidió (409 ``access.request_not_requester``)."""
    reason = payload.reason if payload else None
    return success(
        data=AccessRequestController().cancel(request_id, actor, reason),
        message="Elevación cancelada.",
    )
