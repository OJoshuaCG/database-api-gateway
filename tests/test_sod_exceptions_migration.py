"""
Migración ``f8b0d2e4a6c9``: tabla ``sod_exceptions`` y herencia de las combinaciones existentes.

Corre sobre SQLite en memoria con el ``Operations`` de Alembic, con las tablas mínimas de las que
lee la herencia. Prueba el DDL real contra el modelo, la idempotencia (también de la herencia),
el downgrade, y que la foto de capacidades exclusivas de ``owner`` coincide con el catálogo.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.base import Base
from app.models.sod_exception import SodException
from app.services.capability_catalog import (
    OWNER_ONLY_CAPABILITIES,
    SOD_RULE_ACCESS_ADMIN,
    SOD_RULE_OWNER,
)

_PATH = next(
    pathlib.Path(__file__).resolve().parents[1].glob(
        "alembic/versions/*_f8b0d2e4a6c9_sod_exceptions.py"
    )
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_f8b0d2e4a6c9", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load()


@pytest.fixture
def conn():
    """
    Cuentas: 1 = el sembrado (owner + access_admin + security_officer); 2 = security_officer con
    un rol owner por alcance; 3 = security_officer con una puntual owner-only ACTIVA; 4 =
    security_officer con una owner-only REVOCADA (no cuenta); 5 = owner sin security_officer.
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), "
            "gateway_role VARCHAR(16))"
        ))
        c.execute(sa.text(
            "CREATE TABLE user_global_capabilities (user_id INTEGER, capability VARCHAR(64))"
        ))
        c.execute(sa.text(
            "CREATE TABLE access_grants (user_id INTEGER, scope_type VARCHAR(16), "
            "scope_id INTEGER, role VARCHAR(16))"
        ))
        c.execute(sa.text(
            "CREATE TABLE capability_grants (user_id INTEGER, capability VARCHAR(64), "
            "status VARCHAR(16))"
        ))
        c.execute(sa.text(
            "INSERT INTO users VALUES (1, 'admin', 'owner'), (2, 'so_scope', 'viewer'), "
            "(3, 'so_cap', 'operator'), (4, 'so_rev', 'viewer'), (5, 'owner', 'owner')"
        ))
        c.execute(sa.text(
            "INSERT INTO user_global_capabilities VALUES (1, 'access_admin'), "
            "(1, 'security_officer'), (2, 'security_officer'), (3, 'security_officer'), "
            "(4, 'security_officer')"
        ))
        c.execute(sa.text(
            "INSERT INTO access_grants VALUES (2, 'environment', 3, 'owner'), "
            "(4, 'environment', 3, 'operator')"
        ))
        c.execute(sa.text(
            "INSERT INTO capability_grants VALUES (3, 'blueprints.apply', 'active'), "
            "(4, 'sql_console.execute', 'revoked')"
        ))
        yield c


def _run(conn, fn):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _filas(conn) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(sa.text(
            "SELECT user_id, rule, reason, expires_at, requested_by, approved_by "
            "FROM sod_exceptions ORDER BY user_id, rule"
        ))
    ]


def test_revision_chain():
    assert mig.revision == "f8b0d2e4a6c9"
    assert mig.down_revision == "e7a9c1d3f5b8"


def test_model_is_registered_in_metadata():
    import app.models  # noqa: F401

    assert "sod_exceptions" in Base.metadata.tables
    assert app.models.SodException is SodException


def test_owner_only_snapshot_matches_the_catalog():
    """La foto de la migración es la del catálogo HOY. Si el catálogo cambia, este test lo dice."""
    assert set(mig._OWNER_ONLY) == {c.value for c in OWNER_ONLY_CAPABILITIES}
    assert (mig._RULE_OWNER, mig._RULE_ACCESS_ADMIN) == (SOD_RULE_OWNER, SOD_RULE_ACCESS_ADMIN)


def test_upgrade_creates_the_table_matching_the_model(conn):
    _run(conn, mig.upgrade)
    insp = sa.inspect(conn)
    assert insp.has_table("sod_exceptions")
    assert {c["name"] for c in insp.get_columns("sod_exceptions")} == {
        c.name for c in SodException.__table__.columns
    }
    assert "ix_sod_exceptions_user_rule" in {i["name"] for i in insp.get_indexes("sod_exceptions")}


def test_upgrade_grandfathers_every_existing_violation(conn):
    _run(conn, mig.upgrade)
    assert _filas(conn) == [
        (1, SOD_RULE_ACCESS_ADMIN, "grandfathered", None, None, None),
        (1, SOD_RULE_OWNER, "grandfathered", None, None, None),
        (2, SOD_RULE_OWNER, "grandfathered", None, None, None),
        (3, SOD_RULE_OWNER, "grandfathered", None, None, None),
    ]


def test_upgrade_is_idempotent_including_the_grandfathering(conn):
    _run(conn, mig.upgrade)
    conn.execute(sa.text("DROP INDEX ix_sod_exceptions_user_rule"))
    _run(conn, mig.upgrade)  # no choca consigo misma y repara el índice
    assert len(_filas(conn)) == 4
    names = {i["name"] for i in sa.inspect(conn).get_indexes("sod_exceptions")}
    assert "ix_sod_exceptions_user_rule" in names


def test_downgrade_drops_the_table_and_is_idempotent(conn):
    _run(conn, mig.upgrade)
    _run(conn, mig.downgrade)
    assert not sa.inspect(conn).has_table("sod_exceptions")
    _run(conn, mig.downgrade)
    _run(conn, mig.upgrade)
    assert len(_filas(conn)) == 4


def test_the_rule_check_rejects_an_unknown_rule(conn):
    from sqlalchemy.exc import IntegrityError

    _run(conn, mig.upgrade)
    with pytest.raises(IntegrityError):
        conn.execute(sa.text(
            "INSERT INTO sod_exceptions (user_id, rule, reason) VALUES (5, 'otra', 'x')"
        ))
