"""
El esquema de los tokens de integración (modelos ORM y migración).

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Tres propiedades del esquema son DECISIONES de seguridad y no detalles de DDL, y ninguna se ve
desde un endpoint hasta que ya falló:

- ``expires_at`` nullable: NULL es un token sin expiración, que solo se emite con el flag del
  despliegue encendido y nunca con scopes destructivos.
- Las listas de servidores y blueprints se borran en CASCADE con el token y su PK compuesta
  impide duplicados (una lista con un servidor repetido no es una lista distinta).
- ``created_by_admin_id`` sin FK: la fila de un token sobrevive al usuario que lo emitió, para
  poder mostrar "usuario eliminado (#id)" en lugar de perder la evidencia.

Y cada columna lleva su COMMENT: sin ellos ``alembic check`` reporta drift permanente y el
significado de negocio (unidad, estados permitidos) no queda en la base.

Las pruebas inspeccionan el ``MetaData`` y el fuente de la migración con ``ast``: no necesitan BD.
"""

import ast
from pathlib import Path

import pytest

from app.models import AuditLog, IntegrationToken, IntegrationTokenBlueprint, IntegrationTokenServer

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "20261010_1000_b7d9f1a3c5e8_integration_tokens.py"
)
EXPECTED_REVISION = "b7d9f1a3c5e8"
EXPECTED_DOWN_REVISION = "f5b7d9e1a3c6"


def _table(model):
    return model.__table__


# --------------------------------------------------------------------------- #
# integration_tokens                                                          #
# --------------------------------------------------------------------------- #


def test_integration_tokens_table_name_and_columns():
    table = _table(IntegrationToken)

    assert table.name == "integration_tokens"
    assert {column.name for column in table.columns} == {
        "id",
        "token_id",
        "secret_hmac",
        "name",
        "scopes",
        "created_by_admin_id",
        "expires_at",
        "last_used_at",
        "revoked_at",
        "revoked_by_admin_id",
        "created_at",
        "updated_at",
        "note",
    }


def test_expires_at_is_nullable_so_a_token_can_be_non_expiring():
    # NULL means "does not expire"; the controller decides when that may be issued.
    assert _table(IntegrationToken).c.expires_at.nullable is True


def test_public_id_is_unique_indexed_and_fits_the_minted_length():
    column = _table(IntegrationToken).c.token_id

    assert column.nullable is False
    assert column.type.length == 24
    assert column.unique is True
    assert column.index is True


def test_secret_hmac_is_a_sha256_hex_digest_column():
    column = _table(IntegrationToken).c.secret_hmac

    assert column.nullable is False
    assert column.type.length == 64


def test_scopes_column_fits_every_scope_in_the_vocabulary():
    from app.services.integration_scope_catalog import IntegrationScope

    joined_length = len(",".join(scope.value for scope in IntegrationScope))
    assert _table(IntegrationToken).c.scopes.type.length >= joined_length


def test_issuer_and_revoker_columns_have_no_foreign_key():
    table = _table(IntegrationToken)

    # La fila tiene que sobrevivir al usuario: se renderiza "usuario eliminado (#id)".
    assert not table.c.created_by_admin_id.foreign_keys
    assert not table.c.revoked_by_admin_id.foreign_keys
    assert table.c.created_by_admin_id.index is True


def test_revoked_at_is_nullable_and_indexed():
    column = _table(IntegrationToken).c.revoked_at

    assert column.nullable is True
    assert column.index is True


@pytest.mark.parametrize(
    "model", [IntegrationToken, IntegrationTokenServer, IntegrationTokenBlueprint, AuditLog]
)
def test_every_column_of_the_new_tables_has_a_comment(model):
    table = _table(model)
    if model is AuditLog:
        columns = [table.c.integration_token_id]
    else:
        columns = list(table.columns)

    uncommented = [column.name for column in columns if not (column.comment or "").strip()]
    assert not uncommented, f"columnas sin COMMENT en {table.name}: {uncommented}"
    if model is not AuditLog:
        assert (table.comment or "").strip(), f"{table.name} no tiene COMMENT de tabla"


# --------------------------------------------------------------------------- #
# Allowlists                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model", "table_name", "referenced_table", "id_column"),
    [
        (IntegrationTokenServer, "integration_token_servers", "servers", "server_id"),
        (IntegrationTokenBlueprint, "integration_token_blueprints", "database_models", "model_id"),
    ],
)
def test_allowlist_tables_cascade_and_use_a_composite_primary_key(
    model, table_name, referenced_table, id_column
):
    table = _table(model)

    assert table.name == table_name
    assert {column.name for column in table.primary_key.columns} == {"token_pk", id_column}

    foreign_keys_by_column = {
        foreign_key.parent.name: foreign_key for foreign_key in table.foreign_keys
    }
    assert set(foreign_keys_by_column) == {"token_pk", id_column}
    assert foreign_keys_by_column["token_pk"].column.table.name == "integration_tokens"
    assert foreign_keys_by_column[id_column].column.table.name == referenced_table
    for foreign_key in foreign_keys_by_column.values():
        # Borrar el token (o el servidor/blueprint) limpia la lista; nunca queda una fila colgada.
        assert foreign_key.ondelete == "CASCADE"


# --------------------------------------------------------------------------- #
# audit_log                                                                   #
# --------------------------------------------------------------------------- #


def test_audit_log_gets_a_nullable_indexed_integration_token_id():
    column = _table(AuditLog).c.integration_token_id

    assert column.nullable is True
    assert column.index is True
    assert not column.foreign_keys


def test_audit_log_actor_type_column_fits_the_integration_kind():
    assert _table(AuditLog).c.actor_type.type.length >= len("integration")


# --------------------------------------------------------------------------- #
# Migración (fuente, sin BD ni .env)                                          #
# --------------------------------------------------------------------------- #


def _migration_source() -> str:
    assert MIGRATION_PATH.is_file(), f"falta la migración {MIGRATION_PATH.name}"
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _module_level_string(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} no está definido en la migración")


def test_migration_revision_chain_points_at_the_current_head():
    tree = ast.parse(_migration_source())

    assert _module_level_string(tree, "revision") == EXPECTED_REVISION
    assert _module_level_string(tree, "down_revision") == EXPECTED_DOWN_REVISION


def test_migration_defines_upgrade_and_downgrade():
    tree = ast.parse(_migration_source())
    function_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    assert {"upgrade", "downgrade"} <= function_names


def test_migration_is_inspector_guarded_and_never_hand_names_a_dropped_constraint():
    source = _migration_source()

    # Idempotente: MySQL/MariaDB no tienen DDL transaccional, y un reintento tras una migración
    # que murió a mitad no puede chocar consigo mismo.
    assert "sa.inspect(" in source or "inspect(" in source
    assert "has_table" in source or "get_table_names" in source
    # Regla del repo: una constraint se descubre por introspección, no por nombre escrito a mano.
    assert "drop_constraint(" not in source


def test_migration_creates_all_three_tables_and_the_audit_column():
    source = _migration_source()

    for table_name in (
        "integration_tokens",
        "integration_token_servers",
        "integration_token_blueprints",
    ):
        assert table_name in source
    assert "integration_token_id" in source
    assert "audit_log" in source
