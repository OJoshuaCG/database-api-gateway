"""
Controller de ServerUser (usuarios del motor).

- CRUD del inventario sobre la BD del gateway (ORM).
- Aprovisionamiento opcional en el motor destino (CREATE/ALTER/DROP USER) vía el
  ServerAdapter correspondiente, controlado por flags (``provision``/``drop_remote``).

Consistencia GW↔motor:
- create: se inserta primero en el inventario (reclama unicidad); si el aprovisionamiento
  remoto falla, se hace rollback LIMPIO del registro (no quedó usuario en el motor).
- update de password: se aplica primero en el motor (ALTER USER) y luego se persiste
  el nuevo password cifrado, para no dejar el inventario adelantado al motor.
- delete: si el usuario posee BDs gestionadas se bloquea (409); con ``drop_remote`` se
  hace DROP USER en el motor antes de borrar el registro.

La credencial descifrada NUNCA se persiste en claro, se serializa ni se loguea.
"""

from sqlalchemy.exc import IntegrityError

from app.controllers.common import build_target, get_server_or_404
from app.core.crypto import CryptoConfigError, CryptoError, encrypt
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.managed_database import ManagedDatabase
from app.models.server_user import ServerUser
from app.schemas.server_user import GrantApplyResult, GrantOnCreate, ServerUserFullOut, ServerUserOut
from app.services import audit
from app.services.db_admin.dtos import EngineUserInfo
from app.services.db_admin.factory import get_adapter


