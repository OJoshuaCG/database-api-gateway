"""
Tests de la captura de resultados de SELECT dentro de una migración de blueprint
(``app/services/db_admin/migration_results.py`` + el codegen de ``migrations.py``).

Alcance deliberado: funciones PURAS (clasificación, empaquetado/recorte, durabilidad) y
los INVARIANTES del codegen. La persistencia y el descifrado se ejercitan con dobles de
conexión, sin tocar ningún motor remoto: el camino real contra MySQL/MariaDB/PostgreSQL
queda pendiente de verificación e2e con contenedores (no hay Docker en el entorno de dev).

Los cuatro invariantes que este archivo protege —y por qué importan más que la feature en
sí— están enunciados en los nombres de los tests de la sección "Invariantes".
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

import pytest

from app.models.enums import EngineType
from app.services.db_admin import migration_progress, migration_results as mr
from app.services.db_admin.migrations import MigrationResult, MigrationRunner, MigrationSpec


def _spec(version="0001", up_sql="SELECT 1", *, capture=False, down=None):
    return MigrationSpec(
        id=int(version),
        version=version,
        name=f"m{version}",
        up_sql=up_sql,
        up_sql_mysql=None,
        up_sql_postgresql=None,
        down_sql=down,
        checksum="chk",
        capture_selects=capture,
    )


@contextmanager
def _no_checkpoint():
    """Neutraliza el checkpoint (vive en la BD del gateway; estos tests no la usan)."""
    with (
        mock.patch.object(migration_progress, "get_progress", return_value=None),
        mock.patch.object(migration_progress, "record_statement", lambda *a, **k: None),
    ):
        yield


# --------------------------------------------------------------------------- #
# is_capturable (compuerta AST-first, nunca por palabra clave)                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users",
        "select count(*) as c from users where email is null",
        "WITH c AS (SELECT 1 AS a) SELECT * FROM c",
    ],
)
def test_is_capturable_acepta_lectura_pura(sql):
    assert mr.is_capturable(sql, engine="mysql") is True


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users (email) VALUES ('a')",
        "UPDATE users SET email = NULL",
        "DELETE FROM users",
        "ALTER TABLE users ADD COLUMN x INT",
        "CREATE TABLE t (id INT)",
        "GRANT ALL ON db.* TO 'u'@'%'",
        "SET FOREIGN_KEY_CHECKS=0",
        # UNA sentencia del splitter que ARRANCA con CREATE: el pre-filtro la descarta sin
        # necesidad de ningún caso especial para el SELECT de su cuerpo.
        "CREATE PROCEDURE p() BEGIN SELECT 1; END",
        "",
    ],
)
def test_is_capturable_rechaza_todo_lo_que_no_es_lectura(sql):
    assert mr.is_capturable(sql, engine="mysql") is False


def test_is_capturable_rechaza_escritura_disfrazada_de_select():
    """La raíz del AST es ``Select``, pero el CTE ESCRIBE: no se captura."""
    sql = "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d"
    assert mr.is_capturable(sql, engine="postgresql") is False


def test_is_capturable_no_propaga_errores_del_clasificador():
    """Clasificar es una compuerta, no maquinaria: si revienta, no se captura y punto."""
    with mock.patch.object(
        mr.query_policy, "classify_statement", side_effect=RuntimeError("boom")
    ):
        assert mr.is_capturable("SELECT 1", engine="mysql") is False


# --------------------------------------------------------------------------- #
# pack_rows (recorte del lado del gateway, NUNCA del SQL)                       #
# --------------------------------------------------------------------------- #
def test_pack_rows_marca_truncado_con_la_fila_de_mas():
    # fetchmany(max_rows + 1) devolvió 3 con un tope de 2 → sobra, se recorta y se avisa.
    payload = mr.pack_rows(["a"], [[1], [2], [3]], max_rows=2, max_bytes=10_000)
    assert payload.rows == [[1], [2]]
    assert payload.row_count == 2
    assert payload.truncated is True


def test_pack_rows_no_marca_truncado_si_entraba_justo():
    payload = mr.pack_rows(["a"], [[1], [2]], max_rows=2, max_bytes=10_000)
    assert payload.truncated is False
    assert payload.row_count == 2


def test_pack_rows_corta_por_bytes():
    filas = [["x" * 500] for _ in range(20)]
    payload = mr.pack_rows(
        ["blob"], filas, max_rows=100, max_cell_chars=1000, max_bytes=2_000
    )
    assert payload.truncated is True
    assert 0 < payload.row_count < 20
    assert payload.payload_bytes <= 3_000


def test_pack_rows_conserva_la_primera_fila_aunque_exceda_el_tope():
    """Un payload vacío sin explicación es peor que una fila grande ya recortada por celda."""
    payload = mr.pack_rows(
        ["blob"], [["y" * 5_000]], max_rows=10, max_cell_chars=5_000, max_bytes=100
    )
    assert payload.row_count == 1


def test_pack_rows_recorta_por_celda_y_serializa_tipos_del_driver():
    from decimal import Decimal

    payload = mr.pack_rows(
        ["txt", "num", "nulo"],
        [["z" * 50, Decimal("10.50"), None]],
        max_rows=10,
        max_cell_chars=10,
        max_bytes=10_000,
    )
    fila = payload.rows[0]
    assert fila[0].startswith("zzzzzzzzzz") and "truncado" in fila[0]
    # Decimal como str a propósito: un float perdería la precisión que se está inspeccionando.
    assert fila[1] == "10.50"
    assert fila[2] is None


def test_pack_rows_emite_listas_no_dicts():
    """Una consulta puede repetir nombres de columna; un dict perdería una."""
    payload = mr.pack_rows(["id", "id"], [[1, 2]], max_rows=10, max_bytes=10_000)
    assert payload.columns == ["id", "id"]
    assert payload.rows == [[1, 2]]


# --------------------------------------------------------------------------- #
# Durabilidad y texto persistido                                               #
# --------------------------------------------------------------------------- #
def test_finalize_status_por_modo_de_persistencia():
    # AUTOCOMMIT (MySQL/MariaDB): ya está escrito y commiteado en el destino.
    assert mr.finalize_status(buffered=False, committed=None) == mr.DURABILITY_COMMITTED
    # Transaccional (PostgreSQL): según el desenlace real de la transacción.
    assert mr.finalize_status(buffered=True, committed=True) == mr.DURABILITY_COMMITTED
    assert mr.finalize_status(buffered=True, committed=False) == mr.DURABILITY_ROLLED_BACK
    # Fail-closed: sin saberlo, nunca se afirma "committed".
    assert mr.finalize_status(buffered=True, committed=None) == mr.DURABILITY_UNKNOWN


def test_capture_sql_text_redacta_y_recorta():
    texto = mr.capture_sql_text("SELECT 1 /* IDENTIFIED BY 'sup3r' */")
    assert "sup3r" not in texto
    largo = mr.capture_sql_text("SELECT " + "a" * 100_000)
    assert len(largo) <= mr.MIGRATION_CAPTURE_SQL_MAX_CHARS


# --------------------------------------------------------------------------- #
# Ejecución + captura con dobles de conexión                                    #
# --------------------------------------------------------------------------- #
class _Result:
    returns_rows = True

    def __init__(self, columns, rows, *, fail=False):
        self._columns, self._rows, self._fail = columns, rows, fail
        self.closed = False

    def keys(self):
        return self._columns

    def fetchmany(self, n):
        if self._fail:
            raise UnicodeDecodeError("utf-8", b"\xff\xfe-dato-del-cliente", 0, 1, "bad")
        return self._rows[:n]

    def close(self):
        self.closed = True


class _Conn:
    """Doble de conexión. ``error`` simula un fallo de EJECUCIÓN (no de captura)."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.executed = result, error, []

    def exec_driver_sql(self, sql):
        self.executed.append(sql)
        if self.error:
            raise self.error
        return self.result


