"""
C4: la siembra nueva (``viewer`` + ``access_admin``), la VENTANA DE ARRANQUE y ``ADMIN_RECOVERY``.

Los tests de instalación NUEVA usan ``fresh_client`` (el ``lifespan`` de producción sin
pre-siembra). Los de instalación existente usan ``client``/``admin_client``, cuya cuenta ``admin``
es la combinada heredada con la ventana ya cerrada (``tests/bootstrap_helpers.py``).
"""

from datetime import timedelta

from sqlalchemy import text

from app.core import auth as auth_mod
from app.core.auth import bootstrap_admin
from app.core.database import Database
from app.models.access_bootstrap_model import AccessBootstrapModel, utcnow
from app.models.audit_log import AuditLog
from app.models.user_model import UserModel
from app.services import bootstrap_window
from tests.csrf_helpers import attach_csrf
from tests.scope_helpers import env_id

_PASSWORD = "ContraseñaLarga123"


def _audits(action: str) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        rows = s.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
        s.expunge_all()
        return rows
    finally:
        s.close()


def _sql(sql: str, **params) -> None:
    with Database().engine.begin() as conn:
        conn.execute(text(sql), params)


def _pending_requests() -> int:
    with Database().engine.begin() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM access_change_requests WHERE status = 'pending'")
        ).scalar()


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    attach_csrf(client)
    return client


def _create(client, username, **extra):
    return client.post(
        "/api/v1/gateway-users", json={"username": username, "full_name": "Persona", **extra}
    )


def _accept(token: str) -> None:
    from fastapi.testclient import TestClient

    from main import app

    r = TestClient(app).post(
        "/api/v1/gateway-users/invite/accept", json={"token": token, "password": _PASSWORD}
    )
    assert r.status_code == 200, r.text


def _globals(username: str) -> list[str]:
    um = UserModel()
    return sorted(um.find_access_context(um.find_by_username(username)["id"])["globals"])


# --------------------------------------------------------------------------- #
# La siembra de una instalación nueva                                         #
# --------------------------------------------------------------------------- #


def test_fresh_seed_is_viewer_plus_access_admin_only(fresh_client):
    fila = UserModel().find_by_username("admin")
    assert fila["gateway_role"] == "viewer"
    assert _globals("admin") == ["access_admin"], "la siembra dio más que access_admin"
    with Database().engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM sod_exceptions")).scalar() == 0


def test_fresh_seed_opens_the_window(fresh_client):
    row = AccessBootstrapModel().get()
    assert bootstrap_window.is_open(row)
    assert row["closes_at"] - row["opened_at"] == timedelta(hours=72)
    assert _audits(bootstrap_window.ACTION_OPENED)


def test_me_exposes_the_window_to_access_admins_only(fresh_client):
    me = _login(fresh_client).get("/api/v1/auth/me").json()["data"]
    assert me["bootstrap_window"]["open"] is True
    assert me["bootstrap_window"]["closes_at"]

    r = _create(fresh_client, "operadora", gateway_role="owner")
    assert r.status_code == 201, r.text
    _accept(r.json()["data"]["invite_token"])
    from fastapi.testclient import TestClient

    from main import app

    otra = _login(TestClient(app), "operadora", _PASSWORD)
    assert otra.get("/api/v1/auth/me").json()["data"]["bootstrap_window"] is None


def test_me_shows_a_closed_window_on_an_existing_install(admin_client):
    me = admin_client.get("/api/v1/auth/me").json()["data"]
    assert me["bootstrap_window"]["open"] is False


# --------------------------------------------------------------------------- #
# El primer arranque: el admin solo crea SO, owner y el segundo access_admin   #
# --------------------------------------------------------------------------- #


