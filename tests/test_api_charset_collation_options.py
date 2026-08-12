"""
Catálogo GLOBAL de charsets/collations:
- lógica pura (mapeo dialecto→familia, normalización, validación de altas),
- API del catálogo (listar / crear custom / habilitar / marcar default),
- ENFORCEMENT en la creación de BDs por los DOS caminos (adapter directo con
  ``register=false`` y ``ManagedDatabaseController`` con ``register=true``).

Adapter mockeado (sin motor real), mismo patrón que test_api_engine_users.py.
"""

import pytest

import app.controllers.managed_database_controller as mdc
import app.controllers.server_database_controller as sdc
from app.exceptions import AppHttpException
from app.services import charset_catalog


# ------------------------------ lógica pura -------------------------------- #
def test_engine_family_mapping():
    # MySQL y MariaDB comparten catálogo → MISMA familia.
    assert charset_catalog.engine_family("mysql") == "mysql"
    assert charset_catalog.engine_family("mariadb") == "mysql"
    assert charset_catalog.engine_family("postgresql") == "postgresql"


def test_engine_family_unknown_is_fail_closed():
    with pytest.raises(AppHttpException) as exc:
        charset_catalog.engine_family("oracle")
    assert exc.value.status_code == 500


def test_seed_rows_have_no_duplicates_and_single_default_per_family():
    rows = charset_catalog.charset_option_seed_rows()
    keys = [(r["engine_family"], r["charset"], r["collation"]) for r in rows]
    assert len(keys) == len(set(keys))
    for family in ("mysql", "postgresql"):
        defaults = [
            r for r in rows if r["engine_family"] == family and r["is_default"]
        ]
        assert len(defaults) == 1, f"{family} debe tener exactamente un default"
        # Un default debe estar habilitado (si no, la UI ofrecería algo que el guard rechaza).
        assert defaults[0]["enabled"]


def test_validate_option_values_rejects_injection_attempts():
    for bad in ("utf8mb4; DROP DATABASE x", "utf8mb4'", "utf8mb4 COLLATE y", ""):
        with pytest.raises(AppHttpException) as exc:
            charset_catalog.validate_option_values("mysql", bad, None)
        assert exc.value.status_code == 422

    # La collation de MySQL es un identificador: no admite puntos ni guiones…
    with pytest.raises(AppHttpException):
        charset_catalog.validate_option_values("mysql", "utf8mb4", "en_US.UTF-8")
    # …pero en PostgreSQL es un LOCALE del SO y sí los lleva.
    assert charset_catalog.validate_option_values(
        "postgresql", "UTF8", "en_US.UTF-8"
    ) == ("UTF8", "en_US.UTF-8")
    # Sin collation → centinela "" (NOT NULL en la tabla, para que la UNIQUE funcione).
    assert charset_catalog.validate_option_values("mysql", "utf8mb4", None) == (
        "utf8mb4",
        "",
    )


# ------------------------------ API del catálogo ---------------------------- #
def test_requires_auth(client):
    assert client.get("/api/v1/charset-collation-options").status_code == 401


def test_list_seeded_and_filters(admin_client):
    r = admin_client.get("/api/v1/charset-collation-options")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert {row["engine_family"] for row in rows} == {"mysql", "postgresql"}

    r = admin_client.get(
        "/api/v1/charset-collation-options",
        params={"engine_family": "mysql", "only_enabled": True},
    )
    enabled = r.json()["data"]
    combos = {(row["charset"], row["collation"]) for row in enabled}
    assert ("utf8mb4", "utf8mb4_unicode_ci") in combos
    # latin1 se siembra DESHABILITADA (referencia, no oferta).
    assert all(row["charset"] != "latin1" for row in enabled)
    assert sum(1 for row in enabled if row["is_default"]) == 1


def test_invalid_engine_family_422(admin_client):
    r = admin_client.get(
        "/api/v1/charset-collation-options", params={"engine_family": "oracle"}
    )
    assert r.status_code == 422


