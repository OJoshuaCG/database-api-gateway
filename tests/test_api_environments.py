"""
Endpoints de Environments: CRUD, invariantes de política, filtros y seed.

Los tests que NO pueden faltar:

- ``test_seed_rows_match_the_migration``: las filas del seed se declaran DOS veces (en la
  migración y en el servicio). Si divergen, un gateway provisionado por Alembic queda con una
  política y uno provisionado por ``create_all`` con otra — y acá la política decide si el DDL
  destructivo llega a producción. En el catálogo de charsets divergir era inocuo; acá no.
- ``test_delete_with_databases_is_409``: el borrado no puede desclasificar BDs por efecto
  colateral. Si este test falla, marcar una base como productiva dejó de significar algo.
- ``test_weakening_requires_confirm_slug``: apagar el bloqueo de destructivas es un cambio de
  política, no un toggle.
"""

from app.services.environment_catalog import environment_seed_rows


def _envs(admin_client) -> dict[str, dict]:
    r = admin_client.get("/api/v1/environments?size=50")
    assert r.status_code == 200, r.text
    return {e["slug"]: e for e in r.json()["data"]}


def _server(admin_client, server_payload, name="srv-env") -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(name=name))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _owner(admin_client, server_id: int) -> int:
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": "owner1"}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# ─── auth ─────────────────────────────────────────────────────────────────── #


def test_requires_auth(client):
    assert client.get("/api/v1/environments").status_code == 401


# ─── seed ─────────────────────────────────────────────────────────────────── #


def test_seed_creates_the_four_environments(admin_client):
    """Los entornos son un conjunto FIJO de cuatro; la administración es por API a propósito."""
    envs = _envs(admin_client)
    assert set(envs) == {"local", "development", "staging", "production"}
    # `development` es el default, NO `local`: sumar `local` no cambió el comportamiento de las
    # bases nuevas. Y sigue siendo el entorno más permisivo, así que "nace clasificada" no
    # equivale a "nace protegida".
    assert envs["development"]["is_default"] is True
    assert [s for s, e in envs.items() if e["is_default"]] == ["development"]
    # `production` es el único que bloquea destructivas.
    assert [s for s, e in envs.items() if e["blocks_destructive_migrations"]] == ["production"]
    # Orden de promoción: menor rank = más temprano.
    assert (
        envs["local"]["rank"]
        < envs["development"]["rank"]
        < envs["staging"]["rank"]
        < envs["production"]["rank"]
    )


def _load_migration(glob: str):
    """
    Carga un módulo de migración por RUTA: su nombre de archivo no es un identificador Python
    válido, así que no se puede importar normalmente.
    """
    import importlib.util
    import pathlib

    path = next(pathlib.Path("alembic/versions").glob(glob))
    spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def test_seed_rows_match_the_migrations(admin_client):
    """
    ``environment_seed_rows()`` == la UNIÓN de los literales de las migraciones que siembran.

    Es lo único que impide la divergencia entre las dos vías de provisión, y acá divergir SÍ
    hace daño: cada fila es política, así que un gateway provisionado por Alembic y uno
    provisionado por ``create_all`` quedarían con políticas distintas.

    Se compara contra la unión y no contra una sola migración porque los entornos se sembraron
    en dos tandas: ``environments_and_managed_db_link`` creó la tabla con tres filas, y
    ``environment_local`` sumó la cuarta cuando el conjunto pasó a ser fijo. El orden no importa
    (la clave es el ``slug``), el contenido sí.
    """
    base = _load_migration("*_environments_and_managed_db_link.py")._SEED_ROWS
    extra = [_load_migration("*_environment_local.py")._LOCAL_ROW]

    from_migrations = {row["slug"]: row for row in [*base, *extra]}
    from_service = {row["slug"]: row for row in environment_seed_rows()}

    assert from_migrations.keys() == from_service.keys()
    assert from_migrations == from_service


def test_seed_does_not_resurrect_a_deleted_environment(admin_client):
    """
    El seed siembra SOLO si la tabla está vacía. Con filas presentes no toca nada, así que un
    entorno borrado a propósito queda borrado y el default no se puede duplicar.
    """
    from app.services.environment_catalog import seed_environments

    envs = _envs(admin_client)
    r = admin_client.delete(f"/api/v1/environments/{envs['staging']['id']}")
    assert r.status_code == 200, r.text
    seed_environments()  # segundo arranque
    assert "staging" not in _envs(admin_client)


# ─── CRUD ─────────────────────────────────────────────────────────────────── #


