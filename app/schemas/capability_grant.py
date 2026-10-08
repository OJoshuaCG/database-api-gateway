"""
Schemas de las CAPACIDADES PUNTUALES (``capability_grants``).

``capability`` es ``str`` y no el enum a propósito: una capacidad desconocida tiene que responder
con el código cerrado ``access.capability_not_grantable`` (422), no con el error genérico de
validación de Pydantic, que no nombra ningún código que la SPA pueda mapear a un mensaje.
``scope_type`` sí es un ``Literal``: ``global`` está prohibido y no tiene código propio.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.gateway_user import SodOverrideIn


class CapabilityGrantCreate(BaseModel):
    capability: str = Field(..., min_length=1, max_length=64)
    scope_type: Literal["environment", "server"]
    scope_id: int = Field(..., ge=1)
    reason: str | None = Field(None, max_length=500, description="Motivo declarado (opcional)")
    #: Break-glass de la separación de deberes, si la persona tiene ``security_officer`` y la
    #: capacidad es exclusiva de ``owner``. Ver ``SodOverrideIn``.
    sod_override: SodOverrideIn | None = None


#: Tope de destinos por alta masiva: acota el costo de validar todo antes de insertar.
BULK_MAX_TARGETS = 100


class CapabilityGrantBulkCreate(BaseModel):
    """
    Una o VARIAS capacidades sobre VARIOS entornos o servidores, todo o nada.

    ``capability`` (una) sigue funcionando tal cual; ``capabilities`` (varias) es la forma nueva.
    Exactamente una de las dos. Tras validar, ``capabilities`` queda SIEMPRE poblada (con la única
    de ``capability`` en el caso viejo) para que el controller tenga una sola forma de leerlo.
    El tope de pares ``len(capabilities) x len(scope_ids)`` lo hace cumplir el controller, porque
    necesita un código cerrado (``access.grant_bulk_too_large``) y no el error genérico de Pydantic.
    """

    capability: str | None = Field(None, min_length=1, max_length=64)
    capabilities: list[str] | None = Field(
        None, min_length=1, max_length=BULK_MAX_TARGETS,
        description="Varias capacidades (únicas). Excluyente con ``capability``.",
    )
    scope_type: Literal["environment", "server"]
    scope_ids: list[int] = Field(..., min_length=1, max_length=BULK_MAX_TARGETS)
    reason: str | None = Field(None, max_length=500, description="Motivo declarado (opcional)")
    sod_override: SodOverrideIn | None = None

    @field_validator("scope_ids")
    @classmethod
    def _dedupe(cls, ids: list[int]) -> list[int]:
        if any(i < 1 for i in ids):
            raise ValueError("scope_ids debe contener ids >= 1")
        return list(dict.fromkeys(ids))

    @field_validator("capabilities")
    @classmethod
    def _dedupe_capabilities(cls, capabilities: list[str] | None) -> list[str] | None:
        if capabilities is None:
            return None
        if any(not c or len(c) > 64 for c in capabilities):
            raise ValueError("capabilities debe contener textos de 1 a 64 caracteres")
        return list(dict.fromkeys(capabilities))

    @model_validator(mode="after")
    def _exactly_one_capability_form(self) -> "CapabilityGrantBulkCreate":
        has_single = self.capability is not None
        has_many = self.capabilities is not None
        if has_single == has_many:
            raise ValueError("indica exactamente uno: 'capability' o 'capabilities'")
        if has_single:
            self.capabilities = [self.capability]
        return self


class CapabilityGrantDecision(BaseModel):
    reason: str | None = Field(None, max_length=500, description="Motivo de la decisión (opcional)")


class CapabilityGrantDecisionBulk(BaseModel):
    """Aprobar o rechazar VARIAS solicitudes pendientes en una llamada (mejor esfuerzo)."""

    decision: Literal["approve", "reject"]
    ids: list[int] = Field(..., min_length=1, max_length=BULK_MAX_TARGETS)
    reason: str | None = Field(None, max_length=500, description="Motivo común (opcional)")

    @field_validator("ids")
    @classmethod
    def _dedupe(cls, ids: list[int]) -> list[int]:
        if any(i < 1 for i in ids):
            raise ValueError("ids debe contener ids >= 1")
        # Se conserva el orden pedido: la respuesta lo respeta.
        return list(dict.fromkeys(ids))


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
    sod_override: SodOverrideIn | None = Field(
        None, description="Break-glass que viaja con la solicitud: se aplica al aprobarla"
    )


class CapabilityGrantBulkOut(BaseModel):
    count: int
    #: ``True`` si las filas nacieron ``pending`` (esperan a otro access_admin).
    pending: bool
    grants: list[CapabilityGrantOut]


class CapabilityGrantDecisionItemOut(BaseModel):
    id: int
    ok: bool
    grant: CapabilityGrantOut | None = Field(None, description="La fila decidida (solo si ok)")
    code: str | None = Field(None, description="Código ``access.*`` del fallo (solo si no ok)")
    message: str | None = Field(None, description="Mensaje del fallo (solo si no ok)")


class CapabilityGrantDecisionBulkOut(BaseModel):
    requested: int
    succeeded: int
    failed: int
    results: list[CapabilityGrantDecisionItemOut]


class PendingCapabilityGrantOut(CapabilityGrantOut):
    can_decide: bool = Field(..., description="¿Puede el actor aprobar o rechazar esta solicitud?")
    blocked_reason: str | None = Field(
        None, description="Código ``access.*`` que explica por qué no (solo si can_decide=false)"
    )
