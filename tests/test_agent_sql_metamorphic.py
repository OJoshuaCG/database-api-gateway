"""
Propiedad metamórfica del validador de SQL de agentes (S8, S9).

La idea: si ``S`` es una sentencia que MODIFICA algo, envolverla en cualquier construcción de
lectura no puede convertirla en una lectura aceptable. Es la forma sistemática de buscar el bug que
un corpus escrito a mano no imagina: ``WITH x AS (DELETE …) SELECT``, un subselect, una tabla
derivada, una rama de ``UNION``, un argumento de función.

El producto es sentencia x envoltura x motor y TODAS las combinaciones tienen que rechazarse.
Rechazar incluye "no se pudo parsear" (``invalid``): lo que no se acepta nunca es ``read``.
"""

import pytest

from app.services.db_admin import agent_sql_policy as policy

DATABASE = "mydb"

#: Sentencias que modifican datos, esquema o permisos (S8).
MUTATING = [
    "INSERT INTO t (a) VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "MERGE INTO t USING u ON t.id = u.id WHEN MATCHED THEN DELETE",
    "REPLACE INTO t VALUES (1)",
    "TRUNCATE TABLE t",
    "DROP TABLE t",
    "ALTER TABLE t ADD c INT",
    "CREATE TABLE n (a INT)",
    "GRANT ALL ON *.* TO x",
]

#: Envolturas de lectura. ``{}`` es donde va la sentencia mutante.
WRAPPERS = [
    "{}",
    "WITH x AS ({}) SELECT * FROM x",
    "SELECT ({})",
    "SELECT 1 WHERE 1 IN ({})",
    "SELECT 1 WHERE EXISTS ({})",
    "SELECT * FROM ({}) AS d",
    "SELECT 1 UNION ({})",
    "({}) UNION SELECT 1",
    "SELECT COALESCE(({}), 1)",
    "SELECT LOWER(({}))",
]

ENGINES = ["mysql", "mariadb", "postgresql"]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("statement", MUTATING)
def test_a_wrapped_mutation_is_never_accepted(statement, wrapper, engine):
    sql = wrapper.format(statement)

    verdict = policy.validate_agent_select(sql, engine=engine, database=DATABASE)

    assert not verdict.accepted, f"{engine}: se aceptó {sql!r}"
    assert verdict.classification != "read", f"{engine}: {sql!r} salió read"
    assert verdict.reasons, f"{engine}: {sql!r} sin razón"
    assert verdict.executed_sql is None and verdict.canonical_sql is None


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("statement", MUTATING)
def test_the_draft_envelope_of_a_mutation_is_never_read(statement, engine):
    """S9: el borrador de una mutación nunca dice ``read``, tampoco envuelta en un CTE."""
    for sql in (statement, f"WITH x AS ({statement}) SELECT * FROM x"):
        verdict = policy.validate_agent_select(sql, engine=engine, database=DATABASE)
        envelope = policy.build_draft_envelope(verdict, sql, max_bytes=16_384)

        assert envelope["classification"] != "read", f"{engine}: {sql!r}"
        assert envelope["touches_engine"] is False


@pytest.mark.parametrize("engine", ENGINES)
def test_the_control_group_the_same_wrappers_around_a_select_are_accepted(engine):
    """
    Sin esto la propiedad sería trivial: un validador que rechaza TODO la cumple. Las mismas
    envolturas alrededor de una lectura sí tienen que pasar (donde la forma es válida).
    """
    accepted = 0
    for wrapper in WRAPPERS[1:]:
        sql = wrapper.format("SELECT a FROM t")
        verdict = policy.validate_agent_select(sql, engine=engine, database=DATABASE)
        accepted += verdict.accepted
    # Las envolturas de un solo valor escalar (``SELECT (…)``) pueden rechazarse por forma
    # (``({}) UNION …`` es válida en MySQL y en PostgreSQL); lo que se exige es que la mayoría
    # de las lecturas envueltas pase, o el validador estaría rechazando todo.
    assert accepted >= len(WRAPPERS[1:]) - 2, (
        f"{engine}: solo {accepted} lecturas envueltas pasaron"
    )
