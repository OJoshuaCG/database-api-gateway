"""
El token MCP hereda los permisos de quien lo emitió (``ApiToken.created_by_admin_id``).

LO QUE ESTE ARCHIVO FIJA
------------------------
- Un emisor NULL, inexistente o desactivado deja al token rechazado con el 401 opaco de siempre
  (``mcp.token_invalid``); el motivo ``emisor_inactivo`` solo viaja en el ``detail`` de la
  auditoría.
- Las capacidades del token son ``scopes ∩ techo de agente ∩ capacidades del emisor``: el token
  nunca gana algo que su emisor no tiene, y sin emisor el lector falla cerrado.
- Un emisor normal no cambia nada de lo que el token ya podía.

Límite declarado: se acotan capacidades, NO el alcance por entorno (el modelo de roles no tiene
denegación por entorno).
"""

import pytest
from sqlalchemy import text

from app.core.actor import Actor, admin_actor, token_actor
from app.core.database import Database
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    AGENT_DATA_EXCEPTIONS,
    Capability,
    GatewayRole,
)
from tests.test_mcp_server import _crear_token, _proyecto, _rpc, mcp_on  # noqa: F401


def _sql(sql: str, **params) -> None:
    with Database().engine.begin() as conn:
        conn.execute(text(sql), params)


def _detalle_codigo(r) -> str:
    return r.json()["detail"]["public_context"]["code"]


def _tools(client, bearer: str) -> set[str]:
    return {t["name"] for t in _rpc(client, bearer, "tools/list").json()["result"]["tools"]}


def _emisor_extra(admin_client, username: str) -> int:
    """Un usuario viewer aparte (sin elevación) que sirva de emisor."""
    from app.models.user_model import UserModel
    from tests.access_request_helpers import create_user

    create_user(admin_client, username)
    return UserModel().find_by_username(username)["id"]


def _token(admin_client, **extra) -> dict:
    pid = _proyecto(admin_client)
    return _crear_token(admin_client, project_id=pid, **extra)


def _filas_de_rechazo() -> list[str]:
    from app.models.audit_log import AuditLog

    s = Database().get_declarative_base_session()
    try:
        filas = (
            s.query(AuditLog)
            .filter(AuditLog.action == "mcp.auth", AuditLog.status == "failure")
            .order_by(AuditLog.id)
            .all()
        )
        return [f.detail or "" for f in filas]
    finally:
        s.close()


def _emisor_con(*caps: Capability) -> Actor:
    """Un emisor con EXACTAMENTE esas capacidades (los roles reales siempre leen todo)."""
    return Actor(kind="admin", id=1, username="emisor", capabilities=frozenset(caps))


# --------------------------------------------------------------------------- #
# Rechazo por emisor inválido                                                  #
# --------------------------------------------------------------------------- #


def test_a_token_with_an_active_issuer_authenticates(client, admin_client, mcp_on):  # noqa: F811
    datos = _token(admin_client)
    assert _rpc(client, datos["token"], "tools/list").status_code == 200


def test_an_inactive_issuer_rejects_the_token(client, admin_client, mcp_on):  # noqa: F811
    emisor = _emisor_extra(admin_client, "emisor-inactivo")
    datos = _token(admin_client)
    _sql("UPDATE api_tokens SET created_by_admin_id = :u WHERE id = :t", u=emisor, t=datos["id"])
    assert _rpc(client, datos["token"], "tools/list").status_code == 200

    _sql("UPDATE users SET is_active = 0 WHERE id = :u", u=emisor)
    r = _rpc(client, datos["token"], "tools/list")
    assert r.status_code == 401
    assert _detalle_codigo(r) == "mcp.token_invalid"
    assert "emisor_inactivo" not in r.text  # el motivo va a la auditoría, no a la respuesta
    assert any("rechazo=emisor_inactivo" in f for f in _filas_de_rechazo())


def test_a_deleted_issuer_rejects_the_token(client, admin_client, mcp_on):  # noqa: F811
    datos = _token(admin_client)
    # Sin FK a propósito: el id apunta a una fila que no existe.
    _sql("UPDATE api_tokens SET created_by_admin_id = 999999 WHERE id = :t", t=datos["id"])
    r = _rpc(client, datos["token"], "tools/list")
    assert r.status_code == 401
    assert _detalle_codigo(r) == "mcp.token_invalid"
    assert any("rechazo=emisor_inactivo" in f for f in _filas_de_rechazo())


def test_a_legacy_token_with_null_issuer_is_rejected(client, admin_client, mcp_on):  # noqa: F811
    datos = _token(admin_client)
    _sql("UPDATE api_tokens SET created_by_admin_id = NULL WHERE id = :t", t=datos["id"])
    r = _rpc(client, datos["token"], "tools/list")
    assert r.status_code == 401
    assert _detalle_codigo(r) == "mcp.token_invalid"
    assert any("rechazo=emisor_inactivo" in f for f in _filas_de_rechazo())


# --------------------------------------------------------------------------- #
# Intersección de capacidades                                                  #
# --------------------------------------------------------------------------- #


def test_the_issuer_lacking_a_capability_narrows_the_token():
    emisor = _emisor_con(Capability.BLUEPRINTS_READ)
    actor = token_actor(
        token_pk=1,
        token_id="t",
        name="n",
        scopes="blueprints.read,databases.read",
        project_id=1,
        issuer=emisor,
    )
    assert actor.capabilities == frozenset({Capability.BLUEPRINTS_READ})
    assert not actor.has(Capability.DATABASES_READ)
    assert actor.issuer is emisor


def test_the_token_never_gains_what_the_issuer_has_but_the_scopes_do_not():
    emisor = admin_actor(user_id=1, username="o", role=GatewayRole.OWNER)
    actor = token_actor(
        token_pk=1, token_id="t", name="n", scopes="blueprints.read", project_id=1, issuer=emisor
    )
    assert actor.capabilities == frozenset({Capability.BLUEPRINTS_READ})


def test_a_normal_issuer_leaves_the_token_unchanged():
    emisor = admin_actor(user_id=1, username="o", role=GatewayRole.OWNER)
    actor = token_actor(
        token_pk=1,
        token_id="t",
        name="n",
        scopes=",".join(c.value for c in Capability),
        project_id=1,
        issuer=emisor,
    )
    assert actor.capabilities == AGENT_ALLOWED - AGENT_DATA_EXCEPTIONS


def test_without_issuer_the_reader_fails_closed():
    actor = token_actor(
        token_pk=1, token_id="t", name="n", scopes="blueprints.read", project_id=1
    )
    assert actor.capabilities == frozenset()


def test_a_degraded_issuer_hides_tools_on_the_next_request(
    client, admin_client, mcp_on, monkeypatch  # noqa: F811
):
    """Se relee el emisor en cada request: lo que pierda se le va al token de inmediato."""
    import app.core.mcp_auth as auth_mod

    datos = _token(admin_client, scopes=["blueprints.read", "databases.read"])
    antes = _tools(client, datos["token"])

    monkeypatch.setattr(
        auth_mod, "_load_issuer", lambda _id: _emisor_con(Capability.BLUEPRINTS_READ)
    )
    despues = _tools(client, datos["token"])
    assert despues < antes
