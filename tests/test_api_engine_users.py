"""
Manejo de usuarios del motor por IDENTIDAD (server_id, username, host):
- vista agrupada por username (adopted / unmanaged / orphan; supports_hosts por motor),
- CRUD sobre adoptados y NO adoptados,
- revelar contraseña (solo si el gateway la conoce),
- agregar host (clonar cuenta, misma o nueva contraseña, copiar grants) — MySQL/MariaDB,
- operaciones MASIVAS: adoptar todos los hosts de un username, definir contraseña
  conocida (sin ALTER USER) y rotar contraseña (con ALTER USER) con alcance individual
  o global (todos los hosts en vivo).

Adapter mockeado (sin motor real), mismo patrón que test_api_server_users.py.
"""

import app.controllers.server_user_controller as suc
from app.exceptions import AppHttpException
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


# --------------------- adopción masiva de hosts ---------------------------- #
def test_adopt_all_hosts_adopts_every_live_host(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-aah1", port=3330)
    live = [("bulk", "%"), ("bulk", "localhost"), ("bulk", "10.0.0.9")]
    _patch(monkeypatch, _FakeAdapter(live))
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "bulk"}
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["total_hosts"] == 3
    assert data["adopted"] == 3
    assert {i["status"] for i in data["results"]} == {"adopted"}
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    bulk_rows = [u for u in inv if u["username"] == "bulk"]
    assert len(bulk_rows) == 3
    assert all(not u["has_password"] for u in bulk_rows)


def test_adopt_all_hosts_with_known_password_sets_has_password_without_engine_call(
    admin_client, monkeypatch
):
    sid = _make_server(admin_client, name="eu-aah2", port=3331)
    fake = _FakeAdapter([("bulkpw", "%"), ("bulkpw", "localhost")])
    _patch(monkeypatch, fake)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts",
        json={"username": "bulkpw", "known_password": "Secr3t!"},
    )
    assert r.status_code == 201, r.text
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    rows = [u for u in inv if u["username"] == "bulkpw"]
    assert len(rows) == 2
    assert all(u["has_password"] for u in rows)
    # Nunca se ejecuta ALTER/CREATE en el motor: "definir" no toca el motor.
    assert fake.calls == []


def test_adopt_all_hosts_partial_already_adopted(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-aah3", port=3332)
    live = [("part", "%"), ("part", "localhost")]
    _patch(monkeypatch, _FakeAdapter(live))
    admin_client.post(
        "/api/v1/server-users/adopt", json={"server_id": sid, "username": "part", "host": "%"}
    )
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "part"}
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    by_host = {i["host"]: i["status"] for i in data["results"]}
    assert by_host["%"] == "already_adopted"
    assert by_host["localhost"] == "adopted"
    assert data["adopted"] == 1


def test_adopt_all_hosts_404_when_username_not_live(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-aah4", port=3333)
    _patch(monkeypatch, _FakeAdapter())
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "ghost"}
    )
    assert r.status_code == 404, r.text


def test_adopt_all_hosts_guards_root(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-aah5", port=3334, root_username="gwroot")
    fake = _FakeAdapter([("gwroot", "%")])
    _patch(monkeypatch, fake)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "gwroot"}
    )
    assert r.status_code == 409, r.text
    assert fake.calls == []


def test_adopt_all_hosts_postgres_single_identity(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-aah6", port=5434, engine="postgresql")
    _patch(monkeypatch, _FakeAdapter([("app", None)], supports_hosts=False))
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "app"}
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["total_hosts"] == 1
    assert data["results"][0]["host"] is None


# --------------------- definir contraseña conocida ------------------------- #
def test_define_password_host_scope_updates_only_that_host(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-dp1", port=3335)
    live = [("alice", "%"), ("alice", "localhost")]
    fake = _FakeAdapter(live)
    _patch(monkeypatch, fake)
    admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "alice"}
    )
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={"username": "alice", "scope": "host", "host": "%", "known_password": "Secr3t!"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["updated"] == 1
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    by_host = {u["host"]: u["has_password"] for u in inv if u["username"] == "alice"}
    assert by_host["%"] is True
    assert by_host["localhost"] is False
    # Nunca toca el motor.
    assert fake.calls == []


def test_define_password_all_hosts_scope_updates_every_adopted_row(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-dp2", port=3336)
    live = [("bob", "%"), ("bob", "localhost")]
    _patch(monkeypatch, _FakeAdapter(live))
    admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "bob"}
    )
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={"username": "bob", "scope": "all_hosts", "known_password": "Secr3t!"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["updated"] == 2
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert all(u["has_password"] for u in inv if u["username"] == "bob")


def test_define_password_all_hosts_adopt_if_missing(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-dp3", port=3337)
    live = [("carol", "%"), ("carol", "localhost")]
    _patch(monkeypatch, _FakeAdapter(live))
    # Solo se adopta un host; el otro queda "unmanaged".
    admin_client.post(
        "/api/v1/server-users/adopt", json={"server_id": sid, "username": "carol", "host": "%"}
    )
    # Sin la flag: el host no adoptado se omite.
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={"username": "carol", "scope": "all_hosts", "known_password": "Secr3t!"},
    )
    by_host = {i["host"]: i["status"] for i in r.json()["data"]["results"]}
    assert by_host["localhost"] == "skipped_not_found"
    # Con la flag: se adopta y se define la contraseña.
    r2 = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={
            "username": "carol",
            "scope": "all_hosts",
            "known_password": "Secr3t!",
            "adopt_if_missing": True,
        },
    )
    by_host2 = {i["host"]: i["status"] for i in r2.json()["data"]["results"]}
    assert by_host2["localhost"] == "adopted"
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert any(
        u["username"] == "carol" and u["host"] == "localhost" and u["has_password"]
        for u in inv
    )


