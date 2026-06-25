"""Tests de checksum de integridad, validación de versión y orden numérico."""

import pytest

from app.controllers.managed_migration_controller import ManagedMigrationController
from app.exceptions import AppHttpException
from app.services.db_admin.migration_integrity import (
    compute_checksum,
    validate_version,
    version_sort_key,
)


# --------------------------------------------------------------------------- #
# compute_checksum                                                             #
# --------------------------------------------------------------------------- #
def test_checksum_deterministic():
    a = compute_checksum("CREATE TABLE t (id INT)", None, None, None, "0001")
    b = compute_checksum("CREATE TABLE t (id INT)", None, None, None, "0001")
    assert a == b and len(a) == 64


def test_checksum_covers_down_sql():
    base = compute_checksum("CREATE TABLE t (id INT)", None, None, None, "0001")
    with_down = compute_checksum("CREATE TABLE t (id INT)", None, None, "DROP TABLE t", "0001")
    assert base != with_down


def test_checksum_covers_version():
    v1 = compute_checksum("CREATE TABLE t (id INT)", None, None, None, "0001")
    v2 = compute_checksum("CREATE TABLE t (id INT)", None, None, None, "0002")
    assert v1 != v2


def test_checksum_covers_overrides():
    base = compute_checksum("CREATE TABLE t (id INT)", None, None, None, "0001")
    mysql = compute_checksum("CREATE TABLE t (id INT)", "MYSQL", None, None, "0001")
    pg = compute_checksum("CREATE TABLE t (id INT)", None, "PG", None, "0001")
    assert len({base, mysql, pg}) == 3


# --------------------------------------------------------------------------- #
# validate_version (anti path-traversal)                                       #
# --------------------------------------------------------------------------- #
def test_validate_version_ok():
    assert validate_version("0001") == "0001"
    assert validate_version("0123456789") == "0123456789"


@pytest.mark.parametrize("bad", ["../x", "12", "abc", "0001/x", "00 1", "", "0001;DROP"])
def test_validate_version_rejects(bad):
    with pytest.raises(AppHttpException) as exc:
        validate_version(bad)
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# version_sort_key (numérico, no lexicográfico)                                #
# --------------------------------------------------------------------------- #
def test_version_sort_key_numeric():
    versions = ["9999", "10000", "0099", "00100", "0002"]
    ordered = sorted(versions, key=version_sort_key)
    assert ordered == ["0002", "0099", "00100", "9999", "10000"]
    # El orden lexicográfico daría algo distinto (regresión):
    assert sorted(versions) != ordered


# --------------------------------------------------------------------------- #
# _guard_quarantine (ROB1)                                                     #
# --------------------------------------------------------------------------- #
def test_guard_quarantine_blocks_without_force():
    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._guard_quarantine(1, quarantined=True, force=False, dry_run=False)
    assert exc.value.status_code == 409


def test_guard_quarantine_force_and_dry_run_bypass():
    # No deben lanzar.
    ManagedMigrationController._guard_quarantine(1, quarantined=True, force=True, dry_run=False)
    ManagedMigrationController._guard_quarantine(1, quarantined=True, force=False, dry_run=True)
    ManagedMigrationController._guard_quarantine(1, quarantined=False, force=False, dry_run=False)