def test_create_custom_option_and_duplicate_conflict(admin_client):
    payload = {
        "engine_family": "mysql",
        "charset": "utf8mb4",
        "collation": "utf8mb4_bin",
    }
    r = admin_client.post("/api/v1/charset-collation-options", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    # Nace DESHABILITADA salvo pedido explícito.
    assert data["enabled"] is False and data["is_default"] is False

    # Duplicado exacto → 409 (la allowlist no admite filas repetidas).
    assert admin_client.post("/api/v1/charset-collation-options", json=payload).status_code == 409


def test_create_custom_option_rejects_bad_charset(admin_client):
    r = admin_client.post(
        "/api/v1/charset-collation-options",
        json={"engine_family": "mysql", "charset": "utf8mb4; DROP DATABASE x"},
    )
    assert r.status_code == 422


def test_patch_enable_and_default_moves_within_family(admin_client):
    rows = admin_client.get(
        "/api/v1/charset-collation-options", params={"engine_family": "mysql"}
    ).json()["data"]
    previous_default = next(row for row in rows if row["is_default"])
    other = next(
        row
        for row in rows
        if not row["is_default"] and row["collation"] == "utf8mb4_general_ci"
    )

    r = admin_client.patch(
        f"/api/v1/charset-collation-options/{other['id']}",
        json={"is_default": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["is_default"] is True

    rows = admin_client.get(
        "/api/v1/charset-collation-options", params={"engine_family": "mysql"}
    ).json()["data"]
    assert sum(1 for row in rows if row["is_default"]) == 1
    assert next(row for row in rows if row["id"] == previous_default["id"])["is_default"] is False
    # El default de PostgreSQL no se tocó (el invariante es POR familia).
    pg = admin_client.get(
        "/api/v1/charset-collation-options", params={"engine_family": "postgresql"}
    ).json()["data"]
    assert sum(1 for row in pg if row["is_default"]) == 1


def test_default_must_stay_enabled(admin_client):
    rows = admin_client.get(
        "/api/v1/charset-collation-options", params={"engine_family": "mysql"}
    ).json()["data"]
    current_default = next(row for row in rows if row["is_default"])
    r = admin_client.patch(
        f"/api/v1/charset-collation-options/{current_default['id']}",
        json={"enabled": False},
    )
    assert r.status_code == 422

    disabled = next(row for row in rows if not row["enabled"])
    r = admin_client.patch(
        f"/api/v1/charset-collation-options/{disabled['id']}",
        json={"is_default": True},
    )
    assert r.status_code == 422


def test_patch_unknown_id_404(admin_client):
    r = admin_client.patch(
        "/api/v1/charset-collation-options/999999", json={"enabled": True}
    )
    assert r.status_code == 404


def test_patch_without_fields_422(admin_client):
    r = admin_client.patch("/api/v1/charset-collation-options/1", json={})
    assert r.status_code == 422


# --------------------------- enforcement en create -------------------------- #
class _FakeAdapter:
    """Registra con qué charset/collation se habría emitido el CREATE DATABASE."""

    def __init__(self, dialect="mysql"):
        self.dialect = dialect
        self.calls = []

    def create_database(self, db_name, charset=None, collation=None, owner=None):
        self.calls.append((db_name, charset, collation, owner))


def _server(admin_client, port, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv-cc-{port}",
            "host": "10.0.0.5",
            "port": port,
            "engine": engine,
            "root_username": "root",
            "root_password": "rootpw",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _owner(admin_client, server_id, username="ccowner") -> int:
    return admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    ).json()["data"]["id"]


def test_create_database_disabled_combination_is_rejected(admin_client, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3401)

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={"name": "legacy_db", "charset": "latin1", "collation": "latin1_swedish_ci"},
    )
    assert r.status_code == 422, r.text
    # Fail-closed: el motor NUNCA se tocó.
    assert adapter.calls == []
    # El operador debe poder ver qué SÍ puede elegir (public_context viaja en producción).
    public = r.json()["detail"]["public_context"]
    assert public["engine_family"] == "mysql"
    assert any(c["charset"] == "utf8mb4" for c in public["allowed"])


def test_create_database_enabled_combination_passes_canonical(admin_client, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3402)

    # Mayúsculas: el charset/collation de MySQL es case-insensitive, y lo que llega al DDL
    # es la forma CANÓNICA del catálogo, no el texto del request.
    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={"name": "ok_db", "charset": "UTF8MB4", "collation": "UTF8MB4_UNICODE_CI"},
    )
    assert r.status_code == 201, r.text
    assert adapter.calls == [("ok_db", "utf8mb4", "utf8mb4_unicode_ci", None)]


def test_create_database_without_charset_is_never_blocked(admin_client, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3403)

    r = admin_client.post(f"/api/v1/servers/{sid}/databases", json={"name": "plain_db"})
    assert r.status_code == 201, r.text
    # Sin elección explícita, el adapter aplica su propio default.
    assert adapter.calls == [("plain_db", None, None, None)]


def test_create_database_charset_only_allowed_if_some_combination_uses_it(
    admin_client, monkeypatch
):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3404)

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases", json={"name": "cs_db", "charset": "utf8mb4"}
    )
    assert r.status_code == 201, r.text
    # No se rellena la collation que el caller no pidió.
    assert adapter.calls == [("cs_db", "utf8mb4", None, None)]

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases", json={"name": "cs_bad", "charset": "latin1"}
    )
    assert r.status_code == 422


