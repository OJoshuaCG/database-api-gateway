"""
Scopes de DATOS en un token: re-autenticación del EMISOR, TTL corto, rastro y kill switch.

LO QUE SE FIJA
--------------
- ``_validate_scopes(raw, *, admin)``: un scope de datos exige step-up FRESCO del emisor y deja
  ``api_token.data_scope_grant`` con ``record_intent`` fail-closed. Los scopes sin datos no tocan
  ``admin`` (los llamadores existentes siguen andando).
- Un ``admin`` que no es un ``Actor`` (dict legado) o es un token de agente falla CERRADO.
- TTL: un token con scope de datos vive como máximo ``MCP_DATA_TOKEN_MAX_TTL_DAYS`` desde el alta
  y desde el PATCH (la vida restante cuenta).
- Con el kill switch apagado el scope se guarda y se muestra, pero el token no lo ejerce
  (``token_actor``): inerte, no borrado.
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import pytest

from app.controllers import api_token_controller as atc
from app.core import environments
from app.core.actor import admin_actor, token_actor
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.services import audit as audit_mod
from app.services.capability_catalog import Capability, GatewayRole
from tests.step_up_helpers import OPEN_WINDOW
from tests.test_mcp_server import _crear_token, _proyecto, mcp_on  # noqa: F401

DATOS = ["blueprints.read", "databases.read", "data.read"]


def _emisor(*, fresco: bool):
    return admin_actor(
        user_id=1,
        username="emisor",
        role=GatewayRole.OWNER,
        step_up_until=OPEN_WINDOW if fresco else None,
    )


def _codigo(exc: pytest.ExceptionInfo) -> str:
    return exc.value.public_context["code"]


def _filas(accion: str) -> list[AuditLog]:
    s = Database().get_declarative_base_session()
    try:
        return [a for a in s.query(AuditLog).all() if a.action == accion]
    finally:
        s.close()


def _post(admin_client, pid, **extra):
    return admin_client.post(
        "/api/v1/api-tokens", json={"name": "agente-datos", "project_id": pid, **extra}
    )


def _fila_token(pk: int) -> ApiToken:
    s = Database().get_declarative_base_session()
    try:
        fila = s.get(ApiToken, pk)
        s.expunge(fila)
        return fila
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# _validate_scopes: firma y re-autenticación                                   #
# --------------------------------------------------------------------------- #


def test_non_data_scopes_do_not_touch_the_issuer():
    """Los llamadores existentes (que no traen datos) siguen pasando, incluso con admin=None."""
    assert atc._validate_scopes(["blueprints.read", "databases.read"], admin=None) == [
        "blueprints.read",
        "databases.read",
    ]


def test_a_data_scope_with_a_fresh_issuer_is_accepted_and_audited(client):
    antes = len(_filas("api_token.data_scope_grant"))
    assert atc._validate_scopes(DATOS, admin=_emisor(fresco=True)) == DATOS
    filas = _filas("api_token.data_scope_grant")
    assert len(filas) == antes + 1
    assert filas[-1].status == "attempt"
    assert "data.read" in (filas[-1].detail or "")


def test_a_data_scope_without_a_fresh_step_up_is_refused_before_any_trace(client):
    antes = len(_filas("api_token.data_scope_grant"))
    with pytest.raises(AppHttpException) as exc:
        atc._validate_scopes(DATOS, admin=_emisor(fresco=False))
    assert exc.value.status_code == 403
    assert _codigo(exc) == "access.step_up_required"
    assert len(_filas("api_token.data_scope_grant")) == antes


def test_a_legacy_dict_issuer_fails_closed_for_data_scopes():
    with pytest.raises(AppHttpException) as exc:
        atc._validate_scopes(["data.query"], admin={"id": 1, "username": "x"})
    assert exc.value.status_code == 403
    assert _codigo(exc) == "access.step_up_required"


def test_an_agent_issuer_is_forbidden_for_data_scopes():
    agente = token_actor(
        token_pk=1, token_id="t", name="n", scopes="data.read", project_id=1,
        issuer=_emisor(fresco=True),
    )
    with pytest.raises(AppHttpException) as exc:
        atc._validate_scopes(["data.read"], admin=agente)
    assert _codigo(exc) == "access.forbidden"


def test_the_audit_intent_is_fail_closed(monkeypatch):
    def _cae(action, **kw):
        raise AppHttpException(message="auditoría caída", status_code=500)

    monkeypatch.setattr(audit_mod, "record_intent", _cae)
    with pytest.raises(AppHttpException) as exc:
        atc._validate_scopes(DATOS, admin=_emisor(fresco=True))
    assert exc.value.status_code == 500


def test_create_token_with_a_failing_audit_creates_nothing(admin_client, monkeypatch):
    pid = _proyecto(admin_client)
    s = Database().get_declarative_base_session()
    try:
        antes = s.query(ApiToken).count()
    finally:
        s.close()

    def _cae(action, **kw):
        raise AppHttpException(message="auditoría caída", status_code=500)

    monkeypatch.setattr(audit_mod, "record_intent", _cae)
    r = _post(admin_client, pid, scopes=DATOS, expires_in_days=10)
    assert r.status_code == 500, r.text
    s = Database().get_declarative_base_session()
    try:
        assert s.query(ApiToken).count() == antes
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Por HTTP: alta, TTL y PATCH                                                  #
# --------------------------------------------------------------------------- #


def test_create_with_data_scope_succeeds_within_the_data_ttl(admin_client):
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=DATOS, expires_in_days=environments.MCP_DATA_TOKEN_MAX_TTL_DAYS)
    assert r.status_code == 201, r.text
    # Con el switch apagado el scope se guarda y se muestra (inerte), no se descarta.
    assert "data.read" in r.json()["data"]["scopes"]
    assert _filas("api_token.data_scope_grant")


def test_create_with_data_scope_and_a_longer_ttl_is_refused(admin_client):
    pid = _proyecto(admin_client)
    r = _post(
        admin_client, pid, scopes=DATOS,
        expires_in_days=environments.MCP_DATA_TOKEN_MAX_TTL_DAYS + 1,
    )
    assert r.status_code == 422, r.text
    ctx = r.json()["detail"]["public_context"]
    assert ctx["code"] == "api_token.ttl_too_long"
    assert ctx["max_days"] == environments.MCP_DATA_TOKEN_MAX_TTL_DAYS


def test_create_without_data_scope_keeps_the_long_ttl(admin_client):
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=["blueprints.read"], expires_in_days=90)
    assert r.status_code == 201, r.text


def test_default_ttl_with_a_data_scope_is_refused_not_silently_clamped(admin_client):
    """Sin ``expires_in_days`` el default es el tope general (90): excede el de datos."""
    if environments.MCP_TOKEN_MAX_TTL_DAYS <= environments.MCP_DATA_TOKEN_MAX_TTL_DAYS:
        pytest.skip("el tope general ya no excede al de datos")
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=DATOS)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "api_token.ttl_too_long"


def test_patch_adding_a_data_scope_to_a_long_lived_token_is_refused(admin_client):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"], expires_in_days=90)
    r = admin_client.patch(f"/api/v1/api-tokens/{datos['id']}", json={"scopes": DATOS})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "api_token.ttl_too_long"
    assert "data.read" not in _fila_token(datos["id"]).scopes


def test_patch_adding_a_data_scope_to_a_short_lived_token_works(admin_client):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"], expires_in_days=7)
    r = admin_client.patch(f"/api/v1/api-tokens/{datos['id']}", json={"scopes": DATOS})
    assert r.status_code == 200, r.text
    assert "data.read" in r.json()["data"]["scopes"]
    assert _filas("api_token.data_scope_grant")


def test_patch_with_only_non_data_scopes_still_works_on_a_long_lived_token(admin_client):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"], expires_in_days=90)
    r = admin_client.patch(
        f"/api/v1/api-tokens/{datos['id']}", json={"scopes": ["blueprints.read", "databases.read"]}
    )
    assert r.status_code == 200, r.text


def test_a_stored_data_scope_survives_a_patch_while_the_switch_is_off(admin_client):
    """La SPA muestra lo guardado (inerte); un PATCH que lo reenvía no lo pierde en silencio."""
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=DATOS, expires_in_days=7)
    assert "data.read" in datos["scopes"]
    r = admin_client.patch(f"/api/v1/api-tokens/{datos['id']}", json={"scopes": DATOS})
    assert r.status_code == 200, r.text
    assert "data.read" in r.json()["data"]["scopes"]


# --------------------------------------------------------------------------- #
# Kill switch: el token no ejerce el scope                                     #
# --------------------------------------------------------------------------- #


def _actor(scopes: str):
    return token_actor(
        token_pk=1, token_id="t", name="n", scopes=scopes, project_id=1,
        issuer=_emisor(fresco=True),
    )


def test_a_token_with_a_data_scope_is_inert_while_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", False)
    actor = _actor("databases.read,data.read")
    assert not actor.has(Capability.DATA_READ)
    assert actor.has(Capability.DATABASES_READ)


def test_the_same_token_exercises_the_scope_once_the_switch_is_on(monkeypatch):
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    assert _actor("databases.read,data.read").has(Capability.DATA_READ)


def test_the_issuer_still_caps_a_data_scope(monkeypatch):
    """Un emisor sin ``data.read`` (viewer) no delega lo que no tiene, con el switch ya encendido."""
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    viewer = admin_actor(user_id=2, username="v", role=GatewayRole.VIEWER)
    actor = token_actor(
        token_pk=1, token_id="t", name="n", scopes="databases.read,data.read",
        project_id=1, issuer=viewer,
    )
    assert not actor.has(Capability.DATA_READ)


def test_tools_list_for_a_token_follows_the_kill_switch(client, admin_client, mcp_on, monkeypatch):
    """``tools_for`` filtra por ``actor.has``: con el switch apagado ninguna tool de datos asoma."""
    from app.mcp.registry import tools_for

    sin = tools_for(_actor("databases.read,data.read,data.query"))
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", True)
    con = tools_for(_actor("databases.read,data.read,data.query"))
    assert {t.name for t in sin} <= {t.name for t in con}
    assert not any(t.scope in ("data.read", "data.query") for t in sin)

