"""
El catálogo de scopes de integración (``app.services.integration_scope_catalog``) y la capacidad
``integration_tokens.own``.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Un scope de integración es la unidad de permiso de un bearer que MUTA bases de terceros, y su
mapeo a una ``Capability`` es lo que decide hasta dónde llega. El catálogo afirma sus invariantes
al IMPORTAR (``_assert_integration_invariants``); un invariante que ningún test rompe puede estar
apagado sin que nadie lo note, así que cada uno tiene acá un caso de MUTACIÓN que lo hace fallar.

Alcance: 12 scopes y los tres tiers ``read``/``write``/``destructive``. Los scopes
``migrations.rollback`` y ``migrations.stamp`` forman el tier destructivo (CR-1) y lo gobiernan los
invariantes I10 e I11.

Numeración: I1 un spec por scope; I2 la capacidad está en ``owner``; I3 no es de una global
(access_admin / security_officer); I4 no divulga; I5 ni módulo ``data`` ni nivel
drop/credentials/grant_admin/secrets; I6 no es de tokens ni de acceso; I7 ``destructive`` solo
para apply_forward, rollback y stamp; I8 ``mutates`` del scope == el de su capacidad; I9 el
conjunto de scopes es un literal fijo (12); I10 el tier ``destructive`` si y solo si el scope es
rollback o stamp; I11 el tier ``destructive`` exige que muten, pidan step-up y mapeen a una
capacidad ``destructive``; I12 el tier ``read`` si y solo si no muta.
"""

import dataclasses
from types import MappingProxyType

import pytest

import app.services.capability_catalog as capability_catalog
import app.services.integration_scope_catalog as scope_catalog
from app.services.capability_catalog import Capability, GatewayRole, GlobalCapability
from app.services.integration_scope_catalog import (
    INTEGRATION_ALLOWED,
    INTEGRATION_SCOPE_SPECS,
    IntegrationScope,
    integration_scope_spec,
)

EXPECTED_MAPPING: dict[str, tuple[Capability, str]] = {
    "servers.list": (Capability.SERVERS_READ, "read"),
    "databases.list": (Capability.DATABASES_READ, "read"),
    "blueprint.read_assigned": (Capability.BLUEPRINTS_READ, "read"),
    "migrations.read_version": (Capability.BLUEPRINTS_READ, "read"),
    "databases.create": (Capability.DATABASES_WRITE, "write"),
    "engine_users.create": (Capability.ENGINE_USERS_WRITE, "write"),
    "engine_users.assign_profile": (Capability.ENGINE_USERS_WRITE, "write"),
    "engine_users.assign_database": (Capability.ENGINE_USERS_WRITE, "write"),
    "databases.assign_blueprint": (Capability.DATABASES_WRITE, "write"),
    "migrations.apply_forward": (Capability.BLUEPRINTS_APPLY, "write"),
    "migrations.rollback": (Capability.BLUEPRINTS_APPLY, "destructive"),
    "migrations.stamp": (Capability.BLUEPRINTS_APPLY, "destructive"),
}

DESTRUCTIVE_SCOPE_VALUES = {"migrations.rollback", "migrations.stamp"}


# --------------------------------------------------------------------------- #
# 1.4 Capacidad integration_tokens.own                                        #
# --------------------------------------------------------------------------- #


def test_integration_tokens_own_capability_exists_with_the_stable_value():
    assert Capability.INTEGRATION_TOKENS_OWN.value == "integration_tokens.own"


def test_integration_tokens_own_spec_flags_are_pinned_by_the_invariants():
    capability_spec = capability_catalog.spec(Capability.INTEGRATION_TOKENS_OWN)

    # Mismo patrón que `tokens.own`: la tienen los tres roles (viewer ⇒ no muta ni divulga), es del
    # eje global, no pide step-up en la spec (lo exige la ruta con la de access.admin) y un token no
    # puede emitir otro token.
    assert capability_spec.scope_axis == "global"
    assert capability_spec.mutates is False
    assert capability_spec.discloses is False
    assert capability_spec.requires_step_up is False
    assert capability_spec.agent_allowed is False
    assert capability_spec.destructive is False


@pytest.mark.parametrize("role", list(GatewayRole))
def test_every_role_has_integration_tokens_own(role):
    assert Capability.INTEGRATION_TOKENS_OWN in capability_catalog.role_capabilities(role)


def test_integration_tokens_own_is_not_grantable_nor_sensitive_nor_global_set_member():
    assert capability_catalog.is_grantable(Capability.INTEGRATION_TOKENS_OWN) is False
    assert capability_catalog.is_sensitive(Capability.INTEGRATION_TOKENS_OWN) is False
    for global_capability_set in capability_catalog.GLOBAL_CAPABILITIES.values():
        assert Capability.INTEGRATION_TOKENS_OWN not in global_capability_set
    assert Capability.INTEGRATION_TOKENS_OWN not in capability_catalog.AGENT_ALLOWED


