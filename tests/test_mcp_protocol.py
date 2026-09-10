"""
Cumplimiento del protocolo MCP: las DOS eras, y lo que un cliente real rompe.

POR QUÉ ESTE ARCHIVO EXISTE, Y POR QUÉ NO ALCANZABA `test_mcp_server.py`
------------------------------------------------------------------------
Los tests de `test_mcp_server.py` verifican la LÓGICA (el gate, el token, las tools). Este verifica
el **contrato del transporte**, que es otra cosa y es donde un cliente real se rompe: status HTTP,
headers obligatorios, forma del `result`, y qué se responde a una notificación.

La primera versión de este servidor pasaba todos los tests de lógica y **no conectaba con un
cliente moderno**, por cinco motivos que ninguno de esos tests podía ver:

1. Contestaba un `-32601` a `notifications/initialized` — JSON-RPC prohíbe responder una
   notificación, y todo cliente de la era del handshake la manda como segundo mensaje.
2. No implementaba `ping`.
3. Devolvía `200` a todo, cuando el status **es parte del contrato**: `202` para una
   notificación, `404` para un método desconocido, `400` para una validación.
4. Le faltaba `resultType` en el `result`, que la spec marca como MUST.
5. Hablaba solo la era del handshake, y la revisión `2026-07-28` **retiró el `initialize`**.

Lo que sigue afuera de acá: probar el transporte con un proceso HTTP real. `TestClient` corre la
app ASGI en proceso, así que no ejercita redirecciones ni el comportamiento de un proxy. Esa
verificación se hizo con un uvicorn de verdad y **no está automatizada**.
"""

import json

import pytest

V_STATELESS = "2026-07-28"
V_HANDSHAKE = "2025-06-18"

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"


@pytest.fixture()
def mcp_on(monkeypatch):
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", True)


@pytest.fixture()
def bearer(admin_client, mcp_on):
    pid = admin_client.post("/api/v1/projects", json={"name": "MCP"}).json()["data"]["id"]
    r = admin_client.post("/api/v1/api-tokens", json={"name": "sonda", "project_id": pid})
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def _moderno(client, bearer, metodo, params=None, rid=1, headers=None, sin_meta=()):
    """Request de la era STATELESS: headers obligatorios y `_meta` en params."""
    params = dict(params or {})
    meta = {META_VERSION: V_STATELESS, META_CAPS: {}, META_INFO: {"name": "t", "version": "1"}}
    for k in sin_meta:
        meta.pop(k, None)
    params["_meta"] = meta
    cuerpo = {"jsonrpc": "2.0", "method": metodo, "params": params}
    if rid is not None:
        cuerpo["id"] = rid
    h = {
        "Authorization": f"Bearer {bearer}",
        "MCP-Protocol-Version": V_STATELESS,
        "Mcp-Method": metodo,
        "Accept": "application/json, text/event-stream",
    }
    if metodo == "tools/call" and isinstance(params.get("name"), str):
        h["Mcp-Name"] = params["name"]
    h.update(headers or {})
    return client.post("/mcp/", json=cuerpo, headers=h)


def _legado(client, bearer, metodo, params=None, rid=1):
    """Request de la era del HANDSHAKE: sin headers de metadatos."""
    cuerpo = {"jsonrpc": "2.0", "method": metodo}
    if rid is not None:
        cuerpo["id"] = rid
    if params is not None:
        cuerpo["params"] = params
    return client.post("/mcp/", json=cuerpo, headers={"Authorization": f"Bearer {bearer}"})


# --------------------------------------------------------------------------- #
# Notificaciones: el defecto que rompía el handshake                          #
# --------------------------------------------------------------------------- #


def test_a_notification_gets_202_with_no_body(client, bearer):
    """
    **El defecto más visible que tenía este servidor.** JSON-RPC define una notificación como un
    request SIN ``id`` y prohíbe responderla; sobre HTTP la spec pide ``202`` sin cuerpo.

    Todo cliente de la era del handshake manda ``notifications/initialized`` como **segundo
    mensaje**, y recibía un ``-32601 Método desconocido`` con ``id: null``. Un cliente estricto
    lo lee como servidor roto en el primer intercambio.
    """
    r = _legado(client, bearer, "notifications/initialized", rid=None)
    assert r.status_code == 202, r.text
    assert not r.content, r.text


def test_an_unknown_notification_is_also_accepted_silently(client, bearer):
    """
    Vale para TODA notificación, conocida o no. Las que vengan en versiones futuras del
    protocolo tienen que poder ignorarse en silencio, que es lo que la spec pide de lo que no
    se soporta — no un error que el cliente interprete como falla.
    """
    r = _moderno(client, bearer, "notifications/algo/que/no/existe", rid=None)
    assert r.status_code == 202
    assert not r.content


