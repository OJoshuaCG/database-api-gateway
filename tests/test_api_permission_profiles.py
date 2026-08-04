"""
Tests del CRUD de perfiles de permisos (/permission-profiles).

Cubre: validación de items contra el catálogo del motor (privilegio inválido, nivel no
soportado, admin/DENY), GATE → requires_confirmation, unicidad por motor, y CRUD.
"""


def _profile(name="rw", engine="mysql", items=None, description=None):
    return {
        "name": name,
        "engine": engine,
        "description": description,
        "items": items
        or [
            {"level": "table", "privileges": ["SELECT", "INSERT", "UPDATE"]},
            {"level": "database", "privileges": ["CREATE"]},
        ],
    }


def test_requires_auth(client):
    assert client.get("/api/v1/permission-profiles").status_code in (401, 403)


def test_create_and_get(admin_client):
    r = admin_client.post("/api/v1/permission-profiles", json=_profile())
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["engine"] == "mysql"
    levels = {it["level"]: it for it in data["items"]}
    assert set(levels["table"]["privileges"]) == {"SELECT", "INSERT", "UPDATE"}
    # GET por id
    got = admin_client.get(f"/api/v1/permission-profiles/{data['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["name"] == "rw"


def test_postgres_profile_levels(admin_client):
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(
            name="pg-ro",
            engine="postgresql",
            items=[
                {"level": "table", "privileges": ["SELECT", "TRUNCATE"]},
                {"level": "schema", "privileges": ["USAGE"]},
            ],
        ),
    )
    assert r.status_code == 201, r.text


def test_invalid_privilege_for_engine_422(admin_client):
    # TRUNCATE no existe a nivel tabla en MySQL.
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(name="bad", items=[{"level": "table", "privileges": ["TRUNCATE"]}]),
    )
    assert r.status_code == 422


def test_unsupported_level_for_engine_422(admin_client):
    # SCHEMA no es un nivel soportado en MySQL.
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(name="bad2", items=[{"level": "schema", "privileges": ["USAGE"]}]),
    )
    assert r.status_code == 422


def test_admin_privilege_denied_422(admin_client):
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(name="bad3", items=[{"level": "database", "privileges": ["SUPER"]}]),
    )
    assert r.status_code == 422


def test_gate_privilege_flags_confirmation(admin_client):
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(
            name="rw-grant",
            items=[{"level": "table", "privileges": ["SELECT", "GRANT OPTION"]}],
        ),
    )
    assert r.status_code == 201, r.text
    item = r.json()["data"]["items"][0]
    assert item["requires_confirmation"] is True


def test_duplicate_name_per_engine_conflict(admin_client):
    assert admin_client.post("/api/v1/permission-profiles", json=_profile(name="dup")).status_code == 201
    assert admin_client.post("/api/v1/permission-profiles", json=_profile(name="dup")).status_code == 409
    # Mismo nombre pero otro motor: permitido.
    ok = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(name="dup", engine="postgresql",
                      items=[{"level": "table", "privileges": ["SELECT"]}]),
    )
    assert ok.status_code == 201


def test_list_filter_by_engine(admin_client):
    admin_client.post("/api/v1/permission-profiles", json=_profile(name="m1"))
    admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(name="p1", engine="postgresql",
                      items=[{"level": "table", "privileges": ["SELECT"]}]),
    )
    data = admin_client.get("/api/v1/permission-profiles", params={"engine": "postgresql"}).json()["data"]
    assert all(p["engine"] == "postgresql" for p in data)
    assert "p1" in {p["name"] for p in data}


