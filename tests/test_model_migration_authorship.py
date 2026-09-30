"""
Autoría de las versiones de blueprint (``model_migrations.created_by_*``).

Cubre las dos mitades del cambio: que una versión NUEVA guarde a su autor —por la API y por
los caminos internos del controller— y que el backfill de la migración ``c5e7a9b1d3f6``
atribuya las versiones históricas solo cuando la auditoría lo sostiene.
"""

import importlib.util
import pathlib
from datetime import datetime

from app.core.actor import token_actor

_AUTHOR_FIELDS = ("created_by_admin_id", "created_by_username", "created_by_actor_type")


def _new_model(admin_client, slug="autoria", name="Autoria") -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _admin_id() -> int:
    from app.core.database import Database
    from app.models.user import User

    s = Database().get_declarative_base_session()
    try:
        return s.query(User.id).filter(User.username == "admin").scalar()
    finally:
        s.close()


def _row(migration_id: int):
    from app.core.database import Database
    from app.models.model_migration import ModelMigration

    s = Database().get_declarative_base_session()
    try:
        return s.get(ModelMigration, migration_id)
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Creación: la versión nueva guarda a su autor                                 #
# --------------------------------------------------------------------------- #
def test_create_stores_author_from_admin(admin_client):
    """La fila guarda el MISMO actor que firma la auditoría ``migration.create``."""
    model_id = _new_model(admin_client)
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"name": "inicial", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    admin_id = _admin_id()
    assert data["created_by_admin_id"] == admin_id
    assert data["created_by_username"] == "admin"
    assert data["created_by_actor_type"] == "admin"

    row = _row(data["id"])
    assert (row.created_by_admin_id, row.created_by_username, row.created_by_actor_type) == (
        admin_id,
        "admin",
        "admin",
    )


def test_list_and_detail_expose_author(admin_client):
    model_id = _new_model(admin_client, slug="autoria2", name="Autoria2")
    created = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"name": "inicial", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"},
    )
    assert created.status_code == 201, created.text
    version = created.json()["data"]["version"]

    listing = admin_client.get(f"/api/v1/database-models/{model_id}/migrations")
    assert listing.status_code == 200, listing.text
    item = listing.json()["data"][0]
    detail = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/{version}")
    assert detail.status_code == 200, detail.text
    out = detail.json()["data"]

    for payload in (item, out):
        assert payload["created_by_admin_id"] == _admin_id()
        assert payload["created_by_username"] == "admin"
        assert payload["created_by_actor_type"] == "admin"


def test_unknown_author_is_null_in_list_and_detail(admin_client):
    """Una versión histórica sin autoría viaja con los tres campos en ``null``, no omitidos."""
    from app.core.database import Database
    from app.models.model_migration import ModelMigration

    model_id = _new_model(admin_client, slug="autoria3", name="Autoria3")
    created = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"name": "inicial", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"},
    )
    migration_id = created.json()["data"]["id"]
    version = created.json()["data"]["version"]
    s = Database().get_declarative_base_session()
    try:
        m = s.get(ModelMigration, migration_id)
        m.created_by_admin_id = m.created_by_username = m.created_by_actor_type = None
        s.commit()
    finally:
        s.close()

    item = admin_client.get(f"/api/v1/database-models/{model_id}/migrations").json()["data"][0]
    out = admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/{version}"
    ).json()["data"]
    for payload in (item, out):
        for field in _AUTHOR_FIELDS:
            assert field in payload and payload[field] is None


