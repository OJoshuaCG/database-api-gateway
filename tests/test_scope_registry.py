"""
Registro de rutas con alcance: CADA una niega a un actor restringido.

POR QUÉ ESTE TEST ES GENÉRICO
-----------------------------
La capa 2 es declarativa (``require_at``), así que el conjunto de rutas protegidas es
ENUMERABLE y no hay que escribir un test a mano por ruta —que es justo lo que se olvida—. El
actor es ``owner`` de base (la capa 1 lo deja pasar en todo) con un grant ``viewer`` sobre
CADA entorno: en la capa 2 es lector en todas partes. Con ids que no existen, cada destino
resuelve al entorno más protegido, así que toda ruta con alcance tiene que dar 403
``access.forbidden`` y nunca 404.

Sin rama de salto: ``visitadas == con alcance``. Una ruta con alcance que este test no pudiera
ejercer tiene que FALLAR acá, no quedar fuera del conteo en silencio.
"""

import importlib.util
import pathlib
import re

import pytest
from sqlalchemy import text

from app.core.database import Database
from tests.scope_helpers import otorgar

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("check_route_capabilities", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sintetizar(path: str) -> str:
    """Un id sintético por parámetro de ruta: números inexistentes, o el nombre/versión."""

    def sub(m: re.Match) -> str:
        nombre = m.group(1)
        if nombre == "version":
            return "0001"
        if nombre == "id" or nombre.endswith("_id"):
            return "999999"
        return "x"

    return re.sub(r"\{(\w+)\}", sub, path)


def _rutas_con_alcance(guard):
    from main import app

    out = []
    for path, route in guard._iter_routes(app):
        if guard._scope_of(route) is None:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


def test_every_scoped_route_denies_a_viewer_everywhere(admin_client, guard):
    with Database().engine.begin() as conn:
        entornos = [r[0] for r in conn.execute(text("SELECT id FROM environments"))]
    assert entornos, "no hay entornos sembrados"
    for eid in entornos:
        otorgar("environment", eid, "viewer")

    rutas = _rutas_con_alcance(guard)
    assert rutas, "ninguna ruta declara destino"

    visitadas = 0
    for method, path in rutas:
        r = admin_client.request(method, _sintetizar(path), json={})
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}: {r.text}"
        assert r.json()["detail"]["public_context"]["code"] == "access.forbidden", (
            f"{method} {path}"
        )
        visitadas += 1

    assert visitadas == len(rutas)


def test_scoped_routes_are_never_in_the_pending_or_exempt_lists(guard):
    """Lo que el registro ejerce y lo que las listas dicen que NO está migrado son disjuntos."""
    rutas = set(_rutas_con_alcance(guard))
    assert rutas
    assert not (rutas & guard.SCOPE_PENDING)
    assert not (rutas & set(guard.SCOPE_EXEMPT))
