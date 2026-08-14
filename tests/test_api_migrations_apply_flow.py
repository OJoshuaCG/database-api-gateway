"""
Tests del flujo de aplicación secuencial de migraciones (una sola llamada).

Cubre:
- `MigrationRunner.compute_pending` (lógica pura de secuenciación): nueva BD → todas;
  current=v2 + objetivo v5 → [3,4,5]; objetivo ≤ current → []; orden numérico.
- Guard `422` por versión objetivo inexistente (sin tocar el motor).
- Respuesta enriquecida de `apply` (from_version→to_version, no_op, mensajes) con el
  `MigrationRunner` mockeado (sin MySQL/PG real), igual que la suite mockea el adapter.
"""

from datetime import datetime

from app.services.db_admin.migrations import (
    MigrationResult,
    MigrationRunner,
    MigrationSpec,
)


# --------------------------------------------------------------------------- #
# Unit: compute_pending (el cerebro de la secuencia, sin motor)                #
# --------------------------------------------------------------------------- #
def _specs(versions):
    return [
        MigrationSpec(
            id=i + 1, version=v, name=f"m{v}", up_sql="SELECT 1",
            up_sql_mysql=None, up_sql_postgresql=None, down_sql=None, checksum="x",
        )
        for i, v in enumerate(versions)
    ]


def test_compute_pending_new_db_applies_all():
    specs = _specs(["0001", "0002", "0003", "0004", "0005"])
    pending = MigrationRunner.compute_pending(None, specs)
    assert [s.version for s in pending] == ["0001", "0002", "0003", "0004", "0005"]


def test_compute_pending_from_v2_to_latest():
    specs = _specs([f"{i:04d}" for i in range(1, 11)])  # 0001..0010
    pending = MigrationRunner.compute_pending("0002", specs)
    assert [s.version for s in pending] == [f"{i:04d}" for i in range(3, 11)]


def test_compute_pending_from_v2_up_to_v5():
    specs = _specs([f"{i:04d}" for i in range(1, 11)])
    pending = MigrationRunner.compute_pending("0002", specs, up_to_version="0005")
    assert [s.version for s in pending] == ["0003", "0004", "0005"]


def test_compute_pending_target_below_or_equal_current_is_noop():
    specs = _specs([f"{i:04d}" for i in range(1, 11)])
    assert MigrationRunner.compute_pending("0005", specs, up_to_version="0003") == []
    assert MigrationRunner.compute_pending("0005", specs, up_to_version="0005") == []


def test_compute_pending_numeric_order_not_lexicographic():
    # Lexicográficamente "0010" < "0009"; numéricamente 10 > 9. Debe respetar el orden real.
    specs = _specs(["0009", "0010"])
    pending = MigrationRunner.compute_pending("0008", specs)
    assert [s.version for s in pending] == ["0009", "0010"]


# --------------------------------------------------------------------------- #
# Helpers de API                                                               #
# --------------------------------------------------------------------------- #
def _blueprint_with_migrations(admin_client, n=5, slug="bp-apply"):
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    versions = {}
    for i in range(1, n + 1):
        v = f"{i:04d}"
        r = admin_client.post(
            f"/api/v1/database-models/{model_id}/migrations",
            json={"version": v, "name": f"m{v}",
                  "up_sql": f"CREATE TABLE t{i} (id INT PRIMARY KEY)"},
        )
        assert r.status_code == 201, r.text
        versions[v] = r.json()["data"]["id"]
    return model_id, versions


def _managed_db(admin_client, server_payload, model_id):
    r = admin_client.post("/api/v1/servers", json=server_payload())
    assert r.status_code == 201, r.text
    sid = r.json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner1"}
    )
    assert r.status_code == 201, r.text
    owner = r.json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "appdb", "server_id": sid, "owner_id": owner, "model_id": model_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _mr(version, mig_id, status="applied"):
    return MigrationResult(
        migration_id=mig_id, version=version, status=status,
        error=None, execution_ms=3, applied_at=datetime(2026, 6, 29, 12, 0, 0),
    )


# --------------------------------------------------------------------------- #
# API: guard de versión objetivo inexistente (sin motor)                       #
# --------------------------------------------------------------------------- #
def test_apply_target_version_not_in_blueprint_422(admin_client, server_payload):
    model_id, _ = _blueprint_with_migrations(admin_client, n=5)
    db_id = _managed_db(admin_client, server_payload, model_id)
    # 9999 no existe en el blueprint → 422 ANTES de tocar el motor.
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply?version=9999")
    assert r.status_code == 422, r.text
    assert "no existe en el blueprint" in r.text


# --------------------------------------------------------------------------- #
# API: respuesta enriquecida con runner mockeado                               #
# --------------------------------------------------------------------------- #
def test_apply_reports_from_to_version_single_call(
    admin_client, server_payload, monkeypatch
):
    model_id, vids = _blueprint_with_migrations(admin_client, n=5)
    db_id = _managed_db(admin_client, server_payload, model_id)

    # La BD está en v2; pedimos v5 → el runner (mockeado) aplica 3,4,5 de una sola vez.
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0002")
    monkeypatch.setattr(
        MigrationRunner, "apply",
        lambda self, *a, **k: [_mr("0003", vids["0003"]), _mr("0004", vids["0004"]), _mr("0005", vids["0005"])],
    )

    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply?version=0005")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["from_version"] == "0002"
    assert data["to_version"] == "0005"
    assert data["target_version"] == "0005"
    assert data["applied_count"] == 3
    assert data["no_op"] is False
    assert "0002 → 0005" in r.json()["message"]