def test_create_by_api_token_records_actor_type(admin_client):
    from app.controllers.model_migration_controller import ModelMigrationController

    model_id = _new_model(admin_client, slug="autoria4", name="Autoria4")
    actor = token_actor(token_pk=7, token_id="tok_x", name="agente-ci", scopes="", project_id=1)
    out = ModelMigrationController().create_migration(
        model_id, {"name": "via token", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"}, admin=actor
    )
    assert out["created_by_admin_id"] == 7
    assert out["created_by_username"] == "agente-ci"
    assert out["created_by_actor_type"] == "api_token"


def test_create_without_actor_leaves_author_unknown(admin_client):
    """Sin actor NO se afirma ``admin``: la autoría queda desconocida."""
    from app.controllers.model_migration_controller import ModelMigrationController

    model_id = _new_model(admin_client, slug="autoria5", name="Autoria5")
    out = ModelMigrationController().create_migration(
        model_id, {"name": "interno", "up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"}
    )
    for field in _AUTHOR_FIELDS:
        assert out[field] is None


def test_snapshot_versions_store_author(admin_client):
    """Las N versiones de un blueprint desde snapshot llevan al autor del snapshot."""
    from app.controllers.model_migration_controller import ModelMigrationController
    from app.core.database import Database
    from app.models.model_migration import ModelMigration
    from app.services.db_admin.snapshot_layout import VersionPlan

    plans = [
        VersionPlan(
            name="estructura",
            kind="schema",
            up_sql="CREATE TABLE a (id INT PRIMARY KEY)",
            down_sql_suggested=None,
            has_non_portable=False,
        ),
        VersionPlan(
            name="catálogo",
            kind="data",
            up_sql="INSERT INTO a (id) VALUES (1)",
            down_sql_suggested=None,
            has_non_portable=False,
        ),
    ]
    model_id, _, _ = ModelMigrationController()._persist_snapshot_versions(
        {"name": "Snap", "slug": "snap_autoria"},
        "mysql",
        plans,
        False,
        admin={"id": 3, "username": "legado"},
    )
    s = Database().get_declarative_base_session()
    try:
        rows = s.query(ModelMigration).filter(ModelMigration.model_id == model_id).all()
        assert len(rows) == 2
        for m in rows:
            # Un dict legado es "admin", igual que en ``audit._build``.
            assert (m.created_by_admin_id, m.created_by_username, m.created_by_actor_type) == (
                3,
                "legado",
                "admin",
            )
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Backfill: plan_author_backfill (función pura de la migración)                #
# --------------------------------------------------------------------------- #
def _load_migration():
    """Carga la migración por RUTA: su nombre de archivo no es un identificador Python."""
    path = next(pathlib.Path("alembic/versions").glob("*_c5e7a9b1d3f6_*.py"))
    spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _audit(audit_id, migration_id, model_id, *, created_at=None, admin_id=1, username="ana",
           actor_type="admin", detail=None):
    return {
        "id": audit_id,
        "created_at": created_at or datetime(2026, 9, 1, 10, 0, audit_id % 60),
        "target_id": model_id,
        "detail": detail if detail is not None else f"migración 0001 creada (id={migration_id})",
        "admin_id": admin_id,
        "admin_username": username,
        "actor_type": actor_type,
    }


def _mig(model_id, **author):
    return {
        "model_id": model_id,
        "created_by_admin_id": author.get("admin_id"),
        "created_by_username": author.get("username"),
        "created_by_actor_type": author.get("actor_type"),
    }


def test_backfill_matches_by_id_and_model():
    plan = _load_migration().plan_author_backfill(
        [_audit(1, 42, 5, admin_id=9, username="ana", actor_type="api_token")],
        {42: _mig(5)},
    )
    assert plan == {
        42: {
            "created_by_admin_id": 9,
            "created_by_username": "ana",
            "created_by_actor_type": "api_token",
        }
    }


def test_backfill_ignores_model_id_mismatch():
    plan = _load_migration().plan_author_backfill([_audit(1, 42, 5)], {42: _mig(6)})
    assert plan == {}


def test_backfill_ignores_entries_without_id_or_unknown_version():
    mig = _load_migration()
    rows = [
        _audit(1, 42, 5, detail="migración 0001 creada"),  # formato sin (id=N)
        _audit(2, 42, 5, detail=""),
        _audit(3, 99, 5),  # la versión ya no existe
    ]
    assert mig.plan_author_backfill(rows, {42: _mig(5)}) == {}


def test_backfill_does_not_overwrite_existing_author():
    mig = _load_migration()
    assert mig.plan_author_backfill([_audit(1, 42, 5)], {42: _mig(5, username="beto")}) == {}
    assert mig.plan_author_backfill([_audit(1, 42, 5)], {42: _mig(5, actor_type="admin")}) == {}


def test_backfill_earliest_audit_row_wins():
    mig = _load_migration()
    tarde = _audit(1, 42, 5, created_at=datetime(2026, 9, 2), admin_id=2, username="tarde")
    temprano = _audit(2, 42, 5, created_at=datetime(2026, 9, 1), admin_id=1, username="temprano")
    plan = mig.plan_author_backfill([tarde, temprano], {42: _mig(5)})
    assert plan[42]["created_by_username"] == "temprano"

    # Mismo instante: desempata el id de la auditoría (orden real de inserción).
    mismo = datetime(2026, 9, 1)
    a = _audit(8, 42, 5, created_at=mismo, username="segundo")
    b = _audit(3, 42, 5, created_at=mismo, username="primero")
    assert mig.plan_author_backfill([a, b], {42: _mig(5)})[42]["created_by_username"] == "primero"


def test_backfill_skips_rows_without_identity_and_defaults_actor_type():
    mig = _load_migration()
    anon = _audit(1, 42, 5, admin_id=None, username=None)
    assert mig.plan_author_backfill([anon], {42: _mig(5)}) == {}

    # Una fila anónima MÁS ANTIGUA no bloquea a la siguiente que sí identifica al autor.
    legado = _audit(2, 42, 5, actor_type=None, created_at=datetime(2026, 9, 3))
    plan = mig.plan_author_backfill([anon, legado], {42: _mig(5)})
    assert plan[42]["created_by_actor_type"] == "admin"
