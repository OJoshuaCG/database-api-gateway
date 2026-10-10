"""
La excepción CERRADA del catálogo para ``data.read`` / ``data.query`` (S35-S38).

QUÉ SE FIJA
-----------
- S35: el catálogo importa con EXACTAMENTE las cuatro excepciones (``AGENT_DATA_EXCEPTIONS``).
- S36: una capacidad que divulga y es de agente FUERA de la excepción rompe el import (inv. 5).
- S37: una capacidad de agente que muta rompe el import, ``data.*`` incluida (inv. 5).
- S38: ``viewer`` y ``operator`` nunca las tienen; solo ``owner``, y son sensibles (segundo
  aprobador al otorgarlas sueltas).
- Invariante 13: el conjunto está fijado al conjunto literal de cuatro y cada miembro cumple el contrato.
- Kill switch: con el switch de la capacidad apagado su scope queda INERTE (``parse_scopes``), pero
  ``parse_stored_scopes`` lo conserva para mostrarlo y editarlo.

Las violaciones se simulan reemplazando ``CAPABILITIES``/``_BY_ID`` y re-corriendo
``_assert_invariants()``: es el mismo patrón que ``tests/test_capability_catalog.py``.
"""

import dataclasses
from types import MappingProxyType

import pytest

from app.core import environments
from app.services import capability_catalog as cc
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    AGENT_DATA_EXCEPTIONS,
    CAPABILITIES,
    Capability,
    GatewayRole,
)

EXCEPCION = {
    Capability.DATA_READ,
    Capability.DATA_QUERY,
    Capability.DATA_DEFINITIONS,
    Capability.DATA_BLUEPRINT_SQL,
}


def _con_spec(monkeypatch, cap: Capability, **cambios) -> None:
    """Reemplaza el spec de ``cap`` y re-publica ``CAPABILITIES``/``_BY_ID`` consistentes."""
    nuevo = dataclasses.replace(cc.spec(cap), **cambios)
    capacidades = tuple(nuevo if s.id == cap else s for s in CAPABILITIES)
    monkeypatch.setattr(cc, "CAPABILITIES", capacidades)
    monkeypatch.setattr(cc, "_BY_ID", MappingProxyType({s.id: s for s in capacidades}))


# --------------------------------------------------------------------------- #
# S35: el catálogo carga con exactamente las cuatro excepciones                 #
# --------------------------------------------------------------------------- #


def test_the_catalog_loads_with_exactly_the_four_data_exceptions():
    cc._assert_invariants()  # no levanta
    assert AGENT_DATA_EXCEPTIONS == frozenset(EXCEPCION)
    disclosing_agents = {s.id for s in CAPABILITIES if s.agent_allowed and s.discloses}
    assert disclosing_agents == EXCEPCION
    assert not {s.id for s in CAPABILITIES if s.agent_allowed and s.mutates}


@pytest.mark.parametrize("cap", sorted(EXCEPCION, key=lambda c: c.value))
def test_each_data_capability_has_the_contract_flags(cap):
    s = cc.spec(cap)
    assert s.module == "data"
    assert s.discloses and s.requires_step_up and s.agent_allowed
    assert not s.mutates and not s.destructive
    assert s.scope_axis == "environment"
    assert cap in AGENT_ALLOWED


# --------------------------------------------------------------------------- #
# S36 / S37: lo que sigue prohibido                                            #
# --------------------------------------------------------------------------- #


def test_s36_a_disclosing_agent_capability_outside_the_exception_breaks_the_import(monkeypatch):
    """``exports.download`` divulga con step-up: hacerla de agente sin ser excepción rompe."""
    _con_spec(monkeypatch, Capability.EXPORTS_DOWNLOAD, agent_allowed=True)
    with pytest.raises(AssertionError, match="agent_allowed y divulga"):
        cc._assert_invariants()


def test_s37_an_agent_capability_that_mutates_breaks_the_import(monkeypatch):
    _con_spec(monkeypatch, Capability.DATABASES_WRITE, agent_allowed=True)
    with pytest.raises(AssertionError, match="agent_allowed y muta"):
        cc._assert_invariants()


@pytest.mark.parametrize("cap", sorted(EXCEPCION, key=lambda c: c.value))
def test_s37_a_data_capability_that_mutates_breaks_the_import(monkeypatch, cap):
    """La excepción es de DIVULGACIÓN: mutar no tiene excepción ni siquiera para ``data.*``."""
    _con_spec(monkeypatch, cap, mutates=True)
    with pytest.raises(AssertionError, match="agent_allowed y muta"):
        cc._assert_invariants()


def test_a_step_up_capability_in_the_agent_ceiling_outside_the_exception_breaks_it(monkeypatch):
    """Invariante 11, que sigue valiendo para todo lo que no es de datos."""
    _con_spec(monkeypatch, Capability.DATABASES_DROP, agent_allowed=True)
    with pytest.raises(AssertionError):
        cc._assert_invariants()


# --------------------------------------------------------------------------- #
# Invariante 13: el conjunto está fijado                                       #
# --------------------------------------------------------------------------- #


