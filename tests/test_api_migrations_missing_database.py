"""
Migraciones sobre una BD que NO existe en el motor destino.

Es el caso del incidente: una fila registrada sin aprovisionar (``pending``) o una base
borrada por fuera del gateway. Antes, ``GET /migrations/status`` propagaba el 404 crudo del
driver ("El recurso solicitado no existe en el servidor destino", errno 1049) —indistinguible
del 404 de "BD no encontrada en el inventario"— y ``apply``/``stamp`` seguían hasta
``_set_quarantine``, dejando la BD en ``error`` y enmascarando la causa real.

Ahora: la LECTURA informa ``database_exists: false`` con 200, y todo lo que EJECUTA responde
409 ``managed_database.not_provisioned`` antes de tocar nada.
"""

import app.controllers.managed_migration_controller as mmc
from app.exceptions import AppHttpException
from app.models.enums import ProvisionStatus
from app.services import provisioning_catalog as pcodes
from app.services.db_admin.migrations import MigrationRunner

DB_NAME = "appdb"


def _blueprint(admin_client, n=3, slug="bp-missing"):
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    for i in range(1, n + 1):
        r = admin_client.post(
            f"/api/v1/database-models/{model_id}/migrations",
            json={
                "version": f"{i:04d}",
                "name": f"m{i}",
                "up_sql": f"CREATE TABLE t{i} (id INT PRIMARY KEY)",
                "down_sql": f"DROP TABLE t{i}",
            },
        )
        assert r.status_code == 201, r.text
    return model_id


def _managed_db(admin_client, server_payload, model_id):
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    owner = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner1"}
    ).json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": DB_NAME, "server_id": sid, "owner_id": owner, "model_id": model_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _unknown_db_error(code="1049"):
    """Lo que ``map_driver_error`` produce ante un errno 1049 / SQLSTATE 3D000."""
    return AppHttpException(
        "El recurso solicitado no existe en el servidor destino.",
        404,
        context={
            "op": "migration_status",
            "dialect": "mariadb",
            "remote_error_code": code,
            "database": DB_NAME,
        },
    )


class _Adapter:
    def __init__(self, live=()):
        self.live = list(live)

    def list_databases(self):
        return list(self.live)


def _engine_says_missing(monkeypatch, code="1049", live=()):
    """El runner falla con 1049/3D000 y el adapter confirma (o no) la ausencia."""

    def _raise(self, *a, **k):
        raise _unknown_db_error(code)

    monkeypatch.setattr(MigrationRunner, "get_current_version", _raise)
    monkeypatch.setattr(mmc, "get_adapter", lambda target: _Adapter(live))


# --------------------------------------------------------------------------- #
# Lectura: 200 con database_exists=false                                       #
# --------------------------------------------------------------------------- #
def test_status_reports_missing_database_instead_of_404(
    admin_client, server_payload, monkeypatch
):
    model_id = _blueprint(admin_client, n=3)
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch)

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/status")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["database_exists"] is False
    assert data["current_version"] is None
    # Todas las del blueprint quedan pendientes: no hay base donde nada esté aplicado.
    assert data["pending_versions"] == ["0001", "0002", "0003"]
    assert data["pending_count"] == 3


def test_status_handles_postgres_sqlstate(admin_client, server_payload, monkeypatch):
    model_id = _blueprint(admin_client, n=1, slug="bp-pg")
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch, code="3D000")

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/status")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["database_exists"] is False


def test_other_404_from_driver_is_not_swallowed(
    admin_client, server_payload, monkeypatch
):
    """Un 404 con OTRO código nativo (p. ej. 1008) se propaga tal cual."""
    model_id = _blueprint(admin_client, n=1, slug="bp-other")
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch, code="1008")

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/status")
    assert r.status_code == 404, r.text


def test_1049_with_database_present_is_not_reported_as_missing(
    admin_client, server_payload, monkeypatch
):
    """
    Un 1049 con la BD PRESENTE tiene otra causa (permisos, carrera con un drop). Afirmar
    "no existe" mandaría al operador a crear una base que sí está: se propaga el 404.
    """
    model_id = _blueprint(admin_client, n=1, slug="bp-present")
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch, live=[DB_NAME])

    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/status")
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------- #
# Ejecución: 409 accionable, y SIN cuarentena                                  #
# --------------------------------------------------------------------------- #
def _assert_not_provisioned(r):
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == pcodes.CODE_NOT_PROVISIONED
    assert "/provision" in r.json()["detail"]["msg"]


def _status_of(admin_client, db_id):
    return admin_client.get(f"/api/v1/managed-databases/{db_id}").json()["data"]["status"]


def test_apply_conflicts_and_does_not_quarantine(
    admin_client, server_payload, monkeypatch
):
    model_id = _blueprint(admin_client, n=2, slug="bp-apply-missing")
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch)

    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    _assert_not_provisioned(r)
    # LO ESENCIAL: la BD NO quedó marcada en cuarentena por un fallo que no es de migración.
    assert _status_of(admin_client, db_id) == ProvisionStatus.pending.value


def test_apply_dry_run_still_informs(admin_client, server_payload, monkeypatch):
    """El dry-run es diagnóstico: informa y no falla (mismo criterio que la cuarentena)."""
    model_id = _blueprint(admin_client, n=2, slug="bp-dry")
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch)

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true"
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["database_exists"] is False
    assert data["no_op"] is True


def test_rollback_conflicts_with_provision_message_not_no_migrations(
    admin_client, server_payload, monkeypatch
):
    """El 409 de 'no hay migraciones para revertir' MIENTE si la base no existe."""
    model_id = _blueprint(admin_client, n=2, slug="bp-rb")
    db_id = _managed_db(admin_client, server_payload, model_id)
    _engine_says_missing(monkeypatch)

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version=0001"
    )
    _assert_not_provisioned(r)
    assert "para revertir" not in r.json()["detail"]["msg"]


def test_stamp_conflicts_and_does_not_write_version(
    admin_client, server_payload, monkeypatch
):
    """
    ``runner.stamp`` ya falla solo contra una base ausente (abre conexión al destino), así que
    nunca llega a ``_set_model_version``. Lo que agrega el guard es el MENSAJE: el 404 crudo
    del driver no distingue "la base no existe" de "la BD gestionada no está en el inventario".
    """

    def _raise(self, *a, **k):
        raise _unknown_db_error()

    monkeypatch.setattr(MigrationRunner, "stamp", _raise)
    monkeypatch.setattr(mmc, "get_adapter", lambda target: _Adapter([]))
    model_id = _blueprint(admin_client, n=2, slug="bp-stamp")
    db_id = _managed_db(admin_client, server_payload, model_id)

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/stamp?version=0002"
    )
    _assert_not_provisioned(r)
    row = admin_client.get(f"/api/v1/managed-databases/{db_id}").json()["data"]
    assert row["model_version"] is None
    assert row["status"] == ProvisionStatus.pending.value


def test_stamp_on_reachable_database_is_untouched(
    admin_client, server_payload, monkeypatch
):
    """Con el destino sano, ``stamp`` NO paga ningún round-trip extra (no se toca el adapter)."""
    monkeypatch.setattr(MigrationRunner, "stamp", lambda self, *a, **k: None)
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: "0002")

    def _boom(target):
        raise AssertionError("stamp no debe llamar a list_databases en el camino feliz")

    monkeypatch.setattr(mmc, "get_adapter", _boom)
    model_id = _blueprint(admin_client, n=2, slug="bp-stamp-ok")
    db_id = _managed_db(admin_client, server_payload, model_id)

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/stamp?version=0002"
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["database_exists"] is True
