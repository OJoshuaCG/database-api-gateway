"""
La identidad del ``Actor`` tiene que ATERRIZAR en ``audit_log``.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
El swap de ``AdminDep`` a ``Actor`` toca 227 sitios que hoy pasan el dict ``admin``. Un olvido
en cualquiera de ellos deja la fila de auditoría con ``admin_id`` NULO — y ese es el modo de
fallo peor de todos, porque **ningún test lo detecta**: los 22 tests de la API de charsets
pasan igual con o sin atribución, ya que ninguno mira ``audit_log``.

O sea: sin este archivo, el refactor podía vaciar la atribución de todo el registro de
auditoría sin que nada se pusiera rojo. Y la auditoría es lo que el módulo de exportación
declara como su único control compensatorio.

``Actor`` es frozen, sin ``.get()`` y no subscriptable justamente para que el olvido explote —
pero eso solo cubre los sitios que *acceden* a los campos. Un sitio que simplemente **no pasa**
la identidad no explota: pasa ``None`` y audita anónimo. Eso lo cubre este test, y nada más.

PATRÓN PARA EL RESTO DEL SWAP: al migrar un módulo que audite, se agrega acá una función que
verifique su acción. Es más barato que reabrir cada archivo de tests del módulo.
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


# --------------------------------------------------------------------------- #
# Rutas YA migradas a Actor                                                   #
# --------------------------------------------------------------------------- #


def test_charset_option_create_attributes_the_actor(admin_client):
    """
    ``POST /charset-collation-options`` ya usa ``CatalogsWrite``, así que su controller recibe
    un ``Actor`` donde antes llegaba un dict. La fila tiene que quedar atribuida igual.
    """
    resp = admin_client.post(
        "/api/v1/charset-collation-options",
        json={"engine_family": "mysql", "charset": "armscii8", "collation": "armscii8_general_ci"},
    )
    assert resp.status_code in (200, 201), resp.text

    rows = _rows("charset_collation_option.create")
    assert rows, "la operación no dejó fila de auditoría"
    fila = rows[0]
    assert fila.admin_id == 1, f"admin_id quedó en {fila.admin_id!r}: se perdió la atribución"
    assert fila.admin_username == "admin"


def test_charset_option_update_attributes_the_actor(admin_client):
    creada = admin_client.post(
        "/api/v1/charset-collation-options",
        json={"engine_family": "mysql", "charset": "geostd8", "collation": "geostd8_general_ci"},
    )
    assert creada.status_code in (200, 201), creada.text
    option_id = creada.json()["data"]["id"]

    resp = admin_client.patch(
        f"/api/v1/charset-collation-options/{option_id}", json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text

    rows = _rows("charset_collation_option.update")
    assert rows
    assert rows[0].admin_id == 1
    assert rows[0].admin_username == "admin"


def test_environment_create_attributes_the_actor(admin_client):
    """``POST /environments`` ya usa ``GatewayAdmin``."""
    resp = admin_client.post("/api/v1/environments", json={"name": "Preprod", "slug": "preprod"})
    assert resp.status_code in (200, 201), resp.text

    rows = _rows("environment.create")
    assert rows, "la operación no dejó fila de auditoría"
    assert rows[0].admin_id == 1
    assert rows[0].admin_username == "admin"


def test_weakening_an_environment_attributes_the_actor(admin_client):
    """
    El caso de más valor del módulo: ``environment.weaken`` se registra con ``record_intent``,
    que es **fail-closed** — si el rastro no se puede persistir, el aflojamiento de política no
    se ejecuta. Un rastro que se persiste pero sin atribución es peor que no tenerlo: dice que
    alguien abrió la barrera de producción y no dice quién.

    Camino distinto al de ``record``, así que la normalización de identidades hay que
    verificarla también acá y no alcanza con el ``environment.create`` de arriba.
    """
    listado = admin_client.get("/api/v1/environments?size=50")
    assert listado.status_code == 200, listado.text
    prod = next(e for e in listado.json()["data"] if e["slug"] == "production")

    resp = admin_client.patch(
        f"/api/v1/environments/{prod['id']}?confirm_slug=production",
        json={"blocks_destructive_migrations": False},
    )
    assert resp.status_code == 200, resp.text

    rows = _rows("environment.weaken")
    assert rows, "el aflojamiento de política no dejó rastro"
    assert rows[0].admin_id == 1, f"admin_id quedó en {rows[0].admin_id!r}"
    assert rows[0].admin_username == "admin"


def test_project_create_attributes_the_actor(admin_client):
    """``POST /projects`` ya usa ``BlueprintsWrite`` (``projects`` vive dentro de ese módulo)."""
    resp = admin_client.post("/api/v1/projects", json={"name": "Omnicanal"})
    assert resp.status_code in (200, 201), resp.text

    rows = _rows("project.create")
    assert rows, "la operación no dejó fila de auditoría"
    assert rows[0].admin_id == 1
    assert rows[0].admin_username == "admin"


# --------------------------------------------------------------------------- #
# Rutas TODAVÍA con el guard legado                                           #
# --------------------------------------------------------------------------- #


def test_a_legacy_route_still_attributes_the_dict(admin_client, server_payload):
    """
    El otro lado del invariante: mientras la migración avanza, una ruta que sigue con
    ``AdminDep`` tiene que auditar igual de bien. Si esto se rompiera, el problema no sería el
    swap sino la normalización de identidades de ``audit._identity``.
    """
    resp = admin_client.post("/api/v1/servers", json=server_payload())
    assert resp.status_code in (200, 201), resp.text

    rows = _rows("server.create")
    assert rows
    assert rows[0].admin_id == 1
    assert rows[0].admin_username == "admin"


# --------------------------------------------------------------------------- #
# La normalización, en aislamiento                                            #
# --------------------------------------------------------------------------- #


def test_identity_reads_both_shapes_without_giving_actor_a_get():
    """
    ``identity_of`` es lo que hace posible la migración incremental — y el ``Actor`` sigue
    siendo estricto, que es lo que hace ruidosos los olvidos.

    Vive en ``app/core/actor.py`` y no en ``audit`` porque la auditoría no es su único
    consumidor: el historial de la consola SQL, la autoría de un lote y el ``_guard_owner`` de
    exportación leen la misma identidad — y ese último es una decisión de autorización.
    """
    from app.core.actor import admin_actor, identity_of
    from app.services.capability_catalog import GatewayRole

    actor = admin_actor(user_id=9, username="leo", role=GatewayRole.VIEWER)
    assert identity_of(actor) == (9, "leo")
    assert identity_of({"id": 7, "username": "ana"}) == (7, "ana")
    assert identity_of(None) == (None, None)
    assert not hasattr(actor, "get")


def test_a_swallowed_call_site_is_the_exception_to_the_loud_failure():
    """
    La propiedad "un sitio olvidado explota" **no** vale dentro de un ``try/except``
    best-effort: ahí el ``AttributeError`` se lo traga el except y el efecto es el silencioso
    que el tipo existía para evitar.

    Pasó de verdad: ``_record_history`` de la consola SQL leía ``admin.get("id")`` dentro de su
    swallow, y con un ``Actor`` la fila del historial **desaparecía sin error** — lo detectaron
    dos tests del módulo, no el tipo. Este test fija la lección midiendo la mitad que sí se
    puede medir sin un motor: que el acceso tipo dict falla, para que quede claro por qué el
    swallow lo vuelve invisible y por qué toda lectura de identidad va por ``identity_of``.
    """
    from app.core.actor import admin_actor
    from app.services.capability_catalog import GatewayRole

    actor = admin_actor(user_id=9, username="leo", role=GatewayRole.VIEWER)

    try:
        actor.get("id")  # type: ignore[attr-defined]
    except AttributeError:
        pass
    else:
        raise AssertionError("el Actor aceptó .get(): se perdió la propiedad de fallo ruidoso")

    # Y el modo de fallo real: envuelto, el mismo acceso no deja rastro.
    capturado = None
    try:
        capturado = actor.get("id")  # type: ignore[attr-defined]
    except Exception:
        pass
    assert capturado is None
