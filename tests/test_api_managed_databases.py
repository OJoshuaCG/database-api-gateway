"""
Endpoints de ManagedDatabases: CRUD de inventario, integridad owner↔servidor,
unicidad, y flujos de aprovisionamiento (adapter mockeado, sin motor real).
"""

from app.exceptions import AppHttpException


def _server(admin_client, port, **ov) -> int:
    payload = {
        "name": f"srv{port}",
        "host": "10.0.0.9",
        "port": port,
        "engine": "postgresql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    return admin_client.post("/api/v1/servers", json=payload).json()["data"]["id"]


def _owner(admin_client, server_id, username="owner1") -> int:
    return admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    ).json()["data"]["id"]


def test_requires_auth(client):
    assert client.get("/api/v1/managed-databases").status_code == 401


def test_create_inventory_only_pending(admin_client):
    sid = _server(admin_client, 5440)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "app_db"},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["status"] == "pending"
    assert data["owner_id"] == oid


def test_owner_on_other_server_conflict(admin_client):
    s1 = _server(admin_client, 5441)
    s2 = _server(admin_client, 5442)
    owner_s2 = _owner(admin_client, s2, "foreign")
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": s1, "owner_id": owner_s2, "name": "x_db"},
    )
    assert r.status_code == 409


def test_owner_missing_422(admin_client):
    sid = _server(admin_client, 5443)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": 9999, "name": "y_db"},
    )
    assert r.status_code == 422


def test_bad_model_id_422(admin_client):
    sid = _server(admin_client, 5444)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "z_db", "model_id": 9999},
    )
    assert r.status_code == 422


def test_duplicate_name_per_server_conflict(admin_client):
    sid = _server(admin_client, 5445)
    oid = _owner(admin_client, sid)
    body = {"server_id": sid, "owner_id": oid, "name": "dup_db"}
    assert admin_client.post("/api/v1/managed-databases", json=body).status_code == 201
    assert admin_client.post("/api/v1/managed-databases", json=body).status_code == 409


def test_invalid_name_422(admin_client):
    sid = _server(admin_client, 5446)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "bad-db.name"},
    )
    assert r.status_code == 422


def test_list_filters(admin_client):
    sid = _server(admin_client, 5447)
    oid = _owner(admin_client, sid)
    admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "f1"},
    )
    data = admin_client.get(
        f"/api/v1/managed-databases?server_id={sid}&status=pending"
    ).json()["data"]
    assert len(data) == 1 and data[0]["name"] == "f1"


def test_update_and_delete(admin_client):
    sid = _server(admin_client, 5448)
    oid = _owner(admin_client, sid)
    did = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "life_db"},
    ).json()["data"]["id"]
    upd = admin_client.patch(
        f"/api/v1/managed-databases/{did}", json={"model_version": "2.0.0", "notes": "n"}
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["model_version"] == "2.0.0"
    assert admin_client.delete(f"/api/v1/managed-databases/{did}").status_code == 200
    assert admin_client.get(f"/api/v1/managed-databases/{did}").status_code == 404


def test_reassign_owner_gw_only(admin_client):
    sid = _server(admin_client, 5449)
    o1 = _owner(admin_client, sid, "o1")
    o2 = _owner(admin_client, sid, "o2")
    did = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": o1, "name": "re_db"},
    ).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/managed-databases/{did}/reassign-owner", json={"owner_id": o2}
    )
    assert r.status_code == 200
    assert r.json()["data"]["owner_id"] == o2


def test_reassign_owner_other_server_conflict(admin_client):
    s1 = _server(admin_client, 5450)
    s2 = _server(admin_client, 5451)
    o1 = _owner(admin_client, s1, "a")
    o_foreign = _owner(admin_client, s2, "b")
    did = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": s1, "owner_id": o1, "name": "rb_db"},
    ).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/managed-databases/{did}/reassign-owner", json={"owner_id": o_foreign}
    )
    assert r.status_code == 409


