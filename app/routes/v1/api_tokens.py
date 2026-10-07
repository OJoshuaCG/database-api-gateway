"""
Endpoints de los tokens de agente (``/api-tokens``).

Bajo sesión humana y REST convencional con ``ApiResponse[T]``: es la SPA la que administra
tokens, no un agente. Un token **no puede emitir otro token** — eso está garantizado por el techo
de agente, que excluye `access.admin` y `tokens.own`.

DOS CAPACIDADES, LAS MISMAS RUTAS: ``access.admin`` administra los tokens de TODOS; ``tokens.own``
(la tienen los tres roles) administra SOLO los que la persona emitió. Una sola implementación del
techo del emisor, del step-up y de la auditoría, con el guard ``AccessAdminOrOwnTokens`` y el
acotado por dueño (``owner_scope_of``) en cada handler. El porqué de no duplicar endpoints está en
``authz.require_either`` y en ``docs/development/decisiones-e-incidentes.md``.
"""

from fastapi import APIRouter, Request

from app.controllers.api_token_controller import ApiTokenController, owner_scope_of
from app.core.authz import AccessAdminOrOwnTokens
from app.core.limiter import limiter
from app.schemas.api_token import ApiTokenCreate, ApiTokenCreatedOut, ApiTokenOut, ApiTokenUpdate
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/api-tokens", tags=["API Tokens"])


@router.get("", response_model=ApiResponse[list[ApiTokenOut]])
def list_api_tokens(actor: AccessAdminOrOwnTokens, pagination: PaginationDep):
    """
    Los tokens emitidos, con su estado. **Sin el secreto**, que no se guarda.

    ``access.admin`` ve todos; quien solo tiene ``tokens.own`` ve solo los que emitió (filtrado en
    el servidor, también ``total`` y la paginación).
    """
    items, total = ApiTokenController().list_tokens(
        limit=pagination.size, offset=pagination.offset, owner_scope=owner_scope_of(actor)
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ApiTokenCreatedOut], status_code=201)
@limiter.limit("10/minute")
def create_api_token(request: Request, actor: AccessAdminOrOwnTokens, payload: ApiTokenCreate):
    """
    Emite un token y devuelve el bearer **una sola vez**.

    Con ``tokens.own`` sin ``access.admin`` (autoservicio), los scopes tienen que estar dentro de lo
    que el emisor puede hoy y el emisor tiene que poder ver el proyecto (403 ``access.forbidden`` /
    422 ``project.not_found``).

    Los scopes se validan contra el **techo de agente**, que excluye toda capacidad que mute o
    divulgue: un token no puede recibir una ni por error del operador. La única excepción es el
    par cerrado ``data.read`` / ``data.query``: exige un **step-up fresco del emisor** (403
    ``access.step_up_required`` si la ventana venció), deja rastro ``api_token.data_scope_grant``
    (fail-closed). Si ``MCP_DATA_TOKEN_MAX_TTL_DAYS`` es >= 1, además limita la vida del token a
    ese tope (422 ``api_token.ttl_too_long``); con 0, el valor por defecto, rige solo el tope
    general ``MCP_TOKEN_MAX_TTL_DAYS``. Con el kill switch de datos apagado el scope queda inerte. La
    intersección se vuelve a aplicar al autenticar, o sea que es fail-closed en el lector además
    del escritor.

    **Distribución**: en el `.mcp.json` del repo consumidor va por **expansión de variable de
    entorno**, nunca el literal. El gate de secretos del CI protege *este* repo; el token se
    commitea en los repos de otra gente, así que ahí hace falta su propia regla de escaneo.
    """
    return success(
        data=ApiTokenController().create_token(
            payload.model_dump(), admin=actor, owner_scope=owner_scope_of(actor)
        ),
        message="Token emitido. Copialo ahora: no se vuelve a mostrar.",
    )


@router.patch("/{token_pk}", response_model=ApiResponse[ApiTokenOut])
def update_api_token(actor: AccessAdminOrOwnTokens, token_pk: int, payload: ApiTokenUpdate):
    """
    Reemplaza los **scopes** del token, sin reemitirlo: el bearer no cambia.

    Lista completa (no suma/resta) y se valida contra el **techo de agente**, igual que el alta:
    422 `api_token.scope_not_allowed` con `allowed`. Lista vacía es 422; un token revocado es
    409 `api_token.already_revoked`. Exige `access.admin` o `tokens.own` (esta última, solo sobre tokens propios: uno ajeno es 404)
    y step-up (todo método no seguro).
    Agregar un scope de datos exige el mismo step-up fresco del emisor y, solo si
    ``MCP_DATA_TOKEN_MAX_TTL_DAYS`` es >= 1, que al token le queden como máximo esos días de vida
    (422 ``api_token.ttl_too_long``).
    Ampliar un token ya repartido amplía lo que puede hacer quien lo tenga. El cambio rige desde
    la llamada siguiente del agente.
    """
    return success(
        data=ApiTokenController().update_token(
            token_pk, payload.model_dump(), admin=actor, owner_scope=owner_scope_of(actor)
        ),
        message="Scopes del token actualizados.",
    )


@router.delete("/{token_pk}", response_model=ApiResponse[ApiTokenOut])
def revoke_api_token(actor: AccessAdminOrOwnTokens, token_pk: int):
    """
    Revoca el token. **No hay reactivar**: ``revoked_at`` no se deshace.

    Un ciclo revocar/reactivar dejaría un token que alguien creyó muerto y no lo está, y eso es
    peor que emitir uno nuevo. Si ya estaba revocado responde 409, para que quien lo pide sepa
    que no fue su acción la que cortó el acceso.
    """
    return success(
        data=ApiTokenController().revoke_token(
            token_pk, admin=actor, owner_scope=owner_scope_of(actor)
        ),
        message="Token revocado.",
    )
