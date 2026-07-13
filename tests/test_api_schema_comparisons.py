"""
Tests de la API de comparaciones estructurales (Plan diff, Fases 4-6).

El motor se mockea igual que el resto de la suite: SQLite como BD de metadatos +
adapter falso para el plano "en vivo" (``structural_snapshot``/``render_diff``) y el
``MigrationRunner`` mockeado para ``execute_adhoc``/``apply``. La fidelidad de dialecto
y la ejecución DDL real NO son verificables sin Docker (Fase 7 / motores reales).
"""

from datetime import datetime, timezone

import app.controllers.schema_comparison_controller as scc
from app.controllers.schema_comparison_controller import SchemaComparisonController
from app.services.db_admin.dtos import ColumnInfo, SchemaSnapshot, TableSchema
from app.services.db_admin.migrations import MigrationRunner, StatementResult
from app.services.db_admin.schema_diff import RenderedStatement, RiskFlags


# --------------------------------------------------------------------------- #
# Helpers de inventario                                                        #
# --------------------------------------------------------------------------- #
def _server(admin_client, port, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}",
            "host": "10.0.0.5",
            "port": port,
            "engine": engine,
            "root_username": "root",
            "root_password": "pw",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _owner(admin_client, sid, username="owner1") -> int:
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": username}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _managed(admin_client, sid, oid, name, model_id=None) -> int:
    body = {"server_id": sid, "owner_id": oid, "name": name}
    if model_id is not None:
        body["model_id"] = model_id
    r = admin_client.post("/api/v1/managed-databases", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _blueprint(admin_client, slug) -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# Fixtures de snapshot / render                                                #
# --------------------------------------------------------------------------- #
def _snap(engine="mysql", db="db", marker="a") -> SchemaSnapshot:
    """Snapshot mínimo. ``marker`` altera el contenido para variar el fingerprint."""
    return SchemaSnapshot(
        database=db,
        source_engine=engine,
        tables=[
            TableSchema(
                database=db,
                table=f"t_{marker}",
                columns=[ColumnInfo(name="id", type="int", nullable=False)],
                primary_key=["id"],
                foreign_keys=[],
                indexes=[],
            )
        ],
    )


def _rendered() -> list[RenderedStatement]:
    """Tres sentencias YA en orden de fase: aditiva, procedural, destructiva."""
    return [
        RenderedStatement(
            sql="CREATE TABLE new_t (id INT PRIMARY KEY)",
            object_type="table", object_name="new_t", change_type="new", phase=2,
            risk=RiskFlags(), down_sql="DROP TABLE `new_t`", down_confirmed=True,
        ),
        RenderedStatement(
            sql="CREATE PROCEDURE sp_x() BEGIN SELECT 1; END",
            object_type="routine", object_name="PROCEDURE:sp_x", change_type="new", phase=5,
            risk=RiskFlags(requires_individual_review=True), down_sql=None, down_confirmed=False,
        ),
        RenderedStatement(
            sql="DROP TABLE `old_t`",
            object_type="table", object_name="old_t", change_type="dropped", phase=8,
            risk=RiskFlags(destructive=True), down_sql=None, down_confirmed=False,
        ),
    ]


class _FakeAdapter:
    def __init__(self, snaps, rendered):
        self.snaps = snaps
        self.rendered = rendered

    def structural_snapshot(self, database):
        return self.snaps[database]

    def render_diff(self, diff):
        return list(self.rendered)

    def list_databases(self):
        # Las BDs "reales" del motor son exactamente las que tienen snapshot.
        return list(self.snaps.keys())


def _setup(admin_client, monkeypatch, *, port, target_has_model=False, rendered=None):
    sid = _server(admin_client, port)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    model_id = None
    if target_has_model:
        model_id = _blueprint(admin_client, f"bp-{port}")
    tgt_id = _managed(admin_client, sid, oid, "tgt_db", model_id=model_id)
    fake = _FakeAdapter(
        {"src_db": _snap(db="src_db", marker="src"), "tgt_db": _snap(db="tgt_db", marker="tgt")},
        rendered if rendered is not None else _rendered(),
    )
    monkeypatch.setattr(scc, "get_adapter", lambda target: fake)
    return src_id, tgt_id, model_id, fake


def _create(admin_client, src_id, tgt_id):
    return admin_client.post(
        "/api/v1/schema-comparisons",
        json={"source_database_id": src_id, "target_database_id": tgt_id},
    )


def _fake_exec_ok(self, target, *, db_name, engine, lock_key, statements):
    return [
        StatementResult(
            index=i, status="applied", error=None, execution_ms=1,
            executed_at=datetime.now(timezone.utc),
        )
        for i in range(len(statements))
    ]


def _token(admin_client, comparison_id, engine, mode, selected_ids=None):
    """
    Reproduce el algoritmo documentado del confirm_token sobre los ítems reales.

    El primer componente del token es ahora ``f"{target_server_id}:{target_database_name}"``
    (siempre poblado, gestionada o cruda) — se lee del resumen de la comparación.
    """
    summary = admin_client.get(
        f"/api/v1/schema-comparisons/{comparison_id}"
    ).json()["data"]
    ref = f"{summary['target_server_id']}:{summary['target_database_name']}"
    items = admin_client.get(
        f"/api/v1/schema-comparisons/{comparison_id}/items?size=50"
    ).json()["data"]
    items.sort(key=lambda x: x["seq"])
    if mode == "all":
        chosen = [i for i in items if not i["risk_flags"].get("requires_individual_review")]
    elif mode == "all_except_destructive":
        chosen = [
            i for i in items
            if not i["risk_flags"].get("destructive")
            and not i["risk_flags"].get("requires_individual_review")
        ]
    else:  # custom
        idset = set(selected_ids or [])
        chosen = [i for i in items if i["id"] in idset]
    resolved = [{"sql": i["sql"], "risk": i["risk_flags"]} for i in chosen]
    token = SchemaComparisonController.execution_token(ref, engine, resolved)
    return token, [i["id"] for i in chosen]


# =========================================================================== #
# Fase 4 — creación + lectura                                                  #
# =========================================================================== #
def test_requires_auth(client):
    assert client.post("/api/v1/schema-comparisons", json={}).status_code == 401


def test_create_comparison_persists_items(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3500)
    r = _create(admin_client, src_id, tgt_id)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["item_count"] == 3
    assert data["has_destructive"] is True
    assert data["cross_flavor_warning"] is False
    # counts: 2 tablas (new + dropped) y 1 routine (new).
    assert data["counts"]["table"] == {"new": 1, "dropped": 1}
    assert data["counts"]["routine"] == {"new": 1}

    # Los ítems se listan ordenados por seq con su DDL exacto.
    items = admin_client.get(
        f"/api/v1/schema-comparisons/{data['id']}/items"
    ).json()["data"]
    assert [i["object_name"] for i in items] == ["new_t", "PROCEDURE:sp_x", "old_t"]
    assert items[0]["down_confirmed"] is True
    assert items[2]["risk_flags"]["destructive"] is True


def test_create_same_database_422(admin_client, monkeypatch):
    src_id, _, _, _ = _setup(admin_client, monkeypatch, port=3501)
    r = admin_client.post(
        "/api/v1/schema-comparisons",
        json={"source_database_id": src_id, "target_database_id": src_id},
    )
    assert r.status_code == 422, r.text


def test_create_incompatible_engines_422(admin_client, monkeypatch):
    pg = _server(admin_client, 3502, engine="postgresql")
    my = _server(admin_client, 3503, engine="mysql")
    pg_o, my_o = _owner(admin_client, pg, "pgo"), _owner(admin_client, my, "myo")
    src = _managed(admin_client, pg, pg_o, "pg_src")
    tgt = _managed(admin_client, my, my_o, "my_tgt")
    # No hace falta mockear: el 422 ocurre antes de tocar el motor.
    r = admin_client.post(
        "/api/v1/schema-comparisons",
        json={"source_database_id": src, "target_database_id": tgt},
    )
    assert r.status_code == 422, r.text
    assert "incompatibles" in r.text.lower()


def test_get_comparison_404(admin_client):
    assert admin_client.get("/api/v1/schema-comparisons/9999").status_code == 404


def test_items_filter_by_change_type(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3504)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    data = admin_client.get(
        f"/api/v1/schema-comparisons/{cid}/items?change_type=dropped"
    ).json()["data"]
    assert [i["object_name"] for i in data] == ["old_t"]


# =========================================================================== #
# Fase 5 — adopt (Opción A)                                                    #
# =========================================================================== #
def test_adopt_requires_target_blueprint_422(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3510, target_has_model=False)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/adopt",
        json={"selected_item_ids": [items[0]["id"]], "name": "v1"},
    )
    assert r.status_code == 422, r.text


