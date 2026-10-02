"""
Resolvedor de capacidades puntuales (``app/core/capability_resolution.py``) y su integración con
la capa 1 (``admin_actor``), la capa 2 (``scope``) y el lector (``find_access_context``).

Lo que se mide: las capacidades puntuales SUMAN al rol (nunca restan), solo cuentan las
``active``, valen en su alcance y no fuera, y un lector que ve una fila no otorgable o la tabla
ausente no concede nada ni tumba la autenticación.
"""

import logging

import pytest
from sqlalchemy import text

from app.core import capability_resolution as cr
from app.core.actor import admin_actor, token_actor
from app.core.authz import actor_from_access_context
from app.core.database import Database
from app.core.scope import (
    ScopePoint,
    ScopeTarget,
    assert_at_point,
    assert_layer2,
    assert_scope,
    partition_by_scope,
)
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.services.capability_catalog import (
    IMPLIED_READ,
    Capability,
    GatewayRole,
    is_grantable,
)
from tests.scope_helpers import env_id, sembrar_bd
from tests.step_up_helpers import OPEN_WINDOW

APPLY = Capability.BLUEPRINTS_APPLY
EXEC_SQL = Capability.SQL_CONSOLE_EXECUTE


def _insertar_cg(
    capability: str,
    scope_type: str,
    scope_id: int,
    status: str = "active",
    expires_at: str | None = None,
    user_id: int = 1,
) -> None:
    """Fila directa en la tabla: lo que se mide es el lector, no el alta (que es del B3)."""
    live = 1 if status in ("pending", "active") else None
    with Database().engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO capability_grants (user_id, capability, scope_type, scope_id, "
                "status, live_key, requested_at, expires_at, created_at, updated_at) "
                "VALUES (:u, :c, :t, :i, :s, :l, CURRENT_TIMESTAMP, :e, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": user_id, "c": capability, "t": scope_type, "i": scope_id,
             "s": status, "l": live, "e": expires_at},
        )


def _actor(base=GatewayRole.VIEWER, grants=(), cgs=()):
    return admin_actor(
        user_id=1,
        username="admin",
        role=base,
        grants=list(grants),
        capability_grants=list(cgs),
        # Prueba las capas 1 y 2: la ventana de step-up abierta (ver `step_up_helpers`).
        step_up_until=OPEN_WINDOW,
    )


def _denegado(fn):
    with pytest.raises(AppHttpException) as exc:
        fn()
    assert exc.value.status_code == 403
    assert exc.value.public_context["code"] == "access.forbidden"


# --------------------------------------------------------------------------- #
# expand                                                                       #
# --------------------------------------------------------------------------- #


def test_expand_adds_the_implied_read_and_nothing_else():
    assert cr.expand(EXEC_SQL) == {EXEC_SQL} | IMPLIED_READ[EXEC_SQL]
    assert Capability.SQL_CONSOLE_HISTORY in cr.expand(EXEC_SQL)
    # Una lectura no implica nada más que ella misma.
    assert cr.expand(Capability.SQL_CONSOLE_HISTORY) == {Capability.SQL_CONSOLE_HISTORY}


# --------------------------------------------------------------------------- #
# R5 a-e                                                                       #
# --------------------------------------------------------------------------- #


def test_r5a_grant_adds_to_a_restrictive_scoped_role_only_at_its_scope(client):
    """viewer@prod + blueprints.apply@prod: aplica en producción; en desarrollo, 403."""
    prod, dev = env_id("production"), env_id("development")
    bd_prod = sembrar_bd(server_id=1, environment_id=prod)
    bd_dev = sembrar_bd(server_id=2, environment_id=dev)
    restriccion = [("environment", prod, GatewayRole.VIEWER)]

    # Base owner con viewer@prod: en producción solo lee. Sin el CG, 403 (control negativo).
    sin_cg = _actor(GatewayRole.OWNER, grants=restriccion)
    _denegado(lambda: assert_scope(sin_cg, APPLY, server_id=1, managed_database_id=bd_prod))

    con_cg = _actor(GatewayRole.OWNER, grants=restriccion, cgs=[(APPLY, "environment", prod)])
    assert_scope(con_cg, APPLY, server_id=1, managed_database_id=bd_prod)
    # Fuera de su alcance el CG no hace falta y el rol base manda.
    assert_scope(con_cg, APPLY, server_id=2, managed_database_id=bd_dev)

    # El caso de negocio: viewer de base, CG en producción; en desarrollo sigue siendo viewer.
    viewer = _actor(GatewayRole.VIEWER, cgs=[(APPLY, "environment", prod)])
    assert_scope(viewer, APPLY, server_id=1, managed_database_id=bd_prod)
    _denegado(lambda: assert_scope(viewer, APPLY, server_id=2, managed_database_id=bd_dev))


