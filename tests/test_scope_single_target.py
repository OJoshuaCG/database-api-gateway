"""
Rutas de objetivo único con capa 2 (``require_at``): cada familia niega fuera de su alcance.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
``test_scope_registry`` ejerce TODAS las rutas con ids inexistentes (resuelven al entorno más
protegido). Eso prueba que el guard existe, no que apunte al destino CORRECTO: una ruta que
resolviera el entorno de otra fila también daría 403 con ids falsos. Acá se siembran destinos
reales en producción y en desarrollo y se verifica, por familia, que:

1. el actor restringido (``owner`` de base, ``viewer`` en producción) recibe 403 sobre producción;
2. sobre desarrollo la capa 2 lo deja pasar (cualquier status distinto de 403: lo que pase
   después es del motor, que acá no existe);
3. los escalamientos por payload (``drop_remote``, ``model_version``, ``data_tables``) se evalúan
   EN el destino con ``assert_at``, no con la capa 1 que usa el rol unión.

Ninguna prueba conecta a un motor real: la autorización resuelve antes de tocarlo.
"""

import pytest
from sqlalchemy import text

import app.controllers.managed_database_controller as mdc
from app.core.database import Database
from tests.scope_helpers import env_id, otorgar, sembrar_bd

_API = "/api/v1"


def _set_base(role: str) -> None:
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )


def _forbidden(r) -> bool:
    return (
        r.status_code == 403
        and r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    )


@pytest.fixture()
def escenario(admin_client, server_payload):
    """
    Un servidor con una BD en producción (``appprod``) y otra en desarrollo (``appdev``), un
    servidor solo-desarrollo y un usuario del motor en cada uno. Se siembra ANTES de otorgar
    los grants para que el alta use el rol completo del admin.
    """
    prod, dev = env_id("production"), env_id("development")
    sid = admin_client.post(f"{_API}/servers", json=server_payload()).json()["data"]["id"]
    sid_dev = admin_client.post(
        f"{_API}/servers", json=server_payload(name="srv-dev", port=3400)
    ).json()["data"]["id"]
    u_prod = admin_client.post(
        f"{_API}/server-users", json={"server_id": sid, "username": "uprod"}
    ).json()["data"]["id"]
    u_dev = admin_client.post(
        f"{_API}/server-users", json={"server_id": sid_dev, "username": "udev"}
    ).json()["data"]["id"]
    return {
        "prod": prod,
        "dev": dev,
        "sid": sid,
        "sid_dev": sid_dev,
        "db_prod": sembrar_bd(server_id=sid, environment_id=prod, name="appprod"),
        "db_dev": sembrar_bd(server_id=sid, environment_id=dev, name="appdev"),
        "db_solo_dev": sembrar_bd(server_id=sid_dev, environment_id=dev, name="onlydev"),
        "u_prod": u_prod,
        "u_dev": u_dev,
    }


def _restringir(esc: dict) -> None:
    """Base ``owner`` (la capa 1 pasa en todo) y ``viewer`` en producción."""
    otorgar("environment", esc["prod"], "viewer")


# --------------------------------------------------------------------------- #
# Familia 1: managed-databases sobre la fila (db_id)                            #
# --------------------------------------------------------------------------- #

_POR_DB = [
    ("PATCH", "/managed-databases/{db}", {"notes": "x"}),
    ("POST", "/managed-databases/{db}/reassign-owner", {"owner_id": 1}),
    ("POST", "/managed-databases/{db}/migrations/reconcile-partial?confirm_version=0001", {}),
    ("POST", "/managed-databases/{db}/migrations/stamp?version=0001", {}),
    ("GET", "/managed-databases/{db}/migrations/0001/select-results", None),
    ("DELETE", "/managed-databases/{db}/migrations/0001/select-results", None),
]


@pytest.mark.parametrize("method,path,body", _POR_DB)
def test_managed_database_routes_deny_on_a_production_row(
    admin_client, escenario, method, path, body
):
    _restringir(escenario)
    r = admin_client.request(
        method, _API + path.format(db=escenario["db_prod"]), json=body
    )
    assert _forbidden(r), f"{method} {path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("method,path,body", _POR_DB)