_CTX = dict(
    managed_database_id=1,
    model_migration_id=2,
    direction="up",
    migration_checksum="chk",
)


def test_capture_statement_escapa_el_porcentaje_al_ejecutar():
    """Mismo criterio que el resto del runner: los drivers pyformat parsean ``%``."""
    conn = _Conn(_Result(["c"], [[1]]))
    with mock.patch.object(mr, "_write_rows"):
        mr.capture_statement(
            conn, "SELECT DATE_FORMAT(d, '%Y') AS c FROM t", statement_index=1,
            buffered=False, **_CTX,
        )
    assert conn.executed == ["SELECT DATE_FORMAT(d, '%%Y') AS c FROM t"]


def test_capture_statement_propaga_un_fallo_de_ejecucion():
    """La migración tiene que fallar exactamente igual que hoy."""
    conn = _Conn(error=RuntimeError("tabla inexistente"))
    with mock.patch.object(mr, "_write_rows") as escribir:
        with pytest.raises(RuntimeError):
            mr.capture_statement(
                conn, "SELECT 1", statement_index=1, buffered=False, **_CTX
            )
    escribir.assert_not_called()


def test_capture_statement_no_aborta_si_falla_la_captura():
    conn = _Conn(_Result(["c"], [[1]], fail=True))
    with mock.patch.object(mr, "_write_rows") as escribir:
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=4, buffered=False, **_CTX)
    (pendientes, durabilidad), _ = escribir.call_args
    entrada = pendientes[0]
    assert entrada.status == mr.STATUS_ERROR
    assert "UnicodeDecodeError" in entrada.error_message
    # NUNCA str(exc): arrastra bytes de una fila del cliente.
    assert "dato-del-cliente" not in entrada.error_message
    assert durabilidad == mr.DURABILITY_COMMITTED


def test_capture_statement_cierra_el_cursor_siempre():
    result = _Result(["c"], [[1]], fail=True)
    with mock.patch.object(mr, "_write_rows"):
        mr.capture_statement(
            _Conn(result), "SELECT c FROM t", statement_index=1, buffered=False, **_CTX
        )
    assert result.closed is True


def test_capture_statement_con_kill_switch_ejecuta_pero_no_guarda():
    conn = _Conn(_Result(["c"], [[1]]))
    with (
        mock.patch.object(mr, "MIGRATION_CAPTURE_ENABLED", False),
        mock.patch.object(mr, "_write_rows") as escribir,
    ):
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=False, **_CTX)
    assert conn.executed  # la sentencia SÍ corrió
    escribir.assert_not_called()


def test_modo_buffer_no_toca_la_bd_del_gateway_hasta_finalize():
    conn = _Conn(_Result(["c"], [[7]]))
    mr.discard_buffer(1, 2, "up")
    with mock.patch.object(mr, "_write_rows") as escribir:
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=True, **_CTX)
        escribir.assert_not_called()  # PostgreSQL: la transacción del destino sigue abierta
        assert mr.buffered_count(1, 2, "up") == 1
        assert mr.finalize(1, 2, "up", committed=False) == 1
        (pendientes, durabilidad), _ = escribir.call_args
    assert durabilidad == mr.DURABILITY_ROLLED_BACK
    assert pendientes[0].rows == [[7]]
    # El buffer queda limpio: un segundo finalize es un no-op.
    assert mr.finalize(1, 2, "up", committed=True) == 0


