"""
Tests de PROPIEDAD sobre el catálogo de capacidades.

POR QUÉ DE PROPIEDAD Y NO UNA MATRIZ rol × endpoint
---------------------------------------------------
Con 156 rutas montadas y tres roles, enumerar la matriz son cientos de tests que hay que
mantener a mano y que envejecen con cada endpoint nuevo. Estos tests recorren el CATÁLOGO, así
que escalan con el vocabulario (28 capacidades) y no con la superficie — y corren en
milisegundos, sin `TestClient` ni motor.

Los invariantes de ``capability_catalog`` se afirman **al importar** el módulo, no acá:
fallar al importar es fallar al arrancar, que es lo correcto para un catálogo de autorización.
Estos tests cubren lo que un invariante de import no puede: que las afirmaciones sigan siendo
ciertas si alguien las relaja, y el comportamiento de las funciones.

OJO CON EL NOMBRE: esto es el plano de CONTROL (capacidades del gateway). Los privilegios del
MOTOR viven en ``privilege_catalog`` y sus tests son otros.
"""

from types import MappingProxyType

import pytest

from app.core.actor import admin_actor, token_actor
from app.services import capability_catalog as cc
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    CAPABILITIES,
    GLOBAL_CAPABILITIES,
    ROLE_CAPABILITIES,
    Capability,
    GatewayRole,
    GlobalCapability,
)

# --------------------------------------------------------------------------- #
# Forma del catálogo                                                          #
# --------------------------------------------------------------------------- #


def test_every_capability_has_exactly_one_spec():
    assert len(CAPABILITIES) == len(Capability)
    assert {s.id for s in CAPABILITIES} == set(Capability)


@pytest.mark.parametrize("cap", list(Capability), ids=lambda c: c.value)
def test_capability_id_matches_module_and_level(cap):
    """El valor se declara explícito; derivarlo del nombre del miembro rompe (``engine_users``)."""
    s = cc.spec(cap)
    assert cap.value == f"{s.module}.{s.level}"


# --------------------------------------------------------------------------- #
# Monotonía — es lo que hace bien definido el `max` sobre alcances             #
# --------------------------------------------------------------------------- #


def test_roles_are_monotonic():
    """
    ``viewer ⊆ operator ⊆ owner``.

    Sin esto, "rol máximo" no significa nada y ``union_role`` deja de estar definido — y con
    él toda la resolución de alcance del actor.
    """
    assert ROLE_CAPABILITIES[GatewayRole.VIEWER] <= ROLE_CAPABILITIES[GatewayRole.OPERATOR]
    assert ROLE_CAPABILITIES[GatewayRole.OPERATOR] <= ROLE_CAPABILITIES[GatewayRole.OWNER]


def test_every_role_is_a_subset_of_owner():
    """Evita drift: un rol no puede tener una capacidad que el tope de la cadena no tenga."""
    for role, caps in ROLE_CAPABILITIES.items():
        assert caps <= ROLE_CAPABILITIES[GatewayRole.OWNER], role.value


# --------------------------------------------------------------------------- #
# Los dos ejes: mutar y divulgar                                              #
# --------------------------------------------------------------------------- #


def test_viewer_grants_nothing_that_mutates_or_discloses():
    """
    Las dos cosas, no una.

    Un modelo partido en destructivo/no-destructivo pierde el eje de divulgación entero:
    ``reveal-password``, la descarga de un export y las capturas de ``SELECT`` **no destruyen
    nada**, así que se colarían en un rol de lectura.
    """
    for cap in ROLE_CAPABILITIES[GatewayRole.VIEWER]:
        s = cc.spec(cap)
        assert not s.mutates, f"{cap.value} muta y está en viewer"
        assert not s.discloses, f"{cap.value} divulga y está en viewer"


def test_every_disclosing_capability_requires_step_up():
    """Un factor fresco antes de que un dato del cliente salga del perímetro."""
    for s in CAPABILITIES:
        if s.discloses:
            assert s.requires_step_up, f"{s.id.value} divulga sin step-up"