def test_authz_exposes_the_integration_tokens_own_alias():
    from app.core import authz

    assert hasattr(authz, "IntegrationTokensOwn")


# --------------------------------------------------------------------------- #
# 1.5 / 1.6 El catálogo real                                                  #
# --------------------------------------------------------------------------- #


def test_the_real_catalog_satisfies_every_invariant():
    scope_catalog._assert_integration_invariants()


def test_scope_vocabulary_is_the_twelve_literal_scopes():
    assert {scope.value for scope in IntegrationScope} == set(EXPECTED_MAPPING)
    assert len(IntegrationScope) == 12


@pytest.mark.parametrize("scope_value", sorted(EXPECTED_MAPPING))
def test_each_scope_maps_to_its_capability_and_tier(scope_value):
    expected_capability, expected_tier = EXPECTED_MAPPING[scope_value]
    scope = IntegrationScope(scope_value)

    scope_spec = integration_scope_spec(scope)
    assert scope_spec.capability == expected_capability
    assert scope_spec.tier == expected_tier
    assert INTEGRATION_ALLOWED[scope] == expected_capability


def test_four_read_scopes_six_write_scopes_and_two_destructive_scopes():
    read_scopes = [s for s in INTEGRATION_SCOPE_SPECS if s.tier == "read"]
    write_scopes = [s for s in INTEGRATION_SCOPE_SPECS if s.tier == "write"]
    destructive_scopes = [s for s in INTEGRATION_SCOPE_SPECS if s.tier == "destructive"]
    assert len(read_scopes) == 4
    assert len(write_scopes) == 6
    assert {s.scope.value for s in destructive_scopes} == DESTRUCTIVE_SCOPE_VALUES


def test_rollback_and_stamp_reuse_blueprints_apply_and_add_no_capability():
    # D15: ningún scope nuevo agrega una Capability; el tier lleva la distinción.
    rollback_spec = integration_scope_spec(IntegrationScope.MIGRATIONS_ROLLBACK)
    stamp_spec = integration_scope_spec(IntegrationScope.MIGRATIONS_STAMP)
    assert rollback_spec.capability == Capability.BLUEPRINTS_APPLY
    assert stamp_spec.capability == Capability.BLUEPRINTS_APPLY
    assert rollback_spec.mutates is True
    assert stamp_spec.mutates is True
    # apply_forward sigue siendo `write`: el guard de entorno de `_run_apply` ya la cubre.
    assert integration_scope_spec(IntegrationScope.MIGRATIONS_APPLY_FORWARD).tier == "write"


def test_scope_values_do_not_collide_with_capability_values():
    # Si un scope tuviera el valor de una Capability, un `parse_scopes` de agente lo confundiría
    # con una capacidad real.
    capability_values = {capability.value for capability in Capability}
    for scope in IntegrationScope:
        assert scope.value not in capability_values


def test_integration_scopes_do_not_leak_into_the_authz_catalog():
    published_ids = {entry["id"] for entry in capability_catalog.capability_matrix()}
    for scope in IntegrationScope:
        assert scope.value not in published_ids


def test_only_apply_forward_rollback_and_stamp_map_to_a_destructive_capability():
    destructive_scopes = {
        s.scope
        for s in INTEGRATION_SCOPE_SPECS
        if capability_catalog.spec(s.capability).destructive
    }
    assert destructive_scopes == {
        IntegrationScope.MIGRATIONS_APPLY_FORWARD,
        IntegrationScope.MIGRATIONS_ROLLBACK,
        IntegrationScope.MIGRATIONS_STAMP,
    }


def test_error_codes_are_stable_and_have_the_documented_http_status():
    expected_status_by_code = {
        "integration.disabled": 503,
        "integration.token_invalid": 401,
        "integration.scope_missing": 403,
        "integration.server_not_allowed": 403,
        "integration.blueprint_not_allowed": 403,
        "integration.profile_requires_grant_admin": 403,
        "integration.blueprint_already_assigned": 409,
        "integration.server_mismatch": 409,
        "integration.blueprint_not_assigned": 404,
        "integration.migration_target_not_forward": 422,
        "integration_token.not_found": 404,
        "integration_token.ttl_too_long": 422,
        "integration_token.scope_not_allowed": 403,
        "integration_token.unknown_scope": 422,
        "integration_token.server_allowlist_required": 422,
        "integration_token.already_revoked": 409,
        "integration_token.server_not_found": 422,
        "integration_token.blueprint_not_found": 422,
        "integration.environment_blocks_destructive": 409,
        "integration.environment_unclassified": 409,
        "integration.database_quarantined": 409,
        "integration.rollback_unapplied_version": 409,
        "integration.stamp_version_conflict": 409,
        "integration.stamp_orphan_accounting": 409,
        "integration_token.blueprint_allowlist_required": 422,
        "integration_token.non_expiring_not_allowed": 422,
    }
    assert dict(scope_catalog.INTEGRATION_ERROR_STATUS) == expected_status_by_code


