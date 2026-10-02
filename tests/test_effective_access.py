"""
Vistas de las capacidades puntuales: ``/auth/me.capability_grants`` y el acceso efectivo.

Lo que se mide: la procedencia sale del MISMO resolvedor que la autorización (el conjunto de
capacidades no inertes es exactamente ``Actor.capabilities``), solo ``access_admin`` lee el
acceso de otra persona, y ``/auth/me`` muestra solo las vivas de quien pregunta.
"""

from sqlalchemy import text

from app.core.authz import actor_from_access_context
from app.core.capability_resolution import explain
from app.core.database import Database
from app.models.user_model import UserModel
from tests.scope_helpers import env_id
from tests.test_api_gateway_users import _cliente_como, _code, _crear
from tests.test_capability_grant_crud import _admin_como, _grant, _insert_cg

DEV = "development"
PROD = "production"


def _effective(client, user_id):
    return client.get(f"/api/v1/gateway-users/{user_id}/effective-access")


def _scope_roles(admin_client, user_id, role="viewer", scope_id=None):
    r = admin_client.put(
        f"/api/v1/gateway-users/{user_id}/access",
        json={
            "global_capabilities": [],
            "scope_grants": [{"scope_type": "environment", "scope_id": scope_id, "role": role}],
        },
    )
    assert r.status_code == 200, r.text


def _pairs(data, source):
    return {(e["capability"], e["scope_id"], e["grant_id"], e["implied_by"])
            for e in data["capabilities"] if e["source"] == source}


# --------------------------------------------------------------------------- #
# R8: provenance                                                              #
# --------------------------------------------------------------------------- #


def test_viewer_at_prod_plus_grant_is_attributed_to_the_grant(admin_client):
    uid = _crear(admin_client, "destino")["id"]
    _scope_roles(admin_client, uid, "viewer", env_id(PROD))
    g = _grant(admin_client, uid, "blueprints.apply", scope_id=env_id(PROD)).json()["data"]

    r = _effective(admin_client, uid)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert (d["user_id"], d["username"], d["active"], d["base_role"]) == (uid, "destino", True, "viewer")
    assert d["scope_roles"] == [
        {"scope_type": "environment", "scope_id": env_id(PROD),
         "scope_name": d["scope_roles"][0]["scope_name"], "role": "viewer"}
    ]
    assert d["scope_roles"][0]["scope_name"]
    assert d["catalog_version"]

    apply = [e for e in d["capabilities"] if e["capability"] == "blueprints.apply"]
    assert apply == [{
        "capability": "blueprints.apply", "source": "capability_grant",
        "scope_type": "environment", "scope_id": env_id(PROD),
        "scope_name": apply[0]["scope_name"], "grant_id": g["id"],
        "implied_by": None, "inert": False,
    }]
    assert apply[0]["scope_name"]
    # La lectura implícita sale atribuida a la misma puntual, con `implied_by`.
    implied = [e for e in d["capabilities"]
               if e["capability"] == "blueprints.read" and e["source"] == "capability_grant"]
    assert [(e["grant_id"], e["implied_by"]) for e in implied] == [(g["id"], "blueprints.apply")]
    # Y por rol (viewer) aparece su propia fila: una por fuente.
    assert any(e["capability"] == "blueprints.read" and e["source"] in ("role", "scoped_role")
               for e in d["capabilities"])


def test_global_capabilities_and_sources_are_reported(admin_client):
    uid, _ = _admin_como(admin_client, "otro_admin", role="viewer")
    d = _effective(admin_client, uid).json()["data"]
    assert d["global_capabilities"] == ["access_admin"]
    assert {e["source"] for e in d["capabilities"]} >= {"role", "global"}
    assert all(e["inert"] is False for e in d["capabilities"])


def test_pending_and_terminal_grants_do_not_appear(admin_client):
    uid = _crear(admin_client, "destino")["id"]
    _grant(admin_client, uid, "exports.download")  # sensible: pending
    _insert_cg(uid, "databases.write", "environment", env_id(DEV), status="revoked")
    d = _effective(admin_client, uid).json()["data"]
    assert not [e for e in d["capabilities"] if e["source"] == "capability_grant"]


def test_the_view_equals_what_the_actor_enforces(admin_client):
    """L1: el conjunto no inerte de ``explain`` es EXACTAMENTE ``Actor.capabilities``."""
    uid = _crear(admin_client, "destino")["id"]
    _scope_roles(admin_client, uid, "owner", env_id(DEV))
    _grant(admin_client, uid, "sql_console.execute", scope_id=env_id(PROD))  # pending: sin efecto
    _grant(admin_client, uid, "databases.write", scope_id=env_id(PROD))
    _insert_cg(uid, "servers.admin", "environment", env_id(DEV))  # no otorgable: descartada

    ctx = UserModel().find_access_context(uid)
    actor = actor_from_access_context(uid, "destino", ctx)
    caps = {e.capability for e in explain(ctx) if not e.inert}
    assert caps == set(actor.capabilities)

    d = _effective(admin_client, uid).json()["data"]
    assert {e["capability"] for e in d["capabilities"] if not e["inert"]} == {
        c.value for c in actor.capabilities
    }