def test_the_disclosing_capabilities_are_the_expected_ones():
    """
    Congelado a propósito: si aparece una capacidad que divulga y no está acá, es una decisión
    que alguien tiene que tomar explícitamente, no heredar de un default.
    """
    assert {s.id for s in CAPABILITIES if s.discloses} == {
        Capability.ENGINE_USERS_SECRETS,
        Capability.ENGINE_USERS_CREDENTIALS,
        Capability.BLUEPRINTS_CAPTURES,
        Capability.CLONES_EXECUTE,
        Capability.EXPORTS_DOWNLOAD,
        Capability.SQL_CONSOLE_EXECUTE,
    }


def test_disclosing_capabilities_are_never_implied_by_a_mutating_one():
    """
    Las que divulgan NO participan del orden acumulativo del módulo.

    ``engine_users.secrets`` no puede venir con ``engine_users.write`` (leer una credencial y
    reescribir privilegios del motor son riesgos incomparables), y ``exports.download`` no
    puede venir con ``exports.execute`` (quien solo baja un artefacto no necesita generarlos).
    Se verifica en ``operator``, que es el rol donde el arrastre aparecería.
    """
    operator = ROLE_CAPABILITIES[GatewayRole.OPERATOR]
    for s in CAPABILITIES:
        if s.discloses:
            assert s.id not in operator, f"{s.id.value} divulga y llegó a operator por arrastre"


def test_import_invariants_reject_a_disclosing_capability_in_operator(monkeypatch):
    """
    El test de arriba solo muerde si alguien corre la suite; el invariante 7b muerde al
    importar. Esto prueba que el invariante existe y no se relajó en silencio.
    """
    roles = dict(ROLE_CAPABILITIES)
    roles[GatewayRole.OPERATOR] = roles[GatewayRole.OPERATOR] | {Capability.ENGINE_USERS_SECRETS}
    monkeypatch.setattr(cc, "ROLE_CAPABILITIES", MappingProxyType(roles))
    with pytest.raises(AssertionError, match="operator"):
        cc._assert_invariants()


# --------------------------------------------------------------------------- #
# Destructivas: subconjunto de `mutates`, solo owner, con step-up             #
# --------------------------------------------------------------------------- #


def test_the_destructive_capabilities_are_the_expected_ones():
    """
    Congelado a propósito, como las que divulgan. ``collation.execute`` entró al pasar a
    ``owner``: ``ALTER TABLE ... CONVERT`` reescribe la tabla y es irreversible.
    """
    assert {s.id for s in CAPABILITIES if s.destructive} == {
        Capability.COLLATION_EXECUTE,
        Capability.DATABASES_DROP,
        Capability.ENGINE_USERS_DROP,
        Capability.BLUEPRINTS_APPLY,
        Capability.SCHEMA_DIFF_EXECUTE,
        Capability.CLONES_EXECUTE,
        Capability.SQL_CONSOLE_EXECUTE,
    }


def test_destructive_capabilities_are_owner_only_and_require_step_up():
    for s in CAPABILITIES:
        if s.destructive:
            assert s.mutates, s.id.value
            assert s.requires_step_up, s.id.value
            assert s.id in ROLE_CAPABILITIES[GatewayRole.OWNER], s.id.value
            assert s.id not in ROLE_CAPABILITIES[GatewayRole.OPERATOR], s.id.value
            assert s.id not in ROLE_CAPABILITIES[GatewayRole.VIEWER], s.id.value


def test_import_invariants_reject_a_destructive_capability_in_operator(monkeypatch):
    roles = dict(ROLE_CAPABILITIES)
    roles[GatewayRole.OPERATOR] = roles[GatewayRole.OPERATOR] | {Capability.DATABASES_DROP}
    monkeypatch.setattr(cc, "ROLE_CAPABILITIES", MappingProxyType(roles))
    with pytest.raises(AssertionError, match="destructive"):
        cc._assert_invariants()


# --------------------------------------------------------------------------- #
# Capacidades globales y ortogonales                                          #
# --------------------------------------------------------------------------- #


def test_global_capabilities_are_not_in_the_role_chain():
    """
    Si ``access.admin`` cayera en ``owner``, el rol operativo podría apagar
    ``blocks_destructive_migrations`` — que es exactamente el agujero que la separación entre
    operar y otorgar existe para cerrar.
    """
    globales = set().union(*GLOBAL_CAPABILITIES.values())
    for role, caps in ROLE_CAPABILITIES.items():
        assert not (caps & globales), f"{role.value} tiene capacidades globales"