def test_finalize_no_propaga_un_fallo_de_persistencia():
    conn = _Conn(_Result(["c"], [[1]]))
    mr.discard_buffer(1, 2, "up")
    mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=True, **_CTX)
    with mock.patch.object(mr, "_write_rows", side_effect=RuntimeError("gateway caído")):
        assert mr.finalize(1, 2, "up", committed=True) == 0
    assert mr.buffered_count(1, 2, "up") == 0


def test_captured_at_es_el_momento_de_ejecucion_en_el_destino():
    conn = _Conn(_Result(["c"], [[1]]))
    antes = datetime.now(timezone.utc)
    with mock.patch.object(mr, "_write_rows") as escribir:
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=True, **_CTX)
        mr.finalize(1, 2, "up", committed=True)
    (pendientes, _), _ = escribir.call_args
    assert pendientes[0].captured_at >= antes


# --------------------------------------------------------------------------- #
# Invariantes (lo que no se puede romper al agregar la captura)                 #
# --------------------------------------------------------------------------- #
def test_invariante_1_sin_captura_el_codegen_es_byte_a_byte_el_de_siempre():
    ups = ["SELECT COUNT(*) FROM users", "ALTER TABLE users ADD COLUMN x INT"]
    kwargs = dict(
        managed_db_id=5, migration_id=7, migration_checksum="abc",
        up_resumable=True, down_resumable=False, up_resume_from=0, down_resume_from=0,
    )
    historico = MigrationRunner._render_revision("0002", "0001", ups, [], **kwargs)
    con_flag_apagado = MigrationRunner._render_revision(
        "0002", "0001", ups, [], capture=False, engine="mysql",
        capture_buffered=False, **kwargs,
    )
    assert historico == con_flag_apagado
    assert "migration_results" not in historico


def test_invariante_2_la_captura_no_cambia_la_lista_de_sentencias():
    """Si el conteo cambiara, ``_resolve_resume_offset`` lanzaría un 409 espurio."""
    sql = "SELECT 1;\nALTER TABLE t ADD COLUMN c INT"
    runner = MigrationRunner()
    sin = runner.statement_lists(_spec("0001", sql, capture=False), EngineType.mysql)
    con = runner.statement_lists(_spec("0001", sql, capture=True), EngineType.mysql)
    assert sin == con
    assert len(con[0]) == 2


def test_invariante_3_orden_ejecutar_capturar_luego_checkpoint():
    ups = ["SELECT COUNT(*) FROM users", "ALTER TABLE users ADD COLUMN x INT"]
    body = MigrationRunner._render_revision(
        "0002", None, ups, [], managed_db_id=5, migration_id=7,
        migration_checksum="abc", up_resumable=True, down_resumable=False,
        up_resume_from=0, down_resume_from=0, capture=True, engine="mysql",
        capture_buffered=False,
    )
    lineas = [ln.strip() for ln in body.splitlines() if ln.startswith("    ")]
    assert lineas[0].startswith("migration_results.capture_statement(")
    # El checkpoint se graba DESPUÉS de la sentencia, como siempre.
    assert lineas[1].startswith("migration_progress.record_statement(5, 7, 'up', 1, 2,")
    assert lineas[2].startswith("op.get_bind().exec_driver_sql(")
    assert lineas[3].startswith("migration_progress.record_statement(5, 7, 'up', 2, 2,")
    assert "from app.services.db_admin import migration_results" in body


def test_invariante_4_resume_no_re_ejecuta_ni_re_captura():
    ups = ["SELECT COUNT(*) FROM users", "SELECT 2", "ALTER TABLE t ADD COLUMN c INT"]
    body = MigrationRunner._render_revision(
        "0002", None, ups, [], managed_db_id=5, migration_id=7,
        migration_checksum="abc", up_resumable=True, down_resumable=False,
        up_resume_from=2, down_resume_from=0, capture=True, engine="mysql",
        capture_buffered=False,
    )
    # Las sentencias 1 y 2 ya corrieron en el intento previo: no aparecen (ni su captura).
    assert "capture_statement" not in body
    assert body.count("exec_driver_sql") == 1
    assert "statement_index=1" not in body


def test_el_codegen_bufferiza_en_modo_transaccional():
    body = MigrationRunner._render_revision(
        "0001", None, ["SELECT 1"], [], managed_db_id=5, migration_id=7,
        migration_checksum="abc", up_resumable=False, down_resumable=False,
        up_resume_from=0, down_resume_from=0, capture=True, engine="postgresql",
        capture_buffered=True,
    )
    assert "buffered=True" in body
    # El motor NO viaja a ``capture_statement``: la decisión que depende del dialecto
    # (``is_capturable``) ya la tomó el codegen, y un parámetro muerto en el archivo generado
    # documenta un contrato que no existe.
    assert "engine=" not in body


def test_el_down_sql_tambien_captura():
    body = MigrationRunner._render_revision(
        "0001", None, ["ALTER TABLE t ADD COLUMN c INT"],
        ["SELECT COUNT(*) FROM t", "ALTER TABLE t DROP COLUMN c"],
        managed_db_id=5, migration_id=7, migration_checksum="abc",
        up_resumable=False, down_resumable=False, up_resume_from=0, down_resume_from=0,
        capture=True, engine="mysql", capture_buffered=False,
    )
    assert "direction='down'" in body
    assert "from app.services.db_admin import migration_results" in body


