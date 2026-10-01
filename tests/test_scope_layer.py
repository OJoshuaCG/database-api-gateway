"""
Capa 2: el rol EN ESTE destino, y las tres decisiones que la hacen funcionar o no.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
La capa 1 usa el rol **UNIÓN** —el máximo sobre el rol base y los grants— así que responde
"¿podría, en algún alcance?" y es **más laxa que la política real**. Eso solo es aceptable si la
capa 2 no es salteable, y por lo tanto lo que hay que verificar acá no es "el guard existe", es:

1. **El grant REEMPLAZA al rol base en su alcance, no se suma.** Con ``base=operator`` y un grant
   ``viewer`` sobre producción, un ``max`` daría ``operator`` y la restricción **no haría nada**.
   Es el error que habría hecho decorativo todo el eje.
2. **``NULL`` no es "permitido".** Una BD sin entorno resuelve al más protegido, no al default
   —que el propio modelo declara como el más permisivo—.
3. **Dos grants que se superponen resuelven al más restrictivo.** Si fuera al revés, agregar un
   grant podría *ampliar* el acceso sin que nadie lo pida.
"""

import pytest
from sqlalchemy import text

from app.core.actor import admin_actor
from app.core.database import Database
from app.core.scope import (
    ScopePoint,
    ScopeTarget,
    assert_at,
    assert_layer2,
    assert_scope,
    effective_role_at,
    most_protected_environment_id,
    partition_by_scope,
    resolve_environment_id,
    role_at_point,
)
from app.services.capability_catalog import Capability, GatewayRole
from tests.scope_helpers import actor_con, env_id, otorgar, sembrar_bd


_env_id = env_id
_sembrar_bd = sembrar_bd
_actor = actor_con
_otorgar = otorgar


# --------------------------------------------------------------------------- #
# 1. El grant reemplaza, no se suma                                           #
# --------------------------------------------------------------------------- #


def test_a_restrictive_grant_wins_over_the_base_role(client):
    """
    **El test central.** "Operador en general, lector en producción" tiene que RESTRINGIR en
    producción. Con un ``max`` sobre base y grant, el rol base ganaría y el grant sería adorno.
    """
    prod = _env_id("production")
    bd = _sembrar_bd(environment_id=prod)
    actor = _actor(GatewayRole.OPERATOR, [("environment", prod, GatewayRole.VIEWER)])

    assert effective_role_at(actor, server_id=1, managed_database_id=bd) == GatewayRole.VIEWER


def test_the_same_actor_keeps_the_base_role_elsewhere(client):
    """
    La otra mitad, y el motivo por el que la capa 1 usa el máximo: la restricción en producción
    **no** puede degradar el trabajo en desarrollo.
    """
    prod, dev = _env_id("production"), _env_id("development")
    bd_dev = _sembrar_bd(environment_id=dev)
    actor = _actor(GatewayRole.OPERATOR, [("environment", prod, GatewayRole.VIEWER)])

    assert effective_role_at(actor, server_id=1, managed_database_id=bd_dev) == GatewayRole.OPERATOR


def test_an_elevating_grant_also_applies(client):
    """El grant no es solo para restringir: eleva en su alcance igual de bien."""
    dev = _env_id("development")
    bd = _sembrar_bd(environment_id=dev)
    actor = _actor(GatewayRole.VIEWER, [("environment", dev, GatewayRole.OWNER)])

    assert effective_role_at(actor, server_id=1, managed_database_id=bd) == GatewayRole.OWNER


def test_an_elevating_grant_does_not_leak_outside_its_scope(client):
    """
    REGRESIÓN F-14: sin grant aplicable, ``effective_role_at`` caía a ``actor.role``, que es el
    rol UNIÓN. Un ``viewer`` con ``owner`` en desarrollo resolvía ``owner`` también en
    producción. Tiene que resolver su rol BASE.
    """
    prod, dev = _env_id("production"), _env_id("development")
    bd_prod = _sembrar_bd(environment_id=prod)
    actor = _actor(GatewayRole.VIEWER, [("environment", dev, GatewayRole.OWNER)])

    # La capa 1 sigue usando la unión ("¿podría en algún alcance?")…
    assert actor.role == GatewayRole.OWNER
    assert actor.base_role == GatewayRole.VIEWER
    # …pero la capa 2, fuera del alcance del grant, manda el rol base.
    assert effective_role_at(actor, server_id=1, managed_database_id=bd_prod) == GatewayRole.VIEWER


