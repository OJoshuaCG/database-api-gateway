"""
Guards y auditoría DCL de grants (Plan 07 Fase 1 — cierre).

Cubren, sobre SQLite (sin motor real), las protecciones que ocurren ANTES de tocar el
motor y el cableado de auditoría. El adapter se mockea cuando hace falta llegar al
punto en que ya se ejecutaría el DCL.
"""

from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.services.db_admin.mysql_adapter import MySQLAdapter


def _make_server(admin_client, **ov) -> int:
    payload = {
        "name": "srv",
        "host": "127.0.0.1",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    r = admin_client.post("/api/v1/servers", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _make_user(admin_client, server_id: int, username: str) -> int:
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _audit_rows(action: str | None = None, status: str | None = None) -> list[AuditLog]:
    session = Database().get_declarative_base_session()
    try:
        q = session.query(AuditLog)
        if action:
            q = q.filter(AuditLog.action == action)
        if status:
            q = q.filter(AuditLog.status == status)
        return q.all()
    finally:
        session.close()


def _revoke(admin_client, uid: int, body: dict, **params):
    # httpx 0.28 ignora json= en .delete(); usar request().
    return admin_client.request(
        "DELETE",
        f"/api/v1/server-users/{uid}/grants",
        json=body,
        params=params or None,
    )


_BODY = {"level": "table", "object_ref": {"database": "shop", "table": "orders"}, "privileges": ["SELECT"]}


# --------------------------- Anti auto-lockout -------------------------------- #
def test_revoke_self_credential_blocked_409(admin_client):
    sid = _make_server(admin_client, root_username="root")
    uid = _make_user(admin_client, sid, "root")  # mismo nombre que la credencial del gateway
    r = _revoke(admin_client, uid, _BODY)
    assert r.status_code == 409, r.text
    # Se aborta ANTES de auditar intención: no debe quedar fila 'attempt'.
    assert _audit_rows(action="server_user.revoke_object", status="attempt") == []


def test_revoke_self_credential_case_insensitive_409(admin_client):
    sid = _make_server(admin_client, root_username="Root")
    uid = _make_user(admin_client, sid, "root")
    r = _revoke(admin_client, uid, _BODY)
    assert r.status_code == 409, r.text


# ----------------------------- CASCADE confirm -------------------------------- #
def test_cascade_requires_confirmation_pg_422(admin_client):
    sid = _make_server(admin_client, engine="postgresql", port=5432)
    uid = _make_user(admin_client, sid, "alice")
    body = {**_BODY, "cascade": True}
    r = _revoke(admin_client, uid, body)  # sin confirm_grantee
    assert r.status_code == 422, r.text
    assert "confirm_grantee" in r.text


def test_cascade_not_supported_on_mysql_422(admin_client):
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "bob")
    body = {**_BODY, "cascade": True}
    r = _revoke(admin_client, uid, body, confirm_grantee="bob")
    assert r.status_code == 422, r.text
    assert "CASCADE" in r.text


# --------------------- Auditoría de intención + resultado --------------------- #
def test_revoke_records_intent_and_success_with_granular_fields(admin_client, monkeypatch):
    sid = _make_server(admin_client, engine="mysql", root_username="root")
    uid = _make_user(admin_client, sid, "carol")
    monkeypatch.setattr(MySQLAdapter, "revoke_object", lambda self, *a, **k: None)

    body = {
        "level": "table",
        "object_ref": {"database": "shop", "table": "orders"},
        "privileges": ["SELECT", "UPDATE"],
    }
    r = _revoke(admin_client, uid, body)
    assert r.status_code == 200, r.text

    assert len(_audit_rows(action="server_user.revoke_object", status="attempt")) == 1
    successes = _audit_rows(action="server_user.revoke_object", status="success")
    assert len(successes) == 1
    row = successes[0]
    assert row.grantee == "carol@%"
    assert row.privilege == "SELECT,UPDATE"
    assert row.object_level == "table"
    assert row.object_name == "shop.orders"
    assert row.grantor == "root"
    assert row.touched_engine is True


def test_revoke_engine_failure_records_error(admin_client, monkeypatch):
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "dave")

    def boom(self, *a, **k):
        # Simula el error del motor mapeado por map_driver_error (AppHttpException).
        raise AppHttpException(message="connection refused", status_code=502)

    monkeypatch.setattr(MySQLAdapter, "revoke_object", boom)
    r = _revoke(admin_client, uid, _BODY)
    assert r.status_code == 502, r.text
    # La intención (fail-closed) quedó registrada antes de fallar, y el error después.
    assert len(_audit_rows(action="server_user.revoke_object", status="attempt")) == 1
    assert len(_audit_rows(action="server_user.revoke_object", status="error")) == 1


def test_grant_with_grant_option_records_intent(admin_client, monkeypatch):
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "erin")
    monkeypatch.setattr(MySQLAdapter, "can_grant", lambda self, *a, **k: True)
    monkeypatch.setattr(MySQLAdapter, "grant_object", lambda self, *a, **k: None)

    body = {**_BODY, "with_grant_option": True}
    r = admin_client.post(f"/api/v1/server-users/{uid}/grants", json=body)
    assert r.status_code == 200, r.text
    assert len(_audit_rows(action="server_user.grant_object", status="attempt")) == 1
    success = _audit_rows(action="server_user.grant_object", status="success")
    assert len(success) == 1
    assert success[0].with_grant_option is True


