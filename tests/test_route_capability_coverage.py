"""
Cobertura de autorización: ninguna ruta sin guard.

Corre la MISMA función que ``scripts/check_route_capabilities.py`` usa en CI, no una copia:
mantener dos implementaciones de la regla es cómo una de las dos se relaja sin que nadie lo
note. Existe además del script para que el fallo aparezca **con el diff que lo causó** y no
recién en el pipeline.

El modo de fallo que protege es concreto y ya pasó en este repo: ``app/routes/v1/test.py`` tuvo
**siete rutas sin autenticación** —dos subiendo archivos a disco— montadas durante meses,
porque la ausencia de un parámetro no se lee como un error en un PR.
"""

import importlib.util
import pathlib

import pytest
from fastapi import FastAPI

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_route_capabilities", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    return _load_script()


def test_no_route_is_left_without_a_guard(guard):
    """
    El chequeo completo sobre la app REAL: toda ruta declara una capacidad del catálogo o está
    en ``PUBLIC_ROUTES``. No hay tercera opción desde que se retiró ``AdminDep``.
    """
    assert guard.main() == 0


def test_the_three_buckets_add_up_to_every_mounted_route(guard):
    """
    Con capacidad + públicas + de agente == total montado, **sin rama de descarte**.

    Las cubetas cambiaron dos veces y el test tiene que seguirlas: eran tres con el guard
    legado, quedaron dos al retirarlo, y volvieron a ser tres cuando el servidor MCP sumó una
    ruta autenticada por **bearer** —que no es pública y cuya autorización es por tool, no por
    endpoint—.

    La suma EXACTA es lo que hace que el test valga: si hubiera un ``else`` que absorbiera lo no
    clasificado, esto pasaría con rutas invisibles al chequeo — y una ruta invisible es
    exactamente una ruta sin guard que el chequeo declara sana.
    """
    from main import app

    total = sum(
        len(r.methods - {"HEAD", "OPTIONS"}) for _, r in guard._iter_routes(app)
    )
    con_capacidad = publicas = agente = 0
    for path, route in guard._iter_routes(app):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if guard._capability_of(route):
                con_capacidad += 1
            elif (method, path) in guard.PUBLIC_ROUTES:
                publicas += 1
            elif (method, path) in guard.AGENT_ROUTES:
                agente += 1
    assert con_capacidad + publicas + agente == total
    assert publicas == len(guard.PUBLIC_ROUTES)
    assert agente == len(guard.AGENT_ROUTES)


def test_every_agent_route_really_demands_the_bearer(guard):
    """
    ``AGENT_ROUTES`` no puede ser una forma elegante de declarar "sin guard": cada entrada tiene
    que exigir de verdad la autenticación de agentes.

    Es el mismo chequeo que hace el script, verificado desde acá para que el fallo aparezca con
    el diff que lo causó y no recién en el pipeline.
    """
    from main import app

    vistas = 0
    for path, route in guard._iter_routes(app):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, path) in guard.AGENT_ROUTES:
                assert guard._uses_agent_auth(route), f"{method} {path} no exige el bearer"
                vistas += 1
    assert vistas == len(guard.AGENT_ROUTES), "hay entradas de AGENT_ROUTES que no existen"


def test_detects_a_route_with_no_guard_at_all(guard):
    """
    La prueba de que el chequeo SIRVE: sobre una app sintética con una ruta desnuda,
    ``_capability_of`` no la reconoce y por eso la clasificación la manda a ``sin_guard``.

    Sin este test, el de arriba podría pasar por una regla que no detecta nada.
    """
    app = FastAPI()

    @app.get("/desnuda")
    def desnuda():
        return {}

    (_, route), = list(guard._iter_routes(app))
    assert guard._capability_of(route) is None


def test_detects_a_declared_capability_through_a_dependency(guard):
    """El marcador se encuentra recorriendo el árbol de dependencias resuelto."""
    from app.core.authz import DatabasesDrop

    app = FastAPI()

    @app.delete("/con-capacidad")
    def con_capacidad(actor: DatabasesDrop):
        return {}

    (_, route), = list(guard._iter_routes(app))
    assert guard._capability_of(route) == "databases.drop"


def test_the_legacy_guard_no_longer_exists(guard):
    """
    ``AdminDep`` se RETIRÓ del código, no se deprecó, y esa diferencia es la que importa: un
    endpoint nuevo copiado de uno viejo tiene que **fallar al importar** en vez de nacer
    autenticado y sin autorizar. Este test fija esa propiedad, porque reponerlo "por
    compatibilidad" es exactamente el atajo que reabriría el agujero.
    """
    import app.core.auth as auth_mod

    assert not hasattr(auth_mod, "AdminDep")
    assert not hasattr(auth_mod, "get_current_admin")
    # Y lo que sí queda: la resolución de sesión, que NO autoriza.
    assert hasattr(auth_mod, "authenticated_user")


def test_public_routes_allowlist_is_short_and_explicit(guard):
    """
    Corta y explícita, nunca una heurística por prefijo: `/api/v1/test/*` se quedó sin guard
    durante meses justamente porque nadie tenía que declararlo en ninguna parte.
    """
    assert len(guard.PUBLIC_ROUTES) <= 5
    for method, path in guard.PUBLIC_ROUTES:
        assert method in {"GET", "POST"}
        assert path.startswith("/")


# --------------------------------------------------------------------------- #
# Chequeo 6: toda ruta de alcance declara su destino                           #
# --------------------------------------------------------------------------- #
#
# Se ejerce ``scope_errors`` sobre apps SINTÉTICAS con listas propias: la prueba de que el
# chequeo sirve no puede depender de que la app real esté rota.


