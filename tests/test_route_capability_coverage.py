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
    El chequeo completo sobre la app REAL: toda ruta declara capacidad, o ``AdminDep``, o está
    en ``PUBLIC_ROUTES``. Ninguna las tres cosas a la vez ausentes.
    """
    assert guard.main() == 0


def test_the_three_buckets_add_up_to_every_mounted_route(guard):
    """
    Migradas + legadas + públicas == total montado.

    Si la suma no cierra, hay rutas que la clasificación no ve — y una ruta invisible al
    chequeo es exactamente una ruta sin guard que el chequeo declara sana.
    """
    from main import app

    total = sum(
        len(r.methods - {"HEAD", "OPTIONS"}) for _, r in guard._iter_routes(app)
    )
    migradas = legadas = publicas = 0
    for path, route in guard._iter_routes(app):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if guard._capability_of(route):
                migradas += 1
            elif (method, path) in guard.PUBLIC_ROUTES:
                publicas += 1
            elif guard._uses_legacy_guard(route):
                legadas += 1
    assert migradas + legadas + publicas == total


def test_detects_a_route_with_no_guard_at_all(guard):
    """
    La prueba de que el chequeo SIRVE: sobre una app sintética con una ruta desnuda, ni
    ``_capability_of`` ni ``_uses_legacy_guard`` la reconocen.

    Sin este test, el de arriba podría pasar por una regla que no detecta nada.
    """
    app = FastAPI()

    @app.get("/desnuda")
    def desnuda():
        return {}

    (_, route), = list(guard._iter_routes(app))
    assert guard._capability_of(route) is None
    assert guard._uses_legacy_guard(route) is False


def test_detects_a_declared_capability_through_a_dependency(guard):
    """El marcador se encuentra recorriendo el árbol de dependencias resuelto."""
    from app.core.authz import DatabasesDrop

    app = FastAPI()

    @app.delete("/con-capacidad")
    def con_capacidad(actor: DatabasesDrop):
        return {}

    (_, route), = list(guard._iter_routes(app))
    assert guard._capability_of(route) == "databases.drop"


def test_detects_the_legacy_guard(guard):
    from app.core.auth import AdminDep

    app = FastAPI()

    @app.get("/legada")
    def legada(admin: AdminDep):
        return {}

    (_, route), = list(guard._iter_routes(app))
    assert guard._capability_of(route) is None
    assert guard._uses_legacy_guard(route) is True


def test_public_routes_allowlist_is_short_and_explicit(guard):
    """
    Corta y explícita, nunca una heurística por prefijo: `/api/v1/test/*` se quedó sin guard
    durante meses justamente porque nadie tenía que declararlo en ninguna parte.
    """
    assert len(guard.PUBLIC_ROUTES) <= 5
    for method, path in guard.PUBLIC_ROUTES:
        assert method in {"GET", "POST"}
        assert path.startswith("/")
