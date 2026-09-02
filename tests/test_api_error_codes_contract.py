"""
Contrato de los códigos de error que el frontend usa para clasificar.

Existe porque los tests de cada módulo afirman **status y prosa**, no el `code`. Con eso, mover
un código a `context` (donde el gateway solo lo expone en `development`) o renombrarlo no rompe
ningún test y el frontend se entera en producción — que es exactamente lo que pasó con
`ProjectController`: sus siete excepciones nacieron con `context=`, y el `missing_model_ids` del
422 de vinculación, que es el dato que hace utilizable ese error, no llegaba fuera de desarrollo.

Lo que se afirma acá es la forma del payload público, no el mensaje: `detail.public_context.code`
y los campos estructurados que el cliente necesita para elegir el CTA.
"""

import pytest

from app.services import migration_freeze_catalog as freeze_codes
from app.services import project_catalog as project_codes


def _pc(resp) -> dict:
    """`public_context` viaja dentro de `detail` (ver HandlerExceptions)."""
    return (resp.json().get("detail") or {}).get("public_context") or {}


def _new_project(admin_client, name="Proyecto A", **extra) -> dict:
    payload = {"name": name, **extra}
    return admin_client.post("/api/v1/projects", json=payload)


def _new_model(admin_client, slug="wa", name="WA") -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# Proyectos                                                                    #
# --------------------------------------------------------------------------- #
def test_proyecto_inexistente_clasifica(admin_client):
    r = admin_client.get("/api/v1/projects/9999")
    assert r.status_code == 404
    assert _pc(r)["code"] == project_codes.CODE_NOT_FOUND


def test_nombre_duplicado_clasifica(admin_client):
    assert _new_project(admin_client).status_code == 201
    r = _new_project(admin_client)
    assert r.status_code == 409
    assert _pc(r)["code"] == project_codes.CODE_NAME_TAKEN


def test_vincular_ids_inexistentes_expone_missing_model_ids(admin_client):
    """
    El campo estructurado es el punto de este test, no el código.

    Es lo que permite señalar las filas malas del selector en vez de invalidar la selección
    entera. Vivía solo en `context`, así que en producción no llegaba.
    """
    pid = _new_project(admin_client).json()["data"]["id"]
    ok_id = _new_model(admin_client)
    r = admin_client.post(
        f"/api/v1/projects/{pid}/blueprints", json={"model_ids": [ok_id, 4242]}
    )
    assert r.status_code == 422
    pc = _pc(r)
    assert pc["code"] == project_codes.CODE_BLUEPRINTS_NOT_FOUND
    assert pc["missing_model_ids"] == [4242]
    # Todo-o-nada: el id válido tampoco se vinculó.
    assert admin_client.get(f"/api/v1/projects/{pid}/blueprints").json()["data"] == []


def test_desvincular_lo_que_no_esta_vinculado_clasifica(admin_client):
    """404 de la RELACIÓN, no del recurso: el blueprint existe."""
    pid = _new_project(admin_client).json()["data"]["id"]
    mid = _new_model(admin_client)
    r = admin_client.delete(f"/api/v1/projects/{pid}/blueprints/{mid}")
    assert r.status_code == 404
    assert _pc(r)["code"] == project_codes.CODE_BLUEPRINT_NOT_LINKED


def test_blueprint_inexistente_en_la_vista_inversa_clasifica(admin_client):
    """404 del RECURSO. Se distingue del anterior porque el CTA difiere."""
    r = admin_client.get("/api/v1/database-models/9999/projects")
    assert r.status_code == 404
    assert _pc(r)["code"] == project_codes.CODE_BLUEPRINT_NOT_FOUND


def test_el_alta_con_ids_invalidos_deja_el_proyecto_creado(admin_client):
    """
    La trampa que el contrato documenta en voz alta.

    El 422 llega DESPUÉS de que la fila del proyecto se commiteó, así que reintentar el alta
    daría 409 por el nombre ya tomado. La UI tiene que reintentar solo la vinculación, y este
    test es lo que impide que el comportamiento cambie sin que nadie lo note.
    """
    r = _new_project(admin_client, name="Con ids malos", model_ids=[4242])
    assert r.status_code == 422
    assert _pc(r)["code"] == project_codes.CODE_BLUEPRINTS_NOT_FOUND
    listado = admin_client.get("/api/v1/projects").json()["data"]
    assert [p["name"] for p in listado] == ["Con ids malos"]
    assert listado[0]["blueprint_count"] == 0


# --------------------------------------------------------------------------- #
# Migraciones de blueprint                                                     #
# --------------------------------------------------------------------------- #
def _migration_with_override(admin_client) -> tuple[int, str]:
    model_id = _new_model(admin_client, slug="ov", name="OV")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={
            "version": "0001",
            "name": "inicial",
            "up_sql": "CREATE TABLE t (id INT)",
            "up_sql_postgresql": "CREATE TABLE t (id INTEGER)",
        },
    )
    assert r.status_code == 201, r.text
    return model_id, "0001"


