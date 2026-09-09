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

EL TOKEN ES DE UN SOLO USO POR ``credential_epoch``, NO POR UNA TABLA
---------------------------------------------------------------------
Se firma sobre ``(user_id, credential_epoch)`` y aceptar la invitación **sube el epoch**, así que
el token deja de validar en cuanto se usa. No hace falta una tabla de tokens consumidos ni un
barrido de expirados: el mecanismo es el mismo contador que ya sirve para revocar una invitación
(re-invitar sube el epoch y mata la anterior).
"""

from datetime import datetime

from app.core.authz import assert_not_last_access_admin
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.services import audit, confirm_token
from app.services.capability_catalog import GatewayRole, GlobalCapability
from app.utils.security import PASSWORD_MIN_LENGTH, hash_password

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
                "gateway_role": rol.value,
            }
        )
        if globales:
            self.users.grant_global_capabilities(username, [g.value for g in globales])

        token, expira = self._issue_invite(user_id, epoch=0)
        audit.record(
            "gateway_user.create",
            admin=admin,
            target_type="user",
            target_id=user_id,
            touched_engine=False,
            detail=(
                f"alta de '{username}' con rol {rol.value} y globales "
                f"[{','.join(g.value for g in globales) or '—'}]; invitación emitida SIN "
                "credencial (la password la fija la persona)"
            ),
        )
        creado = self._hydrate(self.users.find_by_id(user_id))
        return {**creado, "invite_token": token, "invite_expires_at": expira}

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
        fila = self._get_or_404(user_id)
        if len(password or "") < PASSWORD_MIN_LENGTH:
            raise AppHttpException(
                message=f"La contraseña tiene que tener al menos {PASSWORD_MIN_LENGTH} caracteres.",
                status_code=422,
                public_context={
                    "code": CODE_WEAK_PASSWORD,
                    "min_length": PASSWORD_MIN_LENGTH,
                },
            )

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
        cambios: dict = {}

        if "gateway_role" in data and data["gateway_role"] is not None:
            cambios["gateway_role"] = self._validate_role(data["gateway_role"]).value

        if "is_active" in data and data["is_active"] is not None:
            if not data["is_active"] and fila.get("is_active"):
                assert_not_last_access_admin(user_id, action="desactivar este usuario")
            cambios["is_active"] = bool(data["is_active"])

        for campo in ("full_name", "email", "notes"):
            if campo in data and data[campo] is not None:
                cambios[campo] = data[campo]

        if cambios:
            self.users.update(user_id, cambios)
            audit.record(
                "gateway_user.update",
                admin=admin,
                target_type="user",
                target_id=user_id,
                touched_engine=False,
                detail=f"{fila['username']}: " + ", ".join(sorted(cambios)),
            )
            # Un cambio de rol o de estado tiene que surtir efecto YA. El rol se relee por
            # request, así que eso ya pasa; las sesiones se tachan igual para que el corte
            # quede con motivo y la persona entienda por qué volvió al login.
            if "gateway_role" in cambios or cambios.get("is_active") is False:
                from app.core import session_store

                session_store.revoke_all_for_user(
                    user_id, session_store.REASON_ROLE_CHANGE
                )

        return self._hydrate(self._get_or_404(user_id))

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
        self._get_or_404(user_id)
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

        quita_access_admin = GlobalCapability.ACCESS_ADMIN not in globales
        if quita_access_admin:
            assert_not_last_access_admin(
                user_id, action="quitarle 'access_admin' a este usuario"
            )

        self.users.replace_access(
            user_id,
            grants=grants,
            globals_=[g.value for g in globales],
        )
        audit.record(
            "gateway_user.access_set",
            admin=admin,
            target_type="user",
            target_id=user_id,
            touched_engine=False,
            detail=(
                f"globales=[{','.join(g.value for g in globales) or '—'}] "
                f"alcances={len(grants)}"
            ),
        )
        from app.core import session_store

        session_store.revoke_all_for_user(user_id, session_store.REASON_ROLE_CHANGE)
        return self._hydrate(self._get_or_404(user_id))

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
                return fila["id"]
            except AppHttpException as exc:
                # 410 es "expiró", y eso no depende de CUÁL usuario sea: se propaga tal cual en
                # vez de seguir probando, para que el mensaje diga la verdad.
                if exc.status_code == 410:
                    raise
                continue
        raise AppHttpException(
            message="La invitación no es válida o ya se usó.",
            status_code=422,
            public_context={"code": CODE_NOT_FOUND},
        )
