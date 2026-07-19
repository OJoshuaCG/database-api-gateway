"""
Manejo de usuarios del motor por IDENTIDAD (server_id, username, host):
- vista agrupada por username (adopted / unmanaged / orphan; supports_hosts por motor),
- CRUD sobre adoptados y NO adoptados,
- revelar contraseña (solo si el gateway la conoce),
- agregar host (clonar cuenta, misma o nueva contraseña, copiar grants) — MySQL/MariaDB.

Adapter mockeado (sin motor real), mismo patrón que test_api_server_users.py.
"""

import app.controllers.server_user_controller as suc
from app.services.db_admin.dtos import EngineUserInfo
from app.services.db_admin.mysql_adapter import MySQLAdapter


# --------- lógica pura: reescritura de líneas SHOW GRANTS (copy_grants) ---- #
def test_rewrite_grant_line():
    r = MySQLAdapter._rewrite_grant_line
    ng = "'app'@'10.0.0.9'"
    # USAGE base, PROXY y credencial embebida → se omiten (None).
    assert r("GRANT USAGE ON *.* TO `app`@`%`", ng) is None
    assert r("GRANT PROXY ON ''@'' TO `app`@`%`", ng) is None
    assert r("GRANT ALL ON `s`.* TO `app`@`%` IDENTIFIED BY PASSWORD '*ABC'", ng) is None
    # Grant real: solo se reescribe el grantee (soporta backtick y comilla simple).
    assert (
        r("GRANT SELECT, INSERT ON `shop`.* TO `app`@`%`", ng)
        == "GRANT SELECT, INSERT ON `shop`.* TO 'app'@'10.0.0.9'"
    )
    assert (
        r("GRANT SELECT ON `s`.* TO 'app'@'%'", ng)
        == "GRANT SELECT ON `s`.* TO 'app'@'10.0.0.9'"
    )
    # WITH GRANT OPTION se preserva.
    assert (
        r("GRANT SELECT ON `s`.`t` TO `app`@`%` WITH GRANT OPTION", ng)
        == "GRANT SELECT ON `s`.`t` TO 'app'@'10.0.0.9' WITH GRANT OPTION"
    )