def test_overrides_obsoletos_clasifica_y_nombra_los_campos(admin_client):
    model_id, version = _migration_with_override(admin_client)
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/{version}",
        json={"up_sql": "CREATE TABLE t (id BIGINT)"},
    )
    assert r.status_code == 409, r.text
    pc = _pc(r)
    assert pc["code"] == freeze_codes.CODE_STALE_OVERRIDES
    assert pc["stale_overrides"] == ["up_sql_postgresql"]


def test_aplicacion_parcial_clasifica_y_nombra_la_bd(admin_client, monkeypatch):
    """
    El checkpoint incompleto se simula: montar una aplicación parcial real exige un motor.

    Lo que se prueba es el contrato del error, no la detección — de eso se ocupan los tests del
    módulo de progreso.
    """
    from app.services.db_admin import migration_progress

    model_id = _new_model(admin_client, slug="pp", name="PP")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "inicial", "up_sql": "CREATE TABLE t (id INT)"},
    )
    assert r.status_code == 201, r.text
    fila = {"managed_database_id": 7, "last_statement_index": 2, "total_statements": 5}
    monkeypatch.setattr(
        migration_progress, "incomplete_progress_for_migration",
        lambda mid, direction: [fila],
    )
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql": "CREATE TABLE t (id BIGINT)"},
    )
    assert r.status_code == 409, r.text
    pc = _pc(r)
    assert pc["code"] == freeze_codes.CODE_PARTIAL_APPLICATION
    assert pc["incomplete_progress"] == [fila]


# --------------------------------------------------------------------------- #
# El vocabulario es cerrado                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "catalogo",
    [project_codes.ERROR_CODES, freeze_codes.ERROR_CODES],
    ids=["projects", "migrations"],
)
def test_los_codigos_llevan_prefijo_de_recurso(catalogo):
    """
    Todo código es `<recurso>.<motivo>`.

    No es cosmético: el frontend agrupa por prefijo para decidir qué pantalla muestra el error,
    así que un código suelto sin recurso no tiene dónde caer.
    """
    assert catalogo, "el catálogo no puede estar vacío"
    for code in catalogo:
        assert "." in code, code
        recurso, _, motivo = code.partition(".")
        assert recurso and motivo, code
        assert code == code.lower(), code


# --------------------------------------------------------------------------- #
# Lote de clonación                                                            #
# --------------------------------------------------------------------------- #
def test_los_codigos_del_lote_de_clon_estan_en_el_vocabulario_cerrado():
    """
    Todo ``CODE_BATCH_*`` tiene que estar en ``ERROR_CODES``. Sin esta afirmación, un código
    nuevo puede emitirse sin entrar al vocabulario, y el cliente —que mapea por código— lo
    recibe como desconocido y cae a un mensaje genérico justo cuando el error es específico.
    """
    from app.services.db_admin import clone_spec as cspec

    batch_codes = {v for k, v in vars(cspec).items() if k.startswith("CODE_BATCH_")}
    assert batch_codes, "no se encontró ningún código de lote"
    assert batch_codes <= cspec.ERROR_CODES
    # Comparten el namespace del módulo a propósito: el lote es la orquestación del clon, no
    # otro módulo, y un vocabulario aparte obligaría al cliente a mantener dos diccionarios.
    assert all(code.startswith("clone.") for code in batch_codes)


def test_el_lote_emite_su_codigo_en_public_context(admin_client):
    """
    El código viaja en ``public_context`` (visible SIEMPRE), no en ``context`` (solo en
    development). Se comprueba sobre un error que no necesita motor: el tope de filas.
    """
    import app.controllers.clone_batch_controller as cbc

    srv = admin_client.post(
        "/api/v1/servers",
        json={
            "name": "srv-lote",
            "host": "10.0.0.5",
            "port": 3306,
            "engine": "mysql",
            "root_username": "root",
            "root_password": "pw",
        },
    )
    assert srv.status_code == 201, srv.text
    sid = srv.json()["data"]["id"]

    original = cbc.CLONE_BATCH_MAX_DATABASES
    cbc.CLONE_BATCH_MAX_DATABASES = 1
    try:
        r = admin_client.post(
            "/api/v1/database-clone-batches",
            json={
                "source_server_id": sid,
                "target_server_id": sid,
                "rows": [
                    {"source_database_name": "a", "target_database_name": "x"},
                    {"source_database_name": "b", "target_database_name": "y"},
                ],
            },
        )
    finally:
        cbc.CLONE_BATCH_MAX_DATABASES = original

    assert r.status_code == 422, r.text
    pc = _pc(r)
    assert pc["code"] == "clone.batch_too_large"
    # Campos estructurados: el cliente arma el mensaje con ellos, no parseando la prosa.
    assert pc["max_databases"] == 1 and pc["requested"] == 2
