"""
F-25: lectura de la auditoría (``audit.read``) y revocación administrativa de sesiones
(``access.admin``).

La separación de deberes se apoya en que toda escalada de un solo actor queda auditada; eso solo
es un control si alguien que NO la hizo puede leer el rastro. Por eso la lectura es de
``security_officer`` y ``access_admin`` —que hace los cambios de acceso— recibe 403.
"""

import json
from datetime import datetime, timedelta

from app.core import session_store
from app.core.database import Database
from app.models.audit_log import AuditLog
from app.models.user_model import UserModel
from tests.access_request_helpers import client_as, create_user
from tests.step_up_helpers import session_sid

AUDIT = "/api/v1/audit-log"
ME = "/api/v1/auth/me"

#: ``target_type`` exclusivo de las filas que siembra este archivo: la BD de test ya tiene filas
#: reales (altas, aprobaciones, denegaciones) y los filtros se miden contra las sembradas.
_T = "zz_audit_test"


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


def _seed(**campos) -> int:
    base = {
        "action": "access.request_created",
        "status": "success",
        "actor_type": "admin",
        "touched_engine": False,
        "target_type": _T,
    }
    base.update(campos)
    s = Database().get_declarative_base_session()
    try:
        fila = AuditLog(**base)
        s.add(fila)
        s.commit()
        return fila.id
    finally:
        s.close()


def _seed_set() -> dict[str, int]:
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    return {
        "a1": _seed(action="access.request_created", admin_id=7, admin_username="ana",
                    target_id=1, request_id="req-a1", created_at=t0,
                    detail=json.dumps({"username": "x", "before": {}, "after": {}})),
        "a2": _seed(action="access.approved", admin_id=8, admin_username="beto",
                    target_id=2, status="failure", created_at=t0 + timedelta(days=1),
                    detail="texto libre, no JSON"),
        "g1": _seed(action="gateway_user.create", admin_id=7, admin_username="ana",
                    target_id=1, created_at=t0 + timedelta(days=2)),
        # `_` es comodín de LIKE: sin escaparlo, 'gateway_user.*' también traería esta.
        "gx": _seed(action="gatewayXuser.create", admin_id=9, admin_username="ceci",
                    target_id=3, created_at=t0 + timedelta(days=3)),
        "t1": _seed(action="mcp.tool", actor_type="api_token", api_token_id=5,
                    admin_username="token:abc", target_id=4,
                    created_at=t0 + timedelta(days=4)),
    }


def _ids(r) -> list[int]:
    assert r.status_code == 200, r.text
    return [e["id"] for e in r.json()["data"]]


def _get(c, **params):
    return c.get(AUDIT, params={"target_type": _T, **params})


# --------------------------------------------------------------------------- #
# Lectura de auditoría                                                         #
# --------------------------------------------------------------------------- #


def test_security_officer_reads_the_audit_newest_first(so_client):
    ids = _seed_set()
    r = _get(so_client)
    assert _ids(r) == sorted(ids.values(), reverse=True)
    assert r.json()["pagination"]["total"] == 5


def test_audit_filters(so_client):
    ids = _seed_set()
    assert set(_ids(_get(so_client, action="access.*"))) == {ids["a1"], ids["a2"]}
    assert _ids(_get(so_client, action="access.approved")) == [ids["a2"]]
    assert _ids(_get(so_client, action="gateway_user.*")) == [ids["g1"]]
    assert set(_ids(_get(so_client, admin_id=7))) == {ids["a1"], ids["g1"]}
    assert _ids(_get(so_client, admin_username="beto")) == [ids["a2"]]
    assert _ids(_get(so_client, actor_type="api_token")) == [ids["t1"]]
    assert _ids(_get(so_client, api_token_id=5)) == [ids["t1"]]
    assert _ids(_get(so_client, target_id=3)) == [ids["gx"]]
    assert _ids(_get(so_client, status="failure")) == [ids["a2"]]
    assert _ids(_get(so_client, request_id="req-a1")) == [ids["a1"]]
    # `from` inclusive, `to` exclusive.
    r = _get(so_client, **{"from": "2026-01-02T12:00:00", "to": "2026-01-04T12:00:00"})
    assert set(_ids(r)) == {ids["a2"], ids["g1"]}
    # Combinados con AND.
    assert _ids(_get(so_client, action="access.*", admin_id=7)) == [ids["a1"]]


def test_audit_empty_range_is_422(so_client):
    r = _get(so_client, **{"from": "2026-01-05T00:00:00", "to": "2026-01-01T00:00:00"})
    assert r.status_code == 422, r.text
    assert _code(r) == "audit.invalid_range"


def test_audit_pagination_is_stable_and_disjoint(so_client):
    ids = _seed_set()
    p1 = _get(so_client, page=1, size=2)
    p2 = _get(so_client, page=2, size=2)
    p3 = _get(so_client, page=3, size=2)
    todos = _ids(p1) + _ids(p2) + _ids(p3)
    assert todos == sorted(ids.values(), reverse=True)
    meta = p1.json()["pagination"]
    assert (meta["total"], meta["pages"], meta["has_next"]) == (5, 3, True)


