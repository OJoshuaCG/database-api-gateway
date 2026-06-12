"""
Endpoints de ServerUsers: CRUD de inventario, validación, no-fuga de password,
y flujos de aprovisionamiento (adapter mockeado, sin motor real).
"""

from app.exceptions import AppHttpException


def _make_server(admin_client, **ov) -> int:
    payload = {
        "name": "srv-su",
        "host": "10.0.0.1",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    r = admin_client.post("/api/v1/servers", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_requires_auth(client):
    assert client.get("/api/v1/server-users").status_code == 401


def test_create_inventory_only_no_password_leak(admin_client):
    sid = _make_server(admin_client)
    r = admin_client.post(
        "/api/v1/server-users",
        json={"server_id": sid, "username": "app_user", "password": "secret123"},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["username"] == "app_user"
    assert data["host"] == "%"
    assert data["has_password"] is True
    assert "secret123" not in r.text
    assert "password" not in data
    assert "password_encrypted" not in data


def test_create_without_password(admin_client):
    sid = _make_server(admin_client, name="s2", port=3307)
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "nopw"}
    )
    assert r.status_code == 201
    assert r.json()["data"]["has_password"] is False


def test_create_invalid_username_422(admin_client):
    sid = _make_server(admin_client, name="s3", port=3308)
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "bad-name"}
    )
    assert r.status_code == 422


def test_duplicate_user_conflict(admin_client):
    sid = _make_server(admin_client, name="s4", port=3309)
    assert admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "dup"}
    ).status_code == 201
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "dup"}
    )
    assert r.status_code == 409


def test_list_filter_by_server(admin_client):
    s1 = _make_server(admin_client, name="sa", port=3310)
    s2 = _make_server(admin_client, name="sb", port=3311)
    admin_client.post("/api/v1/server-users", json={"server_id": s1, "username": "u1"})
    admin_client.post("/api/v1/server-users", json={"server_id": s2, "username": "u2"})
    data = admin_client.get(f"/api/v1/server-users?server_id={s1}").json()["data"]
    assert [u["username"] for u in data] == ["u1"]


def test_get_update_delete(admin_client):
    sid = _make_server(admin_client, name="sc", port=3312)
    uid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "life"}
    ).json()["data"]["id"]
    assert admin_client.get(f"/api/v1/server-users/{uid}").status_code == 200
    upd = admin_client.patch(
        f"/api/v1/server-users/{uid}", json={"notes": "n", "is_active": False}
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["is_active"] is False
    assert admin_client.delete(f"/api/v1/server-users/{uid}").status_code == 200
    assert admin_client.get(f"/api/v1/server-users/{uid}").status_code == 404


def test_get_missing_404(admin_client):
    assert admin_client.get("/api/v1/server-users/9999").status_code == 404


def test_create_missing_server_404(admin_client):
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": 4242, "username": "x"}
    )
    assert r.status_code == 404


def test_provision_requires_password_422(admin_client):
    sid = _make_server(admin_client, name="sd", port=3313)
    r = admin_client.post(
        "/api/v1/server-users?provision=true",
        json={"server_id": sid, "username": "prov"},
    )
    assert r.status_code == 422


def test_provision_creates_user_in_engine(admin_client, monkeypatch):
    import app.controllers.server_user_controller as suc

    calls = {}

    class FakeAdapter:
        def create_user(self, username, password, host):
            calls["create_user"] = (username, host)

    monkeypatch.setattr(suc, "get_adapter", lambda target: FakeAdapter())
    sid = _make_server(admin_client, name="se", port=3314)
    r = admin_client.post(
        "/api/v1/server-users?provision=true",
        json={"server_id": sid, "username": "prov2", "password": "pw123456"},
    )
    assert r.status_code == 201, r.text
    assert calls["create_user"] == ("prov2", "%")


def test_provision_failure_rolls_back(admin_client, monkeypatch):
    import app.controllers.server_user_controller as suc

    class FakeAdapter:
        def create_user(self, *a, **k):
            raise AppHttpException("motor inaccesible", 502)

    monkeypatch.setattr(suc, "get_adapter", lambda target: FakeAdapter())
    sid = _make_server(admin_client, name="sf", port=3315)
    r = admin_client.post(
        "/api/v1/server-users?provision=true",
        json={"server_id": sid, "username": "rollback", "password": "pw123456"},
    )
    assert r.status_code == 502
    # Rollback limpio: el usuario NO quedó en el inventario.
    data = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert all(u["username"] != "rollback" for u in data)


def test_delete_blocked_when_owns_database(admin_client, monkeypatch):
    import app.controllers.managed_database_controller as mdc

    monkeypatch.setattr(mdc, "get_adapter", lambda target: object())
    sid = _make_server(admin_client, name="sg", port=3316)
    uid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "ownerx"}
    ).json()["data"]["id"]
    # BD gestionada en inventario, propiedad del usuario (sin aprovisionar).
    admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": uid, "name": "db_owned"},
    )
    r = admin_client.delete(f"/api/v1/server-users/{uid}")
    assert r.status_code == 409


def test_update_provision_changes_password_in_engine(admin_client, monkeypatch):
    import app.controllers.server_user_controller as suc

    calls = {}

    class FakeAdapter:
        def change_password(self, username, new_password, host):
            calls["change_password"] = (username, host)

    monkeypatch.setattr(suc, "get_adapter", lambda target: FakeAdapter())
    sid = _make_server(admin_client, name="sh", port=3317)
    uid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "rot"}
    ).json()["data"]["id"]
    r = admin_client.patch(
        f"/api/v1/server-users/{uid}?provision=true", json={"password": "newpw123"}
    )
    assert r.status_code == 200, r.text
    assert calls["change_password"] == ("rot", "%")
    assert r.json()["data"]["has_password"] is True


def test_delete_drop_remote_calls_drop_user(admin_client, monkeypatch):
    import app.controllers.server_user_controller as suc

    dropped = []

    class FakeAdapter:
        def drop_user(self, username, host):
            dropped.append((username, host))

    monkeypatch.setattr(suc, "get_adapter", lambda target: FakeAdapter())
    sid = _make_server(admin_client, name="si", port=3318)
    uid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "todrop"}
    ).json()["data"]["id"]
    r = admin_client.delete(f"/api/v1/server-users/{uid}?drop_remote=true")
    assert r.status_code == 200
    assert dropped == [("todrop", "%")]