def test_write_revision_files_respeta_el_opt_in_por_migracion():
    import tempfile
    from pathlib import Path

    runner = MigrationRunner()
    sql = "SELECT COUNT(*) FROM users;\nALTER TABLE users ADD COLUMN x INT"
    with tempfile.TemporaryDirectory() as tmp, _no_checkpoint():
        vdir = Path(tmp)
        runner._write_revision_files(
            vdir, [_spec("0001", sql, capture=False)], EngineType.mysql, -1
        )
        sin = (vdir / "rev_0001.py").read_text(encoding="utf-8")
        runner._write_revision_files(
            vdir, [_spec("0002", sql, capture=True)], EngineType.mysql, -1
        )
        con = (vdir / "rev_0002.py").read_text(encoding="utf-8")
    assert "migration_results" not in sin
    assert "migration_results.capture_statement" in con


# --------------------------------------------------------------------------- #
# Guards del controller que NO necesitan la BD del gateway                      #
# --------------------------------------------------------------------------- #
def test_no_queda_ningun_gate_de_consentimiento_por_corrida():
    """
    ANTI-REGRESIÓN. El consentimiento por corrida (``allow_result_capture``) se ELIMINÓ.

    Si este test falla es porque alguien lo repuso "por simetría" o "por seguridad". Antes de
    borrarlo, leé por qué se sacó — está entero en el docstring de ``_capture_versions``:

    - El gateway es **single-admin** (``app/core/auth.py``: "no gestiona múltiples usuarios",
      sin roles ni permisos). La premisa que lo justificaba ("N BDs de dueños distintos, quien
      aplica sobre UNA debe saber") habla de los dueños de las bases DESTINO, no de operadores
      del gateway: no había un segundo par de ojos, solo un segundo momento.
    - **No se auditaba**: pasar el flag no dejaba rastro en ``audit_log``. Lo único auditado es
      la escritura efectiva, que ocurre con o sin gate. Era fricción sin evidencia forense.
    - ``apply_all`` ya lo contradecía: un query param autorizaba N bases de entornos distintos.
    - Una BD nueva recibe la cadena completa, así que el gate saltaba con más fuerza sobre bases
      vacías (donde no hay nada que extraer) que sobre las productivas ⇒ fatiga de consentimiento.

    El gate que SÍ queda es ``_guard_reviewed_capture`` (ver
    ``test_el_gate_de_reviewed_sigue_bloqueando_apply_y_rollback``).
    """
    import inspect

    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.routes.v1 import managed_databases, model_migrations

    assert not hasattr(ManagedMigrationController, "_guard_capture_consent")

    for fn in (
        ManagedMigrationController.apply,
        ManagedMigrationController._run_apply,
        ManagedMigrationController.rollback,
        ManagedMigrationController.apply_all,
        managed_databases.apply_migrations,
        managed_databases.rollback_migration,
        model_migrations.apply_all,
    ):
        assert "allow_result_capture" not in inspect.signature(fn).parameters, fn.__name__


def test_el_gate_de_reviewed_sigue_bloqueando_apply_y_rollback():
    """
    Complemento del anterior: quitar el consentimiento NO puede llevarse por delante el gate
    que queda. Sin esto, "limpiar" el test de arriba podría dejar la captura sin ninguna
    barrera y nada lo diría.
    """
    import inspect

    from app.controllers.managed_migration_controller import ManagedMigrationController

    for fn in (ManagedMigrationController._run_apply, ManagedMigrationController.rollback):
        assert "_guard_reviewed_capture" in inspect.getsource(fn), fn.__name__


