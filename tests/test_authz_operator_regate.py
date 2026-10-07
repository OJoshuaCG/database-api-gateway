"""
Acciones de parque o irreversibles que el rol ``operator`` ya NO tiene, pero que se le pueden
otorgar a una persona puntual con una capacidad puntual (``capability_grants``).

Cada ruta re-gateada se prueba con los tres lados:

1. ``operator`` → 403 ``access.forbidden``;
2. ``owner`` → pasa la autorización;
3. ``operator`` + la capacidad nueva otorgada EN el alcance correcto → pasa. Es lo que prueba que
   la decisión "fuera del rol, otorgable puntualmente" se cumple, y no solo "fuera del rol".

Más el 409 ``database_model.in_use`` al borrar un blueprint referenciado.

Ninguna prueba conecta a un motor: pasada la autorización, ``remote_engine.get_engine`` falla
con un 502 controlado, y lo único que se mide es que la respuesta NO sea el 403 de autorización.
"""

import pytest
from sqlalchemy import text

from app.core import remote_engine
from app.core.database import Database
from app.exceptions import AppHttpException
from tests.scope_helpers import env_id, sembrar_bd
from tests.test_capability_grant_crud import _insert_cg

_MODELS = "/api/v1/database-models"
_MDB = "/api/v1/managed-databases"


def _forbidden(r) -> bool:
    if r.status_code != 403:
        return False
    detail = r.json().get("detail") or {}
    return (detail.get("public_context") or {}).get("code") == "access.forbidden"


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


def _rol(role: str) -> None:
    """Cambia el rol base del admin sembrado (user 1). El actor se resuelve en cada request."""
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )


def _otorgar_cg(capability: str, scope_type: str, scope_id: int) -> None:
    _insert_cg(1, capability, scope_type, scope_id)


