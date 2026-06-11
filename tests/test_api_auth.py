"""Endpoints de autenticación y protección de rutas."""


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_me_requires_session(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_login_wrong_credentials(client):
    r = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "WRONG"}
    )
    assert r.status_code == 401
    # Mensaje genérico: no revela si el usuario existe.
    assert "inválid" in r.json()["detail"]["msg"].lower()


def test_login_success_sets_session(client):
    r = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"id": 1, "username": "admin"}
    assert "gw_session" in r.cookies


def test_me_with_session(admin_client):
    r = admin_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["data"]["username"] == "admin"


def test_logout_clears_session(admin_client):
    assert admin_client.post("/api/v1/auth/logout").status_code == 200
    # Tras logout, /me vuelve a 401.
    assert admin_client.get("/api/v1/auth/me").status_code == 401


def test_login_validation_error(client):
    r = client.post("/api/v1/auth/login", json={"username": ""})
    assert r.status_code == 422
