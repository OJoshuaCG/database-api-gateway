"""
S6 (mcp-schema-definitions): migración ``f5b7d9e1a3c6`` (``servers.readonly_proc_grant``).

Corre sobre SQLite en memoria con el mismo ``Operations`` de Alembic que usa el arranque: prueba
el DDL real (idempotencia, downgrade, default que cierra) sin tocar una base real. El head único se
verifica con la misma lógica de ``scripts/check_migration_graph.py``. Cubre S6.7.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_readonly_proc_grant_migration``
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.server import Server

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = next(_ROOT.glob("alembic/versions/*_f5b7d9e1a3c6_servers_readonly_proc_grant.py"))
_COLUMN = "readonly_proc_grant"


def _load_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_path("mig_f5b7d9e1a3c6", _PATH)


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE servers (id INTEGER PRIMARY KEY, name VARCHAR(50))"))
        c.execute(sa.text("INSERT INTO servers (id, name) VALUES (1, 'previo')"))
        yield c


def _run(conn, fn):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _columnas(conn) -> dict:
    return {c["name"]: c for c in sa.inspect(conn).get_columns("servers")}


def test_revision_chain():
    assert mig.revision == "f5b7d9e1a3c6"
    assert mig.down_revision == "e4a6c8f0b2d5"


def test_upgrade_adds_a_not_null_boolean_column_matching_the_model(conn):
    _run(conn, mig.upgrade)
    columna = _columnas(conn)[_COLUMN]
    assert columna["nullable"] is False
    modelo = Server.__table__.columns[_COLUMN]
    assert modelo.nullable is False
    assert isinstance(modelo.type, sa.Boolean)


def test_existing_rows_start_with_the_flag_off(conn):
    """El default CIERRA: ningún servidor existente gana SELECT ON mysql.proc por migrar."""
    _run(conn, mig.upgrade)
    valor = conn.execute(sa.text("SELECT readonly_proc_grant FROM servers WHERE id = 1")).scalar()
    assert not valor
    conn.execute(sa.text("INSERT INTO servers (id, name) VALUES (2, 'nuevo')"))
    nuevo = conn.execute(sa.text("SELECT readonly_proc_grant FROM servers WHERE id = 2")).scalar()
    assert not nuevo


def test_s6_7_upgrade_twice_is_a_noop_and_keeps_the_data(conn):
    _run(conn, mig.upgrade)
    conn.execute(sa.text("UPDATE servers SET readonly_proc_grant = 1 WHERE id = 1"))
    _run(conn, mig.upgrade)  # no debe chocar consigo misma ni pisar el valor
    assert conn.execute(sa.text("SELECT readonly_proc_grant FROM servers WHERE id = 1")).scalar()


def test_upgrade_resumes_after_a_partial_run_where_the_column_already_exists(conn):
    """MySQL/MariaDB no tienen DDL transaccional: la columna pudo quedar y alembic_version no."""
    conn.execute(
        sa.text("ALTER TABLE servers ADD COLUMN readonly_proc_grant BOOLEAN NOT NULL DEFAULT 0")
    )
    _run(conn, mig.upgrade)
    assert _COLUMN in _columnas(conn)


def test_downgrade_drops_the_column_and_is_idempotent(conn):
    _run(conn, mig.upgrade)
    _run(conn, mig.downgrade)
    assert _COLUMN not in _columnas(conn)
    _run(conn, mig.downgrade)  # segunda vez: no-op
    _run(conn, mig.upgrade)  # y el ciclo se puede repetir
    assert _COLUMN in _columnas(conn)


def test_every_step_checks_the_column_before_touching_it():
    fuente = _PATH.read_text()
    assert "_columnas(bind)" in fuente
    assert "op.drop_constraint" not in fuente


def test_the_migration_graph_has_a_single_head_and_ours_is_a_valid_link():
    script = _load_path("check_migration_graph_mod_s6", _ROOT / "scripts" / "check_migration_graph.py")
    assert script.main() == 0
    parents = {}
    for path in (_ROOT / "alembic" / "versions").glob("*.py"):
        if path.name == "__init__.py":
            continue
        rev, down = script._parse_migration(path)
        parents[rev] = down
    referenced = {p for down in parents.values() for p in down}
    assert parents["f5b7d9e1a3c6"] == ("e4a6c8f0b2d5",)
    # Cualquier migración futura se encadena sobre la nuestra: nunca queda dos veces como head.
    assert len(set(parents) - referenced) == 1
    assert "f5b7d9e1a3c6" in parents
