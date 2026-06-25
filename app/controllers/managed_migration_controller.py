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
from app.controllers.model_migration_controller import compute_checksum
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType, MigrationStatus
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.services import audit
from app.services.db_admin.migrations import (
    MigrationResult,
    MigrationRunner,
    MigrationSpec,
)


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
            .order_by(ModelMigration.version.asc())
            .all()
        )
        return [
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

    @staticmethod
    def _verify_integrity(specs: list[MigrationSpec]) -> None:
        for spec in specs:
            expected = compute_checksum(
                spec.up_sql, spec.up_sql_mysql, spec.up_sql_postgresql
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
        self, db_id: int, *, up_to_version: str | None = None, admin: dict | None = None
    ) -> dict:
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

        if not specs:
            raise AppHttpException(
                message="El blueprint no tiene migraciones definidas.",
                status_code=422,
                context={"model_id": model.id},
            )

        # Auditar la INTENCIÓN antes de tocar el motor.
        audit.record(
            "migration.apply", status="attempt", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True,
            detail=f"apply hasta {up_to_version or 'head'}",
        )

        results = self.runner.apply(
            target, db_name=db_name, slug=slug, engine=engine,
            managed_db_id=db_id, specs=specs, up_to_version=up_to_version,
        )
        self._record_history(db_id, results)
        self._sync_model_version(db_id, results)

        failed = any(r.status == "failed" for r in results)
        audit.record(
            "migration.apply", status="error" if failed else "success", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True,
            detail=f"{sum(1 for r in results if r.status=='applied')} aplicadas"
                   + (" (con fallo)" if failed else ""),
        )
        return {
            "managed_database_id": db_id,
            "applied_count": sum(1 for r in results if r.status == "applied"),
            "failed": failed,
            "results": [self._result_dict(r) for r in results],
        }

    def rollback(self, db_id: int, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
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
            slug, engine = model.slug, EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            target = build_target(server)
        finally:
            session.close()

        self.runner.stamp(
            target, db_name=db_name, slug=slug, engine=engine,
            specs=specs, version=version,
        )
        self._set_model_version(db_id, version)
        audit.record(
            "migration.stamp", admin=admin, target_type="managed_database",
            target_id=db_id, server_id=server_id, touched_engine=True,
            detail=f"stamp {version}",
        )
        return self.status(db_id)

    def apply_all(
        self, model_id: int, *, max_databases: int, admin: dict | None = None
    ) -> dict:
        """
        Aplica las pendientes a TODAS las BDs del blueprint (síncrono, acotado).
        Continúa con las demás BDs aunque una falle. El job asíncrono es del Plan 06.
        """
        session = self._session()
        try:
            model = session.get(DatabaseModel, model_id)
            if model is None:
                raise AppHttpException(
                    message="Blueprint no encontrado.", status_code=404,
                    context={"model_id": model_id},
                )
            total = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .count()
            )
            db_ids = [
                r.id
                for r in session.query(ManagedDatabase.id)
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id.asc())
                .limit(max_databases)
                .all()
            ]
        finally:
            session.close()

        items: list[dict] = []
        for db_id in db_ids:
            item = {"managed_database_id": db_id, "applied": [], "ok": False}
            try:
                # Reutiliza el flujo individual (carga, integridad, runner, historial).
                out = self.apply(db_id, admin=admin)
                item["ok"] = not out["failed"]
                item["applied"] = out["results"]
                item["database_name"] = self._db_name(db_id)
                item["server_id"] = self._server_id(db_id)
            except AppHttpException as exc:
                item["ok"] = False
                item["error"] = exc.message
                item["database_name"] = self._db_name(db_id)
                item["server_id"] = self._server_id(db_id)
            items.append(item)

        audit.record(
            "migration.apply_all", admin=admin, target_type="database_model",
            target_id=model_id, touched_engine=True,
            detail=f"{len(db_ids)}/{total} BDs procesadas",
        )
        return {
            "model_id": model_id,
            "total_databases": total,
            "processed": len(db_ids),
            "results": items,
        }

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

    def _sync_model_version(self, db_id: int, results: list[MigrationResult]) -> None:
        """model_version = última versión aplicada con éxito (si la hubo)."""
        applied = [r.version for r in results if r.status == "applied"]
        if applied:
            self._set_model_version(db_id, max(applied))

    def _set_model_version(self, db_id: int, version: str | None) -> None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md is not None:
                md.model_version = version
                session.commit()
        finally:
            session.close()

    def _db_name(self, db_id: int) -> str | None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            return md.name if md else None
        finally:
            session.close()

    def _server_id(self, db_id: int) -> int | None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            return md.server_id if md else None
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
