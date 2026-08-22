"""
Tests unitarios PUROS de ``clone_spec``: reglas de coherencia del spec del clon y guard de
compatibilidad del destino. Sin cliente HTTP ni motor, con DTOs en memoria.

El caso más importante del archivo es ``test_narrowing_blocks_in_mysql_family``: verifica la
POLARIDAD de ``is_narrowing``. Esa función está definida en la dirección del diff
(``src`` = deseado, ``tgt`` = actual) y una copia va al revés, así que una implementación que
lea ``DiffItem.risk`` o que pase los argumentos en el orden "natural" aprueba justo el caso
que pierde datos.
"""

from app.services.db_admin import clone_spec as cs
from app.services.db_admin.dtos import (
    CheckConstraintInfo,
    ColumnInfo,
    ComputedInfo,
    ForeignKeyInfo,
    IdentityInfo,
    IndexInfo,
    SchemaSnapshot,
    TableSchema,
    UniqueConstraintInfo,
    ViewInfo,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _col(name, type_="int", *, nullable=True, default=None, collation=None,
         computed=None, identity=None, autoincrement=False, pk=False):
    return ColumnInfo(
        name=name, type=type_, nullable=nullable, default=default, primary_key=pk,
        autoincrement=autoincrement, collation=collation, computed=computed,
        identity=identity,
    )


def _tbl(name, cols, *, pk=None, fks=None, indexes=None, uniques=None, checks=None):
    return TableSchema(
        database="db", table=name, columns=cols, primary_key=pk or [],
        foreign_keys=fks or [], indexes=indexes or [],
        unique_constraints=uniques or [], check_constraints=checks or [],
    )


def _snap(tables, *, engine="mysql", views=None, database="db"):
    return SchemaSnapshot(
        database=database, source_engine=engine, tables=tables, views=views or [],
    )


def _issues(src_tbl, tgt_tbl, *, columns=None, src_engine="mysql", tgt_engine="mysql",
            extra_target=None):
    tgt_tables = [tgt_tbl] if tgt_tbl is not None else []
    tgt_tables += extra_target or []
    return cs.data_compat_issues(
        source=_snap([src_tbl], engine=src_engine),
        target=_snap(tgt_tables, engine=tgt_engine),
        data_columns={src_tbl.table: columns or [c.name for c in src_tbl.columns]},
        source_engine=src_engine,
        target_engine=tgt_engine,
    )


def _reasons(issues, *, blocking_only=False):
    return {i.reason for i in issues if (i.blocking or not blocking_only)}


def _blocking(issues):
    return {i.reason for i in issues if i.blocking}


# =========================================================================== #
# Derivaciones                                                                 #
# =========================================================================== #
def test_entity_ddl_none_only_for_data_only():
    assert cs.entity_ddl_for(cs.CopyIntent.data_only) == "NONE"
    assert cs.entity_ddl_for(cs.CopyIntent.structure_only) == "CREATE"
    assert cs.entity_ddl_for(cs.CopyIntent.structure_and_data) == "CREATE"


def test_scope_ddl_derives_from_container_axes():
    assert cs.scope_ddl_for("new", "none") == "CREATE"
    assert cs.scope_ddl_for("existing", "drop_database") == "DROP_CREATE"
    assert cs.scope_ddl_for("existing", "none") == "NONE"
    assert cs.scope_ddl_for("existing", "objects") == "NONE"


def test_creates_database_only_when_gateway_issues_create():
    assert cs.creates_database("new", "none") is True
    assert cs.creates_database("existing", "drop_database") is True
    assert cs.creates_database("existing", "objects") is False
    assert cs.creates_database("existing", "none") is False


def test_legacy_upsert_reproduces_historical_derivation():
    assert cs.legacy_upsert("existing", "none") is True
    assert cs.legacy_upsert("existing", "objects") is False
    assert cs.legacy_upsert("new", "none") is False


# =========================================================================== #
# Reglas de coherencia                                                         #
# =========================================================================== #
def _validate(**kw):
    base = {
        "intent": cs.CopyIntent.data_only,
        "data_mode": "all",
        "on_existing": cs.DataOnExisting.append,
        "target_mode": "existing",
        "clean_mode": "none",
        "adopt_target": False,
        "charset_override": False,
        "owner_requested": False,
        "target_engine": "mysql",
    }
    base.update(kw)
    return [v.code for v in cs.validate_spec(**base)]


def test_data_only_requires_existing_target():
    assert cs.CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET in _validate(target_mode="new")


def test_data_only_requires_clean_none():
    assert cs.CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET in _validate(clean_mode="objects")
    assert cs.CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET in _validate(clean_mode="drop_database")


def test_data_only_without_data_is_empty_plan():
    assert cs.CODE_EMPTY_PLAN in _validate(data_mode="none")


def test_data_only_requires_explicit_on_existing():
    assert cs.CODE_ON_EXISTING_REQUIRED in _validate(on_existing=None)


def test_adopt_target_rejected_in_data_only():
    assert cs.CODE_ADOPT_REQUIRES_STRUCTURE in _validate(adopt_target=True)


def test_on_existing_rejected_outside_data_only():
    codes = _validate(
        intent=cs.CopyIntent.structure_and_data, on_existing=cs.DataOnExisting.upsert
    )
    assert cs.CODE_CONFLICTING_OPTIONS in codes


def test_structure_only_with_data_is_conflicting():
    codes = _validate(
        intent=cs.CopyIntent.structure_only, on_existing=None, data_mode="all"
    )
    assert cs.CODE_CONFLICTING_OPTIONS in codes


def test_structure_and_data_without_data_is_conflicting():
    codes = _validate(
        intent=cs.CopyIntent.structure_and_data, on_existing=None, data_mode="none"
    )
    assert cs.CODE_CONFLICTING_OPTIONS in codes


def test_valid_data_only_spec_has_no_violations():
    assert _validate() == []


def test_valid_structure_and_data_spec_has_no_violations():
    assert _validate(
        intent=cs.CopyIntent.structure_and_data, on_existing=None, target_mode="new",
        clean_mode="none",
    ) == []


def test_charset_override_requires_a_job_that_creates_the_database():
    assert cs.CODE_CHARSET_NOT_APPLICABLE in _validate(charset_override=True)
    assert cs.CODE_CHARSET_NOT_APPLICABLE not in _validate(
        intent=cs.CopyIntent.structure_and_data, on_existing=None, target_mode="new",
        charset_override=True,
    )


def test_owner_not_applicable_in_mysql_family():
    codes = _validate(
        intent=cs.CopyIntent.structure_and_data, on_existing=None, target_mode="new",
        owner_requested=True, target_engine="mysql",
    )
    assert cs.CODE_OWNER_NOT_APPLICABLE in codes
    codes_pg = _validate(
        intent=cs.CopyIntent.structure_and_data, on_existing=None, target_mode="new",
        owner_requested=True, target_engine="postgresql",
    )
    assert cs.CODE_OWNER_NOT_APPLICABLE not in codes_pg


# =========================================================================== #
# Guard de compatibilidad — bloqueantes en los dos motores                     #
# =========================================================================== #
def test_no_data_tables_means_no_issues():
    assert cs.data_compat_issues(
        source=_snap([]), target=_snap([]), data_columns={},
        source_engine="mysql", target_engine="mysql",
    ) == []


def test_target_not_inspected_is_fail_closed():
    issues = cs.data_compat_issues(
        source=_snap([_tbl("t", [_col("id")])]), target=None,
        data_columns={"t": ["id"]}, source_engine="mysql", target_engine="mysql",
    )
    assert _blocking(issues) == {cs.REASON_TARGET_NOT_INSPECTED}


def test_missing_table_blocks():
    src = _tbl("t", [_col("id")])
    issues = _issues(src, None)
    assert cs.REASON_TABLE_MISSING in _blocking(issues)


def test_name_that_is_a_view_in_target_has_its_own_reason():
    src = _tbl("t", [_col("id")])
    issues = cs.data_compat_issues(
        source=_snap([src]),
        target=_snap([], views=[ViewInfo(name="t", definition="SELECT 1")]),
        data_columns={"t": ["id"]}, source_engine="mysql", target_engine="mysql",
    )
    assert cs.REASON_TABLE_IS_VIEW in _blocking(issues)


def test_missing_column_in_target_blocks():
    src = _tbl("t", [_col("id"), _col("extra")])
    tgt = _tbl("t", [_col("id")])
    assert cs.REASON_COLUMN_MISSING in _blocking(_issues(src, tgt))


def test_generated_column_in_target_blocks():
    src = _tbl("t", [_col("id"), _col("total")])
    tgt = _tbl("t", [_col("id"), _col("total", computed=ComputedInfo(sqltext="1+1"))])
    assert cs.REASON_TARGET_GENERATED in _blocking(_issues(src, tgt))


def test_identity_always_in_target_blocks():
    src = _tbl("t", [_col("id")])
    tgt = _tbl("t", [_col("id", identity=IdentityInfo(always=True))])
    issues = _issues(src, tgt, src_engine="postgresql", tgt_engine="postgresql")
    assert cs.REASON_TARGET_IDENTITY_ALWAYS in _blocking(issues)


def test_identity_by_default_in_target_does_not_block():
    src = _tbl("t", [_col("id")])
    tgt = _tbl("t", [_col("id", identity=IdentityInfo(always=False))])
    issues = _issues(src, tgt, src_engine="postgresql", tgt_engine="postgresql")
    assert cs.REASON_TARGET_IDENTITY_ALWAYS not in _blocking(issues)


def test_target_not_null_without_default_blocks():
    src = _tbl("t", [_col("id")])
    tgt = _tbl("t", [_col("id"), _col("tenant", "int", nullable=False)])
    assert cs.REASON_TARGET_NOT_NULL_NO_DEFAULT in _blocking(_issues(src, tgt))


def test_target_not_null_with_default_is_fine():
    src = _tbl("t", [_col("id")])
    tgt = _tbl("t", [_col("id"), _col("tenant", "int", nullable=False, default="1")])
    assert cs.REASON_TARGET_NOT_NULL_NO_DEFAULT not in _blocking(_issues(src, tgt))


def test_target_autoincrement_column_not_named_is_fine():
    src = _tbl("t", [_col("name", "varchar(10)")])
    tgt = _tbl(
        "t",
        [_col("id", "int", nullable=False, autoincrement=True, pk=True),
         _col("name", "varchar(10)")],
        pk=["id"],
    )
    assert cs.REASON_TARGET_NOT_NULL_NO_DEFAULT not in _blocking(_issues(src, tgt))


def test_target_fk_outside_selection_blocks():
    src = _tbl("child", [_col("id"), _col("parent_id")])
    tgt = _tbl(
        "child", [_col("id"), _col("parent_id")],
        fks=[ForeignKeyInfo(columns=["parent_id"], referred_table="parent",
                            referred_columns=["id"])],
    )
    assert cs.REASON_TARGET_FK_OUTSIDE_SELECTION in _blocking(_issues(src, tgt))


def test_target_fk_inside_selection_does_not_block():
    child_src = _tbl("child", [_col("id"), _col("parent_id")])
    parent_src = _tbl("parent", [_col("id")])
    child_tgt = _tbl(
        "child", [_col("id"), _col("parent_id")],
        fks=[ForeignKeyInfo(columns=["parent_id"], referred_table="parent",
                            referred_columns=["id"])],
    )
    parent_tgt = _tbl("parent", [_col("id")])
    issues = cs.data_compat_issues(
        source=_snap([child_src, parent_src]), target=_snap([child_tgt, parent_tgt]),
        data_columns={"child": ["id", "parent_id"], "parent": ["id"]},
        source_engine="mysql", target_engine="mysql",
    )
    assert cs.REASON_TARGET_FK_OUTSIDE_SELECTION not in _blocking(issues)


# =========================================================================== #
# Guard de compatibilidad — CALIBRACIÓN POR MOTOR                              #
# =========================================================================== #
def test_narrowing_blocks_in_mysql_family():
    """
    EL test de este archivo. Origen ``varchar(50)`` → destino ``varchar(20)``: cada fila de
    más de 20 caracteres se trunca, y en MySQL ``LOAD DATA LOCAL`` lo degrada a warning, así
    que el motor NO falla. Tiene que bloquear.

    Una implementación que llame ``is_narrowing(origen, destino)`` (el orden "natural", que
    es el del diff) devuelve False acá y aprueba la pérdida de datos.
    """
    src = _tbl("t", [_col("name", "varchar(50)")])
    tgt = _tbl("t", [_col("name", "varchar(20)")])
    assert cs.REASON_TYPE_NARROWING in _blocking(_issues(src, tgt))


def test_widening_is_not_flagged_as_narrowing():
    """El caso inverso (destino MÁS ancho) es inofensivo y no puede bloquear."""
    src = _tbl("t", [_col("name", "varchar(20)")])
    tgt = _tbl("t", [_col("name", "varchar(50)")])
    issues = _issues(src, tgt)
    assert cs.REASON_TYPE_NARROWING not in _reasons(issues)


def test_int_narrowing_blocks_in_mysql():
    src = _tbl("t", [_col("n", "bigint")])
    tgt = _tbl("t", [_col("n", "smallint")])
    assert cs.REASON_TYPE_NARROWING in _blocking(_issues(src, tgt))


def test_enum_value_missing_in_target_blocks_in_mysql():
    src = _tbl("t", [_col("s", "enum('a','b','c')")])
    tgt = _tbl("t", [_col("s", "enum('a','b')")])
    assert cs.REASON_TYPE_NARROWING in _blocking(_issues(src, tgt))


def test_narrowing_is_only_a_warning_in_postgresql():
    """En PG ``COPY`` valida y aborta atómico: el motor es la red de seguridad."""
    src = _tbl("t", [_col("name", "varchar(50)")])
    tgt = _tbl("t", [_col("name", "varchar(20)")])
    issues = _issues(src, tgt, src_engine="postgresql", tgt_engine="postgresql")
    assert cs.REASON_TYPE_NARROWING in _reasons(issues)
    assert cs.REASON_TYPE_NARROWING not in _blocking(issues)


def test_unsigned_to_signed_blocks_in_mysql():
    src = _tbl("t", [_col("n", "int unsigned")])
    tgt = _tbl("t", [_col("n", "int")])
    assert cs.REASON_UNSIGNED_TO_SIGNED in _blocking(_issues(src, tgt))


def test_collation_on_key_column_blocks_in_mysql():
    """
    ``utf8mb4_bin`` → ``utf8mb4_general_ci`` en una columna de PK: 'Alice' y 'alice' son dos
    filas en el origen y la MISMA clave en el destino, así que una se pierde sin error.
    """
    src = _tbl("t", [_col("name", "varchar(50)", collation="utf8mb4_bin", pk=True)],
               pk=["name"])
    tgt = _tbl("t", [_col("name", "varchar(50)", collation="utf8mb4_general_ci", pk=True)],
               pk=["name"])
    assert cs.REASON_COLLATION_ON_KEY in _blocking(_issues(src, tgt))


def test_collation_on_non_key_column_is_only_a_warning():
    src = _tbl("t", [_col("bio", "varchar(50)", collation="utf8mb4_bin")])
    tgt = _tbl("t", [_col("bio", "varchar(50)", collation="utf8mb4_general_ci")])
    issues = _issues(src, tgt)
    assert cs.REASON_COLLATION_DIFFERS in _reasons(issues)
    assert cs.REASON_COLLATION_DIFFERS not in _blocking(issues)


def test_collation_on_unique_index_column_counts_as_key():
    src = _tbl("t", [_col("email", "varchar(50)", collation="utf8mb4_bin")])
    tgt = _tbl(
        "t", [_col("email", "varchar(50)", collation="utf8mb4_general_ci")],
        indexes=[IndexInfo(name="uq_email", columns=["email"], unique=True)],
    )
    assert cs.REASON_COLLATION_ON_KEY in _blocking(_issues(src, tgt))


def test_target_only_unique_blocks_in_mysql():
    src = _tbl("t", [_col("id"), _col("email", "varchar(50)")])
    tgt = _tbl(
        "t", [_col("id"), _col("email", "varchar(50)")],
        uniques=[UniqueConstraintInfo(name="uq", columns=["email"])],
    )
    assert cs.REASON_TARGET_UNIQUE_EXTRA in _blocking(_issues(src, tgt))


def test_unique_present_in_both_sides_is_not_flagged():
    uq = [UniqueConstraintInfo(name="uq", columns=["email"])]
    src = _tbl("t", [_col("id"), _col("email", "varchar(50)")], uniques=uq)
    tgt = _tbl("t", [_col("id"), _col("email", "varchar(50)")], uniques=uq)
    assert cs.REASON_TARGET_UNIQUE_EXTRA not in _reasons(_issues(src, tgt))


def test_target_only_check_blocks_in_mysql():
    src = _tbl("t", [_col("n")])
    tgt = _tbl("t", [_col("n")], checks=[CheckConstraintInfo(name="ck", sqltext="n > 0")])
    assert cs.REASON_TARGET_CHECK_EXTRA in _blocking(_issues(src, tgt))


# =========================================================================== #
# Case-folding de nombres de columna, por motor DESTINO                        #
# =========================================================================== #
def test_column_case_is_folded_for_a_mysql_target():
    src = _tbl("t", [_col("ID", "int")])
    tgt = _tbl("t", [_col("id", "int")])
    assert cs.REASON_COLUMN_MISSING not in _blocking(_issues(src, tgt))


def test_column_case_is_significant_for_a_postgresql_target():
    src = _tbl("t", [_col("ID", "int")])
    tgt = _tbl("t", [_col("id", "int")])
    issues = _issues(src, tgt, src_engine="postgresql", tgt_engine="postgresql")
    assert cs.REASON_COLUMN_MISSING in _blocking(issues)


# =========================================================================== #
# Cross-family                                                                 #
# =========================================================================== #
def test_cross_family_does_not_compare_types_and_says_so():
    src = _tbl("t", [_col("n", "bigint")])
    tgt = _tbl("t", [_col("n", "smallint")])
    issues = _issues(src, tgt, src_engine="mysql", tgt_engine="postgresql")
    assert cs.REASON_TYPES_NOT_VERIFIED in _reasons(issues)
    assert cs.REASON_TYPE_NARROWING not in _reasons(issues)
    assert not _blocking(issues)


def test_cross_family_still_blocks_missing_columns():
    src = _tbl("t", [_col("id"), _col("extra")])
    tgt = _tbl("t", [_col("id")])
    issues = _issues(src, tgt, src_engine="mysql", tgt_engine="postgresql",
                     columns=["id", "extra"])
    assert cs.REASON_COLUMN_MISSING in _blocking(issues)


def test_cross_family_blocks_types_the_pipeline_cannot_feed():
    src = _tbl("t", [_col("tags", "text")])
    tgt = _tbl("t", [_col("tags", "text[]")])
    issues = _issues(src, tgt, src_engine="mysql", tgt_engine="postgresql")
    assert cs.REASON_TYPE_NOT_LOADABLE in _blocking(issues)


def test_mysql_and_mariadb_are_the_same_family():
    src = _tbl("t", [_col("name", "varchar(50)")])
    tgt = _tbl("t", [_col("name", "varchar(20)")])
    issues = _issues(src, tgt, src_engine="mysql", tgt_engine="mariadb")
    assert cs.REASON_TYPE_NARROWING in _blocking(issues)
    assert cs.REASON_TYPES_NOT_VERIFIED not in _reasons(issues)


# =========================================================================== #
# Caso feliz                                                                   #
# =========================================================================== #
def test_identical_schema_produces_no_issues():
    cols = [_col("id", "int", nullable=False, pk=True), _col("name", "varchar(50)")]
    src = _tbl("t", cols, pk=["id"])
    tgt = _tbl("t", cols, pk=["id"])
    assert _issues(src, tgt) == []