def test_r5b_server_and_environment_grants_are_a_union(client):
    prod = env_id("production")
    bd = sembrar_bd(server_id=5, environment_id=prod)
    solo_env = _actor(cgs=[(APPLY, "environment", prod)])
    solo_srv = _actor(cgs=[(APPLY, "server", 5)])
    ambos = _actor(cgs=[(APPLY, "environment", prod), (APPLY, "server", 5)])
    otro_srv = _actor(cgs=[(APPLY, "server", 6)])

    for a in (solo_env, solo_srv, ambos):
        assert_scope(a, APPLY, server_id=5, managed_database_id=bd)
    _denegado(lambda: assert_scope(otro_srv, APPLY, server_id=5, managed_database_id=bd))


def test_r5c_a_pending_grant_grants_nothing(client):
    prod = env_id("production")
    _insertar_cg("databases.drop", "environment", prod, status="pending")
    actor = actor_from_access_context(1, "admin", UserModel().find_access_context(1))
    assert actor.capability_grants == frozenset()


def test_r5d_layer1_passes_via_union_layer2_only_where_the_scope_matches(client):
    prod, dev = env_id("production"), env_id("development")
    bd_prod = sembrar_bd(server_id=1, environment_id=prod)
    bd_dev = sembrar_bd(server_id=2, environment_id=dev)
    actor = _actor(GatewayRole.VIEWER, cgs=[(APPLY, "environment", prod)])

    assert actor.has(APPLY)  # capa 1: la unión, en cualquier alcance
    assert_scope(actor, APPLY, server_id=1, managed_database_id=bd_prod)
    _denegado(lambda: assert_scope(actor, APPLY, server_id=2, managed_database_id=bd_dev))


def test_r5e_unclassified_database_resolves_to_the_most_protected(client):
    from app.core.scope import most_protected_environment_id

    protegido = most_protected_environment_id()
    otro = env_id("development")
    assert protegido != otro
    bd = sembrar_bd(server_id=1, environment_id=None)

    assert_scope(
        _actor(cgs=[(APPLY, "environment", protegido)]),
        APPLY, server_id=1, managed_database_id=bd,
    )
    _denegado(
        lambda: assert_scope(
            _actor(cgs=[(APPLY, "environment", otro)]),
            APPLY, server_id=1, managed_database_id=bd,
        )
    )


def test_a_global_target_matches_no_grant(client):
    actor = _actor(cgs=[(APPLY, "environment", env_id("production"))])
    _denegado(lambda: assert_scope(actor, APPLY, server_id=None, managed_database_id=None))


def test_an_implied_read_is_effective_at_the_scope_of_the_grant(client):
    prod, dev = env_id("production"), env_id("development")
    bd_prod = sembrar_bd(server_id=1, environment_id=prod)
    bd_dev = sembrar_bd(server_id=2, environment_id=dev)
    # Un rol sin lectura de consola (el catálogo la deja en viewer, así que se usa un rol por
    # alcance cuyo conjunto no la incluya no es posible): se prueba el predicado puro.
    actor = _actor(cgs=[(EXEC_SQL, "environment", prod)])
    assert actor.has(Capability.SQL_CONSOLE_HISTORY)
    assert cr.grants_allow(
        actor, Capability.SQL_CONSOLE_HISTORY, environment_id=prod, server_id=1
    )
    assert not cr.grants_allow(
        actor, Capability.SQL_CONSOLE_HISTORY, environment_id=dev, server_id=2
    )
    assert bd_prod and bd_dev


# --------------------------------------------------------------------------- #
# Capa 2 por puntos (los demás caminos de capa 2 comparten el mismo predicado)  #
# --------------------------------------------------------------------------- #