def test_define_password_overwrite_required_to_replace_existing(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-dp4", port=3338)
    _patch(monkeypatch, _FakeAdapter([("dana", "%")]))
    admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts",
        json={"username": "dana", "known_password": "Original1!"},
    )
    # Sin overwrite=true → se rechaza esa identidad, no se sobreescribe.
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={"username": "dana", "scope": "host", "host": "%", "known_password": "Nuevo2!"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["results"][0]["status"] == "conflict_needs_overwrite"
    reveal = admin_client.post(
        f"/api/v1/servers/{sid}/users/reveal-password", json={"username": "dana"}
    )
    assert reveal.json()["data"]["password"] == "Original1!"
    # Con overwrite=true → sí reemplaza.
    r2 = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={
            "username": "dana",
            "scope": "host",
            "host": "%",
            "known_password": "Nuevo2!",
            "overwrite": True,
        },
    )
    assert r2.json()["data"]["results"][0]["status"] == "updated"
    reveal2 = admin_client.post(
        f"/api/v1/servers/{sid}/users/reveal-password", json={"username": "dana"}
    )
    assert reveal2.json()["data"]["password"] == "Nuevo2!"


def test_define_password_guards_root(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-dp5", port=3339, root_username="gwroot")
    fake = _FakeAdapter([("gwroot", "%")])
    _patch(monkeypatch, fake)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users/define-password",
        json={"username": "gwroot", "known_password": "whatever1"},
    )
    assert r.status_code == 409, r.text
    assert fake.calls == []


# --------------------- rotar contraseña en todos los hosts ----------------- #
def test_rotate_password_all_hosts_calls_change_password_per_host(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-rp1", port=3340)
    live = [("rot", "%"), ("rot", "localhost")]
    fake = _FakeAdapter(live)
    _patch(monkeypatch, fake)
    admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts", json={"username": "rot"}
    )
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password-all-hosts",
        json={"username": "rot", "new_password": "brandnew1", "confirm_username": "rot"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["updated"] == 2
    assert ("change_password", "rot", "%") in fake.calls
    assert ("change_password", "rot", "localhost") in fake.calls
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert all(u["has_password"] for u in inv if u["username"] == "rot")


def test_rotate_password_all_hosts_requires_confirm_username(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-rp2", port=3341)
    fake = _FakeAdapter([("rot2", "%")])
    _patch(monkeypatch, fake)
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password-all-hosts",
        json={"username": "rot2", "new_password": "brandnew1", "confirm_username": "otro"},
    )
    assert r.status_code == 422, r.text
    assert fake.calls == []


def test_rotate_password_all_hosts_partial_failure_reports_per_item(admin_client, monkeypatch):
    class _PartialFailAdapter(_FakeAdapter):
        def change_password(self, username, new_password, host):
            if host == "localhost":
                raise AppHttpException(message="motor caído", status_code=502)
            super().change_password(username, new_password, host)

    sid = _make_server(admin_client, name="eu-rp3", port=3342)
    live = [("flaky", "%"), ("flaky", "localhost")]
    fake = _PartialFailAdapter(live)
    _patch(monkeypatch, fake)
    admin_client.post(
        f"/api/v1/servers/{sid}/users/adopt-all-hosts",
        json={"username": "flaky", "known_password": "Original1!"},
    )
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password-all-hosts",
        json={"username": "flaky", "new_password": "brandnew1", "confirm_username": "flaky"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    by_host = {i["host"]: i for i in data["results"]}
    assert by_host["%"]["status"] == "rotated"
    assert by_host["localhost"]["status"] == "error"
    assert data["updated"] == 1
    # El host fallido conserva la contraseña anterior (nunca se sobreescribió).
    reveal = admin_client.post(
        f"/api/v1/servers/{sid}/users/reveal-password",
        json={"username": "flaky", "host": "localhost"},
    )
    assert reveal.json()["data"]["password"] == "Original1!"


def test_rotate_password_all_hosts_404_when_username_not_live(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-rp4", port=3343)
    _patch(monkeypatch, _FakeAdapter())
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password-all-hosts",
        json={"username": "ghost", "new_password": "brandnew1", "confirm_username": "ghost"},
    )
    assert r.status_code == 404, r.text


def test_rotate_password_all_hosts_guards_root(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-rp5", port=3344, root_username="gwroot")
    fake = _FakeAdapter([("gwroot", "%")])
    _patch(monkeypatch, fake)
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password-all-hosts",
        json={"username": "gwroot", "new_password": "brandnew1", "confirm_username": "gwroot"},
    )
    assert r.status_code == 409, r.text
    assert fake.calls == []


def test_rotate_password_all_hosts_adopt_if_missing(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="eu-rp6", port=3345)
    fake = _FakeAdapter([("newbie", "%")])
    _patch(monkeypatch, fake)
    r = admin_client.patch(
        f"/api/v1/servers/{sid}/users/password-all-hosts",
        json={
            "username": "newbie",
            "new_password": "brandnew1",
            "confirm_username": "newbie",
            "adopt_if_missing": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["results"][0]["adopted"] is True
    inv = admin_client.get(f"/api/v1/server-users?server_id={sid}").json()["data"]
    assert any(u["username"] == "newbie" and u["has_password"] for u in inv)
