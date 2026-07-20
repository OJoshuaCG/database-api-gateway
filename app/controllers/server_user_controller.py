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

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.crypto import CryptoConfigError, CryptoError, decrypt, encrypt
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.managed_database import ManagedDatabase
from app.models.server_user import ServerUser
from app.schemas.server_user import (
    AddHostOut,
    AdoptAllHostsItemOut,
    BatchAdoptOut,
    EngineUserActionOut,
    EngineUserIdentityOut,
    GrantApplyResult,
    GrantOnCreate,
    GroupedEngineUserOut,
    GroupedEngineUsersOut,
    KnownPasswordSetItemOut,
    KnownPasswordSetOut,
    PasswordChangeItemOut,
    PasswordChangeBatchOut,
    RevealedPasswordOut,
    ServerUserFullOut,
    ServerUserOut,
)
from app.services import audit
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

    def adopt_user(self, data: dict, *, admin: dict | None = None) -> dict:
        """
        Adopta un usuario/rol que YA existe en el motor (Plan 09): registra metadata
        SIN ejecutar CREATE USER y SIN password (``has_password=false`` hasta que se
        rote). Verifica la existencia real (404 si no). Idempotente: 409 si ya está.
        """
        session = self._session()
        try:
            server = get_server_or_404(session, data["server_id"])
            target = build_target(server)  # descifra con la sesión abierta
            username = data["username"]
            host = data.get("host") or "%"
            server_id = server.id
        finally:
            session.close()

        # Verificar existencia REAL en el motor (solo lectura). En PostgreSQL no hay
        # host: se matchea por username; en MySQL/MariaDB por (username, host).
        is_pg = target.dialect == "postgresql"
        live = get_adapter(target).list_users()
        exists = any(
            u.username == username and (is_pg or (u.host or "%") == host) for u in live
        )
        if not exists:
            raise AppHttpException(
                message="El usuario no existe en el motor; no hay nada que adoptar.",
                status_code=404,
                context={"username": username, "host": None if is_pg else host},
            )

        session = self._session()
        try:
            user = ServerUser(
                server_id=server_id,
                username=username,
                host=host,
                password_encrypted=None,
                notes=data.get("notes"),
                is_active=True,
            )
            session.add(user)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un usuario con ese nombre y host en el servidor (¿ya adoptado?).",
                    status_code=409,
                    context={"username": username},
                ) from exc
            session.refresh(user)
            result = self._serialize(user)
            user_id = user.id
        finally:
            session.close()

        audit.record(
            "server_user.adopt",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=False,
            detail="usuario existente adoptado al inventario",
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

    # ------------------------------------------------------------------ #
    # Manejo por IDENTIDAD FÍSICA (server_id, username, host)             #
    # Funciona tanto para usuarios ADOPTADOS como NO adoptados: se opera  #
    # directo sobre el motor y, si existe fila de inventario, se sincroniza.#
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decrypt(ciphertext: str) -> str:
        try:
            return decrypt(ciphertext)
        except (CryptoError, CryptoConfigError) as exc:
            raise AppHttpException(
                message="No se pudo descifrar la credencial del usuario.",
                status_code=500,
            ) from exc

    @staticmethod
    def _guard_not_root(root_username: str | None, username: str) -> None:
        """
        Guard anti auto-lockout: prohíbe operar por identidad sobre la CREDENCIAL
        pseudo-root del gateway (``Server.root_username``), que normalmente NO es una
        fila de ``ServerUser`` — por eso el guard de grant_controller no la cubre y hay
        que replicarlo aquí. Un DROP/ALTER sobre esa cuenta deja al gateway sin control
        del servidor (irreversible desde el gateway).
        """
        if root_username and username.lower() == root_username.lower():
            raise AppHttpException(
                message=(
                    "No se puede operar sobre la propia credencial pseudo-root del gateway "
                    "(riesgo de auto-bloqueo). Para gestionar esa cuenta, hazlo fuera del gateway."
                ),
                status_code=409,
                context={"username": username},
            )

    def _find_inventory_row(self, session, server_id: int, username: str, host: str, *, is_pg: bool):
        """Fila de inventario que corresponde a la identidad, o None. En PG se ignora host."""
        q = session.query(ServerUser).filter(
            ServerUser.server_id == server_id, ServerUser.username == username
        )
        if not is_pg:
            q = q.filter(ServerUser.host == host)
        return q.first()

    def list_users_grouped(self, server_id: int) -> GroupedEngineUsersOut:
        """
        Lista los usuarios del motor AGRUPADOS por username (una entrada por nombre, sus
        hosts como identidades) y CRUZADOS con el inventario del gateway: cada identidad
        se marca ``adopted`` (en inventario), ``unmanaged`` (solo en el motor) u
        ``orphan`` (solo en el inventario, borrada por fuera). En PostgreSQL
        ``supports_hosts=false`` y cada username tiene una sola identidad (host None).
        """
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            target = build_target(server)
            inv = [
                (u.id, u.username, u.host, bool(u.password_encrypted), u.is_active, u.notes)
                for u in session.query(ServerUser)
                .filter(ServerUser.server_id == server_id)
                .all()
            ]
        finally:
            session.close()

        adapter = get_adapter(target)
        is_pg = target.dialect == "postgresql"
        supports_hosts = getattr(adapter, "supports_hosts", not is_pg)
        live = adapter.list_users()

        def key(username: str, host: str | None) -> tuple:
            return (username,) if is_pg else (username, host or "%")

        inv_by_key = {key(r[1], r[2]): r for r in inv}
        groups: dict[str, list[EngineUserIdentityOut]] = {}
        seen: set[tuple] = set()

        for lu in sorted(live, key=lambda x: (x.username, x.host or "")):
            k = key(lu.username, lu.host)
            seen.add(k)
            row = inv_by_key.get(k)
            groups.setdefault(lu.username, []).append(
                EngineUserIdentityOut(
                    host=None if is_pg else (lu.host or "%"),
                    status="adopted" if row else "unmanaged",
                    server_user_id=row[0] if row else None,
                    has_password=row[3] if row else False,
                    is_active=row[4] if row else None,
                    notes=row[5] if row else None,
                )
            )

        for k, row in inv_by_key.items():
            if k in seen:
                continue
            groups.setdefault(row[1], []).append(
                EngineUserIdentityOut(
                    host=None if is_pg else (row[2] or "%"),
                    status="orphan",
                    server_user_id=row[0],
                    has_password=row[3],
                    is_active=row[4],
                    notes=row[5],
                )
            )

        users = [
            GroupedEngineUserOut(username=name, identity_count=len(ids), identities=ids)
            for name, ids in sorted(groups.items())
        ]
        return GroupedEngineUsersOut(
            dialect=target.dialect, supports_hosts=supports_hosts, users=users
        )

    def reveal_password(
        self, server_id: int, username: str, host: str, *, admin: dict | None = None
    ) -> RevealedPasswordOut:
        """
        Revela la contraseña de un usuario ADOPTADO/gestionado — solo posible cuando el
        gateway la fijó y la guarda cifrada. El motor solo guarda un hash irreversible:
        una contraseña que el gateway nunca conoció NO se puede revelar (409).
        """
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            is_pg = engine_value(server) == "postgresql"
            h = host or "%"
            row = self._find_inventory_row(session, server_id, username, h, is_pg=is_pg)
            enc = row.password_encrypted if row else None
            user_id = row.id if row else None
        finally:
            session.close()

        if user_id is None:
            raise AppHttpException(
                message=(
                    "El usuario no está en el inventario del gateway; no hay contraseña "
                    "que revelar. Adóptalo y rota su contraseña por el gateway para gestionarla."
                ),
                status_code=404,
                context={"username": username, "host": None if is_pg else h},
            )
        if not enc:
            raise AppHttpException(
                message=(
                    "El gateway no conoce la contraseña de este usuario (fue adoptado sin "
                    "contraseña o la fijó el motor). Solo se puede rotar, no revelar."
                ),
                status_code=409,
                context={"username": username, "host": None if is_pg else h},
            )

        # Divulgación de un secreto en claro: el rastro debe ser DURABLE (fail-closed),
        # no best-effort. Se audita la intención ANTES de descifrar/retornar; si no se
        # persiste, aborta y el secreto nunca sale.
        audit.record_intent(
            "server_user.password.reveal",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=False,
            detail="INTENT revelar contraseña al admin",
        )
        plaintext = self._decrypt(enc)
        audit.record(
            "server_user.password.reveal",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=False,
            detail="contraseña revelada al admin",
        )
        return RevealedPasswordOut(
            username=username, host=None if is_pg else h, password=plaintext
        )

    def create_user_by_identity(
        self, server_id: int, data: dict, *, admin: dict | None = None
    ) -> EngineUserActionOut:
        username = data["username"]
        host = data.get("host") or "%"
        password = data["password"]
        adopt = bool(data.get("adopt"))
        notes = data.get("notes")

        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
            is_pg = engine_value(server) == "postgresql"
        finally:
            session.close()

        try:
            get_adapter(target).create_user(username, password, host)
        except AppHttpException:
            audit.record(
                "server_user.create",
                status="error",
                admin=admin,
                target_type="server_user",
                server_id=server_id,
                touched_engine=True,
                detail="fallo al crear el usuario en el motor (por identidad)",
            )
            raise

        server_user_id = None
        if adopt:
            server_user_id = self._insert_inventory_row(
                server_id, username, host, self._encrypt(password), notes
            )

        audit.record(
            "server_user.create",
            admin=admin,
            target_type="server_user",
            target_id=server_user_id,
            server_id=server_id,
            touched_engine=True,
            detail="usuario creado en el motor por identidad" + (" + adoptado" if adopt else ""),
        )
        return EngineUserActionOut(
            username=username,
            host=None if is_pg else host,
            adopted=server_user_id is not None,
            server_user_id=server_user_id,
        )

    def set_password_by_identity(
        self, server_id: int, data: dict, *, admin: dict | None = None
    ) -> EngineUserActionOut:
        username = data["username"]
        host = data.get("host") or "%"
        new_password = data["new_password"]
        adopt = bool(data.get("adopt"))

        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
            is_pg = engine_value(server) == "postgresql"
            row = self._find_inventory_row(session, server_id, username, host, is_pg=is_pg)
            existing_id = row.id if row else None
        finally:
            session.close()

        # Motor PRIMERO (no adelantar el inventario al motor).
        try:
            get_adapter(target).change_password(username, new_password, host)
        except AppHttpException:
            audit.record(
                "server_user.update",
                status="error",
                admin=admin,
                target_type="server_user",
                target_id=existing_id,
                server_id=server_id,
                touched_engine=True,
                detail="fallo al cambiar la contraseña en el motor (por identidad)",
            )
            raise

        server_user_id = existing_id
        if existing_id is not None:
            # Fila existente: sincronizar la contraseña cifrada (queda revelable).
            self._update_row_password(existing_id, self._encrypt(new_password))
        elif adopt:
            server_user_id = self._insert_inventory_row(
                server_id, username, host, self._encrypt(new_password), None
            )

        audit.record(
            "server_user.update",
            admin=admin,
            target_type="server_user",
            target_id=server_user_id,
            server_id=server_id,
            touched_engine=True,
            detail="contraseña cambiada en el motor por identidad",
        )
        return EngineUserActionOut(
            username=username,
            host=None if is_pg else host,
            adopted=server_user_id is not None,
            server_user_id=server_user_id,
        )

    def drop_user_by_identity(
        self,
        server_id: int,
        username: str,
        host: str,
        *,
        confirm_username: str | None = None,
        admin: dict | None = None,
    ) -> EngineUserActionOut:
        host = host or "%"
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
            is_pg = engine_value(server) == "postgresql"
            row = self._find_inventory_row(session, server_id, username, host, is_pg=is_pg)
            row_id = row.id if row else None
            owned = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.owner_id == row_id)
                .count()
                if row_id is not None
                else 0
            )
        finally:
            session.close()

        if owned:
            raise AppHttpException(
                message=(
                    "No se puede eliminar: el usuario posee bases de datos gestionadas. "
                    "Reasigna o elimina esas BDs primero."
                ),
                status_code=409,
                context={"username": username, "owned_databases": owned},
            )
        if confirm_username != username:
            raise AppHttpException(
                message=(
                    "Confirmación requerida: para ejecutar DROP USER en el motor, "
                    "'confirm_username' debe coincidir exactamente con el username."
                ),
                status_code=422,
                context={"required": "confirm_username == username"},
            )

        audit.record_intent(
            "server_user.delete",
            admin=admin,
            target_type="server_user",
            target_id=row_id,
            server_id=server_id,
            detail="DROP USER por identidad solicitado (confirmado)",
        )
        try:
            get_adapter(target).drop_user(username, host)
        except AppHttpException:
            audit.record(
                "server_user.delete",
                status="error",
                admin=admin,
                target_type="server_user",
                target_id=row_id,
                server_id=server_id,
                touched_engine=True,
                detail="fallo al eliminar el usuario en el motor (por identidad)",
            )
            raise

        if row_id is not None:
            self._delete_row(row_id)

        audit.record(
            "server_user.delete",
            admin=admin,
            target_type="server_user",
            target_id=row_id,
            server_id=server_id,
            touched_engine=True,
            detail="usuario eliminado del motor por identidad",
        )
        return EngineUserActionOut(
            username=username, host=None if is_pg else host, adopted=False, server_user_id=None
        )

    def add_host(
        self, server_id: int, data: dict, *, admin: dict | None = None
    ) -> AddHostOut:
        username = data["username"]
        source_host = data.get("source_host") or "%"
        new_host = data["new_host"]
        reuse_password = data.get("reuse_password", True)
        new_password = data.get("new_password")
        copy_grants = bool(data.get("copy_grants"))
        adopt = bool(data.get("adopt"))
        notes = data.get("notes")

        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
        finally:
            session.close()

        adapter = get_adapter(target)
        if not getattr(adapter, "supports_hosts", True):
            raise AppHttpException(
                message=(
                    "Este motor no usa host por usuario; 'agregar host' no aplica "
                    "(en PostgreSQL el acceso por host se gestiona en pg_hba.conf)."
                ),
                status_code=422,
                context={"dialect": adapter.dialect},
            )

        audit.record_intent(
            "server_user.add_host",
            admin=admin,
            target_type="server_user",
            server_id=server_id,
            detail=f"CREATE USER {username}@{new_host} (clon de {username}@{source_host})",
        )
        try:
            adapter.add_user_host(
                username,
                source_host,
                new_host,
                new_password=None if reuse_password else new_password,
            )
        except AppHttpException:
            audit.record(
                "server_user.add_host",
                status="error",
                admin=admin,
                target_type="server_user",
                server_id=server_id,
                touched_engine=True,
                detail=f"fallo al crear {username}@{new_host}",
            )
            raise

        grants_copied = 0
        grants_error = None
        if copy_grants:
            try:
                grants_copied = adapter.copy_user_grants(username, source_host, new_host)
            except AppHttpException as exc:  # best-effort: el host ya se creó
                # exc.message ya viene redactado por map_driver_error (sin DSN/credenciales).
                grants_error = exc.message
            except Exception:  # noqa: BLE001 — nunca volcar detalle crudo del driver al cliente
                grants_error = "No se pudieron copiar todos los grants al nuevo host."

        server_user_id = None
        if adopt:
            pw_enc = self._encrypt(new_password) if (not reuse_password and new_password) else None
            server_user_id = self._insert_inventory_row(
                server_id, username, new_host, pw_enc, notes
            )

        audit.record(
            "server_user.add_host",
            admin=admin,
            target_type="server_user",
            target_id=server_user_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"host agregado: {username}@{new_host} "
                f"({'hash reusado' if reuse_password else 'nueva contraseña'}, "
                f"{grants_copied} grants)"
            ),
        )
        return AddHostOut(
            username=username,
            new_host=new_host,
            password_mode="reused" if reuse_password else "new",
            grants_copied=grants_copied,
            grants_error=grants_error,
            adopted=server_user_id is not None,
            server_user_id=server_user_id,
        )

    # ------------------------------------------------------------------ #
    # Operaciones MASIVAS: todos los hosts en vivo de un username.        #
    # No hay tabla "usuario lógico": se orquesta iterando adapter.list_users() #
    # (plano real del motor) + las filas físicas de ServerUser existentes. #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _live_hosts_for_username(adapter, username: str, *, is_pg: bool) -> list[str | None]:
        """Hosts en vivo (motor) de un username. En PG: [None] si existe, [] si no."""
        live = adapter.list_users()
        if is_pg:
            return [None] if any(u.username == username for u in live) else []
        return [u.host or "%" for u in live if u.username == username]

    def adopt_user_all_hosts(
        self, server_id: int, data: dict, *, admin: dict | None = None
    ) -> BatchAdoptOut:
        """
        Adopta TODAS las identidades en vivo de un username en una sola operación
        (nunca ejecuta CREATE USER). Con ``known_password`` opcional, la guarda cifrada
        en todas las filas adoptadas (nunca ejecuta ALTER USER: es responsabilidad del
        admin que el valor coincida con la contraseña real del motor).
        """
        username = data["username"]
        known_password = data.get("known_password")
        notes = data.get("notes")

        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
            is_pg = target.dialect == "postgresql"
        finally:
            session.close()

        adapter = get_adapter(target)
        hosts = self._live_hosts_for_username(adapter, username, is_pg=is_pg)
        if not hosts:
            raise AppHttpException(
                message="El usuario no existe en el motor; no hay nada que adoptar.",
                status_code=404,
                context={"username": username},
            )

        password_encrypted = self._encrypt(known_password) if known_password else None

        results: list[AdoptAllHostsItemOut] = []
        adopted_count = 0
        for host in hosts:
            h = host or "%"
            server_user_id = self._insert_inventory_row(
                server_id, username, h, password_encrypted, notes
            )
            if server_user_id is not None:
                adopted_count += 1
                results.append(
                    AdoptAllHostsItemOut(
                        host=None if is_pg else h, status="adopted", server_user_id=server_user_id
                    )
                )
                continue

            # Ya existía: recuperar su id para reportarlo (no es fatal, el motor no se tocó).
            session = self._session()
            try:
                row = self._find_inventory_row(session, server_id, username, h, is_pg=is_pg)
                existing_id = row.id if row else None
            finally:
                session.close()
            results.append(
                AdoptAllHostsItemOut(
                    host=None if is_pg else h, status="already_adopted", server_user_id=existing_id
                )
            )

        audit.record(
            "server_user.adopt_batch",
            admin=admin,
            target_type="server_user",
            server_id=server_id,
            touched_engine=False,
            detail=(
                f"{adopted_count}/{len(hosts)} hosts adoptados para '{username}'"
                + (" + contraseña definida (sin ALTER USER)" if known_password else "")
            ),
        )
        return BatchAdoptOut(
            username=username,
            dialect=target.dialect,
            total_hosts=len(hosts),
            adopted=adopted_count,
            results=results,
        )

    def set_known_password(
        self, server_id: int, data: dict, *, admin: dict | None = None
    ) -> KnownPasswordSetOut:
        """
        Registra una contraseña YA conocida por el admin humano SIN ejecutar ALTER
        USER/ROLE — solo cifra y guarda, para habilitar reveal-password. Nunca toca el
        motor. Distinto de set_password_by_identity/_all_hosts, que sí rotan de verdad.
        """
        username = data["username"]
        scope = data.get("scope", "host")
        known_password = data["known_password"]
        adopt_if_missing = bool(data.get("adopt_if_missing"))
        overwrite = bool(data.get("overwrite"))

        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
            is_pg = target.dialect == "postgresql"
        finally:
            session.close()

        adapter = get_adapter(target)
        live_hosts = self._live_hosts_for_username(adapter, username, is_pg=is_pg)
        if scope == "all_hosts":
            if not live_hosts:
                raise AppHttpException(
                    message=(
                        "El usuario no existe en el motor; no hay hosts sobre los que "
                        "definir la contraseña."
                    ),
                    status_code=404,
                    context={"username": username},
                )
            hosts = live_hosts
        else:
            hosts = [None] if is_pg else [data.get("host") or "%"]

        live_set = {h or "%" for h in live_hosts}
        password_encrypted = self._encrypt(known_password)

        results: list[KnownPasswordSetItemOut] = []
        updated_count = 0
        for host in hosts:
            h = host or "%"
            session = self._session()
            try:
                row = self._find_inventory_row(session, server_id, username, h, is_pg=is_pg)
                row_id = row.id if row else None
                has_existing_password = bool(row and row.password_encrypted)
            finally:
                session.close()

            if row_id is not None:
                if has_existing_password and not overwrite:
                    results.append(
                        KnownPasswordSetItemOut(
                            host=None if is_pg else h,
                            status="conflict_needs_overwrite",
                            server_user_id=row_id,
                        )
                    )
                    continue
                if has_existing_password:
                    # Sobrescribe un valor que ya era revelable: auditar fail-closed
                    # ANTES de escribir (misma clase de riesgo que reveal_password).
                    audit.record_intent(
                        "server_user.password.define",
                        admin=admin,
                        target_type="server_user",
                        target_id=row_id,
                        server_id=server_id,
                        touched_engine=False,
                        detail=f"INTENT sobrescribir contraseña conocida de '{username}'@'{h}'",
                    )
                self._update_row_password(row_id, password_encrypted)
                updated_count += 1
                results.append(
                    KnownPasswordSetItemOut(
                        host=None if is_pg else h, status="updated", server_user_id=row_id
                    )
                )
            elif adopt_if_missing and h in live_set:
                new_id = self._insert_inventory_row(
                    server_id, username, h, password_encrypted, None
                )
                if new_id is not None:
                    updated_count += 1
                    results.append(
                        KnownPasswordSetItemOut(
                            host=None if is_pg else h, status="adopted", server_user_id=new_id
                        )
                    )
                else:
                    results.append(
                        KnownPasswordSetItemOut(
                            host=None if is_pg else h, status="skipped_not_found", server_user_id=None
                        )
                    )
            else:
                results.append(
                    KnownPasswordSetItemOut(
                        host=None if is_pg else h, status="skipped_not_found", server_user_id=None
                    )
                )

        audit.record(
            "server_user.password.define",
            admin=admin,
            target_type="server_user",
            server_id=server_id,
            touched_engine=False,
            detail=(
                f"contraseña definida en {updated_count}/{len(hosts)} identidad(es) de "
                f"'{username}' (scope={scope}, sin ALTER USER)"
            ),
        )
        return KnownPasswordSetOut(
            username=username,
            scope=scope,
            total_hosts=len(hosts),
            updated=updated_count,
            results=results,
        )

    def set_password_by_identity_all_hosts(
        self, server_id: int, data: dict, *, admin: dict | None = None
    ) -> PasswordChangeBatchOut:
        """
        Rota la contraseña REAL (ALTER USER/ROLE) en TODOS los hosts en vivo de un
        username. ``confirm_username`` ya fue validado por el schema (debe coincidir
        con ``username``). Fail-tolerant por host: un fallo en uno no aborta el resto.
        """
        username = data["username"]
        new_password = data["new_password"]
        adopt_if_missing = bool(data.get("adopt_if_missing"))

        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            self._guard_not_root(server.root_username, username)
            target = build_target(server)
            is_pg = target.dialect == "postgresql"
        finally:
            session.close()

        adapter = get_adapter(target)
        hosts = self._live_hosts_for_username(adapter, username, is_pg=is_pg)
        if not hosts:
            raise AppHttpException(
                message=(
                    "El usuario no existe en el motor; no hay hosts sobre los que "
                    "rotar la contraseña."
                ),
                status_code=404,
                context={"username": username},
            )

        # Auditar la INTENCIÓN del lote completo, fail-closed, ANTES de iterar: es un
        # ALTER USER real sobre N cuentas de una sola vez.
        audit.record_intent(
            "server_user.password.rotate_batch",
            admin=admin,
            target_type="server_user",
            server_id=server_id,
            touched_engine=True,
            detail=f"INTENT rotar contraseña en {len(hosts)} host(s) de '{username}' (confirmado)",
        )

        password_encrypted = self._encrypt(new_password)
        results: list[PasswordChangeItemOut] = []
        updated_count = 0
        for host in hosts:
            h = host or "%"
            try:
                adapter.change_password(username, new_password, h)
            except AppHttpException as exc:
                results.append(
                    PasswordChangeItemOut(host=None if is_pg else h, status="error", error=exc.message)
                )
                continue

            session = self._session()
            try:
                row = self._find_inventory_row(session, server_id, username, h, is_pg=is_pg)
                row_id = row.id if row else None
            finally:
                session.close()

            server_user_id = row_id
            adopted_now = False
            if row_id is not None:
                self._update_row_password(row_id, password_encrypted)
            elif adopt_if_missing:
                server_user_id = self._insert_inventory_row(
                    server_id, username, h, password_encrypted, None
                )
                adopted_now = server_user_id is not None

            updated_count += 1
            results.append(
                PasswordChangeItemOut(
                    host=None if is_pg else h,
                    status="rotated",
                    server_user_id=server_user_id,
                    adopted=adopted_now,
                )
            )

        audit.record(
            "server_user.password.rotate_batch",
            admin=admin,
            target_type="server_user",
            server_id=server_id,
            touched_engine=True,
            detail=f"{updated_count}/{len(hosts)} hosts rotados para '{username}'",
        )
        return PasswordChangeBatchOut(
            username=username, total_hosts=len(hosts), updated=updated_count, results=results
        )

    # ---- helpers de inventario para el flujo por identidad -------------------- #
    def _insert_inventory_row(
        self, server_id: int, username: str, host: str, password_encrypted: str | None, notes
    ) -> int | None:
        """Inserta la fila de inventario; None si ya existía (no es fatal: el motor ya se tocó)."""
        session = self._session()
        try:
            u = ServerUser(
                server_id=server_id,
                username=username,
                host=host or "%",
                password_encrypted=password_encrypted,
                notes=notes,
                is_active=True,
            )
            session.add(u)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
            session.refresh(u)
            return u.id
        finally:
            session.close()

    def _update_row_password(self, user_id: int, password_encrypted: str) -> None:
        session = self._session()
        try:
            u = session.get(ServerUser, user_id)
            if u:
                u.password_encrypted = password_encrypted
                session.commit()
        finally:
            session.close()
