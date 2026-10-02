"""
Controller de autenticación: verifica credenciales contra la tabla ``users``.

EL ORÁCULO DE TIMING, Y POR QUÉ NO ALCANZA UN MENSAJE GENÉRICO
--------------------------------------------------------------
El mensaje de error ya era genérico ("Credenciales inválidas") y aun así el endpoint **delataba
qué usuarios existen**: la condición cortocircuitaba con ``or``, así que para un usuario
inexistente ``verify_password`` no se llamaba nunca. Inexistente ≈1 ms; existente, el costo de
Argon2id (decenas a cientos de ms). La diferencia es medible con ``curl`` desde afuera.

Enumerar usuarios no es un hallazgo menor acá: el gateway administra la producción de terceros con
credenciales pseudo-root, así que saber **qué cuentas existen** es el paso previo a elegir a quién
atacar, y la lista de nombres es exactamente lo que un atacante no tiene.

El arreglo tiene dos mitades y hacen falta las dos: **siempre** ejecutar ``verify_password``
—contra el hash real o contra ``_DUMMY_HASH``— y **evaluar el booleano al final**, sin
cortocircuito. Cubre también la rama ``is_active``, que delataba una cuenta deshabilitada por la
misma vía.

Lo que esto NO iguala, declarado: la consulta a ``users`` es una lectura por índice único y su
costo no depende de si la fila existe, pero **el tiempo total no queda constante** — solo deja de
estar dominado por la diferencia de dos órdenes de magnitud del hash. Igualar el resto exigiría un
presupuesto de tiempo fijo por request, que trae su propio modo de fallo (una carga alta lo
convierte en un límite de tasa involuntario).
"""

from hmac import compare_digest
from secrets import token_urlsafe

from app.controllers.gateway_user_controller import assert_password_policy
from app.core import session_store
from app.core.actor import Actor
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.services import audit
from app.utils.security import hash_password, verify_password

#: Hash contra el que se verifica cuando el usuario NO existe o está inactivo, para pagar el
#: mismo costo de Argon2id que el camino real.
#:
#: Se COMPUTA al importar sobre un token aleatorio, en vez de ser un literal:
#: (a) un hash literal en el fuente lo levanta el gate de `detect-secrets` y obliga a una
#: excepción en el baseline, que es ruido permanente por un valor que no es un secreto; y
#: (b) aleatorio por proceso, no puede coincidir con la password de ninguna fila ni por
#: accidente ni por un `hashed_password` copiado de acá.
#:
#: Cuesta un hash en el arranque (decenas de ms, una vez). Diferirlo al primer login movería ese
#: costo al primer intento, que es justo el request donde la diferencia se mide.
_DUMMY_HASH = hash_password(token_urlsafe(32))

#: Códigos del cambio de password propio. ``gateway_user.weak_password`` NO está acá: es el de
#: la política compartida con la invitación (``assert_password_policy``).
CODE_INVALID_CURRENT_PASSWORD = "auth.invalid_current_password"
CODE_PASSWORD_UNCHANGED = "auth.password_unchanged"
CODE_SESSION_REQUIRED = "auth.session_required"
#: Step-up con contraseña incorrecta. 400 y NO 401: la sesión sigue siendo válida, y un 401
#: dispara el logout global de la SPA por un error de tipeo.
CODE_STEP_UP_FAILED = "auth.step_up_failed"