def test_lone_admin_creates_security_officer_and_owner_immediately(fresh_client):
    c = _login(fresh_client)
    so = _create(c, "oficial", global_capabilities=["security_officer"])
    assert so.status_code == 201, so.text
    owner = _create(c, "operadora", gateway_role="owner")
    assert owner.status_code == 201, owner.text

    assert _globals("oficial") == ["security_officer"]
    assert UserModel().find_by_username("operadora")["gateway_role"] == "owner"
    assert _pending_requests() == 0
    filas = _audits(bootstrap_window.ACTION_ASSIGNMENT)
    assert len(filas) == 2
    assert all('"origin": "create"' in (f.detail or "") for f in filas)
    assert not _audits("access.elevation_unapproved")


def test_lone_admin_set_access_and_sensitive_grant_apply_in_the_window(fresh_client):
    c = _login(fresh_client)
    r = _create(c, "persona")
    assert r.status_code == 201, r.text
    uid = r.json()["data"]["id"]

    r = c.put(f"/api/v1/gateway-users/{uid}/access",
              json={"global_capabilities": ["security_officer"], "scope_grants": []})
    assert r.status_code == 200, r.text
    assert _globals("persona") == ["security_officer"]

    r = _create(c, "otra")
    oid = r.json()["data"]["id"]
    r = c.post(f"/api/v1/gateway-users/{oid}/capability-grants",
               json={"capability": "databases.drop", "scope_type": "environment",
                     "scope_id": env_id("development")})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["status"] == "active"
    assert len(_audits(bootstrap_window.ACTION_ASSIGNMENT)) == 2


def test_expected_first_run_closes_the_window_with_the_second_admin(fresh_client):
    c = _login(fresh_client)
    assert _create(c, "oficial", global_capabilities=["security_officer"]).status_code == 201
    assert _create(c, "operadora", gateway_role="owner").status_code == 201
    aa2 = _create(c, "segunda", global_capabilities=["access_admin"])
    assert aa2.status_code == 201, aa2.text

    # Con la invitación pendiente la segunda no puede aprobar nada: la ventana sigue abierta.
    assert bootstrap_window.is_open(bootstrap_window.refresh())

    _accept(aa2.json()["data"]["invite_token"])
    row = AccessBootstrapModel().get()
    assert row["closed_reason"] == "second_admin"
    assert row["closed_at"] is not None
    assert _audits(bootstrap_window.ACTION_CLOSED)

    # Cerrada: la siguiente elevación queda pendiente de la segunda.
    r = _create(c, "tarde", gateway_role="owner")
    assert r.status_code == 202, r.text
    assert r.json()["data"]["code"] == "access.elevation_pending"
    assert c.get("/api/v1/auth/me").json()["data"]["bootstrap_window"]["open"] is False


def test_the_window_closes_on_the_deadline(fresh_client, monkeypatch):
    c = _login(fresh_client)
    tarde = utcnow() + timedelta(hours=73)
    monkeypatch.setattr(bootstrap_window, "_now", lambda: tarde)

    r = _create(c, "oficial", global_capabilities=["security_officer"])
    assert r.status_code == 202, r.text
    row = AccessBootstrapModel().get()
    assert row["closed_reason"] == "deadline"
    assert not _audits(bootstrap_window.ACTION_ASSIGNMENT)


def test_a_closed_window_never_reopens_by_itself(fresh_client, monkeypatch):
    tarde = utcnow() + timedelta(hours=73)
    monkeypatch.setattr(bootstrap_window, "_now", lambda: tarde)
    bootstrap_window.refresh()
    monkeypatch.undo()

    bootstrap_window.startup()
    bootstrap_admin()
    assert AccessBootstrapModel().get()["closed_reason"] == "deadline"


def test_the_close_is_a_conditional_update(fresh_client):
    row = AccessBootstrapModel().get()
    m = AccessBootstrapModel()
    assert m.close(opened_at=row["opened_at"], closed_at=utcnow(), reason="deadline") is True
    assert m.close(opened_at=row["opened_at"], closed_at=utcnow(), reason="second_admin") is False
    assert AccessBootstrapModel().get()["closed_reason"] == "deadline"


