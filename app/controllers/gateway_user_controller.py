"""
Controller de los usuarios DEL GATEWAY (plano de control).

OJO CON EL NOMBRE: estos son los usuarios que se autentican **contra el gateway**. Los usuarios
del MOTOR —los que el gateway crea en MySQL/PostgreSQL— viven en ``server_user_controller.py`` y
usan otras palabras a propósito (``ServerUser``, ``Privilege``, ``grant``). Es la misma clase de
colisión que las "dos cosas llamadas environment" que documenta ``CLAUDE.md``, con el agravante de
que PostgreSQL llama **roles** a los usuarios del motor.

LA PASSWORD INICIAL NO LA PONE QUIEN CREA LA CUENTA
---------------------------------------------------
Es la decisión que ordena todo este archivo. Si un administrador tipea la password inicial de otra
persona, **conoce una credencial funcional de esa identidad** — y por lo tanto toda fila de
``audit_log`` atribuida a ese usuario es **repudiable**. Para un sistema cuyo valor central es el
rastro, eso es fatal.

Y "cambio forzado en el primer login" **no lo arregla**: el administrador pudo haber entrado antes.

Así que la cuenta nace **sin credencial** (``hashed_password = ''``, que Argon2 nunca puede
verificar) y con un token de invitación de un solo uso. El administrador que la crea **no obtiene
ninguna ventana en la que la cuenta sea usable**.

Con ``access_admin`` en el modelo esto no es solo repudio: sería **la vía de escalada** del §4.4 —
crear una identidad `owner`, conocer su password, y operar producción con la cara de otro.

LAS ELEVACIONES PIDEN UN SEGUNDO APROBADOR (C3)
----------------------------------------------
Alta, ``PATCH`` (rol) y ``PUT /access`` parten cada cambio (``app/core/assignment_policy.split``):
lo que NO eleva —incluidas siempre las bajas— se aplica en el request y responde como siempre;
lo que eleva (``owner`` base o por alcance, cualquier global, un ``sod_override``) nace como
solicitud pendiente de OTRO ``access_admin`` (``access_request_controller``) y la respuesta es
``202 access.elevation_pending``. El alta de una cuenta que pide ``owner`` o globales la crea
como ``viewer`` sin globales, con la invitación igual. Reemplaza al techo por tenencia, que
obligaba a quien administra accesos a tener cada deber que reparte.

EL TOKEN ES DE UN SOLO USO POR ``credential_epoch``, NO POR UNA TABLA
---------------------------------------------------------------------
Se firma sobre ``(user_id, credential_epoch)`` y aceptar la invitación **sube el epoch**, así que
el token deja de validar en cuanto se usa. No hace falta una tabla de tokens consumidos ni un
barrido de expirados: el mecanismo es el mismo contador que ya sirve para revocar una invitación
(re-invitar sube el epoch y mata la anterior).
"""

import json
from datetime import datetime

from app.core.authz import assert_not_last_access_admin
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.controllers import access_request_controller as access_requests
from app.services import audit, confirm_token, sod_service
from app.core.actor import identity_of
from app.core.assignment_policy import AccessState, split
from app.core.separation_of_duties import conflicts as sod_conflicts
from app.services.capability_catalog import (
    CODE_GRANT_SCOPE_NOT_FOUND,
    CODE_SELF_MODIFICATION,
    GatewayRole,
    GlobalCapability,
)
from app.utils.security import PASSWORD_MIN_LENGTH, hash_password

logger = get_logger(__name__)

#: Operación con la que se firma la invitación. Constante compartida por emisor y verificador:
#: si divergen, el token no valida nunca y el fallo se ve recién en runtime.
INVITE_OPERATION = "gateway_user_invite"
#: 48 h. Es una invitación que alguien tiene que recibir y usar, no un confirm de dos minutos.
INVITE_TTL_SECONDS = 48 * 3600

CODE_NOT_FOUND = "gateway_user.not_found"
CODE_USERNAME_TAKEN = "gateway_user.username_taken"
CODE_ALREADY_ACTIVE = "gateway_user.credential_already_set"
CODE_INVALID_ROLE = "gateway_user.invalid_role"
CODE_INVALID_CAPABILITY = "gateway_user.invalid_global_capability"
CODE_WEAK_PASSWORD = "gateway_user.weak_password"

#: Tope de alcances que se copian a CADA lado (antes/después) del ``detail`` de auditoría. Por
#: encima se recorta y se declara el total: ``detail`` es un ``TEXT`` y una fila de auditoría no
#: puede crecer con el tamaño del acceso de una persona.
AUDIT_MAX_GRANTS = 200


