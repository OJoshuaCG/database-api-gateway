"""
Tests unitarios (sin motor) de la clonación de objetos con CUERPO (vistas/rutinas/
triggers/eventos):

1. ``_requalify_body`` — reescribe el calificador de esquema origen→destino en el cuerpo
   (MySQL/MariaDB inyectan el esquema origen en las referencias; sin esto el clon leería
   de la BD origen).
2. ``_run_body_statements`` — ejecución con REINTENTO DIFERIDO: resuelve dependencias
   vista→vista en cualquier orden y marca fallo real solo cuando no hay progreso.
"""

from datetime import datetime, timezone

from app.controllers.clone_controller import CloneController, _StructStmt
from app.services.db_admin.migrations import StatementResult


def _ctrl():
    return CloneController.__new__(CloneController)


# --------------------------------------------------------------------------- #
# _requalify_body                                                             #
# --------------------------------------------------------------------------- #
def test_requalify_rewrites_source_schema_to_target():
    sql = "CREATE OR REPLACE VIEW `v` AS select `id` from `src_db`.`t`"
    out = _ctrl()._requalify_body(sql, "src_db", "dst_db", "mariadb")
    assert "`dst_db`.`t`" in out
    assert "`src_db`" not in out


def test_requalify_noop_when_same_db():
    sql = "select from `src_db`.`t`"
    assert _ctrl()._requalify_body(sql, "src_db", "src_db", "mysql") == sql


def test_requalify_noop_for_postgres():
    sql = "select from public.t"
    assert _ctrl()._requalify_body(sql, "src", "dst", "postgresql") == sql


def test_requalify_preserves_references_to_other_databases():
    # Una referencia intencional a OTRA base no se toca (el backtick delimita el nombre).
    sql = "select a from `src_db`.`t` join `other_db`.`u`"
    out = _ctrl()._requalify_body(sql, "src_db", "dst_db", "mysql")
    assert "`dst_db`.`t`" in out
    assert "`other_db`.`u`" in out  # intacta


def test_requalify_no_prefix_collision():
    # `src_db` no debe reescribir `src_db_backup` (el backtick de cierre lo evita).
    sql = "from `src_db`.`t`, `src_db_backup`.`x`"
    out = _ctrl()._requalify_body(sql, "src_db", "dst_db", "mysql")
    assert "`dst_db`.`t`" in out
    assert "`src_db_backup`.`x`" in out


# --------------------------------------------------------------------------- #
# _run_body_statements — reintento diferido                                   #
# --------------------------------------------------------------------------- #
class _FakeRunner:
    """Runner falso: una sentencia 'CREATE <name> DEPS a,b' aplica solo si TODAS sus
    dependencias ya fueron creadas; si no, falla (simula el orden de dependencias)."""

    def __init__(self, unresolvable: set[str] | None = None):
        self.created: set[str] = set()
        self.unresolvable = unresolvable or set()
        self.passes = 0

    @staticmethod
    def _parse(sql: str):
        # "CREATE <name> DEPS d1,d2"
        _, rest = sql.split("CREATE ", 1)
        name, _, deps_part = rest.partition(" DEPS ")
        deps = {d for d in deps_part.split(",") if d}
        return name.strip(), deps

    def execute_adhoc(self, target, *, db_name, engine, lock_key, statements,
                      already_locked=False, stop_on_error=True):
        self.passes += 1
        out = []
        for i, sql in enumerate(statements):
            name, deps = self._parse(sql)
            if name not in self.unresolvable and deps <= self.created:
                self.created.add(name)
                status, error = "applied", None
            else:
                status, error = "failed", f"depende de {deps - self.created}"
            out.append(StatementResult(index=i, status=status, error=error,
                                       execution_ms=1, executed_at=datetime.now(timezone.utc)))
            if status == "failed" and stop_on_error:
                break
        return out


def _stmt(name, deps=()):
    return _StructStmt("structure", "view", name,
                       f"CREATE {name} DEPS {','.join(deps)}")


def _run(ctrl, runner, statements):
    captured = {}

    def _cap(job_id, rows):
        captured["rows"] = rows

    ctrl._record_items = _cap  # type: ignore[method-assign]
    seq, failed = ctrl._run_body_statements(
        job_id=1, runner=runner, tgt_target=None, db_name="d",
        engine="mariadb", lock_key=1, statements=statements, seq=0,
    )
    return seq, failed, captured["rows"]


def test_deferred_retry_resolves_reverse_order():
    # v_a→v_b→v_c dados en el PEOR orden (dependiente primero). Debe resolverse.
    ctrl = _ctrl()
    runner = _FakeRunner()
    stmts = [_stmt("v_a", {"v_b"}), _stmt("v_b", {"v_c"}), _stmt("v_c")]
    seq, failed, rows = _run(ctrl, runner, stmts)
    assert failed is False
    assert seq == 3
    assert all(r["status"] == "applied" for r in rows)
    assert runner.passes >= 3  # necesitó varias pasadas
    # los ítems se registran en el ORDEN ORIGINAL
    assert [r["object_name"] for r in rows] == ["v_a", "v_b", "v_c"]


def test_deferred_retry_marks_real_failure():
    # v_bad depende de algo que nunca se crea → falla; v_ok se aplica igual.
    ctrl = _ctrl()
    runner = _FakeRunner(unresolvable={"v_bad"})
    stmts = [_stmt("v_ok"), _stmt("v_bad", {"missing"})]
    seq, failed, rows = _run(ctrl, runner, stmts)
    assert failed is True
    by_name = {r["object_name"]: r for r in rows}
    assert by_name["v_ok"]["status"] == "applied"
    assert by_name["v_bad"]["status"] == "failed"
    assert by_name["v_bad"]["error"] is not None


def test_deferred_retry_all_independent_single_pass():
    ctrl = _ctrl()
    runner = _FakeRunner()
    stmts = [_stmt("a"), _stmt("b"), _stmt("c")]
    seq, failed, rows = _run(ctrl, runner, stmts)
    assert failed is False
    assert runner.passes == 1  # sin dependencias: una sola pasada
    assert all(r["status"] == "applied" for r in rows)