def test_create_and_get(admin_client):
    r = admin_client.post(
        "/api/v1/environments",
        json={
            "name": "QA",
            "slug": "qa",
            "rank": 15,
            "color": "info",
            "blocks_destructive_migrations": True,
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["slug"] == "qa"
    assert data["blocks_destructive_migrations"] is True
    assert data["database_count"] == 0
    got = admin_client.get(f"/api/v1/environments/{data['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["name"] == "QA"


def test_slug_is_normalized_in_python(admin_client):
    """
    Se normaliza en Python y no en el motor: MySQL compara case-insensitive por default y
    PostgreSQL no, así que dejarlo al motor haría que la misma fila fuera un duplicado en uno y
    dos filas distintas en el otro.
    """
    data = admin_client.post(
        "/api/v1/environments", json={"name": "Preprod", "slug": "  PrePROD  "}
    ).json()["data"]
    assert data["slug"] == "preprod"


def test_invalid_slug_is_422(admin_client):
    r = admin_client.post(
        "/api/v1/environments", json={"name": "Malo", "slug": "con espacios"}
    )
    assert r.status_code == 422


def test_invalid_color_is_422(admin_client):
    r = admin_client.post(
        "/api/v1/environments", json={"name": "Color", "slug": "color", "color": "#ff0000"}
    )
    assert r.status_code == 422


def test_duplicate_name_and_slug_have_distinct_codes(admin_client):
    """
    Un solo código para "algo está duplicado" obliga a la SPA a adivinar qué input marcar.
    """
    admin_client.post("/api/v1/environments", json={"name": "QA", "slug": "qa"})
    r = admin_client.post("/api/v1/environments", json={"name": "QA", "slug": "qa2"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.name_taken"
    r = admin_client.post("/api/v1/environments", json={"name": "QA2", "slug": "qa"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.slug_taken"


def test_duplicate_rank_is_accepted(admin_client):
    """
    ``rank`` NO es único a propósito: el único rompía el seed en silencio y el orden total
    ``(rank, id)`` ya da un predecesor determinista sin él.
    """
    envs = _envs(admin_client)
    r = admin_client.post(
        "/api/v1/environments",
        json={"name": "Prod EU", "slug": "production-eu", "rank": envs["production"]["rank"]},
    )
    assert r.status_code == 201, r.text


def test_slug_cannot_be_edited(admin_client):
    """El slug es la identidad que se audita y se confirma: no está en el schema de update."""
    envs = _envs(admin_client)
    r = admin_client.patch(
        f"/api/v1/environments/{envs['staging']['id']}", json={"slug": "otro"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["slug"] == "staging"  # el campo se descarta


# ─── is_default ───────────────────────────────────────────────────────────── #


def test_claiming_default_turns_off_the_previous_one(admin_client):
    envs = _envs(admin_client)
    r = admin_client.patch(
        f"/api/v1/environments/{envs['staging']['id']}", json={"is_default": True}
    )
    assert r.status_code == 200, r.text
    after = _envs(admin_client)
    assert after["staging"]["is_default"] is True
    assert after["development"]["is_default"] is False
    assert sum(1 for e in after.values() if e["is_default"]) == 1


def test_default_cannot_be_inactive(admin_client):
    """
    Se valida el estado RESULTANTE, no el enviado: eso cubre las dos mitades (encender el
    default en una fila inactiva, y desactivar la fila que YA es default).
    """
    envs = _envs(admin_client)
    r = admin_client.patch(
        f"/api/v1/environments/{envs['development']['id']}",
        json={"is_active": False},
        params={"confirm_slug": "development"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.default_must_be_active"


def test_cannot_leave_the_system_without_default(admin_client):
    envs = _envs(admin_client)
    r = admin_client.patch(
        f"/api/v1/environments/{envs['development']['id']}",
        json={"is_default": False},
        params={"confirm_slug": "development"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.default_required"


# ─── debilitar la política ────────────────────────────────────────────────── #


def test_weakening_requires_confirm_slug(admin_client):
    envs = _envs(admin_client)
    prod = envs["production"]["id"]
    r = admin_client.patch(
        f"/api/v1/environments/{prod}", json={"blocks_destructive_migrations": False}
    )
    assert r.status_code == 422, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "environment.confirmation_required"
    assert pc["expected_slug"] == "production"
    assert pc["weakened"] == ["blocks_destructive_migrations"]
    # Con el slug equivocado tampoco.
    r = admin_client.patch(
        f"/api/v1/environments/{prod}",
        json={"blocks_destructive_migrations": False},
        params={"confirm_slug": "produccion"},
    )
    assert r.status_code == 422
    # Con el slug correcto sí.
    r = admin_client.patch(
        f"/api/v1/environments/{prod}",
        json={"blocks_destructive_migrations": False},
        params={"confirm_slug": "production"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["blocks_destructive_migrations"] is False


def test_strengthening_does_not_require_confirmation(admin_client):
    """Endurecer es libre; solo aflojar pide el gesto."""
    envs = _envs(admin_client)
    r = admin_client.patch(
        f"/api/v1/environments/{envs['staging']['id']}",
        json={"blocks_destructive_migrations": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["blocks_destructive_migrations"] is True


# ─── borrado ──────────────────────────────────────────────────────────────── #


def test_delete_without_databases_works(admin_client):
    envs = _envs(admin_client)
    r = admin_client.delete(f"/api/v1/environments/{envs['staging']['id']}")
    assert r.status_code == 200, r.text
    assert "staging" not in _envs(admin_client)


def test_delete_with_databases_is_409(admin_client, server_payload):
    """
    El borrado NO desclasifica en masa. No hay ``?force=true``: la vía de retiro de un entorno
    con BDs es ``is_active=false``.
    """
    envs = _envs(admin_client)
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={
            "name": "envdb",
            "server_id": sid,
            "owner_id": oid,
            "environment_id": envs["production"]["id"],
        },
    )
    assert r.status_code == 201, r.text
    r = admin_client.delete(f"/api/v1/environments/{envs['production']['id']}")
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "environment.has_databases"
    assert pc["database_count"] == 1
    # Y el conteo se publica en el listado, así que la SPA puede avisar ANTES del 409.
    assert _envs(admin_client)["production"]["database_count"] == 1


# ─── asignación y filtros ─────────────────────────────────────────────────── #


def test_new_database_gets_the_default_environment(admin_client, server_payload):
    envs = _envs(admin_client)
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "defdb", "server_id": sid, "owner_id": oid},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["environment_id"] == envs["development"]["id"]


def test_inactive_environment_cannot_be_assigned(admin_client, server_payload):
    envs = _envs(admin_client)
    admin_client.patch(
        f"/api/v1/environments/{envs['staging']['id']}",
        json={"is_active": False},
        params={"confirm_slug": "staging"},
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={
            "name": "inact",
            "server_id": sid,
            "owner_id": oid,
            "environment_id": envs["staging"]["id"],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.inactive"


def test_unknown_environment_is_404(admin_client, server_payload):
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "nope", "server_id": sid, "owner_id": oid, "environment_id": 9999},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.not_found"


def test_patch_null_unclassifies(admin_client, server_payload):
    """
    Sin ``environment_id`` en la tupla de campos recorridos del PATCH no habría forma de
    desclasificar una BD: la única vía habría sido borrar el entorno.
    """
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    db_id = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "unclass", "server_id": sid, "owner_id": oid},
    ).json()["data"]["id"]
    r = admin_client.patch(
        f"/api/v1/managed-databases/{db_id}", json={"environment_id": None}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["environment_id"] is None


def test_model_version_is_no_longer_writable_by_patch(admin_client, server_payload):
    """
    Cerrar este camino es lo que impide "promover" una BD tipeando un número, y de paso el
    500 latente: ``version_sort_key`` hace ``int(version)`` y el schema no exigía dígitos.
    """
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    db_id = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "mvdb", "server_id": sid, "owner_id": oid},
    ).json()["data"]["id"]
    r = admin_client.patch(
        f"/api/v1/managed-databases/{db_id}", json={"model_version": "v3-hotfix"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["model_version"] is None  # se descartó, no se persistió


def test_filters_by_environment_and_unassigned(admin_client, server_payload):
    envs = _envs(admin_client)
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    prod = admin_client.post(
        "/api/v1/managed-databases",
        json={
            "name": "fprod",
            "server_id": sid,
            "owner_id": oid,
            "environment_id": envs["production"]["id"],
        },
    ).json()["data"]["id"]
    orphan = admin_client.post(
        "/api/v1/managed-databases",
        json={"name": "forphan", "server_id": sid, "owner_id": oid},
    ).json()["data"]["id"]
    admin_client.patch(
        f"/api/v1/managed-databases/{orphan}", json={"environment_id": None}
    )

    r = admin_client.get(
        f"/api/v1/managed-databases?environment_id={envs['production']['id']}"
    )
    assert [d["id"] for d in r.json()["data"]] == [prod]

    r = admin_client.get("/api/v1/managed-databases?only_unassigned=true")
    assert [d["id"] for d in r.json()["data"]] == [orphan]


def test_conflicting_filters_are_422(admin_client):
    """
    Un filtro contradictorio falla fuerte en vez de devolver una lista vacía en silencio.
    """
    r = admin_client.get(
        "/api/v1/managed-databases?environment_id=1&only_unassigned=true"
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.filter_conflict"
