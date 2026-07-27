"""
MigrationRunner — aplica las migraciones de un blueprint sobre una BD gestionada.

Usa **Alembic como librería embebida** (no como CLI): por cada operación construye
una ``Config`` en memoria que apunta al ``env.py`` compartido
(``migrations/_shared``) e inyecta la conexión a la BD destino. Alembic mantiene la
tabla de versión ``_gw_v_{slug}`` DENTRO de la BD gestionada (idempotente, sobrevive
a caídas) — eso es lo que NO reinventamos.

Diseño:
- **Stateless en disco**: los archivos de revisión ``.py`` se generan en un
  directorio TEMPORAL por operación, con el SQL ya traducido al motor destino. La
  fuente de verdad es ``model_migrations`` (BD del gateway); los archivos son
  derivados reconstituibles. Esto evita estado de filesystem persistente y carreras
  entre motores distintos del mismo blueprint.
- **Aplicación incremental**: se aplica UNA migración por llamada a
  ``command.upgrade`` para obtener tiempo y error por migración, y para detener la
  cadena en la primera que falle.
- **Thread-safety**: el proxy global ``alembic.context`` NO es thread-safe; todas
  las llamadas a ``command.*`` se serializan con ``_ALEMBIC_LOCK``. El fan-out
  masivo real (multiprocessing) se aborda en el Plan 06.
- **Advisory lock** por BD antes de mutar, en la MISMA conexión (lock de sesión que
  sobrevive a los commits de Alembic): evita doble aplicación concurrente.
"""

from __future__ import annotations

import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import SQLAlchemyError

from app.core.logger import get_logger
from app.core.remote_engine import (
    ServerTarget,
    database_connection,
    map_driver_error,
    server_connection,
)
from app.exceptions import AppHttpException
from app.models.enums import EngineType
from app.services.db_admin import migration_progress
from app.services.db_admin.identifiers import GATEWAY_TABLE_PREFIXES
from app.services.db_admin.migration_integrity import validate_version, version_sort_key
from app.services.db_admin.sql_dialect import (
    RollbackGenerator,
    SqlTranslator,
    split_sql_statements,
)

# Timeout (s) que esperan los advisory locks por BD antes de rendirse → 409.
_LOCK_TIMEOUT_S = 30

logger = get_logger(__name__)

# Directorio del env.py compartido (contiene env.py + script.py.mako).
_SHARED_DIR = Path(__file__).resolve().parents[3] / "migrations" / "_shared"

# Alembic usa un EnvironmentContext GLOBAL de módulo: serializamos command.*.
_ALEMBIC_LOCK = threading.Lock()


@dataclass(frozen=True)
class ManifestStatement:
    """
    UNA sentencia del manifiesto de una migración, con su reverso EMPAREJADO.

    Espejo plano de ``model_migration_statements`` (ver el docstring de ese modelo). El
    ``seq`` es 1-based y coincide con el índice del checkpoint
    (``migration_statement_progress.last_statement_index``): ese acople es justamente lo
    que permite saber qué sentencias se aplicaron y cuáles hay que deshacer.
    """

    seq: int
    up_sql: str
    down_sql: str | None = None
    down_confirmed: bool = False
    object_type: str | None = None
    object_name: str | None = None
    destructive: bool = False


@dataclass(frozen=True)
class MigrationSpec:
    """Datos planos de una migración (desacoplados de la sesión ORM)."""

    id: int
    version: str
    name: str
    up_sql: str
    up_sql_mysql: str | None
    up_sql_postgresql: str | None
    down_sql: str | None
    checksum: str
    kind: str = "schema"  # 'schema' | 'data' (los datos no se traducen cross-engine)
    has_non_portable: bool = False  # rutinas/triggers/events no traducibles cross-engine
    source_engine: str | None = None  # motor para el que está renderizado el SQL/manifiesto
    # Manifiesto de sentencias (vacío = no hay; se degrada a partir el blob con el
    # splitter, comportamiento histórico). Solo se usa si ``source_engine`` coincide con
    # el motor destino: el SQL traducido cross-engine puede no partirse igual.
    manifest: tuple[ManifestStatement, ...] = ()

    def manifest_for(self, engine: EngineType) -> tuple[ManifestStatement, ...]:
        """Manifiesto de este motor, o vacío si no hay o es de otro motor."""
        if not self.manifest or not self.source_engine:
            return ()
        if self.source_engine != engine.value:
            return ()
        return self.manifest


@dataclass(frozen=True)
class MigrationResult:
    """Resultado de aplicar/revertir UNA migración."""

    migration_id: int
    version: str
    status: str  # "applied" | "failed"
    error: str | None
    execution_ms: int
    applied_at: datetime
    # Checkpoint por sentencia (ver migration_progress.py): permite reportar si este
    # resultado fue un RESUME (no un intento desde cero) y, ante un fallo, en qué
    # sentencia exacta murió — sin volcar el SQL crudo (puede tener secretos).
    resumed: bool = False
    resumed_from_statement: int | None = None
    statement_total: int | None = None
    failed_at_statement_index: int | None = None


@dataclass(frozen=True)
class StatementResult:
    """Resultado de ejecutar UNA sentencia ad-hoc (Opción B del diff estructural)."""

    index: int
    status: str  # "applied" | "failed"
    error: str | None
    execution_ms: int
    executed_at: datetime


# Prefijo de la tabla de versión, tomado de la lista de objetos internos del gateway
# (``identifiers.GATEWAY_TABLE_PREFIXES``) para que el nombre que se CREA y el que los
# snapshots EXCLUYEN no puedan divergir nunca.
_VERSION_TABLE_PREFIX = GATEWAY_TABLE_PREFIXES[0]  # "_gw_v_"


def version_table_name(slug: str) -> str:
    """
    Nombre de la tabla de versión Alembic en la BD destino: ``_gw_v_{slug}``.

    Truncado a 63 chars: es el límite de identificador de PostgreSQL (NAMEDATALEN-1);
    MySQL/MariaDB admiten 64, así que 63 es seguro en los tres motores y evita que
    PostgreSQL trunque silenciosamente y el nombre deje de coincidir entre escritura
    y lectura.

    El prefijo sale de ``identifiers.GATEWAY_TABLE_PREFIXES``: es la MISMA constante que
    usan los snapshots para excluir esta tabla del diff. Si se cambiara acá sin cambiarla
    allá, el gateway volvería a generar DDL contra su propia contabilidad.
    """
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in slug.lower())
    return f"{_VERSION_TABLE_PREFIX}{safe}"[:63]


