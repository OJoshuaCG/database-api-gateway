"""
Endpoints of the integration tokens (``/integration-tokens``).

Under a HUMAN session and conventional REST with ``ApiResponse[T]``: it is the SPA that manages
tokens, never a bearer. A bearer is rejected here because these routes only resolve a session, so
an integration token cannot issue, widen or revoke tokens (the integration scope vocabulary has no
token-management scope).

TWO CAPABILITIES, THE SAME ROUTES: ``access.admin`` lists and revokes the tokens of EVERYONE (and
edits none); ``integration_tokens.own`` (all three roles) manages only the ones the person issued.
One implementation of the ceiling, the step-up and the audit, with the guard
``AccessAdminOrOwnIntegrationTokens`` and the owner filter in the controller.
"""

from fastapi import APIRouter, Request

from app.controllers.api_token_controller import owner_scope_of
from app.controllers.integration_token_controller import IntegrationTokenController
from app.core.authz import AccessAdminOrOwnIntegrationTokens
from app.core.limiter import limiter
from app.schemas.integration_token import (
    IntegrationCeilingOut,
    IntegrationTokenCreate,
    IntegrationTokenCreatedOut,
    IntegrationTokenOut,
    IntegrationTokenUpdate,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/integration-tokens", tags=["Integration Tokens"])


@router.get("", response_model=ApiResponse[list[IntegrationTokenOut]])
def list_integration_tokens(actor: AccessAdminOrOwnIntegrationTokens, pagination: PaginationDep):
    """
    The issued tokens with their state. Never the secret, which is not stored.

    ``access.admin`` sees every token; whoever only has ``integration_tokens.own`` sees the ones
    they issued (filtered on the server, including ``total`` and the pagination).
    ``suspended_scopes`` is reported only on the caller's own tokens.
    """
    items, total = IntegrationTokenController().list_tokens(
        actor=actor,
        limit=pagination.size,
        offset=pagination.offset,
        owner_scope=owner_scope_of(actor),
    )
    return paginated(items, total=total, pagination=pagination)


@router.get("/ceiling", response_model=ApiResponse[IntegrationCeilingOut])
def get_integration_ceiling(actor: AccessAdminOrOwnIntegrationTokens):
    """
    The scopes the caller may put on a new token, the lifetime rules and the kill-switch state.

    Scopes the caller does not hold are absent (no entry, flag or count).
    """
    return success(data=IntegrationTokenController().ceiling(actor))


@router.post("", response_model=ApiResponse[IntegrationTokenCreatedOut], status_code=201)
@limiter.limit("10/minute")
def create_integration_token(
    request: Request, actor: AccessAdminOrOwnIntegrationTokens, payload: IntegrationTokenCreate
):
    """
    Issues a token for the caller and returns the bearer ONCE.

    Every scope must be held by the caller today (403 ``integration_token.scope_not_allowed``) and
    belong to the closed vocabulary (422 ``integration_token.unknown_scope``). The server allowlist
    is mandatory (422 ``integration_token.server_allowlist_required``). The lifetime is capped at
    90 days with read scopes only, at 30 days with any write scope and at 7 days with any
    destructive scope (422 ``integration_token.ttl_too_long``). A destructive scope
    (``migrations.rollback``, ``migrations.stamp``) also needs a non-empty blueprint allowlist (422
    ``integration_token.blueprint_allowlist_required``). With the kill switch off it answers 503
    ``integration.disabled``. Needs a fresh step-up (every non-safe method does).
    """
    return success(
        data=IntegrationTokenController().create_token(payload.model_dump(), actor=actor),
        message="Token emitido. Copialo ahora: no se vuelve a mostrar.",
    )


@router.patch("/{token_pk}", response_model=ApiResponse[IntegrationTokenOut])
def update_integration_token(
    actor: AccessAdminOrOwnIntegrationTokens, token_pk: int, payload: IntegrationTokenUpdate
):
    """
    Edits name, note, scopes and allowlists of the caller's OWN token; the bearer does not change.

    A token of somebody else (even for ``access.admin``) is the same 404 as a missing one. Scopes
    are the complete list: a scope the issuer lost cannot be re-added, and omitting it removes it.
    Adding a write scope to a token that lives longer than 30 days, or a destructive scope to one
    that lives longer than 7, is 422 ``integration_token.ttl_too_long`` and the token stays
    unchanged. Adding a destructive scope asks for a fresh step-up again, and a token holding one
    cannot be left without a blueprint allowlist (422
    ``integration_token.blueprint_allowlist_required``).
    """
    return success(
        data=IntegrationTokenController().update_token(
            token_pk, payload.model_dump(exclude_unset=True), actor=actor
        ),
        message="Token actualizado.",
    )


@router.delete("/{token_pk}", response_model=ApiResponse[IntegrationTokenOut])
def revoke_integration_token(actor: AccessAdminOrOwnIntegrationTokens, token_pk: int):
    """
    Revokes the token. There is no reactivation.

    Already revoked is 409 ``integration_token.already_revoked``. Works with the kill switch off.
    """
    return success(
        data=IntegrationTokenController().revoke_token(
            token_pk, actor=actor, owner_scope=owner_scope_of(actor)
        ),
        message="Token revocado.",
    )
