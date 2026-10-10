"""
El scope ``data.blueprint_sql`` (``mcp-blueprint-read-tools``): existe, es inerte y no abre nada.

QUÉ SE FIJA
-----------
- El scope pertenece a la excepción cerrada de datos (``AGENT_DATA_EXCEPTIONS``), que ahora tiene
  EXACTAMENTE cuatro miembros.
- Kill switch ``MCP_BLUEPRINT_SQL_ENABLED``: nace apagado y, apagado, el scope queda inerte
  (``parse_scopes`` y el token lo descartan) aunque siga guardado y visible.
- Solo ``owner`` lo tiene; es sensible (segundo aprobador al otorgarlo suelto) y el emisor de un
  token con este scope necesita un step-up fresco.
- Nunca viaja en los scopes por defecto de un token (``blueprints.read``).
- Registro: ninguna tool usa el scope mientras el switch está apagado; encendido, lo usa una sola
  (``get_blueprint_migration``). Las tools en sí se prueban en ``tests.test_mcp_blueprint_tools``.

Propiedades del catálogo y del emisor: sin ``TestClient`` ni BD (el rastro de auditoría se simula).
"""

import pytest

from app.controllers import api_token_controller as atc
from app.core import environments
from app.core.actor import admin_actor, token_actor
from app.exceptions import AppHttpException
from app.mcp import registry
from app.services import audit as audit_mod
from app.services import capability_catalog as cc
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    AGENT_DATA_EXCEPTIONS,
    CAPABILITIES,
    Capability,
    GatewayRole,
)
from tests.step_up_helpers import OPEN_WINDOW

SCOPE = Capability.DATA_BLUEPRINT_SQL
SCOPE_VALUE = "data.blueprint_sql"
EXPECTED_EXCEPTION_SIZE = 4
EXPECTED_SENSITIVE_POLICY_SIZE = 16


def _emisor(*, role: GatewayRole = GatewayRole.OWNER, fresco: bool):
    return admin_actor(
        user_id=1,
        username="emisor",
        role=role,
        step_up_until=OPEN_WINDOW if fresco else None,
    )


def test_the_scope_is_part_of_the_closed_data_exception():
    assert SCOPE in AGENT_DATA_EXCEPTIONS
    assert len(AGENT_DATA_EXCEPTIONS) == EXPECTED_EXCEPTION_SIZE


def test_the_scope_lives_in_the_data_module_and_in_the_agent_ceiling():
    spec = cc.spec(SCOPE)
    assert spec.module == "data"
    assert spec.discloses and spec.requires_step_up and spec.agent_allowed
    assert not spec.mutates and not spec.destructive
    assert SCOPE in AGENT_ALLOWED


def test_the_agent_ceiling_still_excludes_every_mutating_capability():
    mutating = {s.id for s in CAPABILITIES if s.mutates}
    assert mutating
    assert not (mutating & AGENT_ALLOWED)


def test_the_kill_switch_is_off_by_default_and_bound_to_the_scope():
    assert environments.MCP_BLUEPRINT_SQL_ENABLED is False
    assert cc.data_capability_enabled(SCOPE) is False


def test_the_scope_is_inert_while_the_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", False)
    raw = f"blueprints.read,{SCOPE_VALUE}"
    assert cc.parse_scopes(raw) == frozenset({Capability.BLUEPRINTS_READ})
    # Guardado y visible, no borrado: un PATCH no puede descartarlo en silencio.
    assert cc.parse_stored_scopes(raw) == frozenset({Capability.BLUEPRINTS_READ, SCOPE})

    token = token_actor(
        token_pk=1, token_id="t", name="n", scopes=raw, project_id=1, issuer=_emisor(fresco=True)
    )
    assert not token.has(SCOPE)


def test_the_scope_is_exercised_only_once_the_kill_switch_is_on(monkeypatch):
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", True)
    assert cc.parse_scopes(SCOPE_VALUE) == frozenset({SCOPE})
    token = token_actor(
        token_pk=1,
        token_id="t",
        name="n",
        scopes=f"blueprints.read,{SCOPE_VALUE}",
        project_id=1,
        issuer=_emisor(fresco=True),
    )
    assert token.has(SCOPE)


