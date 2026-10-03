"""
Verificación (sonda) de la credencial de DATOS por base: controller y ruta con adapter falso.

Lo que se afirma: la sonda corre con la credencial de DATOS (nunca la pseudo-root), un fallo borra
la verificación anterior y responde 422 con códigos cerrados (sin texto de grants ni del motor),
una sonda que no pudo correr tampoco deja verde, y la ruta respeta el lock por base.
"""

import json
from datetime import datetime

import pytest

from app.core.crypto import decrypt
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.models.managed_database import ManagedDatabase
from app.models.managed_database_data_credential import ManagedDatabaseDataCredential
from app.services.db_admin.readonly_probe import ReadonlyPreflight

PROVISION = "/api/v1/managed-databases/{db}/data-credential/provision"
VERIFY = "/api/v1/managed-databases/{db}/data-credential/verify"
CLEAR = "/api/v1/managed-databases/{db}/data-credential"

USAGE = "GRANT USAGE ON *.* TO `mcp_d_1`@`%`"
GREEN = {
    "grants": [USAGE, "GRANT SELECT ON `app\\_prod`.* TO `mcp_d_1`@`%`"],
    "lower_case_table_names": 0,
    "foreign_engine_tables": 0,
    "cross_schema_views": 0,
    "definer_views": 0,
}


def _server(admin_client, port=3398) -> int:
    payload = {
        "name": f"srv{port}",
        "host": "10.0.0.9",
        "port": port,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    return admin_client.post("/api/v1/servers", json=payload).json()["data"]["id"]


def _database(admin_client, name="app_prod", port=3398) -> int:
    sid = _server(admin_client, port)
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": f"own{port}"}
    ).json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases", json={"server_id": sid, "owner_id": oid, "name": name}
    )
    assert r.status_code == 201, r.text
    db_id = r.json()["data"]["id"]
    s = Database().get_declarative_base_session()
    try:
        s.get(ManagedDatabase, db_id).status = "active"
        s.commit()
    finally:
        s.close()
    return db_id


def _row(db_id):
    s = Database().get_declarative_base_session()
    try:
        row = (
            s.query(ManagedDatabaseDataCredential)
            .filter(ManagedDatabaseDataCredential.managed_database_id == db_id)
            .first()
        )
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


def _audit(action="managed_database.data_credential.verify"):
    s = Database().get_declarative_base_session()
    try:
        return [(a.status, a.detail) for a in s.query(AuditLog).all() if a.action == action]
    finally:
        s.close()


class _Motor:
    def __init__(self):
        self.facts = dict(GREEN)
        self.fail = None  # excepción que lanza la lectura de hechos
        self.targets = []  # (admin_user, password descifrable) con que se pidió la sonda
        self.databases = []
        self.exists = False

    def adapter(self, target):
        return _Adapter(self, target)


class _Adapter:
    def __init__(self, motor, target):
        self._m, self._t = motor, target

    def preflight_readonly_account(self, username, host):
        return ReadonlyPreflight(exists=self._m.exists)

    def is_privileged_role(self, username):
        return False

    def provision_data_account(self, username, password, host, database, preflight):
        self._m.exists = True

    def revoke_data_account(self, username, host, database):
        self._m.exists = False

    def data_credential_facts(self, database):
        self._m.targets.append((self._t.admin_user, self._t.admin_password))
        self._m.databases.append(database)
        if self._m.fail is not None:
            raise self._m.fail
        return dict(self._m.facts)


@pytest.fixture()
def motor(monkeypatch):
    import app.controllers.managed_database_controller as mdc

    m = _Motor()
    monkeypatch.setattr(mdc, "get_adapter", m.adapter)
    monkeypatch.setattr(mdc, "DB_NAME", "datum_meta")
    monkeypatch.setattr(mdc, "DB_HOST", "10.0.0.8")
    monkeypatch.setattr(mdc, "DB_PORT", 3390)
    return m


def _provisioned(admin_client) -> int:
    db_id = _database(admin_client)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    return db_id


def test_requires_auth(client):
    assert client.post(VERIFY.format(db=1)).status_code == 401


def test_verify_without_a_credential_is_409_and_never_probes(admin_client, motor):
    db_id = _database(admin_client)
    r = admin_client.post(VERIFY.format(db=db_id))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "data_credential.missing"
    assert motor.targets == []


def test_unknown_database_is_404(admin_client, motor):
    assert admin_client.post(VERIFY.format(db=999999)).status_code == 404


def test_provisioning_alone_does_not_verify(admin_client, motor):
    db_id = _provisioned(admin_client)
    assert _row(db_id).verified_at is None
    assert motor.targets == []


