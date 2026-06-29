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
from app.core.remote_engine import ServerTarget, database_connection, map_driver_error
from app.exceptions import AppHttpException
from app.models.enums import EngineType
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


@dataclass(frozen=True)
class MigrationResult:
    """Resultado de aplicar/revertir UNA migración."""

    migration_id: int
    version: str
    status: str  # "applied" | "failed"
    error: str | None
    execution_ms: int
    applied_at: datetime


def version_table_name(slug: str) -> str:
    """
    Nombre de la tabla de versión Alembic en la BD destino: ``_gw_v_{slug}``.

    Truncado a 63 chars: es el límite de identificador de PostgreSQL (NAMEDATALEN-1);
    MySQL/MariaDB admiten 64, así que 63 es seguro en los tres motores y evita que
    PostgreSQL trunque silenciosamente y el nombre deje de coincidir entre escritura
    y lectura.
    """
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in slug.lower())
    return f"_gw_v_{safe}"[:63]


class MigrationRunner:
    def __init__(self) -> None:
        self._translator = SqlTranslator()
        self._rollback = RollbackGenerator()

    # ------------------------------------------------------------------ #
    # Selección de SQL por motor                                          #
    # ------------------------------------------------------------------ #
    def select_up_sql(self, spec: MigrationSpec, engine: EngineType) -> str:
        """Override manual si existe; si no, auto-traducción del up_sql base."""
        if engine in (EngineType.mysql, EngineType.mariadb):
            return spec.up_sql_mysql or spec.up_sql
        if engine == EngineType.postgresql:
            if spec.up_sql_postgresql:
                return spec.up_sql_postgresql
            translated = self._translator.translate(spec.up_sql, engine)
            return translated if translated is not None else spec.up_sql
        return spec.up_sql

    def select_down_sql(self, spec: MigrationSpec, engine: EngineType) -> str | None:
        """down_sql confirmado, traducido al motor destino. None si no hay."""
        if not spec.down_sql:
            return None
        if engine in (EngineType.mysql, EngineType.mariadb):
            return spec.down_sql
        translated = self._translator.translate(spec.down_sql, engine)
        return translated if translated is not None else spec.down_sql

    # ------------------------------------------------------------------ #
    # Generación de archivos de revisión (temporales)                    #
    # ------------------------------------------------------------------ #
    def _write_revision_files(
        self, versions_dir: Path, specs: list[MigrationSpec], engine: EngineType
    ) -> None:
        """Escribe un .py de Alembic por migración, con el SQL ya por motor."""
        prev: str | None = None
        # Orden NUMÉRICO (no lexicográfico): "9999" < "10000" debe respetarse.
        for spec in sorted(specs, key=lambda s: version_sort_key(s.version)):
            # Re-validar version antes de usarla en un path: los datos vienen de la BD
            # del gateway; un tampering directo podría inyectar '../' (anti-traversal).
            validate_version(spec.version)
            up = self.select_up_sql(spec, engine)
            down = self.select_down_sql(spec, engine)
            (versions_dir / f"rev_{spec.version}.py").write_text(
                self._render_revision(spec.version, prev, up, down),
                encoding="utf-8",
            )
            prev = spec.version

    @staticmethod
    def _render_revision(
        version: str, down_revision: str | None, up_sql: str, down_sql: str | None
    ) -> str:
        """Genera el cuerpo de un archivo de revisión Alembic con op.execute()."""
        up_calls = "\n".join(
            f"    op.execute({stmt!r})" for stmt in split_sql_statements(up_sql)
        ) or "    pass"

        if down_sql is None:
            down_body = (
                "    raise NotImplementedError("
                f"'La migración {version} no tiene rollback (down_sql) confirmado.')"
            )
        else:
            down_body = "\n".join(
                f"    op.execute({stmt!r})" for stmt in split_sql_statements(down_sql)
            ) or "    pass"

        return (
            "from alembic import op\n\n"
            f"revision = {version!r}\n"
            f"down_revision = {down_revision!r}\n"
            "branch_labels = None\n"
            "depends_on = None\n\n\n"
            "def upgrade():\n"
            f"{up_calls}\n\n\n"
            "def downgrade():\n"
            f"{down_body}\n"
        )

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
    def _lock_key(managed_db_id: int) -> int:
        # int() explícito: blinda la interpolación en el SQL del lock aunque un
        # llamador interno futuro pase un str (defensa en profundidad; el path param
        # de FastAPI ya es int).
        return int(managed_db_id)

    def _acquire_lock(self, conn, engine: EngineType, managed_db_id: int) -> None:
        """
        Advisory lock por BD con semántica HOMOGÉNEA entre motores: si no se obtiene
        dentro de ``_LOCK_TIMEOUT_S``, se aborta con 409 (no se bloquea indefinidamente
        ni se asume el lock). MySQL: GET_LOCK con timeout. PostgreSQL: pg_try_advisory_lock
        en sondeo (pg_advisory_lock bloqueante no respeta lock_timeout).
        """
        key = self._lock_key(managed_db_id)
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
            context={"managed_database_id": key},
        )

    def _release_lock(self, conn, engine: EngineType, managed_db_id: int) -> None:
        key = self._lock_key(managed_db_id)
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
        genera los archivos de revisión en un tempdir, abre la conexión a la BD destino
        en AUTOCOMMIT, adquiere el advisory lock por BD y arma la ``Config``. Cede
        ``(conn, cfg, version_table)`` y, al salir, libera el lock y limpia el tempdir.

        AUTOCOMMIT: cada sentencia (DDL, escritura de la tabla de versión, advisory
        lock) commitea al instante en MySQL y PostgreSQL — evita que el SELECT del lock
        abra una transacción que Alembic no commitea y se perdería al cerrar.

        Mapea errores de driver a AppHttpException con el ``op`` correspondiente.
        """
        version_table = version_table_name(slug)
        with tempfile.TemporaryDirectory(prefix="gw_mig_") as tmp:
            versions_dir = Path(tmp) / "versions"
            versions_dir.mkdir()
            self._write_revision_files(versions_dir, specs, engine)
            try:
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
                result = self._apply_one(cfg, spec)
                results.append(result)
                if result.status == "failed":
                    break  # no continuar tras un fallo
        return results

    def _apply_one(self, cfg: Config, spec: MigrationSpec) -> MigrationResult:
        t0 = time.monotonic()
        try:
            with _ALEMBIC_LOCK:
                command.upgrade(cfg, spec.version)
            ms = int((time.monotonic() - t0) * 1000)
            return MigrationResult(
                migration_id=spec.id, version=spec.version, status="applied",
                error=None, execution_ms=ms, applied_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001 — registrar fallo y detener cadena
            ms = int((time.monotonic() - t0) * 1000)
            logger.warning("Falló la migración %s: %s", spec.version, exc, exc_info=True)
            return MigrationResult(
                migration_id=spec.id, version=spec.version, status="failed",
                error=_clean_error(exc), execution_ms=ms,
                applied_at=datetime.now(timezone.utc),
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
                t0 = time.monotonic()
                try:
                    with _ALEMBIC_LOCK:
                        command.downgrade(cfg, "-1")
                except Exception as exc:  # noqa: BLE001 — registrar fallo y detener
                    ms = int((time.monotonic() - t0) * 1000)
                    logger.warning("Falló el rollback de %s: %s", current, exc, exc_info=True)
                    results.append(MigrationResult(
                        migration_id=mig_id, version=current, status="failed",
                        error=_clean_error(exc), execution_ms=ms,
                        applied_at=datetime.now(timezone.utc),
                    ))
                    break
                ms = int((time.monotonic() - t0) * 1000)
                results.append(MigrationResult(
                    migration_id=mig_id, version=current, status="applied",
                    error=None, execution_ms=ms, applied_at=datetime.now(timezone.utc),
                ))
                new_current = self._read_current(conn, version_table)
                # Salvaguarda anti-bucle: si el puntero no se movió, detener.
                if new_current == current:
                    break
                current = new_current
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
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_current(conn, version_table: str) -> str | None:
        ctx = MigrationContext.configure(conn, opts={"version_table": version_table})
        return ctx.get_current_revision()


def _clean_error(exc: Exception) -> str:
    """Mensaje de error compacto y sin secretos para el historial."""
    orig = getattr(exc, "orig", None)
    msg = str(orig) if orig is not None else str(exc)
    return msg[:500]
