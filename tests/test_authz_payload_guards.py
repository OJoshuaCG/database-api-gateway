"""
Los dos opt-ins que hacen que el gateway PERSISTA DATOS DE NEGOCIO exigen
``blueprints.captures``, no ``blueprints.write``.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
El plan 13 mapeó ``captures`` a *leer* los resultados capturados, pero **no dijo nada del
opt-in que los genera**. Sin esta regla, ``blueprints.write`` —que ``operator`` tiene— alcanzaba
para dos cosas:

1. ``POST /database-models/from-snapshot`` con ``data_tables``: EXTRAE FILAS de la BD de origen
   y las deja como datos-semilla dentro de una migración del blueprint, o sea dentro de algo
   que después lee cualquiera con ``blueprints.read``.
2. Crear una versión con ``capture_selects=true``: los SELECT de esa versión guardan sus
   resultados al aplicarse.

Las dos son decisiones de DIVULGACIÓN disfrazadas de escritura, y es exactamente el agujero
que el eje de divulgación del §4.2 existe para cerrar.

El chequeo va en la RUTA y no en el controller porque depende del payload y una ruta declara
UNA capacidad (§6.3 punto 1): el piso va en la firma y el extra al lado. Los dos llamadores
internos de ``create_migration`` (``schema-comparisons/adopt`` y el lote de collation) no piden
captura, así que la frontera de la ruta las cubre a todas. Cuando llegue la capa 2 el chequeo
se muda al controller, con el destino en la mano.
"""

import pytest
from sqlalchemy import text

_UP_SQL = "CREATE TABLE users (id INT PRIMARY KEY)"


def _set_role(role: str) -> None:
    """
    Cambia el rol del admin sembrado directo en la BD.

    Es también, de paso, la prueba de que el rol **no vive en la cookie**: la sesión no se
    renueva y el cambio surte efecto en el request siguiente, porque ``get_current_actor``
    relee rol y capacidades en cada uno.
    """
    from app.core.database import Database

    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )


@pytest.fixture()
def operator_client(admin_client):
    """
    El admin sembrado, degradado a ``operator``: tiene ``blueprints.write`` y NO
    ``blueprints.captures``. Conserva sus capacidades globales, que no incluyen ninguna de las
    dos, así que el único cambio es el que se quiere medir.
    """
    _set_role("operator")
    return admin_client


def _model(client, slug="bp") -> int:
    r = client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# capture_selects al crear y al parchear                                      #
# --------------------------------------------------------------------------- #


def test_operator_can_create_a_migration_without_capture(operator_client):
    """El piso sigue siendo ``write``: la regla no le saca a operator lo que ya hacía."""
    model_id = _model(operator_client)
    r = operator_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m1", "up_sql": _UP_SQL},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["capture_selects"] is False


def test_operator_cannot_turn_on_capture_at_creation(operator_client):
    model_id = _model(operator_client)
    r = operator_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m1", "up_sql": "SELECT 1", "capture_selects": True},
    )
    assert r.status_code == 403, r.text
    # Código cerrado y sin nombrar la capacidad que falta.
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    assert "captures" not in r.text


def test_operator_cannot_turn_on_capture_with_a_patch(operator_client):
    """
    Cierra la vía de escape obvia: crear la versión sin captura y prenderla después. Si el
    chequeo estuviera solo en el POST, esto pasaría.
    """
    model_id = _model(operator_client)
    creada = operator_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m1", "up_sql": "SELECT 1"},
    )
    assert creada.status_code == 201, creada.text

    r = operator_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"capture_selects": True},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"


def test_operator_can_turn_capture_off(admin_client):
    """
    APAGARLA no pide nada extra. Exigir ``captures`` para desactivar una captura sería pedir
    el permiso de divulgar para dejar de divulgar.
    """
    model_id = _model(admin_client)
    creada = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m1", "up_sql": "SELECT 1", "capture_selects": True},
    )
    assert creada.status_code == 201, creada.text

    _set_role("operator")
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"capture_selects": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["capture_selects"] is False


def test_owner_can_turn_on_capture(admin_client):
    """El otro lado: ``owner`` sí tiene ``blueprints.captures``, así que el guard no lo frena."""
    model_id = _model(admin_client)
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "m1", "up_sql": "SELECT 1", "capture_selects": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["capture_selects"] is True


