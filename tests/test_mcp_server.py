"""
El servidor MCP de punta a punta: token → autenticación → gate → tool.

LO QUE ESTE ARCHIVO VERIFICA Y LO QUE NO
----------------------------------------
Verifica el **vertical completo** de la única tool de la v1 que no toca ningún motor
(``list_databases``, que lee el inventario del gateway): que el token se autentique, que el kill
switch corte, que el gate niegue por default en sus cuatro ejes de política, y que el secreto no
aparezca en ningún rastro.

**No verifica** las tools que leen el catálogo del motor, porque no existen todavía: son
consultas a `information_schema` / `pg_catalog` y sin un motor real no hay nada que comprobar de
ellas. Entregarlas a ciegas las sumaría a la deuda que el `TODO.md` declara como la más grande del
proyecto.
"""

import json

import pytest
from sqlalchemy import text

from app.core.database import Database


@pytest.fixture()
def mcp_on(monkeypatch):
    """
    Enciende el kill switch. Nace apagado, así que hay que prenderlo explícitamente **en los dos
    módulos que lo leen**: el valor se importa por nombre, no se consulta por atributo.
    """
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", True)
    return True


def _crear_token(admin_client, *, project_id: int, **extra) -> dict:
    payload = {"name": "repo-frontend", "project_id": project_id, **extra}
    r = admin_client.post("/api/v1/api-tokens", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _proyecto(admin_client, nombre="Omnicanal") -> int:
    r = admin_client.post("/api/v1/projects", json={"name": nombre})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _rpc(client, bearer: str | None, metodo: str, params: dict | None = None, rid=1):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    cuerpo = {"jsonrpc": "2.0", "id": rid, "method": metodo}
    if params is not None:
        cuerpo["params"] = params
    return client.post("/mcp/", json=cuerpo, headers=headers)


def _contenido(resp) -> dict:
    """El objeto estructurado de la respuesta de una tool."""
    return resp.json()["result"]["structuredContent"]


# --------------------------------------------------------------------------- #
# El kill switch                                                              #
# --------------------------------------------------------------------------- #


def test_the_server_is_off_by_default(client, admin_client):
    """
    Nace APAGADO. Un endpoint que sirve estructura de bases de terceros a un agente no puede
    quedar habilitado por el default de un despliegue que nadie configuró.
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]

    r = _rpc(client, token, "initialize")
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["public_context"]["code"] == "mcp.disabled"


def test_the_kill_switch_is_checked_before_the_credential(client, mcp_on, monkeypatch):
    """
    El switch vive en UN choke point y se evalúa **antes** que el bearer: apagado, ni siquiera
    un token válido entra, y un token basura recibe el mismo 503 — o sea que apagado el servidor
    no es un oráculo de qué tokens existen.
    """
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", False)
    r = _rpc(client, "dbgw.basura.basura", "initialize")
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
# La autenticación                                                            #
# --------------------------------------------------------------------------- #


def test_a_valid_token_can_initialize(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]

    r = _rpc(client, token, "initialize")
    assert r.status_code == 200, r.text
    datos = r.json()["result"]
    assert datos["protocolVersion"]
    # Solo `tools`: declarar `resources` o `prompts` vacíos hace que el cliente los consulte y
    # reciba un método desconocido.
    assert set(datos["capabilities"]) == {"tools"}


@pytest.mark.parametrize(
    "bearer",
    [
        None,
        "sin-prefijo",
        "dbgw.solo-dos-partes",
        "otro.aaaaaaaaaaaaaaaaaaaaaaaa.bbbb",
        "dbgw.inexistente000000000.bbbb",
    ],
)
def test_every_bad_credential_gets_the_same_opaque_401(client, mcp_on, bearer):
    """
    **Un único código opaco.** Inexistente, expirado, revocado y malformado responden igual: si
    se distinguieran, el endpoint sería un oráculo del estado de los tokens que alguien haya
    adivinado.
    """
    r = _rpc(client, bearer, "initialize")
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["public_context"]["code"] == "mcp.token_invalid"


def test_a_revoked_token_stops_working(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    assert _rpc(client, datos["token"], "initialize").status_code == 200

    assert admin_client.delete(f"/api/v1/api-tokens/{datos['id']}").status_code == 200
    r = _rpc(client, datos["token"], "initialize")
    assert r.status_code == 401
    assert r.json()["detail"]["public_context"]["code"] == "mcp.token_invalid"


def test_an_expired_token_stops_working(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE api_tokens SET expires_at = '2000-01-01 00:00:00' WHERE id = :i"),
            {"i": datos["id"]},
        )
    assert _rpc(client, datos["token"], "initialize").status_code == 401


def test_the_mcp_never_accepts_a_cookie(admin_client, mcp_on):
    """
    **El invariante correlativo de la exención de CSRF**: un token nunca autentica por cookie y
    una cookie nunca autentica por ``Authorization``. Si los dos caminos se pudieran mezclar, la
    exención que tienen los agentes —correcta, porque un bearer no es ambiental— se volvería el
    bypass: cualquier página abierta en el navegador del admin podría llamar al MCP.

    ``admin_client`` tiene la cookie de sesión puesta y NO manda bearer.
    """
    r = admin_client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401, r.text


# --------------------------------------------------------------------------- #
# El protocolo                                                                #
# --------------------------------------------------------------------------- #


def test_a_malformed_body_is_a_protocol_error_not_a_422(client, admin_client, mcp_on):
    """
    Un cuerpo no-JSON tiene que salir como ``PARSE_ERROR`` de JSON-RPC, no como el 422 de
    FastAPI: para un cliente MCP un 422 es un servidor roto, no un mensaje mal formado.
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]

    r = client.post(
        "/mcp/", content=b"esto no es json", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["error"]["code"] == -32700


def test_an_unknown_method_is_a_protocol_error(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    r = _rpc(client, token, "resources/list")
    assert r.json()["error"]["code"] == -32601


def test_a_batch_is_rejected(client, admin_client, mcp_on):
    """
    Un batch multiplicaría el presupuesto de bytes por N **sin que el tope lo vea**, y ningún
    cliente MCP lo necesita.
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    r = client.post(
        "/mcp/",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "initialize"}],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["error"]["code"] == -32600


def test_tools_list_publishes_closed_schemas(client, admin_client, mcp_on):
    """
    Lo que ``tools/list`` publica **lo lee el modelo del agente como instrucción** — es el vector
    de tool poisoning. Los invariantes del registro se afirman al importar; acá se verifica que
    lo que sale por el cable sea eso.
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]

    tools = _rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_databases"]
    for t in tools:
        assert t["inputSchema"]["additionalProperties"] is False


def test_an_unknown_tool_is_a_protocol_error(client, admin_client, mcp_on):
    """
    Una tool que ``tools/list`` no publica es un bug del cliente, no una negación de acceso: va
    como error de PROTOCOLO para que el agente no lo confunda con "no tenés permiso".
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    r = _rpc(client, token, "tools/call", {"name": "query", "arguments": {}})
    assert r.json()["error"]["code"] == -32601


# --------------------------------------------------------------------------- #
# El gate: niega por default                                                  #
# --------------------------------------------------------------------------- #


def _bd_alcanzable(admin_client, *, project_id, opt_in=True, blocked=False, env="development",
                   env_allows=True):
    """
    Siembra servidor + blueprint + proyecto + BD y mueve las palancas del gate.

    El servidor hace falta porque el filtro de alcance hace ``JOIN`` con ``servers``: sin fila,
    la base no aparece **aunque todas las palancas estén bien**. Fue lo que detectó el test del
    camino feliz — y por eso ese test es el que importa: los de negación pasan igual con la
    consulta rota.
    """
    from app.models.managed_database import ManagedDatabase

    srv = admin_client.post("/api/v1/servers", json={
        "name": f"srv-mcp-{project_id}", "host": "127.0.0.1", "port": 3399,
        "engine": "mysql", "root_username": "root", "root_password": "supersecret",
    })
    assert srv.status_code == 201, srv.text
    server_id = srv.json()["data"]["id"]

    modelo = admin_client.post(
        "/api/v1/database-models", json={"name": f"Core {project_id}", "slug": f"core-{project_id}"}
    ).json()["data"]["id"]
    assert admin_client.post(
        f"/api/v1/projects/{project_id}/blueprints", json={"model_ids": [modelo]}
    ).status_code in (200, 201)

    with Database().engine.begin() as conn:
        env_id = conn.execute(
            text("SELECT id FROM environments WHERE slug = :s"), {"s": env}
        ).fetchone()[0]
        conn.execute(
            text("UPDATE environments SET allows_agent_access = :v WHERE id = :i"),
            {"v": 1 if env_allows else 0, "i": env_id},
        )

    s = Database().get_declarative_base_session()
    try:
        bd = ManagedDatabase(
            name="core_cliente1", server_id=server_id, owner_id=1, model_id=modelo,
            model_version="0003", environment_id=env_id,
            agent_access_allowed=opt_in, agent_access_blocked=blocked,
        )
        s.add(bd)
        s.commit()
        return bd.id
    finally:
        s.close()


def test_the_gate_denies_by_default(client, admin_client, mcp_on):
    """
    **El default es negar, y está puesto en el DDL además del código.** Una base recién creada
    no tiene el opt-in, así que no aparece — con default permisivo, habilitar el MCP habría
    dejado legible todo lo ya clasificado sin que nadie lo decidiera.
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    _bd_alcanzable(admin_client, project_id=pid, opt_in=False)

    r = _rpc(client, token, "tools/call", {"name": "list_databases", "arguments": {}})
    assert r.status_code == 200, r.text
    assert _contenido(r)["count"] == 0


def test_an_opted_in_database_is_listed(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    _bd_alcanzable(admin_client, project_id=pid, opt_in=True)

    datos = _contenido(_rpc(client, token, "tools/call",
                            {"name": "list_databases", "arguments": {}}))
    assert datos["count"] == 1
    fila = datos["databases"][0]
    assert fila["name"] == "core_cliente1"
    assert fila["blueprint"].startswith("core-")
    assert fila["applied_version"] == "0003"


def test_the_veto_wins_over_the_opt_in(client, admin_client, mcp_on):
    """El bloqueo gana sobre el permiso, y **no tiene override**: ni `force`, ni nada."""
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    _bd_alcanzable(admin_client, project_id=pid, opt_in=True, blocked=True)

    assert _contenido(_rpc(client, token, "tools/call",
                           {"name": "list_databases", "arguments": {}}))["count"] == 0


def test_an_environment_that_denies_agents_hides_its_databases(client, admin_client, mcp_on):
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    _bd_alcanzable(admin_client, project_id=pid, opt_in=True, env_allows=False)

    assert _contenido(_rpc(client, token, "tools/call",
                           {"name": "list_databases", "arguments": {}}))["count"] == 0


def test_a_database_of_another_project_is_invisible(client, admin_client, mcp_on):
    """
    El alcance por proyecto. **No dice "existe y no te la doy"**: simplemente no está, que es lo
    que evita que el listado sea un oráculo de inventario.
    """
    mio = _proyecto(admin_client, "Mio")
    ajeno = _proyecto(admin_client, "Ajeno")
    token = _crear_token(admin_client, project_id=mio)["token"]
    _bd_alcanzable(admin_client, project_id=ajeno, opt_in=True)

    assert _contenido(_rpc(client, token, "tools/call",
                           {"name": "list_databases", "arguments": {}}))["count"] == 0


# --------------------------------------------------------------------------- #
# El secreto no aparece en ninguna parte                                      #
# --------------------------------------------------------------------------- #


def test_the_secret_is_never_stored(admin_client):
    """Lo que persiste es su HMAC. Un sistema que pueda mostrarlo de nuevo es uno que lo tiene."""
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    secreto = datos["token"].split(".")[-1]

    with Database().engine.begin() as conn:
        filas = conn.execute(text("SELECT secret_hmac FROM api_tokens")).fetchall()
    assert filas
    assert all(secreto not in (f[0] or "") for f in filas)


def test_the_listing_never_returns_the_token(admin_client):
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    secreto = datos["token"].split(".")[-1]

    r = admin_client.get("/api/v1/api-tokens?size=50")
    assert r.status_code == 200, r.text
    assert secreto not in r.text
    assert "secret_hmac" not in r.text


def test_the_audit_records_the_token_id_and_never_the_secret(client, admin_client, mcp_on):
    from app.models.audit_log import AuditLog

    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    secreto = datos["token"].split(".")[-1]
    _rpc(client, datos["token"], "tools/call", {"name": "list_databases", "arguments": {}})

    s = Database().get_declarative_base_session()
    try:
        filas = s.query(AuditLog).filter(AuditLog.action == "mcp.list_databases").all()
        assert filas, "la invocación no dejó rastro"
        fila = filas[-1]
        # La CLASE de actor queda registrada: sin esto, una fila de un agente y una de un humano
        # son indistinguibles salvo por el nombre.
        assert fila.actor_type == "api_token"
        assert fila.api_token_id == datos["id"]
        assert datos["token_id"] in (fila.detail or "")
        assert secreto not in (fila.detail or "")
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# El techo de agente                                                          #
# --------------------------------------------------------------------------- #


def test_a_scope_outside_the_agent_ceiling_is_rejected(admin_client):
    """
    Un token **no puede** recibir una capacidad que mute o divulgue, ni por error del operador.
    Y la intersección se vuelve a aplicar al autenticar, o sea que es fail-closed en el lector
    además del escritor.
    """
    pid = _proyecto(admin_client)
    r = admin_client.post(
        "/api/v1/api-tokens",
        json={"name": "peligroso", "project_id": pid, "scopes": ["databases.drop"]},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "api_token.scope_not_allowed"


def test_a_token_without_a_project_is_rejected(admin_client):
    r = admin_client.post("/api/v1/api-tokens", json={"name": "global"})
    assert r.status_code == 422


def test_a_ttl_over_the_cap_is_rejected(admin_client):
    """Sin tokens perpetuos: vive en un `.mcp.json` del repo de otra gente."""
    pid = _proyecto(admin_client)
    r = admin_client.post(
        "/api/v1/api-tokens",
        json={"name": "eterno", "project_id": pid, "expires_in_days": 3650},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["public_context"]["code"] == "api_token.ttl_too_long"


def test_revoking_twice_is_a_409(admin_client):
    """
    Para que quien lo pide sepa que **no fue su acción** la que cortó el acceso. Y no hay
    reactivar: un token que alguien creyó muerto y no lo está es peor que emitir uno nuevo.
    """
    pid = _proyecto(admin_client)
    datos = _crear_token(admin_client, project_id=pid)
    assert admin_client.delete(f"/api/v1/api-tokens/{datos['id']}").status_code == 200
    r = admin_client.delete(f"/api/v1/api-tokens/{datos['id']}")
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "api_token.already_revoked"


def test_the_tool_result_carries_structured_content_and_text(client, admin_client, mcp_on):
    """
    Las dos formas: el bloque de texto que todo cliente sabe renderizar y el objeto
    estructurado para los que lo soportan. Y **el mismo** contenido en las dos.
    """
    pid = _proyecto(admin_client)
    token = _crear_token(admin_client, project_id=pid)["token"]
    r = _rpc(client, token, "tools/call", {"name": "list_databases", "arguments": {}})
    resultado = r.json()["result"]
    assert resultado["isError"] is False
    assert json.loads(resultado["content"][0]["text"]) == resultado["structuredContent"]
