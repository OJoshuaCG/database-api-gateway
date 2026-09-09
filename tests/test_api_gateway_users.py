"""
Usuarios del gateway: el alta sin password, la invitación de un solo uso y el último admin.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Es el módulo que hace **usable** el modelo de capacidades —hasta acá había un solo usuario, así
que roles y alcances existían sin nadie a quien aplicarlos— y trae la decisión que ordena todo:
**la password inicial no la pone quien crea la cuenta**.

El motivo no es comodidad: si un administrador tipeara la password inicial de otra persona,
conocería una credencial funcional de esa identidad, y con eso **toda fila de ``audit_log``
atribuida a esa persona sería repudiable**. Para un sistema cuyo valor central es el rastro, eso
es fatal. Y con ``access_admin`` en el modelo tampoco es solo repudio: es la vía de escalada —
crear una identidad ``owner``, conocer su password y operar producción con la cara de otro.

Y "cambio forzado en el primer login" **no lo arregla**: quien la puso pudo haber entrado antes.
"""

import pytest

from app.core.database import Database
from app.models.user_model import UserModel


def _crear(admin_client, username="nueva", **extra):
    payload = {"username": username, "full_name": "Persona Nueva", **extra}
    r = admin_client.post("/api/v1/gateway-users", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _aceptar(client, token, password="ContraseñaLarga123"):
    return client.post(
        "/api/v1/gateway-users/invite/accept", json={"token": token, "password": password}
    )


# --------------------------------------------------------------------------- #
# El alta no lleva password                                                   #
# --------------------------------------------------------------------------- #


def test_the_create_payload_has_no_password_field(admin_client):
    """
    Mandar una password en el alta **no la fija**: el schema la ignora. Si algún día alguien
    agrega el campo, este test lo pone rojo — y ese campo es la brecha de repudio entera.
    """
    r = admin_client.post(
        "/api/v1/gateway-users",
        json={"username": "colada", "password": "LaPusoElAdmin1"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["credential_set"] is False

    fila = UserModel().find_by_username("colada")
    assert fila["hashed_password"] == "", "el alta fijó una credencial"


def test_a_pending_user_cannot_log_in(client, admin_client):
    """La cuenta existe y **no puede entrar**: es lo que hace que no haya ventana de uso."""
    _crear(admin_client, "pendiente")
    r = client.post(
        "/api/v1/auth/login", json={"username": "pendiente", "password": "cualquiera"}
    )
    assert r.status_code == 401


def test_a_pending_admin_does_not_satisfy_the_invariant(admin_client):
    """
    **El hueco que abre el estado sin credencial.** Una invitación pendiente nace ACTIVA, así
    que sin filtrar por credencial un usuario que todavía no aceptó **satisfaría el invariante
    del último administrador siendo incapaz de entrar** — el bloqueo total disfrazado de "hay un
    administrador".
    """
    _crear(admin_client, "futura", global_capabilities=["access_admin"])
    assert UserModel().count_active_access_admins() == 1, (
        "la invitación pendiente se contó como administrador activo"
    )


# --------------------------------------------------------------------------- #
# La invitación                                                               #
# --------------------------------------------------------------------------- #


def test_accepting_the_invite_sets_the_credential_and_allows_login(client, admin_client):
    datos = _crear(admin_client, "acepta")
    assert _aceptar(client, datos["invite_token"]).status_code == 200

    r = client.post(
        "/api/v1/auth/login",
        json={"username": "acepta", "password": "ContraseñaLarga123"},
    )
    assert r.status_code == 200, r.text


def test_the_invite_is_single_use(client, admin_client):
    """
    Un solo uso por ``credential_epoch``, no por una tabla de tokens consumidos: aceptar sube el
    contador y el token deja de validar. Sin esto, un token con 48 h de TTL permite reescribir
    la password de la cuenta las veces que quiera quien lo tenga.

    El segundo intento cae en el **mismo 422 genérico** que un token inválido, y eso es
    deliberado: una vez aceptada, la cuenta deja de ser candidata, así que el endpoint público
    no puede distinguir "ya se usó" de "no existe" — si lo hiciera sería un oráculo de qué
    invitaciones hay pendientes.
    """
    datos = _crear(admin_client, "unavez")
    assert _aceptar(client, datos["invite_token"]).status_code == 200

    segundo = _aceptar(client, datos["invite_token"], password="OtraContraseña456")
    assert segundo.status_code == 422, segundo.text
    # Y la password NO se reescribió.
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "unavez", "password": "ContraseñaLarga123"},
    )
    assert r.status_code == 200, "la segunda aceptación cambió la credencial"


def test_reinviting_kills_the_previous_token(client, admin_client):
    """
    Reemitir sube el epoch, así que es también la vía para **revocar** una invitación filtrada:
    no hace falta un endpoint de revocación aparte.
    """
    datos = _crear(admin_client, "reinvita")
    viejo = datos["invite_token"]

    r = admin_client.post(f"/api/v1/gateway-users/{datos['id']}/invite")
    assert r.status_code == 200, r.text
    nuevo = r.json()["data"]["invite_token"]
    assert nuevo != viejo

    assert _aceptar(client, viejo).status_code == 422, "el token viejo sigue sirviendo"
    assert _aceptar(client, nuevo).status_code == 200


def test_a_tampered_token_is_rejected(client, admin_client):
    datos = _crear(admin_client, "alterada")
    exp, mac = datos["invite_token"].split(".", 1)
    assert _aceptar(client, f"{exp}.{'0' * len(mac)}").status_code == 422


def test_the_token_of_one_user_does_not_work_for_another(client, admin_client):
    """Está atado al ``user_id``, que viaja DENTRO del token firmado."""
    primera = _crear(admin_client, "primera")
    _crear(admin_client, "segunda")

    assert _aceptar(client, primera["invite_token"]).status_code == 200
    # El de la primera ya se usó; el de la segunda sigue pendiente y su token es otro.
    assert UserModel().find_by_username("segunda")["hashed_password"] == ""


def test_a_short_password_is_rejected(client, admin_client):
    """
    Largo mínimo y **ninguna política de composición**: las reglas de "una mayúscula y un
    símbolo" empujan a `Password1!` y bajan la entropía real.
    """
    datos = _crear(admin_client, "corta")
    r = _aceptar(client, datos["invite_token"], password="corta1")
    assert r.status_code == 422
    # Lo rechaza el SCHEMA, que es la capa correcta para un largo. El controller tiene el mismo
    # chequeo contra la MISMA constante para los llamadores que no pasan por HTTP.
    assert "password" in r.text


def test_the_accept_endpoint_does_not_reveal_which_invites_exist(client):
    """
    "Token inválido" y "usuario inexistente" responden **igual**: si distinguieran, el endpoint
    público sería un oráculo de qué invitaciones hay pendientes.
    """
    r = _aceptar(client, "0000000000.deadbeef")
    assert r.status_code == 422
    assert "no es válida o ya se usó" in r.text


# --------------------------------------------------------------------------- #
# El guard del último administrador                                           #
# --------------------------------------------------------------------------- #


def test_deactivating_the_last_access_admin_is_rejected(admin_client):
    admin_id = UserModel().find_by_username("admin")["id"]
    r = admin_client.patch(f"/api/v1/gateway-users/{admin_id}", json={"is_active": False})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "access.last_admin_protected"


def test_removing_access_admin_from_the_last_one_is_rejected(admin_client):
    """
    El otro camino al mismo bloqueo, y el que un guard solo sobre ``is_active`` dejaría abierto:
    quitarle la capacidad global en vez de desactivar la cuenta.
    """
    admin_id = UserModel().find_by_username("admin")["id"]
    r = admin_client.put(
        f"/api/v1/gateway-users/{admin_id}/access",
        json={"global_capabilities": ["security_officer"], "scope_grants": []},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "access.last_admin_protected"


def test_with_a_second_active_admin_the_first_can_be_deactivated(client, admin_client):
    """
    Y la contracara: el guard **no** puede bloquear el offboarding legítimo. Se exige que el
    segundo administrador esté activo Y con credencial fijada.
    """
    datos = _crear(admin_client, "releva", global_capabilities=["access_admin"])
    assert _aceptar(client, datos["invite_token"]).status_code == 200

    admin_id = UserModel().find_by_username("admin")["id"]
    r = admin_client.patch(f"/api/v1/gateway-users/{admin_id}", json={"is_active": False})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Acceso por alcance                                                          #
# --------------------------------------------------------------------------- #


def test_setting_access_replaces_instead_of_adding(admin_client):
    """
    Es un reemplazo, no un incremento: la pregunta que responde una pantalla de accesos es "qué
    acceso tiene", y con endpoints por grant el estado final depende del orden de N llamadas.
    """
    datos = _crear(admin_client, "conalcance")
    uid = datos["id"]

    r = admin_client.put(
        f"/api/v1/gateway-users/{uid}/access",
        json={
            "global_capabilities": [],
            "scope_grants": [
                {"scope_type": "environment", "scope_id": 1, "role": "operator"},
                {"scope_type": "server", "scope_id": 2, "role": "viewer"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]["scope_grants"]) == 2

    r = admin_client.put(
        f"/api/v1/gateway-users/{uid}/access",
        json={
            "global_capabilities": [],
            "scope_grants": [
                {"scope_type": "environment", "scope_id": 1, "role": "viewer"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    grants = r.json()["data"]["scope_grants"]
    assert len(grants) == 1, "el segundo PUT sumó en vez de reemplazar"
    assert grants[0]["role"] == "viewer"


def test_the_scope_type_survives_the_round_trip(admin_client):
    """
    El defecto que tenía el lector: perdía el ``scope_type`` y un grant de servidor se leía
    como uno de entorno. Se verifica con **el mismo número** en los dos, que es la forma exacta
    en que se manifestaba.
    """
    uid = _crear(admin_client, "mismonum")["id"]
    r = admin_client.put(
        f"/api/v1/gateway-users/{uid}/access",
        json={
            "global_capabilities": [],
            "scope_grants": [
                {"scope_type": "environment", "scope_id": 3, "role": "viewer"},
                {"scope_type": "server", "scope_id": 3, "role": "owner"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    pares = {(g["scope_type"], g["role"]) for g in r.json()["data"]["scope_grants"]}
    assert pares == {("environment", "viewer"), ("server", "owner")}


def test_changing_the_role_revokes_the_sessions(admin_client):
    """
    El rol ya se relee por request, así que el efecto era inmediato igual. Tachar las sesiones
    es para que el corte **quede con motivo** y la persona entienda por qué volvió al login.
    """
    from fastapi.testclient import TestClient

    from app.models.gateway_session import GatewaySession
    from main import app

    datos = _crear(admin_client, "degradada")
    # Un client APARTE: `admin_client` es el mismo objeto que `client`, así que loguearse con
    # otra identidad ahí le quitaría al test la sesión de administrador que necesita después.
    otra = TestClient(app)
    assert _aceptar(otra, datos["invite_token"]).status_code == 200
    assert otra.post(
        "/api/v1/auth/login",
        json={"username": "degradada", "password": "ContraseñaLarga123"},
    ).status_code == 200

    r = admin_client.patch(
        f"/api/v1/gateway-users/{datos['id']}", json={"gateway_role": "owner"}
    )
    assert r.status_code == 200, r.text

    s = Database().get_declarative_base_session()
    try:
        filas = s.query(GatewaySession).filter(GatewaySession.user_id == datos["id"]).all()
        assert filas
        assert all(f.revoked_at is not None for f in filas)
        assert all(f.revoked_reason == "role_change" for f in filas)
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Lo que no se toca                                                           #
# --------------------------------------------------------------------------- #


def test_the_username_cannot_be_changed(admin_client):
    """
    No está en el schema del PATCH y no es un olvido: es la identidad que se audita, y
    ``audit_log`` la desnormaliza **sin FK**, así que renombrar reescribiría el significado de
    las filas viejas.
    """
    uid = _crear(admin_client, "fija")["id"]
    admin_client.patch(f"/api/v1/gateway-users/{uid}", json={"username": "otra"})
    assert UserModel().find_by_id(uid)["username"] == "fija"


def test_a_duplicate_username_is_409(admin_client):
    _crear(admin_client, "repetida")
    r = admin_client.post("/api/v1/gateway-users", json={"username": "repetida"})
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "gateway_user.username_taken"


def test_the_listing_never_exposes_the_credential(admin_client):
    _crear(admin_client, "listada")
    r = admin_client.get("/api/v1/gateway-users?size=50")
    assert r.status_code == 200, r.text
    assert "hashed_password" not in r.text
    assert "$argon2" not in r.text


@pytest.mark.parametrize("rol", ["superuser", "root", ""])
def test_an_invalid_role_is_422(admin_client, rol):
    """
    El caso ``""`` es el que importaba: con un ``or`` se coercionaba a ``viewer`` **en
    silencio**. Un valor que alguien MANDÓ tiene que fallar, no resolverse a un default — es la
    diferencia entre omitir el campo y equivocarse.
    """
    r = admin_client.post(
        "/api/v1/gateway-users", json={"username": "malrol", "gateway_role": rol}
    )
    assert r.status_code == 422, r.text


def test_the_module_requires_gateway_admin(admin_client):
    """
    Todo el módulo detrás de ``gateway.admin``, que **no** lo tiene el rol ``owner`` — solo las
    globales. Administrar accesos no es una operación más del operador.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from main import app

    # Sin sesión: 401. Va en un client aparte porque `admin_client` ES `client` ya autenticado.
    assert TestClient(app).get("/api/v1/gateway-users").status_code == 401

    # Con sesión de `owner` pero sin las globales: 403. Es la mitad que importa — el rol
    # operativo más alto no administra accesos.
    with Database().engine.begin() as conn:
        conn.execute(text("DELETE FROM user_global_capabilities WHERE user_id = 1"))
    r = admin_client.get("/api/v1/gateway-users")
    assert r.status_code == 403, r.text
