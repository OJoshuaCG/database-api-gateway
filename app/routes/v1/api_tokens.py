"""
Endpoints de los tokens de agente (``/api-tokens``).

Bajo sesión de administrador y REST convencional con ``ApiResponse[T]``: es la SPA la que
administra tokens, no un agente. Un token **no puede emitir otro token** — eso está garantizado
por el techo de agente, que excluye `gateway.admin`.
"""

from fastapi import APIRouter, Request

from app.controllers.api_token_controller import ApiTokenController
from app.core.authz import GatewayAdmin
from app.core.limiter import limiter
from app.schemas.api_token import ApiTokenCreate, ApiTokenCreatedOut, ApiTokenOut
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/api-tokens", tags=["API Tokens"])


@router.get("", response_model=ApiResponse[list[ApiTokenOut]])
def list_api_tokens(actor: GatewayAdmin, pagination: PaginationDep):
    """Los tokens emitidos, con su estado. **Sin el secreto**, que no se guarda."""
    items, total = ApiTokenController().list_tokens(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ApiTokenCreatedOut], status_code=201)
@limiter.limit("10/minute")
def create_api_token(request: Request, actor: GatewayAdmin, payload: ApiTokenCreate):
    """
    Emite un token y devuelve el bearer **una sola vez**.

    Los scopes se validan contra el **techo de agente**, que excluye toda capacidad que mute o
    divulgue: un token no puede recibir una ni por error del operador. La intersección se
    vuelve a aplicar al autenticar, o sea que es fail-closed en el lector además del escritor.

    **Distribución**: en el `.mcp.json` del repo consumidor va por **expansión de variable de
    entorno**, nunca el literal. El gate de secretos del CI protege *este* repo; el token se
    commitea en los repos de otra gente, así que ahí hace falta su propia regla de escaneo.
    """
    return success(
        data=ApiTokenController().create_token(payload.model_dump(), admin=actor),
        message="Token emitido. Copialo ahora: no se vuelve a mostrar.",
    )


@router.delete("/{token_pk}", response_model=ApiResponse[ApiTokenOut])
def revoke_api_token(actor: GatewayAdmin, token_pk: int):
    """
    Revoca el token. **No hay reactivar**: ``revoked_at`` no se deshace.

    Un ciclo revocar/reactivar dejaría un token que alguien creyó muerto y no lo está, y eso es
    peor que emitir uno nuevo. Si ya estaba revocado responde 409, para que quien lo pide sepa
    que no fue su acción la que cortó el acceso.
    """
    return success(
        data=ApiTokenController().revoke_token(token_pk, admin=actor),
        message="Token revocado.",
    )
