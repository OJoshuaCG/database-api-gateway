"""
Controller de DatabaseModel (blueprints/categorías).

CRUD puro sobre la BD de metadatos del gateway: NO toca ningún motor destino.
"""

from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.managed_database import ManagedDatabase
from app.services import audit


class DatabaseModelController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(m: DatabaseModel) -> dict:
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

    def _get_or_404(self, session, model_id: int) -> DatabaseModel:
        m = session.get(DatabaseModel, model_id)
        if not m:
            raise AppHttpException(
                message="Blueprint no encontrado.",
                status_code=404,
                context={"model_id": model_id},
            )
        return m

    def list_models(self, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            total = session.query(DatabaseModel).count()
            rows = (
                session.query(DatabaseModel)
                .order_by(DatabaseModel.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize(r) for r in rows], total
        finally:
            session.close()

    def get_model(self, model_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, model_id))
        finally:
            session.close()

    def create_model(self, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            model = DatabaseModel(
                name=data["name"],
                slug=data["slug"],
                description=data.get("description"),
                current_version=data.get("current_version", "0.0.0"),
                is_active=data.get("is_active", True),
            )
            session.add(model)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un blueprint con ese nombre o slug.",
                    status_code=409,
                    context={"slug": data.get("slug")},
                ) from exc
            session.refresh(model)
            result = self._serialize(model)
            model_id = model.id
        finally:
            session.close()
        audit.record(
            "database_model.create", admin=admin, target_type="database_model", target_id=model_id
        )
        return result

    def update_model(self, model_id: int, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            for field in ("name", "slug", "description", "current_version", "is_active"):
                if field in data and data[field] is not None:
                    setattr(model, field, data[field])
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un blueprint con ese nombre o slug.",
                    status_code=409,
                    context={"model_id": model_id},
                ) from exc
            session.refresh(model)
            result = self._serialize(model)
        finally:
            session.close()
        audit.record(
            "database_model.update", admin=admin, target_type="database_model", target_id=model_id
        )
        return result

    def delete_model(self, model_id: int, *, admin: dict | None = None) -> None:
        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            session.delete(model)
            session.commit()
        finally:
            session.close()
        audit.record(
            "database_model.delete", admin=admin, target_type="database_model", target_id=model_id
        )

    def list_model_databases(self, model_id: int) -> list[dict]:
        """BDs gestionadas que replican este blueprint."""
        from app.controllers.managed_database_controller import ManagedDatabaseController

        session = self._session()
        try:
            self._get_or_404(session, model_id)
            rows = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id.desc())
                .all()
            )
            return [ManagedDatabaseController._serialize(r) for r in rows]
        finally:
            session.close()