def _scope_app():
    """Fábrica de apps sintéticas: devuelve ``(app, DatabasesWrite, ScopedWrite)``."""
    from typing import Annotated

    from fastapi import Depends

    from app.core.actor import Actor
    from app.core.authz import DatabasesWrite, require_at
    from app.core.scope_targets import database
    from app.services.capability_catalog import Capability

    scoped = Annotated[
        Actor, Depends(require_at(Capability.DATABASES_WRITE, target=database))
    ]
    return FastAPI(), DatabasesWrite, scoped


def _errors(guard, app, *, pending=frozenset(), exempt=None, max_pending=0):
    return guard.scope_errors(
        app, pending=pending, exempt=exempt or {}, max_pending=max_pending
    )


def test_check_6_fails_naming_an_undeclared_scope_route(guard):
    app, plain, _ = _scope_app()

    @app.post("/sin-destino")
    def sin_destino(actor: plain):
        return {}

    errores = _errors(guard, app)
    assert any("POST /sin-destino" in e for e in errores)


def test_check_6_accepts_a_route_that_declares_its_target(guard):
    app, _, scoped = _scope_app()

    @app.delete("/bd/{db_id}")
    def borrar(actor: scoped, db_id: int):
        return {}

    assert _errors(guard, app) == []


def test_check_6_accepts_listed_pending_and_exempt_routes(guard):
    app, plain, _ = _scope_app()

    @app.post("/pendiente")
    def pendiente(actor: plain):
        return {}

    @app.post("/exenta")
    def exenta(actor: plain):
        return {}

    errores = _errors(
        guard,
        app,
        pending=frozenset({("POST", "/pendiente")}),
        exempt={("POST", "/exenta"): "autoría"},
        max_pending=1,
    )
    assert errores == []


def test_check_6_does_not_ask_a_target_of_viewer_floor_or_global_capabilities(guard):
    from app.core.authz import DatabasesRead, GatewayAdmin

    app = FastAPI()

    @app.get("/lectura")
    def lectura(actor: DatabasesRead):
        return {}

    @app.get("/global")
    def global_(actor: GatewayAdmin):
        return {}

    assert _errors(guard, app) == []


def test_check_6b_rejects_assert_capability_inside_a_scoped_endpoint(guard):
    from app.core.authz import assert_capability
    from app.services.capability_catalog import Capability

    app, _, scoped = _scope_app()

    @app.delete("/bd/{db_id}")
    def borrar(actor: scoped, db_id: int):
        assert_capability(actor, Capability.DATABASES_DROP)
        return {}

    errores = _errors(guard, app)
    assert any("DELETE /bd/{db_id}" in e and "6b" in e for e in errores)


def test_check_6b_ignores_a_docstring_that_only_names_assert_capability(guard):
    app, _, scoped = _scope_app()

    @app.delete("/bd/{db_id}")
    def borrar(actor: scoped, db_id: int):
        """Usa assert_capability( solo en el texto, no la llama."""
        return {}

    assert _errors(guard, app) == []


def test_a_stale_entry_for_an_already_migrated_route_fails(guard):
    app, _, scoped = _scope_app()

    @app.delete("/bd/{db_id}")
    def borrar(actor: scoped, db_id: int):
        return {}

    errores = _errors(
        guard, app, pending=frozenset({("DELETE", "/bd/{db_id}")}), max_pending=1
    )
    assert any("DELETE /bd/{db_id}" in e and "entrada vieja" in e for e in errores)


def test_a_ghost_entry_fails(guard):
    app = FastAPI()
    errores = _errors(
        guard,
        app,
        pending=frozenset({("POST", "/no-existe")}),
        exempt={("POST", "/tampoco"): "x"},
        max_pending=1,
    )
    assert any("POST /no-existe" in e and "POST /tampoco" in e for e in errores)


def test_the_pending_ratchet_fails_when_the_list_grows(guard):
    app, plain, _ = _scope_app()

    @app.post("/a")
    def a(actor: plain):
        return {}

    @app.post("/b")
    def b(actor: plain):
        return {}

    pending = frozenset({("POST", "/a"), ("POST", "/b")})
    assert any("CRECIÓ" in e for e in _errors(guard, app, pending=pending, max_pending=1))
    assert _errors(guard, app, pending=pending, max_pending=2) == []


def test_the_real_pending_list_is_empty_and_exemptions_have_reasons(guard):
    # Check 6 estricto: no queda ninguna ruta "para migrar después".
    assert guard.SCOPE_PENDING == frozenset()
    assert guard.MAX_SCOPE_PENDING == 0
    assert guard.SCOPE_EXEMPT, "la autoría de blueprints y los proyectos están exentos"
    assert all(reason.strip() for reason in guard.SCOPE_EXEMPT.values())
    assert not (guard.SCOPE_PENDING & set(guard.SCOPE_EXEMPT))


def test_main_fails_when_scope_pending_is_not_empty(guard, monkeypatch, capsys):
    """El chequeo 6 es estricto: UNA entrada pendiente rompe el script, aunque el tope la admita."""
    monkeypatch.setattr(guard, "SCOPE_PENDING", frozenset({("POST", "/api/v1/projects")}))
    monkeypatch.setattr(guard, "MAX_SCOPE_PENDING", 1)
    assert guard.main() == 1
    assert "SCOPE_PENDING tiene que estar VACÍO" in capsys.readouterr().err
