"""
Tests unitarios del render de DDL del ``MySQLAdapter`` (sin conexión ni motor).

Foco: la generación de columnas (``_render_column_def``), donde MariaDB refleja la
cláusula ``ON UPDATE …`` DENTRO del ``COLUMN_DEFAULT`` de columnas DATETIME/TIMESTAMP.
Si no se limpia, el CREATE TABLE sale con ``ON UPDATE`` duplicado → SQL inválido y el
clon/aplicación falla. Ver regresión en clone_controller (fase estructura).
"""

from app.services.db_admin.dtos import ColumnInfo, TableSchema, UniqueConstraintInfo
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


def _products_table(pk_order):
    return TableSchema(
        database="db",
        table="products",
        columns=[
            ColumnInfo(name="id", type="int(11)", nullable=False, autoincrement=True),
            ColumnInfo(name="id_product", type="int(11)", nullable=False),
            ColumnInfo(name="name", type="varchar(50)", nullable=True),
        ],
        primary_key=pk_order,
        foreign_keys=[],
        indexes=[],
    )


def test_trailing_autoincrement_in_composite_pk_gets_supporting_key():
    """
    MySQL/MariaDB (InnoDB) exige que AUTO_INCREMENT sea la primera columna de
    alguna clave de la MISMA sentencia CREATE TABLE. Si la PK de origen trae el
    autoincrement al final (patrón heredado, p. ej. tabla migrada de MyISAM a
    InnoDB sin corregir el orden), el render debe agregar una KEY de apoyo o el
    CREATE TABLE dispara el error 1075 al ejecutarse contra un motor real.
    """
    tbl = _products_table(pk_order=["id_product", "id"])
    ddl = _adapter()._render_create_table(tbl)
    assert "PRIMARY KEY (`id_product`, `id`)" in ddl
    assert "KEY `_gw_autoinc_id` (`id`)" in ddl


def test_supporting_key_has_explicit_name_to_avoid_collision_with_real_index():
    """
    Regresión: sin nombre explícito, MySQL/MariaDB auto-nombra un KEY sin nombre igual
    que su columna (`id`). Si el origen YA tiene un índice real sobre esa columna (con
    ese mismo nombre auto-asignado), la sentencia posterior que lo recrea en el destino
    choca con 1061 Duplicate key name. El nombre `_gw_autoinc_id` evita la colisión.
    """
    tbl = _products_table(pk_order=["id_product", "id"])
    ddl = _adapter()._render_create_table(tbl)
    assert "KEY (`id`)" not in ddl  # sin nombre, colisionaría con el índice real del origen


def test_leading_autoincrement_in_pk_does_not_get_redundant_key():
    """Si el autoincrement ya encabeza la PK, no hace falta ninguna KEY extra."""
    tbl = _products_table(pk_order=["id", "id_product"])
    ddl = _adapter()._render_create_table(tbl)
    assert "PRIMARY KEY (`id`, `id_product`)" in ddl
    assert "_gw_autoinc_id" not in ddl


def test_autoincrement_covered_by_leading_unique_does_not_get_redundant_key():
    """Si un UNIQUE inline ya encabeza con el autoincrement, tampoco hace falta la KEY extra."""
    tbl = _products_table(pk_order=["id_product"])
    tbl = tbl.model_copy(
        update={"unique_constraints": [UniqueConstraintInfo(name="uq_id", columns=["id"])]}
    )
    ddl = _adapter()._render_create_table(tbl)
    assert "UNIQUE (`id`)" in ddl
    assert "_gw_autoinc_id" not in ddl