def test_el_409_de_reviewed_lleva_code_estable():
    """
    En ``apply_all`` el guard corre por BD dentro del bucle, así que su 409 viaja como ítem de
    una respuesta 200 y el ``public_context`` de la respuesta HTTP no existe para él. El código
    estable es lo único que le queda al cliente para clasificar sin matchear prosa.
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.exceptions import AppHttpException
    from app.services import migration_capture_catalog as ccodes

    class _Sess:
        def query(self, *a):
            return self

        def filter(self, *a):
            return self

        def all(self):
            return [("0010",)]

    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._guard_reviewed_capture(_Sess(), 1)
    assert exc.value.status_code == 409
    assert exc.value.public_context == {
        "code": ccodes.CODE_UNREVIEWED_CAPTURE,
        "unreviewed_capture": ["0010"],
    }


def test_el_catalogo_de_codigos_de_captura_es_cerrado():
    """Todo literal emitido tiene que estar declarado, o el vocabulario deja de serlo."""
    from app.services import migration_capture_catalog as ccodes

    assert ccodes.CODE_UNREVIEWED_CAPTURE in ccodes.ERROR_CODES
    assert ccodes.CODE_UNREVIEWED_CAPTURE_STAMP in ccodes.ERROR_CODES
    # Dos códigos y no uno: en `stamp` el `force=true` SÍ es escape legítimo, así que un código
    # único llevaría a la SPA a ofrecer «Forzar» donde no sirve.
    assert ccodes.CODE_UNREVIEWED_CAPTURE != ccodes.CODE_UNREVIEWED_CAPTURE_STAMP
    assert all(c.startswith("migration.capture_") for c in ccodes.ERROR_CODES)


def test_capture_versions_anuncia_y_respeta_el_kill_switch():
    """La NOTICIA que reemplazó al gate: qué versiones de esta corrida van a capturar."""
    from app.controllers import managed_migration_controller as mod

    ctrl = mod.ManagedMigrationController
    specs = [_spec("0001", capture=True), _spec("0002"), _spec("0003", capture=True)]
    assert ctrl._capture_versions(specs) == ["0001", "0003"]
    assert ctrl._capture_versions([_spec("0002")]) == []

    # Con el kill switch apagado el codegen no emite una sola llamada de captura, así que
    # anunciarla sería mentir.
    with mock.patch.object(mod, "MIGRATION_CAPTURE_ENABLED", False):
        assert ctrl._capture_versions(specs) == []


def test_spec_or_404_compara_versiones_numericamente():
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.exceptions import AppHttpException

    specs = [_spec("0001"), _spec("0002")]
    assert ManagedMigrationController._spec_or_404(specs, "1", 1).version == "0001"
    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._spec_or_404(specs, "0009", 1)
    assert exc.value.status_code == 404


def test_write_revision_files_respeta_el_kill_switch_global():
    import tempfile
    from pathlib import Path

    runner = MigrationRunner()
    with (
        tempfile.TemporaryDirectory() as tmp,
        _no_checkpoint(),
        mock.patch("app.services.db_admin.migrations.MIGRATION_CAPTURE_ENABLED", False),
    ):
        vdir = Path(tmp)
        runner._write_revision_files(
            vdir, [_spec("0001", "SELECT 1", capture=True)], EngineType.mysql, -1
        )
        generado = (vdir / "rev_0001.py").read_text(encoding="utf-8")
    assert "migration_results" not in generado


# --------------------------------------------------------------------------- #
# B1 — el ROLLBACK también captura, y por eso también se controla               #
# --------------------------------------------------------------------------- #
def test_el_rollback_tambien_anuncia_lo_que_captura():
    """
    El codegen emite ``capture_statement`` para el ``down_sql`` (lo verifica
    ``test_el_down_sql_tambien_captura``), así que un ``rollback`` extrae y persiste datos igual
    que un ``apply``. Por eso el aviso —y el gate de ``reviewed``— rigen en AMBAS direcciones.
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController

    camino = [_spec("0002", capture=True), _spec("0001", capture=False)]
    assert ManagedMigrationController._capture_versions(camino) == ["0002"]


def test_rollback_invoca_el_guard_de_captura_antes_de_tocar_el_motor():
    """
    Invariante de ORDEN: el guard va antes de la auditoría de intento y del runner.

    Es lo que mantiene cerrado el agujero B1 —antes el rollback no llamaba a NINGÚN guard y
    bastaba un ``confirm_version`` para exfiltrar filas—. Que el consentimiento por corrida se
    haya retirado no lo reabre: ``_guard_reviewed_capture`` sigue cubriendo esta dirección.
    """
    import inspect

    from app.controllers.managed_migration_controller import ManagedMigrationController

    src = inspect.getsource(ManagedMigrationController.rollback)
    assert "_guard_reviewed_capture" in src
    assert src.index("_guard_reviewed_capture") < src.index('status="attempt"')
    assert src.index("_guard_reviewed_capture") < src.index("self.runner.rollback_to")


def test_endpoint_de_rollback_ya_no_expone_allow_result_capture():
    """Espejo en el borde HTTP del anti-regresión de arriba."""
    import inspect

    from app.routes.v1.managed_databases import rollback_migration

    assert "allow_result_capture" not in inspect.signature(rollback_migration).parameters


class _CountingQuery:
    """Doble mínimo de Query: cuenta llamadas a filter() y devuelve filas fijas."""

    def __init__(self, rows):
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args):
        self.filter_calls += 1
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _CountingSession:
    def __init__(self, rows):
        self.q = _CountingQuery(rows)
        self.queried = False

    def query(self, *args):
        self.queried = True
        return self.q


def test_guard_reviewed_capture_acota_el_chequeo_al_camino_del_rollback():
    """
    El rollback es la salida de RECUPERACIÓN ante una migración mala: bloquearlo por una
    versión FUTURA sin revisar —que no se va a ejecutar— sería quitarle al operador su única
    herramienta. Por eso acepta ``migration_ids`` y ``apply`` sigue mirando el blueprint
    completo.
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.exceptions import AppHttpException

    # Subconjunto vacío: ni se consulta.
    s = _CountingSession([("0009",)])
    ManagedMigrationController._guard_reviewed_capture(s, 7, migration_ids=[])
    assert s.queried is False

    # Subconjunto con contenido: filtro extra por id y 409.
    s = _CountingSession([("0002",)])
    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._guard_reviewed_capture(s, 7, migration_ids=[2])
    assert exc.value.status_code == 409
    assert s.q.filter_calls == 2

    # Sin el parámetro: comportamiento histórico de apply (un solo filter, sin id).
    s = _CountingSession([])
    ManagedMigrationController._guard_reviewed_capture(s, 7)
    assert s.q.filter_calls == 1


def test_stamp_no_puede_marcar_una_version_de_captura_sin_revisar():
    """
    Defensa en profundidad del camino ``crear (reviewed=false) → stamp → rollback``: el stamp
    no ejecuta SQL, pero es lo que HABILITA el rollback de esa versión. ``force=true`` lo
    omite (recuperar una BD que perdió su puntero de versión sigue siendo posible).
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.exceptions import AppHttpException

    s = _CountingSession([(1,)])
    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._guard_stamp_unreviewed_capture(s, 7, "0002", False)
    assert exc.value.status_code == 409
    assert "force=true" in exc.value.message

    s = _CountingSession([(1,)])
    ManagedMigrationController._guard_stamp_unreviewed_capture(s, 7, "0002", True)
    assert s.queried is False  # force ni consulta

    ManagedMigrationController._guard_stamp_unreviewed_capture(
        _CountingSession([]), 7, "0002", False
    )


