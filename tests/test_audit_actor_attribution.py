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
    ``audit._identity`` es lo que hace posible la migración incremental — y el ``Actor`` sigue
    siendo estricto, que es lo que hace ruidosos los olvidos.
    """
    from app.core.actor import admin_actor
    from app.services.audit import _identity
    from app.services.capability_catalog import GatewayRole

    actor = admin_actor(user_id=9, username="leo", role=GatewayRole.VIEWER)
    assert _identity(actor) == (9, "leo")
    assert _identity({"id": 7, "username": "ana"}) == (7, "ana")
    assert _identity(None) == (None, None)
    assert not hasattr(actor, "get")
