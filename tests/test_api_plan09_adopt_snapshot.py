"""
Tests del Plan 09: reconciliación (drift), adopción de BDs/usuarios existentes,
snapshot estructural y creación de blueprint baseline desde snapshot.

El motor se mockea (FakeAdapter) igual que el resto de la suite: SQLite como BD de
metadatos del gateway + adapter falso para el plano "en vivo".
"""

import app.controllers.managed_database_controller as mdc
import app.controllers.server_controller as sc
import app.controllers.server_user_controller as suc
from app.services.db_admin.dtos import DumpStatement, EngineUserInfo, StructureDump


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _make_server(admin_client, server_payload, **overrides) -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _make_user(admin_client, server_id, username="owner1", host="%") -> int:
    r = admin_client.post(
        "/api/v1/server-users",
        json={"server_id": server_id, "username": username, "host": host},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _make_managed(admin_client, server_id, owner_id, name, model_id=None) -> int:
    body = {"name": name, "server_id": server_id, "owner_id": owner_id}
    if model_id is not None:
        body["model_id"] = model_id
    r = admin_client.post("/api/v1/managed-databases", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


class _FakeAdapter:
    def __init__(self, *, dbs=None, users=None, dump=None):
        self._dbs = dbs or []
        self._users = users or []
        self._dump = dump

    def list_databases(self):
        return list(self._dbs)

    def list_users(self):
        return list(self._users)

    def dump_structure(self, database):
        return self._dump


# --------------------------------------------------------------------------- #
# Reconcile                                                                    #
# --------------------------------------------------------------------------- #
def test_reconcile_classifies_managed_unmanaged_orphan(
    admin_client, server_payload, monkeypatch
):
    sid = _make_server(admin_client, server_payload)
    owner = _make_user(admin_client, sid, "owner1")
    _make_managed(admin_client, sid, owner, "alpha")   # managed (también en vivo)
    _make_managed(admin_client, sid, owner, "gamma")   # orphan (no en vivo)

    fake = _FakeAdapter(
        dbs=["alpha", "beta"],  # beta solo en motor → unmanaged
        users=[EngineUserInfo(username="dba", host="%")],  # solo motor → unmanaged
    )
    monkeypatch.setattr(sc, "get_adapter", lambda target: fake)

    r = admin_client.get(f"/api/v1/servers/{sid}/reconcile")
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    by_name = {d["name"]: d["state"] for d in data["databases"]}
    assert by_name == {"alpha": "managed", "beta": "unmanaged", "gamma": "orphan"}

    by_user = {u["username"]: u["state"] for u in data["users"]}
    assert by_user["dba"] == "unmanaged"      # en motor, no en inventario
    assert by_user["owner1"] == "orphan"      # en inventario, no en motor


# --------------------------------------------------------------------------- #
# Adopt database                                                               #
# --------------------------------------------------------------------------- #
def test_adopt_database_success_then_conflict_and_404(
    admin_client, server_payload, monkeypatch
):
    sid = _make_server(admin_client, server_payload)
    owner = _make_user(admin_client, sid, "owner1")

    monkeypatch.setattr(
        mdc, "get_adapter", lambda target: _FakeAdapter(dbs=["legacy_crm"])
    )

    # 1) Adopción correcta
    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={"name": "legacy_crm", "server_id": sid, "owner_id": owner},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["origin"] == "adopted"
    assert data["status"] == "active"

    # 2) Idempotencia → 409
    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={"name": "legacy_crm", "server_id": sid, "owner_id": owner},
    )
    assert r.status_code == 409, r.text

    # 3) No existe en el motor → 404
    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={"name": "ghost_db", "server_id": sid, "owner_id": owner},
    )
    assert r.status_code == 404, r.text


def test_adopt_database_owner_wrong_server_409(
    admin_client, server_payload, monkeypatch
):
    sid1 = _make_server(admin_client, server_payload, name="srv-1", port=3401)
    sid2 = _make_server(admin_client, server_payload, name="srv-2", port=3402)
    owner_on_2 = _make_user(admin_client, sid2, "owner2")

    monkeypatch.setattr(mdc, "get_adapter", lambda target: _FakeAdapter(dbs=["x"]))
    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={"name": "x", "server_id": sid1, "owner_id": owner_on_2},
    )
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# Adopt database + stamp-on-adopt (model_id/model_version)                    #
# --------------------------------------------------------------------------- #
def _blueprint_with_migration(admin_client, slug="adopt-bp"):
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m1", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 201, r.text
    return model_id


