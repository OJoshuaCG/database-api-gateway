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

import hashlib

from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType
from app.models.model_migration import ModelMigration
from app.services import audit
from app.services.db_admin.sql_dialect import RollbackGenerator, SqlTranslator


def compute_checksum(
    up_sql: str,
    up_sql_mysql: str | None,
    up_sql_postgresql: str | None,
    down_sql: str | None = None,
) -> str:
    """
    SHA256 de TODO el SQL ejecutable de la migración (up + variantes + rollback).

    Incluir ``down_sql`` es CRÍTICO: el rollback ejecuta DDL destructivo, así que su
    integridad debe protegerse igual que la del ``up_sql``. Detecta alteración directa
    de la fila en la BD de metadatos del gateway antes de ejecutar nada en el motor.

    Separadores ``\\x1f`` entre campos para evitar colisiones por concatenación.
    """
    parts = [up_sql or "", up_sql_mysql or "", up_sql_postgresql or "", down_sql or ""]
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
            rows = q.order_by(ModelMigration.version.asc()).limit(limit).offset(offset).all()
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

            migration = ModelMigration(
                model_id=model_id,
                version=data["version"],
                name=data["name"],
                up_sql=up_sql,
                up_sql_mysql=up_mysql,
                up_sql_postgresql=up_pg,
                down_sql=down_sql,
                down_sql_suggested=suggested,
                checksum=compute_checksum(up_sql, up_mysql, up_pg, down_sql),
            )
            session.add(migration)
            try:
                # flush hace visible la nueva migración a _bump_model_version y
                # detecta el conflicto de versión, todo en la MISMA transacción.
                session.flush()
                # Mantener current_version del blueprint = versión más reciente subida.
                self._bump_model_version(session, model_id)
                session.commit()  # inserción + current_version en un único commit atómico
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe una migración con esa versión en el blueprint.",
                    status_code=409,
                    context={"model_id": model_id, "version": data["version"]},
                ) from exc
            session.refresh(migration)

            result = self._serialize(migration)
            migration_id = migration.id
        finally:
            session.close()
        audit.record(
            "migration.create",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {data['version']} creada (id={migration_id})",
        )
        return result

    def update_migration(
        self, model_id: int, version: str, data: dict, *, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            applied_anywhere = self._has_history(session, m.id)

            # El SQL efectivo (overrides) NO puede cambiar si ya se aplicó en alguna BD.
            sql_fields_changing = any(
                f in data and data[f] is not None
                for f in ("up_sql_mysql", "up_sql_postgresql")
            )
            if applied_anywhere and sql_fields_changing:
                raise AppHttpException(
                    message=(
                        "La migración ya fue aplicada en alguna BD: no se puede modificar "
                        "su SQL. Cree una nueva migración para corregir."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version},
                )

            if "name" in data and data["name"] is not None:
                m.name = data["name"]
            if "down_sql" in data:
                m.down_sql = data["down_sql"]
            if "up_sql_mysql" in data:
                m.up_sql_mysql = data["up_sql_mysql"]
            if "up_sql_postgresql" in data:
                m.up_sql_postgresql = data["up_sql_postgresql"]

            # Recalcular checksum si cambió alguna variante de SQL o el rollback.
            m.checksum = compute_checksum(
                m.up_sql, m.up_sql_mysql, m.up_sql_postgresql, m.down_sql
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
            .order_by(ModelMigration.version.desc())
            .first()
        )
        model = session.get(DatabaseModel, model_id)
        if model is not None:
            model.current_version = latest[0] if latest else "0.0.0"
