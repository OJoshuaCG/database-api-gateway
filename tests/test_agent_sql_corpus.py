"""
Corpus del validador de SQL de agentes: qué se rechaza, qué se acepta y cómo queda acotado.

Cubre S1-S2 (write/ddl con advertencia), S5-S10 (rechazo y aceptación por AST), R1-R15 y A1-A8, y la
cota de filas del validador (S27, lado validador). Puro: ni motor ni BD. Los datos están en
``tests/fixtures/agent_sql_corpus.py`` y se expanden a una lista plana de casos porque el arnés
directo (``scripts/run_tests_direct.py``) no multiplica fixtures por parámetro.

Cada fila de ``MUST_REJECT`` se valida contra el código PÚBLICO (``mcp_catalog``): los códigos
internos del validador nunca salen y por eso ningún test los nombra.
"""

import pytest

from app.services import mcp_catalog as codes
from app.services.db_admin import agent_sql_policy as policy
from tests.fixtures.agent_sql_corpus import (
    DATABASE,
    KNOWN_FALSE_POSITIVE_CASES,
    MUST_ACCEPT_CASES,
    MUST_REJECT_CASES,
    MYSQL,
    MYSQL_FAMILY,
    PG,
)


def _verdict(sql, engine=MYSQL, **kwargs):
    return policy.validate_agent_select(sql, engine=engine, database=DATABASE, **kwargs)


# --------------------------------------------------------------------------- #
# Direction A — todo MUST_REJECT se rechaza con su código público              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", MUST_REJECT_CASES)
def test_must_reject(case):
    verdict = _verdict(case.sql, case.engine)

    assert not verdict.accepted, f"{case.id}: se aceptó {case.sql!r}"
    assert verdict.classification in {"write", "ddl", "blocked", "invalid"}, case.id
    assert verdict.reasons, f"{case.id}: rechazo sin razón"
    assert set(verdict.reasons) & case.codes, (
        f"{case.id}: {case.sql!r} -> {verdict.reasons}, se esperaba alguno de {sorted(case.codes)}"
    )
    # Cerrado: ningún código fuera del vocabulario público.
    assert set(verdict.reasons) <= codes.REASON_CODES, case.id
    # Un rechazo no trae nada ejecutable.
    assert verdict.canonical_sql is None and verdict.executed_sql is None, case.id
    # S1/S2: toda escritura o DDL viaja con advertencia.
    if verdict.classification in ("write", "ddl"):
        assert verdict.warnings, f"{case.id}: {verdict.classification} sin advertencia"
        assert set(verdict.warnings) <= codes.WARNING_CODES, case.id


@pytest.mark.parametrize("case", MUST_REJECT_CASES)
def test_a_rejected_text_is_never_a_read_in_the_draft_envelope(case):
    """S9: el borrador de cualquier fila del corpus nunca dice ``read``."""
    verdict = _verdict(case.sql, case.engine)
    envelope = policy.build_draft_envelope(verdict, case.sql, max_bytes=16_384)

    assert envelope["classification"] != "read", case.id
    assert envelope["touches_engine"] is False


# --------------------------------------------------------------------------- #
# Direction B — todo MUST_ACCEPT se acepta sin razones                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", MUST_ACCEPT_CASES)
def test_must_accept(case):
    verdict = _verdict(case.sql, case.engine)

    assert verdict.accepted, (
        f"{case.id}: {case.sql!r} -> {verdict.classification} {verdict.reasons}"
    )
    assert verdict.classification == "read"
    assert verdict.reasons == ()
    # S10: sin advertencias de intención de escritura.
    assert verdict.warnings == ()
    assert verdict.canonical_sql and verdict.executed_sql and verdict.human_query
    assert verdict.sql_hash and verdict.masked_sql
    assert verdict.row_bound is not None and verdict.row_bound.kind in ("pushed", "own_limit")


