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
    # No necesita conectar: la validación del identificador ocurre antes.
    r = admin_client.get(f"/api/v1/servers/{sid}/databases/bad-name/tables")
    assert r.status_code == 422


def test_introspection_requires_existing_server(admin_client):
    assert admin_client.post("/api/v1/servers/12345/test-connection").status_code == 404
