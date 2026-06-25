"""
Tests del MigrationRunner: building blocks + ciclo Alembic real contra SQLite.

Las rutas públicas (apply/rollback/stamp) usan remote_engine (solo MySQL/MariaDB/
PostgreSQL), por lo que se prueban contra motores reales en integración (CI). Aquí
se cubren: selección de SQL por motor, compute_pending, version_table_name, la
generación de archivos de revisión y un ciclo upgrade/downgrade/stamp completo
ejecutando el mismo ``env.py`` y ``command.*`` que usa el runner, sobre SQLite.
"""

import tempfile
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect

from app.models.enums import EngineType
from app.services.db_admin.migrations import (
    MigrationRunner,
    MigrationSpec,
    version_table_name,
)


def _spec(version, up_sql, *, mysql=None, pg=None, down=None):
    return MigrationSpec(
        id=int(version), version=version, name=f"m{version}", up_sql=up_sql,
        up_sql_mysql=mysql, up_sql_postgresql=pg, down_sql=down, checksum="x",
    )


# --------------------------------------------------------------------------- #
# Building blocks                                                              #
# --------------------------------------------------------------------------- #
def test_version_table_name_sanitizes_slug():
    assert version_table_name("whatsapp") == "_gw_v_whatsapp"
    assert version_table_name("my-model") == "_gw_v_my_model"
    assert version_table_name("UP-CASE") == "_gw_v_up_case"


def test_select_up_sql_prefers_override():
    r = MigrationRunner()
    s = _spec("0001", "CREATE TABLE a (id INT)", mysql="MYSQL_OVERRIDE", pg="PG_OVERRIDE")
    assert r.select_up_sql(s, EngineType.mysql) == "MYSQL_OVERRIDE"
    assert r.select_up_sql(s, EngineType.mariadb) == "MYSQL_OVERRIDE"
    assert r.select_up_sql(s, EngineType.postgresql) == "PG_OVERRIDE"


def test_select_up_sql_translates_when_no_override():
    r = MigrationRunner()
    s = _spec("0001", "CREATE TABLE a (id INT AUTO_INCREMENT PRIMARY KEY)")
    assert r.select_up_sql(s, EngineType.mysql) == s.up_sql  # passthrough
    pg = r.select_up_sql(s, EngineType.postgresql)
    assert "AUTO_INCREMENT" not in pg


def test_select_down_sql_none_when_absent():
    r = MigrationRunner()
    s = _spec("0001", "CREATE TABLE a (id INT)")
    assert r.select_down_sql(s, EngineType.postgresql) is None


def test_compute_pending():
    r = MigrationRunner()
    specs = [_spec("0001", "x"), _spec("0002", "y"), _spec("0003", "z")]
    assert [s.version for s in r.compute_pending(None, specs)] == ["0001", "0002", "0003"]
    assert [s.version for s in r.compute_pending("0001", specs)] == ["0002", "0003"]
    assert [s.version for s in r.compute_pending("0001", specs, up_to_version="0002")] == ["0002"]
    assert r.compute_pending("0003", specs) == []


def test_compute_pending_numeric_not_lexicographic():
    """Regresión P3: cruzar de 4 a 5 dígitos no debe saltar la migración nueva."""
    r = MigrationRunner()
    specs = [_spec("9999", "a"), _spec("10000", "b")]
    # current=9999 → 10000 está PENDIENTE (lexicográficamente "10000" < "9999").
    assert [s.version for s in r.compute_pending("9999", specs)] == ["10000"]
    # Orden de aplicación numérico.
    assert [s.version for s in r.compute_pending(None, specs)] == ["9999", "10000"]
    # Ancho mixto: 0099 (99) < 00100 (100).
    mixed = [_spec("00100", "x"), _spec("0099", "y")]
    assert [s.version for s in r.compute_pending(None, mixed)] == ["0099", "00100"]


def test_write_revision_files_chains_down_revision():
    r = MigrationRunner()
    specs = [_spec("0001", "CREATE TABLE a (id INT)"),
             _spec("0002", "ALTER TABLE a ADD COLUMN b INT", down="ALTER TABLE a DROP COLUMN b")]
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql)
        rev1 = (vdir / "rev_0001.py").read_text()
        rev2 = (vdir / "rev_0002.py").read_text()
        assert "down_revision = None" in rev1
        assert "down_revision = '0001'" in rev2
        # Sin down_sql confirmado => el downgrade levanta NotImplementedError.
        assert "NotImplementedError" in rev1
        assert "op.execute('ALTER TABLE a DROP COLUMN b')" in rev2


# --------------------------------------------------------------------------- #
# Ciclo Alembic real contra SQLite (env.py compartido + command.*)            #
# --------------------------------------------------------------------------- #
def test_full_upgrade_downgrade_stamp_cycle_sqlite():
    r = MigrationRunner()
    specs = [
        _spec("0001", "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(50))",
              down="DROP TABLE users"),
        _spec("0002", "ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
              down="ALTER TABLE users DROP COLUMN phone"),
    ]
    dbfile = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{dbfile}")
    vt = version_table_name("whatsapp")

    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql)

        with engine.connect() as conn:
            cfg = r._make_config(vdir, conn, vt)
            assert r._read_current(conn, vt) is None

            command.upgrade(cfg, "0001")
            assert r._read_current(conn, vt) == "0001"
            assert "users" in inspect(conn).get_table_names()

            command.upgrade(cfg, "0002")
            assert r._read_current(conn, vt) == "0002"
            assert "phone" in [c["name"] for c in inspect(conn).get_columns("users")]

            command.downgrade(cfg, "-1")
            assert r._read_current(conn, vt) == "0001"
            assert "phone" not in [c["name"] for c in inspect(conn).get_columns("users")]

    # stamp: marca versión sin ejecutar SQL.
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "versions"
        vdir.mkdir()
        r._write_revision_files(vdir, specs, EngineType.mysql)
        with engine.connect() as conn:
            cfg = r._make_config(vdir, conn, vt)
            command.stamp(cfg, "0002")
            assert r._read_current(conn, vt) == "0002"