# --------------------------------------------------------------------------- #
# from-snapshot con data_tables                                               #
# --------------------------------------------------------------------------- #


def test_operator_cannot_ask_for_seed_data_in_a_snapshot(operator_client, server_payload):
    """
    403 **antes** de tocar el motor: el guard está en la ruta, así que ni siquiera se abre la
    conexión al servidor de origen. Que el servidor no exista de verdad es irrelevante para lo
    que se mide acá, y es lo que hace que el test no necesite un motor.
    """
    srv = operator_client.post("/api/v1/servers", json=server_payload())
    assert srv.status_code == 201, srv.text

    r = operator_client.post(
        "/api/v1/database-models/from-snapshot",
        json={
            "server_id": srv.json()["data"]["id"],
            "database": "origen",
            "name": "Desde snapshot",
            "slug": "desde-snapshot",
            "data_tables": [{"table": "catalogo"}],
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"


def test_a_snapshot_without_data_is_not_blocked_by_the_guard(operator_client, server_payload):
    """
    Sin ``data_tables`` el piso sigue siendo ``write``. No se afirma el resultado —el servidor
    sembrado no existe— sino que el rechazo NO es el de autorización: si esto diera 403, el
    guard estaría cobrándole al caso que solo lee estructura.
    """
    srv = operator_client.post("/api/v1/servers", json=server_payload())
    assert srv.status_code == 201, srv.text

    r = operator_client.post(
        "/api/v1/database-models/from-snapshot",
        json={
            "server_id": srv.json()["data"]["id"],
            "database": "origen",
            "name": "Desde snapshot",
            "slug": "desde-snapshot",
        },
    )
    assert r.status_code != 403, r.text


# --------------------------------------------------------------------------- #
# drop_remote: la MISMA forma de agujero, en otro módulo                      #
# --------------------------------------------------------------------------- #


def _managed_db(client) -> int:
    """
    Siembra la BD directo en el inventario.

    Por la API haría falta un motor real: ``/server-users/adopt`` lista los usuarios del
    servidor antes de adoptar. Lo que se mide acá es la autorización, no el aprovisionamiento,
    así que la fila va derecho — mismo atajo que usa ``test_api_migrations_edit_applied``.
    """
    from app.core.database import Database
    from app.models.managed_database import ManagedDatabase

    srv = client.post("/api/v1/servers", json={
        "name": "srv-drop", "host": "127.0.0.1", "port": 3399, "engine": "mysql",
        "root_username": "root", "root_password": "supersecret",
    })
    assert srv.status_code == 201, srv.text

    session = Database().get_declarative_base_session()
    try:
        bd = ManagedDatabase(name="app_db", server_id=srv.json()["data"]["id"], owner_id=1)
        session.add(bd)
        session.commit()
        return bd.id
    finally:
        session.close()


def test_operator_cannot_drop_the_database_on_the_engine(operator_client):
    """
    Con ``drop_remote=true`` la misma ruta ejecuta un DROP DATABASE sobre la base de un
    tercero, así que exige ``databases.drop`` — que ``operator`` no tiene. El 403 llega ANTES
    de abrir la conexión al motor, que es lo que hace verificable este test sin un motor.
    """
    db_id = _managed_db(operator_client)
    r = operator_client.delete(
        f"/api/v1/managed-databases/{db_id}?drop_remote=true&confirm_name=app_db"
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["public_context"]["code"] == "access.forbidden"


def test_operator_can_forget_a_database_from_the_inventory(operator_client):
    """Sin ``drop_remote`` el piso es ``write``: se olvida una fila y nada más."""
    db_id = _managed_db(operator_client)
    r = operator_client.delete(f"/api/v1/managed-databases/{db_id}")
    assert r.status_code == 200, r.text


def test_owner_is_not_blocked_by_the_drop_guard(admin_client):
    """
    El otro lado: ``owner`` sí tiene ``databases.drop``, así que el guard no lo frena y el
    rechazo que llega es el del motor inexistente, no un 403.
    """
    db_id = _managed_db(admin_client)
    r = admin_client.delete(
        f"/api/v1/managed-databases/{db_id}?drop_remote=true&confirm_name=app_db"
    )
    assert r.status_code != 403, r.text