# --------------------------------------------------------------------------- #
# B3 — el TTL tiene que seguir purgando en un proceso de larga vida             #
# --------------------------------------------------------------------------- #
def test_la_purga_por_ttl_se_repite_mientras_el_proceso_vive():
    """
    La purga del arranque sola convertía ``MIGRATION_CAPTURE_TTL_HOURS`` en una promesa falsa:
    un gateway que corre semanas no volvía a purgar nunca. Se usa ``asyncio.run`` (no un
    plugin async) y un intervalo diminuto: lo que se verifica es el BUCLE, no el reloj.
    """
    import asyncio

    import main

    llamadas: list[int] = []

    async def escenario():
        with mock.patch.object(mr, "purge_expired", lambda ttl: llamadas.append(ttl)):
            task = asyncio.create_task(main._purge_captures_periodically(168, 0.01))
            await asyncio.sleep(0.08)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return task

    task = asyncio.run(escenario())
    assert len(llamadas) >= 2
    assert set(llamadas) == {168}
    # Cancelación LIMPIA: sin esto el intérprete emite "Task was destroyed but it is pending".
    assert task.cancelled()


def test_un_fallo_de_una_pasada_no_mata_el_bucle_de_purga():
    """La retención es best-effort: una BD del gateway momentáneamente caída no puede dejar
    el proceso sin purga hasta el próximo reinicio."""
    import asyncio

    import main

    llamadas: list[int] = []

    def boom(ttl):
        llamadas.append(ttl)
        raise RuntimeError("BD del gateway inalcanzable")

    async def escenario():
        with mock.patch.object(mr, "purge_expired", boom):
            task = asyncio.create_task(main._purge_captures_periodically(1, 0.01))
            await asyncio.sleep(0.08)
            vivo = not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return vivo

    vivo = asyncio.run(escenario())
    assert vivo is True
    assert len(llamadas) >= 2


def test_el_lifespan_cancela_la_tarea_periodica_al_apagar():
    import asyncio

    import main
    from app.core.database import Database
    from app.models import Base

    # El lifespan siembra admin/catálogos: el esquema tiene que existir (este test no usa la
    # fixture ``client``, así que no puede asumir que otro test ya lo creó).
    Base.metadata.create_all(Database().engine)

    async def escenario():
        with mock.patch.object(mr, "purge_expired", lambda ttl: 0):
            async with main.lifespan(main.app):
                dentro = [
                    t for t in asyncio.all_tasks() if t.get_name() == "migration-capture-purge"
                ]
            await asyncio.sleep(0)
            despues = [
                t
                for t in asyncio.all_tasks()
                if t.get_name() == "migration-capture-purge" and not t.done()
            ]
        return len(dentro), len(despues)

    dentro, despues = asyncio.run(escenario())
    assert dentro == 1
    assert despues == 0


# --------------------------------------------------------------------------- #
# Pre-filtro: un SELECT precedido por un COMENTARIO también se captura          #
#                                                                             #
# ``split_sql_statements`` CONSERVA los comentarios dentro de la sentencia que emite (solo
# descarta las que son SOLO comentarios), y una sentencia de verificación real casi siempre
# viene precedida del comentario que explica qué verifica. El pre-filtro anclado en blancos
# rechazaba justo ese caso, en SILENCIO: no se capturaba nada y el endpoint de lectura
# derivaba los ``expected_indexes`` con la MISMA función, así que ni ``missing_indexes``
# denunciaba el problema.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "-- cuantas filas quedan sin backfill\nSELECT COUNT(*) AS n FROM t WHERE c IS NULL",
        "/* verificacion final */\nSELECT COUNT(*) AS n FROM t",
        "/*\n multilinea\n con varias lineas\n*/\nSELECT 1",
        "  \n\t-- uno\n-- dos\n/* tres */  SELECT id FROM users",
        "-- cte\nWITH c AS (SELECT 1 AS a) SELECT * FROM c",
    ],
)
def test_is_capturable_ignora_los_comentarios_iniciales(sql):
    assert mr.is_capturable(sql, engine="mysql") is True
    # El mismo texto vale para PostgreSQL: ``--`` y ``/* */`` son comentarios en los 3 motores.
    assert mr.is_capturable(sql, engine="postgresql") is True


def test_is_capturable_hash_es_comentario_solo_en_mysql():
    """
    ``#`` inicia comentario en MySQL/MariaDB; en PostgreSQL es el operador XOR de enteros.
    Saltearlo en PG trataría código ejecutable como si no existiera (mismo matiz que
    ``query_policy._scan_normalize``).
    """
    sql = "# cuantas quedan\nSELECT COUNT(*) FROM t"
    assert mr.is_capturable(sql, engine="mysql") is True
    assert mr.is_capturable(sql, engine="mariadb") is True
    assert mr.is_capturable(sql, engine="postgresql") is False


def test_is_capturable_con_comentario_no_habilita_lo_que_no_es_lectura():
    """El pre-filtro se relaja para los comentarios, no para el tipo de sentencia."""
    assert mr.is_capturable(
        "-- crea el procedimiento\nCREATE PROCEDURE p() BEGIN SELECT 1; END", engine="mysql"
    ) is False
    assert mr.is_capturable("/* backfill */\nUPDATE t SET c = 0", engine="mysql") is False
    assert mr.is_capturable("-- solo un comentario", engine="mysql") is False


