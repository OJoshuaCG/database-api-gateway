"""Endpoints de Servers: CRUD, validación, errores y no-fuga de credenciales."""


def test_servers_requires_auth(client):
    assert client.get("/api/v1/servers").status_code == 401


def test_list_empty(admin_client):
    r = admin_client.get("/api/v1/servers")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0


def test_create_server_hides_password(admin_client, server_payload):
    r = admin_client.post("/api/v1/servers", json=server_payload())
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["name"] == "srv-test"
    assert data["engine"] == "mysql"
    assert data["status"] == "active"
    assert data["has_root_password"] is True
    # El password NO aparece en la respuesta (ni cifrado ni en claro).
    assert "supersecret" not in r.text
    assert "root_password" not in data
    assert "root_password_encrypted" not in data


def test_create_duplicate_host_port_conflict(admin_client, server_payload):
    assert admin_client.post("/api/v1/servers", json=server_payload(name="a")).status_code == 201
    r = admin_client.post(
        "/api/v1/servers", json=server_payload(name="b")  # mismo host:port
    )
    assert r.status_code == 409


def test_create_duplicate_name_conflict(admin_client, server_payload):
    assert admin_client.post("/api/v1/servers", json=server_payload(name="dup", port=3300)).status_code == 201
    r = admin_client.post(
        "/api/v1/servers", json=server_payload(name="dup", port=3301)
    )
    assert r.status_code == 409


def test_create_invalid_engine_422(admin_client, server_payload):
    r = admin_client.post("/api/v1/servers", json=server_payload(engine="oracle"))
    assert r.status_code == 422


def test_get_update_delete_lifecycle(admin_client, server_payload):
    created = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]
    sid = created["id"]

    assert admin_client.get(f"/api/v1/servers/{sid}").status_code == 200

    upd = admin_client.patch(
        f"/api/v1/servers/{sid}", json={"name": "renamed", "notes": "n"}
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["name"] == "renamed"

    assert admin_client.delete(f"/api/v1/servers/{sid}").status_code == 200
    assert admin_client.get(f"/api/v1/servers/{sid}").status_code == 404


def test_get_missing_404(admin_client):
    assert admin_client.get("/api/v1/servers/9999").status_code == 404


def test_test_connection_unreachable_502(admin_client, server_payload):
    sid = admin_client.post(
        "/api/v1/servers", json=server_payload(port=3399)
    ).json()["data"]["id"]
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection")
    assert r.status_code == 502
    # El estado debe quedar 'unreachable'.
    assert admin_client.get(f"/api/v1/servers/{sid}").json()["data"]["status"] == "unreachable"


def test_introspection_invalid_identifier_422(admin_client, server_payload):
    sid = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    # Nombre con caracteres peligrosos (`;`): rechazado antes de conectar, incluso con
    # la whitelist ampliada de introspección. (Un nombre legado como `bad-name` SÍ es
    # válido ahora — deuda #3 — y procedería a conectar.)
    r = admin_client.get(f"/api/v1/servers/{sid}/databases/bad;name/tables")
    assert r.status_code == 422


def test_introspection_requires_existing_server(admin_client):
    assert admin_client.post("/api/v1/servers/12345/test-connection").status_code == 404


# --------------------------- TLS por servidor (ssl_mode) --------------------- #
def test_ssl_mode_default_is_none(admin_client, server_payload):
    data = admin_client.post("/api/v1/servers", json=server_payload(port=3500)).json()["data"]
    assert data["ssl_mode"] is None  # sin TLS si no se especifica


def test_ssl_mode_persisted_and_normalized(admin_client, server_payload):
    data = admin_client.post(
        "/api/v1/servers", json=server_payload(port=3501, ssl_mode="REQUIRE")
    ).json()["data"]
    assert data["ssl_mode"] == "require"  # normalizado a minúsculas


def test_ssl_mode_invalid_422(admin_client, server_payload):
    r = admin_client.post(
        "/api/v1/servers", json=server_payload(port=3502, ssl_mode="bogus")
    )
    assert r.status_code == 422


def test_ssl_mode_update(admin_client, server_payload):
    sid = admin_client.post("/api/v1/servers", json=server_payload(port=3503)).json()["data"]["id"]
    upd = admin_client.patch(f"/api/v1/servers/{sid}", json={"ssl_mode": "verify-full"})
    assert upd.status_code == 200
    assert upd.json()["data"]["ssl_mode"] == "verify-full"


# --------------------------------------------------------------------------- #
# F-20: re-apuntar un servidor exige volver a enviar la credencial               #
# --------------------------------------------------------------------------- #
_REBIND_CODE = "server.credential_required_for_rebind"


def _pc(r) -> dict:
    return (r.json().get("detail") or {}).get("public_context") or {}


def _new_server(admin_client, server_payload, **ov) -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(**ov))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_repointing_without_password_is_rejected(admin_client, server_payload):
    """
    REGRESIÓN F-20: el ``root_password_encrypted`` sobrevivía al cambio de host, y la próxima
    operación mandaba la credencial pseudo-root al host nuevo (que puede pedirla en claro).
    """
    sid = _new_server(admin_client, server_payload, port=3601)
    for body, field in (
        ({"host": "10.0.0.9"}, "host"),
        ({"port": 3602}, "port"),
        ({"engine": "postgresql"}, "engine"),
    ):
        r = admin_client.patch(f"/api/v1/servers/{sid}", json=body)
        assert r.status_code == 422, (body, r.text)
        assert _pc(r)["code"] == _REBIND_CODE
        assert _pc(r)["fields"] == [field]
    # Nada cambió.
    data = admin_client.get(f"/api/v1/servers/{sid}").json()["data"]
    assert (data["host"], data["port"], data["engine"]) == ("127.0.0.1", 3601, "mysql")