def test_layer2_helpers_honor_grants_at_resolved_points(client):
    prod, dev = env_id("production"), env_id("development")
    actor = _actor(GatewayRole.VIEWER, cgs=[(APPLY, "environment", prod)])

    assert_at_point(actor, APPLY, ScopePoint(environment_id=prod, server_id=1))
    _denegado(lambda: assert_at_point(actor, APPLY, ScopePoint(environment_id=dev, server_id=1)))

    particion = partition_by_scope(
        actor=actor,
        capability=APPLY,
        points=[
            ScopePoint(environment_id=prod, server_id=1, item_id=10),
            ScopePoint(environment_id=dev, server_id=1, item_id=11),
        ],
    )
    assert particion.permitted == (10,)
    assert particion.forbidden == (11,)


def test_assert_layer2_without_scope_roles_still_checks_the_target_when_a_grant_is_relevant(client):
    """
    El hueco que habría abierto el camino rápido sin esta regla: sin alcances por rol, la capa 2
    era un no-op y la capa 1 (que ya incluye la capacidad puntual) dejaba pasar en TODO destino.
    """
    prod, dev = env_id("production"), env_id("development")
    bd_prod = sembrar_bd(server_id=1, environment_id=prod)
    bd_dev = sembrar_bd(server_id=2, environment_id=dev)
    actor = _actor(GatewayRole.VIEWER, cgs=[(APPLY, "environment", prod)])
    assert not actor.scope_roles

    assert_layer2(actor, APPLY, ScopeTarget(kind="database", params=(bd_prod,)))
    _denegado(lambda: assert_layer2(actor, APPLY, ScopeTarget(kind="database", params=(bd_dev,))))


# --------------------------------------------------------------------------- #
# Camino rápido: sin lectura de BD                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sin_bd(monkeypatch):
    """Cualquier resolución de destino explota: prueba que el camino no toca la BD."""
    import app.core.scope as scope

    def _boom(**_):
        raise AssertionError("no debería resolver el destino")

    monkeypatch.setattr(scope, "resolve_environment_id", _boom)
    monkeypatch.setattr(scope, "resolve_points", lambda *_: _boom())


def test_fast_path_actor_without_scope_roles_or_grants_reads_no_db(sin_bd):
    actor = _actor(GatewayRole.OPERATOR)
    assert cr.capability_at(
        actor, Capability.DATABASES_WRITE, server_id=1, managed_database_id=7
    )
    assert not cr.capability_at(
        actor, Capability.DATABASES_DROP, server_id=1, managed_database_id=7
    )
    assert_scope(actor, Capability.DATABASES_WRITE, server_id=1, managed_database_id=7)
    assert_layer2(actor, Capability.DATABASES_WRITE, ScopeTarget(kind="database", params=(7,)))


def test_fast_path_grant_irrelevant_to_the_capability_reads_no_db(sin_bd):
    """Un CG de otra capacidad no obliga a resolver el destino (D8)."""
    cap = Capability.DATABASES_WRITE
    actor = _actor(GatewayRole.OPERATOR, cgs=[(Capability.DATABASES_DROP, "environment", 1)])
    assert not cr.needs_target_resolution(actor, cap)
    assert cr.capability_at(actor, cap, server_id=1, managed_database_id=7)


def test_fast_path_base_role_already_allows_reads_no_db(sin_bd):
    """Relevante pero redundante: si el rol base ya lo permite, no hace falta resolver."""
    cap = Capability.DATABASES_WRITE
    actor = _actor(GatewayRole.OPERATOR, cgs=[(cap, "environment", 1)])
    assert cr.capability_at(actor, cap, server_id=1, managed_database_id=7)


def test_token_actor_is_unaffected_and_never_gains_grants(client):
    tok = token_actor(token_pk=1, token_id="t", name="n", scopes="databases.read", project_id=1)
    assert tok.capability_grants == frozenset()
    assert not cr.has_relevant_grant(tok, APPLY)
    assert not cr.needs_target_resolution(tok, APPLY)
    assert not cr.grants_allow(tok, APPLY, environment_id=1, server_id=1)
    # La capa 2 de un token sigue siendo la del rol viewer; BLUEPRINTS_APPLY no es de viewer.
    _denegado(lambda: assert_scope(tok, APPLY, server_id=1, managed_database_id=None))


# --------------------------------------------------------------------------- #
# Lector: find_access_context + actor_from_access_context                      #
# --------------------------------------------------------------------------- #


