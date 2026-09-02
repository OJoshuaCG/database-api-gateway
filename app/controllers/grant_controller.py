"""
Controller de grants granulares (GRANT/REVOKE/LIST sobre objetos del motor).

Flujo de grant:
  1. Cargar ServerUser → Server → construir ServerTarget (credencial admin).
  2. Pre-chequear capability: adapter.can_grant() — fail-fast 403 antes de tocar el motor.
  3. Si es operación GATE (with_grant_option o privilegio sensible): auditar la
     INTENCIÓN (fail-closed) antes de ejecutar.
  4. Ejecutar adapter.grant_object() contra el motor destino.
  5. Auditar el resultado (con campos DCL granulares).

Flujo de revoke: no pre-chequea can_grant (REVOKE solo requiere tener el privilegio
otorgado). Guards adicionales: anti auto-lockout (no revocar a la propia credencial del
gateway → 409) y CASCADE solo con confirmación explícita. La intención de TODO REVOKE
se audita fail-closed antes de ejecutar.
"""

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.permission_profile import PermissionProfile, PermissionProfileItem
from app.models.server_user import ServerUser
from app.schemas.grant import (
    ApplyProfileBulkItemOut,
    ApplyProfileBulkRequest,
    ApplyProfileBulkResult,
    ApplyProfileRequest,
    ApplyProfileResult,
    EngineUserGrantsOut,
    GrantableRequest,
    GrantRequest,
    RevokeRequest,
)
from app.services import audit
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.dtos import EngineUserInfo, GrantInfo, GrantLevel, ObjectRef
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import validate_host, validate_identifier

logger = get_logger(__name__)


def _grantee_label(grantee: EngineUserInfo) -> str:
    """Etiqueta legible del beneficiario para auditoría: ``user@host`` o ``user``."""
    return f"{grantee.username}@{grantee.host}" if grantee.host else grantee.username


def _summarize_names(names: list[str], *, cap: int = 20) -> str:
    """
    Lista acotada de nombres para el ``detail`` de auditoría. Con lotes realistas queda
    completa; con uno patológico (100 BDs) se corta para que ``detail`` siga siendo un
    resumen legible. Nunca contiene credenciales (son nombres de BD validados).
    """
    if len(names) <= cap:
        return ",".join(names)
    return ",".join(names[:cap]) + f" (+{len(names) - cap} más)"


def _object_name(ref: ObjectRef) -> str | None:
    """Construye un nombre de objeto legible (sin credenciales) para auditoría."""
    segs = [s for s in (ref.database, ref.db_schema, ref.table or ref.sequence) if s]
    name = ".".join(segs)
    if ref.routine is not None:
        rname = getattr(ref.routine, "name", None) or getattr(ref.routine, "kind", "")
        name = f"{name}.{rname}" if name else rname
    if ref.columns:
        name += "(" + ",".join(ref.columns) + ")"
    return name or None