def test_managed_database_routes_pass_layer2_on_a_development_row(
    admin_client, escenario, method, path, body
):
    _restringir(escenario)
    r = admin_client.request(method, _API + path.format(db=escenario["db_dev"]), json=body)
    assert not _forbidden(r), f"{method} {path}: {r.status_code} {r.text}"


def test_patch_with_the_same_environment_still_checks_the_row_scope(admin_client, escenario):
    """Reenviar el entorno actual no es reclasificar, pero el alcance de la fila sigue rigiendo."""
    _restringir(escenario)
    r = admin_client.patch(
        f"{_API}/managed-databases/{escenario['db_prod']}",
        json={"environment_id": escenario["prod"]},
    )
    assert _forbidden(r)


# --------------------------------------------------------------------------- #
# Familia 2: alta y adopción (managed_create)                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ruta", ["/managed-databases", "/managed-databases/adopt"])
def test_create_and_adopt_deny_when_declaring_production(admin_client, escenario, ruta):
    _restringir(escenario)
    r = admin_client.post(
        _API + ruta,
        json={
            "name": "nueva",
            "server_id": escenario["sid_dev"],
            "owner_id": escenario["u_dev"],
            "environment_id": escenario["prod"],
        },
    )
    assert _forbidden(r)


@pytest.mark.parametrize("ruta", ["/managed-databases", "/managed-databases/adopt"])
def test_create_and_adopt_deny_when_the_environment_is_omitted(admin_client, escenario, ruta):
    """Omitir ``environment_id`` resuelve al activo más protegido (producción)."""
    _restringir(escenario)
    r = admin_client.post(
        _API + ruta,
        json={
            "name": "nueva",
            "server_id": escenario["sid_dev"],
            "owner_id": escenario["u_dev"],
        },
    )
    assert _forbidden(r)


def test_create_cannot_hide_a_production_server_behind_a_dev_declaration(
    admin_client, escenario
):
    """
    Peor caso entre el entorno declarado y el derivado del servidor: declarar ``development``
    sobre un servidor que ya aloja una BD de producción sería una reclasificación encubierta.
    """
    _restringir(escenario)
    r = admin_client.post(
        f"{_API}/managed-databases",
        json={
            "name": "encubierta",
            "server_id": escenario["sid"],
            "owner_id": escenario["u_prod"],
            "environment_id": escenario["dev"],
        },
    )
    assert _forbidden(r)


def test_create_in_development_on_a_development_server_passes_layer2(admin_client, escenario):
    _restringir(escenario)
    r = admin_client.post(
        f"{_API}/managed-databases",
        json={
            "name": "devnueva",
            "server_id": escenario["sid_dev"],
            "owner_id": escenario["u_dev"],
            "environment_id": escenario["dev"],
        },
    )
    assert r.status_code == 201, r.text


def test_a_malformed_create_body_fails_closed(admin_client, escenario):
    _restringir(escenario)
    r = admin_client.post(f"{_API}/managed-databases", content=b"not json")
    assert _forbidden(r)


# --------------------------------------------------------------------------- #
# Familia 3: /servers/{sid}/...                                                 #
# --------------------------------------------------------------------------- #

_POR_SERVIDOR = [
    ("POST", "/servers/{s}/users", {}),
    ("DELETE", "/servers/{s}/users?username=u&host=%25&confirm_username=u", None),
    ("PATCH", "/servers/{s}/users/password", {}),
    ("PATCH", "/servers/{s}/users/password-all-hosts", {}),
    ("POST", "/servers/{s}/users/add-host", {}),
    ("POST", "/servers/{s}/users/adopt-all-hosts", {}),
    ("POST", "/servers/{s}/users/define-password", {}),
    ("POST", "/servers/{s}/users/reveal-password", {}),
    ("POST", "/servers/{s}/databases", {"name": "n"}),
]


