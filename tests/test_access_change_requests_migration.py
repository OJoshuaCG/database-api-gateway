"""
Migración ``a9c1e3b5d7f0``: tabla ``access_change_requests`` y ``capability_grants.sod_override_json``.

Corre sobre SQLite en memoria con el ``Operations`` de Alembic. Prueba el DDL contra el modelo, la
idempotencia (incluida la reparación de un índice faltante) y el downgrade.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.access_change_request import AccessChangeRequest
from app.models.base import Base
from app.models.capability_grant import CapabilityGrant

_PATH = next(
    pathlib.Path(__file__).resolve().parents[1].glob(
        "alembic/versions/*_a9c1e3b5d7f0_access_change_requests.py"
    )
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_a9c1e3b5d7f0", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load()


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50))"))
        c.execute(sa.text(
            "CREATE TABLE capability_grants (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "capability VARCHAR(64), status VARCHAR(16))"
        ))
        c.execute(sa.text("INSERT INTO users VALUES (1, 'admin'), (2, 'destino')"))
        yield c


def _run(conn, fn):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _cols(conn, tabla):
    return {c["name"] for c in sa.inspect(conn).get_columns(tabla)}


def test_revision_chain():
    assert mig.revision == "a9c1e3b5d7f0"
    assert mig.down_revision == "f8b0d2e4a6c9"


def test_models_are_registered_in_metadata():
    import app.models  # noqa: F401

    assert "access_change_requests" in Base.metadata.tables
    assert app.models.AccessChangeRequest is AccessChangeRequest


def test_upgrade_creates_the_table_and_the_column_matching_the_models(conn):
    _run(conn, mig.upgrade)
    assert _cols(conn, "access_change_requests") == {
        c.name for c in AccessChangeRequest.__table__.columns
    }
    assert "sod_override_json" in _cols(conn, "capability_grants")
    assert "sod_override_json" in {c.name for c in CapabilityGrant.__table__.columns}
    idx = {i["name"] for i in sa.inspect(conn).get_indexes("access_change_requests")}
    assert set(mig._INDICES) <= idx


def test_upgrade_is_idempotent_and_repairs_a_missing_index(conn):
    _run(conn, mig.upgrade)
    conn.execute(sa.text("DROP INDEX ix_access_change_requests_status_expires"))
    _run(conn, mig.upgrade)
    idx = {i["name"] for i in sa.inspect(conn).get_indexes("access_change_requests")}
    assert set(mig._INDICES) <= idx


def test_the_status_check_rejects_an_unknown_status(conn):
    from sqlalchemy.exc import IntegrityError

    _run(conn, mig.upgrade)
    with pytest.raises(IntegrityError):
        conn.execute(sa.text(
            "INSERT INTO access_change_requests (target_user_id, desired_state_json, "
            "before_hash, status, expires_at) VALUES (2, '{}', 'x', 'otra', CURRENT_TIMESTAMP)"
        ))


def test_downgrade_drops_both_and_is_idempotent(conn):
    _run(conn, mig.upgrade)
    _run(conn, mig.downgrade)
    assert not sa.inspect(conn).has_table("access_change_requests")
    assert "sod_override_json" not in _cols(conn, "capability_grants")
    _run(conn, mig.downgrade)
    _run(conn, mig.upgrade)
    assert sa.inspect(conn).has_table("access_change_requests")
