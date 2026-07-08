"""
Tests de API del snapshot SELECTIVO: selección de objetos (include/exclude), split de
versiones (single/by_class/manual con validación topológica) y datos-semilla
(upsert idempotente + rollback por PK), incluido el guard cross-engine de datos.

El motor se mockea con un FakeAdapter que delega la extracción de datos en el módulo
real ``snapshot_data`` (para ejercitar el render), como el resto de la suite.
"""

import app.controllers.server_controller as sc
from app.services.db_admin import snapshot_data
from app.services.db_admin.dtos import (
    DumpStatement,
    SeedResult,
    StructureDump,
    TableStat,
)


# --------------------------------------------------------------------------- #
# Helpers de inventario                                                        #
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


# --------------------------------------------------------------------------- #
# FakeAdapter                                                                  #
# --------------------------------------------------------------------------- #
def _dump(engine="mysql"):
    """Dump con FK (producto→categoria), una vista y una rutina no portable."""
    return StructureDump(
        database="tienda",
        source_engine=engine,
        statements=[
            DumpStatement(
                object_type="table", name="categoria",
                ddl="CREATE TABLE categoria (id INT PRIMARY KEY, nombre VARCHAR(50))",
            ),
            DumpStatement(
                object_type="table", name="producto",
                ddl="CREATE TABLE producto (id INT PRIMARY KEY, cat_id INT)",
                depends_on=["categoria"],
            ),
            DumpStatement(
                object_type="view", name="v_prod",
                ddl="CREATE VIEW v_prod AS SELECT * FROM producto",
            ),
            DumpStatement(
                object_type="routine", name="sp_x",
                ddl="CREATE PROCEDURE sp_x() BEGIN SELECT 1; END",
            ),
        ],
        has_non_portable=True,
    )


class _FakeAdapter:
    def __init__(self, *, dump=None, rows=None, stats=None, dialect="mysql"):
        self.dialect = dialect
        self._dump = dump if dump is not None else _dump(dialect)
        self._rows = rows or {}
        self._stats = stats or []

    def dump_structure(self, database):
        return self._dump

    def list_table_stats(self, database):
        return self._stats

    def dump_table_data(self, database, table, *, mode, max_rows, max_bytes, batch_rows):
        spec = self._rows.get(table)
        if spec is None:
            return SeedResult(table=table, included=False, reason="no_rows")
        if isinstance(spec, SeedResult):
            return spec  # resultado pre-fabricado (p.ej. oversize)
        columns, pk, rows = spec
        if not pk:
            return SeedResult(table=table, included=False, reason="no_primary_key")
        mr, mb = snapshot_data.effective_limits(max_rows, max_bytes)
        return snapshot_data.build_seed(
            dialect=self.dialect, table=table, columns=columns, pk=pk,
            rows=rows, mode=mode, batch_rows=batch_rows, max_rows=mr, max_bytes=mb,
        )


def _patch(monkeypatch, adapter):
    monkeypatch.setattr(sc, "get_adapter", lambda target: adapter)


def _from_snapshot(admin_client, sid, **body):
    payload = {"server_id": sid, "database": "tienda", "name": body.pop("name", "BP"),
               "slug": body.pop("slug", "bp")}
    payload.update(body)
    return admin_client.post("/api/v1/database-models/from-snapshot", json=payload)


