"""
Tests unitarios del render de DDL del ``MySQLAdapter`` (sin conexión ni motor).

Foco: la generación de columnas (``_render_column_def``), donde MariaDB refleja la
cláusula ``ON UPDATE …`` DENTRO del ``COLUMN_DEFAULT`` de columnas DATETIME/TIMESTAMP.
Si no se limpia, el CREATE TABLE sale con ``ON UPDATE`` duplicado → SQL inválido y el
clon/aplicación falla. Ver regresión en clone_controller (fase estructura).
"""

from app.services.db_admin.dtos import ColumnInfo
from app.services.db_admin.mysql_adapter import MySQLAdapter


def _adapter():
    # Solo necesitamos el render: instanciar sin __init__ (no abre conexión).
    a = MySQLAdapter.__new__(MySQLAdapter)
    a.dialect = "mysql"
    return a


def test_mariadb_on_update_inline_in_default_is_not_duplicated():
    """MariaDB devuelve el ON UPDATE pegado al default → una sola cláusula ON UPDATE."""
    col = ColumnInfo(
        name="updated_at",
        type="DATETIME",
        nullable=False,
        default="current_timestamp() ON UPDATE current_timestamp()",
        on_update="CURRENT_TIMESTAMP",
    )
    ddl = _adapter()._render_column_def(col)
    assert ddl.upper().count("ON UPDATE") == 1
    assert ddl == (
        "`updated_at` DATETIME NOT NULL "
        "DEFAULT current_timestamp() ON UPDATE CURRENT_TIMESTAMP"
    )


def test_mysql_clean_default_plus_on_update_still_single_clause():
    """MySQL separa bien default y on_update: el resultado no cambia (una cláusula)."""
    col = ColumnInfo(
        name="updated_at",
        type="DATETIME",
        nullable=False,
        default="current_timestamp()",
        on_update="CURRENT_TIMESTAMP",
    )
    ddl = _adapter()._render_column_def(col)
    assert ddl.upper().count("ON UPDATE") == 1
    assert ddl == (
        "`updated_at` DATETIME NOT NULL "
        "DEFAULT current_timestamp() ON UPDATE CURRENT_TIMESTAMP"
    )


def test_default_without_on_update_is_untouched():
    col = ColumnInfo(
        name="created_at", type="DATETIME", nullable=False, default="current_timestamp()"
    )
    ddl = _adapter()._render_column_def(col)
    assert "ON UPDATE" not in ddl.upper()
    assert ddl == "`created_at` DATETIME NOT NULL DEFAULT current_timestamp()"


def test_inline_on_update_emitted_even_if_extra_flag_missing():
    """Si el default trae ON UPDATE pero on_update no vino seteado, igual se emite (una vez)."""
    col = ColumnInfo(
        name="updated_at",
        type="DATETIME",
        nullable=False,
        default="current_timestamp() ON UPDATE current_timestamp()",
        on_update=None,
    )
    ddl = _adapter()._render_column_def(col)
    assert ddl.upper().count("ON UPDATE") == 1
    assert ddl.endswith("DEFAULT current_timestamp() ON UPDATE CURRENT_TIMESTAMP")
