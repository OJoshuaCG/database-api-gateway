"""
``environments.write``: los datos de política de entornos son solo de ``security_officer``.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Tres escrituras deciden qué barreras existen: el CRUD de ``/environments`` (flags como
``blocks_destructive_migrations``), ``PUT /{id}/agent-access`` (qué BDs ve un agente) y
reclasificar una BD (``PATCH environment_id``: mover producción a desarrollo la desprotege).
Antes las cubría ``gateway.admin`` —que también tiene ``access_admin``, el rol que administra
USUARIOS— o ``databases.write`` (``operator``). Acá se verifica que ya no.

Sin ``security_officer`` asignado esas escrituras quedan BLOQUEADAS: no hay fallback ni
bootstrap-admin. Y alta/adopción sin ``environment_id`` caen en el entorno ACTIVO más protegido,
no en ``is_default``.

El rol del admin sembrado se cambia directo en la BD (el rol no vive en la cookie: se relee en
cada request), igual que ``test_authz_payload_guards``.
"""

import pytest
from sqlalchemy import text

import app.controllers.managed_database_controller as mdc
from app.core.database import Database
from tests.scope_helpers import env_id, otorgar, sembrar_bd

_ROUTE = "/api/v1/environments"


def _set_actor(role: str, globals_: tuple[str, ...] = ()) -> None:
    """El admin sembrado pasa a tener ``role`` y exactamente estas capacidades globales."""
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )
        conn.execute(text("DELETE FROM user_global_capabilities WHERE user_id = 1"))
        for g in globals_:
            conn.execute(
                text(
                    "INSERT INTO user_global_capabilities (user_id, capability, created_at, "
                    "updated_at) VALUES (1, :c, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"c": g},
            )


def _env_column(db_id: int) -> int | None:
    with Database().engine.begin() as conn:
        return conn.execute(
            text("SELECT environment_id FROM managed_databases WHERE id = :i"), {"i": db_id}
        ).scalar()


_NEW_ENV = {"name": "Preprod", "slug": "preprod", "rank": 25}


# --------------------------------------------------------------------------- #
# CRUD de /environments                                                        #
# --------------------------------------------------------------------------- #


def test_access_admin_keeps_read_but_cannot_write_environments(admin_client):
    """``access_admin`` ve los entornos (``environments.read``) y no puede tocarlos."""
    prod = env_id("production")
    _set_actor("viewer", ("access_admin",))

    assert admin_client.get(_ROUTE).status_code == 200
    assert admin_client.get(f"{_ROUTE}/{prod}").status_code == 200
    assert admin_client.post(_ROUTE, json=_NEW_ENV).status_code == 403
    r = admin_client.patch(f"{_ROUTE}/{prod}", json={"color": "#ff0000"})
    assert r.status_code == 403
    assert admin_client.delete(f"{_ROUTE}/{prod}").status_code == 403


def test_the_forbidden_body_does_not_name_the_missing_capability(admin_client):
    _set_actor("owner", ("access_admin",))
    r = admin_client.post(_ROUTE, json=_NEW_ENV)
    assert r.status_code == 403
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    assert "environments.write" not in r.text


@pytest.mark.parametrize("role", ["operator", "owner"])
def test_operational_roles_cannot_write_environments(admin_client, role):
    _set_actor(role)
    assert admin_client.post(_ROUTE, json=_NEW_ENV).status_code == 403


def test_without_a_security_officer_environment_writes_stay_blocked(admin_client):
    """
    No hay fallback: con el último ``security_officer`` retirado, ni siquiera quien administra
    el gateway (``access_admin`` + ``owner``) escribe entornos hasta que alguien lo asigne.
    """
    _set_actor("owner", ("access_admin",))
    assert admin_client.post(_ROUTE, json=_NEW_ENV).status_code == 403
    _set_actor("owner", ("access_admin", "security_officer"))
    assert admin_client.post(_ROUTE, json=_NEW_ENV).status_code == 201


def test_security_officer_can_create_patch_and_delete_environments(admin_client):
    _set_actor("viewer", ("security_officer",))
    created = admin_client.post(_ROUTE, json=_NEW_ENV)
    assert created.status_code == 201, created.text
    eid = created.json()["data"]["id"]
    assert admin_client.patch(f"{_ROUTE}/{eid}", json={"color": "#00ff00"}).status_code == 200
    assert admin_client.delete(f"{_ROUTE}/{eid}").status_code == 200


# --------------------------------------------------------------------------- #
# PUT /managed-databases/{id}/agent-access                                     #
# --------------------------------------------------------------------------- #


def test_agent_access_is_security_officer_only(admin_client):
    db_id = sembrar_bd(environment_id=env_id("development"))
    url = f"/api/v1/managed-databases/{db_id}/agent-access"
    body = {"allowed": False, "blocked": True}

    _set_actor("owner", ("access_admin",))
    assert admin_client.put(url, json=body).status_code == 403

    _set_actor("viewer", ("security_officer",))
    assert admin_client.put(url, json=body).status_code == 200


# --------------------------------------------------------------------------- #
# PATCH environment_id: reclasificar                                           #
# --------------------------------------------------------------------------- #


def test_an_operator_cannot_reclassify_and_the_row_is_unchanged(admin_client):
    prod, dev = env_id("production"), env_id("development")
    db_id = sembrar_bd(environment_id=prod)
    _set_actor("operator")

    r = admin_client.patch(f"/api/v1/managed-databases/{db_id}", json={"environment_id": dev})
    assert r.status_code == 403
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    assert _env_column(db_id) == prod


def test_an_operator_cannot_unclassify_either(admin_client):
    prod = env_id("production")
    db_id = sembrar_bd(environment_id=prod)
    _set_actor("operator")
    r = admin_client.patch(f"/api/v1/managed-databases/{db_id}", json={"environment_id": None})
    assert r.status_code == 403
    assert _env_column(db_id) == prod


def test_resending_the_same_environment_is_not_a_change(admin_client):
    """El form de la SPA reenvía el entorno actual: un operador sigue editando notas."""
    prod = env_id("production")
    db_id = sembrar_bd(environment_id=prod)
    _set_actor("operator")
    r = admin_client.patch(
        f"/api/v1/managed-databases/{db_id}", json={"environment_id": prod, "notes": "n"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["notes"] == "n"
    assert _env_column(db_id) == prod


def test_security_officer_alone_is_enough_to_reclassify(admin_client):
    """
    Ronda 3: ``environments.write`` es SUFICIENTE; no se exige además ``databases.write`` EN la
    BD. El piso de la ruta (``operator``) lo da el rol base; el grant ``viewer`` sobre
    producción no bloquea la reclasificación del oficial.
    """
    prod, dev = env_id("production"), env_id("development")
    db_id = sembrar_bd(environment_id=prod)
    _set_actor("operator", ("security_officer",))
    otorgar("environment", prod, "viewer")

    r = admin_client.patch(f"/api/v1/managed-databases/{db_id}", json={"environment_id": dev})
    assert r.status_code == 200, r.text
    assert _env_column(db_id) == dev


# --------------------------------------------------------------------------- #
# Alta / adopción sin environment_id                                           #
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    def list_databases(self):
        return ["legacy"]


def _server_and_owner(admin_client, server_payload) -> tuple[int, int]:
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "own1"}
    ).json()["data"]["id"]
    return sid, oid


def test_create_without_environment_resolves_to_the_most_protected(
    admin_client, server_payload
):
    sid, oid = _server_and_owner(admin_client, server_payload)
    r = admin_client.post(
        "/api/v1/managed-databases", json={"name": "d1", "server_id": sid, "owner_id": oid}
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["environment_id"] == env_id("production")


def test_inactive_environments_are_skipped_when_resolving_the_default(
    admin_client, server_payload
):
    sid, oid = _server_and_owner(admin_client, server_payload)
    r = admin_client.patch(
        f"{_ROUTE}/{env_id('production')}",
        json={"is_active": False},
        params={"confirm_slug": "production"},
    )
    assert r.status_code == 200, r.text
    r = admin_client.post(
        "/api/v1/managed-databases", json={"name": "d2", "server_id": sid, "owner_id": oid}
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["environment_id"] == env_id("staging")


def test_a_dev_scoped_operator_cannot_create_without_declaring_the_environment(
    admin_client, server_payload
):
    """
    El agujero que esto cierra: omitir ``environment_id`` caía en ``development`` y un operador
    acotado a desarrollo creaba bases "de desarrollo" a voluntad. Ahora cae en producción,
    donde su rol base (``viewer``) no alcanza.
    """
    sid, oid = _server_and_owner(admin_client, server_payload)
    _set_actor("viewer")
    otorgar("environment", env_id("development"), "operator")

    r = admin_client.post(
        "/api/v1/managed-databases", json={"name": "d3", "server_id": sid, "owner_id": oid}
    )
    assert r.status_code == 403
    # Declarando el entorno donde SÍ opera, el alta pasa.
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={
            "name": "d3",
            "server_id": sid,
            "owner_id": oid,
            "environment_id": env_id("development"),
        },
    )
    assert r.status_code == 201, r.text


def test_a_dev_scoped_operator_cannot_adopt_without_declaring_the_environment(
    admin_client, server_payload, monkeypatch
):
    monkeypatch.setattr(mdc, "get_adapter", lambda target: _FakeAdapter())
    sid, oid = _server_and_owner(admin_client, server_payload)
    _set_actor("viewer")
    otorgar("environment", env_id("development"), "operator")

    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={"name": "legacy", "server_id": sid, "owner_id": oid},
    )
    assert r.status_code == 403
    with Database().engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM managed_databases")).scalar() == 0


# --------------------------------------------------------------------------- #
# Adopt con model_version                                                      #
# --------------------------------------------------------------------------- #


def test_adopt_with_model_version_needs_blueprints_apply_and_stamps_nothing(
    admin_client, server_payload, monkeypatch
):
    """
    ``model_version`` hace ``stamp`` en el motor y escribe la caché que leen los gates de
    promoción: es ``blueprints.apply`` disfrazado de alta. Un operador (que no lo tiene) recibe
    403 ANTES de que se toque el motor o se inserte nada.
    """
    llamadas: list[str] = []

    class _Spy(_FakeAdapter):
        def list_databases(self):
            llamadas.append("list_databases")
            return super().list_databases()

    monkeypatch.setattr(mdc, "get_adapter", lambda target: _Spy())
    sid, oid = _server_and_owner(admin_client, server_payload)
    _set_actor("operator")

    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={
            "name": "legacy",
            "server_id": sid,
            "owner_id": oid,
            "model_id": 1,
            "model_version": "0001",
        },
    )
    assert r.status_code == 403
    assert llamadas == []
    with Database().engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM managed_databases")).scalar() == 0
