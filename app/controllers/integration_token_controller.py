"""
Controller of the integration tokens (management API used by the SPA, under a human session).

THE SECRET IS SHOWN ONCE AND NOT STORED
---------------------------------------
Only its HMAC persists. If it is lost, another token is issued: a system able to show a secret
again is a system that holds it.

A TOKEN ACTS AS ITS ISSUER, NEVER BEYOND
----------------------------------------
Every scope put on a token must be held by the person issuing it TODAY (the ceiling). The token
re-applies the same intersection on every call (``integration_auth``), so a demoted issuer
suspends the scope on the next request; the stored row keeps it so promoting the issuer back
reactivates it without re-issuing. Because of that, editing is limited to the issuer: letting
``access.admin`` widen somebody else's token would hand that token the other person's role on
the admin's decision.

``access.admin`` lists and revokes every token and edits none. A person with only
``integration_tokens.own`` sees and touches their own tokens; a foreign token answers the same
404 as a missing one so ids cannot be enumerated.

STEP-UP
-------
It is enforced by the route guard (``require_either`` applies the ``access.admin`` step-up to
every non-safe method), which covers the issuance and every edit. For the common scopes it is
deliberately NOT repeated here: the machine that later uses the token can never answer a password
prompt (D6), so the human's fresh password is the only proof, and it is paid when the scope is
granted.

The DESTRUCTIVE tier (``migrations.rollback``, ``migrations.stamp``) is the exception (D17): the
issuer confirms again, explicitly and on ``blueprints.apply``, when the token is created with one
or when an edit ADDS one, even if the token already held ``migrations.apply_forward``. The route
guard checks the ``access.admin`` capability, which is not the one these scopes map to, so relying
on it would leave the guarded capability untested here. Both checks share one freshness window.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.actor import Actor, identity_of
from app.core.environments import (
    INTEGRATION_ALLOW_NON_EXPIRING_TOKENS,
    INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS,
    INTEGRATION_TOKEN_MAX_TTL_DAYS,
    INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS,
)
from app.core.integration_auth import integration_api_enabled, integration_token_hmac
from app.core.integration_token_format import mint_integration_token
from app.core.step_up import assert_step_up
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.integration_token import (
    IntegrationToken,
    IntegrationTokenBlueprint,
    IntegrationTokenServer,
)
from app.models.server import Server
from app.services import audit
from app.services.capability_catalog import Capability
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_DISABLED,
    CODE_INTEGRATION_TOKEN_ALREADY_REVOKED,
    CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED,
    CODE_INTEGRATION_TOKEN_BLUEPRINT_NOT_FOUND,
    CODE_INTEGRATION_TOKEN_NOT_FOUND,
    CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED,
    CODE_INTEGRATION_TOKEN_SERVER_ALLOWLIST_REQUIRED,
    CODE_INTEGRATION_TOKEN_SERVER_NOT_FOUND,
    CODE_INTEGRATION_TOKEN_NON_EXPIRING_NOT_ALLOWED,
    CODE_INTEGRATION_TOKEN_TTL_TOO_LONG,
    CODE_INTEGRATION_TOKEN_UNKNOWN_SCOPE,
    DESTRUCTIVE_TIER_SCOPES,
    INTEGRATION_ALLOWED,
    INTEGRATION_SCOPE_SPECS,
    STORED_SCOPES_SEPARATOR,
    IntegrationScope,
    effective_integration_scopes,
    integration_scope_spec,
    parse_stored_integration_scopes,
)

AUDIT_TARGET_TYPE = "integration_token"
AUDIT_MODE_OWN = "own"
AUDIT_MODE_ADMIN = "admin"

#: The closed vocabulary as plain strings, for validating what a client sends.
KNOWN_SCOPE_VALUES: frozenset[str] = frozenset(scope.value for scope in IntegrationScope)

#: The destructive tier as plain strings, for checking a stored scope column.
DESTRUCTIVE_SCOPE_VALUES: frozenset[str] = frozenset(
    scope.value for scope in DESTRUCTIVE_TIER_SCOPES
)

#: The capability the issuer confirms their password for when a destructive scope is granted: the
#: one both destructive scopes map to, i.e. the one a human is asked for in the SPA to roll back.
DESTRUCTIVE_STEP_UP_CAPABILITY = Capability.BLUEPRINTS_APPLY
#: Method the step-up rule of ``blueprints.apply`` is evaluated with (it applies to non-safe ones).
DESTRUCTIVE_STEP_UP_METHOD = "POST"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _audit_mode(owner_scope: int | None) -> str:
    return AUDIT_MODE_ADMIN if owner_scope is None else AUDIT_MODE_OWN


def _api_disabled() -> AppHttpException:
    """
    503 for issuing and editing while the kill switch is off. Listing and revoking stay available
    on purpose: switching the API off must never leave an operator unable to see or kill a token.
    """
    return AppHttpException(
        message="La API de integración está deshabilitada.",
        status_code=503,
        public_context={"code": CODE_INTEGRATION_DISABLED},
    )


def _token_not_found() -> AppHttpException:
    """One shape for a missing token and for somebody else's: no existence oracle."""
    return AppHttpException(
        message="Token no encontrado.",
        status_code=404,
        public_context={"code": CODE_INTEGRATION_TOKEN_NOT_FOUND},
    )