def test_adopt_creates_blueprint_version(admin_client, monkeypatch):
    src_id, tgt_id, model_id, _ = _setup(
        admin_client, monkeypatch, port=3511, target_has_model=True
    )
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    by_name = {i["object_name"]: i["id"] for i in items}
    # Selecciono la tabla aditiva + la rutina (no portable).
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/adopt",
        json={
            "selected_item_ids": [by_name["new_t"], by_name["PROCEDURE:sp_x"]],
            "name": "diff v1",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["version"] == "0001"
    assert data["executed"] is False
    assert data["apply_result"] is None
    mig = data["migration"]
    assert mig["source_engine"] == "mysql"
    assert mig["is_baseline"] is True
    assert mig["reviewed"] is False           # diferido → gate R1 lo protege
    assert mig["has_non_portable"] is True    # incluye una rutina
    # El up_sql concatena en orden de fase; el pin va al override MySQL.
    assert "CREATE TABLE new_t" in mig["up_sql"]
    assert "CREATE PROCEDURE sp_x" in mig["up_sql"]
    assert mig["up_sql"].index("CREATE TABLE new_t") < mig["up_sql"].index("CREATE PROCEDURE sp_x")
    assert mig["up_sql_mysql"] == mig["up_sql"]
    assert mig["up_sql_postgresql"] is None

    # La versión quedó persistida en el blueprint del target.
    got = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert got.status_code == 200, got.text
    assert got.json()["data"]["reviewed"] is False


def test_adopt_confirmed_down_sql_when_all_reversible(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(
        admin_client, monkeypatch, port=3512, target_has_model=True
    )
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    new_t = next(i for i in items if i["object_name"] == "new_t")
    # new_t tiene down_confirmed=True → el down_sql del blueprint queda CONFIRMADO.
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/adopt",
        json={"selected_item_ids": [new_t["id"]], "name": "only add"},
    )
    assert r.status_code == 200, r.text
    mig = r.json()["data"]["migration"]
    assert mig["down_sql"] is not None
    assert "DROP TABLE" in mig["down_sql"]


def test_adopt_execute_immediately_applies(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(
        admin_client, monkeypatch, port=3513, target_has_model=True
    )
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    new_t = next(i for i in items if i["object_name"] == "new_t")

    # apply real mockeado (sin motor): la versión nace reviewed=True → pasa el gate R1.
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: None)
    monkeypatch.setattr(
        MigrationRunner, "apply",
        lambda self, *a, **k: [],  # no importa el detalle; sí que no explote el camino
    )
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/adopt",
        json={
            "selected_item_ids": [new_t["id"]],
            "name": "immediate",
            "execute_immediately": True,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["executed"] is True
    assert data["migration"]["reviewed"] is True
    assert data["apply_result"] is not None


# =========================================================================== #
# Fase 6 — execute (Opción B)                                                  #
# =========================================================================== #
def test_execute_blocked_with_blueprint_409(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(
        admin_client, monkeypatch, port=3520, target_has_model=True
    )
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={
            "mode": "all",
            "confirm_target_name": "tgt_db",
            "confirm_token": "whatever",
        },
    )
    assert r.status_code == 409, r.text
    assert "adopt" in r.text.lower()


def test_execute_wrong_confirm_name_422(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3521)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={"mode": "all", "confirm_target_name": "WRONG", "confirm_token": "x"},
    )
    assert r.status_code == 422, r.text


def test_execute_wrong_token_422(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3522)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={
            "mode": "all",
            "confirm_target_name": "tgt_db",
            "confirm_token": "deadbeef",
        },
    )
    assert r.status_code == 422, r.text
    assert "token" in r.text.lower()