def test_deactivated_user_grants_are_inert(admin_client):
    uid = _crear(admin_client, "destino")["id"]
    g = _grant(admin_client, uid, "databases.write").json()["data"]
    r = admin_client.patch(f"/api/v1/gateway-users/{uid}", json={"is_active": False})
    assert r.status_code == 200, r.text

    d = _effective(admin_client, uid).json()["data"]
    assert d["active"] is False
    cg = [e for e in d["capabilities"] if e["source"] == "capability_grant"]
    assert {e["grant_id"] for e in cg} == {g["id"]} and all(e["inert"] for e in cg)
    assert not any(e["inert"] for e in d["capabilities"] if e["source"] != "capability_grant")
    # La autenticación sigue sin verlas: el cargador normal no las trae.
    assert UserModel().find_access_context(uid)["capability_grants"] == []


def test_unknown_user_is_404(admin_client):
    r = _effective(admin_client, 99999)
    assert r.status_code == 404 and _code(r) == "gateway_user.not_found"


# --------------------------------------------------------------------------- #
# Quién puede leer                                                            #
# --------------------------------------------------------------------------- #


def test_non_access_admin_gets_an_opaque_403(admin_client):
    uid = _crear(admin_client, "destino")["id"]
    datos = _crear(admin_client, "oficial", gateway_role="viewer",
                   global_capabilities=["security_officer"])
    r = _effective(_cliente_como(datos, "oficial"), uid)
    assert r.status_code == 403 and _code(r) == "access.forbidden"

    op = _crear(admin_client, "operador", gateway_role="operator")
    r = _effective(_cliente_como(op, "operador"), uid)
    assert r.status_code == 403


def test_nobody_reads_another_users_effective_access_without_access_admin_even_self(admin_client):
    datos = _crear(admin_client, "operador", gateway_role="operator")
    c = _cliente_como(datos, "operador")
    assert _effective(c, datos["id"]).status_code == 403


# --------------------------------------------------------------------------- #
# /auth/me                                                                    #
# --------------------------------------------------------------------------- #


def test_me_lists_own_live_grants_only(admin_client):
    a = _crear(admin_client, "ana", gateway_role="viewer")
    b = _crear(admin_client, "beto", gateway_role="viewer")
    ca = _cliente_como(a, "ana")
    g1 = _grant(admin_client, a["id"], "databases.write", scope_id=env_id(DEV)).json()["data"]
    g2 = _grant(admin_client, a["id"], "exports.download", scope_id=env_id(PROD)).json()["data"]
    _insert_cg(a["id"], "blueprints.apply", "environment", env_id(DEV), status="revoked")
    gb = _grant(admin_client, b["id"], "databases.write", scope_id=env_id(PROD)).json()["data"]

    d = ca.get("/api/v1/auth/me").json()["data"]
    mine = {g["id"]: g for g in d["capability_grants"]}
    assert set(mine) == {g1["id"], g2["id"]} and gb["id"] not in mine
    assert mine[g1["id"]] == {
        "id": g1["id"], "capability": "databases.write", "scope_type": "environment",
        "scope_id": env_id(DEV), "scope_name": mine[g1["id"]]["scope_name"],
        "status": "active", "expires_at": None,
    }
    assert mine[g1["id"]]["scope_name"]
    assert mine[g2["id"]]["status"] == "pending" and mine[g2["id"]]["expires_at"]
    # Las activas se suman a `capabilities`; la pendiente no.
    assert "databases.write" in d["capabilities"]
    assert "exports.download" not in d["capabilities"]


def test_me_stays_backward_compatible(admin_client):
    d = admin_client.get("/api/v1/auth/me").json()["data"]
    assert d["capability_grants"] == []
    for k in ("id", "username", "role", "base_role", "capabilities", "global_capabilities",
              "scope_roles", "step_up_capabilities", "previous_login_at", "last_failed_at",
              "catalog_version"):
        assert k in d


def test_me_expires_overdue_pending_before_listing(admin_client):
    a = _crear(admin_client, "ana", gateway_role="viewer")
    ca = _cliente_como(a, "ana")
    g = _grant(admin_client, a["id"], "exports.download", scope_id=env_id(DEV)).json()["data"]
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE capability_grants SET expires_at = '2000-01-01 00:00:00' "
                          "WHERE id = :i"), {"i": g["id"]})
    assert ca.get("/api/v1/auth/me").json()["data"]["capability_grants"] == []


def test_me_survives_a_grants_table_failure(admin_client, monkeypatch):
    """
    Sin la tabla (código desplegado antes que la migración, o tras un downgrade) ``/auth/me``
    sigue respondiendo con la lista vacía: es lo que arranca la SPA, y el vencimiento
    perezoso y la lectura de capacidades puntuales son accesorios, no autorización.
    """
    from app.controllers.capability_grant_controller import CapabilityGrantController
    from app.models.capability_grant_model import CapabilityGrantModel

    def _falla(*_args, **_kwargs):
        raise RuntimeError("no such table: capability_grants")

    monkeypatch.setattr(CapabilityGrantController, "expire_overdue", _falla)
    monkeypatch.setattr(CapabilityGrantModel, "list_live_for_user", _falla)

    r = admin_client.get("/api/v1/auth/me")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["capability_grants"] == []
