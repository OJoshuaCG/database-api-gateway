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


def test_template_demo_routes_are_not_mounted(client):
    """
    Los ``/api/v1/test/*`` del template NO están montados.

    Eran 7 rutas sin ``AdminDep`` en un gateway con credenciales pseudo-root, y dos de ellas
    (``POST /test/upload`` y ``/upload/multiple``) escribían archivos a disco sin
    autenticación. ``api-reference.md`` ya los declaraba fuera de la API funcional.
    """
    for method, path in (
        ("get", "/api/v1/test/ping"),
        ("get", "/api/v1/test/paginated"),
        ("delete", "/api/v1/test/resource/1"),
        ("post", "/api/v1/test/upload"),
        ("post", "/api/v1/test/upload/multiple"),
    ):
        r = getattr(client, method)(path)
        assert r.status_code == 404, f"{method.upper()} {path} sigue montado: {r.status_code}"