def test_inv13_the_exception_set_is_pinned_to_the_literal_set(monkeypatch):
    """Agregar un miembro (aun uno coherente con inv. 5 y 11) obliga a tocar el invariante."""
    _con_spec(monkeypatch, Capability.EXPORTS_DOWNLOAD, agent_allowed=True)
    monkeypatch.setattr(
        cc, "AGENT_DATA_EXCEPTIONS", frozenset(EXCEPCION | {Capability.EXPORTS_DOWNLOAD})
    )
    with pytest.raises(AssertionError, match="exactamente"):
        cc._assert_invariants()


def test_inv13_removing_a_member_from_the_set_breaks_it(monkeypatch):
    monkeypatch.setattr(cc, "AGENT_DATA_EXCEPTIONS", frozenset({Capability.DATA_READ}))
    with pytest.raises(AssertionError):
        cc._assert_invariants()


def test_inv13_a_data_capability_must_be_owner_only(monkeypatch):
    operator = cc.ROLE_CAPABILITIES[GatewayRole.OPERATOR] | {Capability.DATA_READ}
    owner = cc.ROLE_CAPABILITIES[GatewayRole.OWNER]
    roles = dict(cc.ROLE_CAPABILITIES)
    roles[GatewayRole.OPERATOR] = operator
    roles[GatewayRole.OWNER] = owner | operator
    monkeypatch.setattr(cc, "ROLE_CAPABILITIES", MappingProxyType(roles))
    with pytest.raises(AssertionError):
        cc._assert_invariants()


def test_inv13_a_data_capability_without_step_up_breaks_the_import(monkeypatch):
    _con_spec(monkeypatch, Capability.DATA_QUERY, requires_step_up=False)
    with pytest.raises(AssertionError):
        cc._assert_invariants()


# --------------------------------------------------------------------------- #
# S38: quién las tiene                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [GatewayRole.VIEWER, GatewayRole.OPERATOR])
def test_s38_viewer_and_operator_never_get_the_data_scopes(role):
    assert not (EXCEPCION & cc.ROLE_CAPABILITIES[role])
    assert not (EXCEPCION & cc.role_capabilities(role))


def test_s38_only_owner_has_them_and_no_global_function_does():
    assert EXCEPCION <= cc.ROLE_CAPABILITIES[GatewayRole.OWNER]
    assert EXCEPCION <= cc.OWNER_ONLY_CAPABILITIES
    for caps in cc.GLOBAL_CAPABILITIES.values():
        assert not (EXCEPCION & caps)


def test_the_data_capabilities_are_grantable_sensitive_and_need_a_second_approver():
    for cap in EXCEPCION:
        assert cc.is_grantable(cap)
        assert cc.is_sensitive(cap)
        assert cc.needs_second_approver(capability=cap)
        assert cap not in cc.IMPLIED_READ  # no hay lectura de nivel viewer en el módulo "data"
    assert not any(cap in implied for implied in cc.IMPLIED_READ.values())
    rows = {r["id"]: r for r in cc.capability_matrix()}
    for cap in EXCEPCION:
        assert rows[cap.value]["sensitive"] and rows[cap.value]["agent_allowed"]
        assert rows[cap.value]["discloses"] and not rows[cap.value]["mutates"]


def test_the_sensitive_set_is_the_old_eleven_plus_the_four_data_capabilities_and_grant_admin():
    sensibles = {s.id.value for s in CAPABILITIES if cc.is_sensitive(s.id)}
    assert len(sensibles) == 16
    assert {
        "data.read",
        "data.query",
        "data.definitions",
        "data.blueprint_sql",
        "engine_users.grant_admin",
    } <= sensibles
    assert sensibles == cc._SENSITIVE_POLICY


# --------------------------------------------------------------------------- #
# Kill switch: scope inerte, no borrado                                        #
# --------------------------------------------------------------------------- #


def test_data_scopes_are_inert_while_their_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", False)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", False)
    raw = "databases.read,data.read,data.query"
    assert cc.parse_scopes(raw) == frozenset({Capability.DATABASES_READ})
    # Mostrar/editar conserva lo guardado.
    assert cc.parse_stored_scopes(raw) == frozenset(
        {Capability.DATABASES_READ, Capability.DATA_READ, Capability.DATA_QUERY}
    )


def test_each_data_scope_follows_its_own_kill_switch(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", False)
    assert cc.parse_scopes("data.read,data.query") == frozenset({Capability.DATA_READ})
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", False)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", True)
    assert cc.parse_scopes("data.read,data.query") == frozenset({Capability.DATA_QUERY})


def test_the_kill_switches_are_off_by_default_and_unknown_capabilities_are_off():
    assert environments.MCP_DATA_READ_ENABLED is False
    assert environments.MCP_DATA_QUERY_ENABLED is False
    assert cc.data_capability_enabled(Capability.DATABASES_READ) is False
    assert cc.data_capability_enabled("no.existe") is False


def test_non_data_scopes_ignore_the_kill_switches(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", False)
    assert cc.parse_scopes("blueprints.read") == frozenset({Capability.BLUEPRINTS_READ})
