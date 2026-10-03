"""
Tabla ``managed_database_data_credentials``: modelo ORM y migración ``e4a6c8f0b2d5``.

Corre sobre SQLite en memoria con el mismo ``Operations`` de Alembic que usa el arranque, así que
prueba el DDL real: idempotencia, downgrade, ``UNIQUE`` y ``ON DELETE CASCADE``. Nunca toca una
base real. El head único se verifica con la misma lógica de ``scripts/check_migration_graph.py``.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.models.base import Base
from app.models.managed_database_data_credential import ManagedDatabaseDataCredential

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = next(
    _ROOT.glob("alembic/versions/*_e4a6c8f0b2d5_managed_database_data_credentials.py")
)
_TABLE = "managed_database_data_credentials"
_UQ = "uq_managed_database_data_credentials_managed_database_id"


def _load_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_path("mig_e4a6c8f0b2d5", _PATH)


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("PRAGMA foreign_keys=ON"))
        c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50))"))
        c.execute(sa.text("CREATE TABLE managed_databases (id INTEGER PRIMARY KEY)"))
        c.execute(sa.text("INSERT INTO users (id, username) VALUES (1, 'a')"))
        c.execute(sa.text("INSERT INTO managed_databases (id) VALUES (1), (2)"))
        yield c


def _run(conn, fn):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _insert(conn, db_id=1, **kw):
    row = {"managed_database_id": db_id, "username": "mcp_d_1", "password_encrypted": "gAAAA"}
    row.update(kw)
    cols = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    conn.execute(sa.text(f"INSERT INTO {_TABLE} ({cols}) VALUES ({vals})"), row)


def test_revision_chain():
    assert mig.revision == "e4a6c8f0b2d5"
    assert mig.down_revision == "d3f5a7b9c1e4"


def test_model_is_registered_in_metadata():
    import app.models  # noqa: F401

    assert _TABLE in Base.metadata.tables
    assert app.models.ManagedDatabaseDataCredential is ManagedDatabaseDataCredential


def test_upgrade_creates_table_matching_the_model(conn):
    _run(conn, mig.upgrade)
    insp = sa.inspect(conn)
    assert insp.has_table(_TABLE)
    model = ManagedDatabaseDataCredential.__table__
    assert {c["name"] for c in insp.get_columns(_TABLE)} == {c.name for c in model.columns}
    uqs = {u["name"]: u["column_names"] for u in insp.get_unique_constraints(_TABLE)}
    assert uqs[_UQ] == ["managed_database_id"]
    assert {fk["referred_table"] for fk in insp.get_foreign_keys(_TABLE)} == {
        "managed_databases",
        "users",
    }


def test_model_has_no_enum_and_the_secret_is_not_nullable():
    cols = ManagedDatabaseDataCredential.__table__.columns
    assert not any(isinstance(c.type, sa.Enum) for c in cols)
    assert cols["password_encrypted"].nullable is False
    assert cols["data_access_allowed"].nullable is False


def test_upgrade_twice_is_a_noop(conn):
    _run(conn, mig.upgrade)
    _insert(conn)
    _run(conn, mig.upgrade)  # no debe chocar consigo misma ni perder filas
    assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {_TABLE}")).scalar() == 1


def test_downgrade_drops_the_table_and_is_idempotent(conn):
    _run(conn, mig.upgrade)
    _run(conn, mig.downgrade)
    assert not sa.inspect(conn).has_table(_TABLE)
    _run(conn, mig.downgrade)  # segunda vez: no-op
    _run(conn, mig.upgrade)  # y el ciclo se puede repetir
    assert sa.inspect(conn).has_table(_TABLE)


def test_defaults_close_the_opt_in_and_unverified(conn):
    _run(conn, mig.upgrade)
    _insert(conn)
    row = conn.execute(
        sa.text(
            f"SELECT account_host, data_access_allowed, verified_at, probe_violations "
            f"FROM {_TABLE}"
        )
    ).one()
    assert row[0] == "%"
    assert not row[1]
    assert row[2] is None and row[3] is None


def test_one_credential_per_database(conn):
    _run(conn, mig.upgrade)
    _insert(conn, db_id=1)
    with pytest.raises(IntegrityError):
        _insert(conn, db_id=1, username="otro")
    _insert(conn, db_id=2, username="mcp_d_2")  # otra base: bien


def test_deleting_the_database_cascades_to_its_credential(conn):
    _run(conn, mig.upgrade)
    _insert(conn, db_id=1)
    conn.execute(sa.text("DELETE FROM managed_databases WHERE id = 1"))
    assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {_TABLE}")).scalar() == 0


def test_migration_never_drops_a_hand_named_constraint():
    """CLAUDE.md: los nombres de la convención pasan de 64 caracteres; se descubre o se borra la tabla."""
    assert "op.drop_constraint" not in _PATH.read_text()


def test_the_migration_graph_has_a_single_head_and_ours_is_it():
    script = _load_path("check_migration_graph_mod", _ROOT / "scripts" / "check_migration_graph.py")
    assert script.main() == 0
    parents = {}
    for path in (_ROOT / "alembic" / "versions").glob("*.py"):
        if path.name == "__init__.py":
            continue
        rev, down = script._parse_migration(path)
        parents[rev] = down
    referenced = {p for down in parents.values() for p in down}
    assert set(parents) - referenced == {"e4a6c8f0b2d5"}
    assert parents["e4a6c8f0b2d5"] == ("d3f5a7b9c1e4",)