class AuthController:
    def __init__(self):
        self.user_model = UserModel()

    def authenticate(self, username: str, password: str) -> dict:
        """
        Verifica usuario+password. Devuelve ``{id, username}`` o lanza 401 genérico.

        Sin cortocircuito: ver el docstring del módulo. Y audita el fallo —``auth.login_failed``
        no existía— con el username INTENTADO y ``admin_id`` nulo, porque no hay identidad
        probada. **Nunca la password**, ni siquiera su longitud.
        """
        user = self.user_model.find_by_username(username)

        existe = user is not None
        activo = bool(user and user.get("is_active"))
        # SIEMPRE se paga el hash, exista o no la fila. Y el `or _DUMMY_HASH` cubre el estado
        # SIN CREDENCIAL de una invitación pendiente: con `hashed_password=''`, Argon2 levanta
        # `InvalidHashError` de inmediato y el 401 volvería en ~1 ms — o sea el mismo oráculo de
        # timing que este método existe para cerrar, reintroducido por una cuenta nueva.
        hash_a_verificar = (user["hashed_password"] or _DUMMY_HASH) if existe else _DUMMY_HASH
        password_ok = verify_password(password, hash_a_verificar)

        if not (existe and activo and password_ok):
            self._record_failure(user, username)
            raise AppHttpException(message="Credenciales inválidas.", status_code=401)

        self.user_model.mark_login_success(user["id"])
        audit.record(
            "auth.login",
            admin={"id": user["id"], "username": user["username"]},
            target_type="user",
            target_id=user["id"],
            touched_engine=False,
        )
        return {"id": user["id"], "username": user["username"]}

    def _record_failure(self, user: dict | None, username: str) -> None:
        """
        Deja rastro del intento fallido. **Best-effort a propósito.**

        Un fallo al auditar no puede convertirse en un fallo al *autenticar*: sería un modo de
        fallo donde una BD de metadatos con problemas deja a todo el mundo afuera del gateway
        justo cuando hay una incidencia. Lo fail-closed (``record_intent``) está reservado a
        operaciones que aflojan política, no a registrar que alguien tipeó mal.

        El ``detail`` distingue las dos causas —cuenta inexistente vs. password incorrecta—
        porque el operador que lee el registro necesita saber si lo que ve es un ataque de
        enumeración o alguien peleándose con su propia password. Esa distinción **no viaja a la
        respuesta**: ahí el 401 es idéntico en texto y ahora también en tiempo.
        """
        if user is not None:
            self.user_model.mark_login_failure(user["id"])
        audit.record(
            "auth.login_failed",
            status="failure",
            admin={"id": None, "username": username[:150]},
            target_type="user",
            target_id=user["id"] if user else None,
            touched_engine=False,
            detail=(
                "password incorrecta"
                if user is not None and user.get("is_active")
                else ("cuenta inactiva" if user is not None else "usuario inexistente")
            ),
        )

    def change_password(
        self, actor: Actor, *, current_password: str, new_password: str, current_sid: str
    ) -> int:
        """
        Cambia la password del PROPIO actor y cierra TODAS sus sesiones, incluida la actual.
        Devuelve cuántas sesiones OTRAS que la actual se cerraron.

        La actual se cierra también —y la ruta abre una nueva acto seguido— porque es la
        rotación de ``sid`` que el login ya hace: quien cambia la password suele estar
        reaccionando a una credencial filtrada, y si la cookie actual también se filtró, dejarla
        viva anula el cambio. El orden (revocar, después abrir la nueva) falla hacia el lado
        seguro: si la sesión nueva no se puede crear, la persona queda deslogueada, nunca con
        una sesión vieja viva.

        La password actual se verifica con el MISMO ``verify_password`` que el login (Argon2id,
        tiempo constante en la comparación). Un fallo es ``422`` y no ``401``: la sesión es
        válida, y un 401 haría que la SPA mande a la persona al login.

        **Nunca se registra ninguna password**, ni su largo, en el detalle de auditoría.
        """
        if actor.is_agent:
            # Inalcanzable hoy (la API solo autentica por cookie), y escrito igual: si mañana un
            # bearer llega a `/api/v1`, un token de agente no puede reescribir la credencial de
            # una persona.
            raise AppHttpException(
                message="Solo una sesión de usuario puede cambiar su contraseña.",
                status_code=403,
                public_context={"code": CODE_SESSION_REQUIRED},
            )

        user = self.user_model.find_by_id(actor.id)
        hash_actual = (user or {}).get("hashed_password") or _DUMMY_HASH
        if not (user and verify_password(current_password, hash_actual)):
            audit.record(
                "auth.password_change_failed",
                status="failure",
                admin=actor,
                target_type="user",
                target_id=actor.id,
                touched_engine=False,
                detail="contraseña actual incorrecta",
            )
            raise AppHttpException(
                message="La contraseña actual no es correcta.",
                status_code=422,
                public_context={"code": CODE_INVALID_CURRENT_PASSWORD},
            )

        assert_password_policy(new_password)
        if compare_digest(new_password.encode("utf-8"), current_password.encode("utf-8")):
            raise AppHttpException(
                message="La contraseña nueva tiene que ser distinta de la actual.",
                status_code=422,
                public_context={"code": CODE_PASSWORD_UNCHANGED},
            )

        self.user_model.set_credential(actor.id, hash_password(new_password))
        otras = session_store.revoke_all_for_user(
            actor.id, session_store.REASON_PASSWORD_CHANGE, except_sid=current_sid
        )
        if current_sid:
            session_store.revoke(current_sid, session_store.REASON_PASSWORD_CHANGE)
        # `record` y no `record_intent`: la password YA cambió, y abortar por un fallo al auditar
        # dejaría a la persona sin saber cuál de las dos vale.
        audit.record(
            "auth.password_changed",
            admin=actor,
            target_type="user",
            target_id=actor.id,
            touched_engine=False,
            detail=f"contraseña cambiada por el propio usuario; {otras} sesión(es) más cerrada(s)",
        )
        return otras

    def step_up(self, actor: Actor, *, password: str, sid: str) -> dict:
        """
        Confirma la contraseña del PROPIO actor y abre la ventana de step-up de ESTA sesión.

        - Éxito: ``step_up_at = ahora`` y fallos en cero (``session_store.mark_step_up``). El
          ``sid`` NO rota, así que el token CSRF de los requests en vuelo sigue valiendo.
        - Contraseña incorrecta: 400 ``auth.step_up_failed`` con ``attempts_remaining``, y un
          fallo más en la sesión. Al llegar a ``STEP_UP_MAX_FAILURES`` la sesión se revoca
          (``step_up_failed``) y la respuesta es el 401 de sesión cerrada: una cookie robada sin
          la contraseña no puede seguir probando.

        Verifica con el MISMO ``verify_password`` que el login. **Nunca se audita la contraseña**,
        ni su largo.
        """
        if actor.is_agent or not sid:
            raise AppHttpException(
                message="Solo una sesión de usuario puede confirmar su contraseña.",
                status_code=403,
                public_context={"code": CODE_SESSION_REQUIRED},
            )

        user = self.user_model.find_by_id(actor.id)
        hash_actual = (user or {}).get("hashed_password") or _DUMMY_HASH
        if user and verify_password(password, hash_actual):
            cuando = session_store.mark_step_up(sid)
            if cuando is None:
                # La sesión murió entre la autenticación y acá (logout en otra pestaña, revocación
                # administrativa): no hay ventana que abrir.
                raise AppHttpException(
                    message="La sesión se cerró. Volvé a iniciar sesión.",
                    status_code=401,
                    public_context={"code": f"auth.session_{session_store.REASON_LOGOUT}"},
                )
            audit.record(
                "auth.step_up",
                admin=actor,
                target_type="user",
                target_id=actor.id,
                touched_engine=False,
            )
            from app.core.step_up import STEP_UP_TTL_SECONDS, window_until

            return {
                "step_up_expires_at": window_until(cuando),
                "step_up_ttl_seconds": STEP_UP_TTL_SECONDS,
            }

        fallos = session_store.record_step_up_failure(sid)
        revocada = fallos >= session_store.STEP_UP_MAX_FAILURES
        audit.record(
            "auth.step_up_failed",
            status="failure",
            admin=actor,
            target_type="user",
            target_id=actor.id,
            touched_engine=False,
            detail=(
                f"contraseña incorrecta ({fallos}/{session_store.STEP_UP_MAX_FAILURES})"
                + ("; sesión revocada" if revocada else "")
            ),
        )
        if revocada:
            raise AppHttpException(
                message=_mensaje_step_up_revocada(),
                status_code=401,
                public_context={"code": f"auth.session_{session_store.REASON_STEP_UP_FAILED}"},
            )
        raise AppHttpException(
            message="La contraseña no es correcta.",
            status_code=400,
            public_context={
                "code": CODE_STEP_UP_FAILED,
                "attempts_remaining": session_store.STEP_UP_MAX_FAILURES - fallos,
            },
        )


def _mensaje_step_up_revocada() -> str:
    from app.core.auth import _MENSAJE_401

    return _MENSAJE_401[session_store.REASON_STEP_UP_FAILED]
