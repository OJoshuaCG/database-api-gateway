"""
Tests del MANIFIESTO de sentencias y de la reconciliación de una aplicación PARCIAL.

Contexto del problema que cubren (ver ``app/models/model_migration_statement.py``): el DDL
corre en AUTOCOMMIT y Alembic escribe la versión en ``_gw_v_{slug}`` recién al terminar el
``upgrade()``. Si el ``apply`` de la versión N muere en la sentencia 3 de 50, el ledger
sigue diciendo "estoy en N-1" mientras la BD tiene 3 sentencias de N ya commiteadas. Un
``rollback`` en ese estado ejecutaba el ``down_sql`` de N-1 contra una BD contaminada.

Con el manifiesto (una fila por sentencia, con su reverso EMPAREJADO por ``seq``) se puede
deshacer exactamente lo que se aplicó. Estos tests son PUROS: verifican la selección de
reversos y las barreras fail-closed, no la ejecución contra un motor.
"""

from app.controllers.managed_migration_controller import ManagedMigrationController
from app.models.enums import EngineType
from app.services.db_admin.migration_progress import is_resumable
from app.services.db_admin.migrations import (
    ManifestStatement,
    MigrationRunner,
    MigrationSpec,
)

_STATEMENTS = [
    ("CREATE TABLE a (id INT PRIMARY KEY)", "DROP TABLE `a`"),
    ("CREATE TABLE b (id INT PRIMARY KEY)", "DROP TABLE `b`"),
    (
        "ALTER TABLE b ADD CONSTRAINT fk_b FOREIGN KEY (id) REFERENCES a (id)",
        "ALTER TABLE b DROP FOREIGN KEY fk_b",
    ),
    # Una redefinición: su reverso es multi-sentencia (DROP nuevo; CREATE viejo).
    (
        "DROP INDEX `ix_z` ON `c`;\nCREATE INDEX `ix_z` ON `c` (`x`)",
        "DROP INDEX `ix_z` ON `c`;\nCREATE INDEX `ix_z` ON `c` (`y`)",
    ),
]


def _spec(**overrides) -> MigrationSpec:
    up_sql = ";\n".join(up for up, _ in _STATEMENTS)
    base = {
        "id": 7,
        "version": "0003",
        "name": "desde diff",
        "up_sql": up_sql,
        "up_sql_mysql": up_sql,
        "up_sql_postgresql": None,
        "down_sql": None,
        "checksum": "abc123",
        "source_engine": "mysql",
        "manifest": tuple(
            ManifestStatement(seq=i, up_sql=up, down_sql=down, down_confirmed=True)
            for i, (up, down) in enumerate(_STATEMENTS, start=1)
        ),
    }
    base.update(overrides)
    return MigrationSpec(**base)


def _progress(applied: int, total: int = 4) -> dict:
    return {
        "model_migration_id": 7,
        "last_statement_index": applied,
        "total_statements": total,
    }


# --------------------------------------------------------------------------- #
# usable_manifest: barreras fail-closed                                        #
# --------------------------------------------------------------------------- #
def test_valid_manifest_is_accepted():
    assert len(MigrationRunner().usable_manifest(_spec(), EngineType.mysql)) == 4


def test_manifest_of_another_engine_is_ignored():
    """El SQL está renderizado para UN motor; traducido puede no partirse igual."""
    assert MigrationRunner().usable_manifest(_spec(), EngineType.postgresql) == ()


def test_manifest_that_does_not_reproduce_up_sql_is_discarded():
    """
    Segunda barrera (la primera es que el PATCH borra el manifiesto al editar el SQL):
    concatenar el manifiesto debe reproducir EXACTAMENTE el ``up_sql`` vigente. Si no, el
    manifiesto quedó desalineado y usarlo desharía la sentencia equivocada.
    """
    tampered = _spec(up_sql_mysql=";\n".join(up for up, _ in _STATEMENTS) + ";\nDROP TABLE zz")
    assert MigrationRunner().usable_manifest(tampered, EngineType.mysql) == ()


def test_statement_lists_uses_the_manifest_without_resplitting_the_up():
    """Una fila del manifiesto = una sentencia = un ``op.execute`` (contrato con el ``seq``)."""
    ups, downs, pinned = MigrationRunner().statement_lists(_spec(), EngineType.mysql)
    assert pinned is True
    assert len(ups) == 4
    # El reverso SÍ se parte: la redefinición aporta 2 sentencias -> 5 en total.
    assert len(downs) == 5
    # Y va en orden inverso: lo último que se aplicó se deshace primero.
    assert downs[0].startswith("DROP INDEX")


def test_statement_lists_falls_back_to_the_splitter_without_manifest():
    ups, _downs, pinned = MigrationRunner().statement_lists(
        _spec(manifest=(), source_engine=None), EngineType.mysql
    )
    assert pinned is False
    assert len(ups) == 5  # el splitter parte también la redefinición


# --------------------------------------------------------------------------- #
# is_resumable: el manifiesto habilita los cuerpos procedurales                 #
# --------------------------------------------------------------------------- #
def test_procedural_body_is_resumable_only_when_pinned_by_the_manifest():
    sql = "CREATE PROCEDURE sp() BEGIN DECLARE x INT; SELECT 1; END"
    assert not is_resumable(sql, [sql], kind="schema", has_non_portable=True)
    assert is_resumable(
        sql, [sql], kind="schema", has_non_portable=True, manifest_pinned=True
    )


