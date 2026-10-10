"""
El actor de integración en la capa 2 de autorización, y los sitios donde "máquina" ya no es
"agente MCP".

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Un token de integración EJERCE los permisos de su emisor, recortados por su lista de scopes. La
capa 1 es estricta (solo la capacidad mapeada del scope) y la capa 2 tiene que resolver contra
el rol REAL del emisor en el destino: si el emisor perdió ``owner`` en producción, el token lo
pierde en producción. Eso exige que ``capability_resolution`` trate al actor de integración como
humano (``{"admin", "integration"}``) sin tocar al token de agente del MCP, que sigue fuera.

La segunda mitad fija las decisiones de ``Actor.is_machine`` (``api_token`` + ``integration``)
sobre cada sitio que antes preguntaba ``is_agent``: un actor de integración lleva el ``id`` de su
emisor, así que cualquier sitio que lea ``actor.id`` para actuar "como el usuario" le entregaría
las credenciales o la identidad de una persona a una máquina.
"""

import pytest

from app.controllers.auth_controller import CODE_SESSION_REQUIRED, AuthController
from app.controllers.managed_database_controller import ManagedDatabaseController
from app.core import capability_resolution as capability_resolution
from app.core import step_up
from app.core.actor import Actor, admin_actor, integration_actor, token_actor
from app.core.scope import ScopeTarget, assert_layer2
from app.exceptions import AppHttpException
from app.services import data_credential_catalog as data_credential_codes
from app.services.capability_catalog import CODE_FORBIDDEN, Capability, GatewayRole
from tests.scope_helpers import env_id, sembrar_bd
from tests.step_up_helpers import OPEN_WINDOW

APPLY = Capability.BLUEPRINTS_APPLY
DATABASES_WRITE = Capability.DATABASES_WRITE
ISSUER_ID = 1
TOKEN_PK = 55
PUBLIC_ID = "layer2PublicId0123456789"


def _issuer(base_role=GatewayRole.VIEWER, grants=(), capability_grants=()) -> Actor:
    return admin_actor(
        user_id=ISSUER_ID,
        username="issuer",
        role=base_role,
        grants=list(grants),
        capability_grants=list(capability_grants),
        step_up_until=OPEN_WINDOW,
    )


def _integration(issuer: Actor, *mapped_capabilities: Capability) -> Actor:
    return integration_actor(
        token_pk=TOKEN_PK,
        public_id=PUBLIC_ID,
        name="ci",
        capabilities=frozenset(mapped_capabilities),
        issuer=issuer,
    )


def _forbidden(callable_under_test) -> AppHttpException:
    with pytest.raises(AppHttpException) as raised:
        callable_under_test()
    assert raised.value.status_code == 403
    return raised.value


# --------------------------------------------------------------------------- #
# El actor de integración                                                     #
# --------------------------------------------------------------------------- #


def test_integration_actor_shape_follows_the_design():
    issuer = _issuer(
        GatewayRole.OPERATOR,
        grants=[("environment", 3, GatewayRole.OWNER)],
        capability_grants=[(APPLY, "environment", 3)],
    )
    actor = _integration(issuer, DATABASES_WRITE)

    assert actor.kind == "integration"
    # D5: id del EMISOR (la auditoría atribuye a la persona), token_id = parte pública del bearer.
    assert actor.id == issuer.id
    assert actor.token_id == PUBLIC_ID
    assert actor.token_pk == TOKEN_PK
    assert actor.issuer is issuer
    # Capa 1 estrictamente acotada a las capacidades mapeadas de los scopes: nada del emisor.
    assert actor.capabilities == frozenset({DATABASES_WRITE})
    assert actor.global_capabilities == frozenset()
    # La capa 2 sí necesita los datos de autorización del emisor para resolver el destino.
    assert actor.role == issuer.role
    assert actor.base_role == issuer.base_role
    assert actor.scope_roles == issuer.scope_roles
    assert actor.capability_grants == issuer.capability_grants


def test_integration_actor_never_gains_the_issuers_global_capabilities():
    from app.services.capability_catalog import GlobalCapability

    issuer = admin_actor(
        user_id=ISSUER_ID,
        username="issuer",
        role=GatewayRole.OWNER,
        globals_=frozenset({GlobalCapability.SECURITY_OFFICER}),
    )
    actor = _integration(issuer, Capability.SERVERS_READ)

    assert actor.global_capabilities == frozenset()
    assert not actor.has(Capability.SERVERS_ADMIN)
    assert not actor.has(Capability.AUDIT_READ)


