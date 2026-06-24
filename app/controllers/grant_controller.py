"""
Controller de grants granulares (GRANT/REVOKE/LIST sobre objetos del motor).

Flujo de grant:
  1. Cargar ServerUser → Server → construir ServerTarget (credencial admin).
  2. Pre-chequear capability: adapter.can_grant() — fail-fast 403 antes de tocar el motor.
  3. Ejecutar adapter.grant_object() contra el motor destino.
  4. Auditar.

Flujo de revoke: no pre-chequea can_grant (REVOKE solo requiere tener el privilegio
otorgado, no with-grant-option). El error del motor es la red de seguridad.
"""

from app.controllers.common import build_target, get_server_or_404
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.permission_profile import PermissionProfile, PermissionProfileItem
from app.models.server import Server
from app.models.server_user import ServerUser
from app.schemas.grant import (
    ApplyProfileRequest,
    ApplyProfileResult,
    GrantRequest,
    GrantableRequest,
    RevokeRequest,
)
from app.services import audit
from app.services.db_admin.dtos import EngineUserInfo, GrantInfo, GrantLevel, ObjectRef
from app.services.db_admin.factory import get_adapter


class GrantController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    def _load_user_context(self, session, user_id: int):
        """Carga ServerUser + Server + adapter. Devuelve (user, server_id, adapter, grantee)."""
        user = session.get(ServerUser, user_id)
        if not user:
            raise AppHttpException(
                message="Usuario de servidor no encontrado.",
                status_code=404,
                context={"server_user_id": user_id},
            )
        server = get_server_or_404(session, user.server_id)
        target = build_target(server)
        adapter = get_adapter(target)
        grantee = EngineUserInfo(username=user.username, host=user.host)
        return user, server.id, adapter, grantee

    # ------------------------------------------------------------------ #
    # Lectura                                                              #
    # ------------------------------------------------------------------ #
    def list_grants(self, user_id: int, database: str | None = None) -> list[GrantInfo]:
        session = self._session()
        try:
            _, _, adapter, grantee = self._load_user_context(session, user_id)
        finally:
            session.close()
        return adapter.list_grants(grantee, database=database)

    # ------------------------------------------------------------------ #
    # Grant                                                                #
    # ------------------------------------------------------------------ #
    def grant_object(
        self, user_id: int, payload: GrantRequest, *, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            user, server_id, adapter, grantee = self._load_user_context(session, user_id)
            username = user.username
        finally:
            session.close()

        # Pre-chequeo: ¿la credencial del gateway puede delegar estos privilegios?
        if not adapter.can_grant(payload.level, payload.object_ref, payload.privileges):
            raise AppHttpException(
                message=(
                    "La credencial del gateway no tiene permisos suficientes para "
                    "otorgar estos privilegios. Verifica que la cuenta admin tenga "
                    "WITH GRANT OPTION para los privilegios solicitados."
                ),
                status_code=403,
                context={
                    "level": payload.level.value,
                    "privileges": payload.privileges,
                    "username": username,
                },
            )

        adapter.grant_object(
            grantee,
            payload.level,
            payload.object_ref,
            payload.privileges,
            with_grant_option=payload.with_grant_option,
        )

        audit.record(
            "server_user.grant_object",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"GRANT {','.join(payload.privileges)} ON {payload.level.value} "
                f"TO {username}"
                + (" WITH GRANT OPTION" if payload.with_grant_option else "")
            ),
        )
        return {
            "granted": True,
            "level": payload.level.value,
            "privileges": payload.privileges,
            "with_grant_option": payload.with_grant_option,
        }

    # ------------------------------------------------------------------ #
    # Revoke                                                               #
    # ------------------------------------------------------------------ #
    def revoke_object(
        self, user_id: int, payload: RevokeRequest, *, admin: dict | None = None
    ) -> None:
        session = self._session()
        try:
            user, server_id, adapter, grantee = self._load_user_context(session, user_id)
            username = user.username
        finally:
            session.close()

        adapter.revoke_object(grantee, payload.level, payload.object_ref, payload.privileges)

        audit.record(
            "server_user.revoke_object",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=True,
            detail=f"REVOKE {','.join(payload.privileges)} ON {payload.level.value} FROM {username}",
        )

    # ------------------------------------------------------------------ #
    # Grantable check (consulta sobre el servidor, no sobre un usuario)   #
    # ------------------------------------------------------------------ #
    def check_grantable(self, server_id: int, payload: GrantableRequest) -> bool:
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            target = build_target(server)
        finally:
            session.close()
        adapter = get_adapter(target)
        return adapter.can_grant(payload.level, payload.object_ref, payload.privileges)

    # ------------------------------------------------------------------ #
    # Apply permission profile                                             #
    # ------------------------------------------------------------------ #
    def apply_profile(
        self,
        user_id: int,
        profile_id: int,
        payload: ApplyProfileRequest,
        *,
        admin: dict | None = None,
    ) -> ApplyProfileResult:
        """
        Aplica un perfil de permisos a un usuario. Para cada item del perfil, busca
        el ``object_mapping`` correspondiente en el payload y ejecuta ``grant_object``.
        Los niveles del perfil sin mapeo se omiten (se reportan en ``skipped_levels``).
        Los errores de grant individuales se capturan para dar visibilidad sin abortar.
        """
        session = self._session()
        try:
            _, server_id, adapter, grantee = self._load_user_context(session, user_id)
            # Cargar el perfil
            profile = session.get(PermissionProfile, profile_id)
            if not profile:
                raise AppHttpException(
                    message="Perfil de permisos no encontrado.",
                    status_code=404,
                    context={"profile_id": profile_id},
                )
            server = get_server_or_404(session, server_id)
            engine = server.engine.value if hasattr(server.engine, "value") else str(server.engine)
            if profile.engine != engine:
                raise AppHttpException(
                    message=(
                        f"El perfil es para motor '{profile.engine}' pero el servidor usa '{engine}'."
                    ),
                    status_code=422,
                    context={"profile_engine": profile.engine, "server_engine": engine},
                )
            items = (
                session.query(PermissionProfileItem)
                .filter(PermissionProfileItem.profile_id == profile_id)
                .all()
            )
            profile_name = profile.name
        finally:
            session.close()

        # Índice de mappings por nivel
        mapping_index: dict[GrantLevel, ObjectRef] = {
            m.level: m.object_ref for m in payload.object_mappings
        }

        grants_applied = 0
        skipped_levels: list[str] = []
        errors: list[str] = []

        for item in items:
            level = GrantLevel(item.level)
            privileges = [p.strip() for p in item.privileges.split(",") if p.strip()]
            ref = mapping_index.get(level)
            if ref is None:
                skipped_levels.append(level.value)
                continue
            try:
                if not adapter.can_grant(level, ref, privileges):
                    errors.append(
                        f"{level.value}: credencial sin permisos suficientes para {privileges}"
                    )
                    continue
                adapter.grant_object(grantee, level, ref, privileges)
                grants_applied += 1
            except Exception as exc:  # noqa: BLE001 — best-effort; reportar, no abortar
                errors.append(f"{level.value}: {exc}")

        audit.record(
            "server_user.apply_profile",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"profile_id={profile_id} ({profile_name}): "
                f"{grants_applied} grants aplicados, {len(skipped_levels)} omitidos"
            ),
        )
        return ApplyProfileResult(
            profile_id=profile_id,
            profile_name=profile_name,
            engine=engine,
            grants_applied=grants_applied,
            skipped_levels=skipped_levels,
            errors=errors,
        )