# =========================================================================== #
# Fase 6 (addendum) — /execute-preview: resuelve modo + token SIN ejecutar     #
# =========================================================================== #
def _preview(admin_client, comparison_id, mode, selected_item_ids=None):
    return admin_client.post(
        f"/api/v1/schema-comparisons/{comparison_id}/execute-preview",
        json={"mode": mode, "selected_item_ids": selected_item_ids},
    )


def test_preview_matches_manual_token_and_is_accepted_by_execute(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3530)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)

    manual_token, manual_ids = _token(admin_client, cid, "mysql", "all")

    r = _preview(admin_client, cid, "all")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["comparison_id"] == cid
    assert data["target_database_id"] == tgt_id
    assert {s["item_id"] for s in data["statements"]} == set(manual_ids)
    # El token del preview debe coincidir byte a byte con el algoritmo documentado.
    assert data["confirm_token"] == manual_token

    # Y ese token, tal cual lo devuelve el preview, debe ser aceptado por /execute.
    r2 = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={
            "mode": "all",
            "confirm_target_name": "tgt_db",
            "confirm_token": data["confirm_token"],
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["applied_count"] == len(manual_ids)


def test_preview_custom_mode_resolves_exact_selection(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3531)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    sp = next(i for i in items if i["object_type"] == "routine")

    r = _preview(admin_client, cid, "custom", selected_item_ids=[sp["id"]])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [s["item_id"] for s in data["statements"]] == [sp["id"]]
    assert data["statements"][0]["object_type"] == "routine"


def test_preview_custom_without_ids_422(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3532)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    r = _preview(admin_client, cid, "custom")
    assert r.status_code == 422, r.text


def test_preview_unknown_comparison_404(admin_client):
    r = _preview(admin_client, 999999, "all")
    assert r.status_code == 404, r.text


def test_execute_mode_all(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3523)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)

    token, ids = _token(admin_client, cid, "mysql", "all")
    assert len(ids) == 2  # new_t + old_t (excluye la rutina que requiere revisión)
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={"mode": "all", "confirm_target_name": "tgt_db", "confirm_token": token},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 2
    assert data["applied_count"] == 2
    assert data["failed"] is False
    names = {s["object_name"] for s in data["statements"]}
    assert names == {"new_t", "old_t"}

    # El resultado por sentencia se persiste en los ítems.
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    by_name = {i["object_name"]: i for i in items}
    assert by_name["new_t"]["execution_status"] == "applied"
    assert by_name["old_t"]["execution_status"] == "applied"
    # La rutina no se ejecutó (no entró en mode=all).
    assert by_name["PROCEDURE:sp_x"]["execution_status"] is None