def test_is_machine_covers_both_machine_kinds_and_is_agent_stays_mcp_only():
    human = _issuer()
    integration = _integration(human, Capability.SERVERS_READ)
    agent = token_actor(
        token_pk=3, token_id="agent", name="a", scopes="", project_id=1, issuer=human
    )

    assert human.is_machine is False
    assert integration.is_machine is True
    assert agent.is_machine is True
    # D7: `is_agent` gatea comportamiento propio del MCP y NO se ensancha.
    assert human.is_agent is False
    assert integration.is_agent is False
    assert agent.is_agent is True


# --------------------------------------------------------------------------- #
# capability_resolution: el conjunto "humano" es {admin, integration}         #
# --------------------------------------------------------------------------- #


def test_integration_actor_with_scope_roles_needs_target_resolution_like_a_human():
    issuer = _issuer(GatewayRole.VIEWER, grants=[("environment", 3, GatewayRole.OWNER)])
    actor = _integration(issuer, APPLY)

    assert capability_resolution.needs_target_resolution(issuer, APPLY) is True
    assert capability_resolution.needs_target_resolution(actor, APPLY) is True


def test_integration_actor_without_scope_roles_or_grants_skips_target_resolution():
    actor = _integration(_issuer(GatewayRole.OPERATOR), DATABASES_WRITE)

    assert capability_resolution.needs_target_resolution(actor, DATABASES_WRITE) is False


def test_integration_actor_with_a_relevant_capability_grant_needs_target_resolution():
    issuer = _issuer(GatewayRole.VIEWER, capability_grants=[(APPLY, "environment", 3)])
    actor = _integration(issuer, APPLY)

    assert capability_resolution.has_relevant_grant(actor, APPLY) is True
    assert capability_resolution.needs_target_resolution(actor, APPLY) is True


def test_grants_allow_honors_the_issuers_capability_grants_for_an_integration_actor():
    issuer = _issuer(GatewayRole.VIEWER, capability_grants=[(APPLY, "environment", 3)])
    actor = _integration(issuer, APPLY)

    assert capability_resolution.grants_allow(
        actor, APPLY, environment_id=3, server_id=None
    )
    assert not capability_resolution.grants_allow(
        actor, APPLY, environment_id=4, server_id=None
    )


def test_mcp_api_token_actor_stays_outside_the_human_set():
    """
    Regresión del lado MCP: un token de agente NO resuelve destino ni honra grants, aunque su
    emisor los tenga. Ensanchar el conjunto por error le daría capacidades puntuales a un agente.
    """
    issuer = _issuer(
        GatewayRole.VIEWER,
        grants=[("environment", 3, GatewayRole.OWNER)],
        capability_grants=[(APPLY, "environment", 3)],
    )
    agent = token_actor(
        token_pk=3,
        token_id="agent",
        name="a",
        scopes="databases.read",
        project_id=1,
        issuer=issuer,
    )

    assert capability_resolution.needs_target_resolution(agent, APPLY) is False
    assert capability_resolution.has_relevant_grant(agent, APPLY) is False
    assert (
        capability_resolution.grants_allow(agent, APPLY, environment_id=3, server_id=None)
        is False
    )


# --------------------------------------------------------------------------- #
# assert_layer2 contra el rol real del emisor en el destino                   #
# --------------------------------------------------------------------------- #


def test_layer2_for_integration_actor_follows_the_issuers_role_at_the_target(client):
    production, development = env_id("production"), env_id("development")
    production_database = sembrar_bd(server_id=1, environment_id=production)
    development_database = sembrar_bd(server_id=2, environment_id=development)
    # El emisor es viewer salvo en desarrollo, donde es owner.
    issuer = _issuer(GatewayRole.VIEWER, grants=[("environment", development, GatewayRole.OWNER)])
    actor = _integration(issuer, APPLY)

    assert_layer2(actor, APPLY, ScopeTarget(kind="database", params=(development_database,)))
    denial = _forbidden(
        lambda: assert_layer2(
            actor, APPLY, ScopeTarget(kind="database", params=(production_database,))
        )
    )
    # Mismo 403 opaco que el humano: no revela si faltó la capacidad o el alcance.
    assert denial.public_context["code"] == CODE_FORBIDDEN


def test_layer2_for_integration_actor_matches_the_human_issuers_verdict(client):
    production, development = env_id("production"), env_id("development")
    production_database = sembrar_bd(server_id=1, environment_id=production)
    development_database = sembrar_bd(server_id=2, environment_id=development)
    issuer = _issuer(GatewayRole.VIEWER, grants=[("environment", development, GatewayRole.OWNER)])
    actor = _integration(issuer, APPLY)

    for database_id in (production_database, development_database):
        target = ScopeTarget(kind="database", params=(database_id,))
        human_allowed = True
        try:
            assert_layer2(issuer, APPLY, target)
        except AppHttpException:
            human_allowed = False
        integration_allowed = True
        try:
            assert_layer2(actor, APPLY, target)
        except AppHttpException:
            integration_allowed = False
        assert integration_allowed == human_allowed