@pytest.mark.parametrize("case", MUST_ACCEPT_CASES)
def test_the_canonical_text_is_a_fixed_point(case):
    """
    D4: el render canónico vuelve a pasar el pipeline COMPLETO y se renderiza igual a sí mismo. Si
    no lo hiciera, lo que se ejecuta sería un texto que nadie validó.
    """
    first = _verdict(case.sql, case.engine)
    again = _verdict(first.canonical_sql, case.engine)

    assert again.accepted, f"{case.id}: {first.canonical_sql!r} -> {again.reasons}"
    assert again.canonical_sql == first.canonical_sql


@pytest.mark.parametrize("case", KNOWN_FALSE_POSITIVE_CASES)
def test_known_false_positives_stay_rejected(case):
    """
    Falsos positivos DELIBERADOS: se fijan para que el costo esté escrito. Si alguno pasa a
    aceptarse, hay que releer ``docs/development/decisiones-e-incidentes.md`` antes de celebrar.
    """
    verdict = _verdict(case.sql, case.engine)

    assert not verdict.accepted, case.id
    assert set(verdict.reasons) & case.codes, f"{case.id}: {verdict.reasons}"


# --------------------------------------------------------------------------- #
# S1/S2: escritura y DDL                                                       #
# --------------------------------------------------------------------------- #
_WRITES = [
    "INSERT INTO t (a) VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "MERGE INTO t USING u ON t.id = u.id WHEN MATCHED THEN DELETE",
]
_DDLS = [
    "DROP TABLE t",
    "TRUNCATE TABLE t",
    "ALTER TABLE t ADD c INT",
    "CREATE TABLE n (a INT)",
]


@pytest.mark.parametrize("engine", [MYSQL, PG])
@pytest.mark.parametrize("sql", _WRITES)
def test_a_write_is_classified_write_and_carries_a_warning(sql, engine):
    verdict = _verdict(sql, engine)

    assert verdict.classification == "write"
    assert verdict.warnings == (codes.WARN_WRITE_NOT_EXECUTED,)
    assert codes.REASON_NOT_SELECT in verdict.reasons


@pytest.mark.parametrize("engine", [MYSQL, PG])
@pytest.mark.parametrize("sql", _DDLS)
def test_a_ddl_is_classified_ddl_and_carries_a_warning(sql, engine):
    verdict = _verdict(sql, engine)

    assert verdict.classification == "ddl"
    assert verdict.warnings == (codes.WARN_DDL_NOT_EXECUTED,)
    assert codes.REASON_NOT_SELECT in verdict.reasons


def test_dcl_is_blocked_not_a_write():
    verdict = _verdict("GRANT ALL ON *.* TO x")

    assert verdict.classification == "blocked"
    assert verdict.warnings == ()


def test_a_read_that_the_agent_profile_rejects_is_reported_blocked():
    """La consola diría ``read``; el perfil de agente no: ``blocked`` con razones, no ``read``."""
    verdict = _verdict("SELECT SLEEP(1)")

    assert verdict.classification == "blocked"
    assert verdict.reasons == (codes.REASON_FUNCTION_NOT_ALLOWED,)


def test_an_unparseable_text_is_invalid_and_never_falls_back_to_text_matching():
    """S3: sin respaldo por palabras clave. ``SHOW GRANTS`` (que la consola sí admite) se rechaza."""
    broken = _verdict("SELECT * FROM (")
    assert broken.classification == "invalid"
    assert broken.reasons == (codes.REASON_PARSE_FAILED,)

    assert not _verdict("SHOW GRANTS FOR CURRENT_USER()").accepted


# --------------------------------------------------------------------------- #
# Entradas degeneradas: todas devuelven veredicto, ninguna levanta              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [None, 123, b"SELECT 1", ["SELECT 1"], {"sql": "x"}])
def test_a_non_string_is_rejected_without_raising(value):
    verdict = _verdict(value)

    assert not verdict.accepted
    assert verdict.classification == "invalid"


