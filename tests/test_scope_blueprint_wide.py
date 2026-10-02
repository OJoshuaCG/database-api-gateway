"""
Operaciones de blueprint con capa 2: se evalúan en el entorno MÁS PROTEGIDO de sus BDs.

Un blueprint escribe en cada BD que lo replica (renombrar el slug son N escrituras remotas), así
que el destino no es una BD sino el conjunto. El actor es ``owner`` de base con ``viewer`` en
producción: lector donde hay producción, dueño en el resto.

Casos que fijan el diseño:

1. blueprint mixto (desarrollo + producción): 403 en TODAS las rutas, aunque en desarrollo el
   actor sea dueño (el peor entorno manda);
2. blueprint solo de desarrollo: la capa 2 lo deja pasar;
3. blueprint SIN BDs: no hay escritura remota, decide el rol base (no se trata como producción);
4. blueprint inexistente: 403, indistinguible de "no podés".

Ninguna prueba conecta a un motor: la autorización resuelve antes de tocarlo.
"""

import pytest
from sqlalchemy import text

from app.core.database import Database
from tests.scope_helpers import env_id, otorgar, sembrar_bd

_API = "/api/v1/database-models"


def _forbidden(r) -> bool:
    return (
        r.status_code == 403
        and r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    )


def _blueprint(admin_client, slug: str) -> int:
    r = admin_client.post(_API, json={"name": slug.upper(), "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _vincular(db_id: int, model_id: int) -> None:
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE managed_databases SET model_id = :m WHERE id = :d"),
            {"m": model_id, "d": db_id},
        )


def _rutas(model_id: int) -> list[tuple[str, str, dict | None]]:
    """Las 8 rutas de blueprint entero; ``None`` = sin cuerpo."""
    base = f"{_API}/{model_id}"
    return [
        ("POST", f"{base}/rename-slug", {"new_slug": "otro-slug"}),
        ("POST", f"{base}/rename-slug/plan", {"new_slug": "otro-slug"}),
        ("POST", f"{base}/migrate-version-table", {}),
        ("POST", f"{base}/migrate-version-table/plan", None),
        ("POST", f"{base}/databases/refresh", None),
        ("GET", f"{base}/migrations/0001/delete-plan", None),
        ("DELETE", f"{base}/migrations/0001", None),
        ("POST", f"{base}/collation-conversions/1/blueprint-version", {"name": "v"}),
    ]


def _llamar(admin_client, metodo: str, path: str, cuerpo: dict | None):
    kwargs = {} if cuerpo is None else {"json": cuerpo}
    return admin_client.request(metodo, path, **kwargs)


@pytest.fixture()
def escenario(admin_client, server_payload):
    """Tres blueprints (mixto, solo-dev, vacío), sembrados ANTES de restringir al actor."""
    prod, dev = env_id("production"), env_id("development")
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]

    mixto = _blueprint(admin_client, "mixto")
    _vincular(sembrar_bd(server_id=sid, environment_id=dev, name="m_dev"), mixto)
    _vincular(sembrar_bd(server_id=sid, environment_id=prod, name="m_prod"), mixto)

    solo_dev = _blueprint(admin_client, "solo-dev")
    _vincular(sembrar_bd(server_id=sid, environment_id=dev, name="d_dev"), solo_dev)

    sin_clasif = _blueprint(admin_client, "sin-clasificar")
    _vincular(sembrar_bd(server_id=sid, environment_id=None, name="d_null"), sin_clasif)

    vacio = _blueprint(admin_client, "vacio")
    otorgar("environment", prod, "viewer")
    return {
        "mixto": mixto,
        "solo_dev": solo_dev,
        "sin_clasificar": sin_clasif,
        "vacio": vacio,
    }


def test_mixed_environment_blueprint_is_denied_on_every_route(admin_client, escenario):
    for metodo, path, cuerpo in _rutas(escenario["mixto"]):
        r = _llamar(admin_client, metodo, path, cuerpo)
        assert _forbidden(r), f"{metodo} {path} -> {r.status_code}: {r.text}"


def test_rename_slug_does_not_change_the_slug_when_denied(admin_client, escenario):
    r = admin_client.post(
        f"{_API}/{escenario['mixto']}/rename-slug", json={"new_slug": "otro-slug"}
    )
    assert _forbidden(r)
    slug = admin_client.get(f"{_API}/{escenario['mixto']}").json()["data"]["slug"]
    assert slug == "mixto"


def test_unclassified_database_counts_as_the_most_protected(admin_client, escenario):
    """Una BD sin entorno resuelve a producción: el blueprint entero queda denegado."""
    for metodo, path, cuerpo in _rutas(escenario["sin_clasificar"]):
        r = _llamar(admin_client, metodo, path, cuerpo)
        assert _forbidden(r), f"{metodo} {path} -> {r.status_code}"


def test_development_only_blueprint_passes_layer_two(admin_client, escenario):
    for metodo, path, cuerpo in _rutas(escenario["solo_dev"]):
        r = _llamar(admin_client, metodo, path, cuerpo)
        # Lo que pase después es del motor (inexistente acá): solo importa que no sea la capa 2.
        assert not _forbidden(r), f"{metodo} {path} -> {r.status_code}: {r.text}"


def test_zero_database_blueprint_is_authorized_by_the_base_role(admin_client, escenario):
    """Sin BDs no hay escritura remota: decide el rol base (owner), no el entorno más protegido."""
    for metodo, path, cuerpo in _rutas(escenario["vacio"]):
        r = _llamar(admin_client, metodo, path, cuerpo)
        assert not _forbidden(r), f"{metodo} {path} -> {r.status_code}: {r.text}"


def test_zero_database_blueprint_still_needs_the_base_role_capability(
    admin_client, escenario
):
    """El rol base sigue mandando: un base viewer no pasa ni la capa 1 aunque el blueprint esté vacío."""
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET gateway_role = 'viewer' WHERE username = 'admin'"))
    r = admin_client.post(
        f"{_API}/{escenario['vacio']}/rename-slug/plan", json={"new_slug": "otro-slug"}
    )
    assert _forbidden(r)


def test_nonexistent_blueprint_is_forbidden_not_404(admin_client, escenario):
    for metodo, path, cuerpo in _rutas(999999):
        r = _llamar(admin_client, metodo, path, cuerpo)
        assert _forbidden(r), f"{metodo} {path} -> {r.status_code}"


def test_refresh_is_blueprint_wide_not_per_item(admin_client, escenario):
    """Decisión de ronda 1/3: refresh es todo-o-nada por el entorno más protegido."""
    r = admin_client.post(f"{_API}/{escenario['mixto']}/databases/refresh")
    assert _forbidden(r)