class GrantController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    def _load_user_context(self, session, user_id: int):
        """
        Carga ServerUser + Server + adapter. Devuelve
        ``(user, server_id, adapter, grantee, root_username)``.
        ``root_username`` es la credencial pseudo-root del gateway (grantor), usada para
        auditoría y para el guard anti auto-lockout.
        """
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
        return user, server.id, adapter, grantee, server.root_username

    # ------------------------------------------------------------------ #
    # Lectura                                                              #
    # ------------------------------------------------------------------ #
    def list_grants(self, user_id: int, database: str | None = None) -> list[GrantInfo]:
        session = self._session()
        try:
            _, _, adapter, grantee, _ = self._load_user_context(session, user_id)
        finally:
            session.close()
        return adapter.list_grants(grantee, database=database)

    def list_grants_by_identity(
        self,
        server_id: int,
        username: str,
        host: str | None = None,
        database: str | None = None,
    ) -> EngineUserGrantsOut:
        """
        Grants de un usuario por IDENTIDAD, sin fila de inventario.

        ``list_grants`` (por ``user_id``) sirve solo para usuarios adoptados: el 404 sale
        del inventario, no del motor. Pero el gateway administra servidores de terceros
        donde la mayoría de las cuentas nunca pasaron por acá, y auditar los permisos de
        una cuenta ajena no debería obligar a adoptarla primero (adoptar es un efecto de
        escritura sobre el inventario para responder una pregunta de solo lectura).

        Se valida la existencia REAL en el motor antes de introspeccionar: un typo en el
        ``username`` devolvería una lista vacía, indistinguible de "existe y no tiene
        ningún privilegio" — que es justo la conclusión peligrosa en una auditoría de
        permisos. Cuesta un ``list_users()`` extra y lo vale.
        """
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            is_pg = dialect == "postgresql"
            target = build_target(server)
            effective_host = None if is_pg else (host or "%")

            validate_identifier(username, dialect, "usuario", allow_existing=True)
            if effective_host is not None:
                validate_host(effective_host)

            query = session.query(ServerUser).filter(
                ServerUser.server_id == server_id,
                ServerUser.username == username,
            )
            if not is_pg:
                query = query.filter(ServerUser.host == effective_host)
            row = query.first()
            server_user_id = row.id if row else None
        finally:
            session.close()

        adapter = get_adapter(target)
        live = adapter.list_users()
        if not any(
            u.username == username and (is_pg or (u.host or "%") == effective_host)
            for u in live
        ):
            raise AppHttpException(
                message="El usuario no existe en el motor.",
                status_code=404,
                context={"username": username, "host": effective_host},
            )

        grantee = EngineUserInfo(username=username, host=effective_host)
        grants = adapter.list_grants(grantee, database=database)
        return EngineUserGrantsOut(
            username=username,
            host=effective_host,
            status="adopted" if server_user_id is not None else "unmanaged",
            server_user_id=server_user_id,
            grants=grants,
        )

    # ------------------------------------------------------------------ #
    # Grant                                                                #
    # ------------------------------------------------------------------ #
    def grant_object(
        self, user_id: int, payload: GrantRequest, *, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            user, server_id, adapter, grantee, grantor = self._load_user_context(
                session, user_id
            )
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

        # ¿Operación GATE? (privilegio sensible o WITH GRANT OPTION) → auditar intención.
        _, requires_confirmation = priv_catalog.validate_privileges(
            payload.privileges, adapter.dialect, payload.level
        )
        is_gate = requires_confirmation or payload.with_grant_option

        priv_csv = ",".join(payload.privileges)
        obj_name = _object_name(payload.object_ref)
        audit_fields = dict(
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            grantee=_grantee_label(grantee),
            privilege=priv_csv,
            object_level=payload.level.value,
            object_name=obj_name,
            with_grant_option=payload.with_grant_option,
            grantor=grantor,
        )

        if is_gate:
            audit.record_intent(
                "server_user.grant_object",
                detail=f"INTENT GRANT {priv_csv} ON {payload.level.value} TO {username}",
                **audit_fields,
            )

        try:
            adapter.grant_object(
                grantee,
                payload.level,
                payload.object_ref,
                payload.privileges,
                with_grant_option=payload.with_grant_option,
            )
        except Exception:
            audit.record(
                "server_user.grant_object",
                status="error",
                touched_engine=True,
                detail=f"GRANT {priv_csv} ON {payload.level.value} TO {username} (falló)",
                **audit_fields,
            )
            raise

        audit.record(
            "server_user.grant_object",
            touched_engine=True,
            detail=(
                f"GRANT {priv_csv} ON {payload.level.value} TO {username}"
                + (" WITH GRANT OPTION" if payload.with_grant_option else "")
            ),
            **audit_fields,
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
        self,
        user_id: int,
        payload: RevokeRequest,
        *,
        confirm_grantee: str | None = None,
        admin: dict | None = None,
    ) -> None:
        session = self._session()
        try:
            user, server_id, adapter, grantee, grantor = self._load_user_context(
                session, user_id
            )
            username = user.username
        finally:
            session.close()

        # Guard anti auto-lockout: nunca revocar a la propia credencial del gateway.
        if grantor and username.lower() == grantor.lower():
            raise AppHttpException(
                message=(
                    "No se puede revocar privilegios a la propia credencial del gateway "
                    "(riesgo de auto-bloqueo). Para degradar esa cuenta, hazlo fuera del "
                    "gateway."
                ),
                status_code=409,
                context={"username": username, "grantor": grantor},
            )

        # CASCADE: operación GATE — solo con confirmación explícita (repetir el username).
        if payload.cascade:
            if adapter.dialect in ("mysql", "mariadb"):
                raise AppHttpException(
                    message="MySQL/MariaDB no soporta REVOKE ... CASCADE.",
                    status_code=422,
                    context={"dialect": adapter.dialect},
                )
            if confirm_grantee != username:
                raise AppHttpException(
                    message=(
                        "REVOKE ... CASCADE es destructivo: repite el username del grantee "
                        "en 'confirm_grantee' para confirmar."
                    ),
                    status_code=422,
                    context={"username": username, "cascade": True},
                )

        priv_csv = ",".join(payload.privileges)
        obj_name = _object_name(payload.object_ref)
        audit_fields = dict(
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=server_id,
            grantee=_grantee_label(grantee),
            privilege=priv_csv,
            object_level=payload.level.value,
            object_name=obj_name,
            grantor=grantor,
        )

        # Auditoría de intención fail-closed: TODO REVOKE deja rastro antes de ejecutar.
        cascade_tag = " CASCADE" if payload.cascade else ""
        audit.record_intent(
            "server_user.revoke_object",
            detail=f"INTENT REVOKE{cascade_tag} {priv_csv} ON {payload.level.value} FROM {username}",
            **audit_fields,
        )

        try:
            adapter.revoke_object(
                grantee,
                payload.level,
                payload.object_ref,
                payload.privileges,
                cascade=payload.cascade,
            )
        except Exception:
            audit.record(
                "server_user.revoke_object",
                status="error",
                touched_engine=True,
                detail=f"REVOKE{cascade_tag} {priv_csv} ON {payload.level.value} FROM {username} (falló)",
                **audit_fields,
            )
            raise

        audit.record(
            "server_user.revoke_object",
            touched_engine=True,
            detail=f"REVOKE{cascade_tag} {priv_csv} ON {payload.level.value} FROM {username}",
            **audit_fields,
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
    def _load_apply_profile_context(self, user_id: int, profile_id: int) -> dict:
        """
        Carga en UNA sola sesión todo lo que necesita un apply de perfil: contexto del
        usuario (ServerUser → Server → adapter), el perfil, la compatibilidad de motor y
        los items del perfil. Compartido por ``apply_profile`` y ``apply_profile_bulk``
        para que las validaciones (404 usuario / 404 perfil / 422 motor incompatible)
        sean idénticas en ambos caminos.

        Los items se MATERIALIZAN a tuplas ``(level_raw, privileges_raw)`` antes de
        cerrar la sesión: en el camino bulk se recorren N veces (una por BD) DESPUÉS del
        cierre, y un ORM detached que necesitara refrescar un atributo reventaría a
        mitad del lote con ``DetachedInstanceError``. Además la sesión de la BD del
        gateway no debe quedar tomada mientras se abren N conexiones remotas.
        """
        session = self._session()
        try:
            _, server_id, adapter, grantee, grantor = self._load_user_context(
                session, user_id
            )
            # Cargar el perfil
            profile = session.get(PermissionProfile, profile_id)
            if not profile:
                raise AppHttpException(
                    message="Perfil de permisos no encontrado.",
                    status_code=404,
                    context={"profile_id": profile_id},
                )
            if not profile.is_active:
                raise AppHttpException(
                    message=(
                        f"El perfil '{profile.name}' está desactivado y no puede aplicarse. "
                        "Reactivalo antes de asignarlo."
                    ),
                    status_code=409,
                    context={"profile_id": profile_id},
                )
            server = get_server_or_404(session, server_id)
            engine = engine_value(server)
            # Los items se necesitan ANTES de decidir la compatibilidad de motor: el cruce
            # dentro de la familia se valida por PRIVILEGIO, no por nombre de motor.
            items = [
                (row.level, row.privileges)
                for row in session.query(PermissionProfileItem)
                .filter(PermissionProfileItem.profile_id == profile_id)
                .all()
            ]
            profile_name = profile.name
            profile_engine = profile.engine
        finally:
            session.close()

        if profile_engine != engine:
            incompatibles = [
                f"{lvl}: {privs}"
                for lvl, privs in items
                if not priv_catalog.tokens_valid_for(
                    engine,
                    GrantLevel(lvl),
                    [p.strip() for p in privs.split(",") if p.strip()],
                )
            ]
            if not priv_catalog.same_family(profile_engine, engine) or incompatibles:
                detail = (
                    f" Privilegios no válidos en '{engine}': {'; '.join(incompatibles)}."
                    if incompatibles
                    else ""
                )
                raise AppHttpException(
                    message=(
                        f"El perfil es para motor '{profile_engine}' y no es aplicable a un "
                        f"servidor '{engine}'.{detail}"
                    ),
                    status_code=422,
                    context={
                        "profile_engine": profile_engine,
                        "server_engine": engine,
                        "incompatible_items": incompatibles,
                    },
                )

        return {
            "server_id": server_id,
            "adapter": adapter,
            "grantee": grantee,
            "grantor": grantor,
            "engine": engine,
            "profile_name": profile_name,
            "items": items,
        }

    @staticmethod
    def _apply_profile_items(
        items: list[tuple[str, str]],
        mapping_index: dict[GrantLevel, ObjectRef],
        adapter,
        grantee: EngineUserInfo,
        engine: str,
    ) -> tuple[int, list[str], list[str]]:
        """
        Ejecuta los items de un perfil contra UN conjunto de objetos destino (un
        ``ObjectRef`` por nivel). Devuelve ``(grants_applied, skipped_levels, errors)``.

        Best-effort POR ITEM: un nivel que falla se reporta en ``errors`` y NO aborta los
        demás niveles — el perfil queda aplicado parcialmente y el operador ve exactamente
        qué faltó (nunca un rollback implícito de grants ya otorgados).

        Compartido por ``apply_profile`` (una BD) y ``apply_profile_bulk`` (N BDs) para
        que el criterio de éxito/omisión/error sea idéntico en ambos caminos.
        """
        grants_applied = 0
        skipped_levels: list[str] = []
        errors: list[str] = []

        for level_raw, privileges_raw in items:
            level = GrantLevel(level_raw)
            raw_privileges = [p.strip() for p in privileges_raw.split(",") if p.strip()]
            ref = mapping_index.get(level)
            if ref is None:
                skipped_levels.append(level.value)
                continue
            try:
                # Canonicalizar contra el motor del SERVIDOR (no el del perfil): con el
                # cruce de familia habilitado, el perfil puede venir del otro motor.
                privileges, _ = priv_catalog.validate_privileges(
                    raw_privileges, engine, level
                )
                if not adapter.can_grant(level, ref, privileges):
                    errors.append(
                        f"{level.value}: credencial sin permisos suficientes para {privileges}"
                    )
                    continue
                adapter.grant_object(grantee, level, ref, privileges)
                grants_applied += 1
            except AppHttpException as exc:
                # ``exc.message`` es el texto pensado para el cliente. ``str(exc)`` en
                # cambio vuelca el ``detail`` COMPLETO de la excepción (incluye ``loc``:
                # archivo/función/línea/código fuente) porque ``HTTPException`` pasa
                # ``detail`` como argumento posicional a ``Exception.__init__`` — y esto
                # va en una respuesta 200, así que el filtrado por entorno del exception
                # handler nunca lo intercepta (se filtraría incluso en producción).
                errors.append(f"{level.value}: {exc.message}")
            except Exception as exc:  # noqa: BLE001 — best-effort; reportar, no abortar
                errors.append(f"{level.value}: {type(exc).__name__}")

        return grants_applied, skipped_levels, errors

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
        ctx = self._load_apply_profile_context(user_id, profile_id)

        # Índice de mappings por nivel
        mapping_index: dict[GrantLevel, ObjectRef] = {
            m.level: m.object_ref for m in payload.object_mappings
        }

        grants_applied, skipped_levels, errors = self._apply_profile_items(
            ctx["items"], mapping_index, ctx["adapter"], ctx["grantee"], ctx["engine"]
        )

        audit.record(
            "server_user.apply_profile",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=ctx["server_id"],
            touched_engine=True,
            grantee=_grantee_label(ctx["grantee"]),
            grantor=ctx["grantor"],
            detail=(
                f"profile_id={profile_id} ({ctx['profile_name']}): "
                f"{grants_applied} grants aplicados, {len(skipped_levels)} omitidos"
            ),
        )
        # No aplicar NADA es un fallo, no un éxito silencioso: antes esto devolvía 200 y
        # los motivos quedaban enterrados en skipped_levels/errors. La auditoría ya quedó
        # registrada arriba (el intento ocurrió), así que recién ahora se corta.
        if grants_applied == 0:
            reasons: list[str] = []
            if skipped_levels:
                reasons.append(
                    "niveles del perfil sin objeto asignado: " + ", ".join(skipped_levels)
                )
            if errors:
                reasons.append("errores: " + "; ".join(errors))
            raise AppHttpException(
                message=(
                    f"No se aplicó ningún permiso del perfil '{ctx['profile_name']}'. "
                    + (" | ".join(reasons) if reasons else "El perfil no tiene items.")
                ),
                status_code=422,
                context={
                    "profile_id": profile_id,
                    "skipped_levels": skipped_levels,
                    "errors": errors,
                },
            )

        return ApplyProfileResult(
            profile_id=profile_id,
            profile_name=ctx["profile_name"],
            engine=ctx["engine"],
            grants_applied=grants_applied,
            skipped_levels=skipped_levels,
            errors=errors,
        )

    def apply_profile_bulk(
        self,
        user_id: int,
        profile_id: int,
        payload: ApplyProfileBulkRequest,
        *,
        admin: dict | None = None,
    ) -> ApplyProfileBulkResult:
        """
        Aplica el MISMO perfil al MISMO usuario sobre N bases de datos en una llamada.

        Mismo patrón que ``ManagedMigrationController.apply_all``: contexto cargado UNA
        vez (la credencial pseudo-root se descifra una sola vez, no por BD), una BD que
        falla NO aborta el lote, y una única entrada de auditoría agregada al final.

        NO se valida la existencia previa de cada BD contra el motor: si no existe, el
        propio motor rechaza el GRANT y ese error nativo cae en los ``errors`` de ESE
        ítem. Es el mismo criterio que ``apply_profile`` (que tampoco la valida) y evita
        una ronda extra de introspección por BD.
        """
        ctx = self._load_apply_profile_context(user_id, profile_id)
        adapter, grantee = ctx["adapter"], ctx["grantee"]

        results: list[ApplyProfileBulkItemOut] = []
        total_grants = 0

        for db_name in payload.databases:
            # ObjectRef NUEVO por iteración: se COPIA en vez de mutar el ref del payload.
            # Mutar el original filtraría la BD de la iteración k a la k+1 (y a la
            # respuesta, que serializa el mismo objeto). La copia es superficial —
            # columns/routine se comparten— y eso es correcto: los refs son de solo
            # lectura, nadie los muta aguas abajo.
            mapping_index: dict[GrantLevel, ObjectRef] = {
                m.level: m.object_ref.model_copy(update={"database": db_name})
                for m in payload.object_mappings
            }
            try:
                applied, skipped, errors = self._apply_profile_items(
                    ctx["items"], mapping_index, adapter, grantee, ctx["engine"]
                )
            except AppHttpException as exc:
                # Fallo que escapa al best-effort por item (p.ej. el motor caído deja de
                # responder a mitad del lote). Se reporta en ESTA BD y se sigue.
                applied, skipped, errors = 0, [], [exc.message]
            except Exception as exc:
                logger.warning(
                    "apply_profile_bulk: error inesperado en BD %s (user_id=%s, "
                    "profile_id=%s): %s",
                    db_name, user_id, profile_id, exc, exc_info=True,
                )
                # Solo el TIPO: el mensaje crudo de una excepción inesperada puede
                # arrastrar detalle interno que no corresponde devolver al cliente.
                applied, skipped, errors = 0, [], [
                    f"error inesperado: {type(exc).__name__}"
                ]

            total_grants += applied
            results.append(
                ApplyProfileBulkItemOut(
                    database=db_name,
                    grants_applied=applied,
                    skipped_levels=skipped,
                    errors=errors,
                    ok=not errors,
                )
            )

        # Auditoría AGREGADA (una fila), igual que apply_profile y apply_all. Se listan
        # las BDs para que el rastro identifique qué se tocó sin necesitar N filas;
        # acotado para que 'detail' siga siendo un resumen y no un volcado.
        failed = [r.database for r in results if not r.ok]
        audit.record(
            "server_user.apply_profile_bulk",
            admin=admin,
            target_type="server_user",
            target_id=user_id,
            server_id=ctx["server_id"],
            touched_engine=True,
            grantee=_grantee_label(grantee),
            grantor=ctx["grantor"],
            detail=(
                f"profile_id={profile_id} ({ctx['profile_name']}): "
                f"{len(payload.databases)} BD(s), {total_grants} grants aplicados, "
                f"{len(failed)} BD(s) con error"
                f" | bds={_summarize_names(payload.databases)}"
                + (f" | fallidas={_summarize_names(failed)}" if failed else "")
            ),
        )
        return ApplyProfileBulkResult(
            profile_id=profile_id,
            profile_name=ctx["profile_name"],
            engine=ctx["engine"],
            total_databases=len(payload.databases),
            results=results,
        )
