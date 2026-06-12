"""
Controller de ManagedDatabase (bases de datos gestionadas).

Orquesta el inventario del gateway y el aprovisionamiento real en el motor:
CREATE DATABASE + GRANT al propietario, DROP DATABASE, y reasignación de propietario.

Consistencia GW↔motor (sin rollback silencioso):
    insertar status=pending → ejecutar DDL/DCL → status=active (éxito)
                                              └→ status=error  (falla; detalle en notas)
El registro en estado ``error`` se conserva para auditoría/reintento; el error HTTP
real (502/504/409/...) se propaga al cliente.

Integridad: el propietario debe ser un ServerUser del MISMO servidor (se valida en
el controller; endurecimiento futuro con FK compuesta — ver docs/plans/00).
"""

from sqlalchemy.exc import IntegrityError

from app.controllers.common import build_target, get_server_or_404
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.enums import ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.server_user import ServerUser
from app.services import audit
from app.services.db_admin.factory import get_adapter


class ManagedDatabaseController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(d: ManagedDatabase) -> dict:
        return {
            "id": d.id,
            "name": d.name,
            "server_id": d.server_id,
            "owner_id": d.owner_id,
            "model_id": d.model_id,
            "model_version": d.model_version,
            "charset": d.charset,
            "collation": d.collation,
            "status": d.status,
            "notes": d.notes,
            "created_at": d.created_at,
            "updated_at": d.updated_at,
        }

    def _get_or_404(self, session, db_id: int) -> ManagedDatabase:
        d = session.get(ManagedDatabase, db_id)
        if not d:
            raise AppHttpException(
                message="Base de datos gestionada no encontrada.",
                status_code=404,
                context={"managed_database_id": db_id},
            )
        return d

    def _set_status(
        self, db_id: int, status: ProvisionStatus, *, detail: str | None = None
    ) -> None:
        session = self._session()
        try:
            d = session.get(ManagedDatabase, db_id)
            if d:
                d.status = status
                if detail is not None:
                    d.notes = detail
                session.commit()
        finally:
            session.close()

    @staticmethod
    def _require_owner_on_server(session, owner_id: int, server_id: int) -> ServerUser:
        owner = session.get(ServerUser, owner_id)
        if not owner:
            raise AppHttpException(
                message="El propietario (server_user) no existe.",
                status_code=422,
                context={"owner_id": owner_id},
            )
        if owner.server_id != server_id:
            raise AppHttpException(
                message="El propietario pertenece a otro servidor.",
                status_code=409,
                context={"owner_id": owner_id, "server_id": server_id},
            )
        return owner

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_databases(
        self,
        *,
        server_id: int | None = None,
        owner_id: int | None = None,
        model_id: int | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            q = session.query(ManagedDatabase)
            if server_id is not None:
                q = q.filter(ManagedDatabase.server_id == server_id)
            if owner_id is not None:
                q = q.filter(ManagedDatabase.owner_id == owner_id)
            if model_id is not None:
                q = q.filter(ManagedDatabase.model_id == model_id)
            if status is not None:
                q = q.filter(ManagedDatabase.status == status)
            total = q.count()
            rows = q.order_by(ManagedDatabase.id.desc()).limit(limit).offset(offset).all()
            return [self._serialize(r) for r in rows], total
        finally:
            session.close()

    def get_database(self, db_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, db_id))
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura (inventario + motor)                                      #
    # ------------------------------------------------------------------ #
    def create_database(
        self, data: dict, *, provision: bool, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            server = get_server_or_404(session, data["server_id"])
            owner = self._require_owner_on_server(session, data["owner_id"], server.id)
            if data.get("model_id") is not None and not session.get(
                DatabaseModel, data["model_id"]
            ):
                raise AppHttpException(
                    message="El blueprint (model_id) no existe.",
                    status_code=422,
                    context={"model_id": data["model_id"]},
                )
            owner_username, owner_host = owner.username, owner.host
            target = build_target(server) if provision else None

            md = ManagedDatabase(
                name=data["name"],
                server_id=server.id,
                owner_id=owner.id,
                model_id=data.get("model_id"),
                model_version=data.get("model_version"),
                charset=data.get("charset"),
                collation=data.get("collation"),
                status=ProvisionStatus.pending,
                notes=data.get("notes"),
            )
            session.add(md)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe una base de datos con ese nombre en el servidor.",
                    status_code=409,
                    context={"name": data.get("name")},
                ) from exc
            session.refresh(md)
            db_id, db_name = md.id, md.name
            charset, collation = md.charset, md.collation
            server_id = md.server_id
            result = self._serialize(md)
        finally:
            session.close()

        if provision:
            adapter = get_adapter(target)
            try:
                adapter.create_database(
                    db_name, charset=charset, collation=collation, owner=owner_username
                )
                adapter.grant_database(owner_username, db_name, host=owner_host)
            except AppHttpException as exc:
                self._set_status(
                    db_id,
                    ProvisionStatus.error,
                    detail=f"Error de aprovisionamiento en el motor (HTTP {getattr(exc, 'status_code', '?')}).",
                )
                audit.record(
                    "managed_database.create",
                    status="error",
                    admin=admin,
                    target_type="managed_database",
                    target_id=db_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al crear/otorgar la BD en el motor",
                )
                raise
            self._set_status(db_id, ProvisionStatus.active)
            result["status"] = ProvisionStatus.active

        audit.record(
            "managed_database.create",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=provision,
        )
        return result

    def update_database(
        self, db_id: int, data: dict, *, admin: dict | None = None
    ) -> dict:
        """Actualiza solo metadatos del inventario (no ejecuta DDL en el motor)."""
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            if data.get("model_id") is not None and not session.get(
                DatabaseModel, data["model_id"]
            ):
                raise AppHttpException(
                    message="El blueprint (model_id) no existe.",
                    status_code=422,
                    context={"model_id": data["model_id"]},
                )
            for field in ("model_id", "model_version", "charset", "collation", "notes"):
                if field in data:
                    setattr(md, field, data[field])
            session.commit()
            session.refresh(md)
            result = self._serialize(md)
        finally:
            session.close()
        audit.record(
            "managed_database.update",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
        )
        return result

    def delete_database(
        self, db_id: int, *, drop_remote: bool, admin: dict | None = None
    ) -> None:
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            server = get_server_or_404(session, md.server_id)
            db_name, server_id = md.name, md.server_id
            target = build_target(server) if drop_remote else None
        finally:
            session.close()

        if drop_remote:
            get_adapter(target).drop_database(db_name)

        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md:
                session.delete(md)
                session.commit()
        finally:
            session.close()

        audit.record(
            "managed_database.delete",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=drop_remote,
        )

    def reassign_owner(
        self, db_id: int, new_owner_id: int, *, provision: bool, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            server = get_server_or_404(session, md.server_id)
            new_owner = self._require_owner_on_server(session, new_owner_id, md.server_id)
            old_owner = session.get(ServerUser, md.owner_id)
            db_name, server_id = md.name, md.server_id
            new_username, new_host = new_owner.username, new_owner.host
            old_username = old_owner.username if old_owner else None
            old_host = old_owner.host if old_owner else "%"
            target = build_target(server) if provision else None
        finally:
            session.close()

        if provision:
            try:
                get_adapter(target).reassign_database_owner(
                    db_name,
                    new_username,
                    new_host=new_host,
                    old_owner=old_username,
                    old_host=old_host,
                )
            except AppHttpException:
                audit.record(
                    "managed_database.reassign_owner",
                    status="error",
                    admin=admin,
                    target_type="managed_database",
                    target_id=db_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al reasignar el propietario en el motor",
                )
                raise

        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            md.owner_id = new_owner_id
            session.commit()
            session.refresh(md)
            result = self._serialize(md)
        finally:
            session.close()

        audit.record(
            "managed_database.reassign_owner",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=provision,
        )
        return result
