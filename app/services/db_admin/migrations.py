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
from app.services.db_admin.sql_dialect import (
    RollbackGenerator,
    SqlTranslator,
    split_sql_statements,
)

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
    """Nombre de la tabla de versión Alembic en la BD destino: ``_gw_v_{slug}``."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in slug.lower())
    return f"_gw_v_{safe}"[:64]


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
        for spec in sorted(specs, key=lambda s: s.version):
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
        """Migraciones con version > current (y <= up_to_version), ordenadas asc."""
        out = []
        for spec in sorted(specs, key=lambda s: s.version):
            if current is not None and spec.version <= current:
                continue
            if up_to_version is not None and spec.version > up_to_version:
                continue
            out.append(spec)
        return out

    # ------------------------------------------------------------------ #
    # Locking (advisory) en la BD destino                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _lock_key(managed_db_id: int) -> int:
        return managed_db_id

    def _acquire_lock(self, conn, engine: EngineType, managed_db_id: int) -> None:
        if engine == EngineType.postgresql:
            conn.exec_driver_sql(f"SELECT pg_advisory_lock({self._lock_key(managed_db_id)})")
        else:
            conn.exec_driver_sql(
                f"SELECT GET_LOCK('gw_migrate_{managed_db_id}', 30)"
            )

    def _release_lock(self, conn, engine: EngineType, managed_db_id: int) -> None:
        try:
            if engine == EngineType.postgresql:
                conn.exec_driver_sql(
                    f"SELECT pg_advisory_unlock({self._lock_key(managed_db_id)})"
                )
            else:
                conn.exec_driver_sql(f"SELECT RELEASE_LOCK('gw_migrate_{managed_db_id}')")
        except SQLAlchemyError:
            logger.warning("No se pudo liberar el advisory lock de la BD %s", managed_db_id)

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
        version_table = version_table_name(slug)
        results: list[MigrationResult] = []

        with tempfile.TemporaryDirectory(prefix="gw_mig_") as tmp:
            versions_dir = Path(tmp) / "versions"
            versions_dir.mkdir()
            self._write_revision_files(versions_dir, specs, engine)

            try:
                with database_connection(target, db_name) as conn:
                    # AUTOCOMMIT: cada sentencia (DDL, escritura de la tabla de versión,
                    # advisory lock) commitea al instante en MySQL y PostgreSQL. Evita
                    # que el SELECT del advisory lock abra una transacción que Alembic no
                    # commitea y que se perdería al cerrar la conexión. Consistente con
                    # el DDL/DCL remoto del resto del proyecto (_execute_database).
                    conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                    self._acquire_lock(conn, engine, managed_db_id)
                    try:
                        cfg = self._make_config(versions_dir, conn, version_table)
                        current = self._read_current(conn, version_table)
                        pending = self.compute_pending(current, specs, up_to_version)

                        for spec in pending:
                            result = self._apply_one(cfg, spec)
                            results.append(result)
                            if result.status == "failed":
                                break  # no continuar tras un fallo
                    finally:
                        self._release_lock(conn, engine, managed_db_id)
            except SQLAlchemyError as exc:
                raise map_driver_error(
                    exc, op="migration_apply", target=target, extra={"database": db_name}
                )
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

    def rollback_last(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        slug: str,
        engine: EngineType,
        managed_db_id: int,
        specs: list[MigrationSpec],
    ) -> MigrationResult:
        """
        Revierte la última migración aplicada (``downgrade -1``). El llamador debe
        verificar ANTES que esa migración tenga ``down_sql`` confirmado (409 si no).
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
                        current = self._read_current(conn, version_table)
                        if current is None:
                            raise AppHttpException(
                                message="La BD no tiene ninguna migración aplicada.",
                                status_code=409,
                                context={"database": db_name},
                            )
                        spec = next((s for s in specs if s.version == current), None)
                        if spec is None:
                            raise AppHttpException(
                                message="La versión actual de la BD no existe en el blueprint.",
                                status_code=409,
                                context={"database": db_name, "current": current},
                            )
                        cfg = self._make_config(versions_dir, conn, version_table)
                        t0 = time.monotonic()
                        try:
                            with _ALEMBIC_LOCK:
                                command.downgrade(cfg, "-1")
                            ms = int((time.monotonic() - t0) * 1000)
                            return MigrationResult(
                                migration_id=spec.id, version=spec.version,
                                status="applied", error=None, execution_ms=ms,
                                applied_at=datetime.now(timezone.utc),
                            )
                        except Exception as exc:  # noqa: BLE001
                            ms = int((time.monotonic() - t0) * 1000)
                            logger.warning(
                                "Falló el rollback de %s: %s", spec.version, exc,
                                exc_info=True,
                            )
                            return MigrationResult(
                                migration_id=spec.id, version=spec.version,
                                status="failed", error=_clean_error(exc),
                                execution_ms=ms, applied_at=datetime.now(timezone.utc),
                            )
                    finally:
                        self._release_lock(conn, engine, managed_db_id)
            except AppHttpException:
                raise
            except SQLAlchemyError as exc:
                raise map_driver_error(
                    exc, op="migration_rollback", target=target,
                    extra={"database": db_name},
                )

    def stamp(
        self,
        target: ServerTarget,
        *,
        db_name: str,
        slug: str,
        engine: EngineType,
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
        version_table = version_table_name(slug)
        with tempfile.TemporaryDirectory(prefix="gw_mig_") as tmp:
            versions_dir = Path(tmp) / "versions"
            versions_dir.mkdir()
            self._write_revision_files(versions_dir, specs, engine)
            try:
                with database_connection(target, db_name) as conn:
                    conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                    cfg = self._make_config(versions_dir, conn, version_table)
                    with _ALEMBIC_LOCK:
                        command.stamp(cfg, version)
            except SQLAlchemyError as exc:
                raise map_driver_error(
                    exc, op="migration_stamp", target=target, extra={"database": db_name}
                )

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