def test_access_admin_is_not_operational():
    """
    ``access_admin`` administra el acceso y NADA operativo: no aplica migraciones, no dropea,
    no revela contraseñas. Es la mitad de la separación de deberes.
    """
    caps = GLOBAL_CAPABILITIES[GlobalCapability.ACCESS_ADMIN]
    for prohibida in (
        Capability.BLUEPRINTS_APPLY,
        Capability.DATABASES_DROP,
        Capability.ENGINE_USERS_SECRETS,
        Capability.SQL_CONSOLE_EXECUTE,
    ):
        assert prohibida not in caps


# --------------------------------------------------------------------------- #
# Separación de deberes: `gateway.admin` partida en `access.admin` + `policy.admin`  #
# --------------------------------------------------------------------------- #


def test_the_global_sets_are_pinned():
    """
    Los dos conjuntos, fijados a mano. Un cambio acá es un cambio de POLÍTICA (quién administra
    qué) y tiene que verse en el diff del test, no solo en el del catálogo.
    """
    assert GLOBAL_CAPABILITIES[GlobalCapability.ACCESS_ADMIN] == frozenset(
        {Capability.ACCESS_ADMIN_CAP}
    )
    assert GLOBAL_CAPABILITIES[GlobalCapability.SECURITY_OFFICER] == frozenset(
        {
            Capability.POLICY_ADMIN,
            Capability.SERVERS_ADMIN,
            Capability.CATALOGS_WRITE,
            Capability.ENVIRONMENTS_WRITE,
        }
    )


def test_global_sets_are_pairwise_disjoint():
    """
    Invariante 9. Con ``gateway.admin`` en las dos globales, un ``security_officer`` sin
    ``access_admin`` administraba usuarios igual: la separación de deberes estaba en el papel.
    """
    vistas: set[Capability] = set()
    for caps in GLOBAL_CAPABILITIES.values():
        assert not (caps & vistas), sorted(c.value for c in caps & vistas)
        vistas |= caps


@pytest.mark.parametrize("cap", [Capability.ACCESS_ADMIN_CAP, Capability.POLICY_ADMIN])
def test_the_split_capabilities_are_global_mutating_step_up_and_never_agent(cap):
    s = cc.spec(cap)
    assert (s.mutates, s.requires_step_up, s.scope_axis) == (True, True, "global")
    assert not s.discloses and not s.agent_allowed and not s.destructive
    assert not cc.is_grantable(cap), "una global otorgable saltearía la separación"
    assert cap not in AGENT_ALLOWED


def test_gateway_admin_is_retired():
    """
    Invariante 12. ``gateway.admin`` no existe en el enum, no se parsea de ningún lado y está en
    la lista de retiradas que también lee ``scripts/check_route_capabilities.py``.
    """
    assert "gateway.admin" not in {c.value for c in Capability}
    assert "gateway.admin" in cc.RETIRED_CAPABILITIES
    assert not cc.is_grantable("gateway.admin")
    assert cc.parse_scopes("gateway.admin") == frozenset()
    with pytest.raises(ValueError):
        Capability("gateway.admin")


def test_a_security_officer_viewer_does_not_administer_access():
    """El efecto buscado de la partición: ``security_officer`` solo ya no administra usuarios."""
    actor = admin_actor(
        user_id=1,
        username="ana",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.SECURITY_OFFICER}),
    )
    assert actor.has(Capability.POLICY_ADMIN)
    assert not actor.has(Capability.ACCESS_ADMIN_CAP)


def test_an_access_admin_viewer_does_not_rotate_crypto():
    actor = admin_actor(
        user_id=1,
        username="ana",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.ACCESS_ADMIN}),
    )
    assert actor.has(Capability.ACCESS_ADMIN_CAP)
    assert not actor.has(Capability.POLICY_ADMIN)
    assert actor.capabilities == (
        ROLE_CAPABILITIES[GatewayRole.VIEWER] | {Capability.ACCESS_ADMIN_CAP}
    )


