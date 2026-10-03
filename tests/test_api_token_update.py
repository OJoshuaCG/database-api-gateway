"""
``PATCH /api-tokens/{id}``: editar los scopes de un token sin reemitirlo.

LO QUE ESTE ARCHIVO VERIFICA
----------------------------
Que el reemplazo de scopes rija desde la llamada siguiente del agente (el bearer no cambia),
que use el MISMO techo de agente que el alta (la edición no puede ser la puerta trasera para
escalar), que un token revocado no se edite y que la auditoría diga antes→después sin secretos.
"""

from app.core.database import Database
from tests.test_mcp_server import _crear_token, _proyecto, _rpc, mcp_on  # noqa: F401


def _patch(admin_client, token_pk: int, scopes):
    return admin_client.patch(f"/api/v1/api-tokens/{token_pk}", json={"scopes": scopes})


def _codigo(r) -> str:
    return r.json()["detail"]["public_context"]["code"]


def _tools(client, bearer: str) -> set[str]:
    return {t["name"] for t in _rpc(client, bearer, "tools/list").json()["result"]["tools"]}


def test_widening_scopes_is_reflected_in_tools_list_without_reissuing(
    client,
    admin_client,
    mcp_on,  # noqa: F811
):
    """
    Los scopes viven en la fila y ``authenticate`` la lee en cada request: el MISMO bearer ve
    más tools tras el PATCH, sin reemitir ni abrir otra terminal.
    """
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"])
    antes = _tools(client, datos["token"])

    r = _patch(admin_client, datos["id"], ["blueprints.read", "databases.read"])
    assert r.status_code == 200, r.text
    assert r.json()["data"]["scopes"] == ["blueprints.read", "databases.read"]
    # El secreto no viaja en la edición.
    assert "token" not in r.json()["data"]

    despues = _tools(client, datos["token"])
    assert antes < despues, "ampliar scopes tendría que publicar más tools con el mismo bearer"


def test_narrowing_scopes_hides_tools_immediately(client, admin_client, mcp_on):  # noqa: F811
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read", "databases.read"])
    antes = _tools(client, datos["token"])

    r = _patch(admin_client, datos["id"], ["blueprints.read"])
    assert r.status_code == 200, r.text
    assert r.json()["data"]["scopes"] == ["blueprints.read"]

    despues = _tools(client, datos["token"])
    assert despues < antes


def test_a_scope_outside_the_agent_ceiling_is_rejected_and_nothing_changes(admin_client):
    """La edición usa el mismo techo que el alta: no es un camino para escalar."""
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"])

    r = _patch(admin_client, datos["id"], ["blueprints.read", "databases.drop"])
    assert r.status_code == 422, r.text
    assert _codigo(r) == "api_token.scope_not_allowed"
    assert "allowed" in r.json()["detail"]["public_context"]

    listado = admin_client.get("/api/v1/api-tokens?size=50").json()["data"]
    fila = next(t for t in listado if t["id"] == datos["id"])
    assert fila["scopes"] == ["blueprints.read"]


def test_an_empty_scope_list_is_a_422(admin_client):
    """Un token sin permisos no sirve: lo que corresponde es revocarlo."""
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    assert _patch(admin_client, datos["id"], []).status_code == 422


def test_extra_fields_are_rejected(admin_client):
    """Solo ``scopes`` se edita: un ``name`` no se ignora en silencio."""
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    r = admin_client.patch(
        f"/api/v1/api-tokens/{datos['id']}",
        json={"scopes": ["blueprints.read"], "name": "otro-nombre"},
    )
    assert r.status_code == 422, r.text


def test_a_revoked_token_cannot_be_edited(admin_client):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    assert admin_client.delete(f"/api/v1/api-tokens/{datos['id']}").status_code == 200

    r = _patch(admin_client, datos["id"], ["blueprints.read"])
    assert r.status_code == 409, r.text
    assert _codigo(r) == "api_token.already_revoked"


def test_an_unknown_token_is_a_404(admin_client):
    r = _patch(admin_client, 999999, ["blueprints.read"])
    assert r.status_code == 404, r.text
    assert _codigo(r) == "api_token.not_found"


def test_the_audit_records_scopes_before_and_after_and_never_the_secret(admin_client):
    from app.models.audit_log import AuditLog

    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid, scopes=["blueprints.read"])
    secreto = datos["token"].split(".")[-1]
    assert (
        _patch(admin_client, datos["id"], ["blueprints.read", "databases.read"]).status_code == 200
    )

    s = Database().get_declarative_base_session()
    try:
        filas = s.query(AuditLog).filter(AuditLog.action == "api_token.update").all()
        assert filas, "la edición no dejó rastro"
        detalle = filas[-1].detail or ""
        assert "[blueprints.read]->[blueprints.read,databases.read]" in detalle
        assert datos["token_id"] in detalle
        assert secreto not in detalle
    finally:
        s.close()


def test_editing_requires_a_fresh_step_up(admin_client, expire_step_up):
    """``_method_requires`` pide step-up a todo método no seguro: el PATCH no es la excepción."""
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    expire_step_up(admin_client)

    r = _patch(admin_client, datos["id"], ["blueprints.read", "databases.read"])
    assert r.status_code == 403, r.text
    assert _codigo(r) == "access.step_up_required"