def test_without_the_table_elevations_go_pending(fresh_client):
    c = _login(fresh_client)
    _sql("DROP TABLE access_bootstrap")
    r = _create(c, "oficial", global_capabilities=["security_officer"])
    assert r.status_code == 202, r.text
    assert c.get("/api/v1/auth/me").json()["data"]["bootstrap_window"] is None


def test_existing_install_elevations_stay_pending(admin_client):
    r = _create(admin_client, "operadora", gateway_role="owner")
    assert r.status_code == 202, r.text
    assert not _audits(bootstrap_window.ACTION_ASSIGNMENT)


def test_four_eyes_off_still_applies_alone_and_audits_unapproved(admin_client, monkeypatch):
    from app.controllers import access_request_controller

    monkeypatch.setattr(access_request_controller, "ACCESS_FOUR_EYES", False)
    r = _create(admin_client, "operadora", gateway_role="owner")
    assert r.status_code == 201, r.text
    assert _audits("access.elevation_unapproved")
    assert not _audits(bootstrap_window.ACTION_ASSIGNMENT)


# --------------------------------------------------------------------------- #
# ADMIN_RECOVERY                                                               #
# --------------------------------------------------------------------------- #


def _break_admin() -> None:
    """Cero access_admin: la cuenta queda desactivada, sin globales y con rol viewer."""
    _sql("UPDATE users SET is_active = 0, gateway_role = 'viewer' WHERE username = 'admin'")
    _sql("DELETE FROM user_global_capabilities WHERE user_id = "
         "(SELECT id FROM users WHERE username = 'admin')")
    _sql("DELETE FROM sod_exceptions")


def test_admin_recovery_restores_only_access_admin_and_reopens(client, monkeypatch):
    _break_admin()
    monkeypatch.setattr(auth_mod, "ADMIN_RECOVERY", True)

    bootstrap_admin()
    bootstrap_window.startup()

    fila = UserModel().find_by_username("admin")
    assert fila["is_active"]
    assert fila["gateway_role"] == "viewer", "la recuperación re-elevó el rol"
    assert _globals("admin") == ["access_admin"], "la recuperación dio más que access_admin"
    row = AccessBootstrapModel().get()
    assert bootstrap_window.is_open(row)
    assert row["closed_reason"] is None
    filas = _audits("access.admin_recovery")
    assert filas and "NO se modificó" in (filas[-1].detail or "")

    # Y la ventana reabierta sirve: el admin recuperado eleva solo.
    c = _login(client)
    assert _create(c, "oficial", global_capabilities=["security_officer"]).status_code == 201


def test_without_admin_recovery_nothing_is_revived_or_reopened(client):
    _break_admin()
    bootstrap_admin()
    bootstrap_window.startup()

    fila = UserModel().find_by_username("admin")
    assert not fila["is_active"]
    assert _globals("admin") == []
    assert AccessBootstrapModel().get()["closed_reason"] == "deadline"
    assert not _audits("access.admin_recovery")


def test_admin_recovery_with_two_admins_recloses_the_window(admin_client, aa_client, monkeypatch):
    monkeypatch.setattr(auth_mod, "ADMIN_RECOVERY", True)
    bootstrap_admin()
    bootstrap_window.startup()
    assert AccessBootstrapModel().get()["closed_reason"] == "second_admin"


def test_admin_recovery_recreates_a_missing_account_as_viewer_plus_aa(client, monkeypatch):
    _sql("DELETE FROM sod_exceptions")
    _sql("DELETE FROM user_global_capabilities")
    _sql("DELETE FROM users")
    monkeypatch.setattr(auth_mod, "ADMIN_RECOVERY", True)

    bootstrap_admin()

    assert UserModel().find_by_username("admin")["gateway_role"] == "viewer"
    assert _globals("admin") == ["access_admin"]
