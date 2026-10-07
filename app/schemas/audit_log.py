"""Schemas de la lectura de auditoría (``GET /audit-log``, ``audit.read``)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditLogOut(BaseModel):
    """
    Una fila de ``audit_log``, tal cual se guardó salvo el ``detail`` de ``query_console.*``,
    que sale enmascarado para quien no puede ejecutar SQL (``detail_masked``).

    Ninguna columna lleva secretos: ``detail`` se escribe sin credenciales (``services/audit``),
    ``api_token_id`` es el PK del token (nunca el bearer), ``grantor`` es el NOMBRE de la cuenta
    del gateway que ejecutó el DCL y ``grantee`` el usuario del motor beneficiario. Queda afuera
    ``updated_at``, que en una tabla append-only no significa nada.
    """

    id: int
    created_at: datetime
    request_id: str | None = None
    actor_type: str = Field(..., description="admin | api_token | system | anonymous")
    admin_id: int | None = Field(None, description="Usuario del gateway; NULL si fue un token")
    admin_username: str | None = Field(
        None, description="Username desnormalizado, o 'token:<token_id>' si fue un token"
    )
    api_token_id: int | None = Field(None, description="PK del token de agente, si lo fue")
    action: str
    target_type: str | None = None
    target_id: int | None = None
    server_id: int | None = None
    touched_engine: bool
    status: str = Field(..., description="attempt | success | error | failure | denied | …")
    detail: str | None = Field(
        None,
        description="El texto guardado; en `query_console.*` puede venir enmascarado (`detail_masked`)",
    )
    detail_json: Any | None = Field(
        None,
        description=(
            "`detail` parseado si es JSON (objeto o lista); null si es texto libre. "
            "Muchas acciones viejas guardan texto, no JSON"
        ),
    )
    detail_masked: bool = Field(
        False,
        description=(
            "true si `detail` es de una acción `query_console.*` y sus literales de SQL se "
            "reemplazaron por `?` porque quien lee no tiene `sql_console.execute` en el destino"
        ),
    )
    ip: str | None = None
    grantee: str | None = None
    privilege: str | None = None
    object_level: str | None = None
    object_name: str | None = None
    with_grant_option: bool | None = None
    grantor: str | None = None
