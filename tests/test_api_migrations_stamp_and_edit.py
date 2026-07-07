"""
Tests de dos comportamientos del módulo de migraciones de blueprints:

1. `stamp` limpia la cuarentena: tras un apply fallido que deja la BD gestionada en
   `status=error` (con `notes`), un `stamp` exitoso posterior la vuelve a `active` y
   limpia `notes` (`ManagedMigrationController._set_quarantine`, ver `stamp()`). Sobre
   una BD que nunca estuvo en error, el stamp es neutro (no rompe nada).
2. Edición de `up_sql` en una migración de blueprint (`update_migration`):
   - Permitida si NO hay ninguna aplicación EXITOSA registrada (`_has_successful_
     application`), incluso si hay historial de intentos FALLIDOS (distinto de
     `_has_history`, que cubre cualquier intento).
   - Bloqueada (409, fix-forward) si ya se aplicó exitosamente en alguna BD.
   - Bloqueada (409) si deja "colgando" un override (`up_sql_mysql`/`up_sql_postgresql`)
     preexistente que no se reenvía en el mismo PATCH; permitida si se reenvía (mismo
     valor, valor corregido, o `null` explícito).
   - Al aplicar el cambio: se regenera `down_sql_suggested` y cambia el `checksum`.

Todo el motor remoto se mockea (`MigrationRunner` parcheado con monkeypatch); el
historial de aplicación se inserta directamente en la BD de metadatos (SQLite) para
no depender de un apply real end-to-end, igual que
`test_api_managed_databases.py::test_provision_failure_writes_error_audit`.
"""

from datetime import datetime

from app.services.db_admin.migrations import MigrationResult, MigrationRunner


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _blueprint_with_migration(admin_client, slug="stamp-bp", up_sql=None):
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={
            "version": "0001",
            "name": "m1",
            "up_sql": up_sql or "CREATE TABLE t1 (id INT PRIMARY KEY)",
        },
    )
    assert r.status_code == 201, r.text
    return model_id, r.json()["data"]["id"]


def _managed_db(admin_client, server_payload, model_id, name="appdb", **ov):
    r = admin_client.post("/api/v1/servers", json=server_payload(**ov))
    assert r.status_code == 201, r.text
    sid = r.json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner1"}
    )
    assert r.status_code == 201, r.text
    owner = r.json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": name, "server_id": sid, "owner_id": owner, "model_id": model_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _set_db_status(db_id, status):
    """Fija el status de una ManagedDatabase directamente en la metadata (determinista,
    sin depender del flujo de provisión ni de mocks de adapter — a prueba de orden)."""
    from app.core.database import Database
    from app.models.enums import ProvisionStatus
    from app.models.managed_database import ManagedDatabase

    s = Database().get_declarative_base_session()
    try:
        md = s.get(ManagedDatabase, db_id)
        md.status = ProvisionStatus(status)
        s.commit()
    finally:
        s.close()


def _mr(version, mig_id, status="applied"):
    return MigrationResult(
        migration_id=mig_id, version=version, status=status,
        error="boom" if status == "failed" else None, execution_ms=3,
        applied_at=datetime(2026, 6, 29, 12, 0, 0),
    )


def _insert_history(db_id, mig_id, status, when=None):
    """Inserta una fila de historial directamente (sin pasar por un apply real)."""
    from app.core.database import Database
    from app.models.database_migration_history import DatabaseMigrationHistory
    from app.models.enums import MigrationStatus

    s = Database().get_declarative_base_session()
    try:
        s.add(
            DatabaseMigrationHistory(
                managed_database_id=db_id,
                model_migration_id=mig_id,
                applied_at=when or datetime.now(),
                status=MigrationStatus(status),
                error=None if status == "applied" else "boom",
                execution_ms=1,
            )
        )
        s.commit()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Comportamiento 1 — stamp limpia la cuarentena                                #
# --------------------------------------------------------------------------- #
def test_stamp_clears_quarantine_after_failed_apply(
    admin_client, server_payload, monkeypatch
):
    model_id, mig_id = _blueprint_with_migration(admin_client, slug="stamp-a")
    db_id = _managed_db(admin_client, server_payload, model_id, port=3501)

    # 1) Apply fallido real (vía runner mockeado) → deja la BD en cuarentena.
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: None)
    monkeypatch.setattr(
        MigrationRunner, "apply", lambda self, *a, **k: [_mr("0001", mig_id, status="failed")]
    )
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 200, r.text

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "error"
    assert r.json()["data"]["notes"]

    # 2) Stamp exitoso (motor mockeado: no ejecuta SQL) → limpia la cuarentena.
    monkeypatch.setattr(MigrationRunner, "stamp", lambda self, *a, **k: None)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0001")
    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/stamp?version=0001"
    )
    assert r.status_code == 200, r.text

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "active"
    assert r.json()["data"]["notes"] is None