def test_execute_mode_all_except_destructive(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3524)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)

    token, ids = _token(admin_client, cid, "mysql", "all_except_destructive")
    assert len(ids) == 1  # solo new_t (excluye destructivo y revisión-individual)
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={
            "mode": "all_except_destructive",
            "confirm_target_name": "tgt_db",
            "confirm_token": token,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 1
    assert [s["object_name"] for s in data["statements"]] == ["new_t"]


def test_execute_mode_custom_requires_ids(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3525)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)

    # Sin selected_item_ids → 422.
    token, _ = _token(admin_client, cid, "mysql", "all")
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={"mode": "custom", "confirm_target_name": "tgt_db", "confirm_token": token},
    )
    assert r.status_code == 422, r.text

    # Con la rutina explícitamente seleccionada (solo posible vía custom).
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    sp = next(i for i in items if i["object_name"] == "PROCEDURE:sp_x")
    token, ids = _token(admin_client, cid, "mysql", "custom", [sp["id"]])
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={
            "mode": "custom",
            "selected_item_ids": ids,
            "confirm_target_name": "tgt_db",
            "confirm_token": token,
        },
    )
    assert r.status_code == 200, r.text
    assert [s["object_name"] for s in r.json()["data"]["statements"]] == ["PROCEDURE:sp_x"]


def test_execute_stops_on_first_failure(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3526)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]

    def _fail_first(self, target, *, db_name, engine, lock_key, statements):
        # Falla la primera; la segunda no llega a ejecutarse (corte en el primer fallo).
        return [
            StatementResult(
                index=0, status="failed", error="boom", execution_ms=1,
                executed_at=datetime.now(timezone.utc),
            )
        ]

    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fail_first)
    token, _ = _token(admin_client, cid, "mysql", "all")
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={"mode": "all", "confirm_target_name": "tgt_db", "confirm_token": token},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["failed"] is True
    assert data["applied_count"] == 0
    statuses = [s["status"] for s in data["statements"]]
    assert statuses == ["failed", "skipped"]  # la 2ª quedó sin ejecutar


