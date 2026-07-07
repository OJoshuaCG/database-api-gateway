"""
Controller de ModelMigration — migraciones versionadas de un blueprint.

CRUD puro sobre la BD de metadatos del gateway (NO toca ningún motor destino). Al
crear una migración:
- calcula el ``checksum`` de integridad,
- auto-traduce el ``up_sql`` a cada motor (campo calculado ``translated``),
- sugiere un ``down_sql`` (rollback) si la operación es aditiva.

La aplicación sobre BDs gestionadas vive en ``ManagedDatabaseController`` (toca el
motor) usando ``MigrationRunner``.
"""

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType, MigrationStatus
from app.models.model_migration import ModelMigration
from app.services import audit
from app.services.db_admin.migration_integrity import compute_checksum, version_sort_key
from app.services.db_admin.sql_dialect import RollbackGenerator, SqlTranslator

# Orden NUMÉRICO de versión en SQL: (longitud, valor) equivale al orden entero para
# strings de solo dígitos (incl. con ceros a la izquierda), evitando el bug del orden
# lexicográfico ("9999" > "10000"). Cross-engine (length() existe en los 4 motores).
_VERSION_ORDER_ASC = (func.length(ModelMigration.version), ModelMigration.version)
_VERSION_ORDER_DESC = (
    func.length(ModelMigration.version).desc(),
    ModelMigration.version.desc(),
)