def test_audit_detail_is_parsed_when_json(so_client):
    ids = _seed_set()
    a1 = so_client.get(f"{AUDIT}/{ids['a1']}").json()["data"]
    assert a1["detail_json"] == {"username": "x", "before": {}, "after": {}}
    a2 = so_client.get(f"{AUDIT}/{ids['a2']}").json()["data"]
    assert a2["detail"] == "texto libre, no JSON"
    assert a2["detail_json"] is None
    assert "updated_at" not in a2


def test_audit_entry_not_found(so_client):
    r = so_client.get(f"{AUDIT}/999999")
    assert r.status_code == 404, r.text
    assert _code(r) == "audit.not_found"


def test_access_admin_alone_cannot_read_the_audit(aa_client):
    """El revisado no se revisa a sí mismo: ``access_admin`` no tiene ``audit.read``."""
    for path in (AUDIT, f"{AUDIT}/1"):
        r = aa_client.get(path)
        assert r.status_code == 403, r.text
        assert _code(r) == "access.forbidden"


def test_viewer_cannot_read_the_audit(admin_client):
    datos = create_user(admin_client, "lector-aud")
    viewer = client_as(datos, "lector-aud")
    r = viewer.get(AUDIT)
    assert r.status_code == 403, r.text
    assert _code(r) == "access.forbidden"


def test_reading_the_audit_does_not_prompt_step_up(so_client, expire_step_up):
    """``audit.read`` no divulga: un GET con la ventana vencida no pide contraseña."""
    expire_step_up(so_client)
    assert so_client.get(AUDIT).status_code == 200


# --------------------------------------------------------------------------- #
# Revocación administrativa de sesiones                                        #
# --------------------------------------------------------------------------- #


def _target(admin_client, username="objetivo"):
    datos = create_user(admin_client, username)
    return datos["id"], client_as(datos, username)


def test_access_admin_revokes_another_users_sessions(admin_client, aa_client):
    tid, target = _target(admin_client)
    assert target.get(ME).status_code == 200

    r = aa_client.post(f"/api/v1/gateway-users/{tid}/sessions/revoke")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"revoked": 1}

    r = target.get(ME)
    assert r.status_code == 401, r.text
    assert _code(r) == "auth.session_access_admin_revoked"

    s = Database().get_declarative_base_session()
    try:
        fila = (
            s.query(AuditLog)
            .filter(AuditLog.action == "gateway_user.sessions_revoked", AuditLog.target_id == tid)
            .one()
        )
        assert json.loads(fila.detail)["revoked"] == 1
        assert fila.admin_username == "aa-segundo"
    finally:
        s.close()


def test_revoking_leaves_expired_rows_with_their_own_reason(admin_client, aa_client):
    """Una fila vencida y sin tachar no se cuenta ni se re-etiqueta."""
    from sqlalchemy import update

    from app.core.environments import SESSION_IDLE_MINUTES
    from app.models.gateway_session import GatewaySession

    tid, target = _target(admin_client, "objetivo-viejo")
    sid = session_sid(target)
    s = session_store._session()
    try:
        s.execute(
            update(GatewaySession)
            .where(GatewaySession.sid == sid)
            .values(
                last_seen_at=session_store._utcnow()
                - timedelta(minutes=SESSION_IDLE_MINUTES + 1)
            )
        )
        s.commit()
    finally:
        s.close()

    r = aa_client.post(f"/api/v1/gateway-users/{tid}/sessions/revoke")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"revoked": 0}
    assert _code(target.get(ME)) == "auth.session_idle"


def test_self_revoke_through_the_admin_route_is_409(admin_client):
    yo = UserModel().find_by_username("admin")["id"]
    r = admin_client.post(f"/api/v1/gateway-users/{yo}/sessions/revoke")
    assert r.status_code == 409, r.text
    assert _code(r) == "access.self_modification_forbidden"
    assert admin_client.get(ME).status_code == 200


def test_revoke_unknown_user_is_404(aa_client):
    r = aa_client.post("/api/v1/gateway-users/999999/sessions/revoke")
    assert r.status_code == 404, r.text
    assert _code(r) == "gateway_user.not_found"


def test_listing_sessions_never_exposes_the_sid(admin_client, aa_client):
    tid, target = _target(admin_client, "objetivo-lista")
    sid = session_sid(target)

    r = aa_client.get(f"/api/v1/gateway-users/{tid}/sessions")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 1
    assert set(data[0]) == {"created_at", "last_seen_at", "expires_at", "ip"}
    assert sid not in r.text
    assert sid[:8] not in r.text


def test_revoke_prompts_step_up_with_a_stale_window(admin_client, aa_client, expire_step_up):
    tid, target = _target(admin_client, "objetivo-stepup")
    expire_step_up(aa_client)
    r = aa_client.post(f"/api/v1/gateway-users/{tid}/sessions/revoke")
    assert r.status_code == 403, r.text
    assert _code(r) == "access.step_up_required"
    # Antes de cualquier efecto: la sesión del objetivo sigue viva.
    assert target.get(ME).status_code == 200


def test_security_officer_alone_cannot_manage_sessions(admin_client, so_client):
    tid, _ = _target(admin_client, "objetivo-so")
    for method, path in (
        ("GET", f"/api/v1/gateway-users/{tid}/sessions"),
        ("POST", f"/api/v1/gateway-users/{tid}/sessions/revoke"),
    ):
        r = so_client.request(method, path)
        assert r.status_code == 403, r.text
        assert _code(r) == "access.forbidden"