def test_provision_creates_without_granting(admin_client, monkeypatch):
    """Provisionar CREA la BD pero NO otorga privilegios al propietario (política:
    sin privilegios por defecto; jamás ALL PRIVILEGES)."""
    import app.controllers.managed_database_controller as mdc

    calls = []

    class FakeAdapter:
        def create_database(self, name, charset=None, collation=None, owner=None):
            calls.append(("create_database", name, owner))

        def grant_database(self, username, db_name, host="%", privileges="ALL PRIVILEGES"):
            calls.append(("grant_database", username, db_name))

    monkeypatch.setattr(mdc, "get_adapter", lambda target: FakeAdapter())
    sid = _server(admin_client, 5452)
    oid = _owner(admin_client, sid, "powner")
    r = admin_client.post(
        "/api/v1/managed-databases?provision=true",
        json={"server_id": sid, "owner_id": oid, "name": "prov_db"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["status"] == "active"
    assert ("create_database", "prov_db", "powner") in calls
    # NINGÚN grant automático: el propietario no recibe privilegios por defecto.
    assert not any(c[0] == "grant_database" for c in calls)


def test_provision_failure_marks_error(admin_client, monkeypatch):
    import app.controllers.managed_database_controller as mdc

    class FakeAdapter:
        def create_database(self, *a, **k):
            raise AppHttpException("motor inaccesible", 502)

    monkeypatch.setattr(mdc, "get_adapter", lambda target: FakeAdapter())
    sid = _server(admin_client, 5453)
    oid = _owner(admin_client, sid, "eowner")
    r = admin_client.post(
        "/api/v1/managed-databases?provision=true",
        json={"server_id": sid, "owner_id": oid, "name": "err_db"},
    )
    assert r.status_code == 502
    # El registro se conserva en estado 'error' (sin rollback silencioso).
    data = admin_client.get(f"/api/v1/managed-databases?server_id={sid}").json()["data"]
    assert len(data) == 1
    assert data[0]["status"] == "error"
    assert data[0]["name"] == "err_db"


def test_delete_drop_remote_calls_engine(admin_client, monkeypatch):
    import app.controllers.managed_database_controller as mdc

    dropped = []

    class FakeAdapter:
        def drop_database(self, name):
            dropped.append(name)

    monkeypatch.setattr(mdc, "get_adapter", lambda target: FakeAdapter())
    sid = _server(admin_client, 5454)
    oid = _owner(admin_client, sid, "downer")
    did = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "drop_db"},
    ).json()["data"]["id"]
    # Confirmación explícita: confirm_name debe coincidir con el nombre de la BD.
    r = admin_client.delete(
        f"/api/v1/managed-databases/{did}?drop_remote=true&confirm_name=drop_db"
    )
    assert r.status_code == 200
    assert dropped == ["drop_db"]


def test_delete_drop_remote_requires_confirmation(admin_client, monkeypatch):
    """Sin confirm_name (o con uno incorrecto) NO se toca el motor → 422."""
    import app.controllers.managed_database_controller as mdc

    dropped = []

    class FakeAdapter:
        def drop_database(self, name):
            dropped.append(name)

    monkeypatch.setattr(mdc, "get_adapter", lambda target: FakeAdapter())
    sid = _server(admin_client, 5470)
    oid = _owner(admin_client, sid, "nc_owner")
    did = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "noconfirm_db"},
    ).json()["data"]["id"]

    # Falta confirm_name.
    r = admin_client.delete(f"/api/v1/managed-databases/{did}?drop_remote=true")
    assert r.status_code == 422
    # confirm_name incorrecto.
    r2 = admin_client.delete(
        f"/api/v1/managed-databases/{did}?drop_remote=true&confirm_name=wrong"
    )
    assert r2.status_code == 422
    # No se ejecutó ningún DROP en el motor y el registro sigue existiendo.
    assert dropped == []
    assert admin_client.get(f"/api/v1/managed-databases/{did}").status_code == 200


# NOTA: el test de compensación de GRANT se eliminó porque ya no hay paso de GRANT
# automático en create_database (política: sin privilegios por defecto). Crear la BD
# es ahora la única operación remota; su fallo se cubre en test_provision_failure_marks_error.


def test_reassign_provision_calls_engine(admin_client, monkeypatch):
    import app.controllers.managed_database_controller as mdc

    calls = {}

    class FakeAdapter:
        def reassign_database_owner(
            self, db_name, new_owner, *, new_host="%", old_owner=None, old_host="%"
        ):
            calls["reassign"] = (db_name, new_owner, old_owner)

    monkeypatch.setattr(mdc, "get_adapter", lambda target: FakeAdapter())
    sid = _server(admin_client, 5460)
    o1 = _owner(admin_client, sid, "ro1")
    o2 = _owner(admin_client, sid, "ro2")
    did = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": o1, "name": "reasg_db"},
    ).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/managed-databases/{did}/reassign-owner?provision=true",
        json={"owner_id": o2},
    )
    assert r.status_code == 200, r.text
    assert calls["reassign"] == ("reasg_db", "ro2", "ro1")
    assert r.json()["data"]["owner_id"] == o2


def test_status_filter_invalid_422(admin_client):
    sid = _server(admin_client, 5461)
    r = admin_client.get(f"/api/v1/managed-databases?server_id={sid}&status=bogus")
    assert r.status_code == 422


def test_provision_failure_writes_error_audit(admin_client, monkeypatch):
    import app.controllers.managed_database_controller as mdc

    class FakeAdapter:
        def create_database(self, *a, **k):
            raise AppHttpException("motor inaccesible", 502)

    monkeypatch.setattr(mdc, "get_adapter", lambda target: FakeAdapter())
    sid = _server(admin_client, 5462)
    oid = _owner(admin_client, sid, "aowner")
    r = admin_client.post(
        "/api/v1/managed-databases?provision=true",
        json={"server_id": sid, "owner_id": oid, "name": "audit_err_db"},
    )
    assert r.status_code == 502
    # La operación fallida dejó una entrada de auditoría con status=error.
    from app.core.database import Database
    from app.models.audit_log import AuditLog

    s = Database().get_declarative_base_session()
    try:
        rows = (
            s.query(AuditLog)
            .filter(
                AuditLog.action == "managed_database.create",
                AuditLog.status == "error",
            )
            .all()
        )
        assert rows and all(rw.touched_engine for rw in rows)
    finally:
        s.close()