def test_grant_plain_does_not_record_intent(admin_client, monkeypatch):
    # GRANT no-GATE (SELECT sin grant option): NO debe auditar intención.
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "frank")
    monkeypatch.setattr(MySQLAdapter, "can_grant", lambda self, *a, **k: True)
    monkeypatch.setattr(MySQLAdapter, "grant_object", lambda self, *a, **k: None)

    r = admin_client.post(f"/api/v1/server-users/{uid}/grants", json=_BODY)
    assert r.status_code == 200, r.text
    assert _audit_rows(action="server_user.grant_object", status="attempt") == []
    assert len(_audit_rows(action="server_user.grant_object", status="success")) == 1


# ------------------------------- apply-profile -------------------------------- #
# Guards de POST /server-users/{id}/apply-profile/{profile_id}. Antes este endpoint
# devolvía 200 aunque no hubiera aplicado NINGÚN grant, y aceptaba perfiles
# desactivados; además rechazaba con 422 todo cruce mysql↔mariadb, incluso el válido.


def _make_profile(admin_client, name="perfil", engine="mysql", items=None, **ov) -> int:
    payload = {
        "name": name,
        "engine": engine,
        "description": None,
        "items": items or [{"level": "table", "privileges": ["SELECT"]}],
    }
    payload.update(ov)
    r = admin_client.post("/api/v1/permission-profiles", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _apply(admin_client, uid: int, pid: int, mappings=None):
    body = {
        "object_mappings": mappings
        if mappings is not None
        else [{"level": "table", "object_ref": {"database": "shop", "table": "orders"}}]
    }
    return admin_client.post(f"/api/v1/server-users/{uid}/apply-profile/{pid}", json=body)


def test_apply_profile_ok(admin_client, monkeypatch):
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "grace")
    pid = _make_profile(admin_client, name="ap-ok")
    monkeypatch.setattr(MySQLAdapter, "can_grant", lambda self, *a, **k: True)
    monkeypatch.setattr(MySQLAdapter, "grant_object", lambda self, *a, **k: None)

    r = _apply(admin_client, uid, pid)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["grants_applied"] == 1


def test_apply_inactive_profile_409(admin_client, monkeypatch):
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "heidi")
    pid = _make_profile(admin_client, name="ap-off", is_active=False)
    monkeypatch.setattr(MySQLAdapter, "can_grant", lambda self, *a, **k: True)
    monkeypatch.setattr(MySQLAdapter, "grant_object", lambda self, *a, **k: None)

    r = _apply(admin_client, uid, pid)
    assert r.status_code == 409, r.text
    # Se corta antes de tocar el motor: no debe haber auditoría de aplicación.
    assert _audit_rows(action="server_user.apply_profile") == []


def test_apply_family_profile_mariadb_on_mysql_ok(admin_client, monkeypatch):
    """Perfil mariadb con privilegios comunes: aplicable sobre un servidor mysql."""
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "ivan")
    pid = _make_profile(admin_client, name="ap-maria", engine="mariadb")
    monkeypatch.setattr(MySQLAdapter, "can_grant", lambda self, *a, **k: True)
    monkeypatch.setattr(MySQLAdapter, "grant_object", lambda self, *a, **k: None)

    r = _apply(admin_client, uid, pid)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["grants_applied"] == 1


def test_apply_family_profile_with_exclusive_privilege_422(admin_client):
    """DELETE HISTORY no existe en MySQL: el cruce debe rechazarse nombrándolo."""
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "judy")
    pid = _make_profile(
        admin_client,
        name="ap-hist",
        engine="mariadb",
        items=[{"level": "table", "privileges": ["DELETE HISTORY"]}],
    )
    r = _apply(admin_client, uid, pid)
    assert r.status_code == 422, r.text
    assert "DELETE HISTORY" in r.text


def test_apply_cross_family_profile_422(admin_client):
    """PostgreSQL y MySQL no son familia: sigue siendo un rechazo duro."""
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "karl")
    pid = _make_profile(
        admin_client,
        name="ap-pg",
        engine="postgresql",
        items=[{"level": "table", "privileges": ["SELECT"]}],
    )
    r = _apply(admin_client, uid, pid)
    assert r.status_code == 422, r.text


def test_apply_profile_without_mappings_422(admin_client):
    """Todos los niveles omitidos => no se aplicó nada => 422, no un 200 silencioso."""
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "laura")
    pid = _make_profile(admin_client, name="ap-nomap")

    r = _apply(admin_client, uid, pid, mappings=[])
    assert r.status_code == 422, r.text
    assert "table" in r.text  # el nivel omitido se nombra en el mensaje
    # El intento igual queda auditado (se llegó a evaluar el perfil).
    assert len(_audit_rows(action="server_user.apply_profile")) == 1


def test_apply_profile_all_grants_fail_422_without_engine_detail(admin_client, monkeypatch):
    """Si todos los GRANT fallan: 422, y el detalle crudo del motor NO viaja en la respuesta."""
    sid = _make_server(admin_client, engine="mysql")
    uid = _make_user(admin_client, sid, "mike")
    pid = _make_profile(admin_client, name="ap-fail")
    monkeypatch.setattr(MySQLAdapter, "can_grant", lambda self, *a, **k: True)

    def boom(self, *a, **k):
        raise RuntimeError("ERROR 1045 (28000): Access denied for user 'gw'@'10.0.0.5'")

    monkeypatch.setattr(MySQLAdapter, "grant_object", boom)
    r = _apply(admin_client, uid, pid)
    assert r.status_code == 422, r.text
    assert "10.0.0.5" not in r.text
    assert "Access denied" not in r.text