def _already_revoked() -> AppHttpException:
    return AppHttpException(
        message="Este token ya estaba revocado.",
        status_code=409,
        public_context={"code": CODE_INTEGRATION_TOKEN_ALREADY_REVOKED},
    )


def _scope_not_allowed() -> AppHttpException:
    """
    403 for a known scope the issuer does not hold. The body names no scope and no capability:
    saying which one is missing would describe what exists beyond the caller's role.
    """
    return AppHttpException(
        message="No puedes dar a un token un permiso que tú no tienes.",
        status_code=403,
        public_context={"code": CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED},
    )


def _ttl_too_long(max_days: int) -> AppHttpException:
    return AppHttpException(
        message=(
            f"Un token con esos permisos vive como máximo {max_days} días: la credencial queda "
            "en el repositorio y en los secretos de CI de otro proyecto."
        ),
        status_code=422,
        public_context={"code": CODE_INTEGRATION_TOKEN_TTL_TOO_LONG, "max_days": max_days},
    )


def _non_expiring_not_allowed() -> AppHttpException:
    return AppHttpException(
        message=("Este despliegue no permite tokens sin expiración. Indicá una vigencia en días."),
        status_code=422,
        public_context={"code": CODE_INTEGRATION_TOKEN_NON_EXPIRING_NOT_ALLOWED},
    )


def _parse_requested_scopes(raw_scopes: list[str]) -> list[IntegrationScope]:
    """
    The requested scopes as members of the closed vocabulary, de-duplicated, in catalog order.

    Anything outside the vocabulary is a 422, including real capabilities such as ``access.admin``
    or ``data.read``: the vocabulary is the whole attack surface a token can ever have.
    """
    requested_values = set(raw_scopes)
    unknown_values = requested_values - KNOWN_SCOPE_VALUES
    if unknown_values:
        raise AppHttpException(
            message="Alguno de los permisos pedidos no existe.",
            status_code=422,
            public_context={"code": CODE_INTEGRATION_TOKEN_UNKNOWN_SCOPE},
        )
    return [spec.scope for spec in INTEGRATION_SCOPE_SPECS if spec.scope.value in requested_values]


def _require_scopes_held_by_issuer(issuer: Actor, scopes: list[IntegrationScope]) -> None:
    for scope in scopes:
        if not issuer.has(INTEGRATION_ALLOWED[scope]):
            raise _scope_not_allowed()


def _has_write_scope(scopes: list[IntegrationScope]) -> bool:
    return any(integration_scope_spec(scope).mutates for scope in scopes)


def _has_destructive_scope(scopes: list[IntegrationScope]) -> bool:
    return any(scope in DESTRUCTIVE_TIER_SCOPES for scope in scopes)


