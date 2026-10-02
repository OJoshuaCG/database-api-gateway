"""
Predicados del catálogo para las capacidades puntuales (``capability_grants``).

Propiedad sobre el CATÁLOGO, sin ``TestClient`` ni BD: ``is_grantable``, ``is_sensitive``,
``IMPLIED_READ``, los códigos de error y las columnas nuevas de ``capability_matrix()``.
"""

import pytest

from app.services import capability_catalog as cc
from app.services.capability_catalog import CAPABILITIES, Capability, GatewayRole

SENSITIVE = {
    "engine_users.secrets",
    "engine_users.credentials",
    "blueprints.captures",
    "clones.execute",
    "exports.download",
    "sql_console.execute",
    "engine_users.drop",
    "databases.drop",
}


def test_sensitive_set_is_exactly_the_eight_policy_capabilities():
    assert {s.id.value for s in CAPABILITIES if cc.is_sensitive(s.id)} == SENSITIVE


def test_global_axis_capabilities_are_never_grantable():
    globales = [s for s in CAPABILITIES if s.scope_axis == "global"]
    assert globales
    assert not any(cc.is_grantable(s.id) for s in globales)
    # La del hardening: solo `security_officer`, nunca suelta.
    assert not cc.is_grantable(Capability.ENVIRONMENTS_WRITE)


def test_grantable_is_exactly_the_non_global_axes():
    for s in CAPABILITIES:
        assert cc.is_grantable(s.id) is (s.scope_axis != "global")


@pytest.mark.parametrize("raw", ["gateway.admin", "no.existe", "", "ENGINE_USERS_WRITE"])
def test_unknown_or_global_strings_fail_closed(raw):
    assert cc.is_grantable(raw) is False
    assert cc.is_sensitive(raw) is False


def test_predicates_accept_plain_strings_and_enum_members():
    assert cc.is_grantable("blueprints.apply") is cc.is_grantable(Capability.BLUEPRINTS_APPLY)
    assert cc.is_sensitive("databases.drop") is True


def test_every_sensitive_capability_is_grantable():
    assert all(cc.is_grantable(c) for c in SENSITIVE)


def test_implied_reads_are_viewer_level_non_mutating_non_disclosing():
    viewer = cc.ROLE_CAPABILITIES[GatewayRole.VIEWER]
    assert cc.IMPLIED_READ
    for cap, implied in cc.IMPLIED_READ.items():
        assert implied, cap
        for r in implied:
            assert r in viewer
            sp = cc.spec(r)
            assert not sp.mutates and not sp.discloses
            assert sp.module == cc.spec(cap).module


def test_sql_console_execute_implies_history():
    assert cc.IMPLIED_READ[Capability.SQL_CONSOLE_EXECUTE] == {Capability.SQL_CONSOLE_HISTORY}


def test_reads_and_global_capabilities_imply_nothing():
    assert Capability.DATABASES_READ not in cc.IMPLIED_READ
    assert Capability.ENVIRONMENTS_WRITE not in cc.IMPLIED_READ


def test_mutating_grantable_capabilities_with_a_read_sibling_all_imply_it():
    for s in CAPABILITIES:
        if cc.is_grantable(s.id) and (s.mutates or s.discloses):
            assert s.id in cc.IMPLIED_READ, s.id.value


def test_matrix_publishes_the_new_columns_derived_from_the_predicates():
    rows = {r["id"]: r for r in cc.capability_matrix()}
    assert {i for i, r in rows.items() if r["sensitive"]} == SENSITIVE
    for cap in Capability:
        row = rows[cap.value]
        assert row["grantable"] is cc.is_grantable(cap)
        assert row["implies"] == sorted(c.value for c in cc.IMPLIED_READ.get(cap, ()))
    assert rows["environments.write"]["grantable"] is False
    assert rows["sql_console.execute"]["implies"] == ["sql_console.history"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("CODE_CAPABILITY_NOT_GRANTABLE", "access.capability_not_grantable"),
        ("CODE_GRANT_DUPLICATE", "access.grant_duplicate"),
        ("CODE_GRANT_SCOPE_NOT_FOUND", "access.grant_scope_not_found"),
        ("CODE_GRANT_USER_INACTIVE", "access.grant_user_inactive"),
        ("CODE_SELF_APPROVAL", "access.self_approval_forbidden"),
        ("CODE_GRANT_NOT_PENDING", "access.grant_not_pending"),
        ("CODE_GRANT_NOT_FOUND", "access.grant_not_found"),
    ],
)
def test_error_codes_exist_with_the_contract_values(name, value):
    assert getattr(cc, name) == value