def _make_server(admin_client, **ov) -> int:
    payload = {
        "name": "srv-eu",
        "host": "10.0.0.2",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    r = admin_client.post("/api/v1/servers", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


class _FakeAdapter:
    """Adapter configurable: usuarios en vivo + registro de llamadas de escritura."""

    def __init__(self, live=(), *, supports_hosts=True, grants=0):
        self.dialect = "mysql"
        self.supports_hosts = supports_hosts
        self._live = list(live)
        self._grants = grants
        self.calls = []

    def list_users(self):
        return [EngineUserInfo(username=u, host=h) for (u, h) in self._live]

    def create_user(self, username, password, host):
        self.calls.append(("create_user", username, host))

    def change_password(self, username, new_password, host):
        self.calls.append(("change_password", username, host))

    def drop_user(self, username, host):
        self.calls.append(("drop_user", username, host))

    def add_user_host(self, username, source_host, new_host, *, new_password=None):
        self.calls.append(("add_user_host", username, source_host, new_host, new_password))

    def copy_user_grants(self, username, source_host, new_host):
        self.calls.append(("copy_user_grants", username, source_host, new_host))
        return self._grants


def _patch(monkeypatch, adapter):
    monkeypatch.setattr(suc, "get_adapter", lambda target: adapter)


# --------------------------- vista agrupada ------------------------------- #
def test_grouped_dedups_username_and_marks_status(admin_client, monkeypatch):
    sid = _make_server(admin_client)
    # 'alice' con 3 hosts en el motor; 'bob' con 1.
    live = [("alice", "localhost"), ("alice", "%"), ("alice", "10.0.0.5"), ("bob", "%")]
    _patch(monkeypatch, _FakeAdapter(live))
    # Adoptar solo alice@localhost → esa identidad debe salir 'adopted', el resto 'unmanaged'.
    assert admin_client.post(
        "/api/v1/server-users/adopt",
        json={"server_id": sid, "username": "alice", "host": "localhost"},
    ).status_code == 201

    r = admin_client.get(f"/api/v1/servers/{sid}/users/grouped")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["supports_hosts"] is True
    by_user = {u["username"]: u for u in data["users"]}
    assert by_user["alice"]["identity_count"] == 3
    assert by_user["bob"]["identity_count"] == 1
    statuses = {i["host"]: i["status"] for i in by_user["alice"]["identities"]}
    assert statuses["localhost"] == "adopted"
    assert statuses["%"] == "unmanaged"
    assert statuses["10.0.0.5"] == "unmanaged"


def test_grouped_marks_orphan(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-orphan", port=3307)
    _patch(monkeypatch, _FakeAdapter(live=[("ghost", "%")]))
    # Adoptar ghost@% (existe en vivo) y luego "desaparece" del motor.
    admin_client.post(
        "/api/v1/server-users/adopt", json={"server_id": sid, "username": "ghost"}
    )
    _patch(monkeypatch, _FakeAdapter(live=[]))  # motor ya no lo tiene
    data = admin_client.get(f"/api/v1/servers/{sid}/users/grouped").json()["data"]
    ident = data["users"][0]["identities"][0]
    assert ident["status"] == "orphan"


def test_grouped_postgres_no_hosts(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-pg", port=5432, engine="postgresql")
    _patch(monkeypatch, _FakeAdapter(live=[("app", None)], supports_hosts=False))
    data = admin_client.get(f"/api/v1/servers/{sid}/users/grouped").json()["data"]
    assert data["supports_hosts"] is False
    assert data["users"][0]["identities"][0]["host"] is None


# --------------------------- CRUD por identidad --------------------------- #
def test_create_by_identity_touches_engine(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-c", port=3308)
    fake = _FakeAdapter()
    _patch(monkeypatch, fake)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users",
        json={"username": "creado", "host": "%", "password": "pw123456"},
    )
    assert r.status_code == 201, r.text
    assert ("create_user", "creado", "%") in fake.calls
    # Sin adopt → no queda en el inventario.
    assert r.json()["data"]["adopted"] is False
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert all(u["username"] != "creado" for u in inv)


def test_create_by_identity_with_adopt_persists(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-ca", port=3309)
    _patch(monkeypatch, _FakeAdapter())
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users",
        json={"username": "adop", "password": "pw123456", "adopt": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["adopted"] is True
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert any(u["username"] == "adop" and u["has_password"] for u in inv)


def test_change_password_by_identity_syncs_inventory(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-p", port=3310)
    fake = _FakeAdapter(live=[("rot", "%")])
    _patch(monkeypatch, fake)
    admin_client.post(
        "/api/v1/server-users/adopt", json={"server_id": sid, "username": "rot"}
    )
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password",
        json={"username": "rot", "new_password": "brandnew1"},
    )
    assert r.status_code == 200, r.text
    assert ("change_password", "rot", "%") in fake.calls
    # Adoptado sin contraseña → tras rotar, el gateway ya la conoce (has_password=true).
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert any(u["username"] == "rot" and u["has_password"] for u in inv)


def test_drop_by_identity_requires_confirmation(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-d", port=3311)
    fake = _FakeAdapter()
    _patch(monkeypatch, fake)
    # Sin confirm → 422, no toca el motor.
    assert admin_client.request(
        "DELETE", f"/api/v1/servers/{sid}/users", params={"username": "x", "host": "%"}
    ).status_code == 422
    assert fake.calls == []
    # Con confirm correcto → DROP.
    r = admin_client.request(
        "DELETE",
        f"/api/v1/servers/{sid}/users",
        params={"username": "x", "host": "%", "confirm_username": "x"},
    )
    assert r.status_code == 200, r.text
    assert ("drop_user", "x", "%") in fake.calls


# --------------------------- revelar contraseña --------------------------- #
def test_reveal_password_roundtrip(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-r", port=3312)
    _patch(monkeypatch, _FakeAdapter())
    # Crear por identidad + adopt con contraseña conocida por el gateway.
    admin_client.post(
        f"/api/v1/servers/{sid}/users",
        json={"username": "vis", "password": "topsecret9", "adopt": True},
    )
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/reveal-password", json={"username": "vis"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["password"] == "topsecret9"


def test_reveal_password_409_when_gateway_unknown(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-r2", port=3313)
    _patch(monkeypatch, _FakeAdapter(live=[("adopted_nopw", "%")]))
    admin_client.post(
        "/api/v1/server-users/adopt", json={"server_id": sid, "username": "adopted_nopw"}
    )
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/reveal-password",
        json={"username": "adopted_nopw"},
    )
    assert r.status_code == 409, r.text


def test_reveal_password_404_when_not_in_inventory(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-r3", port=3314)
    _patch(monkeypatch, _FakeAdapter())
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/reveal-password", json={"username": "nobody"}
    )
    assert r.status_code == 404, r.text


# --------------------------- agregar host --------------------------------- #
def test_add_host_reuse_password(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-h", port=3315)
    fake = _FakeAdapter(grants=3)
    _patch(monkeypatch, fake)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/add-host",
        json={"username": "multi", "new_host": "10.0.0.9", "copy_grants": True},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["password_mode"] == "reused"
    assert data["grants_copied"] == 3
    # reuse_password=True → new_password None (se copia el hash).
    assert ("add_user_host", "multi", "%", "10.0.0.9", None) in fake.calls
    assert ("copy_user_grants", "multi", "%", "10.0.0.9") in fake.calls


def test_add_host_new_password_requires_it(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-h2", port=3316)
    _patch(monkeypatch, _FakeAdapter())
    # reuse_password=false sin new_password → 422 (validación de schema).
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/add-host",
        json={"username": "u", "new_host": "h2", "reuse_password": False},
    )
    assert r.status_code == 422


def test_add_host_new_password_passes_through(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-h3", port=3317)
    fake = _FakeAdapter()
    _patch(monkeypatch, fake)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/add-host",
        json={
            "username": "u",
            "new_host": "h3",
            "reuse_password": False,
            "new_password": "freshpw12",
            "adopt": True,
        },
    )
    assert r.status_code == 201, r.text
    assert ("add_user_host", "u", "%", "h3", "freshpw12") in fake.calls
    assert r.json()["data"]["adopted"] is True


def test_identity_ops_refuse_gateway_root_credential(admin_client, monkeypatch):
    """B1: no se puede DROP/rotar/crear/agregar-host sobre la propia credencial pseudo-root."""
    sid = _make_server(admin_client, name="eu-root", port=3320, root_username="gwroot")
    fake = _FakeAdapter()
    _patch(monkeypatch, fake)

    # DROP sobre root → 409 y el motor NO se toca.
    assert admin_client.request(
        "DELETE",
        f"/api/v1/servers/{sid}/users",
        params={"username": "gwroot", "host": "%", "confirm_username": "gwroot"},
    ).status_code == 409
    # Cambio de contraseña sobre root → 409.
    assert admin_client.patch(
        f"/api/v1/servers/{sid}/users/password",
        json={"username": "gwroot", "new_password": "whatever1"},
    ).status_code == 409
    # Crear/clonar la cuenta root también se rechaza.
    assert admin_client.post(
        f"/api/v1/servers/{sid}/users",
        json={"username": "gwroot", "password": "whatever1"},
    ).status_code == 409
    assert admin_client.post(
        f"/api/v1/servers/{sid}/users/add-host",
        json={"username": "gwroot", "new_host": "10.0.0.1"},
    ).status_code == 409
    # Ninguna tocó el motor.
    assert fake.calls == []


def test_add_host_422_on_postgres(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-h4", port=5433, engine="postgresql")
    _patch(monkeypatch, _FakeAdapter(supports_hosts=False))
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/add-host",
        json={"username": "u", "new_host": "h"},
    )
    assert r.status_code == 422, r.text
