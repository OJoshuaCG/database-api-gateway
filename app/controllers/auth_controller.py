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

from secrets import token_urlsafe

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
        # SIEMPRE se paga el hash, exista o no la fila.
        hash_a_verificar = user["hashed_password"] if existe else _DUMMY_HASH
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
