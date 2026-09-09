"""Schemas de los tokens de agente."""

from datetime import datetime

from pydantic import BaseModel, Field


class ApiTokenCreate(BaseModel):
    """
    Alta de un token. **No hay campo de secreto**: lo genera el servidor.

    ``name`` es obligatorio y describe la máquina o el repo destino. No es burocracia: la
    revocación granular depende de que sea uno por uno, y un token compartido entre seis
    máquinas es un token que **nadie revoca**.
    """

    name: str = Field(..., min_length=3, max_length=128, description="Máquina o repo destino")
    project_id: int = Field(
        ...,
        ge=1,
        description=(
            "Obligatorio: un token sin proyecto no alcanzaría ninguna base, así que lo único "
            "que podría significar es 'token global'"
        ),
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="Vacío = solo 'blueprints.read'. Se valida contra el techo de agente",
    )
    expires_in_days: int | None = Field(
        None, ge=1, description="Default y tope: MCP_TOKEN_MAX_TTL_DAYS (90). Sin perpetuos"
    )
    note: str | None = None


class ApiTokenOut(BaseModel):
    """
    Un token. **Nunca lleva el secreto ni su HMAC.**

    ``token_id`` sí, porque es la parte pública y es lo que aparece en el rastro de auditoría:
    sin él, una fila `mcp.*` no se puede cruzar con el token que la originó.
    """

    id: int
    token_id: str
    name: str
    scopes: list[str]
    project_id: int
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    note: str | None = None
    active: bool
    created_at: datetime | None = None


class ApiTokenCreatedOut(ApiTokenOut):
    """
    El alta, con el bearer completo.

    **Se muestra UNA vez y no se guarda**: lo que persiste es su HMAC. Si se pierde, se emite
    otro — un sistema que pueda mostrarlo de nuevo es un sistema que lo tiene.
    """

    token: str = Field(
        ...,
        description=(
            "El bearer completo, formato 'dbgw.<id>.<secreto>'. Se muestra una sola vez. "
            "Distribuilo por variable de entorno en el .mcp.json del repo, nunca como literal"
        ),
    )