class ServerUserController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(u: ServerUser) -> dict:
        """Dict seguro para la API: SIN el password (ni cifrado ni en claro)."""
        return {
            "id": u.id,
            "server_id": u.server_id,
            "username": u.username,
            "host": u.host,
            "is_active": u.is_active,
            "notes": u.notes,
            "has_password": bool(u.password_encrypted),
            "created_at": u.created_at,
            "updated_at": u.updated_at,
        }

    @staticmethod
    def _encrypt(plaintext: str) -> str:
        try:
            return encrypt(plaintext)
        except (CryptoError, CryptoConfigError) as exc:
            raise AppHttpException(
                message="No se pudo cifrar la credencial del usuario.",
                status_code=500,
            ) from exc

    def _get_or_404(self, session, user_id: int) -> ServerUser:
        u = session.get(ServerUser, user_id)
        if not u:
            raise AppHttpException(
                message="Usuario de servidor no encontrado.",
                status_code=404,
                context={"server_user_id": user_id},
            )
        return u

    def _delete_row(self, user_id: int) -> None:
        session = self._session()
        try:
            u = session.get(ServerUser, user_id)
            if u:
                session.delete(u)
                session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_server_users(
        self, *, server_id: int | None, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            q = session.query(ServerUser)
            if server_id is not None:
                q = q.filter(ServerUser.server_id == server_id)
            total = q.count()
            rows = q.order_by(ServerUser.id.desc()).limit(limit).offset(offset).all()
            return [self._serialize(r) for r in rows], total
        finally:
            session.close()

    def get_server_user(self, user_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, user_id))
        finally:
            session.close()

    def list_user_databases(self, user_id: int) -> list[dict]:
        from app.controllers.managed_database_controller import ManagedDatabaseController

        session = self._session()
        try:
            self._get_or_404(session, user_id)
            rows = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.owner_id == user_id)
                .order_by(ManagedDatabase.id.desc())
                .all()
            )
            return [ManagedDatabaseController._serialize(r) for r in rows]
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura (inventario + motor)                                      #
    # ------------------------------------------------------------------ #
    def create_server_user(
        self, data: dict, *, provision: bool, admin: dict | None = None
    ) -> dict:
        password = data.get("password")
        if provision and not password:
            raise AppHttpException(
                message="Se requiere 'password' para aprovisionar el usuario en el motor.",
                status_code=422,
            )

        session = self._session()
        try:
            server = get_server_or_404(session, data["server_id"])
            target = build_target(server) if provision else None
            user = ServerUser(
                server_id=server.id,
                username=data["username"],
                host=data.get("host") or "%",
                password_encrypted=self._encrypt(password) if password else None,
                notes=data.get("notes"),
                is_active=data.get("is_active", True),
            )
            session.add(user)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un usuario con ese nombre y host en el servidor.",
                    status_code=409,
                    context={"username": data.get("username")},
                ) from exc
            session.refresh(user)
            user_id, username, host = user.id, user.username, user.host
            server_id = user.server_id
            result = self._serialize(user)
        finally:
            session.close()

        if provision:
            try:
                get_adapter(target).create_user(username, password, host)
            except AppHttpException:
                # No quedó usuario en el motor: rollback limpio del inventario.
                self._delete_row(user_id)
                audit.record(
                    "server_user.create",
                    status="error",
                    admin=admin,
                    target_type="server_user",
                    target_id=user_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al crear el usuario en el motor",
                )
                raise

        audit.record(
            "server_user.create",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=provision,
        )
        return result

    def update_server_user(
        self, user_id: int, data: dict, *, provision: bool, admin: dict | None = None
    ) -> dict:
        new_password = data.get("password")

        # 1) leer datos del usuario y, si hace falta, el target.
        session = self._session()
        try:
            user = self._get_or_404(session, user_id)
            server = get_server_or_404(session, user.server_id)
            username, host, server_id = user.username, user.host, user.server_id
            target = build_target(server) if (provision and new_password) else None
        finally:
            session.close()

        # 2) cambio de password en el motor PRIMERO (si se aprovisiona).
        if provision and new_password:
            try:
                get_adapter(target).change_password(username, new_password, host)
            except AppHttpException:
                audit.record(
                    "server_user.update",
                    status="error",
                    admin=admin,
                    target_type="server_user",
                    target_id=user_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al cambiar el password en el motor",
                )
                raise

        # 3) persistir cambios en el inventario.
        session = self._session()
        try:
            user = self._get_or_404(session, user_id)
            if new_password:
                user.password_encrypted = self._encrypt(new_password)
            if data.get("is_active") is not None:
                user.is_active = data["is_active"]
            if "notes" in data:
                user.notes = data["notes"]
            session.commit()
            session.refresh(user)
            result = self._serialize(user)
        finally:
            session.close()

        audit.record(
            "server_user.update",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=bool(provision and new_password),
        )
        return result

    def provision_with_grants(
        self,
        data: dict,
        initial_grants: list[GrantOnCreate],
        *,
        admin: dict | None = None,
    ) -> ServerUserFullOut:
        """
        Crea y aprovisiona el usuario (igual que create_server_user con provision=True)
        y luego aplica la lista de grants iniciales. Los grants se aplican best-effort:
        un fallo no deshace la creación del usuario.
        """
        from app.controllers.grant_controller import GrantController

        # 1) Crear el usuario (siempre con provision=True en este endpoint)
        user_dict = self.create_server_user(data, provision=True, admin=admin)

        # Obtener el user_id recién creado para aplicar grants
        user_id = user_dict["id"]
        user_out = ServerUserOut(**user_dict)

        # 2) Aplicar grants best-effort
        grant_results: list[GrantApplyResult] = []
        grants_applied = 0

        if initial_grants:
            # Re-cargar contexto del usuario para el adapter
            from app.schemas.grant import GrantRequest

            ctrl = GrantController()
            for grant_spec in initial_grants:
                req = GrantRequest(
                    level=grant_spec.level,
                    object_ref=grant_spec.object_ref,
                    privileges=grant_spec.privileges,
                    with_grant_option=grant_spec.with_grant_option,
                )
                obj_label = grant_spec.object_ref.table or grant_spec.object_ref.database
                try:
                    ctrl.grant_object(user_id, req, admin=admin)
                    grants_applied += 1
                    grant_results.append(
                        GrantApplyResult(
                            level=grant_spec.level.value,
                            object=obj_label,
                            privileges=grant_spec.privileges,
                            success=True,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort
                    grant_results.append(
                        GrantApplyResult(
                            level=grant_spec.level.value,
                            object=obj_label,
                            privileges=grant_spec.privileges,
                            success=False,
                            error=str(exc),
                        )
                    )

        return ServerUserFullOut(
            user=user_out,
            grants_applied=grants_applied,
            grant_results=grant_results,
        )

    def delete_server_user(
        self,
        user_id: int,
        *,
        drop_remote: bool,
        confirm_username: str | None = None,
        admin: dict | None = None,
    ) -> None:
        session = self._session()
        try:
            user = self._get_or_404(session, user_id)
            server = get_server_or_404(session, user.server_id)
            owned = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.owner_id == user_id)
                .count()
            )
            if owned:
                raise AppHttpException(
                    message=(
                        "No se puede eliminar: el usuario posee bases de datos gestionadas. "
                        "Reasigna o elimina esas BDs primero."
                    ),
                    status_code=409,
                    context={"server_user_id": user_id, "owned_databases": owned},
                )
            username, host, server_id = user.username, user.host, user.server_id
            target = build_target(server) if drop_remote else None
        finally:
            session.close()

        if drop_remote:
            # Confirmación explícita (doble intención) para DROP USER en el motor:
            # el cliente debe repetir el username exacto.
            if confirm_username != username:
                raise AppHttpException(
                    message=(
                        "Confirmación requerida: para ejecutar DROP USER en el motor, "
                        "'confirm_username' debe coincidir exactamente con el username."
                    ),
                    status_code=422,
                    context={"server_user_id": user_id, "required": "confirm_username == username"},
                )
            # Auditar la INTENCIÓN antes de la acción irreversible.
            audit.record(
                "server_user.delete",
                status="attempt",
                admin=admin,
                target_type="server_user",
                target_id=user_id,
                server_id=server_id,
                touched_engine=True,
                detail="DROP USER solicitado (confirmado)",
            )
            try:
                get_adapter(target).drop_user(username, host)
            except AppHttpException:
                audit.record(
                    "server_user.delete",
                    status="error",
                    admin=admin,
                    target_type="server_user",
                    target_id=user_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al eliminar el usuario en el motor",
                )
                raise

        self._delete_row(user_id)
        audit.record(
            "server_user.delete",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=drop_remote,
        )