def test_the_kill_switch_is_independent_from_the_other_data_switches(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", False)
    assert cc.parse_scopes("data.read,data.query,data.definitions,data.blueprint_sql") == frozenset(
        {Capability.DATA_READ, Capability.DATA_QUERY, Capability.DATA_DEFINITIONS}
    )


def test_turning_on_the_other_data_switches_does_not_enable_the_scope(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", False)
    assert cc.data_capability_enabled(SCOPE) is False


def test_the_issuer_still_caps_the_scope(monkeypatch):
    """Un emisor ``operator`` no delega lo que no tiene, con el switch ya encendido."""
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", True)
    operator = admin_actor(user_id=2, username="op", role=GatewayRole.OPERATOR)
    token = token_actor(
        token_pk=1,
        token_id="t",
        name="n",
        scopes=f"blueprints.read,{SCOPE_VALUE}",
        project_id=1,
        issuer=operator,
    )
    assert not token.has(SCOPE)


@pytest.mark.parametrize("role", [GatewayRole.VIEWER, GatewayRole.OPERATOR])
def test_only_owner_has_the_scope(role):
    assert SCOPE not in cc.ROLE_CAPABILITIES[role]
    assert SCOPE not in cc.role_capabilities(role)
    assert SCOPE in cc.ROLE_CAPABILITIES[GatewayRole.OWNER]
    assert SCOPE in cc.OWNER_ONLY_CAPABILITIES
    for capabilities in cc.GLOBAL_CAPABILITIES.values():
        assert SCOPE not in capabilities


def test_the_scope_is_sensitive_and_needs_a_second_approver():
    assert cc.is_grantable(SCOPE)
    assert cc.is_sensitive(SCOPE)
    assert cc.needs_second_approver(capability=SCOPE)
    assert SCOPE_VALUE in cc._SENSITIVE_POLICY
    assert len(cc._SENSITIVE_POLICY) == EXPECTED_SENSITIVE_POLICY_SIZE


def test_issuing_a_token_with_the_scope_needs_a_fresh_step_up(monkeypatch):
    intents: list[str] = []
    monkeypatch.setattr(audit_mod, "record_intent", lambda action, **kw: intents.append(action))

    with pytest.raises(AppHttpException) as refused:
        atc._validate_scopes([SCOPE_VALUE], admin=_emisor(fresco=False))
    assert refused.value.status_code == 403
    assert refused.value.public_context["code"] == "access.step_up_required"
    assert intents == []

    assert atc._validate_scopes([SCOPE_VALUE], admin=_emisor(fresco=True)) == [SCOPE_VALUE]
    assert intents == ["api_token.data_scope_grant"]


def test_the_default_token_scopes_never_include_the_scope(monkeypatch):
    """
    Sin scopes explícitos ``create_token`` usa ``blueprints.read``; ese valor por defecto es el
    único que no pide step-up y no arrastra el scope, ni siquiera con el switch encendido.
    """
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", True)
    default_scopes = [Capability.BLUEPRINTS_READ.value]
    validated = atc._validate_scopes(default_scopes, admin=_emisor(fresco=False))
    assert validated == default_scopes
    assert SCOPE_VALUE not in validated


def test_a_token_cannot_be_issued_with_a_mutating_scope():
    with pytest.raises(AppHttpException) as refused:
        atc._validate_scopes(["databases.write"], admin=_emisor(fresco=True))
    assert refused.value.status_code == 422


def test_no_tool_uses_the_scope_while_the_kill_switch_is_off(monkeypatch):
    """
    Con el switch apagado ninguna tool del registro usa el scope. ``registry.TOOLS`` ya está fijado
    por el entorno de quien corre el test, así que se reconstruye con el switch apagado explícito:
    el resultado no depende de cómo se lanzó la suite.
    """
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", False)
    tools_with_switch_off = registry._build()
    assert not [t.name for t in tools_with_switch_off if t.scope == SCOPE_VALUE]


def test_only_get_blueprint_migration_uses_the_scope_once_the_kill_switch_is_on():
    tools_with_switch_on = registry._build(blueprint_sql_enabled=True)
    assert [t.name for t in tools_with_switch_on if t.scope == SCOPE_VALUE] == [
        "get_blueprint_migration"
    ]
