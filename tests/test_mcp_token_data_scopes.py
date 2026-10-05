"""
Scopes de DATOS en un token: re-autenticación del EMISOR, TTL corto, rastro y kill switch.

LO QUE SE FIJA
--------------
- ``_validate_scopes(raw, *, admin)``: un scope de datos exige step-up FRESCO del emisor y deja
  ``api_token.data_scope_grant`` con ``record_intent`` fail-closed. Los scopes sin datos no tocan
  ``admin`` (los llamadores existentes siguen andando).
- Un ``admin`` que no es un ``Actor`` (dict legado) o es un token de agente falla CERRADO.
- TTL: con ``MCP_DATA_TOKEN_MAX_TTL_DAYS`` >= 1 un token con scope de datos vive como máximo ese
  tope desde el alta y desde el PATCH (la vida restante cuenta). Con 0, el valor por defecto, no
  hay tope propio y rige solo ``MCP_TOKEN_MAX_TTL_DAYS``. Cada test fija el tope que prueba con
  ``monkeypatch`` sobre el controlador y no depende de lo que traiga el entorno.
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

# Tope propio de vida que se activa en los tests del tope (el valor por defecto de producción es 0).
TOPE_DE_DATOS_ACTIVO_EN_DIAS = 30


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


def test_create_with_data_scope_succeeds_within_the_data_ttl(admin_client, monkeypatch):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", TOPE_DE_DATOS_ACTIVO_EN_DIAS)
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=DATOS, expires_in_days=TOPE_DE_DATOS_ACTIVO_EN_DIAS)
    assert r.status_code == 201, r.text
    # Con el switch apagado el scope se guarda y se muestra (inerte), no se descarta.
    assert "data.read" in r.json()["data"]["scopes"]
    assert _filas("api_token.data_scope_grant")


def test_create_with_data_scope_and_a_longer_ttl_is_refused(admin_client, monkeypatch):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", TOPE_DE_DATOS_ACTIVO_EN_DIAS)
    pid = _proyecto(admin_client)
    r = _post(
        admin_client, pid, scopes=DATOS,
        expires_in_days=TOPE_DE_DATOS_ACTIVO_EN_DIAS + 1,
    )
    assert r.status_code == 422, r.text
    ctx = r.json()["detail"]["public_context"]
    assert ctx["code"] == "api_token.ttl_too_long"
    assert ctx["max_days"] == TOPE_DE_DATOS_ACTIVO_EN_DIAS


def test_create_with_data_scope_and_the_general_max_ttl_works_when_the_data_cap_is_off(
    admin_client, monkeypatch
):
    """Con ``MCP_DATA_TOKEN_MAX_TTL_DAYS=0`` un token de datos puede vivir el tope general."""
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=DATOS, expires_in_days=environments.MCP_TOKEN_MAX_TTL_DAYS)
    assert r.status_code == 201, r.text
    assert "data.read" in r.json()["data"]["scopes"]
    assert _filas("api_token.data_scope_grant")


def test_the_general_max_ttl_still_applies_to_a_data_token_when_the_data_cap_is_off(
    admin_client, monkeypatch
):
    """Quitar el tope propio no habilita tokens perpetuos: el general sigue rigiendo."""
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)
    pid = _proyecto(admin_client)
    r = _post(
        admin_client, pid, scopes=DATOS, expires_in_days=environments.MCP_TOKEN_MAX_TTL_DAYS + 1
    )
    assert r.status_code == 422, r.text
    ctx = r.json()["detail"]["public_context"]
    assert ctx["code"] == "api_token.ttl_too_long"
    assert ctx["max_days"] == environments.MCP_TOKEN_MAX_TTL_DAYS


def test_create_without_data_scope_keeps_the_long_ttl(admin_client):
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=["blueprints.read"], expires_in_days=90)
    assert r.status_code == 201, r.text


def test_default_ttl_with_a_data_scope_is_refused_not_silently_clamped(admin_client, monkeypatch):
    """Sin ``expires_in_days`` el default es el tope general (90): excede el de datos activo."""
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", TOPE_DE_DATOS_ACTIVO_EN_DIAS)
    if environments.MCP_TOKEN_MAX_TTL_DAYS <= TOPE_DE_DATOS_ACTIVO_EN_DIAS:
        pytest.skip("el tope general ya no excede al de datos")
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=DATOS)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "api_token.ttl_too_long"


def test_default_ttl_with_a_data_scope_works_when_the_data_cap_is_off(admin_client, monkeypatch):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)
    pid = _proyecto(admin_client)
    r = _post(admin_client, pid, scopes=DATOS)
    assert r.status_code == 201, r.text


def test_patch_adding_a_data_scope_to_a_long_lived_token_is_refused(admin_client, monkeypatch):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", TOPE_DE_DATOS_ACTIVO_EN_DIAS)
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"], expires_in_days=90)
    r = admin_client.patch(f"/api/v1/api-tokens/{datos['id']}", json={"scopes": DATOS})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "api_token.ttl_too_long"
    assert "data.read" not in _fila_token(datos["id"]).scopes


def test_patch_adding_a_data_scope_to_a_long_lived_token_works_when_the_data_cap_is_off(
    admin_client, monkeypatch
):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"], expires_in_days=90)
    r = admin_client.patch(f"/api/v1/api-tokens/{datos['id']}", json={"scopes": DATOS})
    assert r.status_code == 200, r.text
    assert "data.read" in r.json()["data"]["scopes"]
    assert _filas("api_token.data_scope_grant")


def test_patch_adding_a_data_scope_to_a_short_lived_token_works(admin_client, monkeypatch):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", TOPE_DE_DATOS_ACTIVO_EN_DIAS)
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


# --------------------------------------------------------------------------- #
# Quién puede AGREGAR un scope de datos a un token que ya existe               #
# --------------------------------------------------------------------------- #


def _editor(user_id: int, role: GatewayRole, *, step_up_fresco: bool = True):
    return admin_actor(
        user_id=user_id,
        username=f"editor{user_id}",
        role=role,
        step_up_until=OPEN_WINDOW if step_up_fresco else None,
    )


def _token_ajeno(admin_client, monkeypatch, *, scopes):
    """Un token emitido por el admin del fixture; devuelve (fila serializada, id de su emisor)."""
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)  # el tope de vida no es el tema
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid, scopes=scopes, expires_in_days=7)
    emisor = _fila_token(token["id"]).created_by_admin_id
    assert emisor is not None
    return token, emisor


def _editar(token_pk, scopes, editor):
    return atc.ApiTokenController().update_token(token_pk, {"scopes": scopes}, admin=editor)


def test_an_editor_who_is_not_the_issuer_and_lacks_the_scope_cannot_add_it(
    admin_client, monkeypatch
):
    token, emisor = _token_ajeno(admin_client, monkeypatch, scopes=["blueprints.read"])
    ajeno_sin_el_permiso = _editor(emisor + 100, GatewayRole.OPERATOR)

    with pytest.raises(AppHttpException) as exc:
        _editar(token["id"], DATOS, ajeno_sin_el_permiso)

    assert exc.value.status_code == 403 and _codigo(exc) == "access.forbidden"
    assert "data.read" not in _fila_token(token["id"]).scopes


def test_the_original_issuer_can_add_a_data_scope_to_its_own_token(admin_client, monkeypatch):
    token, emisor = _token_ajeno(admin_client, monkeypatch, scopes=["blueprints.read"])

    # Aunque hoy no tenga el permiso: el scope queda inerte (el token ejerce la intersección con
    # lo que su emisor puede), así que permitirlo no le da a nadie nada que no tuviera.
    salida = _editar(token["id"], DATOS, _editor(emisor, GatewayRole.OPERATOR))

    assert "data.read" in salida["scopes"]


def test_another_editor_who_already_holds_the_scope_can_add_it(admin_client, monkeypatch):
    token, emisor = _token_ajeno(admin_client, monkeypatch, scopes=["blueprints.read"])

    salida = _editar(token["id"], DATOS, _editor(emisor + 100, GatewayRole.OWNER))

    assert "data.read" in salida["scopes"]


def test_an_outsider_can_still_edit_the_non_data_scopes_of_a_token(admin_client, monkeypatch):
    token, emisor = _token_ajeno(admin_client, monkeypatch, scopes=["blueprints.read"])
    ajeno = _editor(emisor + 100, GatewayRole.OPERATOR, step_up_fresco=False)

    salida = _editar(token["id"], ["blueprints.read", "databases.read"], ajeno)

    assert set(salida["scopes"]) == {"blueprints.read", "databases.read"}


def test_an_outsider_can_remove_a_data_scope_or_keep_one_the_token_already_had(
    admin_client, monkeypatch
):
    """La regla mira lo que se AGREGA: quitar o conservar un scope de datos no exige nada nuevo."""
    token, emisor = _token_ajeno(admin_client, monkeypatch, scopes=DATOS)
    ajeno = _editor(emisor + 100, GatewayRole.OPERATOR)

    conservado = _editar(token["id"], DATOS + ["catalogs.read"], ajeno)
    assert "data.read" in conservado["scopes"] and "catalogs.read" in conservado["scopes"]

    quitado = _editar(token["id"], ["blueprints.read", "databases.read"], ajeno)
    assert "data.read" not in quitado["scopes"]


def test_a_legacy_admin_without_capabilities_cannot_add_a_data_scope_either(
    admin_client, monkeypatch
):
    """Un ``admin`` que no es un ``Actor`` (dict legado) no tiene ``has``: falla cerrado."""
    token, emisor = _token_ajeno(admin_client, monkeypatch, scopes=["blueprints.read"])
    fila = _fila_token(token["id"])

    with pytest.raises(AppHttpException) as exc:
        atc._require_editor_may_add_data_scopes(
            {"id": emisor + 100, "username": "legado"}, fila, [Capability.DATA_READ]
        )

    assert exc.value.status_code == 403