def test_apply_already_latest_is_noop(admin_client, server_payload, monkeypatch):
    model_id, _ = _blueprint_with_migrations(admin_client, n=5)
    db_id = _managed_db(admin_client, server_payload, model_id)

    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0005")
    monkeypatch.setattr(MigrationRunner, "apply", lambda self, *a, **k: [])

    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["no_op"] is True
    assert data["applied_count"] == 0
    assert "más reciente" in r.json()["message"]


def test_apply_requested_version_below_current_is_noop_with_hint(
    admin_client, server_payload, monkeypatch
):
    model_id, _ = _blueprint_with_migrations(admin_client, n=5)
    db_id = _managed_db(admin_client, server_payload, model_id)

    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0005")
    monkeypatch.setattr(MigrationRunner, "apply", lambda self, *a, **k: [])

    # Pedimos v3 estando en v5 → no-op, sin downgrade, con pista a /rollback.
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply?version=0003")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["no_op"] is True
    assert data["target_version"] == "0003"
    assert "rollback" in r.json()["message"].lower()


def test_apply_dry_run_plan_lists_pending(admin_client, server_payload, monkeypatch):
    model_id, _ = _blueprint_with_migrations(admin_client, n=5)
    db_id = _managed_db(admin_client, server_payload, model_id)

    # dry_run usa get_current_version (mock) + compute_pending REAL (no mockeado).
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0002")

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true"
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["dry_run"] is True
    assert data["from_version"] == "0002"
    assert data["pending_versions"] == ["0003", "0004", "0005"]
    assert data["to_version"] == "0005"
    assert data["no_op"] is False


# --------------------------------------------------------------------------- #
# Gate de captura sin revisar: ACOTADO a las pendientes reales                  #
#                                                                             #
# ``apply?version=X`` aplica un prefijo ESTRICTO de la cadena, así que evaluar el gate sobre
# TODO el blueprint bloqueaba aplicaciones que no incluían la versión sin revisar — y también
# el dry_run, que no ejecuta nada.
# --------------------------------------------------------------------------- #
def _blueprint_with_capture_tail(admin_client, n=10, slug="bp-capture-scope"):
    """Blueprint 0001..n donde SOLO la última versión captura resultados (nace sin revisar)."""
    model_id, versions = _blueprint_with_migrations(admin_client, n=n - 1, slug=slug)
    v = f"{n:04d}"
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={
            "version": v,
            "name": f"m{v}",
            "up_sql": "SELECT COUNT(*) AS n FROM t1",
            "capture_selects": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["reviewed"] is False
    versions[v] = r.json()["data"]["id"]
    return model_id, versions


def test_apply_hasta_una_version_previa_no_lo_bloquea_una_captura_futura(
    admin_client, server_payload, monkeypatch
):
    model_id, vids = _blueprint_with_capture_tail(admin_client)
    db_id = _managed_db(admin_client, server_payload, model_id)

    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0005")
    monkeypatch.setattr(
        MigrationRunner, "apply",
        lambda self, *a, **k: [_mr("0006", vids["0006"]), _mr("0007", vids["0007"])],
    )

    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply?version=0007")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["applied_count"] == 2
    # Nada capturado: ninguna de las pendientes de esta corrida tiene captura.
    assert r.json()["data"]["captured_select_count"] == 0


def test_apply_que_si_incluye_la_version_con_captura_sin_revisar_da_409(
    admin_client, server_payload, monkeypatch
):
    model_id, _vids = _blueprint_with_capture_tail(admin_client)
    db_id = _managed_db(admin_client, server_payload, model_id)

    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0005")
    monkeypatch.setattr(MigrationRunner, "apply", lambda self, *a, **k: [])

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply"
        "?version=0010&allow_result_capture=true"
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["unreviewed_capture"] == ["0010"]


def test_dry_run_no_lo_bloquea_una_captura_sin_revisar(
    admin_client, server_payload, monkeypatch
):
    """Previsualizar no ejecuta nada: un gate que impide el dry_run solo esconde el plan."""
    model_id, _vids = _blueprint_with_capture_tail(admin_client)
    db_id = _managed_db(admin_client, server_payload, model_id)

    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0005")

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true"
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["pending_versions"][-1] == "0010"


def test_el_kill_switch_global_neutraliza_el_gate_de_reviewed(
    admin_client, server_payload, monkeypatch
):
    """
    Con la captura DESACTIVADA globalmente el codegen no emite una sola llamada de captura:
    el 409 bloqueaba una vía de recuperación por un riesgo que no puede materializarse.
    """
    from app.controllers import managed_migration_controller as mod

    model_id, vids = _blueprint_with_capture_tail(admin_client)
    db_id = _managed_db(admin_client, server_payload, model_id)

    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0009")
    monkeypatch.setattr(
        MigrationRunner, "apply", lambda self, *a, **k: [_mr("0010", vids["0010"])]
    )
    monkeypatch.setattr(mod, "MIGRATION_CAPTURE_ENABLED", False)

    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply?version=0010")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["applied_count"] == 1