# --------------------------------------------------------------------------- #
# Mutaciones: cada invariante tiene que poder FALLAR                          #
# --------------------------------------------------------------------------- #


def _replace_scope_spec(scope: IntegrationScope, **changes):
    """Copia de ``INTEGRATION_SCOPE_SPECS`` con UN spec cambiado."""
    return tuple(
        dataclasses.replace(s, **changes) if s.scope == scope else s
        for s in INTEGRATION_SCOPE_SPECS
    )


def _patch_capability_spec(monkeypatch, capability: Capability, **changes):
    """Cambia la spec de UNA capacidad en el catálogo de capacidades (para el lado del mapeo)."""
    patched_by_id = dict(capability_catalog._BY_ID)
    patched_by_id[capability] = dataclasses.replace(patched_by_id[capability], **changes)
    monkeypatch.setattr(capability_catalog, "_BY_ID", MappingProxyType(patched_by_id))


def test_i1_rejects_a_missing_spec():
    specs_without_one = tuple(
        s for s in INTEGRATION_SCOPE_SPECS if s.scope != IntegrationScope.SERVERS_LIST
    )
    with pytest.raises(AssertionError, match="I1"):
        scope_catalog._assert_integration_invariants(specs_without_one)


def test_i1_rejects_a_duplicated_spec():
    duplicated = INTEGRATION_SCOPE_SPECS + (INTEGRATION_SCOPE_SPECS[0],)
    with pytest.raises(AssertionError, match="I1"):
        scope_catalog._assert_integration_invariants(duplicated)


