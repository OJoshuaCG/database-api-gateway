"""
Tests del catálogo y validación de privilegios (app/services/db_admin/privileges.py).

Cubre: tokens válidos por nivel/motor, normalización/alias, deduplicación,
clasificación ALLOW/GATE/DENY, niveles no soportados e intentos de evasión.
"""

import pytest

from app.exceptions import AppHttpException
from app.services.db_admin.dtos import GrantLevel
from app.services.db_admin.privileges import (
    classify,
    is_level_supported,
    supported_levels,
    validate_privileges,
)


def _err(privs, dialect, level):
    with pytest.raises(AppHttpException) as exc:
        validate_privileges(privs, dialect, level)
    return exc.value


# --------------------------- ALLOW: tokens válidos --------------------------- #
def test_mysql_table_basic_allows():
    canon, gated = validate_privileges(["SELECT", "INSERT", "UPDATE"], "mysql", GrantLevel.TABLE)
    assert canon == ["SELECT", "INSERT", "UPDATE"]
    assert gated is False


def test_postgres_table_basic_allows():
    canon, gated = validate_privileges(["SELECT", "TRUNCATE"], "postgresql", GrantLevel.TABLE)
    assert canon == ["SELECT", "TRUNCATE"]
    assert gated is False


def test_postgres_sequence_and_schema():
    canon, _ = validate_privileges(["USAGE", "SELECT", "UPDATE"], "postgresql", GrantLevel.SEQUENCE)
    assert set(canon) == {"USAGE", "SELECT", "UPDATE"}
    canon2, _ = validate_privileges(["USAGE", "CREATE"], "postgresql", GrantLevel.SCHEMA)
    assert set(canon2) == {"USAGE", "CREATE"}


def test_multiword_token_normalized():
    canon, _ = validate_privileges(["create   view", "show view"], "mysql", GrantLevel.TABLE)
    assert canon == ["CREATE VIEW", "SHOW VIEW"]


def test_dedupe_case_insensitive():
    canon, _ = validate_privileges(["SELECT", "select", "Select"], "mysql", GrantLevel.TABLE)
    assert canon == ["SELECT"]


def test_db_level_has_more_than_table_mysql():
    # EXECUTE es válido a nivel database/routine pero NO a nivel tabla.
    canon, _ = validate_privileges(["EXECUTE", "EVENT"], "mysql", GrantLevel.DATABASE)
    assert set(canon) == {"EXECUTE", "EVENT"}
    assert _err(["EXECUTE"], "mysql", GrantLevel.TABLE).status_code == 422


def test_mariadb_delete_history_extra():
    canon, _ = validate_privileges(["DELETE HISTORY"], "mariadb", GrantLevel.TABLE)
    assert canon == ["DELETE HISTORY"]
    # MySQL puro no lo tiene.
    assert _err(["DELETE HISTORY"], "mysql", GrantLevel.TABLE).status_code == 422


# ------------------------------- GATE (confirm) ------------------------------ #
def test_all_privileges_is_gated():
    canon, gated = validate_privileges(["ALL"], "mysql", GrantLevel.DATABASE)
    assert canon == ["ALL PRIVILEGES"]  # alias canonicalizado
    assert gated is True


def test_grant_option_gated_mysql():
    canon, gated = validate_privileges(["SELECT", "GRANT OPTION"], "mysql", GrantLevel.TABLE)
    assert "GRANT OPTION" in canon
    assert gated is True


def test_maintain_gated_postgres_table_only():
    _, gated = validate_privileges(["MAINTAIN"], "postgresql", GrantLevel.TABLE)
    assert gated is True
    # MAINTAIN no aplica a secuencia.
    assert _err(["MAINTAIN"], "postgresql", GrantLevel.SEQUENCE).status_code == 422


def test_grant_option_not_valid_at_column_level():
    assert _err(["GRANT OPTION"], "mysql", GrantLevel.COLUMN).status_code == 422


# --------------------------------- DENY -------------------------------------- #
@pytest.mark.parametrize("priv", ["SUPER", "FILE", "PROCESS", "CREATE USER", "SET USER", "PROXY"])
def test_mysql_admin_privs_denied(priv):
    assert _err([priv], "mysql", GrantLevel.DATABASE).status_code == 422


@pytest.mark.parametrize("priv", ["SUPERUSER", "CREATEROLE", "CREATEDB", "BYPASSRLS"])
def test_postgres_role_attrs_denied(priv):
    assert _err([priv], "postgresql", GrantLevel.DATABASE).status_code == 422


def test_deny_takes_precedence_even_if_listed_elsewhere():
    # Aunque venga junto a privilegios válidos, el DENY aborta toda la operación.
    assert _err(["SELECT", "SUPER"], "mysql", GrantLevel.TABLE).status_code == 422


# ----------------------------- niveles no soportados ------------------------- #
def test_schema_and_sequence_unsupported_in_mysql():
    assert is_level_supported("mysql", GrantLevel.SCHEMA) is False
    assert is_level_supported("mysql", GrantLevel.SEQUENCE) is False
    assert _err(["USAGE"], "mysql", GrantLevel.SCHEMA).status_code == 422


def test_global_level_unsupported_phase1():
    assert _err(["SELECT"], "mysql", GrantLevel.GLOBAL).status_code == 422
    assert _err(["CONNECT"], "postgresql", GrantLevel.GLOBAL).status_code == 422


def test_supported_levels_per_engine():
    assert GrantLevel.SCHEMA in supported_levels("postgresql")
    assert GrantLevel.SCHEMA not in supported_levels("mysql")


# ----------------------------- entradas inválidas ---------------------------- #
def test_empty_list_rejected():
    assert _err([], "mysql", GrantLevel.TABLE).status_code == 422


def test_empty_token_rejected():
    assert _err(["  "], "mysql", GrantLevel.TABLE).status_code == 422


def test_unknown_engine_rejected():
    assert _err(["SELECT"], "oracle", GrantLevel.TABLE).status_code == 422


@pytest.mark.parametrize(
    "payload",
    ["SELECT; DROP TABLE x", "SELECT--", "BOGUS", "DROP DATABASE", "'; --"],
)
def test_injection_like_tokens_rejected(payload):
    # Cualquier cosa fuera del set cerrado -> 422 (nunca se interpola).
    assert _err([payload], "mysql", GrantLevel.TABLE).status_code == 422


def test_error_does_not_echo_raw_token():
    # El mensaje no debe reflejar el payload crudo del usuario.
    err = _err(["SELECT; DROP TABLE secret"], "mysql", GrantLevel.TABLE)
    assert "DROP TABLE secret" not in err.message
    assert "DROP TABLE secret" not in str(err.context)


# --------------------------------- classify ---------------------------------- #
def test_classify():
    assert classify("SELECT", "mysql", GrantLevel.TABLE) == "allow"
    assert classify("ALL", "mysql", GrantLevel.TABLE) == "gate"
    assert classify("GRANT OPTION", "mysql", GrantLevel.TABLE) == "gate"
    assert classify("SUPER", "mysql", GrantLevel.TABLE) == "deny"
    assert classify("BOGUS", "mysql", GrantLevel.TABLE) == "invalid"
    assert classify("MAINTAIN", "postgresql", GrantLevel.TABLE) == "gate"