def test_adopt_database_with_model_version_stamps_it(
    admin_client, server_payload, monkeypatch
):
    from app.services.db_admin.migrations import MigrationRunner

    sid = _make_server(admin_client, server_payload, name="srv-stamp-a", port=3421)
    owner = _make_user(admin_client, sid, "owner1")
    model_id = _blueprint_with_migration(admin_client, slug="adopt-bp-a")

    monkeypatch.setattr(mdc, "get_adapter", lambda target: _FakeAdapter(dbs=["legacy_a"]))
    monkeypatch.setattr(MigrationRunner, "stamp", lambda self, *a, **k: None)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0001")

    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={
            "name": "legacy_a",
            "server_id": sid,
            "owner_id": owner,
            "model_id": model_id,
            "model_version": "0001",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["model_id"] == model_id
    # El controller relee y re-serializa tras el stamp: model_version queda seteado
    # al valor stampeado (get_current_version mockeado a "0001").
    assert data["model_version"] == "0001"

    r = admin_client.get(f"/api/v1/managed-databases/{data['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["model_version"] == "0001"


def test_adopt_database_model_version_without_model_id_422(
    admin_client, server_payload, monkeypatch
):
    sid = _make_server(admin_client, server_payload, name="srv-stamp-b", port=3422)
    owner = _make_user(admin_client, sid, "owner1")
    monkeypatch.setattr(mdc, "get_adapter", lambda target: _FakeAdapter(dbs=["legacy_b"]))

    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={
            "name": "legacy_b",
            "server_id": sid,
            "owner_id": owner,
            "model_version": "0001",
        },
    )
    assert r.status_code == 422, r.text
    assert "model_id" in r.text.lower()

    # La BD NO quedó registrada: no aparece en el listado del servidor.
    r = admin_client.get(f"/api/v1/managed-databases?server_id={sid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


def test_adopt_database_model_version_not_in_blueprint_422(
    admin_client, server_payload, monkeypatch
):
    sid = _make_server(admin_client, server_payload, name="srv-stamp-c", port=3423)
    owner = _make_user(admin_client, sid, "owner1")
    model_id = _blueprint_with_migration(admin_client, slug="adopt-bp-c")
    monkeypatch.setattr(mdc, "get_adapter", lambda target: _FakeAdapter(dbs=["legacy_c"]))

    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={
            "name": "legacy_c",
            "server_id": sid,
            "owner_id": owner,
            "model_id": model_id,
            "model_version": "9999",  # no existe en el blueprint
        },
    )
    assert r.status_code == 422, r.text

    # No quedó registrada en el inventario (la validación corre ANTES del insert).
    r = admin_client.get(f"/api/v1/managed-databases?server_id={sid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


def test_adopt_database_without_model_version_does_not_stamp(
    admin_client, server_payload, monkeypatch
):
    from app.services.db_admin.migrations import MigrationRunner

    sid = _make_server(admin_client, server_payload, name="srv-stamp-d", port=3424)
    owner = _make_user(admin_client, sid, "owner1")
    model_id = _blueprint_with_migration(admin_client, slug="adopt-bp-d")
    monkeypatch.setattr(mdc, "get_adapter", lambda target: _FakeAdapter(dbs=["legacy_d"]))

    calls = []
    monkeypatch.setattr(
        MigrationRunner, "stamp", lambda self, *a, **k: calls.append((a, k))
    )

    r = admin_client.post(
        "/api/v1/managed-databases/adopt",
        json={
            "name": "legacy_d",
            "server_id": sid,
            "owner_id": owner,
            "model_id": model_id,
            # sin model_version
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["model_version"] is None
    assert calls == []  # nunca se invocó stamp


# --------------------------------------------------------------------------- #
# Adopt user                                                                   #
# --------------------------------------------------------------------------- #
def test_adopt_user_success_then_conflict_and_404(
    admin_client, server_payload, monkeypatch
):
    sid = _make_server(admin_client, server_payload)

    monkeypatch.setattr(
        suc,
        "get_adapter",
        lambda target: _FakeAdapter(users=[EngineUserInfo(username="dba", host="%")]),
    )

    r = admin_client.post(
        "/api/v1/server-users/adopt",
        json={"server_id": sid, "username": "dba", "host": "%"},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["has_password"] is False
    assert data["username"] == "dba"

    # idempotencia
    r = admin_client.post(
        "/api/v1/server-users/adopt",
        json={"server_id": sid, "username": "dba", "host": "%"},
    )
    assert r.status_code == 409, r.text

    # inexistente
    r = admin_client.post(
        "/api/v1/server-users/adopt",
        json={"server_id": sid, "username": "ghost", "host": "%"},
    )
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------- #
# Snapshot preview                                                             #
# --------------------------------------------------------------------------- #
def _sample_dump(engine="mysql"):
    return StructureDump(
        database="legacy",
        source_engine=engine,
        statements=[
            DumpStatement(object_type="table", name="clientes",
                          ddl="CREATE TABLE clientes (id INT PRIMARY KEY)"),
            DumpStatement(object_type="view", name="v_top",
                          ddl="CREATE VIEW v_top AS SELECT * FROM clientes"),
            DumpStatement(object_type="routine", name="sp_x",
                          ddl="CREATE PROCEDURE sp_x() BEGIN SELECT 1; END"),
        ],
        has_non_portable=True,
    )


def test_snapshot_preview(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeAdapter(dump=_sample_dump()))

    r = admin_client.get(f"/api/v1/servers/{sid}/databases/legacy/snapshot")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["source_engine"] == "mysql"
    assert data["has_non_portable"] is True
    assert len(data["statements"]) == 3
    assert {s["object_type"] for s in data["statements"]} == {"table", "view", "routine"}


# --------------------------------------------------------------------------- #
# From-snapshot → blueprint baseline                                           #
# --------------------------------------------------------------------------- #
def test_from_snapshot_creates_baseline(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeAdapter(dump=_sample_dump()))

    r = admin_client.post(
        "/api/v1/database-models/from-snapshot",
        json={
            "server_id": sid,
            "database": "legacy",
            "name": "CRM Legacy",
            "slug": "crm-legacy",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["baseline_version"] == "0001"
    assert data["source_engine"] == "mysql"
    assert data["has_non_portable"] is True
    assert data["statements_captured"] == 3
    model_id = data["model"]["id"]

    # La migración baseline quedó persistida con sus metadatos.
    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 200, r.text
    m = r.json()["data"]
    assert m["is_baseline"] is True
    assert m["source_engine"] == "mysql"
    assert m["has_non_portable"] is True
    assert "CREATE TABLE clientes" in m["up_sql"]


def test_from_snapshot_baseline_is_unreviewed(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeAdapter(dump=_sample_dump()))
    r = admin_client.post(
        "/api/v1/database-models/from-snapshot",
        json={"server_id": sid, "database": "legacy", "name": "Rv", "slug": "rv"},
    )
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["model"]["id"]
    # R1: el baseline de snapshot nace SIN revisar.
    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reviewed"] is False
    assert r.json()["data"]["is_baseline"] is True


def test_unreviewed_baseline_blocks_apply_until_approved(
    admin_client, server_payload, monkeypatch
):
    from app.services.db_admin.migrations import MigrationRunner

    sid = _make_server(admin_client, server_payload)
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeAdapter(dump=_sample_dump()))
    r = admin_client.post(
        "/api/v1/database-models/from-snapshot",
        json={"server_id": sid, "database": "legacy", "name": "Gate", "slug": "gate"},
    )
    model_id = r.json()["data"]["model"]["id"]
    owner = _make_user(admin_client, sid, "owner1")
    db_id = _make_managed(admin_client, sid, owner, "appdb", model_id=model_id)

    # 1) Apply bloqueado: baseline de snapshot sin revisar → 409 (sin tocar el motor).
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 409, r.text
    assert "sin revisar" in r.text.lower() or "baseline" in r.text.lower()

    # 2) Aprobar el baseline tras revisar su DDL.
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001", json={"reviewed": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reviewed"] is True

    # 3) Ahora el apply procede (runner mockeado: sin motor real).
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: None)
    monkeypatch.setattr(MigrationRunner, "apply", lambda self, *a, **k: [])
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 200, r.text


def test_handwritten_migration_is_reviewed_by_default(admin_client):
    r = admin_client.post("/api/v1/database-models", json={"name": "hw", "slug": "hw"})
    model_id = r.json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "x", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 201, r.text
    # Escrita a mano por el admin → nace revisada (no la bloquea el gate R1).
    assert r.json()["data"]["reviewed"] is True


def test_from_snapshot_empty_db_422(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    empty_dump = StructureDump(database="vacia", source_engine="mysql", statements=[])
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeAdapter(dump=empty_dump))

    r = admin_client.post(
        "/api/v1/database-models/from-snapshot",
        json={"server_id": sid, "database": "vacia", "name": "Vacia", "slug": "vacia"},
    )
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# Cross-engine guard                                                           #
# --------------------------------------------------------------------------- #
def test_cross_engine_guard_blocks_apply(admin_client, server_payload, monkeypatch):
    # 1) Blueprint baseline desde un snapshot MySQL con objetos no portables.
    src = _make_server(admin_client, server_payload, name="srv-mysql", port=3411)
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeAdapter(dump=_sample_dump("mysql")))
    r = admin_client.post(
        "/api/v1/database-models/from-snapshot",
        json={"server_id": src, "database": "legacy", "name": "Bp", "slug": "bp"},
    )
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["model"]["id"]

    # 2) Servidor PostgreSQL + BD gestionada que referencia ese blueprint.
    pg = _make_server(
        admin_client, server_payload, name="srv-pg", port=3412, engine="postgresql"
    )
    owner = _make_user(admin_client, pg, "pgowner")
    db_id = _make_managed(admin_client, pg, owner, "pgdb", model_id=model_id)

    # 3) Aplicar el baseline MySQL no portable a un motor PG → 422 (cross-engine).
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 422, r.text
    assert "no puede aplicarse" in r.text or "cross" in r.text.lower()
