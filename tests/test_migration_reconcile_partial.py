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
    """Una fila del manifiesto = una sentencia = un ``exec_driver_sql`` (contrato con el ``seq``)."""
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
    # Es el estado del MEDIO: no se puede deshacer todo, pero ``force`` sí procede. Si
    # esto colapsa en "no reconciliable", la SPA esconde la única salida automática.
    assert plan["reconcilable_with_force"] is True
    assert plan["reason"]


def test_reconcile_plan_has_no_automatic_way_out_when_nothing_is_reversible():
    """
    Si NINGUNA de las sentencias aplicadas tiene reverso no hay nada que ejecutar: los
    dos flags en false y un motivo que lo diga. ``force`` no inventa reversos.

    Antes este camino devolvía ``reason=None``, y el 409 de ``reconcile_partial`` lo
    interpolaba: el operador recibía un mensaje terminado en "None.".
    """
    statements = [
        ("ALTER TABLE a DROP COLUMN uno", None),
        ("ALTER TABLE a DROP COLUMN dos", None),
        ("CREATE TABLE b (id INT)", "DROP TABLE `b`"),
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
    # Aplicadas 1 y 2 (las dos irreversibles); la 3, que sí tiene reverso, nunca corrió.
    plan = ManagedMigrationController._reconcile_plan(
        spec, EngineType.mysql, _progress(applied=2, total=3)
    )
    assert plan["reconcilable"] is False
    assert plan["reconcilable_with_force"] is False
    assert plan["inverses"] == []
    assert "no hay reversos que ejecutar" in plan["reason"]


def test_partial_entry_exposes_the_force_only_way_out():
    """
    El estado del medio tiene que llegar al frontend como tal: es lo que decide si se
    ofrece el botón de reconciliar (con aceptación explícita de force) o si se manda al
    operador a la salida manual.
    """
    statements = [
        ("CREATE TABLE a (id INT)", "DROP TABLE `a`"),
        ("ALTER TABLE a DROP COLUMN old", None),
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
    entry = ManagedMigrationController._partial_entry(
        spec, EngineType.mysql, _progress(applied=2, total=2)
    )
    assert entry["reconcilable"] is False
    assert entry["reconcilable_with_force"] is True
    assert entry["reason"]
    assert entry["statements_to_undo"] == 1


def test_partial_entry_without_a_manifest_has_no_automatic_way_out():
    """
    El caso real que dejó una BD sin salida visible: migración sin manifiesto para el
    motor destino. Ambos flags en false → la SPA debe habilitar ``stamp?force=true``.
    """
    entry = ManagedMigrationController._partial_entry(
        _spec(manifest=(), source_engine=None), EngineType.mysql, _progress(applied=18, total=20)
    )
    assert entry["reconcilable"] is False
    assert entry["reconcilable_with_force"] is False
    assert "no tiene manifiesto" in entry["reason"]
    assert entry["statements_to_undo"] == 0


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


# --------------------------------------------------------------------------- #
# DDL transaccional (PostgreSQL): el estado parcial deja de existir            #
# --------------------------------------------------------------------------- #
def test_postgresql_uses_transactional_ddl():
    """
    Diferencia de motor más importante del módulo: PostgreSQL ejecuta DDL transaccional,
    así que una migración que falla a mitad se deshace SOLA y nunca hay estado parcial.
    """
    assert MigrationRunner().use_transactional_ddl(EngineType.postgresql, [_spec()]) is True


def test_mysql_family_never_uses_transactional_ddl():
    """MySQL/MariaDB hacen COMMIT IMPLÍCITO en cada DDL: la atomicidad es imposible."""
    runner = MigrationRunner()
    assert runner.use_transactional_ddl(EngineType.mysql, [_spec()]) is False
    assert runner.use_transactional_ddl(EngineType.mariadb, [_spec()]) is False


def test_statements_postgresql_cannot_run_in_a_transaction_disable_the_mode():
    """
    Fail-safe: si el SQL trae algo que PostgreSQL no admite en una transacción, se cae al
    modo AUTOCOMMIT histórico en vez de abortar con "cannot run inside a transaction block".
    """
    runner = MigrationRunner()
    for sql in (
        "CREATE INDEX CONCURRENTLY ix ON t (a)",
        "DROP INDEX CONCURRENTLY ix",
        "VACUUM FULL t",
        "ALTER SYSTEM SET work_mem = '8MB'",
        # PostgreSQL 12+ lo permite en una transacción, pero el valor nuevo no se puede
        # USAR ahí mismo: se excluye por prudencia.
        "ALTER TYPE mi_enum ADD VALUE 'z'",
    ):
        spec = _spec(up_sql=sql, up_sql_mysql=sql, up_sql_postgresql=sql, manifest=())
        assert runner.use_transactional_ddl(EngineType.postgresql, [spec]) is False, sql


def test_one_bad_statement_in_any_migration_disables_the_mode():
    """Conservador: la decisión es por operación, no por migración."""
    bad = "CREATE INDEX CONCURRENTLY ix ON a (id)"
    specs = [
        _spec(),
        _spec(up_sql=bad, up_sql_mysql=bad, up_sql_postgresql=bad, manifest=(), version="0004"),
    ]
    assert MigrationRunner().use_transactional_ddl(EngineType.postgresql, specs) is False


def test_transactional_mode_disables_the_statement_checkpoint(tmp_path):
    """
    NO es una optimización, es CORRECCIÓN: el checkpoint se graba en la BD del gateway
    (otra conexión, otro commit). Si la transacción de la migración se deshace en el motor
    destino, un checkpoint sobreviviente afirmaría "10 sentencias aplicadas" sobre una BD
    virgen y el resume arrancaría en la 11.
    """
    runner = MigrationRunner()
    versions = tmp_path / "versions"
    versions.mkdir()
    # Spec PINNEADO a PostgreSQL: con source_engine='mysql' el manifiesto se descarta
    # (correcto) y el SQL se transpila, así que el conteo de sentencias no sería el del
    # manifiesto sino el del splitter sobre el texto traducido.
    up = ";\n".join(up for up, _ in _STATEMENTS)
    pg = _spec(source_engine="postgresql", up_sql_postgresql=up)
    runner._write_revision_files(
        versions, [pg], EngineType.postgresql, 999, transactional=True
    )
    body = (versions / "rev_0003.py").read_text(encoding="utf-8")
    # Ni una sola llamada al checkpoint: es lo que se está verificando.
    assert "migration_progress" not in body
    # Una sentencia del manifiesto = un exec_driver_sql en upgrade() (el downgrade tiene los
    # suyos, así que se cuenta solo en la parte de upgrade).
    upgrade_body = body.split("def downgrade():")[0]
    assert upgrade_body.count("op.get_bind().exec_driver_sql(") == 4, upgrade_body


# --------------------------------------------------------------------------- #
# Traducción MySQL -> PostgreSQL: DDL que sqlglot dejaba inválido              #
# --------------------------------------------------------------------------- #
def test_mysql_only_ddl_is_rewritten_to_valid_postgresql():
    """
    sqlglot transpila expresiones y tipos, pero emitía VERBATIM el DDL de MySQL al escribir
    PostgreSQL: ``DROP INDEX i ON t`` (PG no acepta ON) y ``DROP FOREIGN KEY``/``INDEX``/
    ``CHECK`` (PG usa ``DROP CONSTRAINT``). El resultado solo fallaba contra el motor.
    """
    from app.services.db_admin.sql_dialect import SqlTranslator

    t = SqlTranslator()
    assert t.translate("DROP INDEX `ix` ON `c`", EngineType.postgresql) == 'DROP INDEX "ix"'
    assert (
        t.translate("ALTER TABLE `t` DROP FOREIGN KEY `fk`", EngineType.postgresql)
        == 'ALTER TABLE "t" DROP CONSTRAINT "fk"'
    )
    assert (
        t.translate("ALTER TABLE `t` DROP INDEX `uq`", EngineType.postgresql)
        == 'ALTER TABLE "t" DROP CONSTRAINT "uq"'
    )
    # DROP CHECK cae en el parser opaco de sqlglot y dejaba los backticks intactos.
    assert (
        t.translate("ALTER TABLE `t` DROP CHECK `ck`", EngineType.postgresql)
        == 'ALTER TABLE "t" DROP CONSTRAINT "ck"'
    )


def test_rewrites_do_not_cascade_between_statements():
    """
    Regresión: aplicar las reglas sobre el script COMPLETO hacía que la segunda pisara el
    resultado de la primera (``DROP INDEX i ON t`` terminaba como ``DROP CONSTRAINT i``).
    Se aplican por sentencia y con contexto de ``ALTER TABLE``.
    """
    from app.services.db_admin.sql_dialect import SqlTranslator

    out = SqlTranslator().translate(
        "DROP INDEX `ix` ON `t`;\nALTER TABLE `t` DROP FOREIGN KEY `fk`",
        EngineType.postgresql,
    )
    assert out == 'DROP INDEX "ix";\nALTER TABLE "t" DROP CONSTRAINT "fk"'


def test_untranslatable_mysql_ddl_is_reported_as_blocking():
    """
    Lo que NO tiene traducción exacta se REPORTA en vez de emitirse roto: ``MODIFY COLUMN``
    hay que partirlo semánticamente y ``DROP PRIMARY KEY`` necesita el nombre del
    constraint. Devolver None no alcanzaba: el llamador caía al ``up_sql`` en dialecto
    MySQL crudo, igual de inválido contra PostgreSQL.
    """
    from app.services.db_admin.sql_dialect import SqlTranslator

    t = SqlTranslator()
    assert t.translation_blockers(
        "ALTER TABLE `t` MODIFY COLUMN `a` INT NOT NULL", EngineType.postgresql
    )
    assert t.translation_blockers("ALTER TABLE `t` DROP PRIMARY KEY", EngineType.postgresql)
    assert t.translation_blockers(
        "CREATE TABLE `t` (`id` INT) ENGINE=InnoDB", EngineType.postgresql
    )
    # Lo traducible NO se bloquea.
    assert not t.translation_blockers("DROP INDEX `ix` ON `c`", EngineType.postgresql)
    assert not t.translation_blockers(
        "ALTER TABLE `t` ADD COLUMN `a` INT NOT NULL DEFAULT 0", EngineType.postgresql
    )
    # Con MySQL/MariaDB como destino nunca hay nada que traducir ni bloquear.
    assert not t.translation_blockers(
        "ALTER TABLE `t` MODIFY COLUMN `a` INT", EngineType.mysql
    )


def test_postgresql_serial_columns_are_rendered_as_serial():
    """
    Una columna ``serial`` tiene su secuencia POSEÍDA (``pg_depend.deptype='a'``), que el
    snapshot excluye a propósito. Emitir ``DEFAULT nextval('t_id_seq')`` referenciaba una
    secuencia que nunca se crea -> ``relation "t_id_seq" does not exist`` en el primer
    CREATE TABLE. Rompía el clon PG->PG de cualquier tabla con ``id serial primary key``.
    """
    from app.services.db_admin.dtos import ColumnInfo
    from app.services.db_admin.postgres_adapter import PostgresAdapter

    class _T:
        host, port, username, password, engine = "h", 1, "u", "p", "postgresql"

    ad = PostgresAdapter(_T())
    nextval = "nextval('t_id_seq'::regclass)"
    assert ad._render_column_def(
        ColumnInfo(name="id", type="integer", nullable=False, default=nextval)
    ) == '"id" SERIAL'
    assert ad._render_column_def(
        ColumnInfo(name="id", type="bigint", nullable=False, default=nextval)
    ) == '"id" BIGSERIAL'
    # Un default normal no se toca.
    assert ad._render_column_def(
        ColumnInfo(name="n", type="integer", nullable=False, default="0")
    ) == '"n" integer DEFAULT 0 NOT NULL'
    # Límite conocido: NULLABLE con nextval no se convierte (SERIAL implica NOT NULL y
    # cambiar la nullabilidad en silencio sería peor).
    assert "SERIAL" not in ad._render_column_def(
        ColumnInfo(name="id", type="integer", nullable=True, default=nextval)
    )


# --------------------------------------------------------------------------- #
# Checkpoint por SENTENCIA completa en la reconciliación (reversos multi-parte) #
# --------------------------------------------------------------------------- #
def test_reconcile_checkpoint_decrements_only_after_the_whole_reverse_of_a_seq():
    """
    El reverso de una redefinición son DOS sentencias con el MISMO seq (DROP nuevo;
    CREATE viejo). Decrementar el checkpoint tras la primera afirmaría "la sentencia
    quedó deshecha" con el reverso a medias: si la segunda falla y se reintenta, la
    mitad restante se saltearía en silencio. El checkpoint debe moverse recién cuando
    TODO el grupo del seq terminó.
    """
    from unittest import mock

    from app.services.db_admin import migrations as mig_mod

    recorded: list[tuple] = []
    cleared: list[tuple] = []

    class _FakeConn:
        def __init__(self):
            self.executed: list[str] = []

        def execution_options(self, **kw):
            return self

        def exec_driver_sql(self, sql):
            # El lock (GET_LOCK / RELEASE_LOCK) responde 1; el 2º reverso del seq 4 falla.
            if "GET_LOCK" in sql or "RELEASE_LOCK" in sql:
                return mock.Mock(scalar=lambda: 1)
            self.executed.append(sql)
            if sql == "CREATE INDEX viejo":
                raise RuntimeError("boom")
            return mock.Mock()

    fake_conn = _FakeConn()

    class _Ctx:
        def __enter__(self):
            return fake_conn

        def __exit__(self, *a):
            return False

    spec = _spec()
    with mock.patch.object(mig_mod, "database_connection", lambda *a, **k: _Ctx()), \
         mock.patch.object(mig_mod.migration_progress, "record_statement",
                           side_effect=lambda *a: recorded.append(a)), \
         mock.patch.object(mig_mod.migration_progress, "clear_progress",
                           side_effect=lambda *a: cleared.append(a)):
        results = MigrationRunner().reconcile_partial(
            mock.Mock(),  # target (no se usa: database_connection está mockeada)
            db_name="db", engine=EngineType.mysql, managed_db_id=9, spec=spec,
            # seq 4 tiene reverso de DOS sentencias; la segunda REVIENTA.
            inverses=[(4, "DROP INDEX nuevo"), (4, "CREATE INDEX viejo"),
                      (3, "DROP TABLE `b`")],
            total_statements=4,
        )

    # La primera mitad del seq 4 corrió, la segunda falló, el seq 3 nunca se intentó.
    assert [r.status for r in results] == ["applied", "failed"]
    # CRÍTICO: el checkpoint NO se movió (el seq 4 no terminó) — un reintento vuelve a
    # deshacer el seq 4 completo en vez de saltearlo a medias.
    assert recorded == [], recorded
    assert cleared == [], cleared


def test_reconcile_checkpoint_moves_once_per_seq_when_all_parts_succeed():
    from unittest import mock

    from app.services.db_admin import migrations as mig_mod

    recorded: list[tuple] = []
    cleared: list[tuple] = []

    class _FakeConn:
        def execution_options(self, **kw):
            return self

        def exec_driver_sql(self, sql):
            return mock.Mock(scalar=lambda: 1)

    class _Ctx:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    spec = _spec()
    with mock.patch.object(mig_mod, "database_connection", lambda *a, **k: _Ctx()), \
         mock.patch.object(mig_mod.migration_progress, "record_statement",
                           side_effect=lambda *a: recorded.append(a)), \
         mock.patch.object(mig_mod.migration_progress, "clear_progress",
                           side_effect=lambda *a: cleared.append(a)):
        results = MigrationRunner().reconcile_partial(
            mock.Mock(), db_name="db", engine=EngineType.mysql, managed_db_id=9,
            spec=spec,
            inverses=[(2, "DROP X"), (2, "CREATE Y"), (1, "DROP Z")],
            total_statements=4,
        )

    assert all(r.status == "applied" for r in results)
    # UN movimiento de checkpoint por seq (no por sub-sentencia): 2→1, y al llegar a 0
    # se limpia en vez de grabar.
    assert [a[3] for a in recorded] == [1], recorded  # solo seq-1 = 1
    assert len(cleared) == 1


# --------------------------------------------------------------------------- #
# Guards de dirección CRUZADA (apply con rollback parcial, stamp con ambos)     #
# --------------------------------------------------------------------------- #
def test_apply_is_blocked_while_a_rollback_is_partially_executed():
    """
    Simétrico de ROB2: si el downgrade de N falló a mitad, el ledger sigue en N pero la BD
    tiene ALGUNOS reversos ejecutados. Un apply ahí lee current=N y congela la versión a
    medio deshacer para siempre. La salida es reintentar el rollback (retoma del checkpoint).
    """
    from unittest import mock

    from app.controllers import managed_migration_controller as ctrl_mod
    from app.exceptions import AppHttpException

    row = {"model_migration_id": 7, "last_statement_index": 2, "total_statements": 4}

    def fake_incomplete(db_id, direction="up"):
        return [row] if direction == "down" else []

    with mock.patch.object(
        ctrl_mod.migration_progress, "incomplete_progress_for_database",
        side_effect=fake_incomplete,
    ):
        try:
            ctrl_mod.ManagedMigrationController._guard_partial_down_before_apply(
                1, [_spec()]
            )
            raise AssertionError("debía bloquear con 409")
        except AppHttpException as exc:
            assert exc.status_code == 409
            assert "ROLLBACK parcialmente ejecutado" in exc.message


def test_stamp_guard_detects_partials_in_both_directions():
    """El stamp enmascara igual un apply a medias que un rollback a medias."""
    from unittest import mock

    from app.controllers import managed_migration_controller as ctrl_mod
    from app.exceptions import AppHttpException

    row = {"model_migration_id": 7, "last_statement_index": 1, "total_statements": 3}

    for partial_direction in ("up", "down"):
        def fake_incomplete(db_id, direction="up", _pd=partial_direction):
            return [dict(row)] if direction == _pd else []

        with mock.patch.object(
            ctrl_mod.migration_progress, "incomplete_progress_for_database",
            side_effect=fake_incomplete,
        ):
            try:
                ctrl_mod.ManagedMigrationController._guard_partial_checkpoint(1, force=False)
                raise AssertionError(f"debía bloquear con 409 (partial {partial_direction})")
            except AppHttpException as exc:
                assert exc.status_code == 409
