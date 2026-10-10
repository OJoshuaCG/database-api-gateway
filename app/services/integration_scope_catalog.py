"""
Catálogo de scopes de la API de integración: qué puede hacer un token de integración.

POR QUÉ ES UN CATÁLOGO APARTE Y NO ``Capability``
--------------------------------------------------
El techo de agente del MCP (``AGENT_ALLOWED``) excluye por invariante todo lo que mute (inv. 5) o
exija step-up (inv. 11). Un token de integración existe justamente para MUTAR (crear una base,
un usuario del motor, aplicar una migración), así que no puede vivir en ese vocabulario ni se
puede relajar ese techo sin dejar de proteger a los agentes. Por eso el scope de integración es
un vocabulario PROPIO y CERRADO que se MAPEA a una ``Capability`` existente: el scope decide qué
operación del catálogo de integración se autoriza, y la capacidad mapeada es lo que la capa 1 y la
capa 2 evalúan contra el rol real del emisor. No se agrega ninguna capacidad nueva por scope y
ningún scope aparece en ``/authz/catalog``.

LOS INVARIANTES SE AFIRMAN AL IMPORTAR
--------------------------------------
``_assert_integration_invariants`` corre al final del módulo, igual que el catálogo de capacidades:
un scope mal mapeado rompe el arranque y no el primer request. Cada invariante tiene un test de
mutación que lo hace fallar (``tests/test_integration_scope_catalog.py``).

Son 12 scopes en tres tiers: ``read`` (no muta), ``write`` (muta de forma recuperable) y
``destructive`` (``migrations.rollback`` y ``migrations.stamp``: pueden perder datos o declarar un
estado que el gateway no produjo). El tier destructivo no agrega ninguna ``Capability``: ambos scopes
mapean a ``blueprints.apply``, la misma que ya usan las rutas humanas de rollback y stamp, y lo que
los distingue de ``migrations.apply_forward`` es el tier (marcador que gobiernan I10 e I11).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

import app.services.capability_catalog as capability_catalog
from app.services.capability_catalog import Capability, GatewayRole

# --------------------------------------------------------------------------- #
# Vocabulario                                                                  #
# --------------------------------------------------------------------------- #


class IntegrationScope(StrEnum):
    """Vocabulario cerrado de scopes. El valor viaja en la fila y en la API: es estable."""

    SERVERS_LIST = "servers.list"
    DATABASES_LIST = "databases.list"
    BLUEPRINT_READ_ASSIGNED = "blueprint.read_assigned"
    MIGRATIONS_READ_VERSION = "migrations.read_version"
    DATABASES_CREATE = "databases.create"
    ENGINE_USERS_CREATE = "engine_users.create"
    ENGINE_USERS_ASSIGN_PROFILE = "engine_users.assign_profile"
    ENGINE_USERS_ASSIGN_DATABASE = "engine_users.assign_database"
    DATABASES_ASSIGN_BLUEPRINT = "databases.assign_blueprint"
    MIGRATIONS_APPLY_FORWARD = "migrations.apply_forward"
    MIGRATIONS_ROLLBACK = "migrations.rollback"
    MIGRATIONS_STAMP = "migrations.stamp"


#: ``read`` no muta, ``write`` muta de forma recuperable, ``destructive`` es lo irreversible.
IntegrationScopeTier = Literal["read", "write", "destructive"]

TIER_READ: IntegrationScopeTier = "read"
TIER_WRITE: IntegrationScopeTier = "write"
TIER_DESTRUCTIVE: IntegrationScopeTier = "destructive"


@dataclass(frozen=True, slots=True)
class IntegrationScopeSpec:
    """
    Metadatos de un scope. ``label`` va en español porque lo consume la SPA.

    ``mutates`` se declara en el scope Y se deriva de la capacidad mapeada; el invariante I8 exige
    que coincidan, para que "este scope escribe" no dependa de una sola fuente que alguien pueda
    editar sin notarlo.
    """

    scope: IntegrationScope
    capability: Capability
    tier: IntegrationScopeTier
    label: str
    mutates: bool


INTEGRATION_SCOPE_SPECS: tuple[IntegrationScopeSpec, ...] = (
    IntegrationScopeSpec(
        scope=IntegrationScope.SERVERS_LIST,
        capability=Capability.SERVERS_READ,
        tier=TIER_READ,
        label="Listar servidores permitidos",
        mutates=False,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.DATABASES_LIST,
        capability=Capability.DATABASES_READ,
        tier=TIER_READ,
        label="Listar bases de datos de un servidor permitido",
        mutates=False,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.BLUEPRINT_READ_ASSIGNED,
        capability=Capability.BLUEPRINTS_READ,
        tier=TIER_READ,
        label="Leer el blueprint asignado a una base",
        mutates=False,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.MIGRATIONS_READ_VERSION,
        capability=Capability.BLUEPRINTS_READ,
        tier=TIER_READ,
        label="Leer la versión de migraciones de una base",
        mutates=False,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.DATABASES_CREATE,
        capability=Capability.DATABASES_WRITE,
        tier=TIER_WRITE,
        label="Crear una base de datos",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.ENGINE_USERS_CREATE,
        capability=Capability.ENGINE_USERS_WRITE,
        tier=TIER_WRITE,
        label="Crear un usuario del motor",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.ENGINE_USERS_ASSIGN_PROFILE,
        capability=Capability.ENGINE_USERS_WRITE,
        tier=TIER_WRITE,
        label="Asignar un perfil de permisos a un usuario del motor",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.ENGINE_USERS_ASSIGN_DATABASE,
        capability=Capability.ENGINE_USERS_WRITE,
        tier=TIER_WRITE,
        label="Asignar una base a un usuario del motor",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.DATABASES_ASSIGN_BLUEPRINT,
        capability=Capability.DATABASES_WRITE,
        tier=TIER_WRITE,
        label="Asignar un blueprint a una base",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.MIGRATIONS_APPLY_FORWARD,
        capability=Capability.BLUEPRINTS_APPLY,
        tier=TIER_WRITE,
        label="Aplicar migraciones hacia adelante",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.MIGRATIONS_ROLLBACK,
        capability=Capability.BLUEPRINTS_APPLY,
        tier=TIER_DESTRUCTIVE,
        label="Revertir migraciones (puede borrar datos de forma irreversible)",
        mutates=True,
    ),
    IntegrationScopeSpec(
        scope=IntegrationScope.MIGRATIONS_STAMP,
        capability=Capability.BLUEPRINTS_APPLY,
        tier=TIER_DESTRUCTIVE,
        label="Marcar una versión de migración sin ejecutar SQL",
        mutates=True,
    ),
)

_SPEC_BY_SCOPE: Mapping[IntegrationScope, IntegrationScopeSpec] = MappingProxyType(
    {scope_spec.scope: scope_spec for scope_spec in INTEGRATION_SCOPE_SPECS}
)

#: scope → capacidad mapeada. Es la ÚNICA fuente de "qué capacidad evalúa este scope": la
#: autenticación, la ruta y el script de cobertura la leen de acá.
INTEGRATION_ALLOWED: Mapping[IntegrationScope, Capability] = MappingProxyType(
    {scope_spec.scope: scope_spec.capability for scope_spec in INTEGRATION_SCOPE_SPECS}
)


def integration_scope_spec(scope: IntegrationScope) -> IntegrationScopeSpec:
    return _SPEC_BY_SCOPE[scope]


#: Separator of the ``integration_tokens.scopes`` column.
STORED_SCOPES_SEPARATOR = ","


def parse_stored_integration_scopes(raw_scopes: str | None) -> list[str]:
    """
    The scope strings persisted on a token row, trimmed, de-duplicated and sorted.

    Unknown values are KEPT on purpose: a token written under a wider vocabulary (a rolled-back
    release, a manipulated row) must still report them as suspended instead of silently losing
    them, and the next edit then has the chance to remove them. They never become effective:
    ``effective_integration_scopes`` only returns members of ``IntegrationScope``.
    """
    if not raw_scopes:
        return []
    stripped_values = (value.strip() for value in raw_scopes.split(STORED_SCOPES_SEPARATOR))
    return sorted({value for value in stripped_values if value})


def effective_integration_scopes(
    stored_scopes: Sequence[str], issuer_capabilities: frozenset[Capability]
) -> frozenset[IntegrationScope]:
    """
    What a token can do RIGHT NOW: stored ∩ closed vocabulary ∩ what its issuer holds today.

    Evaluated on every request (the issuer is re-read each time), so demoting the issuer
    suspends the scope on the next call and promoting it back reactivates the scope without
    re-issuing the token. The difference ``stored - effective`` is what the API reports as
    ``suspended_scopes``.
    """
    effective: set[IntegrationScope] = set()
    for stored_value in stored_scopes:
        try:
            scope = IntegrationScope(stored_value)
        except ValueError:
            continue
        if INTEGRATION_ALLOWED[scope] in issuer_capabilities:
            effective.add(scope)
    return frozenset(effective)


# --------------------------------------------------------------------------- #
# Códigos de error (``public_context["code"]``) y su estado HTTP               #
# --------------------------------------------------------------------------- #
# Vocabulario cerrado. Ningún mensaje lleva texto del motor ni detalle de la fila.

CODE_INTEGRATION_DISABLED = "integration.disabled"
CODE_INTEGRATION_TOKEN_INVALID = "integration.token_invalid"
CODE_INTEGRATION_SCOPE_MISSING = "integration.scope_missing"
CODE_INTEGRATION_SERVER_NOT_ALLOWED = "integration.server_not_allowed"
CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED = "integration.blueprint_not_allowed"
CODE_INTEGRATION_PROFILE_REQUIRES_GRANT_ADMIN = "integration.profile_requires_grant_admin"
CODE_INTEGRATION_BLUEPRINT_ALREADY_ASSIGNED = "integration.blueprint_already_assigned"
CODE_INTEGRATION_SERVER_MISMATCH = "integration.server_mismatch"
CODE_INTEGRATION_BLUEPRINT_NOT_ASSIGNED = "integration.blueprint_not_assigned"
CODE_INTEGRATION_MIGRATION_TARGET_NOT_FORWARD = "integration.migration_target_not_forward"
CODE_INTEGRATION_ENVIRONMENT_BLOCKS_DESTRUCTIVE = "integration.environment_blocks_destructive"
CODE_INTEGRATION_ENVIRONMENT_UNCLASSIFIED = "integration.environment_unclassified"
CODE_INTEGRATION_DATABASE_QUARANTINED = "integration.database_quarantined"
CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION = "integration.rollback_unapplied_version"
CODE_INTEGRATION_STAMP_VERSION_CONFLICT = "integration.stamp_version_conflict"
CODE_INTEGRATION_STAMP_ORPHAN_ACCOUNTING = "integration.stamp_orphan_accounting"
CODE_INTEGRATION_TOKEN_NOT_FOUND = "integration_token.not_found"
CODE_INTEGRATION_TOKEN_TTL_TOO_LONG = "integration_token.ttl_too_long"
CODE_INTEGRATION_TOKEN_NON_EXPIRING_NOT_ALLOWED = "integration_token.non_expiring_not_allowed"
CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED = "integration_token.scope_not_allowed"
CODE_INTEGRATION_TOKEN_UNKNOWN_SCOPE = "integration_token.unknown_scope"
CODE_INTEGRATION_TOKEN_SERVER_ALLOWLIST_REQUIRED = "integration_token.server_allowlist_required"
CODE_INTEGRATION_TOKEN_ALREADY_REVOKED = "integration_token.already_revoked"
CODE_INTEGRATION_TOKEN_SERVER_NOT_FOUND = "integration_token.server_not_found"
CODE_INTEGRATION_TOKEN_BLUEPRINT_NOT_FOUND = "integration_token.blueprint_not_found"
CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED = (
    "integration_token.blueprint_allowlist_required"
)

INTEGRATION_ERROR_STATUS: Mapping[str, int] = MappingProxyType(
    {
        CODE_INTEGRATION_DISABLED: 503,
        CODE_INTEGRATION_TOKEN_INVALID: 401,
        CODE_INTEGRATION_SCOPE_MISSING: 403,
        CODE_INTEGRATION_SERVER_NOT_ALLOWED: 403,
        CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED: 403,
        CODE_INTEGRATION_PROFILE_REQUIRES_GRANT_ADMIN: 403,
        CODE_INTEGRATION_BLUEPRINT_ALREADY_ASSIGNED: 409,
        CODE_INTEGRATION_SERVER_MISMATCH: 409,
        CODE_INTEGRATION_BLUEPRINT_NOT_ASSIGNED: 404,
        CODE_INTEGRATION_MIGRATION_TARGET_NOT_FORWARD: 422,
        CODE_INTEGRATION_ENVIRONMENT_BLOCKS_DESTRUCTIVE: 409,
        CODE_INTEGRATION_ENVIRONMENT_UNCLASSIFIED: 409,
        CODE_INTEGRATION_DATABASE_QUARANTINED: 409,
        CODE_INTEGRATION_ROLLBACK_UNAPPLIED_VERSION: 409,
        CODE_INTEGRATION_STAMP_VERSION_CONFLICT: 409,
        CODE_INTEGRATION_STAMP_ORPHAN_ACCOUNTING: 409,
        CODE_INTEGRATION_TOKEN_NOT_FOUND: 404,
        CODE_INTEGRATION_TOKEN_TTL_TOO_LONG: 422,
        CODE_INTEGRATION_TOKEN_NON_EXPIRING_NOT_ALLOWED: 422,
        CODE_INTEGRATION_TOKEN_SCOPE_NOT_ALLOWED: 403,
        CODE_INTEGRATION_TOKEN_UNKNOWN_SCOPE: 422,
        CODE_INTEGRATION_TOKEN_SERVER_ALLOWLIST_REQUIRED: 422,
        CODE_INTEGRATION_TOKEN_ALREADY_REVOKED: 409,
        CODE_INTEGRATION_TOKEN_SERVER_NOT_FOUND: 422,
        CODE_INTEGRATION_TOKEN_BLUEPRINT_NOT_FOUND: 422,
        CODE_INTEGRATION_TOKEN_BLUEPRINT_ALLOWLIST_REQUIRED: 422,
    }
)


# --------------------------------------------------------------------------- #
# Invariantes                                                                  #
# --------------------------------------------------------------------------- #

#: I9: el conjunto de scopes es un literal FIJO. Agregar un miembro al enum sin tocar esto rompe
#: el import, así que ampliar el vocabulario es una decisión explícita y revisada, nunca un efecto
#: lateral de agregar una línea al enum.
EXPECTED_SCOPE_VALUES: frozenset[str] = frozenset(
    {
        "servers.list",
        "databases.list",
        "blueprint.read_assigned",
        "migrations.read_version",
        "databases.create",
        "engine_users.create",
        "engine_users.assign_profile",
        "engine_users.assign_database",
        "databases.assign_blueprint",
        "migrations.apply_forward",
        "migrations.rollback",
        "migrations.stamp",
    }
)

#: I5: niveles que un scope de integración nunca puede alcanzar por mapeo, sea cual sea el módulo.
_FORBIDDEN_CAPABILITY_LEVELS: frozenset[str] = frozenset(
    {"drop", "credentials", "grant_admin", "secrets"}
)
#: I5: módulo cuyo contenido es DATO de negocio del tercero.
_FORBIDDEN_CAPABILITY_MODULE = "data"
#: I6: lo que gobierna credenciales y accesos no puede delegarse a una máquina.
_FORBIDDEN_CAPABILITY_MODULES_FOR_I6: frozenset[str] = frozenset({"access"})
_FORBIDDEN_CAPABILITIES_FOR_I6: frozenset[Capability] = frozenset(
    {Capability.TOKENS_OWN, Capability.INTEGRATION_TOKENS_OWN}
)
#: I7: los únicos scopes que pueden mapear a una capacidad ``destructive``.
_DESTRUCTIVE_CAPABILITY_ALLOWED_SCOPES: frozenset[IntegrationScope] = frozenset(
    {
        IntegrationScope.MIGRATIONS_APPLY_FORWARD,
        IntegrationScope.MIGRATIONS_ROLLBACK,
        IntegrationScope.MIGRATIONS_STAMP,
    }
)
#: I10: los scopes del tier ``destructive``. ``migrations.apply_forward`` queda en ``write`` a
#: propósito: su protección (guard de entorno, solo hacia adelante) vive en ``apply``; rollback y
#: stamp no la tienen en el controlador y se la pone el adaptador de integración.
DESTRUCTIVE_TIER_SCOPES: frozenset[IntegrationScope] = frozenset(
    {IntegrationScope.MIGRATIONS_ROLLBACK, IntegrationScope.MIGRATIONS_STAMP}
)


def _assert_integration_invariants(
    scope_specs: Sequence[IntegrationScopeSpec] | None = None,
) -> None:
    """
    Afirma I1-I12 sobre ``scope_specs`` (por defecto, el catálogo real).

    Recibe los specs por parámetro y lee el catálogo de capacidades por módulo en cada llamada
    para que los tests de mutación puedan romper cada invariante sin editar código de producción.
    """
    specs: Sequence[IntegrationScopeSpec] = (
        INTEGRATION_SCOPE_SPECS if scope_specs is None else scope_specs
    )
    owner_capabilities = capability_catalog.ROLE_CAPABILITIES[GatewayRole.OWNER]
    global_capabilities = set().union(*capability_catalog.GLOBAL_CAPABILITIES.values())

    # I1. Exactamente un spec por scope del enum, y ninguno de más.
    scopes_seen = [scope_spec.scope for scope_spec in specs]
    if len(scopes_seen) != len(set(scopes_seen)):
        raise AssertionError("I1: hay scopes de integración con más de un spec.")
    if set(scopes_seen) != set(IntegrationScope):
        missing = sorted(s.value for s in set(IntegrationScope) - set(scopes_seen))
        raise AssertionError(f"I1: scopes de integración sin spec (o de más): {missing}")

    for scope_spec in specs:
        scope_value = scope_spec.scope.value
        capability = scope_spec.capability
        capability_spec = capability_catalog.spec(capability)

        # I2. La capacidad mapeada existe en la cadena de roles hasta `owner`: si ningún rol
        #     humano la puede tener, el scope no sería ejercible por nadie.
        if capability not in owner_capabilities:
            raise AssertionError(f"I2: {scope_value} mapea a {capability.value}, que no es de owner.")

        # I3. Una global (access_admin, security_officer) no sigue la capa 2 por destino: mapear
        #     un scope a una capacidad global la haría valer en todos los alcances.
        if capability in global_capabilities:
            raise AssertionError(f"I3: {scope_value} mapea a {capability.value}, que es de una global.")

        # I4. Un token de integración nunca DIVULGA datos del tercero.
        if capability_spec.discloses:
            raise AssertionError(f"I4: {scope_value} mapea a {capability.value}, que divulga.")

        # I5. Ni datos de negocio ni nada que borre, entregue credenciales o delegue privilegios.
        if capability_spec.module == _FORBIDDEN_CAPABILITY_MODULE:
            raise AssertionError(f"I5: {scope_value} mapea al módulo de datos ({capability.value}).")
        if capability_spec.level in _FORBIDDEN_CAPABILITY_LEVELS:
            raise AssertionError(
                f"I5: {scope_value} mapea a {capability.value} de nivel '{capability_spec.level}'."
            )

        # I6. Ni la emisión de tokens ni la administración de accesos son delegables a una máquina.
        if (
            capability in _FORBIDDEN_CAPABILITIES_FOR_I6
            or capability_spec.module in _FORBIDDEN_CAPABILITY_MODULES_FOR_I6
        ):
            raise AssertionError(f"I6: {scope_value} mapea a {capability.value}, no delegable.")

        # I7. Solo un scope de la lista cerrada puede mapear a una capacidad destructiva.
        if (
            capability_spec.destructive
            and scope_spec.scope not in _DESTRUCTIVE_CAPABILITY_ALLOWED_SCOPES
        ):
            raise AssertionError(f"I7: {scope_value} mapea a {capability.value}, que es destructive.")

        # I8. "Este scope escribe" coincide con "su capacidad escribe".
        if scope_spec.mutates != capability_spec.mutates:
            raise AssertionError(
                f"I8: {scope_value} declara mutates={scope_spec.mutates} y su capacidad "
                f"{capability.value} tiene mutates={capability_spec.mutates}."
            )

        # I10. El tier `destructive` es exactamente el literal {rollback, stamp}: ni un scope más
        #      lo adquiere por accidente, ni uno de los dos lo pierde en silencio.
        is_destructive_tier = scope_spec.tier == TIER_DESTRUCTIVE
        if is_destructive_tier != (scope_spec.scope in DESTRUCTIVE_TIER_SCOPES):
            raise AssertionError(
                f"I10: {scope_value} tiene tier '{scope_spec.tier}' y el tier destructivo es "
                "exactamente rollback y stamp."
            )

        # I11. Un scope destructivo muta, y su capacidad exige step-up y es destructive: si la
        #      capacidad mapeada dejara de pedir step-up, quien emite el token dejaría de
        #      confirmar su contraseña justo para el scope más peligroso.
        if is_destructive_tier and not (
            scope_spec.mutates and capability_spec.requires_step_up and capability_spec.destructive
        ):
            raise AssertionError(
                f"I11: {scope_value} es destructivo y su capacidad {capability.value} no cumple "
                "mutates + step-up + destructive."
            )

        # I12. El tier `read` es exactamente lo que no muta.
        if (scope_spec.tier == TIER_READ) != (not scope_spec.mutates):
            raise AssertionError(
                f"I12: {scope_value} tiene tier '{scope_spec.tier}' y mutates={scope_spec.mutates}."
            )

    # I9. El vocabulario es el literal fijo.
    actual_values = {scope.value for scope in IntegrationScope}
    if actual_values != EXPECTED_SCOPE_VALUES:
        raise AssertionError(
            "I9: el conjunto de scopes de integración no es el literal fijo "
            f"(diferencia: {sorted(actual_values ^ EXPECTED_SCOPE_VALUES)})."
        )


_assert_integration_invariants()