def test_an_access_admin_viewer_holds_nothing_beyond_viewer_and_its_globals():
    """
    El ACTOR efectivo, no solo ``GLOBAL_CAPABILITIES[ACCESS_ADMIN]``: un actor es rol base ∪
    grants ∪ globales. Un ``viewer`` con ``access_admin`` no puede terminar con ninguna
    capacidad operativa por el camino del acuñado (implícitas, unión de roles).
    """
    actor = admin_actor(
        user_id=1,
        username="ana",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.ACCESS_ADMIN}),
    )
    permitido = (
        ROLE_CAPABILITIES[GatewayRole.VIEWER]
        | GLOBAL_CAPABILITIES[GlobalCapability.ACCESS_ADMIN]
    )
    assert actor.capabilities <= permitido, sorted(c.value for c in actor.capabilities - permitido)
    for cap in actor.capabilities:
        s = cc.spec(cap)
        assert not s.destructive and not s.discloses, cap.value


def test_servers_admin_belongs_only_to_security_officer():
    """
    Editar un servidor puede RE-APUNTAR un ``server_id`` a un host que el editor controla, y
    desde ahí toda operación futura de todo operador con alcance corre contra su máquina. El
    guard anti-SSRF limita a dónde, no a quién.
    """
    assert Capability.SERVERS_ADMIN in GLOBAL_CAPABILITIES[GlobalCapability.SECURITY_OFFICER]
    for caps in ROLE_CAPABILITIES.values():
        assert Capability.SERVERS_ADMIN not in caps


def test_environments_write_belongs_only_to_security_officer():
    """
    Escribir entornos, abrir BDs a agentes y reclasificarlas es dato de política. ``access_admin``
    conserva LEER (``environments.read`` viene de ``viewer``) pero no escribir, y ningún rol
    operativo lo tiene.
    """
    spec = cc.spec(Capability.ENVIRONMENTS_WRITE)
    assert (spec.mutates, spec.requires_step_up, spec.scope_axis) == (True, True, "global")
    assert not spec.discloses and not spec.agent_allowed
    assert Capability.ENVIRONMENTS_WRITE in GLOBAL_CAPABILITIES[GlobalCapability.SECURITY_OFFICER]
    assert Capability.ENVIRONMENTS_WRITE not in GLOBAL_CAPABILITIES[GlobalCapability.ACCESS_ADMIN]
    for caps in ROLE_CAPABILITIES.values():
        assert Capability.ENVIRONMENTS_WRITE not in caps
        assert Capability.ENVIRONMENTS_READ in caps
    assert "entornos" not in cc.spec(Capability.ACCESS_ADMIN_CAP).label
    assert "entornos" not in cc.spec(Capability.POLICY_ADMIN).label


# --------------------------------------------------------------------------- #
# Techo de agente (plan 12)                                                   #
# --------------------------------------------------------------------------- #


def test_agent_allowed_never_mutates_nor_discloses():
    for s in CAPABILITIES:
        if s.agent_allowed:
            assert not s.mutates and not s.discloses, s.id.value


def test_parse_scopes_intersects_with_the_agent_ceiling():
    """
    Una fila de ``api_tokens`` manipulada o legada NO puede otorgar fuera del techo, incluso
    si el string lo dice. Fail-closed en el lector, no solo en el escritor.
    """
    caps = cc.parse_scopes("databases.read,databases.drop,access.admin,gateway.admin")
    assert caps == frozenset({Capability.DATABASES_READ})


def test_parse_scopes_ignores_garbage_without_raising():
    assert cc.parse_scopes("no.existe,,  ,databases.read") == frozenset(
        {Capability.DATABASES_READ}
    )
    assert cc.parse_scopes("") == frozenset()


# --------------------------------------------------------------------------- #
# Resolución de rol                                                           #
# --------------------------------------------------------------------------- #


def test_union_role_takes_the_maximum_not_the_minimum():
    """
    "Operador en desarrollo, lector en producción" tiene que poder operar en desarrollo. Con
    el mínimo, tener un alcance restringido degradaría también el trabajo donde sí puede.
    """
    assert (
        cc.union_role(GatewayRole.VIEWER, {1: GatewayRole.OPERATOR}) is GatewayRole.OPERATOR
    )
    assert cc.union_role(GatewayRole.OWNER, {1: GatewayRole.VIEWER}) is GatewayRole.OWNER
    assert cc.union_role(GatewayRole.VIEWER, {}) is GatewayRole.VIEWER


def test_unknown_role_resolves_to_the_empty_set_not_an_exception():
    """
    Fail-closed en el lector. Una fila con un rol que el código no conoce (rollback de un
    deploy, ``UPDATE`` manual, dato legado) no puede tumbar el camino de autenticación ni
    resolver a un default permisivo.
    """
    assert cc.role_capabilities("auditor") == frozenset()