def assert_password_policy(password: str) -> None:
    """
    La política de una password que elige una persona. **Una sola**, para todo camino que fije
    una: aceptar la invitación y el cambio de password propio (``AuthController``).

    Vive extraída porque dos copias del chequeo son dos políticas el día que alguien suba el
    mínimo en una sola, y el código ``gateway_user.weak_password`` que lee la SPA quedaría
    significando cosas distintas según la pantalla.
    """
    if len(password or "") < PASSWORD_MIN_LENGTH:
        raise AppHttpException(
            message=f"La contraseña tiene que tener al menos {PASSWORD_MIN_LENGTH} caracteres.",
            status_code=422,
            public_context={
                "code": CODE_WEAK_PASSWORD,
                "min_length": PASSWORD_MIN_LENGTH,
            },
        )


class GatewayUserController:
    def __init__(self):
        self.users = UserModel()

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize(fila: dict, *, grants: list, globals_: list) -> dict:
        """
        **Nunca incluye ``hashed_password``.** El estado de la credencial se publica como el
        booleano ``credential_set``: quien administra accesos necesita saber si la invitación
        se aceptó, y no necesita —ni puede— ver nada del hash.
        """
        return {
            "id": fila["id"],
            "username": fila["username"],
            "email": fila["email"],
            "full_name": fila.get("full_name"),
            "gateway_role": fila.get("gateway_role") or GatewayRole.VIEWER.value,
            "is_active": bool(fila.get("is_active")),
            "credential_set": bool(fila.get("hashed_password")),
            "global_capabilities": sorted(globals_),
            "scope_grants": [
                {"scope_type": t, "scope_id": i, "role": r} for (t, i, r) in grants
            ],
            "last_login_at": fila.get("last_login_at"),
            "previous_login_at": fila.get("previous_login_at"),
            "last_failed_at": fila.get("last_failed_at"),
            "created_at": fila.get("created_at"),
        }

    def _hydrate(self, fila: dict) -> dict:
        ctx = self.users.find_access_context(fila["id"])
        return self._serialize(fila, grants=ctx["grants"], globals_=ctx["globals"])

    def _get_or_404(self, user_id: int) -> dict:
        fila = self.users.find_by_id(user_id)
        if not fila:
            raise AppHttpException(
                message="Usuario del gateway no encontrado.",
                status_code=404,
                public_context={"code": CODE_NOT_FOUND},
                context={"user_id": user_id},
            )
        return fila

    def _access_snapshot(self, fila: dict, ctx: dict | None = None) -> dict:
        """
        El estado de AUTORIZACIÓN de una cuenta, tal como se audita antes y después de cambiarlo.

        Existe porque el rastro anterior no permitía reconstruir nada: ``gateway_user.update``
        guardaba solo los NOMBRES de los campos y ``gateway_user.access_set`` las globales
        DESPUÉS y la CANTIDAD de alcances. Y ``replace_access`` borra y re-inserta los grants,
        así que ``access_grants`` tampoco guarda historia: la única respuesta a "¿quién le dio
        ``owner`` en producción, y qué tenía antes?" es esta foto en ``audit_log``.

        Forma (la misma de los dos lados, y la misma idea ``{before, after}`` que las capacidades
        puntuales): rol base, estado, globales y la lista COMPLETA de ``(scope_type, scope_id,
        role)`` ordenada, recortada a ``AUDIT_MAX_GRANTS`` con el total declarado.
        """
        if ctx is None:
            ctx = self.users.find_access_context(fila["id"])
        grants = sorted(
            (str(t), int(i), str(r)) for (t, i, r) in (ctx.get("grants") or [])
        )
        snap = {
            "gateway_role": fila.get("gateway_role") or GatewayRole.VIEWER.value,
            "is_active": bool(fila.get("is_active")),
            "global_capabilities": sorted(ctx.get("globals") or []),
            "scope_grants": [
                {"scope_type": t, "scope_id": i, "role": r}
                for (t, i, r) in grants[:AUDIT_MAX_GRANTS]
            ],
            "scope_grants_total": len(grants),
        }
        if len(grants) > AUDIT_MAX_GRANTS:
            snap["scope_grants_truncated"] = True
        return snap

    @staticmethod
    def _access_detail(username: str, before: dict, after: dict, **extra) -> str:
        return json.dumps(
            {"username": username, **extra, "before": before, "after": after},
            ensure_ascii=False,
        )

    def list_users(self, *, limit: int, offset: int) -> tuple[list[dict], int]:
        filas = self.users.find_all(limit=limit, offset=offset)
        return [self._hydrate(f) for f in filas], self.users.count()

    def get_user(self, user_id: int) -> dict:
        return self._hydrate(self._get_or_404(user_id))

    # ------------------------------------------------------------------ #
    # Alta                                                               #
    # ------------------------------------------------------------------ #
    def create_user(self, data: dict, *, admin) -> dict:
        """
        Crea la cuenta **sin credencial** y devuelve el token de invitación.

        El token se devuelve en la respuesta a propósito y **no se manda a ningún lado**: el
        gateway no tiene sustrato de notificación (ni SMTP, ni webhook, ni cola), y fingir que
        lo tiene sería peor que no tenerlo. Quien crea la cuenta se lo entrega a la persona por
        el canal que corresponda.

        Y eso **no reintroduce el problema que este diseño evita**: el token no es una
        credencial de la cuenta, es la autorización para *fijar* una. Quien lo tiene no puede
        entrar; puede fijar una password que la persona después usa —y si lo hiciera, la
        persona lo nota en el momento en que su invitación ya no funciona—.
        """
        username = (data.get("username") or "").strip()
        if self.users.find_by_username(username):
            raise AppHttpException(
                message=f"El username '{username}' ya está en uso.",
                status_code=409,
                public_context={"code": CODE_USERNAME_TAKEN},
            )

        # `if ... is None` y no `or`: con `or`, un `gateway_role=""` explícito se coercionaba a
        # `viewer` en silencio. Un valor inválido que alguien MANDÓ tiene que fallar, no
        # resolverse a un default — es la diferencia entre omitir el campo y equivocarse.
        crudo = data.get("gateway_role")
        rol = self._validate_role(
            GatewayRole.VIEWER.value if crudo is None else crudo
        )
        globales = self._validate_globals(data.get("global_capabilities") or [])
        vacio = AccessState.of(GatewayRole.VIEWER.value, [], [])
        deseado = AccessState.of(rol.value, [g.value for g in globales], [])
        self._assert_assignable(admin, vacio, deseado)
        # Separación de deberes sobre el estado RESULTANTE. Una cuenta nueva no tiene
        # excepciones: o la combinación es válida, o viene con `sod_override`.
        override = data.get("sod_override")
        plan = sod_service.check(
            sod_conflicts(base_role=rol, scope_roles=[], globals_=globales),
            covered=(),
            override=override,
        )
        inmediato, elevaciones = split(vacio, deseado)
        modo = access_requests.decide(elevaciones, plan, admin)
        pendiente = modo == access_requests.MODE_WAIT
        # Con elevación pendiente la cuenta nace con la parte que NO eleva: `viewer` (u
        # `operator`) y sin globales. El override viaja con la solicitud.
        objetivo = inmediato if pendiente else deseado
        if plan and not pendiente:
            sod_service.record_override_intent(plan, admin=admin, target_id=None, username=username)

        user_id = self.users.create(
            {
                "username": username,
                "email": (data.get("email") or f"{username}@gateway.local").strip(),
                # Sin credencial: Argon2 nunca puede verificar la cadena vacía. Es el estado
                # `pending_invite` y no hace falta una columna para representarlo.
                "hashed_password": "",
                "full_name": data.get("full_name"),
                "notes": data.get("notes"),
                "is_active": True,
                "gateway_role": objetivo.base_role,
            }
        )
        if objetivo.globals_:
            self.users.grant_global_capabilities(username, sorted(objetivo.globals_))

        if plan and not pendiente:
            sod_service.apply_override(plan, user_id=user_id, admin=admin, username=username)

        token, expira = self._issue_invite(user_id, epoch=0)
        audit.record(
            "gateway_user.create",
            admin=admin,
            target_type="user",
            target_id=user_id,
            touched_engine=False,
            detail=(
                f"alta de '{username}' con rol {objetivo.base_role} y globales "
                f"[{','.join(sorted(objetivo.globals_)) or '—'}]; invitación emitida SIN "
                "credencial (la password la fija la persona)"
                + ("; la elevación pedida quedó PENDIENTE de un segundo aprobador"
                   if pendiente else "")
            ),
        )
        if modo and not pendiente:
            access_requests.record_unapproved(
                admin=admin, target_id=user_id, username=username,
                elevations=elevaciones, origin="create", override=override, mode=modo,
            )
        creado = {
            **self._hydrate(self.users.find_by_id(user_id)),
            "invite_token": token,
            "invite_expires_at": expira,
        }
        if pendiente:
            solicitud = access_requests.AccessRequestController().create(
                target_id=user_id, admin=admin, desired=deseado, elevations=elevaciones,
                override=override, origin="create",
            )
            return access_requests.AccessRequestController.pending_response(creado, solicitud)
        return creado

    def reinvite(self, user_id: int, *, admin) -> dict:
        """
        Emite una invitación nueva y **mata la anterior** subiendo el ``credential_epoch``.

        Es también la vía para revocar una invitación que se filtró: no hace falta un endpoint
        de revocación aparte, porque emitir invalida.
        """
        fila = self._get_or_404(user_id)
        if fila.get("hashed_password"):
            raise AppHttpException(
                message=(
                    "Esta cuenta ya tiene credencial: la invitación es solo para la primera. "
                    "Para reemplazar la password, la persona la cambia desde su sesión."
                ),
                status_code=409,
                public_context={"code": CODE_ALREADY_ACTIVE},
            )
        nuevo_epoch = self.users.bump_credential_epoch(user_id)
        token, expira = self._issue_invite(user_id, epoch=nuevo_epoch)
        audit.record(
            "gateway_user.reinvite",
            admin=admin,
            target_type="user",
            target_id=user_id,
            touched_engine=False,
            detail=f"invitación reemitida (epoch {nuevo_epoch}); la anterior quedó inválida",
        )
        return {"invite_token": token, "invite_expires_at": expira}

    def accept_invite(self, token: str, password: str) -> dict:
        """
        Fija la primera password. **Es el único endpoint público que escribe.**

        Se autoriza con el token y nada más, porque quien la usa **todavía no puede
        autenticarse** — eso es justamente el punto del diseño. Por eso el token es HMAC sobre
        ``(user_id, credential_epoch)``: sin el epoch sería reutilizable durante 48 h.

        El ``user_id`` viaja DENTRO del token firmado y no como parámetro aparte: si viniera
        aparte, habría que verificar que coincide, y ese es el chequeo que alguien olvida.
        """
        # No hay rama de "ya se usó" acá, y es por construcción: `_verify_invite` solo mira
        # usuarios SIN credencial, así que una invitación ya aceptada no es candidata y cae en
        # el mismo 422 genérico que un token inválido. Eso es lo correcto para un endpoint
        # público — distinguir los dos casos lo convertiría en un oráculo de qué invitaciones
        # hay pendientes.
        user_id = self._verify_invite(token)
        # No `_get_or_404`: si la fila desaparece entre la verificación y acá (un admin la
        # borró), un 404 sería un motivo distinguible más. Sale el mismo 422 que todo lo demás.
        fila = self.users.find_by_id(user_id)
        if not fila:
            raise self._invite_invalid()
        assert_password_policy(password)

        self.users.set_credential(user_id, hash_password(password))
        # `record` y no `record_intent`: la password YA se fijó, así que un fallo al auditar no
        # puede deshacerlo y abortar acá dejaría a la persona sin saber si entró o no.
        audit.record(
            "gateway_user.credential_set",
            admin={"id": user_id, "username": fila["username"]},
            target_type="user",
            target_id=user_id,
            touched_engine=False,
            detail="la persona fijó su primera contraseña desde la invitación",
        )
        # Si era un segundo `access_admin`, la ventana de arranque se cierra ACÁ y no en la
        # próxima decisión: así `closed_at` dice cuándo pasó de verdad. Best-effort: el cierre
        # perezoso de cada decisión es el respaldo.
        try:
            from app.services import bootstrap_window

            bootstrap_window.refresh()
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo reevaluar la ventana de arranque tras la invitación.")
        return {"username": fila["username"]}

    # ------------------------------------------------------------------ #
    # Modificación                                                       #
    # ------------------------------------------------------------------ #
    def update_user(self, user_id: int, data: dict, *, admin) -> dict:
        """
        Cambia rol, estado y datos de contacto. **El username no se edita nunca**: es la
        identidad que se audita, y `audit_log` la desnormaliza sin FK, así que renombrar
        reescribiría el significado de las filas viejas.
        """
        fila = self._get_or_404(user_id)
        # La foto ANTES de validar nada: es lo que el rastro compara contra el después.
        antes = self._access_snapshot(fila)
        cambios: dict = {}
        plan = None
        cambia_rol = False
        # Elevación aplicada sin segundo aprobador (ACCESS_FOUR_EYES=False o ventana de arranque):
        # (elevaciones, override, modo) para auditarla con la salida que se decidió.
        sin_aprobar: tuple | None = None

        elevacion: tuple | None = None  # (deseado, elevaciones, override) si queda pendiente
        if "gateway_role" in data and data["gateway_role"] is not None:
            nuevo = self._validate_role(data["gateway_role"])
            actual = fila.get("gateway_role") or GatewayRole.VIEWER.value
            if nuevo.value != actual:
                self._guard_not_self(admin, user_id, action="cambiar tu propio rol")
                estado = access_requests.current_state(user_id, fila)
                deseado = AccessState(
                    base_role=nuevo.value, globals_=estado.globals_, grants=estado.grants
                )
                self._assert_assignable(admin, estado, deseado)
                plan = self._assert_sod(
                    user_id, data.get("sod_override"), base_role=nuevo.value
                )
                _, elevaciones = split(estado, deseado)
                modo = access_requests.decide(elevaciones, plan, admin)
                if modo == access_requests.MODE_WAIT:
                    # El rol queda pendiente; el resto del PATCH (contacto, estado) se aplica.
                    elevacion = (deseado, elevaciones, data.get("sod_override"))
                    plan = None
                else:
                    cambia_rol = True
                    if modo:
                        sin_aprobar = (elevaciones, data.get("sod_override"), modo)
            if elevacion is None:
                cambios["gateway_role"] = nuevo.value

        if "is_active" in data and data["is_active"] is not None:
            if not data["is_active"] and fila.get("is_active"):
                self._guard_not_self(admin, user_id, action="desactivar tu propia cuenta")
                assert_not_last_access_admin(user_id, action="desactivar este usuario")
            cambios["is_active"] = bool(data["is_active"])

        for campo in ("full_name", "email", "notes"):
            if campo in data and data[campo] is not None:
                cambios[campo] = data[campo]

        if cambios:
            if plan:
                sod_service.record_override_intent(
                    plan, admin=admin, target_id=user_id, username=fila["username"]
                )
            if cambios.get("is_active") is False and fila.get("is_active"):
                # El pre-chequeo de arriba falla temprano; éste es el candado: cuenta y escribe
                # en la MISMA transacción con las filas bloqueadas (ver
                # ``UserModel._guard_last_access_admin_locked``).
                self.users.deactivate_guarded(
                    user_id, cambios, last_admin_action="desactivar este usuario"
                )
            else:
                self.users.update(user_id, cambios)
            if plan:
                sod_service.apply_override(
                    plan, user_id=user_id, admin=admin, username=fila["username"]
                )
            elif cambia_rol:
                sod_service.reconcile(user_id)
            audit.record(
                "gateway_user.update",
                admin=admin,
                target_type="user",
                target_id=user_id,
                touched_engine=False,
                # Los VALORES de contacto (email, notas) no se copian: son datos personales y no
                # autorización. Basta con nombrarlos en `changed`.
                detail=self._access_detail(
                    fila["username"],
                    antes,
                    self._access_snapshot(self._get_or_404(user_id)),
                    changed=sorted(cambios),
                ),
            )
            # Un cambio de rol o de estado tiene que surtir efecto YA. El rol se relee por
            # request, así que eso ya pasa; las sesiones se tachan igual para que el corte
            # quede con motivo y la persona entienda por qué volvió al login.
            if "gateway_role" in cambios or cambios.get("is_active") is False:
                from app.core import session_store

                session_store.revoke_all_for_user(
                    user_id, session_store.REASON_ROLE_CHANGE
                )
            if cambios.get("is_active") is False:
                self._cancel_pending_requests(user_id)
            if sin_aprobar is not None:
                access_requests.record_unapproved(
                    admin=admin, target_id=user_id, username=fila["username"],
                    elevations=sin_aprobar[0], origin="update", override=sin_aprobar[1],
                    mode=sin_aprobar[2],
                )

        actualizado = self._hydrate(self._get_or_404(user_id))
        if elevacion is not None:
            deseado, elevaciones, override = elevacion
            solicitud = access_requests.AccessRequestController().create(
                target_id=user_id, admin=admin, desired=deseado, elevations=elevaciones,
                override=override, origin="update",
            )
            return access_requests.AccessRequestController.pending_response(
                actualizado, solicitud
            )
        return actualizado

    @staticmethod
    def _cancel_pending_requests(user_id: int) -> None:
        """
        Cancela las capacidades puntuales y las elevaciones PENDIENTES que pidió quien pierde
        ``access_admin`` o queda inactivo (D7). Best-effort: no puede tumbar el cambio de acceso
        ya aplicado, y las dos aprobaciones re-verifican al solicitante como respaldo.
        """
        try:
            from app.controllers.capability_grant_controller import CapabilityGrantController

            CapabilityGrantController().cancel_pending_requested_by(user_id)
        except Exception:
            logger.exception("No se pudieron cancelar las solicitudes pendientes de %s", user_id)
        try:
            access_requests.AccessRequestController().cancel_pending_requested_by(user_id)
        except Exception:
            logger.exception("No se pudieron cancelar las elevaciones pendientes de %s", user_id)

    def set_access(self, user_id: int, data: dict, *, admin) -> dict:
        """
        Reemplaza los alcances y las globales del usuario, en una sola llamada.

        Es un PUT y no un POST por grant a propósito: la pregunta que una pantalla de accesos
        responde es *"qué acceso tiene esta persona"*, y con endpoints por grant el estado final
        depende del orden de N llamadas — y una a mitad de camino deja un acceso que nadie pidió.

        El guard del último administrador se evalúa contra el estado RESULTANTE, no contra el
        payload: quitarse `access_admin` a sí mismo y agregárselo a alguien inactivo es dos
        cambios que por separado parecen inofensivos.
        """
        fila = self._get_or_404(user_id)
        self._guard_not_self(admin, user_id, action="cambiar tu propio acceso")
        globales = self._validate_globals(data.get("global_capabilities") or [])
        grants = []
        for g in data.get("scope_grants") or []:
            tipo = g.get("scope_type")
            if tipo not in ("environment", "server"):
                raise AppHttpException(
                    message=f"Alcance inválido: {tipo!r}. Solo 'environment' o 'server'.",
                    status_code=422,
                    public_context={"code": CODE_INVALID_CAPABILITY},
                )
            grants.append((tipo, int(g["scope_id"]), self._validate_role(g["role"]).value))
        self._assert_scopes_exist(grants)

        actual = self.users.find_access_context(user_id)
        antes = self._access_snapshot(fila, actual)
        estado = AccessState.of(
            fila.get("gateway_role") or actual.get("role"),
            actual.get("globals") or [],
            actual.get("grants") or [],
        )
        deseado = AccessState.of(estado.base_role, [g.value for g in globales], grants)
        self._assert_assignable(admin, estado, deseado)

        override = data.get("sod_override")
        plan = self._assert_sod(
            user_id,
            override,
            base_role=actual.get("role"),
            scope_roles=grants,
            globals_=[g.value for g in globales],
        )
        inmediato, elevaciones = split(estado, deseado)
        modo = access_requests.decide(elevaciones, plan, admin)
        pendiente = modo == access_requests.MODE_WAIT
        if pendiente:
            # La parte que NO eleva (bajas incluidas) se aplica ya; la que eleva, y el override,
            # esperan a otro access_admin. Si no hay parte inmediata, no se escribe nada.
            plan = None
            objetivo = inmediato
        else:
            objetivo = deseado

        if not pendiente or objetivo != estado:
            self._write_access(
                user_id, fila, objetivo, plan=plan, admin=admin, antes=antes
            )
        if modo and not pendiente:
            access_requests.record_unapproved(
                admin=admin, target_id=user_id, username=fila["username"],
                elevations=elevaciones, origin="set_access", override=override if plan else None,
                mode=modo,
            )
        actualizado = self._hydrate(self._get_or_404(user_id))
        if pendiente:
            solicitud = access_requests.AccessRequestController().create(
                target_id=user_id, admin=admin, desired=deseado, elevations=elevaciones,
                override=override, origin="set_access",
            )
            return access_requests.AccessRequestController.pending_response(
                actualizado, solicitud
            )
        return actualizado

    def _write_access(
        self, user_id: int, fila: dict, objetivo: AccessState, *, plan, admin, antes: dict
    ) -> None:
        """
        Escribe globales y alcances de ``objetivo`` (el rol base no: ``PUT /access`` no lo toca),
        con el candado del último administrador, el override si viene, la auditoría antes/después
        y el corte de sesiones. Es el ``PUT /access`` de siempre, sobre el estado que corresponde.
        """
        quita_access_admin = GlobalCapability.ACCESS_ADMIN.value not in objetivo.globals_
        accion_last_admin = "quitarle 'access_admin' a este usuario"
        if quita_access_admin:
            # Pre-chequeo para fallar temprano; el candado está en `replace_access`.
            assert_not_last_access_admin(user_id, action=accion_last_admin)

        if plan:
            sod_service.record_override_intent(
                plan, admin=admin, target_id=user_id, username=fila["username"]
            )
        self.users.replace_access(
            user_id,
            grants=sorted(objetivo.grants),
            globals_=sorted(objetivo.globals_),
            last_admin_action=accion_last_admin if quita_access_admin else None,
        )
        if plan:
            sod_service.apply_override(
                plan, user_id=user_id, admin=admin, username=fila["username"]
            )
        else:
            sod_service.reconcile(user_id)
        audit.record(
            "gateway_user.access_set",
            admin=admin,
            target_type="user",
            target_id=user_id,
            touched_engine=False,
            detail=self._access_detail(
                fila["username"],
                antes,
                self._access_snapshot(self._get_or_404(user_id)),
            ),
        )
        from app.core import session_store

        session_store.revoke_all_for_user(user_id, session_store.REASON_ROLE_CHANGE)
        if quita_access_admin:
            self._cancel_pending_requests(user_id)

    # ------------------------------------------------------------------ #
    # Guards anti auto-escalada                                          #
    # ------------------------------------------------------------------ #
    def _assert_sod(
        self,
        user_id: int,
        override,
        *,
        base_role: str | None = None,
        scope_roles: "list[tuple[str, int, str]] | None" = None,
        globals_: "list[str] | None" = None,
    ) -> "sod_service.OverridePlan | None":
        """
        Separación de deberes sobre el estado RESULTANTE de ``user_id``: lo que el payload cambia
        (``base_role`` / ``scope_roles`` / ``globals_``; ``None`` = queda como está) sobre lo que
        ya tiene, más sus capacidades puntuales VIVAS.

        409 ``access.sod_conflict`` si viola una regla que ninguna excepción viva cubre y no hay
        ``sod_override``. Una cuenta HEREDADA (el admin sembrado) sigue editable mientras el
        cambio no agregue una regla nueva: su excepción la cubre. Ver ``sod_service.check``.
        """
        actual = self.users.find_access_context(user_id)
        found = sod_conflicts(
            base_role=actual.get("role") if base_role is None else base_role,
            scope_roles=(actual.get("grants") or []) if scope_roles is None else scope_roles,
            globals_=(actual.get("globals") or []) if globals_ is None else globals_,
            capabilities=sod_service.live_capability_keys(user_id),
        )
        return sod_service.check(
            found, covered=sod_service.covered_rules(user_id), override=override
        )

    @staticmethod
    def _assert_scopes_exist(grants: list[tuple[str, int, str]]) -> None:
        """
        422 ``access.grant_scope_not_found`` si algún alcance apunta a un entorno o servidor que
        NO existe. Mismo chequeo (``CapabilityGrantModel.scope_names``) que las capacidades
        puntuales.

        ``scope_id`` no tiene FK (es polimórfico), así que sin esto se podía otorgar un rol sobre
        un id futuro, que regiría sobre el próximo objeto creado con ese id. 422 y no el 404 de
        ``capability_grants``: acá el alcance es un campo del payload, no el recurso de la URL.
        """
        if not grants:
            return
        from app.models.capability_grant_model import CapabilityGrantModel

        claves = sorted({(t, i) for (t, i, _) in grants})
        existentes = CapabilityGrantModel().scope_names(claves)
        faltantes = [k for k in claves if k not in existentes]
        if faltantes:
            raise AppHttpException(
                message="Algún entorno o servidor de los alcances indicados no existe.",
                status_code=422,
                public_context={
                    "code": CODE_GRANT_SCOPE_NOT_FOUND,
                    "missing_scopes": [
                        {"scope_type": t, "scope_id": i} for (t, i) in faltantes
                    ],
                },
            )

    @staticmethod
    def _guard_not_self(admin, user_id: int, *, action: str) -> None:
        """
        Nadie cambia su PROPIO rol, su propio acceso ni se desactiva a sí mismo.

        POR QUÉ. ``access.admin`` (la global ``access_admin``) no es operativo, pero
        administraba a cualquier usuario sin excepción, incluido quien hacía la request: ``PATCH /gateway-users/{yo} {gateway_role: owner}`` o
        ``PUT /gateway-users/{yo}/access {global_capabilities: [security_officer]}`` convertían
        al administrador de accesos en operador de producción en un request, que es justo la
        separación de deberes que ``access_admin`` existe para sostener (docs/plans/13 §4.4:
        "self-grant 409 sin override"). Desactivarse también cae acá: el corte de la propia
        cuenta lo hace otra persona, igual que el resto de su acceso.

        Solo, este guard se esquiva con un títere (crear otra cuenta privilegiada y aceptar su
        invitación); por eso toda elevación pide además un SEGUNDO APROBADOR (C3).
        """
        actor_id, _ = identity_of(admin)
        if actor_id is not None and int(actor_id) == int(user_id):
            raise AppHttpException(
                message=(
                    f"No puedes {action}: esa operación la tiene que hacer otra persona con "
                    "permiso de administración de accesos."
                ),
                status_code=409,
                public_context={"code": CODE_SELF_MODIFICATION},
            )

    @staticmethod
    def _assert_assignable(admin, actual: AccessState, deseado: AccessState) -> None:
        """
        Política de ASIGNACIÓN (``ASSIGNABLE_BY``): lo que el cambio AGREGA —rol base nuevo,
        alcances nuevos o con otro rol, globales nuevas— tiene que poder asignarlo la función del
        actor. Hoy ``access_admin`` asigna todo; quitar no se mide (bajar nunca escala).

        Reemplaza al techo por TENENCIA (``_assert_within_ceiling``, "nunca más de lo que tienes"),
        que exigía que quien crea un ``owner`` fuera ``owner`` y quien crea un
        ``security_officer`` lo fuera: la combinación permanente que la separación de deberes
        prohíbe. Lo que ese techo impedía —el títere privilegiado de F-2— lo impide ahora el
        segundo aprobador (``needs_second_approver``), no este chequeo.

        Fail-closed: un actor que no es ``Actor`` no asigna nada.
        """
        agregado: list[dict] = []
        if deseado.base_role != actual.base_role:
            agregado.append({"kind": "base_role", "role": deseado.base_role})
        for g in sorted(deseado.globals_ - actual.globals_):
            agregado.append({"kind": "global_capability", "global_capability": g})
        for t, i, r in sorted(deseado.grants - actual.grants):
            agregado.append({"kind": "scope_grant", "scope_type": t, "scope_id": i, "role": r})
        if not access_requests.actor_can_assign(admin, agregado):
            raise access_requests.not_assignable_error()

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_role(raw: str) -> GatewayRole:
        try:
            return GatewayRole(raw)
        except ValueError as exc:
            raise AppHttpException(
                message=f"Rol inválido: {raw!r}.",
                status_code=422,
                public_context={
                    "code": CODE_INVALID_ROLE,
                    "allowed": [r.value for r in GatewayRole],
                },
            ) from exc

    @staticmethod
    def _validate_globals(raw: list[str]) -> list[GlobalCapability]:
        out = []
        for r in raw:
            try:
                out.append(GlobalCapability(r))
            except ValueError as exc:
                raise AppHttpException(
                    message=f"Capacidad global inválida: {r!r}.",
                    status_code=422,
                    public_context={
                        "code": CODE_INVALID_CAPABILITY,
                        "allowed": [g.value for g in GlobalCapability],
                    },
                ) from exc
        return out

    @staticmethod
    def _issue_invite(user_id: int, *, epoch: int) -> tuple[str, datetime]:
        """
        Firma ``(user_id, credential_epoch)`` con la máquina de ``confirm_token``.

        Se reusa esa máquina en vez de escribir un segundo esquema de tokens firmados: es el
        mismo HMAC con TTL que el repo ya usa para las confirmaciones, y un segundo esquema
        sería un segundo lugar donde equivocarse con la comparación de tiempo constante.

        Los parámetros se mapean así: ``server_id=0`` (no hay servidor), ``db_name`` = el id del
        usuario, ``subject`` = el epoch. Es un abuso de los nombres y se declara acá para que
        nadie lo lea como si hubiera un servidor 0 involucrado.
        """
        return confirm_token.issue(
            INVITE_OPERATION,
            0,
            str(user_id),
            ttl_seconds=INVITE_TTL_SECONDS,
            subject=str(epoch),
        )

    def _verify_invite(self, token: str) -> int:
        """
        Valida la invitación y devuelve el ``user_id`` que viene DENTRO del token.

        Recorre los usuarios sin credencial y prueba el token contra cada uno con su epoch
        actual. Suena caro y no lo es: son las invitaciones pendientes, que en este sistema son
        unidades. La alternativa —mandar el ``user_id`` como parámetro— obliga a verificar que
        coincida con el del token, y ese es exactamente el chequeo que alguien olvida.

        **Todo fallo es el mismo 422** (``_invite_invalid``): firma inválida, vencida, ya usada o
        usuario inexistente. Antes un 410 "expiró" se propagaba en el primer candidato, y como
        ``verify`` miraba la expiración antes que la firma, ``"1.x"`` respondía 410 si y solo si
        existía al menos una cuenta pendiente — un oráculo público sobre justo las cuentas que se
        toman con el token. Distinguir "vencida" le sirve a la persona invitada, pero el mensaje
        único ya le dice qué hacer (pedir otra), y eso no vale un oráculo.
        """
        candidatos = self.users.find_without_credential()
        for fila in candidatos:
            try:
                confirm_token.verify(
                    token,
                    INVITE_OPERATION,
                    0,
                    str(fila["id"]),
                    subject=str(fila.get("credential_epoch") or 0),
                )
            except AppHttpException:
                # Sin ramas por status: un 410 de un candidato es tan "no válida" como un 422.
                continue
            return fila["id"]
        raise self._invite_invalid()

    @staticmethod
    def _invite_invalid() -> AppHttpException:
        """
        EL rechazo de la invitación pública: un solo status, un solo código, un solo texto.

        El código es ``gateway_user.not_found`` por compatibilidad: la SPA ya lo enruta en la
        pantalla de invitación. Cualquier rama nueva de fallo de ``accept_invite`` tiene que
        salir por acá; un mensaje o status distinto por motivo reabre el oráculo.
        """
        return AppHttpException(
            message="La invitación no es válida, venció o ya se usó.",
            status_code=422,
            public_context={"code": CODE_NOT_FOUND},
        )