class MigrationRunner:
    def __init__(self) -> None:
        self._translator = SqlTranslator()
        self._rollback = RollbackGenerator()

    # ------------------------------------------------------------------ #
    # Selección de SQL por motor                                          #
    # ------------------------------------------------------------------ #
    def select_up_sql(self, spec: MigrationSpec, engine: EngineType) -> str:
        """Override manual si existe; si no, auto-traducción del up_sql base.

        Una migración de DATOS (``kind='data'``) NUNCA se auto-traduce: la sintaxis
        upsert (``ON DUPLICATE KEY UPDATE`` vs ``ON CONFLICT``) no es transpilable con
        seguridad por sqlglot. Se usa el override del motor de origen; aplicarla a otro
        motor lo bloquea antes el guard cross-engine del controller.
        """
        if engine in (EngineType.mysql, EngineType.mariadb):
            return spec.up_sql_mysql or spec.up_sql
        if engine == EngineType.postgresql:
            if spec.up_sql_postgresql:
                return spec.up_sql_postgresql
            if spec.kind == "data":
                return spec.up_sql
            translated = self._translator.translate(spec.up_sql, engine)
            return translated if translated is not None else spec.up_sql
        return spec.up_sql

    def select_down_sql(self, spec: MigrationSpec, engine: EngineType) -> str | None:
        """down_sql confirmado, traducido al motor destino. None si no hay.

        Una migración de DATOS (``kind='data'``) NO se traduce: su ``down_sql`` (DELETE por
        PK) ya está renderizado en el dialecto de origen (identificadores quoteados por
        motor) y sqlglot, leyéndolo como MySQL, malinterpretaría los identificadores PG
        entre comillas dobles como literales de cadena. El guard cross-engine garantiza
        que el destino coincide con ``source_engine``.
        """
        if not spec.down_sql:
            return None
        if engine in (EngineType.mysql, EngineType.mariadb) or spec.kind == "data":
            return spec.down_sql
        translated = self._translator.translate(spec.down_sql, engine)
        return translated if translated is not None else spec.down_sql

    # ------------------------------------------------------------------ #
    # Manifiesto de sentencias (fuente ÚNICA de la lista de sentencias)    #
    # ------------------------------------------------------------------ #
    # El separador con el que ``SchemaComparisonController.adopt_comparison`` ensambla el
    # ``up_sql`` a partir de las sentencias. Reproducirlo es el chequeo de integridad del
    # manifiesto: exacto y SIN pasar por el splitter.
    _MANIFEST_JOIN = ";\n"

    # Sentencias que PostgreSQL NO admite dentro de un bloque de transacción. Si alguna
    # aparece, el modo transaccional se desactiva para TODA la operación (fail-safe): es
    # mejor caer al comportamiento histórico (AUTOCOMMIT + checkpoint) que abortar con
    # "cannot run inside a transaction block".
    #
    # ``ALTER TYPE … ADD VALUE`` se incluye a propósito aunque PostgreSQL 12+ lo permita
    # en una transacción: el valor nuevo NO se puede USAR en la misma transacción, así que
    # una migración que lo agrega y luego lo referencia fallaría. Conservador por diseño.
    _PG_NON_TRANSACTIONAL_RE = re.compile(
        r"\b(?:"
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY"
        r"|DROP\s+INDEX\s+CONCURRENTLY"
        r"|REINDEX\s+\w+\s+CONCURRENTLY"
        r"|VACUUM"
        r"|CREATE\s+DATABASE|DROP\s+DATABASE"
        r"|ALTER\s+SYSTEM"
        r"|CREATE\s+TABLESPACE|DROP\s+TABLESPACE"
        r"|ALTER\s+TYPE\s+[^;]*?\bADD\s+VALUE"
        r")\b",
        re.IGNORECASE,
    )

    def use_transactional_ddl(
        self, engine: EngineType, specs: list[MigrationSpec]
    ) -> bool:
        """
        ¿Se puede aplicar cada migración dentro de UNA transacción (atómica)?

        **Solo PostgreSQL.** Es la diferencia de motor más importante de todo este módulo:
        PostgreSQL ejecuta DDL transaccional, así que una migración de 50 sentencias que
        falla en la 10 **se deshace sola** — no queda estado parcial, el ledger y el plano
        físico nunca divergen, y el ``rollback`` posterior opera sobre un estado conocido.
        MySQL/MariaDB hacen COMMIT IMPLÍCITO en cada DDL: ahí la atomicidad es imposible y
        el checkpoint por sentencia (con su reconciliación) es la única defensa.

        Se desactiva si CUALQUIER migración del blueprint contiene una sentencia que
        PostgreSQL no admite en una transacción (``CREATE INDEX CONCURRENTLY``, ``VACUUM``,
        ``ALTER TYPE … ADD VALUE``, …). Es conservador: se evalúan TODAS las migraciones,
        no solo las pendientes, porque la decisión se toma antes de saber cuáles se van a
        aplicar. Se registra en el log qué versión lo desactivó para que no sea un misterio.
        """
        if engine != EngineType.postgresql:
            return False
        for spec in specs:
            for sql in (self.select_up_sql(spec, engine), self.select_down_sql(spec, engine)):
                if sql and self._PG_NON_TRANSACTIONAL_RE.search(sql):
                    logger.info(
                        "DDL transaccional desactivado: la migración %s tiene una sentencia "
                        "que PostgreSQL no admite dentro de una transacción.",
                        spec.version,
                    )
                    return False
        return True

    def usable_manifest(
        self, spec: MigrationSpec, engine: EngineType
    ) -> tuple[ManifestStatement, ...]:
        """
        Manifiesto que se puede usar con CONFIANZA para este motor, o vacío.

        Verificación de integridad: concatenar las sentencias del manifiesto tiene que
        reproducir EXACTAMENTE el ``up_sql`` vigente para el motor. Es la misma operación
        con la que se construyó (``";\\n".join(...)`` en la adopción), así que es una
        igualdad exacta y —a diferencia de comparar cantidades— no depende del splitter.

        Si no coincide, el ``up_sql`` fue editado sin regenerar el manifiesto (el ``PATCH``
        ya lo borra, esto es la segunda barrera) y el manifiesto NO se usa: se vuelve al
        camino histórico de partir el blob. Fail-closed: un manifiesto desalineado haría
        que una reconciliación deshiciera la sentencia equivocada.
        """
        manifest = spec.manifest_for(engine)
        if not manifest:
            return ()
        expected = self.select_up_sql(spec, engine)
        if self._MANIFEST_JOIN.join(m.up_sql for m in manifest) != expected:
            logger.warning(
                "Manifiesto de la migración %s descartado: no reproduce el up_sql vigente "
                "(¿SQL editado sin regenerarlo?). Se usa el splitter.",
                spec.version,
            )
            return ()
        return manifest

    def statement_lists(
        self, spec: MigrationSpec, engine: EngineType
    ) -> tuple[list[str], list[str], bool]:
        """
        ``(sentencias_up, sentencias_down, pinned)`` para este motor — fuente ÚNICA de la
        lista de sentencias de una migración.

        Existe para que el codegen, el conteo del resultado y la resolución del offset de
        resume vean EXACTAMENTE la misma lista. Cuando cada uno la calculaba por su cuenta,
        una discrepancia de conteo entre manifiesto y splitter disparaba el 409 de
        "checkpoint que ya no coincide" sin que nada hubiera cambiado.

        Con manifiesto: una sentencia por fila (el ``seq`` del manifiesto ES el índice del
        checkpoint, así que NUNCA se re-parte una fila del up). El reverso sí se parte: el
        ``down_sql`` de una redefinición es multi-sentencia (``DROP nuevo; CREATE viejo``)
        y cada ``op.execute`` admite una sola.
        """
        manifest = self.usable_manifest(spec, engine)
        if manifest:
            up_statements = [m.up_sql for m in manifest]
            down_statements = [
                s
                for m in reversed(manifest)
                if m.down_sql
                for s in split_sql_statements(m.down_sql)
            ]
            return up_statements, down_statements, True
        up = self.select_up_sql(spec, engine)
        down = self.select_down_sql(spec, engine)
        return (
            split_sql_statements(up),
            split_sql_statements(down) if down else [],
            False,
        )

    # ------------------------------------------------------------------ #
    # Generación de archivos de revisión (temporales)                    #
    # ------------------------------------------------------------------ #
    def _write_revision_files(
        self,
        versions_dir: Path,
        specs: list[MigrationSpec],
        engine: EngineType,
        managed_db_id: int,
        *,
        transactional: bool = False,
    ) -> None:
        """
        Escribe un .py de Alembic por migración, con el SQL ya por motor.

        Si existe un CHECKPOINT de sentencia válido (mismo ``checksum``) de un
        apply/rollback previo que falló a mitad de camino, el archivo generado incluye
        SOLO las sentencias restantes — un reintento retoma donde quedó en vez de
        re-ejecutar lo que ya commiteó (DDL en AUTOCOMMIT). Ver
        ``migration_progress.is_resumable`` para qué migraciones son elegibles
        (fail-closed: cualquier duda, todo-o-nada, igual que hoy).

        ``transactional=True`` (PostgreSQL) **desactiva el checkpoint por completo**, y no
        es una optimización: es CORRECCIÓN. El checkpoint se graba en la BD del gateway,
        que es otra conexión con su propio commit; si la transacción de la migración se
        deshace en el motor destino, el checkpoint quedaría afirmando "10 sentencias
        aplicadas" cuando en realidad no quedó ninguna. Un resume posterior arrancaría en
        la 11 sobre una BD virgen. Con DDL transaccional no hay estado parcial que
        rastrear, así que no hay nada que grabar.
        """
        prev: str | None = None
        # Orden NUMÉRICO (no lexicográfico): "9999" < "10000" debe respetarse.
        for spec in sorted(specs, key=lambda s: version_sort_key(s.version)):
            # Re-validar version antes de usarla en un path: los datos vienen de la BD
            # del gateway; un tampering directo podría inyectar '../' (anti-traversal).
            validate_version(spec.version)
            up = self.select_up_sql(spec, engine)
            down = self.select_down_sql(spec, engine)
            up_statements, down_statements, pinned = self.statement_lists(spec, engine)

            up_resumable = not transactional and migration_progress.is_resumable(
                up, up_statements, kind=spec.kind, has_non_portable=spec.has_non_portable,
                manifest_pinned=pinned,
            )
            down_resumable = (
                not transactional
                and bool(down_statements)
                and migration_progress.is_resumable(
                    down or "", down_statements, kind=spec.kind,
                    has_non_portable=spec.has_non_portable, manifest_pinned=pinned,
                )
            )
            up_resume_from = self._resolve_resume_offset(
                managed_db_id, spec, "up", up_resumable, len(up_statements)
            )
            down_resume_from = self._resolve_resume_offset(
                managed_db_id, spec, "down", down_resumable, len(down_statements)
            )

            (versions_dir / f"rev_{spec.version}.py").write_text(
                self._render_revision(
                    spec.version, prev, up_statements, down_statements,
                    managed_db_id=managed_db_id,
                    migration_id=spec.id,
                    migration_checksum=spec.checksum,
                    up_resumable=up_resumable, down_resumable=down_resumable,
                    up_resume_from=up_resume_from, down_resume_from=down_resume_from,
                ),
                encoding="utf-8",
            )
            prev = spec.version

    @staticmethod
    def _resolve_resume_offset(
        managed_db_id: int,
        spec: MigrationSpec,
        direction: str,
        resumable: bool,
        total: int,
    ) -> int:
        """
        0 si no hay checkpoint útil (empezar desde la sentencia 1). Si hay un checkpoint
        VÁLIDO (mismo checksum y mismo total de sentencias) y parcial, el índice (1-based)
        de la última sentencia ya ejecutada con éxito.

        Fail-closed: si hay un checkpoint pero su checksum/total ya NO coincide con la
        migración vigente (el SQL fue editado entre el fallo y el reintento — no debería
        poder pasar, el guard de ``ModelMigrationController.update_migration`` lo bloquea,
        pero es una segunda barrera barata), se aborta con 409 en vez de asumir a ciegas
        a qué sentencia corresponde cada índice.
        """
        if not resumable or total == 0:
            return 0
        progress = migration_progress.get_progress(managed_db_id, spec.id, direction)
        if progress is None:
            return 0
        if progress.migration_checksum != spec.checksum or progress.total_statements != total:
            raise AppHttpException(
                message=(
                    f"La migración {spec.version} tiene un checkpoint de aplicación "
                    "parcial que ya no coincide con su SQL actual (fue editado tras un "
                    "fallo, o el checkpoint quedó de una versión anterior de este "
                    "mecanismo). No se puede continuar automáticamente: reconcilie el "
                    "estado físico de la BD manualmente y limpie el checkpoint "
                    "(managed_migration_controller.stamp con force=true) antes de "
                    "reintentar."
                ),
                status_code=409,
                context={
                    "managed_database_id": managed_db_id,
                    "version": spec.version,
                    "direction": direction,
                },
            )
        if 0 < progress.last_statement_index < total:
            return progress.last_statement_index
        return 0

    @staticmethod
    def _render_revision(
        version: str,
        down_revision: str | None,
        up_statements: list[str],
        down_statements: list[str],
        *,
        managed_db_id: int,
        migration_id: int,
        migration_checksum: str,
        up_resumable: bool,
        down_resumable: bool,
        up_resume_from: int,
        down_resume_from: int,
    ) -> str:
        """Genera el cuerpo de un archivo de revisión Alembic con op.execute().

        Cuando ``up_resumable``/``down_resumable``, cada sentencia registra su progreso
        en la BD del gateway (``migration_progress.record_statement``) justo DESPUÉS de
        ejecutarse con éxito — nunca antes: grabar antes haría que un resume saltee una
        sentencia que nunca corrió (corrupción silenciosa), grabar después en el peor
        caso hace que un resume re-intente UNA sentencia ya aplicada (ruidoso, no
        silencioso). Los índices son ABSOLUTOS respecto al total original, aunque el
        archivo generado solo incluya las sentencias posteriores a ``*_resume_from``.
        """
        up_body = MigrationRunner._render_statement_calls(
            up_statements, managed_db_id, migration_id, "up", migration_checksum,
            resumable=up_resumable, resume_from=up_resume_from,
        )

        if not down_statements:
            down_body = (
                "    raise NotImplementedError("
                f"'La migración {version} no tiene rollback (down_sql) confirmado.')"
            )
        else:
            down_body = MigrationRunner._render_statement_calls(
                down_statements, managed_db_id, migration_id, "down", migration_checksum,
                resumable=down_resumable, resume_from=down_resume_from,
            )

        needs_progress_import = up_resumable or down_resumable
        import_line = (
            "from app.services.db_admin import migration_progress\n" if needs_progress_import else ""
        )

        return (
            "from alembic import op\n"
            f"{import_line}\n"
            f"revision = {version!r}\n"
            f"down_revision = {down_revision!r}\n"
            "branch_labels = None\n"
            "depends_on = None\n\n\n"
            "def upgrade():\n"
            f"{up_body}\n\n\n"
            "def downgrade():\n"
            f"{down_body}\n"
        )

    @staticmethod
    def _render_statement_calls(
        statements: list[str],
        managed_db_id: int,
        migration_id: int,
        direction: str,
        migration_checksum: str,
        *,
        resumable: bool,
        resume_from: int,
    ) -> str:
        total = len(statements)
        if total == 0:
            return "    pass"
        lines: list[str] = []
        for i, stmt in enumerate(statements, start=1):
            if i <= resume_from:
                continue  # ya ejecutada en un intento previo (checkpoint confirmado)
            lines.append(f"    op.execute({stmt!r})")
            if resumable:
                lines.append(
                    "    migration_progress.record_statement("
                    f"{managed_db_id!r}, {migration_id!r}, {direction!r}, {i!r}, "
                    f"{total!r}, {migration_checksum!r})"
                )
        return "\n".join(lines) or "    pass"

    def _make_config(self, versions_dir: Path, connection, version_table: str) -> Config:
        cfg = Config()
        cfg.set_main_option("script_location", str(_SHARED_DIR))
        # path_separator=os evita el split legacy por espacios/comas: los paths de
        # los directorios temporales pueden contener espacios (p.ej. en Windows).
        cfg.set_main_option("path_separator", "os")
        cfg.set_main_option("version_locations", str(versions_dir))
        cfg.attributes["connection"] = connection
        cfg.attributes["version_table"] = version_table
        return cfg

    # ------------------------------------------------------------------ #
    # Lectura de versión actual (sin archivos, thread-safe)               #
    # ------------------------------------------------------------------ #
    def get_current_version(self, target: ServerTarget, db_name: str, slug: str) -> str | None:
        """Lee la versión actual de la BD destino desde su tabla ``_gw_v_{slug}``."""
        version_table = version_table_name(slug)
        try:
            with database_connection(target, db_name) as conn:
                ctx = MigrationContext.configure(
                    conn, opts={"version_table": version_table}
                )
                return ctx.get_current_revision()
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="migration_status", target=target, extra={"database": db_name}
            )

    @staticmethod
    def compute_pending(
        current: str | None, specs: list[MigrationSpec], up_to_version: str | None = None
    ) -> list[MigrationSpec]:
        """Migraciones con version > current (y <= up_to_version), ordenadas asc.

        Comparación NUMÉRICA: el orden lexicográfico de strings de ancho variable es
        incorrecto ("10000" < "9999") y provocaría saltar migraciones silenciosamente.
        """
        cur = version_sort_key(current) if current is not None else None
        upto = version_sort_key(up_to_version) if up_to_version is not None else None
        out = []
        for spec in sorted(specs, key=lambda s: version_sort_key(s.version)):
            v = version_sort_key(spec.version)
            if cur is not None and v <= cur:
                continue
            if upto is not None and v > upto:
                continue
            out.append(spec)
        return out

    # ------------------------------------------------------------------ #
    # Locking (advisory) en la BD destino                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _lock_key(lock_key: int) -> int:
        # int() explícito: blinda la interpolación en el SQL del lock aunque un
        # llamador interno futuro pase un str (defensa en profundidad). El valor puede
        # ser un managed_database_id real (positivo) o una clave sintética NEGATIVA de
        # una BD sin gestionar (ver SchemaComparisonController); en ambos casos ya viene
        # como int producido por el propio código, nunca texto de usuario.
        return int(lock_key)

    def _acquire_lock(self, conn, engine: EngineType, lock_key: int) -> None:
        """
        Advisory lock por BD con semántica HOMOGÉNEA entre motores: si no se obtiene
        dentro de ``_LOCK_TIMEOUT_S``, se aborta con 409 (no se bloquea indefinidamente
        ni se asume el lock). MySQL: GET_LOCK con timeout. PostgreSQL: pg_try_advisory_lock
        en sondeo (pg_advisory_lock bloqueante no respeta lock_timeout).
        """
        key = self._lock_key(lock_key)
        if engine == EngineType.postgresql:
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                got = conn.exec_driver_sql(f"SELECT pg_try_advisory_lock({key})").scalar()
                if got:  # True => adquirido
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
            raise self._lock_busy(key)
        # MySQL/MariaDB: GET_LOCK devuelve 1 si lo obtuvo, 0 si expiró, NULL si error.
        got = conn.exec_driver_sql(
            f"SELECT GET_LOCK('gw_migrate_{key}', {_LOCK_TIMEOUT_S})"
        ).scalar()
        if got != 1:
            raise self._lock_busy(key)

    @staticmethod
    def _lock_busy(key: int) -> AppHttpException:
        return AppHttpException(
            message=(
                "No se pudo adquirir el lock de migración de la BD "
                "(¿otra migración en curso?). Reintente más tarde."
            ),
            status_code=409,
            context={"lock_key": key},
        )

    def _release_lock(self, conn, engine: EngineType, lock_key: int) -> None:
        key = self._lock_key(lock_key)
        try:
            if engine == EngineType.postgresql:
                conn.exec_driver_sql(f"SELECT pg_advisory_unlock({key})")
            else:
                conn.exec_driver_sql(f"SELECT RELEASE_LOCK('gw_migrate_{key}')")
        except SQLAlchemyError:
            logger.warning("No se pudo liberar el advisory lock de la BD %s", key)

    # ------------------------------------------------------------------ #
    # Preparación común (tempdir + conexión AUTOCOMMIT + lock + Config)    #
    # ------------------------------------------------------------------ #
    @contextmanager
    def _prepared(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        slug: str,
        engine: EngineType,
        specs: list[MigrationSpec],
        managed_db_id: int,
        op: str,
    ):
        """
        Context manager que centraliza el preámbulo de toda operación del runner:
        genera los archivos de revisión en un tempdir, abre la conexión a la BD destino,
        adquiere el advisory lock por BD y arma la ``Config``. Cede
        ``(conn, cfg, version_table)`` y, al salir, libera el lock y limpia el tempdir.

        **Dos modos según el motor** (ver ``use_transactional_ddl``):

        - **PostgreSQL — TRANSACCIONAL.** El DDL es transaccional, así que cada migración
          corre dentro de su propia transacción (``transaction_per_migration=True`` en el
          ``env.py`` compartido) junto con la escritura de la tabla de versión: si falla en
          la sentencia 10 de 50, PostgreSQL **deshace las 10** y el ledger nunca divergió
          del plano físico. Aquí el advisory lock vive en su **propia sesión** (otra
          conexión): así la transacción de la migración queda limpia y un ROLLBACK no lo
          afecta — los advisory locks de SESIÓN de PostgreSQL sobreviven a COMMIT y a
          ROLLBACK (los de transacción son ``pg_advisory_xact_lock``, que NO se usan).
        - **MySQL/MariaDB — AUTOCOMMIT.** El DDL hace COMMIT IMPLÍCITO: la atomicidad es
          imposible, así que se mantiene el comportamiento histórico (lock en la misma
          conexión) y la defensa es el checkpoint por sentencia + la reconciliación.

        Mapea errores de driver a AppHttpException con el ``op`` correspondiente.
        """
        version_table = version_table_name(slug)
        transactional = self.use_transactional_ddl(engine, specs)
        with tempfile.TemporaryDirectory(prefix="gw_mig_") as tmp:
            versions_dir = Path(tmp) / "versions"
            versions_dir.mkdir()
            self._write_revision_files(
                versions_dir, specs, engine, managed_db_id, transactional=transactional
            )
            try:
                if transactional:
                    with self.advisory_lock(target, engine=engine, lock_key=managed_db_id):
                        with database_connection(target, db_name) as conn:
                            cfg = self._make_config(versions_dir, conn, version_table)
                            yield conn, cfg, version_table
                else:
                    with database_connection(target, db_name) as conn:
                        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                        self._acquire_lock(conn, engine, managed_db_id)
                        try:
                            cfg = self._make_config(versions_dir, conn, version_table)
                            yield conn, cfg, version_table
                        finally:
                            self._release_lock(conn, engine, managed_db_id)
            except AppHttpException:
                raise
            except SQLAlchemyError as exc:
                raise map_driver_error(
                    exc, op=op, target=target, extra={"database": db_name}
                )

    # ------------------------------------------------------------------ #
    # Aplicación de migraciones                                           #
    # ------------------------------------------------------------------ #
    def apply(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        slug: str,
        engine: EngineType,
        managed_db_id: int,
        specs: list[MigrationSpec],
        up_to_version: str | None = None,
    ) -> list[MigrationResult]:
        """
        Aplica las migraciones pendientes en orden. Se detiene en la primera que
        falle (la BD queda en la última versión aplicada con éxito).
        """
        results: list[MigrationResult] = []
        with self._prepared(
            target, db_name=db_name, slug=slug, engine=engine, specs=specs,
            managed_db_id=managed_db_id, op="migration_apply",
        ) as (conn, cfg, version_table):
            current = self._read_current(conn, version_table)
            pending = self.compute_pending(current, specs, up_to_version)
            for spec in pending:
                # MISMA lista que vio el codegen (ver statement_lists): si el conteo
                # difiere, _resolve_resume_offset dispara un 409 espurio.
                up_total = len(self.statement_lists(spec, engine)[0])
                result = self._apply_one(
                    cfg, spec, managed_db_id=managed_db_id, statement_total=up_total
                )
                results.append(result)
                if result.status == "failed":
                    self._discard_failed_transaction(conn)
                    break  # no continuar tras un fallo
        return results

    @staticmethod
    def _discard_failed_transaction(conn) -> None:
        """
        Deja la conexión utilizable después de un fallo (modo transaccional).

        Alembic ya revierte su propia transacción al propagar la excepción; esto es una
        segunda barrera: si quedara una transacción abortada, PostgreSQL rechazaría
        cualquier sentencia posterior con ``current transaction is aborted`` — incluido el
        ``pg_advisory_unlock`` de la limpieza. En AUTOCOMMIT es un no-op.
        """
        try:
            if conn.in_transaction():
                conn.rollback()
        except SQLAlchemyError:
            pass  # la conexión se cierra igual al salir de _prepared

    def _apply_one(
        self, cfg: Config, spec: MigrationSpec, *, managed_db_id: int, statement_total: int
    ) -> MigrationResult:
        """
        Aplica UNA migración. Antes de ejecutar, consulta si hay un checkpoint de
        sentencia (resume de un fallo parcial previo) para reportarlo en el resultado;
        el archivo de revisión que realmente ejecuta ``command.upgrade`` ya fue generado
        (en ``_write_revision_files``) solo con las sentencias restantes si corresponde.
        """
        t0 = time.monotonic()
        pre = migration_progress.get_progress(managed_db_id, spec.id, "up")
        resumed_from = (
            pre.last_statement_index
            if pre
            and pre.migration_checksum == spec.checksum
            and 0 < pre.last_statement_index < statement_total
            else None
        )
        try:
            with _ALEMBIC_LOCK:
                command.upgrade(cfg, spec.version)
            ms = int((time.monotonic() - t0) * 1000)
            # Éxito total de la dirección: el checkpoint ya no hace falta.
            migration_progress.clear_progress(managed_db_id, spec.id, "up")
            return MigrationResult(
                migration_id=spec.id, version=spec.version, status="applied",
                error=None, execution_ms=ms, applied_at=datetime.now(timezone.utc),
                resumed=resumed_from is not None, resumed_from_statement=resumed_from,
                statement_total=statement_total, failed_at_statement_index=None,
            )
        except Exception as exc:  # noqa: BLE001 — registrar fallo y detener cadena
            ms = int((time.monotonic() - t0) * 1000)
            logger.warning("Falló la migración %s: %s", spec.version, exc, exc_info=True)
            post = migration_progress.get_progress(managed_db_id, spec.id, "up")
            failed_at = (
                post.last_statement_index + 1
                if post and post.migration_checksum == spec.checksum
                else None
            )
            return MigrationResult(
                migration_id=spec.id, version=spec.version, status="failed",
                error=_clean_error(exc), execution_ms=ms,
                applied_at=datetime.now(timezone.utc),
                resumed=resumed_from is not None, resumed_from_statement=resumed_from,
                statement_total=statement_total, failed_at_statement_index=failed_at,
            )

    def rollback_to(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        slug: str,
        engine: EngineType,
        managed_db_id: int,
        specs: list[MigrationSpec],
        to_version: str | None,
    ) -> list[MigrationResult]:
        """
        Revierte SECUENCIALMENTE, de la más reciente a la más antigua, todas las
        migraciones aplicadas hasta dejar la BD en ``to_version`` (``None`` = base:
        revierte todas). Análogo a ``apply`` pero hacia atrás: aplica ``downgrade -1``
        repetido y devuelve un resultado por migración revertida. Se detiene en el
        primer fallo (la BD queda en la última versión revertida con éxito).

        El llamador debe validar ANTES que cada migración del camino tenga ``down_sql``
        confirmado (si falta, el ``downgrade()`` generado lanza NotImplementedError).
        """
        results: list[MigrationResult] = []
        to_key = version_sort_key(to_version) if to_version is not None else None
        by_version = {s.version: s for s in specs}
        with self._prepared(
            target, db_name=db_name, slug=slug, engine=engine, specs=specs,
            managed_db_id=managed_db_id, op="migration_rollback",
        ) as (conn, cfg, version_table):
            current = self._read_current(conn, version_table)
            while current is not None and (
                to_key is None or version_sort_key(current) > to_key
            ):
                spec = by_version.get(current)
                mig_id = spec.id if spec else -1
                down_total = (
                    len(self.statement_lists(spec, engine)[1]) if spec else 0
                ) or None
                pre = (
                    migration_progress.get_progress(managed_db_id, mig_id, "down")
                    if spec else None
                )
                resumed_from = (
                    pre.last_statement_index
                    if pre and spec
                    and pre.migration_checksum == spec.checksum
                    and down_total and 0 < pre.last_statement_index < down_total
                    else None
                )
                t0 = time.monotonic()
                try:
                    with _ALEMBIC_LOCK:
                        command.downgrade(cfg, "-1")
                except Exception as exc:  # noqa: BLE001 — registrar fallo y detener
                    ms = int((time.monotonic() - t0) * 1000)
                    logger.warning("Falló el rollback de %s: %s", current, exc, exc_info=True)
                    post = (
                        migration_progress.get_progress(managed_db_id, mig_id, "down")
                        if spec else None
                    )
                    failed_at = (
                        post.last_statement_index + 1
                        if post and spec and post.migration_checksum == spec.checksum
                        else None
                    )
                    results.append(MigrationResult(
                        migration_id=mig_id, version=current, status="failed",
                        error=_clean_error(exc), execution_ms=ms,
                        applied_at=datetime.now(timezone.utc),
                        resumed=resumed_from is not None, resumed_from_statement=resumed_from,
                        statement_total=down_total, failed_at_statement_index=failed_at,
                    ))
                    self._discard_failed_transaction(conn)
                    break
                ms = int((time.monotonic() - t0) * 1000)
                if spec:
                    migration_progress.clear_progress(managed_db_id, mig_id, "down")
                results.append(MigrationResult(
                    migration_id=mig_id, version=current, status="applied",
                    error=None, execution_ms=ms, applied_at=datetime.now(timezone.utc),
                    resumed=resumed_from is not None, resumed_from_statement=resumed_from,
                    statement_total=down_total, failed_at_statement_index=None,
                ))
                new_current = self._read_current(conn, version_table)
                # Salvaguarda anti-bucle: si el puntero no se movió, detener.
                if new_current == current:
                    break
                current = new_current
        return results

    # ------------------------------------------------------------------ #
    # Reconciliación de una aplicación PARCIAL                            #
    # ------------------------------------------------------------------ #
    def reconcile_partial(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        engine: EngineType,
        managed_db_id: int,
        spec: MigrationSpec,
        inverses: list[tuple[int, str]],
        total_statements: int,
    ) -> list[StatementResult]:
        """
        Deshace las sentencias que SÍ se aplicaron de una migración que falló a mitad,
        dejando la BD igual a lo que el ledger de Alembic ya afirma.

        **Por qué existe.** El DDL corre en AUTOCOMMIT (obligado en MySQL/MariaDB, donde no
        es transaccional), y Alembic escribe la versión en ``_gw_v_{slug}`` recién al
        terminar el ``upgrade()``. Si la migración N muere en la sentencia 3 de 50 queda un
        estado partido: el ledger dice "estoy en N-1" y la BD tiene, físicamente, 3
        sentencias de N. Un ``rollback`` en ese estado arranca en N-1 y ejecuta el
        ``down_sql`` de N-1 contra una BD contaminada con parte de N.

        Esto NO es un ``downgrade`` de Alembic: la versión N nunca se aplicó, no hay nada
        que "bajar" en el ledger (y por eso tampoco se lo toca). Es una COMPENSACIÓN —
        ejecutar el reverso exacto de las sentencias 1..k en orden inverso— que vuelve el
        plano físico a coincidir con el ledger.

        ``inverses`` viene ya ordenado del ``seq`` más alto al más bajo, con el ``down_sql``
        de cada sentencia. El checkpoint se DECREMENTA recién cuando TODOS los reversos de
        esa sentencia terminaron — un ``seq`` puede aportar VARIAS entradas (el reverso de
        una redefinición es ``DROP nuevo; CREATE viejo``): decrementar tras la primera
        afirmaría "la sentencia quedó deshecha" con el reverso a medias, y un reintento
        posterior saltearía la mitad restante en silencio. Grabar tarde es seguro: en el
        peor caso se re-intenta un reverso ya hecho (falla ruidosa tipo "no existe"), nunca
        se omite uno pendiente. Se limpia solo al llegar a 0.
        """
        results: list[StatementResult] = []
        # Agrupar los reversos por sentencia de ORIGEN preservando el orden (ya viene
        # seq desc): la unidad de progreso es la SENTENCIA, no cada reverso individual.
        grouped: list[tuple[int, list[str]]] = []
        for seq, sql in inverses:
            if grouped and grouped[-1][0] == seq:
                grouped[-1][1].append(sql)
            else:
                grouped.append((seq, [sql]))
        try:
            with database_connection(target, db_name) as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                self._acquire_lock(conn, engine, managed_db_id)
                try:
                    aborted = False
                    for seq, stmts in grouped:
                        for sql in stmts:
                            t0 = time.monotonic()
                            try:
                                conn.exec_driver_sql(self._escape_percent(sql))
                            except Exception as exc:  # noqa: BLE001 — registrar y detener
                                ms = int((time.monotonic() - t0) * 1000)
                                logger.warning(
                                    "Reconciliación parcial de %s: falló el reverso de la "
                                    "sentencia %d: %s", spec.version, seq, exc, exc_info=True,
                                )
                                results.append(StatementResult(
                                    index=seq, status="failed", error=_clean_error(exc),
                                    execution_ms=ms, executed_at=datetime.now(timezone.utc),
                                ))
                                aborted = True
                                break
                            ms = int((time.monotonic() - t0) * 1000)
                            results.append(StatementResult(
                                index=seq, status="applied", error=None,
                                execution_ms=ms, executed_at=datetime.now(timezone.utc),
                            ))
                        if aborted:
                            break
                        # TODOS los reversos de `seq` commitearon: lo aplicado llega ahora
                        # hasta `seq-1`. Se graba DESPUÉS (nunca antes): re-deshacer es
                        # ruidoso pero visible; saltear un reverso pendiente es silencioso.
                        if seq - 1 <= 0:
                            migration_progress.clear_progress(managed_db_id, spec.id, "up")
                        else:
                            migration_progress.record_statement(
                                managed_db_id, spec.id, "up", seq - 1,
                                total_statements, spec.checksum,
                            )
                finally:
                    self._release_lock(conn, engine, managed_db_id)
        except AppHttpException:
            raise
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="migration_reconcile_partial", target=target,
                extra={"database": db_name},
            )
        return results

    def stamp(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        slug: str,
        engine: EngineType,
        managed_db_id: int,
        specs: list[MigrationSpec],
        version: str,
    ) -> None:
        """Marca la BD destino en ``version`` SIN ejecutar SQL (BDs pre-existentes)."""
        if not any(s.version == version for s in specs):
            raise AppHttpException(
                message="La versión a marcar (stamp) no existe en el blueprint.",
                status_code=422,
                context={"version": version},
            )
        with self._prepared(
            target, db_name=db_name, slug=slug, engine=engine, specs=specs,
            managed_db_id=managed_db_id, op="migration_stamp",
        ) as (_conn, cfg, _vt):
            with _ALEMBIC_LOCK:
                command.stamp(cfg, version)

    # ------------------------------------------------------------------ #
    # Ejecución AD-HOC (Opción B del diff estructural)                    #
    # ------------------------------------------------------------------ #
    @contextmanager
    def advisory_lock(self, target: ServerTarget, *, engine: EngineType, lock_key: int):
        """
        Sostiene el advisory lock por BD durante TODO un bloque (no por sentencia). Se usa
        para operaciones multi-fase que deben ser atómicas frente a otras (p. ej. el
        pipeline de clonación: limpiar → estructura → datos → adopt), donde llamar a
        ``execute_adhoc`` por fase soltaría el lock entre fases.

        Abre una conexión a NIVEL SERVIDOR (no requiere que la BD destino exista todavía:
        ``GET_LOCK``/``pg_advisory_lock`` son globales a la instancia, independientes de la
        BD conectada), adquiere el lock y lo libera al salir. Dentro del bloque, pasar
        ``already_locked=True`` a ``execute_adhoc`` para que NO intente re-adquirir la misma
        clave en su propia sesión (se auto-bloquearía hasta el timeout).
        """
        with server_connection(target) as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            self._acquire_lock(conn, engine, lock_key)
            try:
                yield
            finally:
                self._release_lock(conn, engine, lock_key)

    @staticmethod
    def _escape_percent(stmt: str) -> str:
        """
        Los 3 motores soportados (MySQL/MariaDB vía pymysql, PostgreSQL vía psycopg —
        ambos paramstyle ``pyformat``/``format``) parsean la sentencia buscando
        placeholders ``%s``/``%(name)s`` en cuanto ``cursor.execute`` recibe params NO
        ``None`` — y SQLAlchemy, aun sin bind params, distila ``None`` a una tupla vacía
        (``()``) antes de llegar al DBAPI, así que SIEMPRE entra a ese parseo. Cualquier
        ``%`` LITERAL en el DDL (``GENERATED ... AS (id % 10)``, un ``CHECK``/``DEFAULT``/
        cuerpo de vista con ``LIKE '%...%'`` o ``DATE_FORMAT(..., '%Y-%m-%d')``) revienta
        al ejecutarse via ``exec_driver_sql``: pymysql con ``ValueError: unsupported
        format character``, psycopg con ``ProgrammingError: incomplete placeholder`` /
        ``only '%s', '%b', '%t' are allowed as placeholders`` (verificado invocando el
        parser real de ambos drivers, no solo por lectura de código). Se escapa a ``%%``
        incondicionalmente — es seguro para los 3 motores y no requiere distinguir por
        ``engine``.
        """
        return stmt.replace("%", "%%")

    @staticmethod
    def _toggle_fk_checks(conn, engine: EngineType, *, enabled: bool) -> None:
        """
        Activa/desactiva el chequeo de FKs para la SESIÓN de destino. MySQL/MariaDB usan
        ``FOREIGN_KEY_CHECKS``; PostgreSQL ``session_replication_role`` ('replica' apaga
        triggers/FKs, requiere pseudo-root). Mismo mecanismo que
        ``data_copy.py::_set_fk_enforcement`` para la fase de DATOS del clon — aquí se usa
        para la fase de LIMPIEZA (DROP objeto por objeto, ``clean_mode=objects``).

        El orden topológico inverso de los DROP (``schema_diff.py::order_diff_items``,
        ``_table_dep_order``) ya cubre el caso normal (tabla hija con FK antes que la
        tabla padre), pero por construcción NO puede ver: (1) una FK desde una tabla de
        OTRA base de datos del mismo servidor hacia una tabla de la BD que se limpia (el
        snapshot es de una sola BD) — el candidato más probable dado
        ``(1451, 'Cannot delete or update a parent row...')`` en un DROP TABLE aislado; ni
        (2) un ciclo de FKs dentro de la misma BD (el fallback de ``_table_dep_order`` no
        garantiza orden drop-safe). Desactivar los checks durante la limpieza cubre ambos
        casos sin necesitar ver el resto del servidor. Best-effort: si el SET falla (motor
        sin soporte, o el pseudo-root de PostgreSQL sin permiso), se ignora — el orden
        topológico sigue siendo la garantía primaria para el caso común.
        """
        if engine in (EngineType.mysql, EngineType.mariadb):
            sql = "SET FOREIGN_KEY_CHECKS=1" if enabled else "SET FOREIGN_KEY_CHECKS=0"
        elif engine == EngineType.postgresql:
            sql = (
                "SET session_replication_role = 'origin'" if enabled
                else "SET session_replication_role = 'replica'"
            )
        else:
            return
        try:
            conn.exec_driver_sql(sql)
        except SQLAlchemyError:
            pass

    def execute_adhoc(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        engine: EngineType,
        lock_key: int,
        statements: list[str],
        already_locked: bool = False,
        stop_on_error: bool = True,
        disable_fk_checks: bool = False,
    ) -> list[StatementResult]:
        """
        Ejecuta ``statements`` DDL directamente sobre la BD destino, UNA por una,
        deteniéndose en el primer fallo (igual que ``_apply_one``).

        ``stop_on_error=False`` INTENTA todas las sentencias y devuelve un resultado por
        cada una (aplicada/fallida) sin cortar en el primer fallo. Lo usa el clon para
        ejecutar objetos con cuerpo (vistas/rutinas/triggers/eventos) en pasadas con
        reintento diferido: los que fallan por una dependencia aún no creada se reintentan
        en la pasada siguiente. Cada sentencia es autónoma (AUTOCOMMIT), así que un fallo
        no deja una transacción a medias.

        ``disable_fk_checks=True``: desactiva el chequeo de FKs de la sesión antes de
        ejecutar y lo restaura al final (ver ``_toggle_fk_checks``). Lo usa el clon para
        la fase de limpieza (``clean_mode=objects``): el orden topológico inverso de los
        DROP ya cubre el caso normal, esto es defensa en profundidad para FKs
        cross-database o ciclos que el snapshot de una sola BD no puede ver.

        ``lock_key`` es la clave del advisory lock por BD: para una BD gestionada es su
        ``managed_database_id`` (positivo, comparte lock con ``apply``/``rollback`` de esa
        misma BD); para una BD SIN gestionar es una clave sintética NEGATIVA estable
        derivada de ``(server_id, database_name)`` (ver ``SchemaComparisonController``),
        que nunca colisiona con un id real y serializa ejecuciones concurrentes sobre la
        misma BD física. El controller la resuelve; el runner solo la usa como entero.

        Reutiliza las primitivas ya probadas del runner: conexión en AUTOCOMMIT
        (``database_connection``), advisory lock por BD (``_acquire_lock``/
        ``_release_lock``) para evitar dos ejecuciones concurrentes sobre la misma
        BD, y ``map_driver_error``/``_clean_error`` para no filtrar secretos.

        DIFERENCIA con ``apply``: NO usa Alembic, NO genera archivos de revisión, NO
        toca la tabla de versión ``_gw_v_{slug}`` ni ``database_migration_history``.
        Es DDL derivado de un diff estructural sobre una BD SIN blueprint; ensuciar
        esas estructuras (cuya FK apunta NOT NULL a ``model_migrations``) sería
        incorrecto. El resultado por sentencia lo persiste el controller en
        ``schema_comparison_items``.

        Usa ``exec_driver_sql`` (no ``text()``): el DDL renderizado puede contener
        ``::`` (casts de PostgreSQL) que ``text()`` interpretaría como bind params.
        """
        results: list[StatementResult] = []
        try:
            with database_connection(target, db_name) as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                if not already_locked:
                    self._acquire_lock(conn, engine, lock_key)
                if disable_fk_checks:
                    self._toggle_fk_checks(conn, engine, enabled=False)
                try:
                    for i, stmt in enumerate(statements):
                        t0 = time.monotonic()
                        try:
                            conn.exec_driver_sql(self._escape_percent(stmt))
                            ms = int((time.monotonic() - t0) * 1000)
                            results.append(
                                StatementResult(
                                    index=i, status="applied", error=None,
                                    execution_ms=ms, executed_at=datetime.now(timezone.utc),
                                )
                            )
                        except Exception as exc:  # noqa: BLE001 — registrar y detener
                            ms = int((time.monotonic() - t0) * 1000)
                            # Con reintento diferido (stop_on_error=False) un fallo puede ser
                            # de ORDEN (dependencia aún no creada) y resolverse en otra pasada:
                            # log a debug SIN traceback para no alarmar. Con corte al primer
                            # fallo el error es definitivo → warning con traceback.
                            if stop_on_error:
                                logger.warning(
                                    "execute_adhoc: la sentencia %d falló: %s", i, exc,
                                    exc_info=True,
                                )
                            else:
                                logger.debug(
                                    "execute_adhoc: la sentencia %d falló (reintentable): %s",
                                    i, exc,
                                )
                            results.append(
                                StatementResult(
                                    index=i, status="failed", error=_clean_error(exc),
                                    execution_ms=ms, executed_at=datetime.now(timezone.utc),
                                )
                            )
                            if stop_on_error:
                                break  # no continuar tras un fallo (posible estado parcial)
                finally:
                    if disable_fk_checks:
                        self._toggle_fk_checks(conn, engine, enabled=True)
                    if not already_locked:
                        self._release_lock(conn, engine, lock_key)
        except AppHttpException:
            raise
        except SQLAlchemyError as exc:
            # Fallo ANTES de ejecutar ninguna sentencia (conexión/lock).
            raise map_driver_error(
                exc, op="schema_execute_adhoc", target=target,
                extra={"database": db_name},
            )
        return results

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_current(conn, version_table: str) -> str | None:
        """
        Lee la versión actual y CIERRA la transacción implícita del SELECT.

        En modo transaccional (PostgreSQL) la conexión no está en AUTOCOMMIT, así que este
        SELECT abre una transacción por autobegin. Dejarla abierta le impediría a Alembic
        abrir la suya para la migración —que es justamente lo que da la atomicidad— y en
        ``rollback_to`` (que relee la versión entre downgrades) mantendría una transacción
        viva durante todo el bucle. En AUTOCOMMIT el commit es un no-op inofensivo.
        """
        ctx = MigrationContext.configure(conn, opts={"version_table": version_table})
        current = ctx.get_current_revision()
        if conn.in_transaction():
            conn.commit()
        return current


def _clean_error(exc: Exception) -> str:
    """Mensaje de error compacto y sin secretos para el historial."""
    orig = getattr(exc, "orig", None)
    msg = str(orig) if orig is not None else str(exc)
    return msg[:500]