def test_an_elevating_grant_does_not_reach_unclassified_databases(client):
    """Una BD sin entorno resuelve al más protegido, que no es desarrollo: rige el base."""
    dev = _env_id("development")
    bd = _sembrar_bd(environment_id=None)
    actor = _actor(GatewayRole.VIEWER, [("environment", dev, GatewayRole.OWNER)])

    assert effective_role_at(actor, server_id=1, managed_database_id=bd) == GatewayRole.VIEWER


@pytest.mark.parametrize(
    "capability", [Capability.DATABASES_DROP, Capability.BLUEPRINTS_APPLY]
)
def test_an_elevating_grant_is_denied_in_production_and_allowed_in_its_scope(client, capability):
    from app.exceptions import AppHttpException

    prod, dev = _env_id("production"), _env_id("development")
    bd_prod = _sembrar_bd(server_id=1, environment_id=prod)
    bd_dev = _sembrar_bd(server_id=2, environment_id=dev)
    actor = _actor(GatewayRole.VIEWER, [("environment", dev, GatewayRole.OWNER)])

    with pytest.raises(AppHttpException) as exc:
        assert_scope(actor, capability, server_id=1, managed_database_id=bd_prod)
    assert exc.value.status_code == 403
    assert exc.value.public_context["code"] == "access.forbidden"

    # En su alcance, el grant sí eleva.
    assert_scope(actor, capability, server_id=2, managed_database_id=bd_dev)


def test_auth_me_exposes_the_base_role(admin_client):
    """``role`` conserva su significado (unión); ``base_role`` es el que rige sin grant."""
    _otorgar("environment", _env_id("development"), "viewer")
    data = admin_client.get("/api/v1/auth/me").json()["data"]
    assert data["role"] == "owner"
    assert data["base_role"] == "owner"


# --------------------------------------------------------------------------- #
# 2. NULL no es "permitido"                                                   #
# --------------------------------------------------------------------------- #


def test_an_unclassified_database_resolves_to_the_most_protected(client):
    """
    Una BD sin entorno **no** resuelve al ``is_default``: el propio modelo declara que el default
    es el más permisivo, así que usarlo acá convertiría un hueco de datos en un permiso.
    """
    bd = _sembrar_bd(environment_id=None)
    assert resolve_environment_id(server_id=1, managed_database_id=bd) == most_protected_environment_id()


def test_the_default_environment_is_not_the_fallback(client):
    """
    Lo que hace falta afirmar aparte: que el entorno más protegido **no sea** el default. Si un
    día alguien iguala los dos, el test de arriba pasaría sin decir nada.
    """
    with Database().engine.begin() as conn:
        default = conn.execute(
            text("SELECT id FROM environments WHERE is_default = 1")
        ).fetchone()
    assert default, "no hay entorno por defecto sembrado"
    assert most_protected_environment_id() != default[0]


def test_a_restrictive_grant_reaches_unclassified_databases(client):
    """
    La consecuencia operativa que hay que ver antes de otorgar, y que el reporte de
    ``/authz/scope-readiness`` existe para anticipar: un "lector en producción" también queda
    lector sobre toda base **sin clasificar**.
    """
    prod = _env_id("production")
    bd = _sembrar_bd(environment_id=None)
    actor = _actor(GatewayRole.OPERATOR, [("environment", prod, GatewayRole.VIEWER)])

    assert effective_role_at(actor, server_id=1, managed_database_id=bd) == GatewayRole.VIEWER