def test_session_state_stays_excluded_even_with_a_manifest():
    """Un resume abre conexión NUEVA: el estado de sesión no sobrevive."""
    sql = "SET FOREIGN_KEY_CHECKS=0"
    assert not is_resumable(
        sql, [sql], kind="schema", has_non_portable=False, manifest_pinned=True
    )


def test_data_migrations_stay_excluded_even_with_a_manifest():
    sql = "INSERT INTO x VALUES (1)"
    assert not is_resumable(
        sql, [sql], kind="data", has_non_portable=False, manifest_pinned=True
    )


# --------------------------------------------------------------------------- #
# Plan de reconciliación                                                       #
# --------------------------------------------------------------------------- #
def test_reconcile_plan_only_undoes_the_statements_that_were_applied():
    """Falló en la 3 de 4 -> hay 2 aplicadas -> se deshacen 2, en orden inverso."""
    plan = ManagedMigrationController._reconcile_plan(
        _spec(), EngineType.mysql, _progress(applied=2)
    )
    assert plan["reconcilable"] is True
    assert [seq for seq, _ in plan["inverses"]] == [2, 1]
    assert [sql for _, sql in plan["inverses"]] == ["DROP TABLE `b`", "DROP TABLE `a`"]


def test_reconcile_plan_splits_a_multi_statement_reverse():
    """El reverso de una redefinición son 2 sentencias: cada una va por separado."""
    plan = ManagedMigrationController._reconcile_plan(
        _spec(), EngineType.mysql, _progress(applied=4)
    )
    assert plan["reconcilable"] is True
    # seq 4 aporta 2 sentencias; 3, 2 y 1 aportan una cada una.
    assert [seq for seq, _ in plan["inverses"]] == [4, 4, 3, 2, 1]


def test_reconcile_plan_refuses_without_a_manifest():
    """Sin manifiesto no se puede saber qué reverso corresponde a la sentencia k."""
    plan = ManagedMigrationController._reconcile_plan(
        _spec(manifest=(), source_engine=None), EngineType.mysql, _progress(applied=2)
    )
    assert plan["reconcilable"] is False
    assert plan["reason"]
    assert plan["inverses"] == []


def test_reconcile_plan_refuses_when_checkpoint_and_manifest_disagree():
    plan = ManagedMigrationController._reconcile_plan(
        _spec(), EngineType.mysql, _progress(applied=2, total=9)
    )
    assert plan["reconcilable"] is False
    assert "no coinciden" in plan["reason"]


def test_reconcile_plan_reports_unreversible_statements_but_offers_the_rest():
    """
    Una sentencia aplicada sin reverso hace ``reconcilable=False`` (el endpoint devuelve
    409), pero se ofrecen los reversos del resto para el camino ``force=true``: sin eso,
    una sola sentencia irreversible dejaría al admin sin salida automática.
    """
    statements = [
        ("CREATE TABLE a (id INT)", "DROP TABLE `a`"),
        ("ALTER TABLE a DROP COLUMN old", None),
        ("CREATE TABLE b (id INT)", "DROP TABLE `b`"),
        ("CREATE TABLE c (id INT)", "DROP TABLE `c`"),
    ]
    up_sql = ";\n".join(up for up, _ in statements)
    spec = _spec(
        up_sql=up_sql,
        up_sql_mysql=up_sql,
        manifest=tuple(
            ManifestStatement(seq=i, up_sql=up, down_sql=down)
            for i, (up, down) in enumerate(statements, start=1)
        ),
    )
    plan = ManagedMigrationController._reconcile_plan(
        spec, EngineType.mysql, _progress(applied=3)
    )
    assert plan["reconcilable"] is False
    assert [u["seq"] for u in plan["unreversible"]] == [2]
    assert [seq for seq, _ in plan["inverses"]] == [3, 1]


def test_reconcile_plan_flags_reverses_that_are_not_demonstrably_safe():
    """
    Un reverso puede existir y NO ser seguro: recrear una tabla borrada devuelve la
    estructura pero no las filas. No bloquea (es el mejor reverso disponible) pero tiene
    que ser visible en el dry-run.
    """
    statements = [
        ("CREATE TABLE a (id INT)", "DROP TABLE `a`", True, False),
        ("DROP TABLE `vieja`", "CREATE TABLE `vieja` (id INT)", False, True),
    ]
    up_sql = ";\n".join(up for up, _, _, _ in statements)
    spec = _spec(
        up_sql=up_sql,
        up_sql_mysql=up_sql,
        manifest=tuple(
            ManifestStatement(
                seq=i, up_sql=up, down_sql=down, down_confirmed=confirmed,
                destructive=destructive, object_name="vieja" if destructive else "a",
                object_type="table",
            )
            for i, (up, down, confirmed, destructive) in enumerate(statements, start=1)
        ),
    )
    plan = ManagedMigrationController._reconcile_plan(
        spec, EngineType.mysql, _progress(applied=2, total=2)
    )
    assert plan["reconcilable"] is True  # ambas tienen reverso
    assert [u["seq"] for u in plan["unconfirmed"]] == [2]
    assert plan["unconfirmed"][0]["destructive"] is True
