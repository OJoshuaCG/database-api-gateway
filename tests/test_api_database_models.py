"""Endpoints de DatabaseModels (blueprints): CRUD, unicidad, validación, auth."""


def test_requires_auth(client):
    assert client.get("/api/v1/database-models").status_code == 401


def test_create_and_get(admin_client):
    r = admin_client.post(
        "/api/v1/database-models",
        json={"name": "Whatsapp", "slug": "whatsapp", "description": "blueprint WA"},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["slug"] == "whatsapp"
    assert data["current_version"] == "0.0.0"
    assert data["is_active"] is True
    assert admin_client.get(f"/api/v1/database-models/{data['id']}").status_code == 200


def test_duplicate_slug_conflict(admin_client):
    assert admin_client.post(
        "/api/v1/database-models", json={"name": "SMS", "slug": "sms"}
    ).status_code == 201
    r = admin_client.post(
        "/api/v1/database-models", json={"name": "SMS 2", "slug": "sms"}
    )
    assert r.status_code == 409


def test_invalid_slug_422(admin_client):
    r = admin_client.post(
        "/api/v1/database-models", json={"name": "Bad", "slug": "Bad Slug!"}
    )
    assert r.status_code == 422


def test_update_and_delete(admin_client):
    mid = admin_client.post(
        "/api/v1/database-models", json={"name": "Llamadas", "slug": "llamadas"}
    ).json()["data"]["id"]
    upd = admin_client.patch(
        f"/api/v1/database-models/{mid}", json={"current_version": "1.2.0"}
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["current_version"] == "1.2.0"
    assert admin_client.delete(f"/api/v1/database-models/{mid}").status_code == 200
    assert admin_client.get(f"/api/v1/database-models/{mid}").status_code == 404


def test_get_missing_404(admin_client):
    assert admin_client.get("/api/v1/database-models/9999").status_code == 404
