"""
Tabla ``capability_grants``: modelo ORM y migración ``d6f8a0b2c4e7``.

Corre sobre SQLite en memoria con el mismo ``Operations`` de Alembic que usa el arranque, así
que prueba el DDL real: idempotencia, downgrade, CHECKs y el ``UNIQUE`` con ``live_key``.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.models.base import Base
from app.models.capability_grant import CapabilityGrant

_PATH = next(
    pathlib.Path(__file__).resolve().parents[1].glob(
        "alembic/versions/*_d6f8a0b2c4e7_capability_grants.py"
    )
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_d6f8a0b2c4e7", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load()


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("PRAGMA foreign_keys=ON"))
        c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50))"))
        c.execute(sa.text("INSERT INTO users (id, username) VALUES (1, 'a'), (2, 'b')"))
        yield c


def _run(conn, fn):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _insert(conn, **kw):
    row = {
        "user_id": 1,
        "capability": "blueprints.apply",
        "scope_type": "environment",
        "scope_id": 3,
        "status": "active",
        "live_key": 1,
    }
    row.update(kw)
    conn.execute(
        sa.text(
            "INSERT INTO capability_grants (user_id, capability, scope_type, scope_id, status,"
            " live_key) VALUES (:user_id, :capability, :scope_type, :scope_id, :status, :live_key)"
        ),
        row,
    )


def test_revision_chain():
    assert mig.revision == "d6f8a0b2c4e7"
    assert mig.down_revision == "c5e7a9b1d3f6"


def test_model_is_registered_in_metadata():
    import app.models  # noqa: F401

    assert "capability_grants" in Base.metadata.tables
    assert app.models.CapabilityGrant is CapabilityGrant


def test_upgrade_creates_table_matching_the_model(conn):
    _run(conn, mig.upgrade)
    insp = sa.inspect(conn)
    assert insp.has_table("capability_grants")
    model = CapabilityGrant.__table__
    # `sod_override_json` la agrega una migración POSTERIOR (a9c1e3b5d7f0, C3).
    assert {c["name"] for c in insp.get_columns("capability_grants")} == {
        c.name for c in model.columns
    } - {"sod_override_json"}
    assert {i["name"] for i in insp.get_indexes("capability_grants")} >= {
        "ix_capability_grants_user_status",
        "ix_capability_grants_status_expires",
    }
    uqs = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("capability_grants")}
    assert uqs["uq_capability_grants_live"] == [
        "user_id", "capability", "scope_type", "scope_id", "live_key",
    ]


def test_upgrade_is_idempotent_and_repairs_a_missing_index(conn):
    _run(conn, mig.upgrade)
    conn.execute(sa.text("DROP INDEX ix_capability_grants_status_expires"))
    _run(conn, mig.upgrade)  # no debe chocar consigo misma
    names = {i["name"] for i in sa.inspect(conn).get_indexes("capability_grants")}
    assert "ix_capability_grants_status_expires" in names


def test_downgrade_drops_the_table_and_is_idempotent(conn):
    _run(conn, mig.upgrade)
    _run(conn, mig.downgrade)
    assert not sa.inspect(conn).has_table("capability_grants")
    _run(conn, mig.downgrade)  # segunda vez: no-op
    _run(conn, mig.upgrade)  # y el ciclo se puede repetir
    assert sa.inspect(conn).has_table("capability_grants")


def test_live_duplicate_is_rejected_but_terminal_history_is_not(conn):
    _run(conn, mig.upgrade)
    _insert(conn)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert(conn, status="pending")  # misma clave, también viva
    # Cualquier cantidad de filas terminales conviven con la viva.
    _insert(conn, status="revoked", live_key=None)
    _insert(conn, status="revoked", live_key=None)
    _insert(conn, status="rejected", live_key=None)
    assert conn.execute(sa.text("SELECT COUNT(*) FROM capability_grants")).scalar() == 4


def test_check_ties_live_key_to_status(conn):
    _run(conn, mig.upgrade)
    for kw in (
        {"status": "active", "live_key": None},
        {"status": "pending", "live_key": None},
        {"status": "revoked", "live_key": 1},
        {"status": "expired", "live_key": 1},
    ):
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                _insert(conn, **kw)


@pytest.mark.parametrize(
    "kw",
    [
        {"scope_type": "global", "scope_id": 1},
        {"scope_type": "environment", "scope_id": 0},
        {"scope_type": "server", "scope_id": -1},
        {"status": "bogus", "live_key": None},
    ],
)
def test_check_rejects_invalid_scope_and_status(conn, kw):
    _run(conn, mig.upgrade)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert(conn, **kw)


def test_deleting_the_user_cascades_but_requested_by_is_set_null(conn):
    _run(conn, mig.upgrade)
    _insert(conn, user_id=1)
    conn.execute(sa.text("UPDATE capability_grants SET requested_by = 2"))
    conn.execute(sa.text("DELETE FROM users WHERE id = 2"))
    assert conn.execute(sa.text("SELECT requested_by FROM capability_grants")).scalar() is None
    conn.execute(sa.text("DELETE FROM users WHERE id = 1"))
    assert conn.execute(sa.text("SELECT COUNT(*) FROM capability_grants")).scalar() == 0


def test_orm_model_creates_the_same_constraints_via_create_all():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = sa.inspect(engine)
    assert insp.has_table("capability_grants")
    cks = {c["name"] for c in insp.get_check_constraints("capability_grants")}
    assert cks == {
        "ck_capability_grants_scope",
        "ck_capability_grants_status",
        "ck_capability_grants_live_key_status",
    }