@pytest.mark.parametrize("method,path,body", _POR_SERVIDOR)
def test_server_level_routes_use_the_most_protected_env_among_inventoried_dbs(
    admin_client, escenario, method, path, body
):
    """El servidor ``sid`` aloja producción y desarrollo: manda producción. ``sid_dev`` pasa."""
    _restringir(escenario)
    r = admin_client.request(method, _API + path.format(s=escenario["sid"]), json=body)
    assert _forbidden(r), f"{method} {path}: {r.status_code}"
    r = admin_client.request(method, _API + path.format(s=escenario["sid_dev"]), json=body)
    assert not _forbidden(r), f"{method} {path}: {r.status_code} {r.text}"


def test_drop_database_routes_resolve_by_inventory_row(admin_client, escenario):
    """Sobre el servidor mixto, la fila de inventario decide: producción niega, desarrollo no."""
    _restringir(escenario)
    sid = escenario["sid"]
    r = admin_client.post(f"{_API}/servers/{sid}/databases/appprod/drop-preview")
    assert _forbidden(r)
    r = admin_client.request(
        "DELETE",
        f"{_API}/servers/{sid}/databases/appprod",
        json={"confirm_target_name": "appprod", "confirm_token": "x"},
    )
    assert _forbidden(r)
    r = admin_client.post(f"{_API}/servers/{sid}/databases/appdev/drop-preview")
    assert not _forbidden(r), r.text


def test_server_level_inventory_only_for_a_database_outside_the_inventory(
    admin_client, escenario
):
    """
    Escenario del spec: una base que el inventario no conoce resuelve por la regla del servidor
    (producción, la más protegida entre las inventariadas). No se lista el motor.
    """
    _restringir(escenario)
    sid = escenario["sid"]
    cuerpo = {"database": "noinventariada", "sql": "SELECT 1"}
    for ruta in ("preview", "execute"):
        r = admin_client.post(f"{_API}/servers/{sid}/query/{ruta}", json=cuerpo)
        assert _forbidden(r), ruta


def test_query_console_uses_the_inventory_row_of_the_payload_database(admin_client, escenario):
    _restringir(escenario)
    sid = escenario["sid"]
    r = admin_client.post(
        f"{_API}/servers/{sid}/query/execute", json={"database": "appprod", "sql": "SELECT 1"}
    )
    assert _forbidden(r)
    r = admin_client.post(
        f"{_API}/servers/{sid}/query/execute", json={"database": "appdev", "sql": "SELECT 1"}
    )
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Familia 4: /server-users                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ruta", ["", "/adopt", "/provision"])
def test_server_user_creation_routes_resolve_the_payload_server(
    admin_client, escenario, ruta
):
    _restringir(escenario)
    r = admin_client.post(
        f"{_API}/server-users{ruta}", json={"server_id": escenario["sid"], "username": "nuevo"}
    )
    assert _forbidden(r)
    r = admin_client.post(
        f"{_API}/server-users{ruta}", json={"server_id": escenario["sid_dev"], "username": "nuevo"}
    )
    assert not _forbidden(r), f"{ruta}: {r.status_code} {r.text}"


_POR_USUARIO = [
    ("PATCH", "/server-users/{u}", {"notes": "x"}),
    ("DELETE", "/server-users/{u}", None),
    ("POST", "/server-users/{u}/grants", {}),
    ("DELETE", "/server-users/{u}/grants", {}),
    ("POST", "/server-users/{u}/apply-profile/1", {}),
]


@pytest.mark.parametrize("method,path,body", _POR_USUARIO)
def test_server_user_routes_resolve_the_server_of_the_user_row(
    admin_client, escenario, method, path, body
):
    _restringir(escenario)
    r = admin_client.request(method, _API + path.format(u=escenario["u_prod"]), json=body)
    assert _forbidden(r), f"{method} {path}: {r.status_code}"
    r = admin_client.request(method, _API + path.format(u=escenario["u_dev"]), json=body)
    assert not _forbidden(r), f"{method} {path}: {r.status_code} {r.text}"