def test_is_capturable_comentario_de_bloque_sin_cerrar_no_se_captura():
    """Fail-closed: si no se puede saltar el comentario, no se captura."""
    assert mr.is_capturable("/* sin cerrar\nSELECT 1", engine="mysql") is False


def test_up_sql_real_con_comentarios_se_captura_de_punta_a_punta():
    """Reproducción del caso reportado, pasando por el splitter real."""
    from app.services.db_admin.sql_dialect import split_sql_statements

    up_sql = (
        "-- cuantas filas quedan sin backfill\n"
        "SELECT COUNT(*) AS n FROM t WHERE c IS NULL;\n"
        "UPDATE t SET c=0 WHERE c IS NULL;\n"
        "/* verificacion final */\n"
        "SELECT COUNT(*) AS n FROM t WHERE c IS NULL"
    )
    statements = split_sql_statements(up_sql)
    assert len(statements) == 3
    capturables = [
        i for i, s in enumerate(statements, start=1) if mr.is_capturable(s, engine="mysql")
    ]
    assert capturables == [1, 3]  # el UPDATE del medio nunca se captura


def test_el_codegen_emite_la_captura_de_un_select_comentado():
    """El invariante que importa: lo que el pre-filtro acepta, el archivo generado captura."""
    body = MigrationRunner._render_revision(
        "0001", None,
        ["-- verificacion\nSELECT COUNT(*) FROM t", "ALTER TABLE t ADD COLUMN c INT"],
        [],
        managed_db_id=5, migration_id=7, migration_checksum="abc",
        up_resumable=False, down_resumable=False, up_resume_from=0, down_resume_from=0,
        capture=True, engine="mysql", capture_buffered=False,
    )
    assert body.count("migration_results.capture_statement(") == 1
    assert body.count("exec_driver_sql(") == 1


# --------------------------------------------------------------------------- #
# Topes: BYTES UTF-8, no caracteres                                            #
# --------------------------------------------------------------------------- #
def test_pack_rows_mide_bytes_utf8_no_caracteres():
    """
    Con contenido CJK/emoji cada carácter pesa 3-4 bytes: medir con ``len()`` sobre el ``str``
    dejaba pasar un payload 3-4× más grande que el tope y reportaba caracteres como bytes.
    """
    filas = [["ñ" * 200] for _ in range(20)]  # 2 bytes por carácter en UTF-8
    payload = mr.pack_rows(
        ["txt"], filas, max_rows=100, max_cell_chars=1000, max_bytes=1_000
    )
    assert payload.truncated is True
    # El tope se respeta en BYTES (con un margen chico por la última fila y el envelope).
    assert len(
        __import__("json").dumps(
            {"columns": payload.columns, "rows": payload.rows}, ensure_ascii=False
        ).encode("utf-8")
    ) == payload.payload_bytes
    assert payload.payload_bytes > len(str(payload.rows))  # multibyte: bytes > caracteres


def test_pack_rows_marca_truncado_si_una_sola_fila_rebasa_el_tope():
    """
    La fila se conserva (un payload vacío sin explicación es peor), pero el presupuesto SE
    REBASÓ: ``truncated`` significa "el resultado real tenía más filas/bytes que los topes".
    """
    payload = mr.pack_rows(
        ["blob"], [["y" * 5_000]], max_rows=10, max_cell_chars=5_000, max_bytes=100
    )
    assert payload.row_count == 1
    assert payload.truncated is True


# --------------------------------------------------------------------------- #
# El puntero cuenta lo que ESTA corrida escribió, no lo que hay en la tabla     #
# --------------------------------------------------------------------------- #
def test_finalize_cuenta_las_escrituras_inmediatas_de_autocommit():
    """
    En MySQL/MariaDB cada captura se escribe al instante, así que el buffer está vacío y
    ``finalize`` devolvía 0: el contador de la corrida es la única fuente honesta.
    """
    conn = _Conn(_Result(["c"], [[1]]))
    mr.discard_buffer(1, 2, "up")
    with mock.patch.object(mr, "_write_rows"):
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=False, **_CTX)
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=2, buffered=False, **_CTX)
    assert mr.finalize(1, 2, "up", committed=True) == 2
    # El contador se consume: una segunda corrida no hereda el número de la anterior.
    assert mr.finalize(1, 2, "up", committed=True) == 0


def test_finalize_no_cuenta_una_escritura_que_fallo():
    conn = _Conn(_Result(["c"], [[1]]))
    mr.discard_buffer(1, 2, "up")
    with mock.patch.object(mr, "_write_rows", side_effect=RuntimeError("gateway caído")):
        mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=False, **_CTX)
    assert mr.finalize(1, 2, "up", committed=True) == 0


def test_begin_barre_un_buffer_huerfano_de_un_intento_previo():
    """Borde de un ``BaseException`` entre una captura y su finalize."""
    conn = _Conn(_Result(["c"], [[1]]))
    mr.discard_buffer(1, 2, "up")
    mr.capture_statement(conn, "SELECT c FROM t", statement_index=1, buffered=True, **_CTX)
    assert mr.buffered_count(1, 2, "up") == 1
    mr.begin(1, 2, "up")
    assert mr.buffered_count(1, 2, "up") == 0
    assert mr.finalize(1, 2, "up", committed=True) == 0


