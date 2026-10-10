"""
Schemas of the integration API operations (``/integration/...``).

Every request schema forbids unknown fields (``extra="forbid"``): the integration API is a closed
contract, so a field the endpoint does not honour (``force``, ``model_id`` on database creation, a
client-chosen ``password``) is a 422 and never silently ignored. Output schemas are minimal
projections: no host, credential or ciphertext of the underlying inventory rows is exposed.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.grant import LevelObjectMapping
from app.schemas.managed_database import _CHARSET, _COLLATION, _DBNAME
from app.schemas.server_user import _HOST, _USERNAME
from app.services.db_admin.dtos import GrantLevel

#: Same shape as the ``?version=`` of the human apply route (digits only, 4 to 10 of them).
MIGRATION_VERSION_PATTERN = r"^\d{4,10}$"


class _ClosedRequest(BaseModel):
    """Base of every integration request body: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Read operations                                                              #
# --------------------------------------------------------------------------- #


class IntegrationServerOut(BaseModel):
    """A server of the token allowlist. No host, port or credential flags."""

    id: int
    name: str
    engine: str


class IntegrationDatabaseOut(BaseModel):
    """A managed database. ``model_id`` is the assigned blueprint, if any."""

    id: int
    name: str
    server_id: int
    model_id: int | None = None
    model_version: str | None = None
    environment_id: int | None = None
    status: str
    charset: str | None = None
    collation: str | None = None


class IntegrationBlueprintOut(BaseModel):
    """The blueprint assigned to a database, and the version the inventory has recorded."""

    model_id: int
    name: str
    slug: str
    model_version: str | None = Field(
        None, description="Version recorded in the inventory for this database."
    )


class IntegrationMigrationVersionOut(BaseModel):
    """Where a database stands against its blueprint, reduced to what an integration needs."""

    current_version: str | None = None
    latest_version: str | None = None
    pending: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Write operations                                                             #
# --------------------------------------------------------------------------- #


class IntegrationDatabaseCreate(_ClosedRequest):
    """
    Creates and provisions an EMPTY database. The blueprint is assigned and applied by their own
    scopes, so ``model_id``, ``model_version``, ``apply_migrations`` and ``target_version`` of the
    human endpoint are not part of this contract.
    """

    name: str = Field(..., pattern=_DBNAME)
    server_id: int = Field(..., ge=1)
    owner_id: int = Field(..., ge=1, description="Engine user of the same server that owns it")
    environment_id: int | None = Field(None, ge=1)
    charset: str | None = Field(None, pattern=_CHARSET)
    collation: str | None = Field(None, pattern=_COLLATION)
    notes: str | None = None


class IntegrationEngineUserCreate(_ClosedRequest):
    """
    Creates an engine user. There is NO ``password`` field: the gateway generates it, so a client
    can never choose (or leak through a request log) a weak or reused one.
    """

    server_id: int = Field(..., ge=1)
    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="MySQL/MariaDB only; ignored by PostgreSQL")
    notes: str | None = None


class IntegrationEngineUserCreatedOut(BaseModel):
    """
    The created engine user. ``password`` is returned ONLY in this response: it is stored
    encrypted and no endpoint of the integration API can read it back.
    """

    id: int
    server_id: int
    username: str
    host: str
    is_active: bool
    notes: str | None = None
    has_password: bool = False
    created_at: datetime
    updated_at: datetime
    password: str = Field(..., description="Generated secret. Shown once; not retrievable later.")


class IntegrationLevelObjectMapping(LevelObjectMapping):
    """
    A level-to-object mapping of an integration profile assignment. Engine-wide (``GLOBAL``)
    mappings are rejected and the object must name a database: an integration token only ever
    grants inside databases that the gateway manages.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("level")
    @classmethod
    def _reject_global_level(cls, level: GrantLevel) -> GrantLevel:
        if level == GrantLevel.GLOBAL:
            raise ValueError("El nivel GLOBAL no se puede asignar por la API de integración.")
        return level

    @field_validator("object_ref")
    @classmethod
    def _require_database_in_object(cls, object_ref):
        if not object_ref.database:
            raise ValueError("object_ref.database es obligatorio.")
        return object_ref


class IntegrationApplyProfileRequest(_ClosedRequest):
    """Maps every level of the profile to the database object it should be granted on."""

    object_mappings: list[IntegrationLevelObjectMapping] = Field(..., min_length=1)


class IntegrationAssignDatabaseRequest(_ClosedRequest):
    """Assigns a profile to an engine user on ONE database; the gateway builds the mapping."""

    profile_id: int = Field(..., ge=1)


class IntegrationAssignBlueprintRequest(_ClosedRequest):
    """Only the blueprint: environment, owner, notes and the rest stay out of this scope."""

    model_id: int = Field(..., ge=1)


class IntegrationApplyMigrationsRequest(_ClosedRequest):
    """
    Forward migration. ``force`` and ``on_failure`` are deliberately absent (the gateway fixes
    them), so sending either one is a 422.
    """

    version: str | None = Field(
        None,
        pattern=MIGRATION_VERSION_PATTERN,
        description="Target version (inclusive). Omitted: apply up to the latest.",
    )
    dry_run: bool = Field(False, description="Return the plan without applying anything.")


class IntegrationRollbackRequest(_ClosedRequest):
    """
    Rollback with compare-and-set on BOTH ends. There is no way to say "one step back" or "back to
    the base": the caller names the version it believes the database is on and the version it
    wants, so a stale retry or a concurrent run fails instead of reverting twice. ``force`` and
    ``dry_run`` do not exist (422): the endpoint behind this operation has neither.
    """

    from_version: str = Field(
        ...,
        pattern=MIGRATION_VERSION_PATTERN,
        description="Version the database is on right now. Must match, or the call is refused.",
    )
    to_version: str = Field(
        ...,
        pattern=MIGRATION_VERSION_PATTERN,
        description="Version to go back to (it stays applied). Must be older than from_version.",
    )


class IntegrationStampRequest(_ClosedRequest):
    """
    Declares that a database is on ``version`` WITHOUT running any SQL, guarded by compare-and-set.

    ``expected_current_version`` is required as a KEY and may be ``null`` (a database with no
    version yet): omitting it is a 422, because a stamp that does not say what it overwrites is a
    blind overwrite. ``force`` and ``purge`` do not exist (422).
    """

    expected_current_version: str | None = Field(
        ...,
        pattern=MIGRATION_VERSION_PATTERN,
        description="Version the database is on right now, or null if it has none. Must match.",
    )
    version: str = Field(
        ...,
        pattern=MIGRATION_VERSION_PATTERN,
        description="Version to declare. Must exist in the assigned blueprint.",
    )