def test_stamp_on_healthy_database_is_neutral(admin_client, server_payload, monkeypatch):
    model_id, mig_id = _blueprint_with_migration(admin_client, slug="stamp-b")
    db_id = _managed_db(admin_client, server_payload, model_id, port=3502)
    # Precondición determinista: BD sana (active), sin pasar por el flujo de provisión.
    _set_db_status(db_id, "active")

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}")
    assert r.json()["data"]["status"] == "active"

    monkeypatch.setattr(MigrationRunner, "stamp", lambda self, *a, **k: None)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0001")
    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/stamp?version=0001"
    )
    assert r.status_code == 200, r.text

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "active"
    assert r.json()["data"]["notes"] is None


# --------------------------------------------------------------------------- #
# Comportamiento 3 — editar up_sql                                             #
# --------------------------------------------------------------------------- #
def test_patch_up_sql_allowed_without_history(admin_client):
    model_id, _mig_id = _blueprint_with_migration(admin_client, slug="edit-a")
    before = admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).json()["data"]

    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql": "CREATE TABLE t1_fixed (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["up_sql"] == "CREATE TABLE t1_fixed (id INT PRIMARY KEY)"
    assert data["down_sql_suggested"] == "DROP TABLE IF EXISTS t1_fixed;"
    assert data["down_sql_suggested"] != before["down_sql_suggested"]
    assert data["checksum"] != before["checksum"]


def test_patch_up_sql_blocked_after_successful_application(admin_client, server_payload):
    model_id, mig_id = _blueprint_with_migration(admin_client, slug="edit-b")
    db_id = _managed_db(admin_client, server_payload, model_id, port=3503)
    _insert_history(db_id, mig_id, "applied")

    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql": "CREATE TABLE t1_fixed (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 409, r.text
    body = r.text.lower()
    assert "fix-forward" in body or "no se puede modificar" in body


def test_patch_up_sql_allowed_when_history_is_only_failed(admin_client, server_payload):
    """Guard relajado: _has_successful_application, NO _has_history."""
    model_id, mig_id = _blueprint_with_migration(admin_client, slug="edit-c")
    db_id = _managed_db(admin_client, server_payload, model_id, port=3504)
    _insert_history(db_id, mig_id, "failed")

    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql": "CREATE TABLE t1_fixed (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["up_sql"] == "CREATE TABLE t1_fixed (id INT PRIMARY KEY)"


def test_patch_up_sql_blocked_by_stale_override_unless_resent(admin_client):
    model_id, _mig_id = _blueprint_with_migration(admin_client, slug="edit-d")
    # Crea el override PostgreSQL preexistente.
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql_postgresql": "CREATE TABLE t1 (id SERIAL PRIMARY KEY)"},
    )
    assert r.status_code == 200, r.text

    # 1) Cambiar up_sql SIN reenviar el override → 409 (override quedaría obsoleto).
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql": "CREATE TABLE t1_v2 (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 409, r.text
    assert "override" in r.text.lower()

    # 2) Reenviando el override (corregido) en la misma llamada → 200.
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={
            "up_sql": "CREATE TABLE t1_v2 (id INT PRIMARY KEY)",
            "up_sql_postgresql": "CREATE TABLE t1_v2 (id SERIAL PRIMARY KEY)",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["up_sql"] == "CREATE TABLE t1_v2 (id INT PRIMARY KEY)"
    assert r.json()["data"]["translated"]["postgresql"] == (
        "CREATE TABLE t1_v2 (id SERIAL PRIMARY KEY)"
    )


def test_patch_up_sql_with_null_override_explicit_is_allowed(admin_client):
    model_id, _mig_id = _blueprint_with_migration(admin_client, slug="edit-e")
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql_postgresql": "CREATE TABLE t1 (id SERIAL PRIMARY KEY)"},
    )
    assert r.status_code == 200, r.text

    # Enviar up_sql_postgresql=null explícito junto con up_sql → limpia el override, 200.
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={
            "up_sql": "CREATE TABLE t1_v3 (id INT PRIMARY KEY)",
            "up_sql_postgresql": None,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["up_sql"] == "CREATE TABLE t1_v3 (id INT PRIMARY KEY)"
    # Sin override persistido, la traducción cross-engine se recalcula desde el nuevo
    # up_sql (ya no es el override viejo, que referenciaba t1/SERIAL).
    assert "t1_v3" in data["translated"]["postgresql"]