def test_execute_anti_toctou_409(admin_client, monkeypatch):
    src_id, tgt_id, _, fake = _setup(admin_client, monkeypatch, port=3527)
    cid = _create(admin_client, src_id, tgt_id).json()["data"]["id"]
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)

    token, _ = _token(admin_client, cid, "mysql", "all")
    # El esquema del target cambia DESPUÉS de calcular la comparación → fingerprint distinto.
    fake.snaps["tgt_db"] = _snap(db="tgt_db", marker="drifted")

    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/execute",
        json={"mode": "all", "confirm_target_name": "tgt_db", "confirm_token": token},
    )
    assert r.status_code == 409, r.text
    assert "cambió" in r.text.lower() or "recal" in r.text.lower()


# =========================================================================== #
# Extensión — comparar/ejecutar contra BDs CRUDAS (no registradas en inventario) #
# =========================================================================== #
def _setup_raw(admin_client, monkeypatch, *, port, raw_names=("raw_tgt",), rendered=None):
    """
    Servidor + una BD source GESTIONADA ('src_db') + N BDs CRUDAS no registradas cuyos
    snapshots existen en el motor (via fake.list_databases). Devuelve (sid, src_id, fake).
    """
    sid = _server(admin_client, port)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    snaps = {"src_db": _snap(db="src_db", marker="src")}
    for name in raw_names:
        snaps[name] = _snap(db=name, marker=name)
    fake = _FakeAdapter(snaps, rendered if rendered is not None else _rendered())
    monkeypatch.setattr(scc, "get_adapter", lambda target: fake)
    return sid, src_id, fake


def _create_raw(admin_client, src_id, sid, name):
    return admin_client.post(
        "/api/v1/schema-comparisons",
        json={
            "source_database_id": src_id,
            "target_server_id": sid,
            "target_database_name": name,
        },
    )


def _exec(admin_client, cid, mode, confirm_name, token, selected_item_ids=None):
    body = {"mode": mode, "confirm_target_name": confirm_name, "confirm_token": token}
    if selected_item_ids is not None:
        body["selected_item_ids"] = selected_item_ids
    return admin_client.post(f"/api/v1/schema-comparisons/{cid}/execute", json=body)


def test_create_with_raw_target_not_in_inventory(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3540)
    r = _create_raw(admin_client, src_id, sid, "raw_tgt")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["item_count"] == 3
    # La BD física del target se persiste siempre (server + nombre)...
    assert data["target_server_id"] == sid
    assert data["target_database_name"] == "raw_tgt"
    # ...pero al no estar en inventario, no hay managed_database_id (null → sin Opción A).
    assert data["target_database_id"] is None
    # El source (por id) SÍ está en inventario.
    assert data["source_database_id"] == src_id
    assert data["source_server_id"] == sid
    assert data["source_database_name"] == "src_db"


def test_create_raw_ref_autoresolves_to_managed(admin_client, monkeypatch):
    """
    Una referencia CRUDA (server_id + nombre) a una BD que YA está en el inventario debe
    tratarse IDÉNTICO a pasar su managed_database_id: auto-resuelve el mismo id y aplica
    el bloqueo por blueprint (que solo existe para targets gestionados con model_id).
    """
    src_id, tgt_id, _model_id, _ = _setup(
        admin_client, monkeypatch, port=3541, target_has_model=True
    )
    # Referencia canónica por id (para descubrir el sid del servidor).
    by_id = _create(admin_client, src_id, tgt_id).json()["data"]
    sid = by_id["target_server_id"]
    assert by_id["target_database_id"] == tgt_id

    # Misma BD física, pero referida CRUDAMENTE por (server_id + nombre).
    r = _create_raw(admin_client, src_id, sid, "tgt_db")
    assert r.status_code == 201, r.text
    by_raw = r.json()["data"]
    # Auto-resuelta al MISMO managed_database_id → tratada como gestionada.
    assert by_raw["target_database_id"] == tgt_id
    assert by_raw["target_server_id"] == sid
    assert by_raw["target_database_name"] == "tgt_db"

    # Y execute queda BLOQUEADO por blueprint (comportamiento exclusivo de target gestionado).
    blocked = admin_client.post(
        f"/api/v1/schema-comparisons/{by_raw['id']}/execute",
        json={"mode": "all", "confirm_target_name": "tgt_db", "confirm_token": "x"},
    )
    assert blocked.status_code == 409, blocked.text
    assert "adopt" in blocked.text.lower()