# --------------------------------------------------------------------------- #
# Selección de objetos                                                         #
# --------------------------------------------------------------------------- #
def test_single_default_captures_everything(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    _patch(monkeypatch, _FakeAdapter())
    r = _from_snapshot(admin_client, sid, slug="bp-all")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["total_versions"] == 1
    assert data["baseline_version"] == "0001"
    assert data["statements_captured"] == 4
    assert data["has_non_portable"] is True


def test_exclude_object_types_drops_them(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    _patch(monkeypatch, _FakeAdapter())
    r = _from_snapshot(
        admin_client, sid, slug="bp-noproc", exclude_object_types=["routine", "view"]
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["statements_captured"] == 2  # solo las 2 tablas
    assert data["has_non_portable"] is False


def test_by_class_layout_creates_a_version_per_class(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    _patch(monkeypatch, _FakeAdapter())
    r = _from_snapshot(admin_client, sid, slug="bp-byclass", layout="by_class")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    # tablas → vistas → rutinas
    assert data["total_versions"] == 3
    kinds = [v["kind"] for v in data["versions"]]
    assert kinds == ["schema", "schema", "schema"]
    assert data["versions"][0]["object_counts"].get("table") == 2


# --------------------------------------------------------------------------- #
# Layout manual                                                                #
# --------------------------------------------------------------------------- #
def test_manual_layout_valid(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    _patch(monkeypatch, _FakeAdapter())
    r = _from_snapshot(
        admin_client, sid, slug="bp-manual",
        layout="manual",
        exclude_object_types=["view", "routine"],
        manual_layout=[
            {"objects": [{"object_type": "table", "name": "categoria"}]},
            {"objects": [{"object_type": "table", "name": "producto"}]},
        ],
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["total_versions"] == 2


def test_manual_layout_fk_violation_422(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    _patch(monkeypatch, _FakeAdapter())
    r = _from_snapshot(
        admin_client, sid, slug="bp-manual-bad",
        layout="manual",
        exclude_object_types=["view", "routine"],
        manual_layout=[
            {"objects": [{"object_type": "table", "name": "producto"}]},   # depende de categoria
            {"objects": [{"object_type": "table", "name": "categoria"}]},  # en versión posterior
        ],
    )
    assert r.status_code == 422, r.text
    body = r.json()
    reasons = {v["reason"] for v in body["detail"]["context"]["violations"]}
    assert "dependency_in_later_version" in reasons


# --------------------------------------------------------------------------- #
# Datos-semilla                                                                #
# --------------------------------------------------------------------------- #
def test_data_seeding_adds_data_version_last(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    rows = {"categoria": (["id", "nombre"], ["id"], [(1, "A"), (2, "B")])}
    _patch(monkeypatch, _FakeAdapter(rows=rows))
    r = _from_snapshot(
        admin_client, sid, slug="bp-data",
        data_tables=[{"table": "categoria", "mode": "upsert"}],
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["total_versions"] == 2
    assert data["data_tables_captured"] == 1
    assert data["versions"][-1]["kind"] == "data"
    model_id = data["model"]["id"]

    # La migración de datos (0002) es kind=data con INSERT + rollback sugerido, sin confirmar.
    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 200, r.text
    m = r.json()["data"]
    assert m["kind"] == "data"
    assert "INSERT INTO `categoria`" in m["up_sql"]
    assert m["down_sql_suggested"] is not None
    assert m["down_sql"] is None  # no confirmado por defecto


def test_data_seeding_confirm_rollback_sets_down_sql(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    rows = {"categoria": (["id", "nombre"], ["id"], [(1, "A")])}
    _patch(monkeypatch, _FakeAdapter(rows=rows))
    r = _from_snapshot(
        admin_client, sid, slug="bp-data-cfm",
        data_tables=[{"table": "categoria"}],
        confirm_data_rollback=True,
    )
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["model"]["id"]
    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.json()["data"]["down_sql"] is not None


def test_data_for_table_without_structure_422(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    rows = {"categoria": (["id", "nombre"], ["id"], [(1, "A")])}
    _patch(monkeypatch, _FakeAdapter(rows=rows))
    r = _from_snapshot(
        admin_client, sid, slug="bp-data-nostruct",
        exclude_object_types=["table"],  # sin ninguna tabla en el blueprint
        data_tables=[{"table": "categoria"}],
    )
    assert r.status_code == 422, r.text
    assert "estructura" in r.text.lower()


def test_data_oversize_error_vs_skip(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    oversize = SeedResult(table="categoria", included=False, reason="oversize_rows")
    _patch(monkeypatch, _FakeAdapter(rows={"categoria": oversize}))

    # on_oversize=error → 422
    r = _from_snapshot(
        admin_client, sid, slug="bp-ovr-err",
        data_tables=[{"table": "categoria"}], on_oversize="error",
    )
    assert r.status_code == 422, r.text

    # on_oversize=skip → 201, la tabla queda reportada como omitida
    r = _from_snapshot(
        admin_client, sid, slug="bp-ovr-skip",
        data_tables=[{"table": "categoria"}], on_oversize="skip",
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["data_tables_captured"] == 0
    assert data["skipped_tables"] == [{"table": "categoria", "reason": "oversize_rows"}]
    assert data["total_versions"] == 1  # solo el esquema


# --------------------------------------------------------------------------- #
# Preview con estadísticas de datos                                            #
# --------------------------------------------------------------------------- #
def test_snapshot_preview_with_data_stats(admin_client, server_payload, monkeypatch):
    sid = _make_server(admin_client, server_payload)
    stats = [
        TableStat(table="categoria", estimated_rows=12, has_primary_key=True),
        TableStat(table="log", estimated_rows=999999, has_primary_key=False),
    ]
    _patch(monkeypatch, _FakeAdapter(stats=stats))

    # Sin flag: no hay table_stats.
    r = admin_client.get(f"/api/v1/servers/{sid}/databases/tienda/snapshot")
    assert r.status_code == 200, r.text
    assert r.json()["data"].get("table_stats") is None

    # Con flag: aparecen las estimaciones (sin valores de filas).
    r = admin_client.get(
        f"/api/v1/servers/{sid}/databases/tienda/snapshot?include_data_stats=true"
    )
    assert r.status_code == 200, r.text
    ts = {t["table"]: t for t in r.json()["data"]["table_stats"]}
    assert ts["categoria"]["has_primary_key"] is True
    assert ts["log"]["has_primary_key"] is False


# --------------------------------------------------------------------------- #
# Guard cross-engine de datos                                                  #
# --------------------------------------------------------------------------- #
def test_data_migration_cross_engine_guard_blocks_apply(
    admin_client, server_payload, monkeypatch
):
    # 1) Blueprint MySQL: solo tablas portables + datos-semilla (kind=data).
    src = _make_server(admin_client, server_payload, name="srv-my", port=3451)
    rows = {"categoria": (["id", "nombre"], ["id"], [(1, "A")])}
    _patch(monkeypatch, _FakeAdapter(rows=rows, dialect="mysql"))
    r = _from_snapshot(
        admin_client, src, slug="bp-xe-data",
        exclude_object_types=["view", "routine"],  # estructura portable
        data_tables=[{"table": "categoria"}],
    )
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["model"]["id"]

    # 2) Servidor PostgreSQL + BD gestionada con ese blueprint.
    pg = _make_server(admin_client, server_payload, name="srv-pg2", port=3452, engine="postgresql")
    owner = _make_user(admin_client, pg, "pgowner")
    db_id = _make_managed(admin_client, pg, owner, "pgdb2", model_id=model_id)

    # 3) Aplicar datos MySQL (upsert por motor) a PG → 422 cross-engine.
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 422, r.text
    assert "no puede aplicarse" in r.text.lower() or "datos" in r.text.lower()
