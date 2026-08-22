"""
Controller de PROYECTOS (agrupación de blueprints).

CRUD puro sobre la BD de metadatos del gateway: NO toca ningún motor destino. Un proyecto
no tiene servidor, ni credenciales, ni versión — es un nombre y una descripción con una
lista de blueprints colgada.

REGLA DURA que este archivo debe preservar: **borrar un proyecto no borra blueprints.**
``delete_project`` borra los vínculos y la fila del proyecto, y nada más. Un blueprint es
el esquema que replican N bases de datos reales; que un agrupador pueda arrastrarlas sería
una pérdida de datos causada por una operación de organización.
"""

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.controllers.database_model_controller import DatabaseModelController
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.project import Project, ProjectDatabaseModel
from app.services import audit


class ProjectController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(p: Project, *, blueprint_count: int = 0) -> dict:
        return {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "blueprint_count": blueprint_count,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }

    def _get_or_404(self, session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if not project:
            raise AppHttpException(
                message="Proyecto no encontrado.",
                status_code=404,
                context={"project_id": project_id},
            )
        return project

    @staticmethod
    def _counts_for(session, project_ids: list[int]) -> dict[int, int]:
        """
        Vínculos por proyecto en UNA query para toda la página.

        Contar dentro del bucle sería una query por fila; con veinte proyectos por página
        eso son veintiuna consultas para pintar una tabla que no las necesita.
        """
        if not project_ids:
            return {}
        rows = (
            session.query(
                ProjectDatabaseModel.project_id,
                func.count(ProjectDatabaseModel.model_id),
            )
            .filter(ProjectDatabaseModel.project_id.in_(project_ids))
            .group_by(ProjectDatabaseModel.project_id)
            .all()
        )
        return {pid: count for pid, count in rows}

    @staticmethod
    def _validate_models_exist(session, model_ids: list[int]) -> list[int]:
        """
        Devuelve los ids en orden estable, o 422 nombrando los que no existen.

        Fail-closed y TODO-O-NADA: vincular los que sí existen e ignorar el resto dejaría
        al cliente creyendo que su selección entró completa. Es la misma política que usa
        la selección de items en schema-comparisons.
        """
        unique_ids = list(dict.fromkeys(model_ids))
        found = {
            row[0]
            for row in session.query(DatabaseModel.id)
            .filter(DatabaseModel.id.in_(unique_ids))
            .all()
        }
        missing = [mid for mid in unique_ids if mid not in found]
        if missing:
            raise AppHttpException(
                message=(
                    "Hay blueprints inexistentes en la selección; no se vinculó ninguno: "
                    + ", ".join(str(m) for m in missing)
                ),
                status_code=422,
                context={"missing_model_ids": missing},
            )
        return unique_ids

    # ─── CRUD del proyecto ────────────────────────────────────────────────── #

    def list_projects(self, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            total = session.query(Project).count()
            rows = (
                session.query(Project)
                .order_by(Project.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            counts = self._counts_for(session, [r.id for r in rows])
            return [
                self._serialize(r, blueprint_count=counts.get(r.id, 0)) for r in rows
            ], total
        finally:
            session.close()

    def get_project(self, project_id: int) -> dict:
        session = self._session()
        try:
            project = self._get_or_404(session, project_id)
            counts = self._counts_for(session, [project.id])
            return self._serialize(project, blueprint_count=counts.get(project.id, 0))
        finally:
            session.close()

    def create_project(self, data: dict, *, admin: dict | None = None) -> dict:
        model_ids = data.get("model_ids") or []
        session = self._session()
        try:
            project = Project(name=data["name"], description=data.get("description"))
            session.add(project)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un proyecto con ese nombre.",
                    status_code=409,
                    context={"name": data.get("name")},
                ) from exc
            session.refresh(project)
            project_id = project.id
            # Los vínculos del alta se validan DESPUÉS de que el proyecto existe: si algún
            # id es inválido, el 422 deja el proyecto creado y vacío. Es preferible a
            # rechazar el alta entera por un id mal tipeado en la lista opcional, y el
            # cliente reintenta solo la vinculación.
            linked = 0
            if model_ids:
                valid_ids = self._validate_models_exist(session, model_ids)
                for mid in valid_ids:
                    session.add(
                        ProjectDatabaseModel(project_id=project_id, model_id=mid)
                    )
                session.commit()
                linked = len(valid_ids)
            result = self._serialize(project, blueprint_count=linked)
        finally:
            session.close()
        audit.record(
            "project.create", admin=admin, target_type="project", target_id=project_id
        )
        return result

    def update_project(
        self, project_id: int, data: dict, *, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            project = self._get_or_404(session, project_id)
            # ``description`` se puede LIMPIAR (mandar null), así que se asigna por
            # PRESENCIA de la clave; ``name`` es obligatorio y un null no significa nada.
            if "description" in data:
                project.description = data["description"]
            if data.get("name") is not None:
                project.name = data["name"]
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un proyecto con ese nombre.",
                    status_code=409,
                    context={"project_id": project_id},
                ) from exc
            session.refresh(project)
            counts = self._counts_for(session, [project.id])
            result = self._serialize(project, blueprint_count=counts.get(project.id, 0))
        finally:
            session.close()
        audit.record(
            "project.update", admin=admin, target_type="project", target_id=project_id
        )
        return result

    def delete_project(self, project_id: int, *, admin: dict | None = None) -> int:
        """
        Borra el proyecto y SOLO sus vínculos. Devuelve cuántos vínculos se soltaron.

        El ``DELETE`` explícito de los vínculos no es redundante con el
        ``ondelete="CASCADE"`` de la FK: SQLite no aplica claves foráneas salvo que se
        active ``PRAGMA foreign_keys``, así que en los entornos de test el cascade del
        motor no dispara y quedarían filas apuntando a un proyecto que ya no existe. Y va
        en la MISMA transacción que el borrado del proyecto: si algo falla, no queda un
        proyecto sin sus vínculos ni al revés.
        """
        session = self._session()
        try:
            project = self._get_or_404(session, project_id)
            unlinked = (
                session.query(ProjectDatabaseModel)
                .filter(ProjectDatabaseModel.project_id == project_id)
                .delete(synchronize_session=False)
            )
            session.delete(project)
            session.commit()
        finally:
            session.close()
        audit.record(
            "project.delete",
            admin=admin,
            target_type="project",
            target_id=project_id,
            detail=f"{unlinked} vínculo(s) de blueprint soltado(s); blueprints intactos",
        )
        return unlinked

    # ─── Vínculos con blueprints ──────────────────────────────────────────── #

    def list_project_blueprints(self, project_id: int) -> list[dict]:
        session = self._session()
        try:
            self._get_or_404(session, project_id)
            rows = (
                session.query(DatabaseModel)
                .join(
                    ProjectDatabaseModel,
                    ProjectDatabaseModel.model_id == DatabaseModel.id,
                )
                .filter(ProjectDatabaseModel.project_id == project_id)
                .order_by(DatabaseModel.id.desc())
                .all()
            )
            return [DatabaseModelController._serialize(r) for r in rows]
        finally:
            session.close()

    def link_blueprints(
        self, project_id: int, model_ids: list[int], *, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            self._get_or_404(session, project_id)
            valid_ids = self._validate_models_exist(session, model_ids)
            existing = {
                row[0]
                for row in session.query(ProjectDatabaseModel.model_id)
                .filter(ProjectDatabaseModel.project_id == project_id)
                .all()
            }
            to_link = [mid for mid in valid_ids if mid not in existing]
            already = [mid for mid in valid_ids if mid in existing]
            for mid in to_link:
                session.add(ProjectDatabaseModel(project_id=project_id, model_id=mid))
            try:
                session.commit()
            except IntegrityError as exc:
                # Dos llamadas simultáneas pueden pasar el chequeo de ``existing`` a la
                # vez; la PK compuesta lo corta en el motor. El vínculo existe igual, así
                # que el resultado correcto es "ya estaba", no un 500.
                session.rollback()
                raise AppHttpException(
                    message="Otro proceso vinculó estos blueprints al mismo tiempo; reintentá.",
                    status_code=409,
                    context={"project_id": project_id},
                ) from exc
            total = (
                session.query(func.count(ProjectDatabaseModel.model_id))
                .filter(ProjectDatabaseModel.project_id == project_id)
                .scalar()
            ) or 0
            result = {
                "project_id": project_id,
                "linked": to_link,
                "already_linked": already,
                "blueprint_count": int(total),
            }
        finally:
            session.close()
        if to_link:
            audit.record(
                "project.blueprints.link",
                admin=admin,
                target_type="project",
                target_id=project_id,
                detail=f"blueprints vinculados: {', '.join(str(m) for m in to_link)}",
            )
        return result

    def unlink_blueprint(
        self, project_id: int, model_id: int, *, admin: dict | None = None
    ) -> None:
        """Suelta UN vínculo. El blueprint no se toca: sigue existiendo con sus BDs."""
        session = self._session()
        try:
            self._get_or_404(session, project_id)
            deleted = (
                session.query(ProjectDatabaseModel)
                .filter(
                    ProjectDatabaseModel.project_id == project_id,
                    ProjectDatabaseModel.model_id == model_id,
                )
                .delete(synchronize_session=False)
            )
            if not deleted:
                session.rollback()
                raise AppHttpException(
                    message="Ese blueprint no pertenece a este proyecto.",
                    status_code=404,
                    context={"project_id": project_id, "model_id": model_id},
                )
            session.commit()
        finally:
            session.close()
        audit.record(
            "project.blueprints.unlink",
            admin=admin,
            target_type="project",
            target_id=project_id,
            detail=f"blueprint {model_id} desvinculado; el blueprint no se borró",
        )

    def list_model_projects(self, model_id: int) -> list[dict]:
        """
        Proyectos a los que pertenece un blueprint (la vista inversa).

        Necesaria para la pantalla del blueprint: sin ella el frontend tendría que pedir
        todos los proyectos y sus blueprints para responder "¿de qué proyectos es este?".
        """
        session = self._session()
        try:
            if not session.get(DatabaseModel, model_id):
                raise AppHttpException(
                    message="Blueprint no encontrado.",
                    status_code=404,
                    context={"model_id": model_id},
                )
            rows = (
                session.query(Project)
                .join(
                    ProjectDatabaseModel,
                    ProjectDatabaseModel.project_id == Project.id,
                )
                .filter(ProjectDatabaseModel.model_id == model_id)
                .order_by(Project.id.desc())
                .all()
            )
            counts = self._counts_for(session, [r.id for r in rows])
            return [
                self._serialize(r, blueprint_count=counts.get(r.id, 0)) for r in rows
            ]
        finally:
            session.close()