def test_an_unknown_engine_is_rejected_not_defaulted_to_mysql():
    verdict = policy.validate_agent_select("SELECT 1", engine="oracle", database=DATABASE)

    assert not verdict.accepted
    assert codes.REASON_UNSUPPORTED_NODE in verdict.reasons


def test_control_characters_are_rejected():
    for text in ("SELECT 1\x00", "SELECT\x07 1", "SELECT '\x1b[31m'"):
        verdict = _verdict(text)
        assert verdict.classification == "invalid", repr(text)
        assert verdict.reasons == (codes.REASON_PARSE_FAILED,), repr(text)


def test_an_oversized_text_is_rejected_before_lexing():
    verdict = _verdict("SELECT '" + "a" * 20_000 + "'")

    assert verdict.classification == "invalid"
    assert verdict.reasons == (codes.REASON_SQL_TOO_LARGE,)


def test_the_size_cap_counts_bytes_not_characters():
    # 'é' son 2 bytes: 40 caracteres = 80 bytes.
    sql = "SELECT '" + "é" * 40 + "'"

    assert _verdict(sql, max_bytes=120).accepted
    assert _verdict(sql, max_bytes=60).reasons == (codes.REASON_SQL_TOO_LARGE,)


def test_a_very_deep_nesting_is_rejected_not_walked():
    sql = "SELECT " + "(" * 90 + "1" + ")" * 90

    verdict = _verdict(sql)

    assert not verdict.accepted
    assert set(verdict.reasons) & {codes.REASON_UNSUPPORTED_NODE, codes.REASON_PARSE_FAILED}


# --------------------------------------------------------------------------- #
# Cota de filas (D15, S27 lado validador)                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine", [MYSQL, PG])
def test_an_own_literal_limit_within_the_cap_is_kept_as_written(engine):
    verdict = _verdict("SELECT * FROM t LIMIT 10", engine)

    assert verdict.accepted
    assert verdict.row_bound.kind == "own_limit"
    assert verdict.executed_sql == "SELECT * FROM t LIMIT 10" == verdict.canonical_sql


@pytest.mark.parametrize("engine", [MYSQL, PG])
def test_no_limit_gets_the_cap_plus_one_pushed(engine):
    verdict = _verdict("SELECT * FROM t", engine)

    assert verdict.accepted
    assert verdict.row_bound.kind == "pushed"
    assert verdict.executed_sql == "SELECT * FROM t LIMIT 101"
    # Lo que se le muestra a un humano NO lleva el tope del gateway y nunca se ejecuta.
    assert verdict.human_query == verdict.canonical_sql == "SELECT * FROM t"


@pytest.mark.parametrize("engine", [MYSQL, PG])
def test_a_limit_above_the_cap_is_replaced_and_the_agent_text_is_kept_for_humans(engine):
    verdict = _verdict("SELECT * FROM t LIMIT 1000", engine)

    assert verdict.row_bound.kind == "pushed"
    assert verdict.executed_sql == "SELECT * FROM t LIMIT 101"
    assert verdict.human_query == "SELECT * FROM t LIMIT 1000"


def test_the_cap_is_a_parameter_not_a_constant():
    verdict = _verdict("SELECT * FROM t LIMIT 10", max_rows=5)

    assert verdict.row_bound.kind == "pushed"
    assert verdict.executed_sql == "SELECT * FROM t LIMIT 6"


def test_an_offset_is_preserved_when_the_limit_is_pushed():
    verdict = _verdict("SELECT * FROM t LIMIT 1000 OFFSET 5")

    assert verdict.executed_sql == "SELECT * FROM t LIMIT 101 OFFSET 5"


def test_postgres_limit_all_is_accepted_and_bounded_by_the_pushdown():
    """``LIMIT ALL`` no deja rastro en el AST: se acepta, y queda acotado por el ``LIMIT n+1``."""
    verdict = _verdict("SELECT * FROM t LIMIT ALL", PG)

    assert verdict.accepted
    assert verdict.row_bound.kind == "pushed"
    assert verdict.executed_sql == "SELECT * FROM t LIMIT 101"