def test_a_server_with_any_unclassified_database_derives_to_the_most_protected(client):
    """
    Regla del §4.3 para los destinos a nivel SERVIDOR: el ``rank`` máximo entre sus bases,
    tratando cualquier ``NULL`` como el máximo global. Un servidor mixto cae en el más
    protegido, no en el de sus bases clasificadas.
    """
    dev = _env_id("development")
    _sembrar_bd(server_id=1, environment_id=dev)
    _sembrar_bd(server_id=1, environment_id=None)

    assert resolve_environment_id(server_id=1, managed_database_id=None) == most_protected_environment_id()


def test_a_server_with_no_databases_derives_to_the_most_protected(client):
    """Caso límite de la MISMA regla, no una cláusula aparte."""
    assert resolve_environment_id(server_id=999, managed_database_id=None) == most_protected_environment_id()


# --------------------------------------------------------------------------- #
# 3. Dos grants que se superponen                                             #
# --------------------------------------------------------------------------- #


def test_two_applicable_grants_resolve_to_the_most_restrictive(client):
    """
    Fail-closed por elección: si resolviera a la más permisiva, **agregar** un grant podría
    ampliar el acceso sin que nadie lo pida.
    """
    dev = _env_id("development")
    bd = _sembrar_bd(server_id=1, environment_id=dev)
    actor = _actor(
        GatewayRole.VIEWER,
        [("environment", dev, GatewayRole.OWNER), ("server", 1, GatewayRole.VIEWER)],
    )

    assert effective_role_at(actor, server_id=1, managed_database_id=bd) == GatewayRole.VIEWER


def test_a_server_grant_does_not_apply_to_another_server(client):
    dev = _env_id("development")
    bd = _sembrar_bd(server_id=2, environment_id=dev)
    actor = _actor(GatewayRole.OPERATOR, [("server", 1, GatewayRole.VIEWER)])

    assert effective_role_at(actor, server_id=2, managed_database_id=bd) == GatewayRole.OPERATOR


def test_an_environment_id_does_not_collide_with_a_server_id(client):
    """
    El defecto que había en el lector: devolvía ``{scope_id: role}`` y **perdía el
    ``scope_type``**, así que un grant de servidor se leía como uno de entorno. El entorno 3 y
    el servidor 3 son cosas distintas.
    """
    dev = _env_id("development")
    bd = _sembrar_bd(server_id=dev, environment_id=dev)
    # Grant de ENTORNO con el mismo número que el server_id del destino.
    actor = _actor(GatewayRole.OPERATOR, [("environment", dev, GatewayRole.VIEWER)])
    assert effective_role_at(actor, server_id=dev, managed_database_id=bd) == GatewayRole.VIEWER

    # Y el simétrico: grant de SERVIDOR con el número del entorno del destino.
    otro = _actor(GatewayRole.OPERATOR, [("server", 9999, GatewayRole.VIEWER)])
    assert effective_role_at(otro, server_id=dev, managed_database_id=bd) == GatewayRole.OPERATOR


# --------------------------------------------------------------------------- #
# El camino rápido, y que no sea salteable                                    #
# --------------------------------------------------------------------------- #


def test_an_actor_without_grants_skips_the_database(client, monkeypatch):
    """
    Camino rápido: sin ningún grant —el caso de todo despliegue hasta que alguien otorgue el
    primero— se devuelve el rol base **sin tocar la BD**. Importa porque esto corre en cada
    operación con destino.
    """
    import app.core.scope as scope_mod

    def explotar(**kwargs):
        raise AssertionError("se resolvió el entorno sin necesidad")

    monkeypatch.setattr(scope_mod, "resolve_environment_id", explotar)
    actor = _actor(GatewayRole.OWNER)
    assert effective_role_at(actor, server_id=1, managed_database_id=1) == GatewayRole.OWNER


def test_the_destination_is_a_mandatory_keyword():
    """
    Lo que hace que la capa 2 no sea salteable: ``assert_scope`` **no se puede llamar sin
    declarar el destino**. Un sitio que se olvide falla con ``TypeError`` en la suite, no en
    silencio. Pasar ``None`` explícito es una declaración, no un olvido.
    """
    actor = _actor(GatewayRole.OWNER)
    with pytest.raises(TypeError):
        assert_scope(actor, Capability.DATABASES_DROP)  # type: ignore[call-arg]