def test_create_raw_target_missing_in_engine_404(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3542)
    # 'ghost_db' no está entre las BDs reales del motor (fake.list_databases).
    r = _create_raw(admin_client, src_id, sid, "ghost_db")
    assert r.status_code == 404, r.text
    assert "no existe" in r.text.lower()


def test_create_validator_rejects_both_representations(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3543)
    r = admin_client.post(
        "/api/v1/schema-comparisons",
        json={
            "source_database_id": src_id,
            "target_database_id": 999,
            "target_server_id": sid,
            "target_database_name": "raw_tgt",
        },
    )
    assert r.status_code == 422, r.text


def test_create_validator_rejects_missing_representation(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3544)
    # source sin NINGUNA representación.
    r = admin_client.post(
        "/api/v1/schema-comparisons",
        json={"target_server_id": sid, "target_database_name": "raw_tgt"},
    )
    assert r.status_code == 422, r.text
    # raw PARCIAL: server_id sin database_name.
    r2 = admin_client.post(
        "/api/v1/schema-comparisons",
        json={"source_database_id": src_id, "target_server_id": sid},
    )
    assert r2.status_code == 422, r2.text


def test_execute_raw_target_end_to_end_all_modes(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3545)
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)
    cid = _create_raw(admin_client, src_id, sid, "raw_tgt").json()["data"]["id"]

    # mode=all (flujo real: execute-preview → execute con el token devuelto).
    prev = _preview(admin_client, cid, "all").json()["data"]
    assert prev["target_database_id"] is None  # sin inventario
    r = _exec(admin_client, cid, "all", "raw_tgt", prev["confirm_token"])
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["applied_count"] == 2  # new_t + old_t (excluye la rutina)
    assert d["failed"] is False
    assert d["target_database_id"] is None
    assert {s["object_name"] for s in d["statements"]} == {"new_t", "old_t"}

    # mode=all_except_destructive.
    prev2 = _preview(admin_client, cid, "all_except_destructive").json()["data"]
    r2 = _exec(admin_client, cid, "all_except_destructive", "raw_tgt", prev2["confirm_token"])
    assert r2.status_code == 200, r2.text
    assert [s["object_name"] for s in r2.json()["data"]["statements"]] == ["new_t"]

    # mode=custom (la rutina, solo alcanzable por custom).
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    sp = next(i for i in items if i["object_type"] == "routine")
    prev3 = _preview(admin_client, cid, "custom", selected_item_ids=[sp["id"]]).json()["data"]
    r3 = _exec(
        admin_client, cid, "custom", "raw_tgt", prev3["confirm_token"],
        selected_item_ids=[sp["id"]],
    )
    assert r3.status_code == 200, r3.text
    assert [s["object_name"] for s in r3.json()["data"]["statements"]] == ["PROCEDURE:sp_x"]


def test_execute_raw_target_token_parity_and_accepted(admin_client, monkeypatch):
    """El confirm_token del preview coincide con el algoritmo documentado y /execute lo acepta."""
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3546)
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)
    cid = _create_raw(admin_client, src_id, sid, "raw_tgt").json()["data"]["id"]

    manual_token, manual_ids = _token(admin_client, cid, "mysql", "all")
    prev = _preview(admin_client, cid, "all").json()["data"]
    assert prev["confirm_token"] == manual_token

    r = _exec(admin_client, cid, "all", "raw_tgt", prev["confirm_token"])
    assert r.status_code == 200, r.text
    assert r.json()["data"]["applied_count"] == len(manual_ids)


def test_execute_raw_target_wrong_confirm_name_422(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3547)
    monkeypatch.setattr(MigrationRunner, "execute_adhoc", _fake_exec_ok)
    cid = _create_raw(admin_client, src_id, sid, "raw_tgt").json()["data"]["id"]
    r = _exec(admin_client, cid, "all", "WRONG", "whatever")
    assert r.status_code == 422, r.text


def test_adopt_raw_target_422_use_execute(admin_client, monkeypatch):
    sid, src_id, _ = _setup_raw(admin_client, monkeypatch, port=3548)
    cid = _create_raw(admin_client, src_id, sid, "raw_tgt").json()["data"]["id"]
    items = admin_client.get(f"/api/v1/schema-comparisons/{cid}/items").json()["data"]
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{cid}/adopt",
        json={"selected_item_ids": [items[0]["id"]], "name": "v1"},
    )
    assert r.status_code == 422, r.text
    body = r.text.lower()
    assert "inventario" in body and "execute" in body
