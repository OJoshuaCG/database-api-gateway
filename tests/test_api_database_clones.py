"""
Tests de la API de clonación de bases de datos (feature clone).

El motor se mockea (SQLite como BD de metadatos + adapter falso para el plano en vivo).
El worker asíncrono se ejecuta SÍNCRONO en el test (``enqueue`` → ``run_job`` inline) y
``MigrationRunner.execute_adhoc``/``copy_tables`` se sustituyen por fakes que devuelven
'applied'. Así se ejercita todo el pipeline del controller sin motores reales.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

import app.controllers.clone_controller as cc
import app.services.clone_runner as clone_runner
from app.services.db_admin.data_copy import TableCopyResult
from app.services.db_admin.dtos import (
    ColumnInfo,
    ForeignKeyInfo,
    RoutineInfo,
    SchemaSnapshot,
    TableSchema,
)
from app.services.db_admin.migrations import StatementResult
from app.services.db_admin.schema_diff import RenderedStatement


# --------------------------------------------------------------------------- #
# Inventario                                                                   #
# --------------------------------------------------------------------------- #
def _server(admin_client, port, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={"name": f"srv{port}", "host": "10.0.0.5", "port": port, "engine": engine,
              "root_username": "root", "root_password": "pw"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _owner(admin_client, sid, username="owner1") -> int:
    r = admin_client.post("/api/v1/server-users", json={"server_id": sid, "username": username})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _managed(admin_client, sid, oid, name, model_id=None) -> int:
    body = {"server_id": sid, "owner_id": oid, "name": name}
    if model_id is not None:
        body["model_id"] = model_id
    r = admin_client.post("/api/v1/managed-databases", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# Snapshot de origen                                                           #
# --------------------------------------------------------------------------- #
def _source_snapshot(db="src_db", engine="mysql") -> SchemaSnapshot:
    parent = TableSchema(
        database=db, table="parent",
        columns=[ColumnInfo(name="id", type="int", nullable=False)],
        primary_key=["id"], foreign_keys=[], indexes=[],
    )
    child = TableSchema(
        database=db, table="child",
        columns=[ColumnInfo(name="id", type="int", nullable=False),
                 ColumnInfo(name="pid", type="int", nullable=True)],
        primary_key=["id"],
        foreign_keys=[ForeignKeyInfo(columns=["pid"], referred_table="parent", referred_columns=["id"])],
        indexes=[],
    )
    return SchemaSnapshot(
        database=db, source_engine=engine, tables=[parent, child],
        routines=[RoutineInfo(name="sp_x", kind="PROCEDURE", body="CREATE PROCEDURE sp_x() BEGIN END")],
    )


class _FakeAdapter:
    """Adapter en memoria: snapshots, list/create/drop de BD y render_diff determinista."""

    def __init__(self, snaps: dict, existing: set):
        self.snaps = snaps
        self.existing = set(existing)
        self.created: list[str] = []
        self.dropped: list[str] = []

    def structural_snapshot(self, database):
        return self.snaps.get(database, SchemaSnapshot(database=database, source_engine="mysql"))

    def list_databases(self):
        return sorted(self.existing)

    def create_database(self, db_name, charset=None, collation=None, owner=None):
        self.existing.add(db_name)
        self.created.append(db_name)

    def drop_database(self, db_name):
        self.existing.discard(db_name)
        self.dropped.append(db_name)

    def render_diff(self, diff):
        # Una sentencia por ítem del diff, con SQL determinista.
        return [
            RenderedStatement(
                sql=f"-- {it.change_type} {it.object_type} {it.object_name}",
                object_type=it.object_type, object_name=it.object_name,
                change_type=it.change_type, phase=it.phase, risk=it.risk,
                down_sql=None, down_confirmed=False,
            )
            for it in diff.items
        ]


class _FakeRunner:
    @contextmanager
    def advisory_lock(self, target, *, engine, lock_key):
        yield  # no-op en test (sin motor real que lockear)

    def execute_adhoc(self, target, *, db_name, engine, lock_key, statements, already_locked=False):
        return [
            StatementResult(index=i, status="applied", error=None, execution_ms=1,
                            executed_at=datetime.now(timezone.utc))
            for i in range(len(statements))
        ]


def _fake_copy_tables(*, specs, **kwargs):
    return [TableCopyResult(table=s.table, status="applied", rows_copied=10) for s in specs]


def _install(monkeypatch, *, source_db="src_db", target_db="dst_db", target_exists=False):
    """Instala el adapter fake + runner síncrono. Devuelve el fake para inspección."""
    snaps = {source_db: _source_snapshot(db=source_db)}
    existing = {source_db}
    if target_exists:
        existing.add(target_db)
        snaps[target_db] = SchemaSnapshot(database=target_db, source_engine="mysql")
    fake = _FakeAdapter(snaps, existing)
    monkeypatch.setattr(cc, "get_adapter", lambda target: fake)
    monkeypatch.setattr(cc, "MigrationRunner", _FakeRunner)
    monkeypatch.setattr(cc, "copy_tables", _fake_copy_tables)
    # Ejecutar el job SÍNCRONO en el test.
    monkeypatch.setattr(clone_runner, "enqueue", lambda job_id: cc.CloneController().run_job(job_id))
    return fake


def _preview_and_execute(admin_client, job_id, *, target_db="dst_db"):
    pr = admin_client.post(f"/api/v1/database-clones/{job_id}/preview", json={})
    assert pr.status_code == 200, pr.text
    token = pr.json()["data"]["confirm_token"]
    ex = admin_client.post(
        f"/api/v1/database-clones/{job_id}/execute",
        json={"confirm_target_name": target_db, "confirm_token": token},
    )
    return pr, ex


# =========================================================================== #
# Tests                                                                        #
# =========================================================================== #
def test_requires_auth(client):
    assert client.post("/api/v1/database-clones", json={}).status_code == 401


def test_create_plan_new_target_structure_only(admin_client, monkeypatch):
    _install(monkeypatch, target_exists=False)
    sid = _server(admin_client, 3600)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db",
        "target_mode": "new", "include_data": False,
    })
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["status"] == "pending"
    assert data["target_mode"] == "new"

    _pr, ex = _preview_and_execute(admin_client, data["id"])
    assert ex.status_code == 200, ex.text
    # Job corrió síncrono → succeeded.
    summary = admin_client.get(f"/api/v1/database-clones/{data['id']}").json()["data"]
    assert summary["status"] == "succeeded", summary
    items = admin_client.get(f"/api/v1/database-clones/{data['id']}/items").json()["data"]
    kinds = {i["kind"] for i in items}
    assert "structure" in kinds
    # Se creó la BD destino.
    assert any(i["object_type"] == "database" and i["kind"] == "clean" for i in items)


def test_full_clone_with_data_records_row_counts(admin_client, monkeypatch):
    _install(monkeypatch, target_exists=False)
    sid = _server(admin_client, 3601)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db",
        "target_mode": "new", "include_data": True,
    })
    job_id = r.json()["data"]["id"]
    _pr, ex = _preview_and_execute(admin_client, job_id)
    assert ex.status_code == 200, ex.text
    summary = admin_client.get(f"/api/v1/database-clones/{job_id}").json()["data"]
    assert summary["status"] == "succeeded"
    items = admin_client.get(f"/api/v1/database-clones/{job_id}/items").json()["data"]
    data_items = [i for i in items if i["kind"] == "data"]
    assert data_items and all(i["rows_copied"] == 10 for i in data_items)
    assert {i["object_name"] for i in data_items} == {"parent", "child"}


def test_same_database_422(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3602)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "src_db",
        "target_mode": "existing",
    })
    assert r.status_code == 422, r.text


def test_new_target_already_exists_422(admin_client, monkeypatch):
    _install(monkeypatch, target_exists=True)
    sid = _server(admin_client, 3603)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db",
        "target_mode": "new",
    })
    assert r.status_code == 422, r.text


def test_execute_wrong_token_422(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3604)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    job_id = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db", "target_mode": "new",
    }).json()["data"]["id"]
    admin_client.post(f"/api/v1/database-clones/{job_id}/preview", json={})
    ex = admin_client.post(f"/api/v1/database-clones/{job_id}/execute",
                           json={"confirm_target_name": "dst_db", "confirm_token": "deadbeef"})
    assert ex.status_code == 422, ex.text


def test_execute_wrong_confirm_name_422(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3605)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    job_id = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db", "target_mode": "new",
    }).json()["data"]["id"]
    pr = admin_client.post(f"/api/v1/database-clones/{job_id}/preview", json={})
    token = pr.json()["data"]["confirm_token"]
    ex = admin_client.post(f"/api/v1/database-clones/{job_id}/execute",
                           json={"confirm_target_name": "WRONG", "confirm_token": token})
    assert ex.status_code == 422, ex.text


def test_resolve_selection_pulls_fk_parent(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3606)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    job_id = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db", "target_mode": "new",
    }).json()["data"]["id"]
    r = admin_client.post(f"/api/v1/database-clones/{job_id}/resolve-selection",
                          json={"selection": [{"object_type": "table", "name": "child"}]})
    assert r.status_code == 200, r.text
    closure = {(o["object_type"], o["name"]) for o in r.json()["data"]["closure"]}
    assert ("table", "parent") in closure  # FK arrastra al padre


def test_data_failure_redacts_row_values_in_error(admin_client, monkeypatch):
    """R4: el error crudo del driver en la fase de datos (con posibles valores de filas)
    NO se persiste; se guarda un motivo genérico."""
    _install(monkeypatch, target_exists=False)

    def _failing_copy(*, specs, **kwargs):
        return [TableCopyResult(table=specs[0].table, status="failed", rows_copied=0,
                                error="Duplicate entry 'alice@secret.com' for key 'users.email'")]

    monkeypatch.setattr(cc, "copy_tables", _failing_copy)
    sid = _server(admin_client, 3608)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    job_id = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db",
        "target_mode": "new", "include_data": True,
    }).json()["data"]["id"]
    _pr, ex = _preview_and_execute(admin_client, job_id)
    assert ex.status_code == 200, ex.text
    summary = admin_client.get(f"/api/v1/database-clones/{job_id}").json()["data"]
    assert summary["status"] == "failed"
    items = admin_client.get(f"/api/v1/database-clones/{job_id}/items").json()["data"]
    data_fail = [i for i in items if i["kind"] == "data" and i["status"] == "failed"]
    assert data_fail
    # El valor sensible NUNCA debe aparecer en el error persistido.
    assert all("secret.com" not in (i["error"] or "") for i in data_fail)


def test_structure_failure_marks_job_failed(admin_client, monkeypatch):
    _install(monkeypatch, target_exists=False)

    class _FailingRunner(_FakeRunner):
        def execute_adhoc(self, target, *, db_name, engine, lock_key, statements, already_locked=False):
            return [StatementResult(index=0, status="failed", error="boom",
                                    execution_ms=1, executed_at=datetime.now(timezone.utc))]

    monkeypatch.setattr(cc, "MigrationRunner", _FailingRunner)
    sid = _server(admin_client, 3609)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    job_id = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db", "target_mode": "new",
    }).json()["data"]["id"]
    _pr, ex = _preview_and_execute(admin_client, job_id)
    assert ex.status_code == 200, ex.text
    summary = admin_client.get(f"/api/v1/database-clones/{job_id}").json()["data"]
    assert summary["status"] == "failed"


def test_adopt_target_requires_owner(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3607)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    # adopt_target sin adopt_owner_id → 422 de validación de schema.
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id,
        "target_server_id": sid, "target_database_name": "dst_db", "target_mode": "new",
        "adopt_target": True,
    })
    assert r.status_code == 422, r.text