def test_a_null_id_is_rejected(client, bearer):
    """MCP prohíbe ``id: null``, a diferencia de JSON-RPC pelado."""
    r = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": None, "method": "ping"},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------- #
# El status HTTP es parte del contrato                                        #
# --------------------------------------------------------------------------- #


def test_an_unknown_method_is_404_with_the_json_rpc_error(client, bearer):
    """
    ``404`` **y** un cuerpo con ``-32601``. Las dos cosas: el cuerpo es lo que le permite a un
    cliente moderno distinguir este caso del ``404`` de un servidor legado que no hospeda el
    endpoint MCP — sin él, caería al transporte deprecado.
    """
    r = _moderno(client, bearer, "resources/list")
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == -32601


def test_ping_is_implemented(client, bearer):
    """
    Utilidad del protocolo que los clientes usan como keepalive. Sin ella, el ``-32601`` le dice
    al cliente que la conexión está rota.
    """
    r = _moderno(client, bearer, "ping")
    assert r.status_code == 200, r.text
    assert "result" in r.json()


def test_a_malformed_body_is_400_and_parse_error(client, bearer):
    r = client.post(
        "/mcp/", content=b"{no es json", headers={"Authorization": f"Bearer {bearer}"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


# --------------------------------------------------------------------------- #
# La forma del result                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("metodo", ["ping", "tools/list"])
def test_every_result_carries_result_type(client, bearer, metodo):
    """
    ``resultType`` es un **MUST** de la spec. Su ausencia un cliente la trata como
    ``"complete"`` solo por compatibilidad con versiones viejas; se declara explícito.
    """
    r = _moderno(client, bearer, metodo)
    assert r.json()["result"]["resultType"] == "complete", r.text


def test_every_result_carries_server_info(client, bearer):
    """
    En un protocolo **sin handshake**, ``_meta.serverInfo`` por request es la única vía por la
    que el cliente sabe con qué está hablando.
    """
    r = _moderno(client, bearer, "ping")
    assert META_SERVER in r.json()["result"]["_meta"], r.text


def test_a_tool_call_carries_content_and_is_error(client, bearer):
    r = _moderno(client, bearer, "tools/call", {"name": "list_databases", "arguments": {}})
    res = r.json()["result"]
    assert res["resultType"] == "complete"
    assert isinstance(res["content"], list) and res["content"][0]["type"] == "text"
    assert res["isError"] is False
    # La spec pide que una tool con contenido estructurado devuelva IGUAL el JSON serializado en
    # un bloque de texto, para los clientes que no soportan lo estructurado.
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]


# --------------------------------------------------------------------------- #
# Era stateless: los headers obligatorios y su calce con el cuerpo            #
# --------------------------------------------------------------------------- #


def test_an_unsupported_version_lists_the_supported_ones(client, bearer):
    """
    ``-32022`` con la lista en ``data.supported``: es lo que le permite al cliente reintentar
    con una versión que sí hablamos, en vez de caer al ``initialize`` de la era vieja.
    """
    r = _moderno(client, bearer, "ping", headers={"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["code"] == -32022
    assert err["data"]["supported"]


def test_a_header_that_does_not_match_the_body_is_rejected(client, bearer):
    """
    **El motivo es de seguridad, no de prolijidad.** Un balanceador puede rutear por el header
    mientras el servidor ejecuta por el cuerpo, y esa discrepancia es explotable — de ahí que la
    spec pida validar el calce y reserve un código propio (``-32020``) para el fallo.
    """
    r = _moderno(client, bearer, "ping", headers={"Mcp-Method": "tools/list"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == -32020


def test_the_version_header_must_match_the_meta(client, bearer):
    cuerpo = {
        "jsonrpc": "2.0", "id": 1, "method": "ping",
        "params": {"_meta": {META_VERSION: V_HANDSHAKE, META_CAPS: {}}},
    }
    r = client.post(
        "/mcp/", json=cuerpo,
        headers={"Authorization": f"Bearer {bearer}",
                 "MCP-Protocol-Version": V_STATELESS, "Mcp-Method": "ping"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_the_name_header_must_match_the_body(client, bearer):
    r = _moderno(client, bearer, "tools/call", {"name": "list_databases", "arguments": {}},
                 headers={"Mcp-Name": "otra"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_missing_client_capabilities_is_rejected(client, bearer):
    """
    ``clientCapabilities`` puede ser un objeto vacío pero tiene que **estar**: un servidor no
    puede apoyarse en una capacidad que el cliente no declaró, así que la ausencia de la
    declaración no es lo mismo que una declaración vacía.
    """
    r = _moderno(client, bearer, "ping", sin_meta=(META_CAPS,))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == -32602


def test_initialize_does_not_exist_in_the_stateless_era(client, bearer):
    """
    Aceptarlo ahí sería anunciar un handshake que el protocolo **retiró**, y un cliente moderno
    que por error lo llame tiene que enterarse.
    """
    r = _moderno(client, bearer, "initialize")
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == -32601


# --------------------------------------------------------------------------- #
# Era del handshake: la compatibilidad hacia atrás                            #
# --------------------------------------------------------------------------- #


def test_the_handshake_era_still_works(client, bearer):
    """
    Se soportan las DOS eras porque el parque real tiene las dos, y porque la propia spec
    describe la ruta de compatibilidad: un cliente moderno prueba lo moderno primero y cae al
    ``initialize`` si el ``400`` no trae un error moderno reconocible.
    """
    r = _legado(client, bearer, "initialize", {"protocolVersion": V_HANDSHAKE})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    # Negocia: devuelve la versión que el cliente pidió, no una fija.
    assert res["protocolVersion"] == V_HANDSHAKE
    assert res["capabilities"] == {"tools": {}}
    assert res["serverInfo"]["name"]


def test_the_handshake_era_can_call_tools_without_meta(client, bearer):
    r = _legado(client, bearer, "tools/call", {"name": "list_databases", "arguments": {}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["isError"] is False


# --------------------------------------------------------------------------- #
# Robustez: "nunca levanta" tiene que ser verdad                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("params", ["cadena", 7, ["x"], {"name": ["lista"]}, {"_meta": "no-dict"}])
def test_a_malformed_params_never_produces_a_500(client, bearer, params):
    """
    El docstring del dispatch promete "nunca levanta" y **no era cierto**: un ``params`` truthy
    que no fuera dict pasaba el ``or {}`` y reventaba en el ``.get()``; y un ``params.name`` no
    hasheable reventaba en el lookup del registro. Los dos daban ``500`` — y con
    ``APP_ENV=development`` el handler genérico devolvía **archivo, función, línea y la línea de
    código** al agente, o sea al contexto de un modelo.

    El arreglo tuvo que ir en DOS lados: el validador del protocolo corre antes del dispatch, así
    que arreglar solo uno movía el 500 de lugar. Lo detectó una sonda en vivo, no un test.
    """
    cuerpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    r = client.post(
        "/mcp/", json=cuerpo,
        headers={"Authorization": f"Bearer {bearer}",
                 "MCP-Protocol-Version": V_STATELESS, "Mcp-Method": "tools/call",
                 "Mcp-Name": "x"},
    )
    assert r.status_code != 500, r.text


def test_a_batch_is_rejected_with_400(client, bearer):
    """
    La spec de esta revisión dice que el cuerpo es **un** request o notificación. Y un batch
    multiplicaría el presupuesto de bytes por N sin que el tope lo vea.
    """
    r = client.post(
        "/mcp/", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600


# --------------------------------------------------------------------------- #
# Origin: DNS rebinding                                                       #
# --------------------------------------------------------------------------- #


def test_a_foreign_origin_is_403(client, bearer):
    """
    La spec lo pone como **MUST** y el ataque es concreto: sin esto, una página cualquiera puede
    hacer *DNS rebinding* contra un servidor MCP que corre en la máquina del operador y hablarle
    como si fuera local.
    """
    r = _moderno(client, bearer, "ping", headers={"Origin": "https://atacante.example"})
    assert r.status_code == 403, r.text


def test_a_missing_origin_does_not_reject(client, bearer):
    """
    **Ausente no rechaza**, y es deliberado: un cliente que no es un navegador no manda `Origin`
    y no está sujeto al ataque. Rechazar por ausencia rompería a `curl`, al CI y a cualquier
    script sin ganar nada.
    """
    r = _moderno(client, bearer, "ping")
    assert r.status_code == 200, r.text


def test_the_session_header_is_ignored_and_never_emitted(client, bearer):
    """
    ``Mcp-Session-Id`` se **retiró** en esta revisión. Un servidor que lo implemente tiene que
    ignorarlo al recibirlo y no emitirlo — no es opcional-pero-bueno: emitirlo le diría a un
    cliente moderno que este servidor habla una revisión que no habla.
    """
    r = _moderno(client, bearer, "ping", headers={"Mcp-Session-Id": "inventado"})
    assert r.status_code == 200, r.text
    assert "mcp-session-id" not in {k.lower() for k in r.headers}