def test_reader_loads_only_active_grants(client):
    prod = env_id("production")
    _insertar_cg("blueprints.apply", "environment", prod, status="active")
    for estado in ("pending", "rejected", "expired", "cancelled", "revoked"):
        _insertar_cg("clones.execute", "environment", prod, status=estado)

    ctx = UserModel().find_access_context(1)
    assert [(g["capability"], g["scope_id"]) for g in ctx["capability_grants"]] == [
        ("blueprints.apply", prod)
    ]
    actor = actor_from_access_context(1, "admin", ctx)
    assert actor.capability_grants == {(APPLY, "environment", prod)}


def test_reader_ignores_grants_of_a_deactivated_user(client):
    _insertar_cg("blueprints.apply", "environment", env_id("production"))
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE id = 1"))
    assert UserModel().find_access_context(1)["capability_grants"] == []


def test_reader_ignores_an_active_grant_with_an_elapsed_expiry(client):
    """Vencimiento perezoso: aunque el barrido no la marcó, una fila vencida no concede."""
    _insertar_cg(
        "blueprints.apply", "environment", env_id("production"),
        expires_at="2000-01-01 00:00:00",
    )
    assert UserModel().find_access_context(1)["capability_grants"] == []


@pytest.mark.parametrize(
    "capability",
    ["gateway.admin", "access.admin", "environments.write", "no.existe", "", "BLUEPRINTS_APPLY"],
)
def test_reader_drops_non_grantable_or_unknown_rows(client, capability):
    """D3: una fila editada a mano no puede acuñar una capacidad (fail-closed en el lector)."""
    _insertar_cg(capability, "environment", env_id("production"))
    ctx = UserModel().find_access_context(1)
    assert len(ctx["capability_grants"]) == 1  # la fila existe y se lee…
    actor = actor_from_access_context(1, "admin", ctx)
    assert actor.capability_grants == frozenset()  # …pero no concede nada
    assert not is_grantable(capability)


def test_reader_drops_unknown_scope_types_and_non_positive_ids():
    rows = [
        {"id": 1, "capability": "blueprints.apply", "scope_type": "global", "scope_id": 1},
        {"id": 2, "capability": "blueprints.apply", "scope_type": "environment", "scope_id": 0},
        {"id": 3, "capability": "blueprints.apply", "scope_type": "environment", "scope_id": "x"},
        {"id": 4, "capability": "blueprints.apply", "scope_type": "server", "scope_id": 3},
    ]
    assert cr.parse_capability_grants(rows) == [(APPLY, "server", 3, 4)]


def test_an_unparseable_restrictive_scope_role_resolves_to_viewer_not_dropped(caplog):
    """F-15: un ``viewer`` en prod sobre base ``owner`` con rol ilegible NO puede desaparecer."""
    from app.core.scope import role_at_point

    ctx = {
        "role": "owner",
        "grants": [
            ("environment", 7, "Viewer"),  # mayúscula: valor legado / UPDATE a mano
            ("server", "3", None),
            ("galaxy", 9, "viewer"),  # scope_type desconocido: no hay a qué aplicarlo
            ("environment", "x", "viewer"),  # scope_id ilegible: no empareja con nada
        ],
    }
    with caplog.at_level(logging.WARNING, logger="app.core.capability_resolution"):
        parsed = cr.parse_access_context(ctx)
    assert parsed.base == GatewayRole.OWNER
    assert parsed.scope_roles == (
        ("environment", 7, GatewayRole.VIEWER),
        ("server", 3, GatewayRole.VIEWER),
    )
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4

    actor = actor_from_access_context(1, "admin", ctx)
    assert role_at_point(actor, ScopePoint(environment_id=7, server_id=None)) == GatewayRole.VIEWER
    assert not cr.capability_at_point(actor, APPLY, ScopePoint(environment_id=7, server_id=None))
    # Fuera del alcance restringido manda el base.
    assert role_at_point(actor, ScopePoint(environment_id=8, server_id=None)) == GatewayRole.OWNER


def test_a_missing_table_yields_no_grants_and_keeps_roles_working(client, caplog):
    """D9 / R12: código sin la tabla no concede nada, no tumba la autenticación y deja ERROR."""
    with Database().engine.begin() as conn:
        conn.execute(text("DROP TABLE capability_grants"))
    with caplog.at_level(logging.ERROR):
        ctx = UserModel().find_access_context(1)
    assert ctx["capability_grants"] == []
    assert ctx["role"] == "owner"  # los roles siguen resolviendo
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert actor_from_access_context(1, "admin", ctx).has(APPLY)


