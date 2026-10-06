"""
El scope ``data.definitions`` (S3 de ``mcp-schema-definitions``): existe, es inerte y no abre nada.

QUÉ SE FIJA
-----------
- La excepción cerrada es EXACTAMENTE la terna ``{data.read, data.query, data.definitions}``.
- Kill switch ``MCP_SCHEMA_DEFINITIONS_ENABLED``: nace apagado y, apagado, el scope queda inerte
  (``parse_scopes`` y el token lo descartan) aunque siga guardado y visible.
- Solo ``owner`` lo tiene; es sensible (segundo aprobador al otorgarlo suelto) y el emisor de un
  token con este scope necesita un step-up fresco.
- El techo de agente sigue sin contener ninguna capacidad que mute.
- Esta etapa no publica ninguna tool: ninguna entrada del registro usa el scope.

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

SCOPE = Capability.DATA_DEFINITIONS


def _emisor(*, role: GatewayRole = GatewayRole.OWNER, fresco: bool):
    return admin_actor(
        user_id=1,
        username="emisor",
        role=role,
        step_up_until=OPEN_WINDOW if fresco else None,
    )


def test_the_data_exception_set_is_exactly_the_literal_triple():
    assert AGENT_DATA_EXCEPTIONS == frozenset(
        {Capability.DATA_READ, Capability.DATA_QUERY, Capability.DATA_DEFINITIONS}
    )
    assert len(AGENT_DATA_EXCEPTIONS) == 3


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
    assert environments.MCP_SCHEMA_DEFINITIONS_ENABLED is False
    assert cc.data_capability_enabled(SCOPE) is False


def test_the_scope_is_inert_while_the_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", False)
    raw = "databases.read,data.definitions"
    assert cc.parse_scopes(raw) == frozenset({Capability.DATABASES_READ})
    # Guardado y visible, no borrado: un PATCH no puede descartarlo en silencio.
    assert cc.parse_stored_scopes(raw) == frozenset({Capability.DATABASES_READ, SCOPE})

    token = token_actor(
        token_pk=1, token_id="t", name="n", scopes=raw, project_id=1, issuer=_emisor(fresco=True)
    )
    assert not token.has(SCOPE)


def test_the_scope_is_exercised_only_once_the_kill_switch_is_on(monkeypatch):
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", True)
    assert cc.parse_scopes("data.definitions") == frozenset({SCOPE})
    token = token_actor(
        token_pk=1,
        token_id="t",
        name="n",
        scopes="databases.read,data.definitions",
        project_id=1,
        issuer=_emisor(fresco=True),
    )
    assert token.has(SCOPE)


def test_the_kill_switch_is_independent_from_the_other_data_switches(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", False)
    assert cc.parse_scopes("data.read,data.query,data.definitions") == frozenset(
        {Capability.DATA_READ, Capability.DATA_QUERY}
    )


def test_the_issuer_still_caps_the_scope(monkeypatch):
    """Un emisor ``operator`` no delega lo que no tiene, con el switch ya encendido."""
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", True)
    operator = admin_actor(user_id=2, username="op", role=GatewayRole.OPERATOR)
    token = token_actor(
        token_pk=1,
        token_id="t",
        name="n",
        scopes="databases.read,data.definitions",
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
    assert "data.definitions" in cc._SENSITIVE_POLICY
    assert len(cc._SENSITIVE_POLICY) == 14


def test_issuing_a_token_with_the_scope_needs_a_fresh_step_up(monkeypatch):
    intents: list[str] = []
    monkeypatch.setattr(audit_mod, "record_intent", lambda action, **kw: intents.append(action))

    with pytest.raises(AppHttpException) as refused:
        atc._validate_scopes(["data.definitions"], admin=_emisor(fresco=False))
    assert refused.value.status_code == 403
    assert refused.value.public_context["code"] == "access.step_up_required"
    assert intents == []

    assert atc._validate_scopes(["data.definitions"], admin=_emisor(fresco=True)) == [
        "data.definitions"
    ]
    assert intents == ["api_token.data_scope_grant"]


def test_a_token_cannot_be_issued_with_a_mutating_scope():
    with pytest.raises(AppHttpException) as refused:
        atc._validate_scopes(["databases.write"], admin=_emisor(fresco=True))
    assert refused.value.status_code == 422


def test_no_tool_uses_the_scope_while_the_kill_switch_is_off(monkeypatch):
    """
    ``get_definition`` es la única tool del scope y solo se registra con
    ``MCP_SCHEMA_DEFINITIONS_ENABLED`` encendido AL IMPORTAR. ``registry.TOOLS`` ya está fijado por el
    entorno de quien corre el test, así que se reconstruye con el switch apagado explícito: el
    resultado no depende de cómo se lanzó la suite.
    """
    monkeypatch.setattr(environments, "MCP_SCHEMA_DEFINITIONS_ENABLED", False)
    tools_with_switch_off = registry._build()
    assert not [t.name for t in tools_with_switch_off if t.scope == SCOPE.value]
