"""
Contrato de ``/auth/me`` y ``/authz/catalog``.

OJO CON EL PLANO: capacidades **del gateway**. Los privilegios **del motor** se prueban en
``test_api_server_users.py`` y ``test_api_privileges.py``, y son otro vocabulario.

El test central de este archivo es ``test_me_publishes_exactly_what_require_enforces``: si lo
publicado y lo hecho cumplir divergen, la SPA esconde botones que sí funcionan o —peor— ofrece
botones que cobran un 403 al apretarlos. Publicar una promesa que el servidor no cumple es peor
que no publicarla.
"""

from app.core.authz import declared_capability, require
from app.services.capability_catalog import (
    Capability,
    GatewayRole,
    GlobalCapability,
    capability_matrix,
)

# --------------------------------------------------------------------------- #
# /auth/me                                                                    #
# --------------------------------------------------------------------------- #


def test_me_is_additive_and_keeps_the_old_contract(admin_client):
    """
    ``id`` y ``username`` siguen ahí.

    La SPA hace ``safeParse`` del envelope COMPLETO, así que una divergencia de un campo
    descarta la respuesta entera. Quitar un campo rompería el panel entero, no una pantalla.
    """
    data = admin_client.get("/api/v1/auth/me").json()["data"]
    assert data["id"] == 1
    assert data["username"] == "admin"


def test_me_publishes_exactly_what_require_enforces(admin_client):
    """
    Lo publicado == lo hecho cumplir, ni un elemento más ni uno menos.

    Uno más es una promesa incumplida (la UI ofrece el botón y cobra 403). Uno menos es
    funcionalidad escondida. Se verifica contra el MISMO predicado, iterando el enum.
    """
    from app.core.actor import admin_actor

    publicado = set(admin_client.get("/api/v1/auth/me").json()["data"]["capabilities"])
    actor = admin_actor(
        user_id=1,
        username="admin",
        role=GatewayRole.OWNER,
        globals_=frozenset(
            {GlobalCapability.ACCESS_ADMIN, GlobalCapability.SECURITY_OFFICER}
        ),
    )
    hecho_cumplir = {c.value for c in Capability if actor.has(c)}
    assert publicado == hecho_cumplir


def test_the_seeded_admin_keeps_every_capability(admin_client):
    """
    Comportamiento IDÉNTICO: el admin sembrado conserva las 28.

    Es el requisito de la fase 0. Si esto baja de 28, la migración dejó de ser transparente y
    alguien va a perder el alta de servidores o la rotación de crypto sin aviso.
    """
    data = admin_client.get("/api/v1/auth/me").json()["data"]
    assert len(data["capabilities"]) == len(Capability)
    assert data["role"] == "owner"
    assert sorted(data["global_capabilities"]) == ["access_admin", "security_officer"]


def test_me_publishes_the_step_up_subset(admin_client):
    """
    Se publica para que la UI pida la contraseña ANTES de mandar la operación, en vez de
    descubrirlo por un error. El subconjunto sale del catálogo, no de una lista aparte.
    """
    data = admin_client.get("/api/v1/auth/me").json()["data"]
    step_up = set(data["step_up_capabilities"])
    assert step_up <= set(data["capabilities"])
    # Las que divulgan siempre lo exigen: un factor fresco antes de que un dato del cliente
    # salga del perímetro.
    assert {
        Capability.ENGINE_USERS_SECRETS.value,
        Capability.EXPORTS_DOWNLOAD.value,
        Capability.SQL_CONSOLE_EXECUTE.value,
    } <= step_up


def test_me_requires_a_session(client):
    assert client.get("/api/v1/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# /authz/catalog                                                              #
# --------------------------------------------------------------------------- #


def test_catalog_requires_a_session(client):
    """
    No es público, y no por gusto: el catálogo es el mapa de la política, y el 403 de
    ``require()`` usa un código cerrado justamente para no filtrarlo por fuerza bruta.
    Publicárselo a un anónimo tiraría ese control.
    """
    assert client.get("/api/v1/authz/catalog").status_code == 401


def test_catalog_publishes_every_capability(admin_client):
    rows = admin_client.get("/api/v1/authz/catalog").json()["data"]
    assert {r["id"] for r in rows} == {c.value for c in Capability}
    assert len(rows) == len(capability_matrix())


def test_catalog_marks_the_two_axes_separately(admin_client):
    """
    ``mutates`` y ``discloses`` viajan como campos INDEPENDIENTES.

    Colapsarlos en uno es exactamente lo que hace que ``reveal-password`` y la descarga de un
    export —que no destruyen nada— se cuelen en un rol de escritura.
    """
    rows = {r["id"]: r for r in admin_client.get("/api/v1/authz/catalog").json()["data"]}
    secretos = rows[Capability.ENGINE_USERS_SECRETS.value]
    assert secretos["discloses"] is True
    assert secretos["mutates"] is False
    escritura = rows[Capability.ENGINE_USERS_WRITE.value]
    assert escritura["mutates"] is True
    assert escritura["discloses"] is False


# --------------------------------------------------------------------------- #
# El marcador que hace enumerable la cobertura                                #
# --------------------------------------------------------------------------- #


def test_require_stamps_the_capability_on_the_dependency():
    """
    Sin el marcador, "ningún endpoint quedó sin proteger" no sería una propiedad verificable:
    es lo que va a permitir recorrer ``route.dependant`` y exigir que toda ruta declare una
    capacidad del catálogo.
    """
    dep = require(Capability.DATABASES_DROP)
    assert declared_capability(dep) == "databases.drop"


def test_a_plain_callable_declares_nothing():
    assert declared_capability(lambda r: None) is None


def test_every_capability_has_a_public_alias():
    """
    Un alias por capacidad, y NINGUNO "sin capacidad": un alias genérico sería el atajo por el
    que un endpoint nuevo quedaría autenticado pero no autorizado.
    """
    from app.core import authz

    declaradas = set()
    for value in vars(authz).values():
        metadata = getattr(value, "__metadata__", None)
        if not metadata:
            continue
        cap = declared_capability(metadata[0].dependency)
        if cap:
            declaradas.add(cap)
    assert declaradas == {c.value for c in Capability}