# --------------------------------------------------------------------------- #
# Capa 1 y explain                                                             #
# --------------------------------------------------------------------------- #


def test_layer1_unions_grants_with_the_role(client):
    base = _actor(GatewayRole.VIEWER)
    con = _actor(GatewayRole.VIEWER, cgs=[(EXEC_SQL, "server", 9)])
    assert not base.has(EXEC_SQL)
    assert con.has(EXEC_SQL) and con.has(Capability.SQL_CONSOLE_HISTORY)
    assert con.capabilities >= base.capabilities


@pytest.mark.parametrize("base", list(GatewayRole))
def test_explain_non_inert_entries_equal_the_layer1_set(base):
    ctx = {
        "role": base.value,
        "grants": [("environment", 3, "owner"), ("server", 4, "viewer")],
        "globals": ["access_admin"],
        "capability_grants": [
            {"id": 7, "capability": "sql_console.execute", "scope_type": "server", "scope_id": 9},
            {"id": 8, "capability": "gateway.admin", "scope_type": "server", "scope_id": 9},
        ],
    }
    actor = actor_from_access_context(1, "u", ctx)
    entradas = cr.explain(ctx)
    assert {e.capability for e in entradas if not e.inert} == actor.capabilities
    cg = [e for e in entradas if e.source == "capability_grant"]
    assert {(e.capability.value, e.implied_by) for e in cg} == {
        ("sql_console.execute", None),
        ("sql_console.history", EXEC_SQL),
    }
    assert all(e.grant_id == 7 for e in cg)  # la fila no otorgable ni aparece


def test_explain_marks_grants_of_a_deactivated_user_inert():
    ctx = {
        "role": "viewer", "grants": [], "globals": [],
        "capability_grants": [
            {"id": 1, "capability": "blueprints.apply", "scope_type": "server", "scope_id": 2}
        ],
    }
    entradas = cr.explain(ctx, active=False)
    assert all(e.inert for e in entradas if e.source == "capability_grant")
    assert not any(e.inert for e in entradas if e.source != "capability_grant")


# --------------------------------------------------------------------------- #
# Riesgo 1: rutas exentas de capa 2 con capacidades otorgables                  #
# --------------------------------------------------------------------------- #


def test_scope_exempt_routes_with_grantable_capabilities_are_explicitly_justified():
    """
    Las rutas de ``SCOPE_EXEMPT`` solo corren la capa 1, y la capa 1 incluye las capacidades
    puntuales de CUALQUIER alcance. Si una exenta exige una capacidad otorgable, un CG la hace
    alcanzable sin destino. Cualquier capacidad otorgable en una ruta exenta tiene que
    justificarse acá, a propósito:

    - ``blueprints.write`` es SOLO AUTORÍA: crear/editar blueprints, versiones y proyectos en la
      BD del gateway. Todo lo que escribe en BDs de terceros (rename-slug, migrate-version-table,
      stamp, la versión de un lote de collation) pide ``blueprints.apply`` con destino, y el
      borrado de un blueprint también pide ``apply``. Así un CG de ``write`` en cualquier alcance
      no alcanza ningún motor.
    - ``blueprints.apply`` aparece solo en ``DELETE /database-models/{id}``: responde 409
      (``database_model.in_use``) mientras alguna BD lo referencie, así que cuando procede no hay
      destino al que anclar el alcance. Con ``require_at(target=model)`` decidiría el rol base y
      un CG de ``apply`` nunca podría autorizarlo.
    """
    import importlib.util
    import pathlib

    from main import app

    ruta = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"
    spec = importlib.util.spec_from_file_location("check_route_capabilities", ruta)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    justificadas = {
        "blueprints.write": "solo autoría de blueprints/proyectos: no ejecuta en ninguna BD",
        "blueprints.apply": "borrar un blueprint sin BDs (409 si alguna lo referencia)",
    }
    encontradas: set[str] = set()
    for path, route in guard._iter_routes(app):
        for metodo in route.methods:
            if (metodo, path) in guard.SCOPE_EXEMPT:
                cap = guard._capability_of(route)
                if cap and is_grantable(cap):
                    encontradas.add(cap)
    assert encontradas <= set(justificadas), (
        f"ruta exenta con capacidad otorgable sin justificar: {encontradas - set(justificadas)}"
    )
    assert encontradas  # el test deja de medir algo si la exención desaparece: que avise