def test_mysql_limit_offset_comma_form_is_normalized_and_bounded():
    verdict = _verdict("SELECT * FROM t LIMIT 5, 10")

    assert verdict.accepted
    assert verdict.canonical_sql == "SELECT * FROM t LIMIT 10 OFFSET 5"
    assert verdict.row_bound.kind == "own_limit"


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", PG),
        ("SELECT * FROM t OFFSET 1 ROW FETCH NEXT 1 ROW ONLY", PG),
        ("SELECT * FROM t LIMIT ?", MYSQL),
        ("SELECT * FROM t LIMIT 1 + 1", MYSQL),
        ("SELECT * FROM t LIMIT 1 + 1", PG),
        ("SELECT * FROM t LIMIT 5 OFFSET ?", MYSQL),
        ("SELECT * FROM t LIMIT -1", PG),
    ],
)
def test_a_limit_that_cannot_be_bounded_is_rejected(sql, engine):
    verdict = _verdict(sql, engine)

    assert not verdict.accepted
    assert codes.REASON_LIMIT_NOT_BOUNDABLE in verdict.reasons


@pytest.mark.parametrize("engine", [MYSQL, PG])
def test_a_huge_literal_offset_is_refused(engine):
    verdict = _verdict("SELECT * FROM t LIMIT 5 OFFSET 10000000", engine)

    assert not verdict.accepted
    assert codes.REASON_OFFSET_TOO_HIGH in verdict.reasons


def test_the_offset_ceiling_is_inclusive_and_configurable():
    assert _verdict("SELECT * FROM t LIMIT 5 OFFSET 10000").accepted
    assert not _verdict("SELECT * FROM t LIMIT 5 OFFSET 10001").accepted
    assert _verdict("SELECT * FROM t LIMIT 5 OFFSET 100", max_offset=100).accepted
    assert (
        codes.REASON_OFFSET_TOO_HIGH
        in _verdict("SELECT * FROM t LIMIT 5 OFFSET 101", max_offset=100).reasons
    )


@pytest.mark.parametrize("engine", [MYSQL, PG])
@pytest.mark.parametrize("op", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
def test_a_set_operation_gets_the_limit_on_the_whole_result(op, engine):
    verdict = _verdict(f"SELECT 1 {op} SELECT 2", engine)

    assert verdict.accepted
    assert verdict.row_bound.kind == "pushed"
    assert verdict.executed_sql == f"SELECT 1 {op} SELECT 2 LIMIT 101"


def test_a_zero_limit_is_the_agents_own_bound():
    verdict = _verdict("SELECT * FROM t LIMIT 0")

    assert verdict.accepted and verdict.row_bound.kind == "own_limit"


# --------------------------------------------------------------------------- #
# Lo que ve auditoría                                                          #
# --------------------------------------------------------------------------- #
def test_masked_sql_never_carries_a_literal():
    verdict = _verdict("SELECT * FROM t WHERE a = 'secreto' AND b = 42 LIMIT 3")

    assert verdict.masked_sql == "SELECT * FROM t WHERE a = ? AND b = ? LIMIT ?"
    assert "secreto" not in verdict.masked_sql and "42" not in verdict.masked_sql


def test_the_hash_is_of_the_canonical_text_not_of_the_formatting():
    one = _verdict("select   *\n from   t where a=1")
    two = _verdict("SELECT * FROM t WHERE a = 1")

    assert one.sql_hash == two.sql_hash


@pytest.mark.parametrize("engine", MYSQL_FAMILY)
def test_the_mariadb_engine_name_is_a_mysql_family_member(engine):
    assert _verdict("SELECT 1", engine).accepted
    assert not _verdict("SELECT 1 /*M!100000 ,2 */", engine).accepted