def test_layer2_for_integration_actor_honors_a_capability_grant_only_inside_its_scope(client):
    production, development = env_id("production"), env_id("development")
    production_database = sembrar_bd(server_id=1, environment_id=production)
    development_database = sembrar_bd(server_id=2, environment_id=development)
    issuer = _issuer(GatewayRole.VIEWER, capability_grants=[(APPLY, "environment", production)])
    actor = _integration(issuer, APPLY)

    assert_layer2(actor, APPLY, ScopeTarget(kind="database", params=(production_database,)))
    _forbidden(
        lambda: assert_layer2(
            actor, APPLY, ScopeTarget(kind="database", params=(development_database,))
        )
    )


def test_layer2_for_mcp_api_token_is_still_a_noop(client):
    """El token de agente no resuelve destino: su frontera es el `project_id`."""
    production = env_id("production")
    production_database = sembrar_bd(server_id=1, environment_id=production)
    agent = token_actor(
        token_pk=3,
        token_id="agent",
        name="a",
        scopes="databases.read",
        project_id=1,
        issuer=_issuer(GatewayRole.VIEWER),
    )

    assert_layer2(
        agent,
        Capability.DATABASES_READ,
        ScopeTarget(kind="database", params=(production_database,)),
    )


# --------------------------------------------------------------------------- #
# 1.9 Sitios de `is_agent` → `is_machine`                                     #
# --------------------------------------------------------------------------- #


def test_step_up_fails_closed_for_an_integration_actor_even_with_an_open_window():
    # `blueprints.apply` pide step-up. Una máquina no puede contestar un prompt de contraseña, y
    # el step-up del emisor se cumplió al crear/editar el token (D6), no en cada llamada.
    actor = _integration(_issuer(GatewayRole.OWNER), APPLY)

    denial = _forbidden(lambda: step_up.assert_step_up(actor, APPLY, method="POST"))
    assert denial.public_context["code"] == CODE_FORBIDDEN


def test_step_up_still_fails_closed_for_the_mcp_api_token():
    agent = token_actor(
        token_pk=3, token_id="agent", name="a", scopes="", project_id=1, issuer=_issuer()
    )

    denial = _forbidden(lambda: step_up.assert_step_up(agent, APPLY, method="POST"))
    assert denial.public_context["code"] == CODE_FORBIDDEN


def test_change_password_refuses_an_integration_actor(client):
    actor = _integration(_issuer(GatewayRole.OWNER), Capability.SERVERS_READ)

    # Sin esto, un bearer que alcanzara la ruta reescribiría la credencial del EMISOR: el actor
    # de integración lleva su `id`.
    denial = _forbidden(
        lambda: AuthController().change_password(
            actor, current_password="irrelevant", new_password="irrelevant", current_sid="sid"
        )
    )
    assert denial.public_context["code"] == CODE_SESSION_REQUIRED


def test_step_up_endpoint_refuses_an_integration_actor(client):
    actor = _integration(_issuer(GatewayRole.OWNER), Capability.SERVERS_READ)

    denial = _forbidden(lambda: AuthController().step_up(actor, password="irrelevant", sid="sid"))
    assert denial.public_context["code"] == CODE_SESSION_REQUIRED


def test_data_access_identity_refuses_an_integration_actor():
    # `_data_actor_id` compara ids para el control del segundo aprobador: un actor de integración
    # con el `id` de su emisor se leería como el humano que abre/aprueba el acceso a datos.
    actor = _integration(_issuer(GatewayRole.OWNER), Capability.SERVERS_READ)

    denial = _forbidden(lambda: ManagedDatabaseController._data_actor_id(actor))
    assert (
        denial.public_context["code"]
        == data_credential_codes.CODE_DATA_ACCESS_IDENTITY_REQUIRED
    )


def test_data_access_identity_still_accepts_a_human_and_refuses_the_mcp_token():
    human = _issuer(GatewayRole.OWNER)
    agent = token_actor(
        token_pk=3, token_id="agent", name="a", scopes="", project_id=1, issuer=human
    )

    assert ManagedDatabaseController._data_actor_id(human) == ISSUER_ID
    _forbidden(lambda: ManagedDatabaseController._data_actor_id(agent))


def test_token_issuance_step_up_helper_refuses_an_integration_actor():
    from app.controllers.api_token_controller import _require_issuer_step_up

    actor = _integration(_issuer(GatewayRole.OWNER), APPLY)

    denial = _forbidden(lambda: _require_issuer_step_up(actor, Capability.DATA_READ))
    assert denial.public_context["code"] == CODE_FORBIDDEN
