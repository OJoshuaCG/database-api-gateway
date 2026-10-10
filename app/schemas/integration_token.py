"""Schemas of the integration tokens (REST bearer credentials issued per user)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Same bounds as ``IntegrationToken.name`` (String(128)); the minimum keeps names meaningful.
TOKEN_NAME_MIN_LENGTH = 3
TOKEN_NAME_MAX_LENGTH = 128


class IntegrationTokenCreate(BaseModel):
    """
    Issue a token. There is NO secret field: the server generates it.

    ``name`` is mandatory and says which project or pipeline uses the token. Granular revocation
    depends on one token per consumer: a token shared by six pipelines is a token nobody revokes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=TOKEN_NAME_MIN_LENGTH,
        max_length=TOKEN_NAME_MAX_LENGTH,
        description="Project or pipeline that will use the token",
    )
    scopes: list[str] = Field(
        ...,
        min_length=1,
        description="Integration scopes. Validated against the closed vocabulary and the issuer ceiling",
    )
    server_ids: list[int] = Field(
        default_factory=list,
        description="Allowlist of servers the token may operate on. Required for operation scopes",
    )
    blueprint_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Allowlist of blueprints. Empty means no blueprint restriction, EXCEPT for a token "
            "with a destructive scope (migrations.rollback, migrations.stamp), which needs at "
            "least one"
        ),
    )
    expires_in_days: int | None = Field(
        None,
        ge=1,
        description=(
            "Defaults to, and is capped by, the tier TTL (90 days read-only, 30 days with any "
            "write scope, 7 days with any destructive scope)"
        ),
    )
    never_expires: bool = Field(
        False,
        description=(
            "Issue a token without expiration. Only accepted when the deployment enables it "
            "(INTEGRATION_ALLOW_NON_EXPIRING_TOKENS) and never with a destructive scope. "
            "Mutually exclusive with expires_in_days"
        ),
    )
    note: str | None = None

    @model_validator(mode="after")
    def _expiry_options_are_exclusive(self) -> "IntegrationTokenCreate":
        if self.never_expires and self.expires_in_days is not None:
            raise ValueError("never_expires and expires_in_days are mutually exclusive")
        return self


class IntegrationTokenUpdate(BaseModel):
    """
    Edit an existing token. Every field is optional and a provided field REPLACES the stored one.

    The secret, the TTL and the issuer never change: scopes live in the row and not inside the
    bearer, so widening a token does not require re-distributing the secret. ``extra="forbid"``
    prevents a misspelled field from being silently ignored while the operator believes it changed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        None, min_length=TOKEN_NAME_MIN_LENGTH, max_length=TOKEN_NAME_MAX_LENGTH
    )
    scopes: list[str] | None = Field(
        None,
        min_length=1,
        description="COMPLETE list of scopes. An empty token is useless: revoke it instead",
    )
    server_ids: list[int] | None = None
    blueprint_ids: list[int] | None = None
    note: str | None = None


class IntegrationTokenOut(BaseModel):
    """
    A token. It NEVER carries the secret or its HMAC.

    ``token_id`` is public and is what the audit trail shows, so a row can be crossed with the
    token that produced it. ``suspended_scopes`` is only computed for the owner's own tokens: it
    lists stored scopes the issuer does not hold today (or that are out of vocabulary), and it
    never reveals a scope the token never had.
    """

    id: int
    token_id: str
    name: str
    scopes: list[str]
    suspended_scopes: list[str]
    server_ids: list[int]
    blueprint_ids: list[int]
    created_by_admin_id: int
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    note: str | None = None
    active: bool
    created_at: datetime | None = None


class IntegrationTokenCreatedOut(IntegrationTokenOut):
    """
    The issuance response, with the full bearer.

    It is shown ONCE and is not stored (only its HMAC is). If it is lost, issue another token:
    a system able to show it again is a system that holds it.
    """

    token: str = Field(
        ...,
        description="Full bearer, format 'datumint.<id>.<secret>'. Shown once. Keep it in a secret store",
    )


class IntegrationScopeCeilingEntry(BaseModel):
    """One scope the issuer may grant today."""

    scope: str
    label: str
    mutates: bool
    tier: str


class IntegrationCeilingOut(BaseModel):
    """
    What the caller may put on a new token, and the lifetime rules.

    Scopes the issuer does not hold are ABSENT: no entry, flag or count. A picker that disabled
    them would tell the user which capabilities exist beyond their role.
    """

    enabled: bool
    scopes: list[IntegrationScopeCeilingEntry]
    max_ttl_days: int
    max_write_ttl_days: int
    max_destructive_ttl_days: int
    allow_non_expiring: bool
