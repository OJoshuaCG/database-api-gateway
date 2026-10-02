"""
Schemas de las CAPACIDADES PUNTUALES (``capability_grants``).

``capability`` es ``str`` y no el enum a propósito: una capacidad desconocida tiene que responder
con el código cerrado ``access.capability_not_grantable`` (422), no con el error genérico de
validación de Pydantic, que no nombra ningún código que la SPA pueda mapear a un mensaje.
``scope_type`` sí es un ``Literal``: ``global`` está prohibido y no tiene código propio.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CapabilityGrantCreate(BaseModel):
    capability: str = Field(..., min_length=1, max_length=64)
    scope_type: Literal["environment", "server"]
    scope_id: int = Field(..., ge=1)
    reason: str | None = Field(None, max_length=500, description="Motivo declarado (opcional)")


class CapabilityGrantDecision(BaseModel):
    reason: str | None = Field(None, max_length=500, description="Motivo de la decisión (opcional)")


class UserRef(BaseModel):
    id: int
    username: str


class CapabilityGrantOut(BaseModel):
    id: int
    user_id: int
    username: str | None = None
    capability: str
    scope_type: str
    scope_id: int
    scope_name: str | None = Field(None, description="Nombre del entorno o servidor")
    status: str = Field(..., description="pending|active|rejected|expired|cancelled|revoked")
    sensitive: bool = Field(..., description="Exige un segundo aprobador")
    requested_by: UserRef | None = None
    requested_at: datetime | None = None
    decided_by: UserRef | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    request_reason: str | None = None
    decision_reason: str | None = None
    implies: list[str] = Field(default_factory=list, description="Lecturas que trae implícitas")


class PendingCapabilityGrantOut(CapabilityGrantOut):
    can_decide: bool = Field(..., description="¿Puede el actor aprobar o rechazar esta solicitud?")
    blocked_reason: str | None = Field(
        None, description="Código ``access.*`` que explica por qué no (solo si can_decide=false)"
    )
