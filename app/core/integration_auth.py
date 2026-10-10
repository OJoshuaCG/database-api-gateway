"""
Authentication and authorization of the integration API:
``Authorization: Bearer datumint.<public_id>.<secret>``.

An integration token is a bearer credential that MUTATES third-party databases, so every gate of
this module is fail-closed and the order of the gates is part of the contract (``_authorize_call``).

THE KILL SWITCH IS A SINGLE CHOKE POINT
---------------------------------------
``INTEGRATION_API_ENABLED`` is evaluated here and not in each route: if it were spread over the
routes, turning it off would stop working the day somebody adds a route and forgets the line.
The management endpoints (``integration_token_controller``) read it through
``integration_api_enabled()`` so tests have ONE place to patch.

IT IS BEARER-ONLY
-----------------
This module never reads cookies and the session dependency never reads ``Authorization``. If the
two paths could be mixed, the CSRF exemption that bearer clients have (correct, a bearer is not
ambient authority) would become a bypass. CSRF is enforced in ``authz._identify`` only for session
actors, so a bearer route simply never goes through it.

EVERY CREDENTIAL FAILURE IS THE SAME 401
----------------------------------------
Unknown, malformed, wrong secret, revoked, expired and inactive issuer answer with byte-identical
bodies: any difference is an oracle on the state of tokens somebody guessed. The reason goes to
the audit trail only, and the 401 is built at ONE site (``_reject``) because in development the
error body carries the file/line where the exception was created.

REJECTIONS ARE LIMITED PER IP AND AUDITED AGGREGATED
----------------------------------------------------
Same design and same trade-off as ``mcp_auth`` (read its docstring for the full reasoning): each
rejection spends one slot of a per-IP quota, an IP over its quota gets 429 BEFORE any database
access, and the audit writes at most one row per IP per window carrying how many rejections it
left out. The declared cost: a legitimate client behind the same IP as an attacker (a shared CI
runner) also gets 429 until the window drains.

THE TOKEN INHERITS, AND NEVER EXCEEDS, ITS ISSUER
--------------------------------------------------
Effective scopes = stored scopes ∩ closed vocabulary ∩ what the issuer holds TODAY, re-evaluated on
every request. Layer 2 (per-environment role) is evaluated with the issuer's real role at the
target. The allowlists (servers, blueprints) only narrow, never widen.

NO STEP-UP, BY DESIGN (D6)
--------------------------
``assert_step_up`` fails closed for machine actors, and this dependency never calls it: the step-up
of the human is paid when the scope is added to a token (``integration_token_controller``), not on
every call, because a machine cannot answer a password prompt. Routes that use this dependency
carry ``__gw_integration_scope__``, the enumerable marker that ``scripts/check_route_capabilities.py``
verifies.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from hmac import new as hmac_new
from math import ceil
from time import monotonic, time

from fastapi import Depends, Request
from limits import parse
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.actor import Actor, integration_actor
from app.core.audit_aggregator import WindowedAggregator
from app.core.crypto import integration_token_pepper
from app.core.environments import (
    INTEGRATION_API_ENABLED,
    INTEGRATION_AUTH_FAILURE_RATE_LIMIT,
    INTEGRATION_DESTRUCTIVE_RATE_LIMIT,
    INTEGRATION_RATE_LIMIT,
    INTEGRATION_WRITE_RATE_LIMIT,
)
from app.core.integration_token_format import parse_integration_bearer
from app.core.limiter import (
    INTEGRATION_BUCKET_BASE,
    INTEGRATION_BUCKET_DESTRUCTIVE,
    INTEGRATION_BUCKET_WRITE,
    hit_or_429,
    integration_limiter,
)

# The phantom-issuer guard (NULL, nonexistent or inactive user) lives in ONE place, and an
# integration token has exactly the same issuer semantics as an MCP token.
from app.core.mcp_auth import _load_issuer
from app.core.scope import ScopeTarget, assert_layer2, resolve_points
from app.exceptions import AppHttpException
from app.models.integration_token import (
    IntegrationToken,
    IntegrationTokenBlueprint,
    IntegrationTokenServer,
)
from app.models.managed_database import ManagedDatabase
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED,
    CODE_INTEGRATION_DISABLED,
    CODE_INTEGRATION_SCOPE_MISSING,
    CODE_INTEGRATION_SERVER_NOT_ALLOWED,
    CODE_INTEGRATION_TOKEN_INVALID,
    INTEGRATION_ALLOWED,
    TIER_DESTRUCTIVE,
    IntegrationScope,
    effective_integration_scopes,
    integration_scope_spec,
    parse_stored_integration_scopes,
)

#: How often, at most, ``last_used_at`` is written. One UPDATE per request would be free write
#: amplification on the shared metadata database.
LAST_USED_RESOLUTION = timedelta(seconds=60)

#: Window of the rejection audit: at most one row per IP per window.
AUDIT_WINDOW_SECONDS = 60.0
#: Cap on the IPs the aggregator remembers. Without it an attacker with an IPv6 block would grow
#: the dictionary without bound: the structure that exists to bound a resource would be another
#: unbounded one.
AUDIT_MAX_IPS = 10_000

#: Limiter bucket names. A token's buckets are separate keys inside the same limiter instance:
#: spending one never spends another, nor another token's.
LIMITER_NAMESPACE = "integration"
FAILURE_BUCKET = "auth_failure"

#: The smallest ``Retry-After`` a client is ever told. ``0`` would invite an immediate retry
#: that fails again.
MIN_RETRY_AFTER_SECONDS = 1

#: ``ScopeTarget.kind`` of a managed database (key of ``scope_targets.TARGET_KINDS``).
DATABASE_TARGET_KIND = "database"

AUDIT_ACTION_AUTH = "integration.auth"
AUDIT_ACTION_CALL = "integration.call"
AUDIT_TARGET_TYPE = "integration_token"
#: ``audit_log.actor_type`` of a failed authentication: whoever fails is NOT the token they claim.
AUDIT_ANONYMOUS_ACTOR_TYPE = "anonymous"
UNKNOWN_IP = "unknown"

#: Rejection reasons (closed vocabulary: they are read during an incident, and free text per call
#: site makes them ungroupable). They go to the audit trail and NEVER to the response.
REASON_DISABLED = "disabled"
REASON_IP_LIMIT = "ip_limit"
REASON_MISSING_BEARER = "missing_bearer"
REASON_MALFORMED = "malformed"
REASON_UNKNOWN_TOKEN = "unknown_token"
REASON_BAD_HMAC = "bad_hmac"
REASON_REVOKED = "revoked"
REASON_EXPIRED = "expired"
REASON_ISSUER_INACTIVE = "issuer_inactive"

_BEARER_PREFIX = "bearer "

#: Marker stamped on every dependency built by ``require_integration``.
INTEGRATION_SCOPE_MARKER = "__gw_integration_scope__"


def integration_api_enabled() -> bool:
    """
    The current value of the kill switch. Read from the module global on every call (never
    cached) so a test, or a config reload, takes effect on the next request.
    """
    return bool(INTEGRATION_API_ENABLED)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _session():
    from app.core.database import Database

    return Database().get_declarative_base_session()


def integration_token_hmac(secret: str) -> str:
    """HMAC-SHA256 of the secret under the integration pepper, hex-encoded."""
    return hmac_new(integration_token_pepper(), secret.encode("utf-8"), sha256).hexdigest()


# The clock is read from THIS module (``monotonic``) so tests can advance it by patching
# ``integration_auth.monotonic``.
_rejection_audit = WindowedAggregator(
    window=AUDIT_WINDOW_SECONDS, max_keys=AUDIT_MAX_IPS, clock=lambda: monotonic()
)


def reset_integration_auth_state() -> None:
    """
    Forgets the rejection aggregator and every limiter quota. For tests: the in-memory storage
    lives as long as the process, and without this the rejections of one test would spend the
    quota of the next.
    """
    _rejection_audit.reset()
    integration_limiter.reset()


@dataclass(frozen=True, slots=True)
class IntegrationCall:
    """
    What a route receives from ``require_integration``.

    It returns more than an ``Actor`` (unlike ``require_at``) because the allowlists travel with
    the call: list operations filter their rows by ``allowed_server_ids`` and the assignment
    operations validate the requested blueprint against ``allowed_blueprint_ids``.
    """

    actor: Actor
    scope: IntegrationScope
    allowed_server_ids: frozenset[int]
    allowed_blueprint_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class _AuthenticatedToken:
    """A token that passed every credential check, copied out of the ORM session."""

    token_pk: int
    public_id: str
    name: str
    stored_scopes: tuple[str, ...]
    issuer: Actor
    allowed_server_ids: frozenset[int]
    allowed_blueprint_ids: frozenset[int]


def _client_ip(request: Request) -> str:
    return get_remote_address(request) or UNKNOWN_IP


def _seconds_until_window_reset(rate_item, identifiers: tuple[str, ...]) -> int:
    """
    Seconds a client must wait before its quota refills, for the ``Retry-After`` header.

    Falls back to the whole window when the storage cannot answer: a longer wait is safe, a
    missing or zero value is not. The result is clamped to ``[1, window]``.
    """
    window_seconds = int(rate_item.get_expiry())
    try:
        window_stats = integration_limiter.limiter.get_window_stats(rate_item, *identifiers)
        seconds_until_reset = ceil(window_stats.reset_time - time())
    except Exception:  # noqa: BLE001 — a limiter storage hiccup must not become a 500
        seconds_until_reset = window_seconds
    return max(MIN_RETRY_AFTER_SECONDS, min(seconds_until_reset, window_seconds))


def _hit_or_429(rate: str, *identifiers: str) -> None:
    """``hit_or_429`` that tells the client when to retry (``Retry-After``)."""
    try:
        hit_or_429(integration_limiter, rate, *identifiers)
    except RateLimitExceeded as exceeded:
        # Read by ``rate_limit_handler``; the other limiters of the app never set it.
        exceeded.retry_after_seconds = _seconds_until_window_reset(parse(rate), identifiers)
        raise


def _failure_quota_exhausted(ip: str) -> bool:
    """
    Has the IP already spent its rejection quota? ``test`` and not ``hit``: asking does not
    consume. ``_reject`` consumes, one slot per real rejection.
    """
    if not integration_limiter.enabled:
        return False
    return not integration_limiter.limiter.test(
        parse(INTEGRATION_AUTH_FAILURE_RATE_LIMIT), LIMITER_NAMESPACE, FAILURE_BUCKET, ip
    )


def _audit_rejection(ip: str, reason: str, public_id: str | None = None) -> None:
    """The rejection row, only when the aggregator admits it. Best-effort, like any audit."""
    omitted_since_last_row = _rejection_audit.admit(ip)
    if omitted_since_last_row is None:
        return
    from app.services import audit

    audit.record(
        AUDIT_ACTION_AUTH,
        status="failure",
        admin=None,
        actor_type=AUDIT_ANONYMOUS_ACTOR_TYPE,
        target_type=AUDIT_TARGET_TYPE,
        touched_engine=False,
        # ``public_id`` is the PUBLIC part of the bearer. The secret never appears, whole or cut:
        # a secret prefix in a log is a secret in a log.
        detail=(
            f"rejection={reason}"
            + (f" token={public_id}" if public_id else "")
            + f" ip={ip} aggregated={omitted_since_last_row}"
        ),
    )


def _reject(
    request: Request, reason: str, *, public_id: str | None = None
) -> AppHttpException:
    """
    The opaque 401, with the reason in the AUDIT and not in the response.

    It spends one slot of the IP's rejection quota; if that was the last one, what is raised is
    the 429 and not the 401 (both reveal the same thing: that the credential did not work).

    Best-effort audit: a failure while auditing must not turn into a failure while rejecting.
    Fail-closed auditing is reserved for what discloses data, and nothing is disclosed here.
    """
    ip = _client_ip(request)
    _audit_rejection(ip, reason, public_id)
    _hit_or_429(INTEGRATION_AUTH_FAILURE_RATE_LIMIT, LIMITER_NAMESPACE, FAILURE_BUCKET, ip)
    return AppHttpException(
        message="Credencial de integración inválida.",
        status_code=401,
        public_context={"code": CODE_INTEGRATION_TOKEN_INVALID},
    )


def _authenticate_token(request: Request) -> _AuthenticatedToken:
    """
    Steps 3-7 of the flow: parse the bearer, look the token up, verify the secret, check
    revocation, expiry and issuer. Raises the opaque 401 on any failure.

    Never reads cookies: see the module docstring.
    """
    raw_header = request.headers.get("authorization") or ""
    if not raw_header.lower().startswith(_BEARER_PREFIX):
        raise _reject(request, REASON_MISSING_BEARER)
    bearer_parts = parse_integration_bearer(raw_header[len(_BEARER_PREFIX) :].strip())
    if bearer_parts is None:
        raise _reject(request, REASON_MALFORMED)

    now = _utcnow()
    session = _session()
    try:
        row = (
            session.query(IntegrationToken)
            .filter(IntegrationToken.token_id == bearer_parts.public_id)
            .one_or_none()
        )
        if row is None:
            # The HMAC is paid anyway: it narrows the timing signal. The security argument is the
            # entropy of the identifier.
            integration_token_hmac(bearer_parts.secret)
            raise _reject(request, REASON_UNKNOWN_TOKEN, public_id=bearer_parts.public_id)

        secret_matches = compare_digest(
            row.secret_hmac, integration_token_hmac(bearer_parts.secret)
        )
        if not secret_matches:
            raise _reject(request, REASON_BAD_HMAC, public_id=bearer_parts.public_id)
        if row.revoked_at is not None:
            raise _reject(request, REASON_REVOKED, public_id=bearer_parts.public_id)
        # NULL means a non-expiring token (see INTEGRATION_ALLOW_NON_EXPIRING_TOKENS).
        if row.expires_at is not None and row.expires_at <= now:
            raise _reject(request, REASON_EXPIRED, public_id=bearer_parts.public_id)

        # The token delegates to its issuer, who is re-read on EVERY request: deactivating or
        # demoting the issuer takes effect on the next call.
        issuer = _load_issuer(row.created_by_admin_id)
        if issuer is None:
            raise _reject(request, REASON_ISSUER_INACTIVE, public_id=bearer_parts.public_id)

        if row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION:
            row.last_used_at = now
            session.commit()

        allowed_server_ids = frozenset(
            server_id
            for (server_id,) in session.query(IntegrationTokenServer.server_id).filter(
                IntegrationTokenServer.token_pk == row.id
            )
        )
        allowed_blueprint_ids = frozenset(
            model_id
            for (model_id,) in session.query(IntegrationTokenBlueprint.model_id).filter(
                IntegrationTokenBlueprint.token_pk == row.id
            )
        )
        return _AuthenticatedToken(
            token_pk=row.id,
            public_id=row.token_id,
            name=row.name,
            stored_scopes=tuple(parse_stored_integration_scopes(row.scopes)),
            issuer=issuer,
            allowed_server_ids=allowed_server_ids,
            allowed_blueprint_ids=allowed_blueprint_ids,
        )
    finally:
        session.close()


def _server_not_allowed() -> AppHttpException:
    """
    The 403 for a target outside the server allowlist. A nonexistent target answers the same
    thing: telling "does not exist" apart from "not yours" would be an existence oracle.
    """
    return AppHttpException(
        message="El token no puede operar sobre este destino.",
        status_code=403,
        public_context={"code": CODE_INTEGRATION_SERVER_NOT_ALLOWED},
    )


def _assert_target_within_allowlists(token: _AuthenticatedToken, target: ScopeTarget) -> None:
    """
    Step 10: every point of the target must sit on an allowlisted server, and a database that has
    a blueprint must have one the token is allowed to touch.

    Fail-closed on purpose: no points (a blueprint without databases) or a point without server
    (a nonexistent target) is a 403, never "nothing to check".
    """
    points = resolve_points(target)
    if not points:
        raise _server_not_allowed()
    for point in points:
        if point.server_id is None or point.server_id not in token.allowed_server_ids:
            raise _server_not_allowed()

    if target.kind != DATABASE_TARGET_KIND or not token.allowed_blueprint_ids:
        return
    (database_id,) = target.params
    session = _session()
    try:
        assigned_blueprint_id = (
            session.query(ManagedDatabase.model_id)
            .filter(ManagedDatabase.id == database_id)
            .scalar()
        )
    finally:
        session.close()
    # A database with no blueprint has nothing for the allowlist to restrict: assigning one is
    # validated against the allowlist by the assignment operation itself.
    if assigned_blueprint_id is not None and assigned_blueprint_id not in token.allowed_blueprint_ids:
        raise AppHttpException(
            message="El token no puede operar con el blueprint de esta base.",
            status_code=403,
            public_context={"code": CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED},
        )


def _authorize_call(
    request: Request, scope: IntegrationScope, resolved_target: ScopeTarget | None
) -> IntegrationCall:
    """
    The whole flow, in order. The order is deliberate: each gate is cheaper, or reveals less,
    than the next one.

    1. Kill switch (503).  2. Per-IP failure quota, BEFORE the database.  3-7. Credential
    (opaque 401).  8. Effective scope (403).  9. Per-token quotas (429 + Retry-After).
    10. Allowlists (403).  11. Layer 2 with the issuer's real role at the target (403).
    12. Actor, audit of the call.
    """
    client_ip = _client_ip(request)

    if not integration_api_enabled():
        # Aggregated like the rest: with the API off, one row per request would still be free
        # writes. 503 and not 404: the operator who turned it on and sees it fail needs to know
        # it is OFF, and the switch is configuration, not a secret.
        _audit_rejection(client_ip, REASON_DISABLED)
        raise AppHttpException(
            message="La API de integración está deshabilitada.",
            status_code=503,
            public_context={"code": CODE_INTEGRATION_DISABLED},
        )

    if _failure_quota_exhausted(client_ip):
        # Before reading the database and before paying the HMAC: this is what makes the cap a
        # COST cap and not just a row cap. Counted in the aggregator so the next row states the volume.
        _audit_rejection(client_ip, REASON_IP_LIMIT)
        _hit_or_429(
            INTEGRATION_AUTH_FAILURE_RATE_LIMIT, LIMITER_NAMESPACE, FAILURE_BUCKET, client_ip
        )

    token = _authenticate_token(request)

    effective_scopes = effective_integration_scopes(
        token.stored_scopes, token.issuer.capabilities
    )
    if scope not in effective_scopes:
        # Same answer for a scope the token never had and one the issuer lost (suspended): the
        # response does not say which, and does not name the capability.
        raise AppHttpException(
            message="El token no tiene permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_INTEGRATION_SCOPE_MISSING},
        )

    _hit_or_429(INTEGRATION_RATE_LIMIT, LIMITER_NAMESPACE, INTEGRATION_BUCKET_BASE, token.public_id)
    scope_spec = integration_scope_spec(scope)
    if scope_spec.mutates:
        _hit_or_429(
            INTEGRATION_WRITE_RATE_LIMIT, LIMITER_NAMESPACE, INTEGRATION_BUCKET_WRITE, token.public_id
        )
    if scope_spec.tier == TIER_DESTRUCTIVE:
        # Third, narrowest bucket, on top of the other two: rollback and stamp of one token spend
        # the SAME slots, so alternating between them does not double the quota.
        _hit_or_429(
            INTEGRATION_DESTRUCTIVE_RATE_LIMIT,
            LIMITER_NAMESPACE,
            INTEGRATION_BUCKET_DESTRUCTIVE,
            token.public_id,
        )

    if resolved_target is not None:
        _assert_target_within_allowlists(token, resolved_target)

    mapped_capability = INTEGRATION_ALLOWED[scope]
    # Layer 1 is strictly the capability of the CALLED scope, not every scope of the token.
    actor = integration_actor(
        token_pk=token.token_pk,
        public_id=token.public_id,
        name=token.name,
        capabilities=frozenset({mapped_capability}),
        issuer=token.issuer,
    )
    if resolved_target is not None:
        # Scopes without a target (list operations) are layer 1 only: their rows are filtered by
        # the allowlists the call carries.
        assert_layer2(actor, mapped_capability, resolved_target)

    from app.services import audit

    audit.record(
        AUDIT_ACTION_CALL,
        admin=actor,
        target_type=AUDIT_TARGET_TYPE,
        target_id=token.token_pk,
        touched_engine=False,
        detail=f"scope={scope.value} method={request.method} path={request.url.path}",
    )
    return IntegrationCall(
        actor=actor,
        scope=scope,
        allowed_server_ids=token.allowed_server_ids,
        allowed_blueprint_ids=token.allowed_blueprint_ids,
    )


def require_integration(
    scope: IntegrationScope, *, target: Callable[..., ScopeTarget] | None = None
) -> Callable[..., IntegrationCall]:
    """
    Dependency factory for a route of the integration API. Returns an ``IntegrationCall``.

    ``target`` is a pure resolver of ``app.core.scope_targets`` (same contract as ``require_at``);
    an unregistered resolver fails with ``KeyError`` when the route is imported, not at runtime.

    Two inner variants because ``Depends(None)`` is not expressible: FastAPI must be handed the
    resolver as a sub-dependency only when there is one.

    Stamps ``__gw_integration_scope__`` (the enumerable marker of the route coverage script),
    ``__gw_capability__`` (the mapped capability) and, with a target, ``__gw_scope__``.
    """
    mapped_capability = INTEGRATION_ALLOWED[scope]

    if target is None:

        def _dependency(request: Request) -> IntegrationCall:
            return _authorize_call(request, scope, None)

        target_kind = None
    else:
        from app.core.scope_targets import TARGET_KINDS

        target_kind = TARGET_KINDS[target]

        def _dependency(  # type: ignore[misc]
            request: Request, resolved_target: ScopeTarget = Depends(target)
        ) -> IntegrationCall:
            return _authorize_call(request, scope, resolved_target)

    setattr(_dependency, INTEGRATION_SCOPE_MARKER, scope.value)
    _dependency.__gw_capability__ = mapped_capability.value  # type: ignore[attr-defined]
    if target_kind is not None:
        _dependency.__gw_scope__ = target_kind  # type: ignore[attr-defined]
    _dependency.__name__ = f"require_integration_{scope.value.replace('.', '_')}"
    return _dependency


def declared_integration_scope(dependency: Callable) -> str | None:
    """
    The integration scope a dependency declares, or ``None``.

    Lives next to the producer so that producer and reader of the marker cannot drift apart.
    """
    return getattr(dependency, INTEGRATION_SCOPE_MARKER, None)
