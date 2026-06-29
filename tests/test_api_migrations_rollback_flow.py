"""
Tests del rollback secuencial a una versión objetivo (una sola llamada).

El motor se mockea (igual que la suite): se monkepatchea `MigrationRunner.get_current_version`
y `rollback_to`. Cubre: validaciones (confirm, target ≥ actual, target inexistente, down_sql
faltante en el camino), respuesta enriquecida (from→to, reverted_versions) y la sincronización
de `model_version` en el inventario del gateway tras revertir.
"""

from datetime import datetime

from app.services.db_admin.migrations import MigrationResult, MigrationRunner


def _bp_with_downsql(admin_client, versions_with_down):
    """Crea un blueprint con migraciones; `versions_with_down` = {version: bool down_sql}."""
    r = admin_client.post("/api/v1/database-models", json={"name": "rb", "slug": "rb-bp"})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    for i, (v, has_down) in enumerate(versions_with_down.items(), start=1):
        body = {"version": v, "name": f"m{v}", "up_sql": f"CREATE TABLE t{i} (id INT PRIMARY KEY)"}
        if has_down:
            body["down_sql"] = f"DROP TABLE t{i}"
        r = admin_client.post(f"/api/v1/database-models/{model_id}/migrations", json=body)
        assert r.status_code == 201, r.text
    return model_id


def _managed_db(admin_client, server_payload, model_id, port=3500):
    r = admin_client.post("/api/v1/servers", json=server_payload(port=port))
    assert r.status_code == 201, r.text
    sid = r.json()["data"]["id"]
    r = admin_client.post("/api/v1/server-users", json={"server_id": sid, "username": "o"})
    assert r.status_code == 201, r.text
    owner = r.json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "appdb", "server_id": sid, "owner_id": owner, "model_id": model_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _mr(version, status="applied"):
    return MigrationResult(
        migration_id=int(version), version=version, status=status,
        error=None, execution_ms=2, applied_at=datetime(2026, 6, 29, 12, 0, 0),
    )


def _seq_current(monkeypatch, values):
    """get_current_version devuelve `values` en orden (current antes, new_current después)."""
    it = iter(values)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: next(it))


# --------------------------------------------------------------------------- #
# Éxito: rollback secuencial v4 → v2 en una sola llamada                       #
# --------------------------------------------------------------------------- #
def test_rollback_to_target_sequential_updates_version(
    admin_client, server_payload, monkeypatch
):
    model_id = _bp_with_downsql(
        admin_client, {"0001": True, "0002": True, "0003": True, "0004": True}
    )
    db_id = _managed_db(admin_client, server_payload, model_id)

    _seq_current(monkeypatch, ["0004", "0002"])  # actual=0004; tras revertir=0002
    monkeypatch.setattr(
        MigrationRunner, "rollback_to",
        lambda self, *a, **k: [_mr("0004"), _mr("0003")],  # revierte 0004 y 0003
    )

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback"
        f"?confirm_version=0004&target_version=0002"
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["from_version"] == "0004"
    assert data["to_version"] == "0002"
    assert data["target_version"] == "0002"
    assert data["reverted_count"] == 2
    assert data["reverted_versions"] == ["0004", "0003"]
    assert data["failed"] is False
    assert "0004 → 0002" in r.json()["message"]

    # La versión quedó sincronizada en el inventario del gateway (bug reportado).
    r = admin_client.get(f"/api/v1/managed-databases/{db_id}")
    assert r.json()["data"]["model_version"] == "0002"


def test_rollback_default_single_step(admin_client, server_payload, monkeypatch):
    model_id = _bp_with_downsql(admin_client, {"0001": True, "0002": True, "0003": True})
    db_id = _managed_db(admin_client, server_payload, model_id, port=3501)

    _seq_current(monkeypatch, ["0003", "0002"])
    monkeypatch.setattr(MigrationRunner, "rollback_to", lambda self, *a, **k: [_mr("0003")])

    # Sin target_version → revierte solo la última (0003 → 0002).
    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version=0003"
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["reverted_count"] == 1
    assert data["from_version"] == "0003"
    assert data["to_version"] == "0002"


# --------------------------------------------------------------------------- #
# Validaciones (sin tocar el motor)                                            #
# --------------------------------------------------------------------------- #
def test_rollback_confirm_mismatch_422(admin_client, server_payload, monkeypatch):
    model_id = _bp_with_downsql(admin_client, {"0001": True, "0002": True})
    db_id = _managed_db(admin_client, server_payload, model_id, port=3502)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0002")

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version=0001"
    )
    assert r.status_code == 422, r.text


def test_rollback_target_not_below_current_422(admin_client, server_payload, monkeypatch):
    model_id = _bp_with_downsql(admin_client, {"0001": True, "0002": True})
    db_id = _managed_db(admin_client, server_payload, model_id, port=3503)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0002")

    # target ≥ actual → 422 (para avanzar se usa apply).
    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback"
        f"?confirm_version=0002&target_version=0002"
    )
    assert r.status_code == 422, r.text


def test_rollback_target_not_in_blueprint_422(admin_client, server_payload, monkeypatch):
    # Migraciones 0001, 0002, 0004 (hueco en 0003).
    model_id = _bp_with_downsql(admin_client, {"0001": True, "0002": True, "0004": True})
    db_id = _managed_db(admin_client, server_payload, model_id, port=3504)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0004")

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback"
        f"?confirm_version=0004&target_version=0003"
    )
    assert r.status_code == 422, r.text
    assert "no existe" in r.text


def test_rollback_missing_down_sql_in_path_409(admin_client, server_payload, monkeypatch):
    # 0002 NO tiene down_sql confirmado → revertir de 0003 a 0001 debe fallar (409).
    model_id = _bp_with_downsql(admin_client, {"0001": True, "0002": False, "0003": True})
    db_id = _managed_db(admin_client, server_payload, model_id, port=3505)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0003")

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback"
        f"?confirm_version=0003&target_version=0001"
    )
    assert r.status_code == 409, r.text
    assert "0002" in r.text  # señala la versión sin rollback confirmado