def test_update_replaces_items(admin_client):
    pid = admin_client.post("/api/v1/permission-profiles", json=_profile(name="upd")).json()["data"]["id"]
    r = admin_client.patch(
        f"/api/v1/permission-profiles/{pid}",
        json={"description": "solo lectura", "items": [{"level": "table", "privileges": ["SELECT"]}]},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["description"] == "solo lectura"
    assert len(data["items"]) == 1
    assert data["items"][0]["privileges"] == ["SELECT"]


def test_update_invalid_items_rejected(admin_client):
    pid = admin_client.post("/api/v1/permission-profiles", json=_profile(name="upd2")).json()["data"]["id"]
    r = admin_client.patch(
        f"/api/v1/permission-profiles/{pid}",
        json={"items": [{"level": "table", "privileges": ["BOGUS"]}]},
    )
    assert r.status_code == 422


def test_delete_profile(admin_client):
    pid = admin_client.post("/api/v1/permission-profiles", json=_profile(name="del")).json()["data"]["id"]
    assert admin_client.delete(f"/api/v1/permission-profiles/{pid}").status_code == 200
    assert admin_client.get(f"/api/v1/permission-profiles/{pid}").status_code == 404


# --------------------------------------------------------------------------- #
# Compatibilidad de familia MySQL↔MariaDB en el filtro de motor.
# Regresión: un perfil `mariadb` no aparecía al pedir `?engine=mysql`, así que el
# selector de «Aplicar perfil» del frontend salía vacío sin explicación.
# --------------------------------------------------------------------------- #


def test_list_includes_compatible_family_profile(admin_client):
    """Un perfil mariadb sin privilegios exclusivos SÍ aparece al filtrar por mysql."""
    admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(
            name="maria-rw",
            engine="mariadb",
            items=[{"level": "table", "privileges": ["SELECT", "INSERT"]}],
        ),
    )
    data = admin_client.get(
        "/api/v1/permission-profiles", params={"engine": "mysql", "active": True}
    ).json()["data"]
    names = {p["name"] for p in data}
    assert "maria-rw" in names
    # El motor real del perfil sigue viajando en la respuesta (la UI puede etiquetarlo).
    assert next(p for p in data if p["name"] == "maria-rw")["engine"] == "mariadb"


def test_list_excludes_family_profile_with_exclusive_privilege(admin_client):
    """DELETE HISTORY es exclusivo de MariaDB: ese perfil NO debe ofrecerse para MySQL."""
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(
            name="maria-hist",
            engine="mariadb",
            items=[{"level": "table", "privileges": ["SELECT", "DELETE HISTORY"]}],
        ),
    )
    assert r.status_code == 201, r.text
    for_mysql = admin_client.get(
        "/api/v1/permission-profiles", params={"engine": "mysql"}
    ).json()["data"]
    assert "maria-hist" not in {p["name"] for p in for_mysql}
    # Pero sí para su propio motor.
    for_maria = admin_client.get(
        "/api/v1/permission-profiles", params={"engine": "mariadb"}
    ).json()["data"]
    assert "maria-hist" in {p["name"] for p in for_maria}


def test_exact_engine_restores_strict_filter(admin_client):
    admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(
            name="maria-strict",
            engine="mariadb",
            items=[{"level": "table", "privileges": ["SELECT"]}],
        ),
    )
    data = admin_client.get(
        "/api/v1/permission-profiles",
        params={"engine": "mysql", "exact_engine": True},
    ).json()["data"]
    assert all(p["engine"] == "mysql" for p in data)
    assert "maria-strict" not in {p["name"] for p in data}


def test_postgresql_never_mixes_with_mysql_family(admin_client):
    """PostgreSQL no tiene familia: nunca debe aparecer al pedir mysql, ni al revés."""
    admin_client.post("/api/v1/permission-profiles", json=_profile(name="mix-my"))
    admin_client.post(
        "/api/v1/permission-profiles",
        json=_profile(
            name="mix-pg",
            engine="postgresql",
            items=[{"level": "table", "privileges": ["SELECT"]}],
        ),
    )
    for_pg = admin_client.get(
        "/api/v1/permission-profiles", params={"engine": "postgresql"}
    ).json()["data"]
    assert all(p["engine"] == "postgresql" for p in for_pg)
    for_my = admin_client.get(
        "/api/v1/permission-profiles", params={"engine": "mysql"}
    ).json()["data"]
    assert "mix-pg" not in {p["name"] for p in for_my}


# --------------------------------------------------------------------------- #
# is_active en la creación (antes solo se podía cambiar por PATCH).
# --------------------------------------------------------------------------- #


def test_create_defaults_to_active(admin_client):
    r = admin_client.post("/api/v1/permission-profiles", json=_profile(name="act-default"))
    assert r.status_code == 201, r.text
    assert r.json()["data"]["is_active"] is True


def test_create_can_start_inactive(admin_client):
    payload = _profile(name="act-off")
    payload["is_active"] = False
    r = admin_client.post("/api/v1/permission-profiles", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["is_active"] is False
    # Y no debe salir en el listado de activos.
    active = admin_client.get(
        "/api/v1/permission-profiles", params={"active": True}
    ).json()["data"]
    assert "act-off" not in {p["name"] for p in active}