def test_capture_pointer_solo_cuenta_lo_de_esta_corrida():
    """
    Escenario real del defecto: una versión con captura aplicada (1 fila) y luego revertida
    con un ``down_sql`` SIN lecturas. El rollback informaba ``captured_select_count: 1`` y
    auditaba una escritura que nunca ocurrió.
    """
    from app.controllers import managed_migration_controller as mod

    ctrl = mod.ManagedMigrationController
    sin_escritura = [
        MigrationResult(
            migration_id=2, version="0002", status="applied", error=None,
            execution_ms=1, applied_at=datetime.now(timezone.utc), captured_results=0,
        )
    ]
    with mock.patch.object(mod.audit, "record") as auditar:
        assert ctrl._capture_pointer(
            ctrl.__new__(ctrl), 7, sin_escritura, admin=None, server_id=3
        ) == (0, [])
    auditar.assert_not_called()

    con_escritura = [
        MigrationResult(
            migration_id=2, version="0002", status="applied", error=None,
            execution_ms=1, applied_at=datetime.now(timezone.utc), captured_results=2,
        )
    ]
    with mock.patch.object(mod.audit, "record") as auditar:
        assert ctrl._capture_pointer(
            ctrl.__new__(ctrl), 7, con_escritura, admin=None, server_id=3
        ) == (2, ["0002"])
    assert auditar.call_args.args[0] == "migration.select_results.write"
    # El detalle nombra la versión, no solo cuenta: es lo que hace el rastro reconstruible.
    assert "0002" in auditar.call_args.kwargs["detail"]


def test_capture_pointer_nombra_solo_las_versiones_que_escribieron():
    """
    ``captured_versions`` es lo que el cliente necesita para enlazar a
    ``…/{version}/select-results``. Antes adivinaba con ``to_version`` (la última aplicada), así
    que un apply 0005→0010 cuya captura ocurrió en 0007 enlazaba a una página vacía.
    """
    from app.controllers import managed_migration_controller as mod

    ctrl = mod.ManagedMigrationController
    results = [
        MigrationResult(
            migration_id=1, version="0007", status="applied", error=None,
            execution_ms=1, applied_at=datetime.now(timezone.utc), captured_results=3,
        ),
        MigrationResult(
            migration_id=2, version="0010", status="applied", error=None,
            execution_ms=1, applied_at=datetime.now(timezone.utc), captured_results=0,
        ),
    ]
    with mock.patch.object(mod.audit, "record"):
        written, versions = ctrl._capture_pointer(
            ctrl.__new__(ctrl), 7, results, admin=None, server_id=3
        )
    assert written == 3
    # 0010 se aplicó pero NO capturó: enlazar ahí sería el bug que este campo corrige.
    assert versions == ["0007"]


# --------------------------------------------------------------------------- #
# Gate de ``reviewed``: kill switch global y alcance ACOTADO                    #
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Sesión mínima: cuenta si el guard llegó a consultar la BD del gateway."""

    def __init__(self, rows):
        self._rows = rows
        self.queries = 0

    def query(self, *cols):
        self.queries += 1
        return _FakeQuery(self._rows)


def test_guard_reviewed_capture_bloquea_una_version_sin_revisar():
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.exceptions import AppHttpException

    session = _FakeSession([("0010",)])
    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._guard_reviewed_capture(session, 1)
    assert exc.value.status_code == 409
    assert exc.value.public_context == {
        "code": "migration.capture_unreviewed",
        "unreviewed_capture": ["0010"],
    }


def test_guard_reviewed_capture_no_corre_con_el_kill_switch_apagado():
    """
    Con ``MIGRATION_CAPTURE_ENABLED=False`` el codegen no emite una sola llamada a
    ``capture_statement``: capturar es FÍSICAMENTE imposible, así que el 409 solo bloqueaba
    la recuperación — y el ``rollback`` no tiene ningún ``force`` con el que saltearlo.
    """
    from app.controllers import managed_migration_controller as mod

    session = _FakeSession([("0010",)])
    with mock.patch.object(mod, "MIGRATION_CAPTURE_ENABLED", False):
        mod.ManagedMigrationController._guard_reviewed_capture(session, 1)
    assert session.queries == 0  # ni se consulta la BD del gateway


def test_guard_reviewed_capture_con_subconjunto_vacio_no_consulta():
    from app.controllers.managed_migration_controller import ManagedMigrationController

    session = _FakeSession([("0010",)])
    ManagedMigrationController._guard_reviewed_capture(session, 1, migration_ids=[])
    assert session.queries == 0


def test_el_gate_de_reviewed_se_evalua_sobre_las_pendientes_reales():
    """
    Invariante de ALCANCE: ``apply?version=X`` aplica un prefijo ESTRICTO, así que el gate no
    puede mirar todo el blueprint. Se evalúa en ``_run_apply`` (que ya calculó las
    pendientes), lo que además cubre ``apply_all`` con el 409 por BD.
    """
    import inspect

    from app.controllers.managed_migration_controller import ManagedMigrationController

    # No se LLAMA en apply/apply_all (el nombre puede aparecer en un comentario que remite
    # al guard; lo que no puede haber es la invocación sobre todo el blueprint).
    src_apply = inspect.getsource(ManagedMigrationController.apply)
    assert "self._guard_reviewed_capture(" not in src_apply

    src_run = inspect.getsource(ManagedMigrationController._run_apply)
    assert "migration_ids=capture_pending_ids" in src_run
    # ...y sobre las pendientes, no sobre ``specs``.
    assert src_run.index("compute_pending") < src_run.index("_guard_reviewed_capture(")

    src_all = inspect.getsource(ManagedMigrationController.apply_all)
    assert "self._guard_reviewed_capture(" not in src_all
