"""
Migración ``b1d3f5a7c9e2``: tabla ``access_bootstrap`` y su fila según los ``access_admin`` que hay.

Corre sobre SQLite en memoria con el ``Operations`` de Alembic: DDL contra el modelo, la fila POR
ABRIR con ≤ 1 ``access_admin`` activo con credencial, cerrada con ≥ 2, idempotencia y downgrade.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.access_bootstrap import AccessBootstrap

_PATH = next(
    pathlib.Path(__file__).resolve().parents[1].glob(
        "alembic/versions/*_b1d3f5a7c9e2_access_bootstrap.py"
    )
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_b1d3f5a7c9e2", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load()


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), "
            "is_active BOOLEAN, hashed_password VARCHAR(255))"
        ))
        c.execute(sa.text(
            "CREATE TABLE user_global_capabilities (user_id INTEGER, capability VARCHAR(32))"
        ))
        yield c


def _admin(conn, uid: int, *, active=True, password="hash"):
    conn.execute(sa.text("INSERT INTO users VALUES (:i, :u, :a, :p)"),
                 {"i": uid, "u": f"u{uid}", "a": active, "p": password})
    conn.execute(sa.text("INSERT INTO user_global_capabilities VALUES (:i, 'access_admin')"),
                 {"i": uid})


def _run(conn, fn):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _row(conn):
    return conn.execute(sa.text(
        "SELECT id, opened_at, closes_at, closed_at, closed_reason FROM access_bootstrap"
    )).fetchall()


def test_revision_chain():
    assert mig.revision == "b1d3f5a7c9e2"
    assert mig.down_revision == "a9c1e3b5d7f0"


def test_model_is_registered_in_metadata():
    import app.models
    from app.models.base import Base

    assert "access_bootstrap" in Base.metadata.tables
    assert app.models.AccessBootstrap is AccessBootstrap


def test_upgrade_creates_the_table_matching_the_model(conn):
    _run(conn, mig.upgrade)
    cols = {c["name"] for c in sa.inspect(conn).get_columns("access_bootstrap")}
    assert cols == {c.name for c in AccessBootstrap.__table__.columns}


def test_fresh_install_leaves_the_window_to_be_opened_at_first_boot(conn):
    _run(conn, mig.upgrade)
    rows = _row(conn)
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1:] == (None, None, None, None)


def test_one_admin_leaves_the_window_to_be_opened(conn):
    _admin(conn, 1)
    _run(conn, mig.upgrade)
    assert _row(conn)[0][1:] == (None, None, None, None)


def test_inactive_and_uninvited_admins_do_not_count(conn):
    _admin(conn, 1)
    _admin(conn, 2, active=False)
    _admin(conn, 3, password="")
    _run(conn, mig.upgrade)
    assert _row(conn)[0][3] is None, "contó un admin inactivo o sin credencial"


def test_two_admins_insert_it_closed(conn):
    _admin(conn, 1)
    _admin(conn, 2)
    _run(conn, mig.upgrade)
    row = _row(conn)[0]
    assert row[1] is None
    assert row[3] is not None
    assert row[4] == "multiple_admins_at_upgrade"


def test_upgrade_is_idempotent_and_keeps_the_row(conn):
    _run(conn, mig.upgrade)
    _admin(conn, 1)
    _admin(conn, 2)
    _run(conn, mig.upgrade)
    rows = _row(conn)
    assert len(rows) == 1
    assert rows[0][4] is None, "el reintento reescribió la fila"


def test_the_checks_reject_a_second_row_and_an_unknown_reason(conn):
    from sqlalchemy.exc import IntegrityError

    _run(conn, mig.upgrade)
    with pytest.raises(IntegrityError):
        conn.execute(sa.text("INSERT INTO access_bootstrap (id) VALUES (2)"))
    with pytest.raises(IntegrityError):
        conn.execute(sa.text("UPDATE access_bootstrap SET closed_reason = 'otra' WHERE id = 1"))


def test_downgrade_drops_it_and_is_idempotent(conn):
    _run(conn, mig.upgrade)
    _run(conn, mig.downgrade)
    assert not sa.inspect(conn).has_table("access_bootstrap")
    _run(conn, mig.downgrade)
    _run(conn, mig.upgrade)
    assert len(_row(conn)) == 1