def test_global_capabilities_are_not_narrowed_by_destination(client):
    """
    ``access_admin`` y ``security_officer`` son ORTOGONALES a la cadena de roles, así que lo que
    otorgan no se recorta por destino. Si se recortaran, un grant restrictivo sobre producción
    dejaría a quien administra accesos sin poder administrarlos.
    """
    from app.services.capability_catalog import GlobalCapability

    prod = _env_id("production")
    bd = _sembrar_bd(environment_id=prod)
    actor = admin_actor(
        user_id=1,
        username="admin",
        role=GatewayRole.VIEWER,
        grants=[("environment", prod, GatewayRole.VIEWER)],
        globals_=frozenset({GlobalCapability.SECURITY_OFFICER}),
    )
    # No levanta: la capacidad viene de la global, no del rol del destino.
    assert_scope(actor, Capability.CATALOGS_WRITE, server_id=1, managed_database_id=bd)


def test_a_restricted_actor_is_denied_at_the_destination(client):
    prod = _env_id("production")
    bd = _sembrar_bd(environment_id=prod)
    actor = _actor(GatewayRole.OWNER, [("environment", prod, GatewayRole.VIEWER)])

    from app.exceptions import AppHttpException

    with pytest.raises(AppHttpException) as exc:
        assert_scope(actor, Capability.DATABASES_DROP, server_id=1, managed_database_id=bd)
    assert exc.value.status_code == 403
    assert exc.value.public_context["code"] == "access.forbidden"


# --------------------------------------------------------------------------- #
# Que la capa 2 CORRA de verdad en las rutas                                 #
# --------------------------------------------------------------------------- #