def test_green_probe_sets_verified_at_with_the_data_credential(admin_client, motor):
    db_id = _provisioned(admin_client)
    r = admin_client.post(VERIFY.format(db=db_id))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["verified_at"] is not None and data["probed_at"] is not None
    assert data["probe_violations"] == [] and data["probe_warnings"] == []
    row = _row(db_id)
    assert isinstance(row.verified_at, datetime)
    # Con la credencial de DATOS de esa base, descifrada; nunca la pseudo-root.
    user, password = motor.targets[0]
    assert user == f"mcp_d_{db_id}" and user != "root"
    assert password == decrypt(row.password_encrypted)
    assert motor.databases == ["app_prod"]
    assert ("success", "sonda superada: SELECT sobre exactamente esta base y nada más") in _audit()


def test_warnings_are_stored_but_do_not_block(admin_client, motor):
    db_id = _provisioned(admin_client)
    motor.facts.update(definer_views=1, cross_schema_views=1)
    r = admin_client.post(VERIFY.format(db=db_id))
    assert r.status_code == 200
    assert r.json()["data"]["probe_warnings"] == [
        "cross_schema_view_reference",
        "definer_views_present",
    ]
    assert _row(db_id).verified_at is not None


@pytest.mark.parametrize(
    "grants,reason",
    [
        ([USAGE, "GRANT SELECT ON `app\\_prod`.* TO `u`@`%`", "GRANT SELECT ON `b`.* TO `u`@`%`"],
         "CREDENTIAL_TOO_BROAD"),
        ([USAGE, "GRANT SELECT, INSERT ON `app\\_prod`.* TO `u`@`%`"], "WRITE_PRIVILEGE_PRESENT"),
    ],
)
def test_s30_s31_failing_probe_is_422_with_closed_codes(admin_client, motor, grants, reason):
    db_id = _provisioned(admin_client)
    motor.facts["grants"] = grants
    r = admin_client.post(VERIFY.format(db=db_id))
    assert r.status_code == 422
    body = r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "managed_database.data_probe_failed"
    assert reason in pc["reasons"] and pc["violations"]
    assert "`u`@`%`" not in body and "GRANT SELECT" not in body  # nada del texto del grant
    row = _row(db_id)
    assert row.verified_at is None and row.probed_at is not None
    assert json.loads(row.probe_violations)
    assert "GRANT" not in row.probe_violations
    assert _audit()[-1][0] == "failure"


def test_s32_amended_federated_table_blocks_the_credential(admin_client, motor):
    db_id = _provisioned(admin_client)
    motor.facts["foreign_engine_tables"] = 1
    r = admin_client.post(VERIFY.format(db=db_id))
    assert r.status_code == 422
    pc = r.json()["detail"]["public_context"]
    assert pc["reasons"] == ["FEDERATED_TABLE_PRESENT"] and pc["violations"] == ["foreign_engine_table"]
    assert _row(db_id).verified_at is None


def test_a_failing_probe_erases_the_previous_verification(admin_client, motor):
    db_id = _provisioned(admin_client)
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 200
    assert _row(db_id).verified_at is not None
    motor.facts["grants"] = [USAGE, "GRANT DELETE ON `app\\_prod`.* TO `u`@`%`"]
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 422
    assert _row(db_id).verified_at is None


def test_a_probe_that_could_not_run_also_erases_the_verification(admin_client, motor):
    db_id = _provisioned(admin_client)
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 200
    motor.fail = AppHttpException(message="El motor no contesta.", status_code=502)
    r = admin_client.post(VERIFY.format(db=db_id))
    assert r.status_code == 502
    row = _row(db_id)
    assert row.verified_at is None and row.probed_at is None and row.probe_violations is None
    assert _audit()[-1][0] == "error"


def test_reprovisioning_after_a_green_probe_requires_a_new_probe(admin_client, motor):
    db_id = _provisioned(admin_client)
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 200
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    assert _row(db_id).verified_at is None  # la contraseña nueva no está verificada


def test_verify_returns_409_while_a_provision_holds_the_database_lock(admin_client, motor):
    from app.controllers.server_controller import _release_provision, _try_acquire_provision

    db_id = _provisioned(admin_client)
    assert _try_acquire_provision(("data", db_id))
    try:
        r = admin_client.post(VERIFY.format(db=db_id))
        assert r.status_code == 409
        assert r.json()["detail"]["public_context"]["code"] == "data_credential.provision_in_progress"
        assert motor.targets == []
    finally:
        _release_provision(("data", db_id))
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 200


def test_the_lock_is_released_after_a_failing_probe(admin_client, motor):
    db_id = _provisioned(admin_client)
    motor.fail = AppHttpException(message="x", status_code=502)
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 502
    motor.fail = None
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 200


def test_clear_after_a_green_probe_cuts_the_verification(admin_client, motor):
    db_id = _provisioned(admin_client)
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 200
    assert admin_client.delete(CLEAR.format(db=db_id)).status_code == 200
    assert _row(db_id) is None
    assert admin_client.post(VERIFY.format(db=db_id)).status_code == 409


def test_catalog_codes_are_in_the_closed_vocabulary():
    from app.services import data_credential_catalog as dcodes

    assert dcodes.CODE_DATA_PROBE_FAILED in dcodes.ERROR_CODES
    assert dcodes.CODE_DATA_CREDENTIAL_MISSING in dcodes.ERROR_CODES