# --------------------------------------------------------------------------- #
# Publicado == hecho cumplir                                                  #
# --------------------------------------------------------------------------- #


def test_the_matrix_publishes_exactly_what_the_roles_grant():
    """
    ``capability_matrix()`` se DERIVA de ``ROLE_CAPABILITIES``; no es una segunda lista que
    mantener sincronizada. Publicar una promesa que el servidor no cumple es peor que no
    publicarla.
    """
    matrix = {row["id"]: set(row["roles"]) for row in cc.capability_matrix()}
    for role, caps in ROLE_CAPABILITIES.items():
        for cap in caps:
            assert role.value in matrix[cap.value], f"{cap.value} no publica el rol {role.value}"
    for row in cc.capability_matrix():
        for role_name in row["roles"]:
            assert Capability(row["id"]) in ROLE_CAPABILITIES[GatewayRole(role_name)]


def test_the_matrix_covers_every_capability():
    assert {row["id"] for row in cc.capability_matrix()} == {c.value for c in Capability}


# --------------------------------------------------------------------------- #
# Actor                                                                       #
# --------------------------------------------------------------------------- #


def test_actor_is_not_subscriptable_so_forgotten_sites_explode():
    """
    Los 227 sitios que hoy pasan el dict ``admin`` se migran a mano. Con un dict, un
    ``actor.get("id")`` mal escrito devuelve ``None`` y ``audit_log`` queda con ``admin_id``
    nulo — un agujero de auditabilidad silencioso. Sin ``__getitem__`` ni ``.get()``, el olvido
    es ruidoso.
    """
    actor = admin_actor(user_id=1, username="admin", role=GatewayRole.OWNER)
    with pytest.raises(TypeError):
        actor["id"]
    assert not hasattr(actor, "get")


def test_actor_is_immutable():
    actor = admin_actor(user_id=1, username="admin", role=GatewayRole.VIEWER)
    with pytest.raises(Exception):
        actor.capabilities = frozenset()


def test_admin_actor_capabilities_come_from_the_union_role():
    actor = admin_actor(
        user_id=1,
        username="ana",
        role=GatewayRole.VIEWER,
        grants=[("environment", 3, GatewayRole.OPERATOR)],
    )
    assert actor.role is GatewayRole.OPERATOR
    assert actor.capabilities == ROLE_CAPABILITIES[GatewayRole.OPERATOR]


def test_admin_actor_keeps_the_scope_type_of_each_grant():
    """
    El ``scope_type`` tiene que SOBREVIVIR al acuñado del actor. La versión anterior recibía un
    dict ``{scope_id: role}`` y lo perdía, así que un grant de servidor se leía como uno de
    entorno — y el entorno 3 y el servidor 3 son cosas distintas. Para la unión daba igual
    (solo mira los roles), pero la capa 2 resuelve por destino y ahí el tipo ES la pregunta.
    """
    actor = admin_actor(
        user_id=1,
        username="ana",
        role=GatewayRole.VIEWER,
        grants=[
            ("environment", 3, GatewayRole.OPERATOR),
            ("server", 3, GatewayRole.OWNER),
        ],
    )
    assert ("environment", 3, GatewayRole.OPERATOR) in actor.scope_roles
    assert ("server", 3, GatewayRole.OWNER) in actor.scope_roles
    assert len(actor.scope_roles) == 2, "los dos alcances colapsaron en uno"


def test_global_capabilities_add_on_top_of_the_role():
    actor = admin_actor(
        user_id=1,
        username="ana",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.SECURITY_OFFICER}),
    )
    assert actor.has(Capability.SERVERS_ADMIN)
    assert actor.has(Capability.CATALOGS_WRITE)
    # Y sigue sin poder operar: la global no arrastra la cadena.
    assert not actor.has(Capability.DATABASES_DROP)


def test_token_actor_is_capped_at_the_agent_ceiling():
    actor = token_actor(
        token_pk=1,
        token_id="tok",
        name="ci-facturacion",
        scopes=",".join(c.value for c in Capability),
        project_id=7,
    )
    assert actor.capabilities == AGENT_ALLOWED
    assert actor.is_agent
    assert not actor.has(Capability.DATABASES_DROP)
