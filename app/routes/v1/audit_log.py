"""
Lectura de la auditoría (``/audit-log``). Solo ``audit.read`` (``security_officer``).

Quien hace los cambios de acceso (``access_admin``) no lee el rastro que los registra: recibe el
403 opaco de ``require()``. El porqué completo, y por qué la lectura no divulga (un ``GET`` no
pide step-up), en el docstring de ``app/controllers/audit_log_controller.py``.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query

from app.controllers.audit_log_controller import AuditLogController
from app.core.authz import AuditRead
from app.schemas.audit_log import AuditLogOut
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/audit-log", tags=["Audit"])


@router.get("", response_model=ApiResponse[list[AuditLogOut]])
def list_audit_log(
    actor: AuditRead,
    pagination: PaginationDep,
    action: str | None = Query(
        None,
        max_length=65,
        description="Acción exacta, o prefijo si termina en '*' (p. ej. 'access.*')",
    ),
    admin_id: int | None = Query(None, ge=1, description="Usuario del gateway que actuó"),
    admin_username: str | None = Query(None, max_length=128, description="Username exacto"),
    actor_type: Literal["admin", "api_token", "integration", "system", "anonymous"] | None = Query(None),
    api_token_id: int | None = Query(None, ge=1, description="PK del token de agente"),
    target_type: str | None = Query(None, max_length=64),
    target_id: int | None = Query(None, description="Id del objeto (junto con target_type)"),
    server_id: int | None = Query(None, ge=1),
    status: str | None = Query(
        None, max_length=20, description="Exacto: success | failure | error | attempt | denied…"
    ),
    request_id: str | None = Query(None, max_length=32),
    date_from: datetime | None = Query(
        None, alias="from", description="Desde (inclusive). ISO 8601; sin zona = UTC"
    ),
    date_to: datetime | None = Query(
        None, alias="to", description="Hasta (EXCLUSIVE). ISO 8601; sin zona = UTC"
    ),
):
    """
    Entradas de auditoría, las más nuevas primero (``id`` descendente), paginadas.

    Todos los filtros se combinan con AND. ``detail`` viaja tal cual se guardó y, si es JSON,
    también parseado en ``detail_json``. ``422 audit.invalid_range`` si ``from >= to``.
    """
    filters = {
        "action": action,
        "admin_id": admin_id,
        "admin_username": admin_username,
        "actor_type": actor_type,
        "api_token_id": api_token_id,
        "target_type": target_type,
        "target_id": target_id,
        "server_id": server_id,
        "status": status,
        "request_id": request_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    items, total = AuditLogController().list_entries(
        filters, limit=pagination.size, offset=pagination.offset, reader=actor
    )
    return paginated(items, total=total, pagination=pagination)


@router.get("/{entry_id}", response_model=ApiResponse[AuditLogOut])
def get_audit_entry(actor: AuditRead, entry_id: int):
    """Una entrada. ``404 audit.not_found`` si no existe."""
    return success(data=AuditLogController().get_entry(entry_id, reader=actor))