# --------------------------------------------------------------------------- #
# Familia 5: from-snapshot                                                      #
# --------------------------------------------------------------------------- #

_SNAPSHOT = {"name": "bp", "slug": "bp"}


def test_from_snapshot_resolves_the_source_server_and_database(admin_client, escenario):
    _restringir(escenario)
    r = admin_client.post(
        f"{_API}/database-models/from-snapshot",
        json={**_SNAPSHOT, "server_id": escenario["sid"], "database": "appprod"},
    )
    assert _forbidden(r)
    r = admin_client.post(
        f"{_API}/database-models/from-snapshot",
        json={**_SNAPSHOT, "server_id": escenario["sid_dev"], "database": "onlydev"},
    )
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Escalamientos por payload: assert_at, no la capa 1                            #
# --------------------------------------------------------------------------- #


def _operador_con_owner_en_dev(esc: dict) -> None:
    """
    Base ``operator`` y ``owner`` SOLO en desarrollo. El rol unión es ``owner`` (la capa 1 lo
    deja pasar en todo), pero en producción rige el base: ``operator``.
    """
    _set_base("operator")
    otorgar("environment", esc["dev"], "owner")


def test_drop_remote_is_checked_at_the_target_not_with_the_union_role(admin_client, escenario):
    _operador_con_owner_en_dev(escenario)
    r = admin_client.delete(
        f"{_API}/server-users/{escenario['u_prod']}",
        params={"drop_remote": "true", "confirm_username": "uprod"},
    )
    assert _forbidden(r)
    # Sin drop_remote es solo ``engine_users.write``: el operador base lo tiene en producción.
    r = admin_client.delete(f"{_API}/server-users/{escenario['u_prod']}")
    assert not _forbidden(r), r.text


def test_adopt_model_version_is_checked_at_the_target(admin_client, escenario, monkeypatch):
    class _Adapter:
        def list_databases(self):
            return ["legacy"]

    monkeypatch.setattr(mdc, "get_adapter", lambda target: _Adapter())
    _operador_con_owner_en_dev(escenario)
    cuerpo = {
        "name": "legacy",
        "server_id": escenario["sid_dev"],
        "owner_id": escenario["u_dev"],
        "model_id": 1,
        "model_version": "0001",
    }
    r = admin_client.post(
        f"{_API}/managed-databases/adopt", json={**cuerpo, "environment_id": escenario["prod"]}
    )
    assert _forbidden(r)
    r = admin_client.post(
        f"{_API}/managed-databases/adopt", json={**cuerpo, "environment_id": escenario["dev"]}
    )
    assert not _forbidden(r), r.text


def test_create_with_apply_migrations_is_checked_at_the_target(admin_client, escenario):
    _operador_con_owner_en_dev(escenario)
    r = admin_client.post(
        f"{_API}/managed-databases",
        json={
            "name": "conmig",
            "server_id": escenario["sid_dev"],
            "owner_id": escenario["u_dev"],
            "environment_id": escenario["prod"],
            "apply_migrations": True,
            "model_id": 1,
        },
    )
    assert _forbidden(r)


def test_from_snapshot_data_tables_need_captures_at_the_source(admin_client, escenario):
    _operador_con_owner_en_dev(escenario)
    cuerpo = {**_SNAPSHOT, "data_tables": [{"table": "catalogo"}]}
    r = admin_client.post(
        f"{_API}/database-models/from-snapshot",
        json={**cuerpo, "server_id": escenario["sid"], "database": "appprod"},
    )
    assert _forbidden(r)
    r = admin_client.post(
        f"{_API}/database-models/from-snapshot",
        json={**cuerpo, "server_id": escenario["sid_dev"], "database": "onlydev"},
    )
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Sin grants por alcance: nada cambia                                           #
# --------------------------------------------------------------------------- #


def test_without_scope_grants_the_routes_behave_as_before(admin_client, escenario):
    r = admin_client.patch(
        f"{_API}/managed-databases/{escenario['db_prod']}", json={"notes": "ok"}
    )
    assert r.status_code == 200, r.text