def test_create_database_collation_only_is_validated(admin_client, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3405)

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={"name": "co_bad", "collation": "latin1_swedish_ci"},
    )
    assert r.status_code == 422
    assert adapter.calls == []


def test_register_path_also_enforces_catalog(admin_client, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    monkeypatch.setattr(mdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3406)
    oid = _owner(admin_client, sid)

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={
            "name": "reg_bad",
            "charset": "latin1",
            "collation": "latin1_swedish_ci",
            "register": True,
            "owner_id": oid,
        },
    )
    assert r.status_code == 422, r.text
    assert adapter.calls == []
    # No debe quedar inventario colgado de un create rechazado.
    listed = admin_client.get(
        "/api/v1/managed-databases", params={"server_id": sid}
    ).json()["data"]
    assert all(row["name"] != "reg_bad" for row in listed)

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={
            "name": "reg_ok",
            "charset": "utf8mb4",
            "collation": "utf8mb4_unicode_ci",
            "register": True,
            "owner_id": oid,
        },
    )
    assert r.status_code == 201, r.text
    assert adapter.calls == [("reg_ok", "utf8mb4", "utf8mb4_unicode_ci", "ccowner")]


def test_managed_databases_endpoint_enforces_catalog(admin_client, monkeypatch):
    """El otro entrypoint del mismo controller (POST /managed-databases) también valida."""
    adapter = _FakeAdapter()
    monkeypatch.setattr(mdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3407)
    oid = _owner(admin_client, sid)

    r = admin_client.post(
        "/api/v1/managed-databases",
        json={
            "server_id": sid,
            "owner_id": oid,
            "name": "md_bad",
            "charset": "latin1",
            "collation": "latin1_swedish_ci",
        },
    )
    assert r.status_code == 422, r.text
    assert adapter.calls == []


def test_postgresql_locale_from_catalog_is_accepted(admin_client, monkeypatch):
    adapter = _FakeAdapter(dialect="postgresql")
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 5470, engine="postgresql")

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={"name": "pg_db", "charset": "UTF8", "collation": "en_US.UTF-8"},
    )
    assert r.status_code == 201, r.text
    assert adapter.calls == [("pg_db", "UTF8", "en_US.UTF-8", None)]

    # El locale de PostgreSQL viaja al DDL como LITERAL: uno que no está en el catálogo
    # (aunque exista en el SO) se rechaza antes de tocar el motor.
    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={"name": "pg_bad", "charset": "UTF8", "collation": "es_AR.UTF-8"},
    )
    assert r.status_code == 422
    assert len(adapter.calls) == 1


def test_enabling_a_combination_makes_it_usable(admin_client, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(sdc, "get_adapter", lambda target: adapter)
    sid = _server(admin_client, 3408)

    rows = admin_client.get(
        "/api/v1/charset-collation-options", params={"engine_family": "mysql"}
    ).json()["data"]
    latin1 = next(row for row in rows if row["charset"] == "latin1")
    assert (
        admin_client.patch(
            f"/api/v1/charset-collation-options/{latin1['id']}", json={"enabled": True}
        ).status_code
        == 200
    )

    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases",
        json={"name": "now_ok", "charset": "latin1", "collation": "latin1_swedish_ci"},
    )
    assert r.status_code == 201, r.text
    assert adapter.calls == [("now_ok", "latin1", "latin1_swedish_ci", None)]
