"""
Schemas de las ELEVACIONES de acceso con segundo aprobador (``/access-requests``, C3), y de la
respuesta ``202 access.elevation_pending`` de los escritores de ``/gateway-users``.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.capability_grant import UserRef
from app.schemas.gateway_user import (
    GatewayUserCreatedOut,
    GatewayUserOut,
    ScopeGrantOut,
    SodOverrideIn,
)


class AccessRequestDesiredOut(BaseModel):
    """El acceso FINAL que deja la solicitud si se aprueba (no un delta)."""

    gateway_role: str
    global_capabilities: list[str] = Field(default_factory=list)
    scope_grants: list[ScopeGrantOut] = Field(default_factory=list)


class AccessRequestOut(BaseModel):
    id: int
    target: UserRef
    requested_by: UserRef | None = None
    status: str = Field(..., description="pending | applied | rejected | cancelled | expired")
    origin: str | None = Field(None, description="create | update | set_access")
    desired: AccessRequestDesiredOut
    elevations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Lo que eleva: {kind: base_role|scope_grant|global_capability, role, scope_type, "
            "scope_id, global_capability}"
        ),
    )
    sod_override: SodOverrideIn | None = Field(
        None, description="Break-glass que se aplica junto con la elevación"
    )
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_by: UserRef | None = None
    decided_at: datetime | None = None
    reason: str | None = Field(
        None,
        description=(
            "Motivo de la decisión, o el cierre automático: expired | requester_lost_access | "
            "superseded | stale"
        ),
    )


class PendingAccessRequestOut(AccessRequestOut):
    can_decide: bool = Field(..., description="¿Puede el actor aprobar esta solicitud?")
    blocked_reason: str | None = Field(
        None, description="Código ``access.*`` que explica por qué no (solo si can_decide=false)"
    )


class AccessRequestDecision(BaseModel):
    reason: str | None = Field(None, max_length=500, description="Motivo (opcional)")


class GatewayUserPendingOut(GatewayUserOut):
    """
    ``202``: la persona tal como quedó YA (la parte que no eleva, aplicada) y la solicitud con la
    parte que espera a otro ``access_admin``.
    """

    code: Literal["access.elevation_pending"]
    pending_request: AccessRequestOut


class GatewayUserCreatedPendingOut(GatewayUserCreatedOut):
    """``202`` del alta: la cuenta nace sin la elevación, con la invitación igual."""

    code: Literal["access.elevation_pending"]
    pending_request: AccessRequestOut
