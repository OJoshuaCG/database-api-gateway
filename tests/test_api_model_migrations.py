"""Endpoints de migraciones de blueprints: CRUD, checksum, traducción, rollback sugerido."""


def _new_model(admin_client, slug="whatsapp", name="Whatsapp") -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_migration(admin_client, model_id, **overrides):
    payload = {
        "version": "0001",
        "name": "Esquema inicial",
        "up_sql": "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100))",
    }
    payload.update(overrides)
    return admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations", json=payload
    )


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #
def test_requires_auth(client):
    assert client.get("/api/v1/database-models/1/migrations").status_code == 401


# --------------------------------------------------------------------------- #
# Create                                                                       #
# --------------------------------------------------------------------------- #
def test_create_returns_translation_and_suggested_rollback(admin_client):
    model_id = _new_model(admin_client)
    r = _create_migration(admin_client, model_id)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["version"] == "0001"
    # Traducción cross-engine calculada.
    assert "mysql" in data["translated"] and "postgresql" in data["translated"]
    assert "AUTO_INCREMENT" not in data["translated"]["postgresql"]
    # Rollback sugerido (aditivo) pero NO confirmado.
    assert data["down_sql_suggested"] == "DROP TABLE IF EXISTS users;"
    assert data["down_sql"] is None
    assert len(data["checksum"]) == 64


def test_create_bumps_model_current_version(admin_client):
    model_id = _new_model(admin_client, slug="sms", name="SMS")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(admin_client, model_id, version="0002",
                      up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)")
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0002"


def test_create_with_explicit_down_sql(admin_client):
    model_id = _new_model(admin_client, slug="logistica", name="Logistica")
    r = _create_migration(admin_client, model_id, down_sql="DROP TABLE users")
    data = r.json()["data"]
    assert data["down_sql"] == "DROP TABLE users"


def test_create_non_additive_has_no_suggested_rollback(admin_client):
    model_id = _new_model(admin_client, slug="ventas", name="Ventas")
    r = _create_migration(
        admin_client, model_id,
        up_sql="INSERT INTO config (k, v) VALUES ('a', 'b')",
    )
    assert r.json()["data"]["down_sql_suggested"] is None


def test_duplicate_version_conflict(admin_client):
    model_id = _new_model(admin_client, slug="dup", name="Dup")
    assert _create_migration(admin_client, model_id).status_code == 201
    assert _create_migration(admin_client, model_id).status_code == 409


def test_invalid_version_pattern_422(admin_client):
    model_id = _new_model(admin_client, slug="badver", name="BadVer")
    assert _create_migration(admin_client, model_id, version="1.2.0").status_code == 422
    assert _create_migration(admin_client, model_id, version="abc").status_code == 422


def test_create_on_missing_model_404(admin_client):
    assert _create_migration(admin_client, 9999).status_code == 404


# --------------------------------------------------------------------------- #
# List / Get                                                                   #
# --------------------------------------------------------------------------- #
def test_list_and_get(admin_client):
    model_id = _new_model(admin_client, slug="listme", name="ListMe")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(admin_client, model_id, version="0002",
                      up_sql="ALTER TABLE users ADD COLUMN x INT")

    lst = admin_client.get(f"/api/v1/database-models/{model_id}/migrations")
    assert lst.status_code == 200
    items = lst.json()["data"]
    assert [i["version"] for i in items] == ["0001", "0002"]
    assert items[0]["has_rollback"] is False

    detail = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert detail.status_code == 200
    assert detail.json()["data"]["up_sql"].startswith("ALTER TABLE")


def test_get_missing_version_404(admin_client):
    model_id = _new_model(admin_client, slug="missing", name="Missing")
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0009"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Patch                                                                        #
# --------------------------------------------------------------------------- #
def test_patch_confirms_rollback(admin_client):
    model_id = _new_model(admin_client, slug="patchrb", name="PatchRb")
    _create_migration(admin_client, model_id)
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"down_sql": "DROP TABLE IF EXISTS users"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["down_sql"] == "DROP TABLE IF EXISTS users"


def test_patch_override_changes_checksum_and_translation(admin_client):
    model_id = _new_model(admin_client, slug="override", name="Override")
    before = _create_migration(admin_client, model_id).json()["data"]
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql_postgresql": "CREATE TABLE users (id SERIAL PRIMARY KEY)"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["checksum"] != before["checksum"]
    assert data["translated"]["postgresql"] == "CREATE TABLE users (id SERIAL PRIMARY KEY)"


# --------------------------------------------------------------------------- #
# Delete                                                                       #
# --------------------------------------------------------------------------- #
def test_delete_migration(admin_client):
    model_id = _new_model(admin_client, slug="delme", name="DelMe")
    _create_migration(admin_client, model_id)
    assert admin_client.delete(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 200
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 404