def _holds_destructive_scope_value(stored_scope_values: list[str]) -> bool:
    """Same question over the raw column, where suspended or unknown values may also sit."""
    return any(value in DESTRUCTIVE_SCOPE_VALUES for value in stored_scope_values)


def _max_ttl_days_for(scopes: list[IntegrationScope]) -> int:
    """The strictest TTL that applies: 7 days with a destructive scope, 30 with a write one, else 90."""
    if _has_destructive_scope(scopes):
        return INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS
    if _has_write_scope(scopes):
        return INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS
    return INTEGRATION_TOKEN_MAX_TTL_DAYS


def _require_issuer_step_up_for_destructive_scopes(issuer: Actor) -> None:
    """D17: a fresh password confirmation, on the capability the destructive scopes map to."""
    assert_step_up(issuer, DESTRUCTIVE_STEP_UP_CAPABILITY, method=DESTRUCTIVE_STEP_UP_METHOD)


def _deduplicated(identifiers: list[int]) -> list[int]:
    return sorted(set(identifiers))


def _server_allowlist_required() -> AppHttpException:
    return AppHttpException(
        message="El token necesita al menos un servidor permitido.",
        status_code=422,
        public_context={"code": CODE_INTEGRATION_TOKEN_SERVER_ALLOWLIST_REQUIRED},
    )


def _server_not_found() -> AppHttpException:
    return AppHttpException(
        message="Alguno de los servidores permitidos no existe.",
        status_code=422,
        public_context={"code": CODE_INTEGRATION_TOKEN_SERVER_NOT_FOUND},
    )


def _blueprint_allowlist_required() -> AppHttpException:
    """
    A destructive scope without a blueprint allowlist would reach every blueprint the issuer can
    operate on: the allowlist is what bounds the blast radius of a leaked rollback/stamp token.
    """
    return AppHttpException(
        message=("Un token con permisos destructivos necesita al menos un blueprint permitido."),
        status_code=422,
        public_context={"code": CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED},
    )


def _blueprint_not_found() -> AppHttpException:
    return AppHttpException(
        message="Alguno de los blueprints permitidos no existe.",
        status_code=422,
        public_context={"code": CODE_INTEGRATION_TOKEN_BLUEPRINT_NOT_FOUND},
    )