def test_repointing_with_a_new_password_is_allowed(admin_client, server_payload):
    sid = _new_server(admin_client, server_payload, port=3603)
    r = admin_client.patch(
        f"/api/v1/servers/{sid}", json={"host": "10.0.0.9", "root_password": "nueva-clave"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["host"] == "10.0.0.9"


def test_sending_the_same_connection_values_is_not_a_rebind(admin_client, server_payload):
    sid = _new_server(admin_client, server_payload, port=3604)
    r = admin_client.patch(
        f"/api/v1/servers/{sid}",
        json={"host": "127.0.0.1", "port": 3604, "engine": "mysql", "notes": "x"},
    )
    assert r.status_code == 200, r.text


def test_weakening_tls_without_password_is_rejected(admin_client, server_payload):
    sid = _new_server(admin_client, server_payload, port=3605, ssl_mode="verify-full")
    for weaker in ("verify-ca", "require", "prefer", "disable", None):
        r = admin_client.patch(f"/api/v1/servers/{sid}", json={"ssl_mode": weaker})
        assert r.status_code == 422, (weaker, r.text)
        assert _pc(r)["code"] == _REBIND_CODE
        assert _pc(r)["fields"] == ["ssl_mode"]
    r = admin_client.patch(
        f"/api/v1/servers/{sid}", json={"ssl_mode": "disable", "root_password": "nueva-clave"}
    )
    assert r.status_code == 200, r.text


def test_tls_changes_that_do_not_weaken_a_required_mode_are_allowed(admin_client, server_payload):
    # Endurecer siempre se permite.
    sid = _new_server(admin_client, server_payload, port=3606, ssl_mode="require")
    assert admin_client.patch(
        f"/api/v1/servers/{sid}", json={"ssl_mode": "verify-full"}
    ).status_code == 200
    # Un modo que nunca exigió TLS (prefer) puede bajar sin re-enviar la credencial.
    sid2 = _new_server(admin_client, server_payload, port=3607, ssl_mode="prefer", name="srv-2")
    assert admin_client.patch(
        f"/api/v1/servers/{sid2}", json={"ssl_mode": "disable"}
    ).status_code == 200