def _blueprint(admin_client, slug: str) -> int:
    r = admin_client.post(_MODELS, json={"name": slug.upper(), "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _vincular(db_id: int, model_id: int) -> None:
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE managed_databases SET model_id = :m WHERE id = :d"),
            {"m": model_id, "d": db_id},
        )


@pytest.fixture()
def sin_motor(monkeypatch):
    def _boom(*args, **kwargs):
        raise AppHttpException("Motor no disponible en la prueba.", 502)

    monkeypatch.setattr(remote_engine, "get_engine", _boom)


@pytest.fixture()
def parque(admin_client, server_payload, sin_motor):
    """Un servidor, un blueprint con UNA BD en desarrollo, sembrado como owner."""
    dev, prod = env_id("development"), env_id("production")
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    mid = _blueprint(admin_client, "regate")
    db_id = sembrar_bd(server_id=sid, environment_id=dev, name="regate_dev")
    _vincular(db_id, mid)
    return {"sid": sid, "mid": mid, "db_id": db_id, "dev": dev, "prod": prod}


# --------------------------------------------------------------------------- #
# F-4: rename-slug y migrate-version-table → blueprints.apply                   #
# --------------------------------------------------------------------------- #

_F4 = [
    ("rename-slug", {"new_slug": "otro-slug"}),
    ("migrate-version-table", {}),
]


@pytest.mark.parametrize("ruta,cuerpo", _F4, ids=[r for r, _ in _F4])
def test_f4_operator_cannot_rewrite_version_tables(admin_client, parque, ruta, cuerpo):
    _rol("operator")
    r = admin_client.post(f"{_MODELS}/{parque['mid']}/{ruta}", json=cuerpo)
    assert _forbidden(r), r.text


@pytest.mark.parametrize("ruta,cuerpo", _F4, ids=[r for r, _ in _F4])
def test_f4_owner_passes(admin_client, parque, ruta, cuerpo):
    r = admin_client.post(f"{_MODELS}/{parque['mid']}/{ruta}", json=cuerpo)
    assert not _forbidden(r), r.text


@pytest.mark.parametrize("ruta,cuerpo", _F4, ids=[r for r, _ in _F4])
def test_f4_operator_with_apply_grant_in_the_environment_passes(
    admin_client, parque, ruta, cuerpo
):
    _rol("operator")
    _otorgar_cg("blueprints.apply", "environment", parque["dev"])
    r = admin_client.post(f"{_MODELS}/{parque['mid']}/{ruta}", json=cuerpo)
    assert not _forbidden(r), r.text


@pytest.mark.parametrize("ruta,cuerpo", _F4, ids=[r for r, _ in _F4])
def test_f4_apply_grant_in_another_environment_does_not_reach(
    admin_client, parque, ruta, cuerpo
):
    """La capa 2 sigue mandando: un CG en producción no alcanza un blueprint de desarrollo."""
    _rol("operator")
    _otorgar_cg("blueprints.apply", "environment", parque["prod"])
    r = admin_client.post(f"{_MODELS}/{parque['mid']}/{ruta}", json=cuerpo)
    assert _forbidden(r), r.text


@pytest.mark.parametrize(
    "ruta,cuerpo",
    [("rename-slug/plan", {"new_slug": "otro-slug"}), ("migrate-version-table/plan", None)],
    ids=["rename-slug-plan", "migrate-version-table-plan"],
)
def test_f4_plans_stay_on_blueprints_write(admin_client, parque, ruta, cuerpo):
    """Los ``/plan`` no escriben nada: el operator los sigue viendo."""
    _rol("operator")
    kwargs = {} if cuerpo is None else {"json": cuerpo}
    r = admin_client.post(f"{_MODELS}/{parque['mid']}/{ruta}", **kwargs)
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# F-5: DELETE /database-models/{id} → blueprints.apply + 409 in_use             #
# --------------------------------------------------------------------------- #


def test_f5_operator_cannot_delete_a_blueprint(admin_client):
    mid = _blueprint(admin_client, "borrable")
    _rol("operator")
    r = admin_client.delete(f"{_MODELS}/{mid}")
    assert _forbidden(r), r.text
    _rol("owner")
    assert admin_client.get(f"{_MODELS}/{mid}").status_code == 200


def test_f5_owner_deletes_an_unreferenced_blueprint(admin_client):
    mid = _blueprint(admin_client, "borrable")
    r = admin_client.delete(f"{_MODELS}/{mid}")
    assert r.status_code == 200, r.text
    assert admin_client.get(f"{_MODELS}/{mid}").status_code == 404


def test_f5_operator_with_apply_grant_deletes_an_unreferenced_blueprint(
    admin_client, server_payload
):
    mid = _blueprint(admin_client, "borrable")
    _rol("operator")
    _otorgar_cg("blueprints.apply", "environment", env_id("development"))
    r = admin_client.delete(f"{_MODELS}/{mid}")
    assert r.status_code == 200, r.text


def test_f5_blueprint_referenced_by_a_managed_database_is_409(admin_client, server_payload):
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    mid = _blueprint(admin_client, "en-uso")
    db_id = sembrar_bd(server_id=sid, environment_id=env_id("development"), name="usa_bp")
    _vincular(db_id, mid)

    r = admin_client.delete(f"{_MODELS}/{mid}")
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "database_model.in_use"
    assert pc["managed_database_count"] == 1
    assert pc["blocking_databases"] == [{"id": db_id, "name": "usa_bp"}]
    # Nada se borró: el blueprint y el vínculo de la BD siguen.
    assert admin_client.get(f"{_MODELS}/{mid}").status_code == 200
    with Database().engine.begin() as conn:
        fila = conn.execute(
            text("SELECT model_id FROM managed_databases WHERE id = :d"), {"d": db_id}
        ).fetchone()
    assert fila[0] == mid


# --------------------------------------------------------------------------- #
# F-6: blueprint-version de un lote de collation → + blueprints.apply           #
# --------------------------------------------------------------------------- #


def _version_de_lote(admin_client, mid):
    return admin_client.post(
        f"{_MODELS}/{mid}/collation-conversions/1/blueprint-version", json={"name": "v"}
    )


def test_f6_operator_cannot_register_a_collation_version(admin_client, parque):
    _rol("operator")
    assert _forbidden(_version_de_lote(admin_client, parque["mid"]))


def test_f6_collation_execute_grant_alone_is_not_enough(admin_client, parque):
    """Sin ``blueprints.apply`` no se stampea: el CG de collation solo no alcanza."""
    _rol("operator")
    _otorgar_cg("collation.execute", "environment", parque["dev"])
    assert _forbidden(_version_de_lote(admin_client, parque["mid"]))


def test_f6_operator_with_collation_and_apply_grants_passes(admin_client, parque):
    _rol("operator")
    _otorgar_cg("collation.execute", "environment", parque["dev"])
    _otorgar_cg("blueprints.apply", "environment", parque["dev"])
    r = _version_de_lote(admin_client, parque["mid"])
    assert not _forbidden(r), r.text


def test_f6_owner_passes(admin_client, parque):
    r = _version_de_lote(admin_client, parque["mid"])
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# F-10: reassign-owner?provision=true → databases.drop                          #
# --------------------------------------------------------------------------- #


def _reasignar(admin_client, db_id, provision: bool):
    return admin_client.post(
        f"{_MDB}/{db_id}/reassign-owner",
        params={"provision": str(provision).lower()},
        json={"owner_id": 999999},
    )


def test_f10_operator_cannot_reassign_with_provision(admin_client, parque):
    _rol("operator")
    assert _forbidden(_reasignar(admin_client, parque["db_id"], True))


def test_f10_operator_keeps_the_inventory_only_reassign(admin_client, parque):
    """Sin ``provision`` solo cambia la fila del gateway: sigue en ``databases.write``."""
    _rol("operator")
    r = _reasignar(admin_client, parque["db_id"], False)
    assert not _forbidden(r), r.text


def test_f10_owner_passes_with_provision(admin_client, parque):
    r = _reasignar(admin_client, parque["db_id"], True)
    assert not _forbidden(r), r.text


def test_f10_operator_with_drop_grant_on_the_database_environment_passes(
    admin_client, parque
):
    _rol("operator")
    _otorgar_cg("databases.drop", "environment", parque["dev"])
    # Desde la partición ``engine_users.grant_admin``, entregar el control de la base con
    # ``provision`` pide también esa capacidad: el grant de ``databases.drop`` solo ya no alcanza
    # (ver ``tests/test_engine_users_grant_admin.py``).
    _otorgar_cg("engine_users.grant_admin", "environment", parque["dev"])
    r = _reasignar(admin_client, parque["db_id"], True)
    assert not _forbidden(r), r.text
    assert _code(r) != "engine_user.grant_admin_required", r.text


# --------------------------------------------------------------------------- #
# F-11: purga de capturas → blueprints.captures                                 #
# --------------------------------------------------------------------------- #


def _purgar(admin_client, db_id):
    return admin_client.delete(f"{_MDB}/{db_id}/migrations/0001/select-results")


def test_f11_operator_cannot_purge_captures(admin_client, parque):
    _rol("operator")
    assert _forbidden(_purgar(admin_client, parque["db_id"]))


def test_f11_owner_purges(admin_client, parque):
    r = _purgar(admin_client, parque["db_id"])
    assert not _forbidden(r), r.text


def test_f11_operator_with_captures_grant_purges(admin_client, parque):
    _rol("operator")
    _otorgar_cg("blueprints.captures", "environment", parque["dev"])
    r = _purgar(admin_client, parque["db_id"])
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# collation.execute → solo owner; las lecturas siguen en collation.read          #
# --------------------------------------------------------------------------- #


def _crear_conversion(admin_client, sid):
    return admin_client.post(
        f"/api/v1/servers/{sid}/databases/regate_dev/collation-conversions",
        json={"target_charset": "utf8mb4", "target_collation": "utf8mb4_unicode_ci"},
    )


def test_collation_operator_cannot_plan_a_conversion(admin_client, parque):
    _rol("operator")
    assert _forbidden(_crear_conversion(admin_client, parque["sid"]))


def test_collation_operator_with_execute_grant_on_the_server_passes(admin_client, parque):
    _rol("operator")
    _otorgar_cg("collation.execute", "server", parque["sid"])
    r = _crear_conversion(admin_client, parque["sid"])
    assert not _forbidden(r), r.text


def test_collation_operator_keeps_the_reads(admin_client, parque):
    _rol("operator")
    for path in (
        "/api/v1/collation-conversions/999999",
        f"{_MODELS}/{parque['mid']}/collation-conversions/999999",
        f"{_MODELS}/{parque['mid']}/collation-drift",
    ):
        r = admin_client.get(path)
        assert not _forbidden(r), f"{path} -> {r.status_code}: {r.text}"