def test_the_route_denies_a_drop_in_a_restricted_environment(admin_client, server_payload):
    """
    Extremo a extremo, y es lo que prueba que el guard **está aplicado** y no solo escrito: el
    admin sembrado es ``owner`` con las dos globales, o sea que la capa 1 lo deja pasar. Lo que
    lo frena es la capa 2.
    """
    srv = admin_client.post("/api/v1/servers", json=server_payload())
    assert srv.status_code == 201, srv.text
    server_id = srv.json()["data"]["id"]

    prod = _env_id("production")
    bd = _sembrar_bd(server_id=server_id, environment_id=prod)
    _otorgar("environment", prod, "viewer")

    r = admin_client.delete(
        f"/api/v1/managed-databases/{bd}?drop_remote=true&confirm_name=x"
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"


def test_the_same_route_passes_in_an_unrestricted_environment(admin_client, server_payload):
    """
    La contracara: el mismo actor, con el mismo grant, sobre una base de desarrollo. Si esto
    diera 403, el guard estaría negando por alcance donde no corresponde — que es la forma en
    que un modelo de alcance se vuelve inusable y alguien lo apaga.
    """
    srv = admin_client.post("/api/v1/servers", json=server_payload())
    assert srv.status_code == 201, srv.text
    server_id = srv.json()["data"]["id"]

    prod, dev = _env_id("production"), _env_id("development")
    bd = _sembrar_bd(server_id=server_id, environment_id=dev)
    _otorgar("environment", prod, "viewer")

    r = admin_client.delete(f"/api/v1/managed-databases/{bd}")
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# El reporte de preparación                                                   #
# --------------------------------------------------------------------------- #


def test_the_readiness_report_counts_the_unclassified(admin_client):
    _sembrar_bd(environment_id=None)
    r = admin_client.get("/api/v1/authz/scope-readiness")
    assert r.status_code == 200, r.text
    datos = r.json()["data"]
    assert datos["unclassified_databases"] >= 1
    assert datos["ready"] is False
    assert datos["fallback_environment_slug"] == "production"


def test_the_readiness_report_is_ready_when_everything_is_classified(admin_client):
    _sembrar_bd(environment_id=_env_id("development"))
    datos = admin_client.get("/api/v1/authz/scope-readiness").json()["data"]
    assert datos["unclassified_databases"] == 0
    assert datos["ready"] is True


def test_the_report_uses_the_same_rule_as_the_guard(admin_client, server_payload):
    """
    Lo que hace útil el reporte: usa la MISMA regla de derivación que el guard, no un criterio
    paralelo. Si dijera una cosa y el guard hiciera otra, sería peor que no tener reporte.
    """
    srv = admin_client.post("/api/v1/servers", json=server_payload())
    server_id = srv.json()["data"]["id"]
    _sembrar_bd(server_id=server_id, environment_id=_env_id("development"))
    _sembrar_bd(server_id=server_id, environment_id=None)

    fila = next(
        f
        for f in admin_client.get("/api/v1/authz/scope-readiness").json()["data"]["servers"]
        if f["server_id"] == server_id
    )
    assert fila["unclassified"] == 1
    assert fila["derived_from_gap"] is True
    assert fila["derived_environment_slug"] == "production"

    # Y el guard coincide.
    from app.models.environment import Environment

    s = Database().get_declarative_base_session()
    try:
        derivado = s.get(Environment, resolve_environment_id(server_id=server_id, managed_database_id=None))
        assert derivado.slug == fila["derived_environment_slug"]
    finally:
        s.close()



# --------------------------------------------------------------------------- #
# require_at: el mecanismo declarativo (role_at_point, min sobre N puntos)    #
# --------------------------------------------------------------------------- #


def _explotar(*args, **kwargs):
    raise AssertionError("se tocó la BD sin necesidad")


def _fijar_puntos(monkeypatch, puntos):
    import app.core.scope as scope_mod

    monkeypatch.setattr(scope_mod, "resolve_points", lambda target: list(puntos))


def test_role_at_point_is_pure_and_the_grant_replaces_the_base():
    """Sin BD: el grant del entorno del punto reemplaza al base; fuera de él manda el base."""
    actor = _actor(GatewayRole.OPERATOR, [("environment", 5, GatewayRole.VIEWER)])

    assert role_at_point(actor, ScopePoint(environment_id=5, server_id=None)) == GatewayRole.VIEWER
    assert role_at_point(actor, ScopePoint(environment_id=6, server_id=None)) == GatewayRole.OPERATOR


def test_role_at_point_takes_the_most_restrictive_of_two_applicable_grants():
    actor = _actor(
        GatewayRole.VIEWER,
        [("environment", 5, GatewayRole.OWNER), ("server", 9, GatewayRole.VIEWER)],
    )
    assert role_at_point(actor, ScopePoint(environment_id=5, server_id=9)) == GatewayRole.VIEWER


def test_the_role_over_many_points_is_the_minimum(monkeypatch):
    """
    Con cuantificador ``all`` el actor necesita la capacidad en CADA punto: un punto prohibido
    basta para negar, aunque el otro esté permitido.
    """
    from app.exceptions import AppHttpException

    actor = _actor(GatewayRole.OPERATOR, [("environment", 2, GatewayRole.VIEWER)])
    _fijar_puntos(
        monkeypatch,
        [ScopePoint(environment_id=1, server_id=None), ScopePoint(environment_id=2, server_id=None)],
    )

    with pytest.raises(AppHttpException) as exc:
        assert_layer2(actor, Capability.DATABASES_WRITE, ScopeTarget("x"))
    assert exc.value.status_code == 403
    assert exc.value.public_context["code"] == "access.forbidden"

    # ``any``: alcanza con un punto permitido (el controller particiona el resto).
    assert_layer2(actor, Capability.DATABASES_WRITE, ScopeTarget("x", quantifier="any"))


def test_an_empty_point_set_falls_back_to_the_base_role(monkeypatch):
    """
    Un blueprint sin BDs no hace ninguna escritura remota: decide el rol BASE. Con base
    ``operator`` pasa; con base ``viewer`` (aunque la unión sea ``owner``) se niega.
    """
    from app.exceptions import AppHttpException

    _fijar_puntos(monkeypatch, [])

    permitido = _actor(GatewayRole.OPERATOR, [("environment", 2, GatewayRole.VIEWER)])
    assert_layer2(permitido, Capability.DATABASES_WRITE, ScopeTarget("model", (1,)))

    negado = _actor(GatewayRole.VIEWER, [("environment", 2, GatewayRole.OWNER)])
    assert negado.has(Capability.DATABASES_WRITE)  # la capa 1 (unión) lo deja pasar
    with pytest.raises(AppHttpException):
        assert_layer2(negado, Capability.DATABASES_WRITE, ScopeTarget("model", (1,)))


def test_a_token_actor_skips_layer_two(monkeypatch):
    """El alcance por destino de un token es su ``project_id``: la capa 2 no se evalúa."""
    import dataclasses

    from app.core.actor import token_actor

    token = token_actor(
        token_pk=1, token_id="t", name="agente", scopes="databases.read", project_id=1
    )
    token = dataclasses.replace(
        token, scope_roles=frozenset({("environment", 1, GatewayRole.VIEWER)})
    )
    import app.core.scope as scope_mod

    monkeypatch.setattr(scope_mod, "resolve_points", _explotar)
    assert_layer2(token, Capability.DATABASES_WRITE, ScopeTarget("x"))


def test_no_scope_grants_is_a_no_op_without_touching_the_database(monkeypatch):
    """El camino rápido de ``require_at``: cero resolución y cero BD para quien no tiene grants."""
    import app.core.scope as scope_mod

    monkeypatch.setattr(scope_mod, "resolve_points", _explotar)
    monkeypatch.setattr(scope_mod, "_session", _explotar)

    actor = _actor(GatewayRole.OWNER)
    assert_at(actor, Capability.DATABASES_DROP, ScopeTarget("database", (1,)))
    assert_layer2(actor, Capability.DATABASES_DROP, ScopeTarget("database", (1,)))


def test_assert_at_runs_layer_one_first_with_the_same_403():
    from app.exceptions import AppHttpException

    actor = _actor(GatewayRole.VIEWER)
    with pytest.raises(AppHttpException) as exc:
        assert_at(actor, Capability.DATABASES_WRITE, ScopeTarget("database", (1,)))
    assert exc.value.status_code == 403
    assert exc.value.public_context["code"] == "access.forbidden"


def test_partition_by_scope_splits_permitted_and_forbidden():
    actor = _actor(GatewayRole.OPERATOR, [("environment", 2, GatewayRole.VIEWER)])
    puntos = [
        ScopePoint(environment_id=1, server_id=None, item_id=10),
        ScopePoint(environment_id=2, server_id=None, item_id=20),
    ]
    part = partition_by_scope(actor=actor, capability=Capability.DATABASES_WRITE, points=puntos)
    assert part.permitted == (10,)
    assert part.forbidden == (20,)


def test_partition_by_scope_requires_the_actor_keyword():
    with pytest.raises(TypeError):
        partition_by_scope(capability=Capability.DATABASES_WRITE, points=[])  # type: ignore[call-arg]


def test_a_nonexistent_target_resolves_to_the_most_protected_environment(client):
    """Para un actor con restricciones, "no existe" y "no podés" son indistinguibles."""
    from app.core.scope import resolve_points

    puntos = resolve_points(ScopeTarget("database", (999999,)))
    assert [p.environment_id for p in puntos] == [most_protected_environment_id()]


def test_require_at_stamps_capability_and_scope_and_rejects_unregistered_resolvers():
    from app.core.authz import declared_capability, declared_scope, require_at
    from app.core.scope_targets import database

    dep = require_at(Capability.DATABASES_WRITE, target=database)
    assert declared_capability(dep) == "databases.write"
    assert declared_scope(dep) == "database"

    def no_registrado(db_id: int) -> ScopeTarget:
        return ScopeTarget("database", (db_id,))

    with pytest.raises(KeyError):
        require_at(Capability.DATABASES_WRITE, target=no_registrado)
