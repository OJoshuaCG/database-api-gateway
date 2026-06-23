"""
Tests del catálogo de privilegios (tabla `privileges`, seed y API).

Incluye el test de CONSISTENCIA: todo token que la plataforma controla
(privileges.controlled_tokens) debe estar sembrado como ACTIVO y con descripción,
de modo que la tabla y el catálogo de validación nunca diverjan.
"""

import pytest

from app.services.db_admin.privileges import controlled_tokens, token_is_sensitive
from app.services.db_admin.privilege_seed import privilege_seed_rows

ENGINES = ("mysql", "mariadb", "postgresql")


# ------------------------------- seed (datos) -------------------------------- #
def test_seed_has_no_duplicates():
    rows = privilege_seed_rows()
    keys = [(r["engine"], r["name"]) for r in rows]
    assert len(keys) == len(set(keys)), "Hay (engine, name) duplicados en el seed"


@pytest.mark.parametrize("engine", ENGINES)
def test_controlled_tokens_are_active_with_description(engine):
    rows = [r for r in privilege_seed_rows() if r["engine"] == engine]
    active = {r["name"]: r for r in rows if r["is_active"]}
    for token in controlled_tokens(engine):
        assert token in active, f"{token} controlado pero no activo en seed ({engine})"
        assert active[token]["description"].strip(), f"{token} sin descripción ({engine})"


@pytest.mark.parametrize("engine", ENGINES)
def test_sensitive_flag_matches_validation(engine):
    for r in privilege_seed_rows():
        if r["engine"] == engine and r["is_active"]:
            assert r["is_sensitive"] == token_is_sensitive(engine, r["name"])


def test_inactive_admin_privs_present():
    # Existen pero no se controlan (los "de sobra"): activos=False, categoría admin.
    mysql_inactive = {
        r["name"] for r in privilege_seed_rows()
        if r["engine"] == "mysql" and not r["is_active"]
    }
    assert {"SUPER", "FILE", "CREATE USER"} <= mysql_inactive
    pg_inactive = {
        r["name"] for r in privilege_seed_rows()
        if r["engine"] == "postgresql" and not r["is_active"]
    }
    assert {"SUPERUSER", "CREATEROLE"} <= pg_inactive


# ----------------------------------- API ------------------------------------- #
def test_list_active_by_engine(admin_client):
    resp = admin_client.get("/api/v1/privileges", params={"engine": "mysql", "active": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    names = {p["name"] for p in data}
    assert "SELECT" in names and "CREATE VIEW" in names
    assert all(p["is_active"] for p in data)
    # Solo trae lo controlado (no los administrativos "de sobra").
    assert "SUPER" not in names
    assert len(data) == len(controlled_tokens("mysql"))


def test_list_includes_inactive_when_not_filtered(admin_client):
    resp = admin_client.get("/api/v1/privileges", params={"engine": "postgresql"})
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()["data"]}
    assert "SELECT" in names          # activo
    assert "SUPERUSER" in names       # inactivo, presente sin filtro


def test_invalid_engine_returns_422(admin_client):
    resp = admin_client.get("/api/v1/privileges", params={"engine": "oracle"})
    assert resp.status_code == 422


def test_requires_authentication(client):
    # Sin sesión de admin no se puede consultar el catálogo.
    resp = client.get("/api/v1/privileges", params={"engine": "mysql"})
    assert resp.status_code in (401, 403)


def test_toggle_activation_changes_listing(admin_client):
    # Tomar un privilegio activo de postgres y desactivarlo vía PATCH.
    listing = admin_client.get(
        "/api/v1/privileges", params={"engine": "postgresql", "active": True}
    ).json()["data"]
    target = next(p for p in listing if p["name"] == "TRUNCATE")
    patched = admin_client.patch(
        f"/api/v1/privileges/{target['id']}", json={"is_active": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["is_active"] is False
    # Ya no aparece entre los activos.
    after = admin_client.get(
        "/api/v1/privileges", params={"engine": "postgresql", "active": True}
    ).json()["data"]
    assert "TRUNCATE" not in {p["name"] for p in after}


def test_reseed_preserves_operator_toggle(admin_client):
    from app.services.privilege_catalog import list_privileges, seed_privileges, set_active

    target = list_privileges(engine="mysql", active=True)[0]
    set_active(target.id, False)
    seed_privileges()  # un reinicio no debe reactivar lo que el operador apagó
    refreshed = [p for p in list_privileges(engine="mysql") if p.id == target.id][0]
    assert refreshed.is_active is False
