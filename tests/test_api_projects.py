"""
Endpoints de Projects: CRUD, vínculos N:M con blueprints, auth.

El test que NO puede faltar es ``test_delete_project_keeps_blueprints``: la regla dura del
módulo es que borrar un agrupador no borra los esquemas agrupados. Si alguna vez ese test
falla, hay pérdida de datos, no un detalle de API.
"""

from app.schemas.project import DESCRIPTION_MAX_LENGTH


def _blueprint(admin_client, name: str, slug: str) -> int:
    r = admin_client.post(
        "/api/v1/database-models", json={"name": name, "slug": slug}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _project(admin_client, name: str, **extra) -> int:
    r = admin_client.post("/api/v1/projects", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# ─── auth ─────────────────────────────────────────────────────────────────── #


def test_requires_auth(client):
    assert client.get("/api/v1/projects").status_code == 401


# ─── CRUD ─────────────────────────────────────────────────────────────────── #


def test_create_and_get(admin_client):
    r = admin_client.post(
        "/api/v1/projects", json={"name": "Citas", "description": "agenda de citas"}
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["name"] == "Citas"
    assert data["description"] == "agenda de citas"
    assert data["blueprint_count"] == 0
    got = admin_client.get(f"/api/v1/projects/{data['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["name"] == "Citas"


def test_create_without_description(admin_client):
    data = admin_client.post("/api/v1/projects", json={"name": "Vacio"}).json()["data"]
    assert data["description"] is None


def test_duplicate_name_conflict(admin_client):
    _project(admin_client, "Omnicanal")
    r = admin_client.post("/api/v1/projects", json={"name": "Omnicanal"})
    assert r.status_code == 409


def test_name_is_trimmed_and_blank_rejected(admin_client):
    data = admin_client.post("/api/v1/projects", json={"name": "  Citas  "}).json()["data"]
    assert data["name"] == "Citas"
    assert admin_client.post("/api/v1/projects", json={"name": "   "}).status_code == 422


def test_description_limit(admin_client):
    ok = admin_client.post(
        "/api/v1/projects",
        json={"name": "Largo", "description": "x" * DESCRIPTION_MAX_LENGTH},
    )
    assert ok.status_code == 201, ok.text
    assert len(ok.json()["data"]["description"]) == DESCRIPTION_MAX_LENGTH
    too_long = admin_client.post(
        "/api/v1/projects",
        json={"name": "Largo 2", "description": "x" * (DESCRIPTION_MAX_LENGTH + 1)},
    )
    assert too_long.status_code == 422


def test_update_name_and_description(admin_client):
    pid = _project(admin_client, "Nombre viejo", description="desc vieja")
    r = admin_client.patch(
        f"/api/v1/projects/{pid}",
        json={"name": "Nombre nuevo", "description": "desc nueva"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["name"] == "Nombre nuevo"
    assert r.json()["data"]["description"] == "desc nueva"


def test_update_can_clear_description_but_empty_patch_changes_nothing(admin_client):
    pid = _project(admin_client, "Con desc", description="algo")
    # PATCH vacío: no toca nada (exclude_unset).
    assert admin_client.patch(f"/api/v1/projects/{pid}", json={}).json()["data"][
        "description"
    ] == "algo"
    # description: null SÍ limpia — es la única forma de distinguirlo de "no enviado".
    cleared = admin_client.patch(f"/api/v1/projects/{pid}", json={"description": None})
    assert cleared.json()["data"]["description"] is None


def test_update_duplicate_name_conflict(admin_client):
    _project(admin_client, "Ocupado")
    pid = _project(admin_client, "Libre")
    assert admin_client.patch(
        f"/api/v1/projects/{pid}", json={"name": "Ocupado"}
    ).status_code == 409


def test_get_missing_404(admin_client):
    assert admin_client.get("/api/v1/projects/9999").status_code == 404
    assert admin_client.patch("/api/v1/projects/9999", json={"name": "x"}).status_code == 404
    assert admin_client.delete("/api/v1/projects/9999").status_code == 404


def test_list_is_paginated_with_counts(admin_client):
    mid = _blueprint(admin_client, "WA", "wa")
    pid = _project(admin_client, "Con blueprint", model_ids=[mid])
    _project(admin_client, "Sin blueprint")
    body = admin_client.get("/api/v1/projects?page=1&size=20").json()
    assert body["pagination"]["total"] == 2
    counts = {p["id"]: p["blueprint_count"] for p in body["data"]}
    assert counts[pid] == 1


# ─── vínculos ─────────────────────────────────────────────────────────────── #


def test_link_and_list_blueprints(admin_client):
    a = _blueprint(admin_client, "Agenda", "agenda")
    b = _blueprint(admin_client, "Pacientes", "pacientes")
    pid = _project(admin_client, "Citas")
    r = admin_client.post(
        f"/api/v1/projects/{pid}/blueprints", json={"model_ids": [a, b]}
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert sorted(data["linked"]) == sorted([a, b])
    assert data["already_linked"] == []
    assert data["blueprint_count"] == 2
    listed = admin_client.get(f"/api/v1/projects/{pid}/blueprints").json()["data"]
    assert sorted(item["id"] for item in listed) == sorted([a, b])


def test_link_is_idempotent(admin_client):
    mid = _blueprint(admin_client, "Uno", "uno")
    pid = _project(admin_client, "Idempotente")
    admin_client.post(f"/api/v1/projects/{pid}/blueprints", json={"model_ids": [mid]})
    again = admin_client.post(
        f"/api/v1/projects/{pid}/blueprints", json={"model_ids": [mid]}
    )
    assert again.status_code == 200
    data = again.json()["data"]
    assert data["linked"] == []
    assert data["already_linked"] == [mid]
    assert data["blueprint_count"] == 1


def test_link_unknown_model_is_422_and_links_nothing(admin_client):
    mid = _blueprint(admin_client, "Existe", "existe")
    pid = _project(admin_client, "Todo o nada")
    r = admin_client.post(
        f"/api/v1/projects/{pid}/blueprints", json={"model_ids": [mid, 9999]}
    )
    assert r.status_code == 422, r.text
    # Todo-o-nada: el id válido tampoco se vinculó.
    assert admin_client.get(f"/api/v1/projects/{pid}/blueprints").json()["data"] == []


def test_link_empty_list_is_422(admin_client):
    pid = _project(admin_client, "Sin ids")
    assert admin_client.post(
        f"/api/v1/projects/{pid}/blueprints", json={"model_ids": []}
    ).status_code == 422


def test_unlink_keeps_the_blueprint(admin_client):
    mid = _blueprint(admin_client, "Sobrevive", "sobrevive")
    pid = _project(admin_client, "Desvincular", model_ids=[mid])
    r = admin_client.delete(f"/api/v1/projects/{pid}/blueprints/{mid}")
    assert r.status_code == 200, r.text
    assert admin_client.get(f"/api/v1/projects/{pid}/blueprints").json()["data"] == []
    # El blueprint sigue existiendo.
    assert admin_client.get(f"/api/v1/database-models/{mid}").status_code == 200


def test_unlink_missing_link_404(admin_client):
    mid = _blueprint(admin_client, "Ajeno", "ajeno")
    pid = _project(admin_client, "Sin vinculo")
    assert admin_client.delete(
        f"/api/v1/projects/{pid}/blueprints/{mid}"
    ).status_code == 404


def test_blueprint_can_belong_to_several_projects(admin_client):
    shared = _blueprint(admin_client, "Compartido", "compartido")
    p1 = _project(admin_client, "Citas", model_ids=[shared])
    p2 = _project(admin_client, "Omnicanal", model_ids=[shared])
    projects = admin_client.get(
        f"/api/v1/database-models/{shared}/projects"
    ).json()["data"]
    assert sorted(p["id"] for p in projects) == sorted([p1, p2])


def test_model_projects_empty_and_404(admin_client):
    mid = _blueprint(admin_client, "Suelto", "suelto")
    assert admin_client.get(f"/api/v1/database-models/{mid}/projects").json()["data"] == []
    assert admin_client.get("/api/v1/database-models/9999/projects").status_code == 404


# ─── la regla dura ────────────────────────────────────────────────────────── #


def test_delete_project_keeps_blueprints(admin_client):
    """Borrar el proyecto borra la entidad y los vínculos. Los blueprints NO se tocan."""
    a = _blueprint(admin_client, "Agenda", "agenda")
    b = _blueprint(admin_client, "Pacientes", "pacientes")
    pid = _project(admin_client, "Citas", model_ids=[a, b])

    assert admin_client.delete(f"/api/v1/projects/{pid}").status_code == 200
    assert admin_client.get(f"/api/v1/projects/{pid}").status_code == 404

    # Los dos blueprints siguen vivos y ya no pertenecen a ningún proyecto.
    for mid in (a, b):
        assert admin_client.get(f"/api/v1/database-models/{mid}").status_code == 200
        assert admin_client.get(f"/api/v1/database-models/{mid}/projects").json()["data"] == []


def test_delete_blueprint_keeps_projects(admin_client):
    """El caso simétrico: borrar un blueprint suelta su pertenencia, no borra proyectos."""
    a = _blueprint(admin_client, "Se va", "se-va")
    b = _blueprint(admin_client, "Se queda", "se-queda")
    pid = _project(admin_client, "Omnicanal", model_ids=[a, b])

    assert admin_client.delete(f"/api/v1/database-models/{a}").status_code == 200

    project = admin_client.get(f"/api/v1/projects/{pid}")
    assert project.status_code == 200
    assert project.json()["data"]["blueprint_count"] == 1
    remaining = admin_client.get(f"/api/v1/projects/{pid}/blueprints").json()["data"]
    assert [item["id"] for item in remaining] == [b]


def test_create_with_unknown_model_ids_422_leaves_project_empty(admin_client):
    r = admin_client.post(
        "/api/v1/projects", json={"name": "Alta parcial", "model_ids": [9999]}
    )
    assert r.status_code == 422, r.text
    # El proyecto quedó creado y vacío: el cliente reintenta solo la vinculación.
    listed = admin_client.get("/api/v1/projects").json()["data"]
    created = [p for p in listed if p["name"] == "Alta parcial"]
    assert len(created) == 1
    assert created[0]["blueprint_count"] == 0