class IntegrationTokenController:
    def _session(self):
        from app.core.database import Database

        return Database().get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Serialization                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_scopes(row: IntegrationToken, viewer: Actor) -> tuple[list[str], list[str]]:
        """
        ``(scopes, suspended_scopes)`` as the viewer is allowed to see them.

        Suspension is the difference between what is stored and what the ISSUER holds today, so it
        can be computed only for the viewer's own tokens (the viewer is the issuer). For somebody
        else's token, an ``access.admin`` sees the stored in-vocabulary scopes and no suspension:
        reading another person's current role through this endpoint would be a leak.
        """
        stored_scopes = parse_stored_integration_scopes(row.scopes)
        viewer_is_the_issuer = viewer.id == row.created_by_admin_id
        if not viewer_is_the_issuer:
            in_vocabulary = [value for value in stored_scopes if value in KNOWN_SCOPE_VALUES]
            return sorted(in_vocabulary), []
        effective_scopes = effective_integration_scopes(stored_scopes, viewer.capabilities)
        effective_values = {scope.value for scope in effective_scopes}
        suspended_values = [value for value in stored_scopes if value not in effective_values]
        return sorted(effective_values), sorted(suspended_values)

    def _serialize(
        self,
        row: IntegrationToken,
        viewer: Actor,
        server_ids: list[int],
        blueprint_ids: list[int],
    ) -> dict:
        """NEVER the secret or its HMAC. ``token_id`` is public and is what the audit trail shows."""
        scopes, suspended_scopes = self._split_scopes(row, viewer)
        return {
            "id": row.id,
            "token_id": row.token_id,
            "name": row.name,
            "scopes": scopes,
            "suspended_scopes": suspended_scopes,
            "server_ids": server_ids,
            "blueprint_ids": blueprint_ids,
            "created_by_admin_id": row.created_by_admin_id,
            "expires_at": row.expires_at,
            "last_used_at": row.last_used_at,
            "revoked_at": row.revoked_at,
            "note": row.note,
            "active": row.revoked_at is None
            and (row.expires_at is None or row.expires_at > _utcnow()),
            "created_at": row.created_at,
        }

    @staticmethod
    def _allowlists_of(
        session, token_pks: list[int]
    ) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
        """Both allowlists of every token in ``token_pks``, keyed by token pk, sorted by id."""
        servers_by_token: dict[int, list[int]] = {token_pk: [] for token_pk in token_pks}
        blueprints_by_token: dict[int, list[int]] = {token_pk: [] for token_pk in token_pks}
        if not token_pks:
            return servers_by_token, blueprints_by_token
        server_rows = (
            session.query(IntegrationTokenServer.token_pk, IntegrationTokenServer.server_id)
            .filter(IntegrationTokenServer.token_pk.in_(token_pks))
            .order_by(IntegrationTokenServer.server_id)
            .all()
        )
        for token_pk, server_id in server_rows:
            servers_by_token[token_pk].append(server_id)
        blueprint_rows = (
            session.query(IntegrationTokenBlueprint.token_pk, IntegrationTokenBlueprint.model_id)
            .filter(IntegrationTokenBlueprint.token_pk.in_(token_pks))
            .order_by(IntegrationTokenBlueprint.model_id)
            .all()
        )
        for token_pk, model_id in blueprint_rows:
            blueprints_by_token[token_pk].append(model_id)
        return servers_by_token, blueprints_by_token

    def _serialize_one(self, session, row: IntegrationToken, viewer: Actor) -> dict:
        servers_by_token, blueprints_by_token = self._allowlists_of(session, [row.id])
        return self._serialize(row, viewer, servers_by_token[row.id], blueprints_by_token[row.id])

    # ------------------------------------------------------------------ #
    # Allowlist validation and persistence                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_servers_exist(session, server_ids: list[int]) -> None:
        """
        SQLite does not enforce foreign keys and MariaDB does: validating here keeps the answer a
        domain 422 on both, instead of a 500 from the INSERT in production only.
        """
        existing_count = session.query(Server.id).filter(Server.id.in_(server_ids)).count()
        if existing_count != len(server_ids):
            raise _server_not_found()

    @staticmethod
    def _require_blueprints_exist(session, blueprint_ids: list[int]) -> None:
        if not blueprint_ids:
            return
        existing_count = (
            session.query(DatabaseModel.id).filter(DatabaseModel.id.in_(blueprint_ids)).count()
        )
        if existing_count != len(blueprint_ids):
            raise _blueprint_not_found()

    @staticmethod
    def _stored_blueprint_ids(session, token_pk: int) -> list[int]:
        return [
            model_id
            for (model_id,) in session.query(IntegrationTokenBlueprint.model_id).filter(
                IntegrationTokenBlueprint.token_pk == token_pk
            )
        ]

    @staticmethod
    def _replace_server_allowlist(session, token_pk: int, server_ids: list[int]) -> None:
        session.query(IntegrationTokenServer).filter(
            IntegrationTokenServer.token_pk == token_pk
        ).delete(synchronize_session=False)
        for server_id in server_ids:
            session.add(IntegrationTokenServer(token_pk=token_pk, server_id=server_id))

    @staticmethod
    def _replace_blueprint_allowlist(session, token_pk: int, blueprint_ids: list[int]) -> None:
        session.query(IntegrationTokenBlueprint).filter(
            IntegrationTokenBlueprint.token_pk == token_pk
        ).delete(synchronize_session=False)
        for blueprint_id in blueprint_ids:
            session.add(IntegrationTokenBlueprint(token_pk=token_pk, model_id=blueprint_id))

    def _translate_allowlist_race(self, session, server_ids: list[int], blueprint_ids: list[int]):
        """
        A server or blueprint deleted between the existence check and the INSERT surfaces as an
        IntegrityError on MariaDB. Translate it only when that is really the cause; any other
        violation stays an honest 500.
        """
        session.rollback()
        self._require_servers_exist(session, server_ids)
        self._require_blueprints_exist(session, blueprint_ids)

    # ------------------------------------------------------------------ #
    # Operations                                                          #
    # ------------------------------------------------------------------ #

    def ceiling(self, actor: Actor) -> dict:
        """
        What ``actor`` may put on a new token, plus the lifetime rules.

        Only the scopes the actor holds appear: an unheld scope has no entry, flag or count, so the
        endpoint cannot be used to learn what exists beyond the caller's role.
        """
        held_scope_entries = [
            {
                "scope": spec.scope.value,
                "label": spec.label,
                "mutates": spec.mutates,
                "tier": spec.tier,
            }
            for spec in INTEGRATION_SCOPE_SPECS
            if actor.has(spec.capability)
        ]
        return {
            "enabled": integration_api_enabled(),
            "scopes": held_scope_entries,
            "max_ttl_days": INTEGRATION_TOKEN_MAX_TTL_DAYS,
            "max_write_ttl_days": INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS,
            "max_destructive_ttl_days": INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS,
            "allow_non_expiring": INTEGRATION_ALLOW_NON_EXPIRING_TOKENS,
        }

    def list_tokens(
        self, *, actor: Actor, limit: int, offset: int, owner_scope: int | None
    ) -> tuple[list[dict], int]:
        """
        ``owner_scope`` (see ``owner_scope_of``) restricts the list to that user's tokens. The
        filter runs BEFORE the count so ``total`` and the pagination do not reveal how many
        foreign tokens exist.
        """
        session = self._session()
        try:
            query = session.query(IntegrationToken)
            if owner_scope is not None:
                query = query.filter(IntegrationToken.created_by_admin_id == owner_scope)
            query = query.order_by(IntegrationToken.created_at.desc(), IntegrationToken.id.desc())
            total = query.count()
            page_rows = query.limit(limit).offset(offset).all()
            servers_by_token, blueprints_by_token = self._allowlists_of(
                session, [row.id for row in page_rows]
            )
            items = [
                self._serialize(row, actor, servers_by_token[row.id], blueprints_by_token[row.id])
                for row in page_rows
            ]
            return items, total
        finally:
            session.close()

    def create_token(self, data: dict, *, actor: Actor) -> dict:
        """
        Issues a token for ``actor`` and returns the bearer ONCE.

        The order of the checks is deliberate: switch, vocabulary (422), ceiling (403), the explicit
        step-up of the destructive tier, allowlist shape, lifetime, and only then the database.
        Nothing is written unless every rule passed.
        """
        if not integration_api_enabled():
            raise _api_disabled()

        requested_scopes = _parse_requested_scopes(data["scopes"])
        _require_scopes_held_by_issuer(actor, requested_scopes)
        if _has_destructive_scope(requested_scopes):
            _require_issuer_step_up_for_destructive_scopes(actor)

        server_ids = _deduplicated(data.get("server_ids") or [])
        if not server_ids:
            raise _server_allowlist_required()
        blueprint_ids = _deduplicated(data.get("blueprint_ids") or [])
        if _has_destructive_scope(requested_scopes) and not blueprint_ids:
            raise _blueprint_allowlist_required()

        max_ttl_days = _max_ttl_days_for(requested_scopes)
        never_expires = bool(data.get("never_expires"))
        if never_expires:
            if not INTEGRATION_ALLOW_NON_EXPIRING_TOKENS:
                raise _non_expiring_not_allowed()
            # A destructive scope keeps its short cap even when non-expiring tokens are enabled.
            if _has_destructive_scope(requested_scopes):
                raise _ttl_too_long(INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS)
            token_expires_at = None
            audit_ttl_label = "never"
        else:
            requested_ttl_days = data.get("expires_in_days") or max_ttl_days
            if requested_ttl_days > max_ttl_days:
                raise _ttl_too_long(max_ttl_days)
            token_expires_at = _utcnow() + timedelta(days=requested_ttl_days)
            audit_ttl_label = f"{requested_ttl_days}d"

        public_id, secret, bearer = mint_integration_token()
        issuer_id, _ = identity_of(actor)
        scope_values = [scope.value for scope in requested_scopes]
        session = self._session()
        try:
            self._require_servers_exist(session, server_ids)
            self._require_blueprints_exist(session, blueprint_ids)
            row = IntegrationToken(
                token_id=public_id,
                secret_hmac=integration_token_hmac(secret),
                name=data["name"],
                scopes=STORED_SCOPES_SEPARATOR.join(scope_values),
                created_by_admin_id=issuer_id,
                expires_at=token_expires_at,
                note=data.get("note"),
            )
            session.add(row)
            try:
                session.flush()
                self._replace_server_allowlist(session, row.id, server_ids)
                self._replace_blueprint_allowlist(session, row.id, blueprint_ids)
                session.commit()
            except IntegrityError:
                self._translate_allowlist_race(session, server_ids, blueprint_ids)
                raise
            session.refresh(row)
            output = self._serialize_one(session, row, actor)
        finally:
            session.close()

        audit.record(
            "integration_token.create",
            admin=actor,
            target_type=AUDIT_TARGET_TYPE,
            target_id=output["id"],
            touched_engine=False,
            detail=(
                f"token={public_id} name='{data['name']}' scopes=[{','.join(scope_values)}] "
                f"servers=[{','.join(map(str, server_ids))}] "
                f"blueprints=[{','.join(map(str, blueprint_ids))}] ttl={audit_ttl_label} "
                f"issuer={issuer_id} mode={AUDIT_MODE_OWN}"
            ),
        )
        # The bearer travels ONLY here: ``_serialize`` does not know it, so no later listing can
        # return it by accident.
        return {**output, "token": bearer}

    def update_token(self, token_pk: int, data: dict, *, actor: Actor) -> dict:
        """
        Edits name, note, scopes and allowlists of the ACTOR'S OWN token (see the module docstring
        for why ``access.admin`` cannot edit a foreign one). A provided field replaces the stored
        one; the secret and the expiry never change.

        ``scopes`` is the COMPLETE list. Every scope in it must be held by the issuer today, which
        is what makes a suspended scope not re-addable while omitting it removes it.

        D9: adding a write scope to a token that outlives the write cap (or a destructive one to a
        token that outlives the destructive cap) is refused and the token is left untouched. The
        expiry is never clamped: shortening it silently would break a pipeline without warning.

        D17: adding a destructive scope asks the issuer for a fresh step-up again, even when the
        token already held ``migrations.apply_forward``; and the token that results from the edit
        must keep a non-empty blueprint allowlist while it holds a destructive scope, whichever
        field of the edit is the one that would break that.
        """
        if not integration_api_enabled():
            raise _api_disabled()

        actor_id, _ = identity_of(actor)
        session = self._session()
        try:
            row = session.get(IntegrationToken, token_pk)
            if row is None or row.created_by_admin_id != actor_id:
                raise _token_not_found()
            if row.revoked_at is not None:
                raise _already_revoked()

            scopes_before = parse_stored_integration_scopes(row.scopes)
            scope_values_after = scopes_before
            added_destructive_values: list[str] = []
            if data.get("scopes") is not None:
                requested_scopes = _parse_requested_scopes(data["scopes"])
                _require_scopes_held_by_issuer(actor, requested_scopes)
                scope_values_after = [scope.value for scope in requested_scopes]
                added_scopes = [
                    scope for scope in requested_scopes if scope.value not in scopes_before
                ]
                added_destructive_scopes = [
                    scope for scope in added_scopes if scope in DESTRUCTIVE_TIER_SCOPES
                ]
                added_destructive_values = [scope.value for scope in added_destructive_scopes]
                if added_destructive_scopes:
                    _require_issuer_step_up_for_destructive_scopes(actor)
                    cap_days = INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS
                elif _has_write_scope(added_scopes):
                    cap_days = INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS
                else:
                    cap_days = None
                if cap_days is not None:
                    if row.expires_at is None:
                        # A non-expiring token may gain write scopes (when the deployment allows
                        # them), but never a destructive one: that tier always has a short TTL.
                        if added_destructive_scopes:
                            raise _ttl_too_long(cap_days)
                    elif row.expires_at > _utcnow() + timedelta(days=cap_days):
                        raise _ttl_too_long(cap_days)

            new_server_ids: list[int] | None = None
            if data.get("server_ids") is not None:
                new_server_ids = _deduplicated(data["server_ids"])
                if not new_server_ids:
                    raise _server_allowlist_required()
                self._require_servers_exist(session, new_server_ids)

            new_blueprint_ids: list[int] | None = None
            if data.get("blueprint_ids") is not None:
                new_blueprint_ids = _deduplicated(data["blueprint_ids"])
                self._require_blueprints_exist(session, new_blueprint_ids)

            if _holds_destructive_scope_value(scope_values_after):
                blueprint_ids_after = (
                    new_blueprint_ids
                    if new_blueprint_ids is not None
                    else self._stored_blueprint_ids(session, row.id)
                )
                if not blueprint_ids_after:
                    raise _blueprint_allowlist_required()

            if data.get("name") is not None:
                row.name = data["name"]
            if data.get("note") is not None:
                row.note = data["note"]
            if data.get("scopes") is not None:
                row.scopes = STORED_SCOPES_SEPARATOR.join(scope_values_after)
            if new_server_ids is not None:
                self._replace_server_allowlist(session, row.id, new_server_ids)
            if new_blueprint_ids is not None:
                self._replace_blueprint_allowlist(session, row.id, new_blueprint_ids)
            try:
                session.commit()
            except IntegrityError:
                self._translate_allowlist_race(
                    session, new_server_ids or [], new_blueprint_ids or []
                )
                raise
            session.refresh(row)
            output = self._serialize_one(session, row, actor)
        finally:
            session.close()

        audit.record(
            "integration_token.update",
            admin=actor,
            target_type=AUDIT_TARGET_TYPE,
            target_id=token_pk,
            touched_engine=False,
            detail=(
                f"token={output['token_id']} name='{output['name']}' "
                f"scopes=[{','.join(scopes_before)}]->[{','.join(scope_values_after)}] "
                f"added_destructive=[{','.join(added_destructive_values)}] "
                f"servers=[{','.join(map(str, output['server_ids']))}] "
                f"blueprints=[{','.join(map(str, output['blueprint_ids']))}] "
                f"issuer={actor_id} mode={AUDIT_MODE_OWN}"
            ),
        )
        return output

    def revoke_token(self, token_pk: int, *, actor: Actor, owner_scope: int | None) -> dict:
        """
        Revokes. There is no reactivation: ``revoked_at`` is not undone (a revoke/reactivate cycle
        would leave a token somebody believed dead, which is worse than issuing a new one).

        A foreign token answers the same 404 as a missing one BEFORE any state check, because the
        409 of "already revoked" would confirm that it exists.
        """
        actor_id, _ = identity_of(actor)
        session = self._session()
        try:
            row = session.get(IntegrationToken, token_pk)
            token_is_visible = row is not None and (
                owner_scope is None or row.created_by_admin_id == owner_scope
            )
            if not token_is_visible:
                raise _token_not_found()
            if row.revoked_at is not None:
                raise _already_revoked()
            row.revoked_at = _utcnow()
            row.revoked_by_admin_id = actor_id
            token_owner_id = row.created_by_admin_id
            session.commit()
            session.refresh(row)
            output = self._serialize_one(session, row, actor)
        finally:
            session.close()

        audit.record(
            "integration_token.revoke",
            admin=actor,
            target_type=AUDIT_TARGET_TYPE,
            target_id=token_pk,
            touched_engine=False,
            detail=(
                f"token={output['token_id']} name='{output['name']}' "
                f"issuer={token_owner_id} mode={_audit_mode(owner_scope)}"
            ),
        )
        return output
