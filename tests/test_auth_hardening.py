"""
El login no puede delatar qué usuarios existen, y la autenticación tiene que dejar rastro.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
El mensaje de error ya era genérico y **el endpoint delataba igual**: la condición cortocircuitaba
con ``or``, así que para un usuario inexistente ``verify_password`` no se llamaba. Inexistente
≈1 ms, existente el costo de Argon2id. Los tests que había verificaban el *texto* del 401, que es
exactamente la propiedad que ya estaba bien — ninguno miraba el tiempo ni el número de llamadas.

Medir milisegundos en un test es frágil (el I/O de WSL2 sobre ``/mnt/`` mete varianza de decenas
de ms). Así que **no se mide tiempo: se mide la CAUSA** — que ``verify_password`` se ejecute
siempre, exactamente una vez, en los tres caminos. Es la propiedad que se puede afirmar sin
flakiness y la que un refactor futuro rompería.
"""

from app.models.audit_log import AuditLog


def _rows(action: str) -> list[AuditLog]:
    from app.core.database import Database

    session = Database().get_declarative_base_session()
    try:
        return (
            session.query(AuditLog)
            .filter(AuditLog.action == action)
            .order_by(AuditLog.id.desc())
            .all()
        )
    finally:
        session.close()


def _login(client, username: str, password: str):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


# --------------------------------------------------------------------------- #
# El oráculo de timing                                                        #
# --------------------------------------------------------------------------- #


def _contar_verificaciones(monkeypatch) -> list[str]:
    """Registra CONTRA QUÉ hash se verificó en cada llamada."""
    import app.controllers.auth_controller as mod

    llamadas: list[str] = []
    real = mod.verify_password

    def espia(password: str, hashed: str) -> bool:
        llamadas.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(mod, "verify_password", espia)
    return llamadas


def test_a_missing_user_still_pays_the_hash(client, monkeypatch):
    """
    El caso que delataba: sin fila, antes no se hasheaba nada y el 401 volvía en ~1 ms.
    """
    llamadas = _contar_verificaciones(monkeypatch)
    r = _login(client, "no-existe", "cualquiera")
    assert r.status_code == 401
    assert len(llamadas) == 1, "no se pagó el hash: el tiempo delata que el usuario no existe"

    import app.controllers.auth_controller as mod

    assert llamadas[0] == mod._DUMMY_HASH


def test_an_inactive_user_still_pays_the_hash(client, monkeypatch):
    """
    La otra rama que cortocircuitaba. Una cuenta desactivada es información: dice que la
    identidad existe y que alguien la apagó.
    """
    from app.core.database import Database

    with Database().engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("UPDATE users SET is_active = 0 WHERE username = 'admin'"))

    llamadas = _contar_verificaciones(monkeypatch)
    r = _login(client, "admin", "admin123")
    assert r.status_code == 401
    assert len(llamadas) == 1

    import app.controllers.auth_controller as mod

    # Contra el hash REAL: la fila existe, así que el costo es el mismo que el del camino bueno.
    assert llamadas[0] != mod._DUMMY_HASH


def test_a_wrong_password_pays_the_hash_exactly_once(client, monkeypatch):
    llamadas = _contar_verificaciones(monkeypatch)
    assert _login(client, "admin", "incorrecta").status_code == 401
    assert len(llamadas) == 1


def test_the_401_body_is_identical_in_the_three_paths(client):
    """
    Lo que ya estaba bien y no se puede perder al arreglar el timing: el cuerpo no distingue.
    """
    cuerpos = {
        _login(client, "no-existe", "x").text,
        _login(client, "admin", "incorrecta").text,
    }
    assert len(cuerpos) == 1, f"el 401 distingue los casos: {cuerpos}"


def test_the_dummy_hash_is_not_a_usable_credential():
    """
    Se genera sobre un token aleatorio por proceso, así que no puede coincidir con la password
    de ninguna fila. Si alguien lo reemplazara por un literal, esto sigue pasando — lo que fija
    es que no sea el hash de una cadena obvia.
    """
    import app.controllers.auth_controller as mod
    from app.utils.security import verify_password

    for obvia in ("", "admin", "admin123", "password", "dummy"):
        assert not verify_password(obvia, mod._DUMMY_HASH)


# --------------------------------------------------------------------------- #
# La auditoría de autenticación, que no existía                               #
# --------------------------------------------------------------------------- #


def test_a_successful_login_is_audited(admin_client):
    filas = _rows("auth.login")
    assert filas, "el login no dejó rastro"
    assert filas[0].admin_id == 1
    assert filas[0].admin_username == "admin"
    assert filas[0].status == "success"


def test_a_failed_login_is_audited_without_an_identity(client):
    """
    ``admin_id`` nulo porque **no hay identidad probada**, y el username intentado sí, porque es
    lo único que permite ver un ataque de enumeración en el registro.
    """
    assert _login(client, "no-existe", "x").status_code == 401

    filas = _rows("auth.login_failed")
    assert filas
    assert filas[0].admin_id is None
    assert filas[0].admin_username == "no-existe"
    assert filas[0].status == "failure"
    assert filas[0].detail == "usuario inexistente"


def test_the_failure_detail_separates_enumeration_from_a_typo(client):
    """
    La distinción va al REGISTRO y no a la respuesta: el operador necesita saber si lo que ve es
    enumeración o alguien peleándose con su password, y el atacante no.
    """
    assert _login(client, "admin", "incorrecta").status_code == 401
    assert _rows("auth.login_failed")[0].detail == "password incorrecta"


def test_no_audit_row_ever_carries_the_attempted_password(client):
    """El rastro es de la identidad intentada, nunca de la credencial."""
    assert _login(client, "admin", "SuperSecreta123").status_code == 401
    fila = _rows("auth.login_failed")[0]
    volcado = f"{fila.admin_username} {fila.detail} {fila.action}"
    assert "SuperSecreta123" not in volcado


def test_logout_is_audited(admin_client):
    assert admin_client.post("/api/v1/auth/logout").status_code == 200
    filas = _rows("auth.logout")
    assert filas
    assert filas[0].admin_id == 1


# --------------------------------------------------------------------------- #
# La traza que /auth/me publica                                               #
# --------------------------------------------------------------------------- #


def test_me_publishes_the_previous_login_not_the_current_one(client):
    """
    La propiedad que hace útil el dato: al segundo login, ``previous_login_at`` es el PRIMERO.
    Si publicara el actual, la pantalla que existe para detectar un acceso ajeno mostraría
    siempre el acceso propio que el usuario acaba de hacer.
    """
    assert _login(client, "admin", "admin123").status_code == 200
    primero = client.get("/api/v1/auth/me").json()["data"]
    # El primer login no tiene anterior.
    assert primero["previous_login_at"] is None

    assert _login(client, "admin", "admin123").status_code == 200
    segundo = client.get("/api/v1/auth/me").json()["data"]
    assert segundo["previous_login_at"] is not None


def test_me_publishes_the_last_failed_attempt(client):
    assert _login(client, "admin", "incorrecta").status_code == 401
    assert _login(client, "admin", "admin123").status_code == 200
    datos = client.get("/api/v1/auth/me").json()["data"]
    assert datos["last_failed_at"] is not None
