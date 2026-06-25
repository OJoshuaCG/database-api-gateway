"""
Controller de aplicación de migraciones sobre BDs gestionadas (TOCA el motor).

Orquesta el ``MigrationRunner`` (Alembic embebido) y el inventario del gateway:
- ``status``   : versión actual de la BD (leída de ``_gw_v_{slug}``) vs. pendientes.
- ``apply``    : aplica pendientes; registra ``database_migration_history`` y
                 actualiza ``managed_database.model_version``.
- ``rollback`` : revierte la última (409 si la versión actual no tiene ``down_sql``).
- ``stamp``    : marca versión sin ejecutar (BDs pre-existentes).
- ``apply_all``: aplica a TODAS las BDs del blueprint (síncrono, acotado; el job
                 asíncrono real es del Plan 06).

Integridad: antes de tocar el motor se re-valida el ``checksum`` de cada migración
(detecta alteración directa en la BD del gateway).
"""

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType, MigrationStatus, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.services import audit
from app.services.db_admin.migration_integrity import compute_checksum, version_sort_key
from app.services.db_admin.migrations import (
    MigrationResult,
    MigrationRunner,
    MigrationSpec,
)

logger = get_logger(__name__)


class ManagedMigrationController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
        self.runner = MigrationRunner()

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Carga de contexto                                                   #
    # ------------------------------------------------------------------ #
    def _load_context(self, session, db_id: int):
        """Devuelve (managed_db, server, model) validando blueprint asignado."""
        md = session.get(ManagedDatabase, db_id)
        if not md:
            raise AppHttpException(
                message="Base de datos gestionada no encontrada.",
                status_code=404,
                context={"managed_database_id": db_id},
            )
        if md.model_id is None:
            raise AppHttpException(
                message="La BD no tiene un blueprint asignado; nada que migrar.",
                status_code=422,
                context={"managed_database_id": db_id},
            )
        server = get_server_or_404(session, md.server_id)
        model = session.get(DatabaseModel, md.model_id)
        if model is None:
            raise AppHttpException(
                message="El blueprint asignado a la BD ya no existe.",
                status_code=409,
                context={"managed_database_id": db_id, "model_id": md.model_id},
            )
        return md, server, model

    @staticmethod
    def _load_specs(session, model_id: int) -> list[MigrationSpec]:
        rows = (
            session.query(ModelMigration)
            .filter(ModelMigration.model_id == model_id)
            .all()
        )
        specs = [
            MigrationSpec(
                id=r.id,
                version=r.version,
                name=r.name,
                up_sql=r.up_sql,
                up_sql_mysql=r.up_sql_mysql,
                up_sql_postgresql=r.up_sql_postgresql,
                down_sql=r.down_sql,
                checksum=r.checksum,
            )
            for r in rows
        ]
        # Orden NUMÉRICO de versión (no lexicográfico): status/latest dependen de él.
        specs.sort(key=lambda s: version_sort_key(s.version))
        return specs

    @staticmethod
    def _verify_integrity(specs: list[MigrationSpec]) -> None:
        for spec in specs:
            expected = compute_checksum(
                spec.up_sql, spec.up_sql_mysql, spec.up_sql_postgresql,
                spec.down_sql, spec.version,
            )
            if expected != spec.checksum:
                raise AppHttpException(
                    message=(
                        f"Integridad: la migración {spec.version} fue alterada "
                        "(checksum no coincide). Se aborta para no aplicar SQL no verificado."
                    ),
                    status_code=409,
                    context={"version": spec.version},
                )

    # ------------------------------------------------------------------ #
    # Estado                                                              #
    # ------------------------------------------------------------------ #
    def status(self, db_id: int) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            slug = model.slug
            db_name, model_id = md.name, model.id
            target = build_target(server)
        finally:
            session.close()

        current = self.runner.get_current_version(target, db_name, slug)
        latest = specs[-1].version if specs else None
        pending = self.runner.compute_pending(current, specs)
        return {
            "managed_database_id": db_id,
            "model_id": model_id,
            "slug": slug,
            "current_version": current,
            "latest_available": latest,
            "pending_count": len(pending),
            "pending_versions": [s.version for s in pending],
        }

    # ------------------------------------------------------------------ #
    # Aplicación                                                          #
    # ------------------------------------------------------------------ #
    def apply(
        self,
        db_id: int,
        *,
        up_to_version: str | None = None,
        force: bool = False,
        dry_run: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)
            slug, engine = model.slug, EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            quarantined = md.status == ProvisionStatus.error
            target = build_target(server)
        finally:
            session.close()

        if not specs:
            raise AppHttpException(
                message="El blueprint no tiene migraciones definidas.",
                status_code=422,
                context={"model_id": model.id},
            )

        # ROB1 — cuarentena: una migración fallida previa pudo dejar la BD en estado
        # parcial (DDL no transaccional en MySQL). Se exige inspección + force=true.
        self._guard_quarantine(db_id, quarantined, force, dry_run)

        if dry_run:
            return self._dry_run_plan(db_id, db_name, server_id, target, slug, specs, up_to_version)

        return self._run_apply(
            db_id, db_name=db_name, server_id=server_id, target=target,
            engine=engine, slug=slug, specs=specs,
            up_to_version=up_to_version, was_quarantined=quarantined, admin=admin,
        )

    @staticmethod
    def _guard_quarantine(db_id: int, quarantined: bool, force: bool, dry_run: bool) -> None:
        if quarantined and not force and not dry_run:
            raise AppHttpException(
                message=(
                    "La BD está en cuarentena por un fallo de migración previo. "
                    "Inspeccione el estado real y reintente con force=true."
                ),
                status_code=409,
                context={"managed_database_id": db_id, "required": "force=true"},
            )

    def _dry_run_plan(
        self, db_id, db_name, server_id, target, slug, specs, up_to_version
    ) -> dict:
        """Calcula el plan (pendientes) SIN tocar el motor más que para leer la versión."""
        current = self.runner.get_current_version(target, db_name, slug)
        pending = self.runner.compute_pending(current, specs, up_to_version)
        return {
            "managed_database_id": db_id,
            "database_name": db_name,
            "server_id": server_id,
            "dry_run": True,
            "current_version": current,
            "pending_versions": [s.version for s in pending],
            "pending_count": len(pending),
        }

    def _run_apply(
        self, db_id, *, db_name, server_id, target, engine, slug, specs,
        up_to_version, was_quarantined, admin,
    ) -> dict:
        """Ejecuta el apply real sobre UNA BD ya cargada/validada (reutilizable por apply_all)."""
        audit.record(
            "migration.apply", status="attempt", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True, detail=f"apply hasta {up_to_version or 'head'}",
        )
        try:
            results = self.runner.apply(
                target, db_name=db_name, slug=slug, engine=engine,
                managed_db_id=db_id, specs=specs, up_to_version=up_to_version,
            )
        except AppHttpException as exc:
            # Fallo ANTES de aplicar ninguna migración (conexión/lock): no hay
            # resultado por-migración que registrar; dejamos traza en auditoría.
            audit.record(
                "migration.apply", status="error", admin=admin,
                target_type="managed_database", target_id=db_id, server_id=server_id,
                touched_engine=True,
                detail=f"fallo al aplicar (HTTP {getattr(exc, 'status_code', '?')})",
            )
            raise

        self._record_history(db_id, results)
        # model_version se SINCRONIZA releyendo la fuente de verdad (tabla de versión
        # que Alembic mantiene en la BD destino), no la contabilidad local.
        self._sync_model_version_from_engine(db_id, target, db_name, slug)

        failed = any(r.status == "failed" for r in results)
        # ROB1 — marcar/limpiar cuarentena según el desenlace.
        self._set_quarantine(db_id, failed, results)

        audit.record(
            "migration.apply", status="error" if failed else "success", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True,
            detail=f"{sum(1 for r in results if r.status=='applied')} aplicadas"
                   + (" (con fallo)" if failed else ""),
        )
        return {
            "managed_database_id": db_id,
            "database_name": db_name,
            "server_id": server_id,
            "applied_count": sum(1 for r in results if r.status == "applied"),
            "failed": failed,
            "quarantined": failed,
            "results": [self._result_dict(r) for r in results],
        }

    def rollback(
        self, db_id: int, *, confirm_version: str | None = None, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)  # el rollback ejecuta DDL destructivo
            slug, engine = model.slug, EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            target = build_target(server)
        finally:
            session.close()

        # Verificar que la versión ACTUAL tenga rollback confirmado (409 si no).
        current = self.runner.get_current_version(target, db_name, slug)
        if current is None:
            raise AppHttpException(
                message="La BD no tiene ninguna migración aplicada para revertir.",
                status_code=409,
                context={"managed_database_id": db_id},
            )
        # Doble intención para una operación DESTRUCTIVA (puede perder datos): el
        # cliente debe repetir la versión exacta a revertir, igual que confirm_name
        # en DROP DATABASE.
        if confirm_version != current:
            raise AppHttpException(
                message=(
                    "Confirmación requerida: para revertir, 'confirm_version' debe "
                    f"coincidir con la versión actual de la BD ({current})."
                ),
                status_code=422,
                context={"managed_database_id": db_id, "required": "confirm_version == current"},
            )
        spec = next((s for s in specs if s.version == current), None)
        if spec is None or not spec.down_sql:
            raise AppHttpException(
                message=f"La migración {current} no tiene rollback (down_sql) confirmado.",
                status_code=409,
                context={"managed_database_id": db_id, "version": current},
            )

        audit.record(
            "migration.rollback", status="attempt", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True, detail=f"rollback de {current}",
        )
        result = self.runner.rollback_last(
            target, db_name=db_name, slug=slug, engine=engine,
            managed_db_id=db_id, specs=specs,
        )
        # La versión tras el rollback se re-lee del motor (fuente de verdad).
        new_current = self.runner.get_current_version(target, db_name, slug)
        self._set_model_version(db_id, new_current)

        audit.record(
            "migration.rollback",
            status="success" if result.status == "applied" else "error",
            admin=admin, target_type="managed_database", target_id=db_id,
            server_id=server_id, touched_engine=True,
            detail=f"rollback {current} -> {new_current or 'base'} ({result.status})",
        )
        return {
            "managed_database_id": db_id,
            "rolled_back_version": current,
            "current_version": new_current,
            "result": self._result_dict(result),
        }

    def stamp(self, db_id: int, version: str, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)
            slug, engine = model.slug, EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            target = build_target(server)
        finally:
            session.close()

        self.runner.stamp(
            target, db_name=db_name, slug=slug, engine=engine,
            managed_db_id=db_id, specs=specs, version=version,
        )
        self._set_model_version(db_id, version)
        audit.record(
            "migration.stamp", admin=admin, target_type="managed_database",
            target_id=db_id, server_id=server_id, touched_engine=True,
            detail=f"stamp {version}",
        )
        return self.status(db_id)

    def apply_all(
        self,
        model_id: int,
        *,
        max_databases: int,
        force: bool = False,
        dry_run: bool = False,
        admin: dict | None = None,
    ) -> dict:
        """
        Aplica las pendientes a TODAS las BDs del blueprint (síncrono, acotado).
        Continúa con las demás BDs aunque una falle. El job asíncrono es del Plan 06.

        Optimización (evita trabajo N+1): carga y verifica ``specs`` UNA sola vez y
        cachea el ``ServerTarget`` por servidor (la credencial se descifra una vez por
        servidor, no por BD).
        """
        session = self._session()
        try:
            model = session.get(DatabaseModel, model_id)
            if model is None:
                raise AppHttpException(
                    message="Blueprint no encontrado.", status_code=404,
                    context={"model_id": model_id},
                )
            slug = model.slug
            specs = self._load_specs(session, model_id)
            total = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .count()
            )
            db_rows = (
                session.query(
                    ManagedDatabase.id, ManagedDatabase.name,
                    ManagedDatabase.server_id, ManagedDatabase.status,
                )
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id.asc())
                .limit(max_databases)
                .all()
            )
            dbs = [(r.id, r.name, r.server_id, r.status) for r in db_rows]
            # ServerTarget + engine por servidor distinto (descifra credencial 1×/servidor).
            targets: dict[int, tuple] = {}
            for sid in {d[2] for d in dbs}:
                srv = get_server_or_404(session, sid)
                targets[sid] = (build_target(srv), EngineType(engine_value(srv)))
        finally:
            session.close()

        if not specs:
            raise AppHttpException(
                message="El blueprint no tiene migraciones definidas.",
                status_code=422,
                context={"model_id": model_id},
            )
        self._verify_integrity(specs)  # una sola vez para todo el lote

        items: list[dict] = []
        for db_id, name, server_id, status in dbs:
            target, engine = targets[server_id]
            item = {
                "managed_database_id": db_id, "database_name": name,
                "server_id": server_id, "applied": [], "ok": False,
            }
            try:
                quarantined = status == ProvisionStatus.error
                self._guard_quarantine(db_id, quarantined, force, dry_run)
                if dry_run:
                    plan = self._dry_run_plan(
                        db_id, name, server_id, target, slug, specs, None
                    )
                    item["ok"] = True
                    item["pending_versions"] = plan["pending_versions"]
                    item["dry_run"] = True
                else:
                    out = self._run_apply(
                        db_id, db_name=name, server_id=server_id, target=target,
                        engine=engine, slug=slug, specs=specs, up_to_version=None,
                        was_quarantined=quarantined, admin=admin,
                    )
                    item["ok"] = not out["failed"]
                    item["applied"] = out["results"]
            except AppHttpException as exc:
                item["error"] = exc.message
            except Exception as exc:  # noqa: BLE001 — una BD no debe abortar el lote
                logger.warning("apply_all: error inesperado en BD %s: %s", db_id, exc,
                               exc_info=True)
                item["error"] = f"error inesperado: {type(exc).__name__}"
            items.append(item)

        audit.record(
            "migration.apply_all", admin=admin, target_type="database_model",
            target_id=model_id, touched_engine=True,
            detail=f"{len(dbs)}/{total} BDs procesadas" + (" (dry-run)" if dry_run else ""),
        )
        return {
            "model_id": model_id,
            "total_databases": total,
            "processed": len(dbs),
            "results": items,
        }

    # ------------------------------------------------------------------ #
    # Historial (lectura)                                                 #
    # ------------------------------------------------------------------ #
    def history(self, db_id: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
        """Historial de aplicaciones de migraciones de una BD (más reciente primero)."""
        session = self._session()
        try:
            if session.get(ManagedDatabase, db_id) is None:
                raise AppHttpException(
                    message="Base de datos gestionada no encontrada.",
                    status_code=404,
                    context={"managed_database_id": db_id},
                )
            q = (
                session.query(DatabaseMigrationHistory, ModelMigration.version)
                .outerjoin(
                    ModelMigration,
                    ModelMigration.id == DatabaseMigrationHistory.model_migration_id,
                )
                .filter(DatabaseMigrationHistory.managed_database_id == db_id)
            )
            total = q.count()
            rows = (
                q.order_by(
                    DatabaseMigrationHistory.applied_at.desc(),
                    DatabaseMigrationHistory.id.desc(),
                )
                .limit(limit)
                .offset(offset)
                .all()
            )
            items = [
                {
                    "id": h.id,
                    "managed_database_id": h.managed_database_id,
                    "model_migration_id": h.model_migration_id,
                    "version": version,
                    "applied_at": h.applied_at,
                    "status": h.status.value if hasattr(h.status, "value") else h.status,
                    "error": h.error,
                    "execution_ms": h.execution_ms,
                }
                for h, version in rows
            ]
            return items, total
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Persistencia de resultados                                          #
    # ------------------------------------------------------------------ #
    def _record_history(self, db_id: int, results: list[MigrationResult]) -> None:
        if not results:
            return
        session = self._session()
        try:
            for r in results:
                session.add(
                    DatabaseMigrationHistory(
                        managed_database_id=db_id,
                        model_migration_id=r.migration_id,
                        applied_at=r.applied_at,
                        status=MigrationStatus(r.status),
                        error=r.error,
                        execution_ms=r.execution_ms,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _sync_model_version_from_engine(
        self, db_id: int, target, db_name: str, slug: str
    ) -> None:
        """
        Sincroniza model_version releyendo la FUENTE DE VERDAD: la tabla de versión
        que Alembic mantiene dentro de la BD destino (no la contabilidad local).
        """
        current = self.runner.get_current_version(target, db_name, slug)
        self._set_model_version(db_id, current)

    def _set_model_version(self, db_id: int, version: str | None) -> None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md is not None:
                md.model_version = version
                session.commit()
        finally:
            session.close()

    def _set_quarantine(
        self, db_id: int, failed: bool, results: list[MigrationResult]
    ) -> None:
        """
        ROB1 — marca/limpia la cuarentena de la BD según el desenlace del apply:
        - failed → status=error + nota con la versión que falló (posible estado parcial).
        - éxito tras haber estado en error → vuelve a active y limpia la nota.
        """
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md is None:
                return
            if failed:
                bad = next((r for r in results if r.status == "failed"), None)
                md.status = ProvisionStatus.error
                md.notes = (
                    f"Migración {bad.version if bad else '?'} falló; posible estado "
                    f"parcial. Inspeccione y reintente con force=true."
                )
                session.commit()
            elif md.status == ProvisionStatus.error:
                md.status = ProvisionStatus.active
                md.notes = None
                session.commit()
        finally:
            session.close()

    @staticmethod
    def _result_dict(r: MigrationResult) -> dict:
        return {
            "migration_id": r.migration_id,
            "version": r.version,
            "status": r.status,
            "error": r.error,
            "execution_ms": r.execution_ms,
        }