class ModelMigrationController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
        self._translator = SqlTranslator()
        self._rollback = RollbackGenerator()

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    def _translated(self, m: ModelMigration) -> dict[str, str]:
        """SQL efectivo por motor (override si existe; si no, traducción)."""
        out: dict[str, str] = {"mysql": m.up_sql_mysql or m.up_sql}
        if m.up_sql_postgresql:
            out["postgresql"] = m.up_sql_postgresql
        else:
            pg = self._translator.translate(m.up_sql, EngineType.postgresql)
            if pg is not None:
                out["postgresql"] = pg
        return out

    def _serialize(self, m: ModelMigration) -> dict:
        return {
            "id": m.id,
            "model_id": m.model_id,
            "version": m.version,
            "name": m.name,
            "up_sql": m.up_sql,
            "up_sql_mysql": m.up_sql_mysql,
            "up_sql_postgresql": m.up_sql_postgresql,
            "down_sql": m.down_sql,
            "down_sql_suggested": m.down_sql_suggested,
            "translated": self._translated(m),
            "checksum": m.checksum,
            "source_engine": m.source_engine,
            "is_baseline": m.is_baseline,
            "has_non_portable": m.has_non_portable,
            "reviewed": m.reviewed,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }

    @staticmethod
    def _serialize_summary(m: ModelMigration) -> dict:
        return {
            "id": m.id,
            "model_id": m.model_id,
            "version": m.version,
            "name": m.name,
            "has_mysql_override": m.up_sql_mysql is not None,
            "has_postgresql_override": m.up_sql_postgresql is not None,
            "has_rollback": m.down_sql is not None,
            "checksum": m.checksum,
            "is_baseline": m.is_baseline,
            "reviewed": m.reviewed,
            "created_at": m.created_at,
        }

    # ------------------------------------------------------------------ #
    # Helpers internos                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _model_or_404(session, model_id: int) -> DatabaseModel:
        model = session.get(DatabaseModel, model_id)
        if not model:
            raise AppHttpException(
                message="Blueprint no encontrado.",
                status_code=404,
                context={"model_id": model_id},
            )
        return model

    @staticmethod
    def _migration_or_404(session, model_id: int, version: str) -> ModelMigration:
        m = (
            session.query(ModelMigration)
            .filter(
                ModelMigration.model_id == model_id,
                ModelMigration.version == version,
            )
            .first()
        )
        if not m:
            raise AppHttpException(
                message="Migración no encontrada para este blueprint.",
                status_code=404,
                context={"model_id": model_id, "version": version},
            )
        return m

    @staticmethod
    def _has_history(session, migration_id: int) -> bool:
        return (
            session.query(DatabaseMigrationHistory)
            .filter(DatabaseMigrationHistory.model_migration_id == migration_id)
            .first()
            is not None
        )

    @staticmethod
    def _has_successful_application(session, migration_id: int) -> bool:
        """
        True si la migración se aplicó EXITOSAMENTE en al menos una BD (status=applied).

        Distinto de ``_has_history``: un intento que solo FALLÓ deja historial pero no
        cambió ninguna BD, así que su SQL todavía puede corregirse. El SQL solo se
        congela cuando existe una aplicación exitosa (alguna BD ya depende de él).
        """
        return (
            session.query(DatabaseMigrationHistory)
            .filter(
                DatabaseMigrationHistory.model_migration_id == migration_id,
                DatabaseMigrationHistory.status == MigrationStatus.applied,
            )
            .first()
            is not None
        )

    # ------------------------------------------------------------------ #
    # Lectura                                                             #
    # ------------------------------------------------------------------ #
    def list_migrations(
        self, model_id: int, *, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            q = session.query(ModelMigration).filter(ModelMigration.model_id == model_id)
            total = q.count()
            rows = q.order_by(*_VERSION_ORDER_ASC).limit(limit).offset(offset).all()
            return [self._serialize_summary(r) for r in rows], total
        finally:
            session.close()

    def get_migration(self, model_id: int, version: str) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            return self._serialize(self._migration_or_404(session, model_id, version))
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura                                                           #
    # ------------------------------------------------------------------ #
    # Reintentos al autoasignar versión: ante colisión por concurrencia (varios
    # colaboradores creando a la vez), se recalcula el siguiente número y se reintenta.
    _AUTO_VERSION_RETRIES = 5

    @staticmethod
    def _next_version(session, model_id: int) -> str:
        """
        Siguiente versión secuencial del blueprint = (máximo numérico actual) + 1, con
        padding a 4 dígitos ('0001', '0002'…). Usa el orden NUMÉRICO, no lexicográfico.
        """
        latest = (
            session.query(ModelMigration.version)
            .filter(ModelMigration.model_id == model_id)
            .order_by(*_VERSION_ORDER_DESC)
            .first()
        )
        next_n = (int(latest[0]) + 1) if latest else 1
        return f"{next_n:04d}"

    def create_migration(self, model_id: int, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)

            up_sql = data["up_sql"]
            up_mysql = data.get("up_sql_mysql")
            up_pg = data.get("up_sql_postgresql")
            down_sql = data.get("down_sql")
            # Sugerir rollback solo si el admin no proporcionó uno explícito.
            suggested = self._rollback.generate(up_sql)

            # Versión: explícita si el admin la pasó; si no, autoasignada (secuencial).
            explicit_version = data.get("version")
            attempts = 1 if explicit_version else self._AUTO_VERSION_RETRIES
            migration = None
            last_exc: IntegrityError | None = None
            for _ in range(attempts):
                version = explicit_version or self._next_version(session, model_id)
                migration = ModelMigration(
                    model_id=model_id,
                    version=version,
                    name=data["name"],
                    up_sql=up_sql,
                    up_sql_mysql=up_mysql,
                    up_sql_postgresql=up_pg,
                    down_sql=down_sql,
                    down_sql_suggested=suggested,
                    # El checksum cubre la versión: se recalcula en cada intento.
                    checksum=compute_checksum(up_sql, up_mysql, up_pg, down_sql, version),
                )
                session.add(migration)
                try:
                    # flush hace visible la migración a _bump_model_version y detecta el
                    # conflicto de versión (UNIQUE) en la MISMA transacción.
                    session.flush()
                    self._bump_model_version(session, model_id)
                    session.commit()  # inserción + current_version en un único commit
                    break
                except IntegrityError as exc:
                    session.rollback()
                    last_exc = exc
                    if explicit_version:
                        # Versión EXPLÍCITA duplicada → 409 (no se reintenta).
                        raise AppHttpException(
                            message="Ya existe una migración con esa versión en el blueprint.",
                            status_code=409,
                            context={"model_id": model_id, "version": explicit_version},
                        ) from exc
                    # Autoasignada: colisión por concurrencia → recomputar y reintentar.
                    continue
            else:
                # Se agotaron los reintentos en modo autónomo (alta concurrencia).
                raise AppHttpException(
                    message=(
                        "No se pudo asignar una versión secuencial por concurrencia alta. "
                        "Reintenta la operación."
                    ),
                    status_code=409,
                    context={"model_id": model_id},
                ) from last_exc

            session.refresh(migration)
            result = self._serialize(migration)
            migration_id = migration.id
            assigned_version = migration.version
        finally:
            session.close()
        audit.record(
            "migration.create",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {assigned_version} creada (id={migration_id})",
        )
        return result

    def create_from_snapshot(self, data: dict, *, admin: dict | None = None) -> dict:
        """
        Crea un blueprint NUEVO cuyo baseline (v0001) es el snapshot estructural de una
        BD existente (Plan 09, modo 3). El dump lo produce el adapter del motor
        (estructura, nunca filas); aquí solo se persiste metadata.

        El baseline queda en el override del motor de origen (NO se auto-traduce) y se
        etiqueta ``source_engine``/``has_non_portable``: si trae objetos procedurales,
        el blueprint queda atado a su motor (sqlglot no transpila PL/pgSQL ↔ MySQL). El
        DDL es un BORRADOR revisable: las rutinas con ``;`` internos pueden requerir
        ajuste antes de aplicar (ver docs/plans/02 sobre el splitter).
        """
        from app.controllers.server_controller import ServerController

        # 1) Dump EN VIVO (lee el motor; solo estructura).
        dump = ServerController().snapshot(data["server_id"], data["database"])
        if not dump.statements:
            raise AppHttpException(
                message="La base de datos no tiene objetos estructurales que fotografiar.",
                status_code=422,
                context={"database": data["database"]},
            )

        source_engine = dump.source_engine
        baseline_sql = "\n\n".join(
            f"{s.ddl.rstrip().rstrip(';')};" for s in dump.statements
        )
        version = "0001"
        up_mysql = baseline_sql if source_engine in ("mysql", "mariadb") else None
        up_pg = baseline_sql if source_engine == "postgresql" else None

        session = self._session()
        try:
            model = DatabaseModel(
                name=data["name"],
                slug=data["slug"],
                description=data.get("description"),
                current_version=version,
                is_active=True,
            )
            session.add(model)
            try:
                session.flush()  # asigna id y detecta conflicto de name/slug
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un blueprint con ese nombre o slug.",
                    status_code=409,
                    context={"slug": data.get("slug")},
                ) from exc

            migration = ModelMigration(
                model_id=model.id,
                version=version,
                name=data.get("baseline_name") or "Snapshot baseline",
                up_sql=baseline_sql,
                up_sql_mysql=up_mysql,
                up_sql_postgresql=up_pg,
                down_sql=None,
                down_sql_suggested=None,
                checksum=compute_checksum(baseline_sql, up_mysql, up_pg, None, version),
                source_engine=source_engine,
                is_baseline=True,
                has_non_portable=dump.has_non_portable,
                reviewed=False,  # R1: DDL capturado del motor → requiere aprobación antes de aplicar
            )
            session.add(migration)
            session.commit()
            session.refresh(model)
            model_result = self._serialize_model(model)
            model_id = model.id
        finally:
            session.close()

        audit.record(
            "database_model.from_snapshot",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            server_id=data["server_id"],
            touched_engine=True,  # se leyó la estructura del motor
            detail=(
                f"baseline desde snapshot de '{data['database']}' "
                f"({source_engine}, {len(dump.statements)} objetos)"
            ),
        )
        return {
            "model": model_result,
            "baseline_version": version,
            "source_engine": source_engine,
            "has_non_portable": dump.has_non_portable,
            "object_counts": dump.object_counts,
            "statements_captured": len(dump.statements),
        }

    @staticmethod
    def _serialize_model(m: DatabaseModel) -> dict:
        return {
            "id": m.id,
            "name": m.name,
            "slug": m.slug,
            "description": m.description,
            "current_version": m.current_version,
            "is_active": m.is_active,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }

    def update_migration(
        self, model_id: int, version: str, data: dict, *, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            applied_successfully = self._has_successful_application(session, m.id)

            # El SQL efectivo (base u overrides) NO puede cambiar si ya se aplicó
            # EXITOSAMENTE en alguna BD: editarlo aquí no re-ejecuta nada en el motor, así
            # que la metadata divergiría de lo que realmente corrió. Un intento que solo
            # falló no congela el SQL (ninguna BD depende de él). Fix-forward si ya se aplicó.
            sql_fields_changing = any(
                f in data and data[f] is not None
                for f in ("up_sql", "up_sql_mysql", "up_sql_postgresql")
            )
            if applied_successfully and sql_fields_changing:
                raise AppHttpException(
                    message=(
                        "La migración ya fue aplicada exitosamente en alguna BD: no se "
                        "puede modificar su SQL. Cree una nueva migración para corregir "
                        "(fix-forward)."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version},
                )

            if "name" in data and data["name"] is not None:
                m.name = data["name"]
            if "up_sql" in data and data["up_sql"] is not None:
                # Al cambiar el SQL base, un override por-motor que NO se re-envíe en este
                # mismo PATCH quedaría obsoleto (gana en _translated sobre el nuevo up_sql).
                # Exigir intención explícita: reenviar el override corregido o limpiarlo
                # (null) en la misma llamada. Evita que quede SQL viejo aplicándose en silencio.
                stale = [
                    f
                    for f in ("up_sql_mysql", "up_sql_postgresql")
                    if getattr(m, f) is not None and f not in data
                ]
                if stale:
                    raise AppHttpException(
                        message=(
                            "Al cambiar 'up_sql' debes reenviar (corregido) o limpiar "
                            f"(null) los overrides que quedarían obsoletos: {', '.join(stale)}."
                        ),
                        status_code=409,
                        context={"model_id": model_id, "version": version, "stale_overrides": stale},
                    )
                # Cascade: al corregir el SQL base se regenera el rollback SUGERIDO
                # (la traducción cross-engine se recalcula al vuelo en _translated, no
                # hay campo persistido que actualizar). El down_sql CONFIRMADO no se toca.
                m.up_sql = data["up_sql"]
                m.down_sql_suggested = self._rollback.generate(m.up_sql)
            if "down_sql" in data:
                m.down_sql = data["down_sql"]
            if "up_sql_mysql" in data:
                m.up_sql_mysql = data["up_sql_mysql"]
            if "up_sql_postgresql" in data:
                m.up_sql_postgresql = data["up_sql_postgresql"]
            # R1: aprobación del baseline (revisión del DDL capturado). No es un campo
            # de SQL, así que se permite aunque la migración ya esté aplicada en alguna BD.
            reviewed_approved = False
            if data.get("reviewed") is not None:
                reviewed_approved = bool(data["reviewed"]) and not m.reviewed
                m.reviewed = bool(data["reviewed"])

            # Recalcular checksum si cambió alguna variante de SQL o el rollback.
            m.checksum = compute_checksum(
                m.up_sql, m.up_sql_mysql, m.up_sql_postgresql, m.down_sql, m.version
            )
            session.commit()
            session.refresh(m)
            result = self._serialize(m)
        finally:
            session.close()
        audit.record(
            "migration.update",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {version} actualizada",
        )
        if reviewed_approved:
            audit.record(
                "migration.review",
                admin=admin,
                target_type="database_model",
                target_id=model_id,
                detail=f"baseline {version} revisado y aprobado para aplicar",
            )
        return result

    def delete_migration(self, model_id: int, version: str, *, admin: dict | None = None) -> None:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            if self._has_history(session, m.id):
                raise AppHttpException(
                    message=(
                        "No se puede eliminar una migración con historial de aplicación. "
                        "Revierta y/o cree una migración compensatoria."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version},
                )
            # Solo se puede eliminar la ÚLTIMA versión (la punta de la secuencia). Borrar
            # una intermedia dejaría un hueco y una versión posterior podría depender de
            # ella (forward-only encadenado). Para tocar una intermedia: edita, o revierte
            # hasta ahí y recréala.
            latest = (
                session.query(ModelMigration.version)
                .filter(ModelMigration.model_id == model_id)
                .order_by(*_VERSION_ORDER_DESC)
                .first()
            )
            if latest and version_sort_key(latest[0]) > version_sort_key(m.version):
                raise AppHttpException(
                    message=(
                        f"Solo se puede eliminar la última versión del blueprint "
                        f"(actual: {latest[0]}). Existen versiones posteriores a {version} "
                        "que podrían depender de ella."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version, "latest": latest[0]},
                )
            session.delete(m)
            session.flush()  # el borrado debe verse antes de recalcular current_version
            self._bump_model_version(session, model_id)
            session.commit()  # borrado + current_version en un único commit
        finally:
            session.close()
        audit.record(
            "migration.delete",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {version} eliminada",
        )

    # ------------------------------------------------------------------ #
    # Mantenimiento de current_version del blueprint                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bump_model_version(session, model_id: int) -> None:
        """
        Fija current_version del blueprint a la migración más reciente (o 0.0.0).

        NO commitea: el llamador lo hace en la misma transacción que la
        inserción/borrado de la migración (atomicidad).
        """
        latest = (
            session.query(ModelMigration.version)
            .filter(ModelMigration.model_id == model_id)
            .order_by(*_VERSION_ORDER_DESC)
            .first()
        )
        model = session.get(DatabaseModel, model_id)
        if model is not None:
            model.current_version = latest[0] if latest else "0.0.0"