def test_i2_rejects_a_capability_outside_owner():
    # `servers.admin` no está en la cadena de roles (es de una global).
    mutated = _replace_scope_spec(IntegrationScope.SERVERS_LIST, capability=Capability.SERVERS_ADMIN)
    with pytest.raises(AssertionError, match="I2"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i3_rejects_a_capability_that_belongs_to_a_global(monkeypatch):
    # Simula que `databases.read` pasara a pertenecer a access_admin: sigue estando en owner (I2
    # pasa) pero una global la otorgaría fuera de la capa 2.
    patched_globals = dict(capability_catalog.GLOBAL_CAPABILITIES)
    patched_globals[GlobalCapability.ACCESS_ADMIN] = frozenset(
        {Capability.ACCESS_ADMIN_CAP, Capability.DATABASES_READ}
    )
    monkeypatch.setattr(
        capability_catalog, "GLOBAL_CAPABILITIES", MappingProxyType(patched_globals)
    )
    with pytest.raises(AssertionError, match="I3"):
        scope_catalog._assert_integration_invariants()


def test_i3_rejects_a_security_officer_capability(monkeypatch):
    patched_globals = dict(capability_catalog.GLOBAL_CAPABILITIES)
    patched_globals[GlobalCapability.SECURITY_OFFICER] = frozenset(
        {Capability.AUDIT_READ, Capability.SERVERS_READ}
    )
    monkeypatch.setattr(
        capability_catalog, "GLOBAL_CAPABILITIES", MappingProxyType(patched_globals)
    )
    with pytest.raises(AssertionError, match="I3"):
        scope_catalog._assert_integration_invariants()


def test_i4_rejects_a_disclosing_capability(monkeypatch):
    _patch_capability_spec(monkeypatch, Capability.DATABASES_READ, discloses=True)
    with pytest.raises(AssertionError, match="I4"):
        scope_catalog._assert_integration_invariants()


def test_i5_rejects_a_data_module_capability(monkeypatch):
    _patch_capability_spec(monkeypatch, Capability.DATABASES_READ, module="data")
    with pytest.raises(AssertionError, match="I5"):
        scope_catalog._assert_integration_invariants()


@pytest.mark.parametrize("forbidden_level", ["drop", "credentials", "grant_admin", "secrets"])
def test_i5_rejects_a_forbidden_level(monkeypatch, forbidden_level):
    _patch_capability_spec(monkeypatch, Capability.ENGINE_USERS_WRITE, level=forbidden_level)
    with pytest.raises(AssertionError, match="I5"):
        scope_catalog._assert_integration_invariants()


def test_i6_rejects_the_agent_tokens_own_capability():
    mutated = _replace_scope_spec(
        IntegrationScope.SERVERS_LIST, capability=Capability.TOKENS_OWN, mutates=False
    )
    with pytest.raises(AssertionError, match="I6"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i6_rejects_the_integration_tokens_own_capability():
    mutated = _replace_scope_spec(
        IntegrationScope.SERVERS_LIST,
        capability=Capability.INTEGRATION_TOKENS_OWN,
        mutates=False,
    )
    with pytest.raises(AssertionError, match="I6"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i7_rejects_a_destructive_capability_outside_the_three_migration_scopes(monkeypatch):
    _patch_capability_spec(monkeypatch, Capability.DATABASES_WRITE, destructive=True)
    with pytest.raises(AssertionError, match="I7"):
        scope_catalog._assert_integration_invariants()


def test_i7_allows_apply_forward_rollback_and_stamp_on_a_destructive_capability():
    # `blueprints.apply` es destructive en el catálogo real y los tres scopes lo mapean.
    assert capability_catalog.spec(Capability.BLUEPRINTS_APPLY).destructive is True
    scope_catalog._assert_integration_invariants()


def test_i8_rejects_a_scope_whose_mutates_flag_disagrees_with_its_capability():
    # `databases.create` declarada de solo lectura mientras su capacidad muta.
    mutated = _replace_scope_spec(
        IntegrationScope.DATABASES_CREATE, mutates=False, tier="read"
    )
    with pytest.raises(AssertionError, match="I8"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i9_rejects_a_scope_beyond_the_fixed_literal(monkeypatch):
    # El enum tiene 12 miembros; si el literal fijo esperara uno menos, hay un scope de más.
    shrunk_literal = frozenset(scope_catalog.EXPECTED_SCOPE_VALUES) - {"servers.list"}
    monkeypatch.setattr(scope_catalog, "EXPECTED_SCOPE_VALUES", shrunk_literal)
    with pytest.raises(AssertionError, match="I9"):
        scope_catalog._assert_integration_invariants()


def test_i12_rejects_tier_read_on_a_mutating_scope():
    mutated = _replace_scope_spec(IntegrationScope.DATABASES_CREATE, tier="read")
    with pytest.raises(AssertionError, match="I12"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i12_rejects_a_non_read_tier_on_a_read_only_scope():
    mutated = _replace_scope_spec(IntegrationScope.SERVERS_LIST, tier="write")
    with pytest.raises(AssertionError, match="I12"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i9_literal_has_exactly_the_twelve_scope_values():
    assert set(scope_catalog.EXPECTED_SCOPE_VALUES) == set(EXPECTED_MAPPING)
    assert len(scope_catalog.EXPECTED_SCOPE_VALUES) == 12


def test_i10_rejects_the_destructive_tier_on_a_scope_outside_rollback_and_stamp():
    # apply_forward marcado destructivo: I11 se cumple (su capacidad es destructive y pide step-up),
    # así que lo que tiene que saltar es la pertenencia al literal de I10.
    mutated = _replace_scope_spec(IntegrationScope.MIGRATIONS_APPLY_FORWARD, tier="destructive")
    with pytest.raises(AssertionError, match="I10"):
        scope_catalog._assert_integration_invariants(mutated)


@pytest.mark.parametrize("scope", [IntegrationScope.MIGRATIONS_ROLLBACK, IntegrationScope.MIGRATIONS_STAMP])
def test_i10_rejects_rollback_or_stamp_without_the_destructive_tier(scope):
    mutated = _replace_scope_spec(scope, tier="write")
    with pytest.raises(AssertionError, match="I10"):
        scope_catalog._assert_integration_invariants(mutated)


def test_i11_rejects_a_destructive_scope_whose_capability_does_not_ask_for_step_up(monkeypatch):
    _patch_capability_spec(monkeypatch, Capability.BLUEPRINTS_APPLY, requires_step_up=False)
    with pytest.raises(AssertionError, match="I11"):
        scope_catalog._assert_integration_invariants()


def test_i11_rejects_a_destructive_scope_whose_capability_is_not_destructive(monkeypatch):
    _patch_capability_spec(monkeypatch, Capability.BLUEPRINTS_APPLY, destructive=False)
    with pytest.raises(AssertionError, match="I11"):
        scope_catalog._assert_integration_invariants()


def test_i11_rejects_a_destructive_scope_that_does_not_mutate():
    # Con mutates=False saltan primero I8/I12 si se evalúan antes; se afirma que NINGUNA mutación
    # de este tipo pasa en silencio.
    mutated = _replace_scope_spec(IntegrationScope.MIGRATIONS_STAMP, mutates=False)
    with pytest.raises(AssertionError, match="I8|I11|I12"):
        scope_catalog._assert_integration_invariants(mutated)
