"""Endpoints de migraciones de blueprints: CRUD, checksum, traducción, rollback sugerido."""

from contextlib import contextmanager


def _new_model(admin_client, slug="whatsapp", name="Whatsapp") -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_migration(admin_client, model_id, **overrides):
    payload = {
        "version": "0001",
        "name": "Esquema inicial",
        "up_sql": "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100))",
    }
    payload.update(overrides)
    return admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations", json=payload
    )


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #
def test_requires_auth(client):
    assert client.get("/api/v1/database-models/1/migrations").status_code == 401


# --------------------------------------------------------------------------- #
# Create                                                                       #
# --------------------------------------------------------------------------- #
def test_create_returns_translation_and_suggested_rollback(admin_client):
    model_id = _new_model(admin_client)
    r = _create_migration(admin_client, model_id)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["version"] == "0001"
    # Traducción cross-engine calculada.
    assert "mysql" in data["translated"] and "postgresql" in data["translated"]
    assert "AUTO_INCREMENT" not in data["translated"]["postgresql"]
    # Rollback sugerido (aditivo) pero NO confirmado.
    assert data["down_sql_suggested"] == "DROP TABLE IF EXISTS users;"
    assert data["down_sql"] is None
    assert len(data["checksum"]) == 64


def _create_auto(admin_client, model_id, up_sql="CREATE TABLE t (id INT PRIMARY KEY)", name="m"):
    """Crea una migración SIN pasar 'version' (autoasignación secuencial)."""
    return admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"name": name, "up_sql": up_sql},
    )


def test_version_autoassigned_when_omitted(admin_client):
    model_id = _new_model(admin_client, slug="auto", name="Auto")
    versions = []
    for _ in range(3):
        r = _create_auto(admin_client, model_id)
        assert r.status_code == 201, r.text
        versions.append(r.json()["data"]["version"])
    assert versions == ["0001", "0002", "0003"]
    # El blueprint refleja la última autoasignada.
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0003"


def test_autoassign_is_max_plus_one_not_count(admin_client):
    model_id = _new_model(admin_client, slug="mix", name="Mix")
    # Versión explícita alta; la autoasignación debe seguir desde max+1, no desde count.
    assert _create_migration(admin_client, model_id, version="0005").status_code == 201
    r = _create_auto(admin_client, model_id)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["version"] == "0006"


def test_explicit_version_still_honored_and_duplicate_409(admin_client):
    model_id = _new_model(admin_client, slug="exp", name="Exp")
    assert _create_migration(admin_client, model_id, version="0003").status_code == 201
    # Explícita respetada (aunque no haya 0001/0002).
    assert _create_auto(admin_client, model_id).json()["data"]["version"] == "0004"
    # Explícita duplicada → 409.
    assert _create_migration(admin_client, model_id, version="0003").status_code == 409


def test_create_bumps_model_current_version(admin_client):
    model_id = _new_model(admin_client, slug="sms", name="SMS")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(admin_client, model_id, version="0002",
                      up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)")
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0002"


def test_create_with_explicit_down_sql(admin_client):
    model_id = _new_model(admin_client, slug="logistica", name="Logistica")
    r = _create_migration(admin_client, model_id, down_sql="DROP TABLE users")
    data = r.json()["data"]
    assert data["down_sql"] == "DROP TABLE users"


def test_create_non_additive_has_no_suggested_rollback(admin_client):
    model_id = _new_model(admin_client, slug="ventas", name="Ventas")
    r = _create_migration(
        admin_client, model_id,
        up_sql="INSERT INTO config (k, v) VALUES ('a', 'b')",
    )
    assert r.json()["data"]["down_sql_suggested"] is None


def test_duplicate_version_conflict(admin_client):
    model_id = _new_model(admin_client, slug="dup", name="Dup")
    assert _create_migration(admin_client, model_id).status_code == 201
    assert _create_migration(admin_client, model_id).status_code == 409


def test_invalid_version_pattern_422(admin_client):
    model_id = _new_model(admin_client, slug="badver", name="BadVer")
    assert _create_migration(admin_client, model_id, version="1.2.0").status_code == 422
    assert _create_migration(admin_client, model_id, version="abc").status_code == 422


def test_create_on_missing_model_404(admin_client):
    assert _create_migration(admin_client, 9999).status_code == 404


def test_up_sql_too_large_422(admin_client):
    model_id = _new_model(admin_client, slug="toobig", name="TooBig")
    huge = "SELECT 1; " + ("x" * 262_200)
    assert _create_migration(admin_client, model_id, up_sql=huge).status_code == 422


def test_version_ordering_is_numeric(admin_client):
    """Regresión P3: list y current_version ordenan numéricamente, no lexicográfico."""
    model_id = _new_model(admin_client, slug="numorder", name="NumOrder")
    _create_migration(admin_client, model_id, version="0009")
    _create_migration(admin_client, model_id, version="00010",
                      up_sql="ALTER TABLE users ADD COLUMN c INT")
    items = admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations"
    ).json()["data"]
    assert [i["version"] for i in items] == ["0009", "00010"]  # 9 < 10
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "00010"  # max numérico, no "0009"


# --------------------------------------------------------------------------- #
# List / Get                                                                   #
# --------------------------------------------------------------------------- #
def test_list_and_get(admin_client):
    model_id = _new_model(admin_client, slug="listme", name="ListMe")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(admin_client, model_id, version="0002",
                      up_sql="ALTER TABLE users ADD COLUMN x INT")

    lst = admin_client.get(f"/api/v1/database-models/{model_id}/migrations")
    assert lst.status_code == 200
    items = lst.json()["data"]
    assert [i["version"] for i in items] == ["0001", "0002"]
    assert items[0]["has_rollback"] is False

    detail = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert detail.status_code == 200
    assert detail.json()["data"]["up_sql"].startswith("ALTER TABLE")


def test_get_missing_version_404(admin_client):
    model_id = _new_model(admin_client, slug="missing", name="Missing")
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0009"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Patch                                                                        #
# --------------------------------------------------------------------------- #
def test_patch_confirms_rollback(admin_client):
    model_id = _new_model(admin_client, slug="patchrb", name="PatchRb")
    _create_migration(admin_client, model_id)
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"down_sql": "DROP TABLE IF EXISTS users"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["down_sql"] == "DROP TABLE IF EXISTS users"


def test_patch_override_changes_checksum_and_translation(admin_client):
    model_id = _new_model(admin_client, slug="override", name="Override")
    before = _create_migration(admin_client, model_id).json()["data"]
    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"up_sql_postgresql": "CREATE TABLE users (id SERIAL PRIMARY KEY)"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["checksum"] != before["checksum"]
    assert data["translated"]["postgresql"] == "CREATE TABLE users (id SERIAL PRIMARY KEY)"


def test_checksum_covers_down_sql(admin_client):
    """Confirmar el down_sql cambia el checksum (integridad cubre el rollback)."""
    model_id = _new_model(admin_client, slug="cksdown", name="CksDown")
    before = _create_migration(admin_client, model_id).json()["data"]
    assert before["down_sql"] is None
    after = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001",
        json={"down_sql": "DROP TABLE IF EXISTS users"},
    ).json()["data"]
    assert after["down_sql"] == "DROP TABLE IF EXISTS users"
    assert after["checksum"] != before["checksum"]


# --------------------------------------------------------------------------- #
# Delete                                                                       #
# --------------------------------------------------------------------------- #
def test_delete_migration(admin_client):
    model_id = _new_model(admin_client, slug="delme", name="DelMe")
    _create_migration(admin_client, model_id)
    assert admin_client.delete(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 200
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 404


def _insert_history_row(managed_db_id, migration_id, status="applied", db_version=None):
    """Inserta una fila de historial directamente (sin apply real end-to-end).

    ``db_version`` fija además la versión CACHEADA de la BD gestionada
    (``ManagedDatabase.model_version``), y hay que pasarla siempre que el test quiera
    representar "esta BD depende de la versión". El historial por sí solo ya no alcanza: es
    un log de EVENTOS que nunca se revoca —``_record_history`` escribe ``applied`` tanto en
    el ``apply`` como en el ``rollback``, y no hay columna ``direction``—, así que lo que
    congela una versión es la versión ACTUAL de la BD, no que alguna vez haya corrido.

    Sin ``db_version`` la BD queda como la deja el alta por API (``model_version = NULL``),
    o sea "está en la base y no tiene ninguna versión aplicada": ese es exactamente el
    estado de una BD que revirtió todo, y el ``apply`` real nunca lo combina con historial
    ``applied`` vigente.
    """
    from datetime import datetime

    from app.core.database import Database
    from app.models.database_migration_history import DatabaseMigrationHistory
    from app.models.enums import MigrationStatus
    from app.models.managed_database import ManagedDatabase

    s = Database().get_declarative_base_session()
    try:
        s.add(
            DatabaseMigrationHistory(
                managed_database_id=managed_db_id,
                model_migration_id=migration_id,
                applied_at=datetime.now(),
                status=MigrationStatus(status),
                error=None if status == "applied" else "boom",
                execution_ms=1,
            )
        )
        if db_version is not None:
            s.get(ManagedDatabase, managed_db_id).model_version = db_version
        s.commit()
    finally:
        s.close()


#: Centinela para ``_engine_version``: el motor no se puede leer (caído, base sin
#: aprovisionar, credenciales rotas).
_ENGINE_UNREADABLE = object()


@contextmanager
def _engine_version(version):
    """Doble del motor para el guard AUTORITATIVO de edición/borrado de versiones.

    ``update_migration`` y ``delete_migration`` no deciden con la caché del inventario:
    leen la versión ACTUAL de cada BD con ``MigrationRunner.get_current_version``, porque
    están por autorizar algo irreversible. Los fixtures registran servidores con hosts
    inventados, así que sin este doble toda conexión falla y el fail-closed responde 409 con
    ``reason='unreadable'``: el test pasaría, pero por un motivo distinto del que dice
    probar.

    ``version`` es lo que el motor reporta para CUALQUIER BD del bloque; el centinela
    ``_ENGINE_UNREADABLE`` simula un motor ilegible, y su excepción lleva credenciales a
    propósito para poder verificar que no se filtran al ``message`` del 409 (criterio R4).

    Se parchean atributos del MÓDULO del controller —no de las clases originales— porque es
    ahí donde se resuelven en tiempo de llamada, y se restauran en ``finally`` para no
    contaminar el resto de la suite.
    """
    import app.controllers.model_migration_controller as mod

    class _Runner:
        def get_current_version(self, target, db_name, slug):
            if version is _ENGINE_UNREADABLE:
                raise RuntimeError(
                    "(2003, \"Can't connect to server on '10.0.0.9:5480' (user=root "
                    "password=rootpw)\")"
                )
            return version

    originales = (mod.MigrationRunner, mod.build_target, mod.get_server_or_404)
    mod.MigrationRunner = _Runner
    mod.build_target = lambda server: object()
    mod.get_server_or_404 = lambda session, server_id: object()
    try:
        yield
    finally:
        mod.MigrationRunner, mod.build_target, mod.get_server_or_404 = originales


def _managed_db_for_model(admin_client, model_id, port, name="mdb"):
    sid = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}", "host": "10.0.0.9", "port": port,
            "engine": "postgresql", "root_username": "root", "root_password": "rootpw",
        },
    ).json()["data"]["id"]
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner1"}
    ).json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": name, "model_id": model_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_delete_migration_applied_successfully_409(admin_client):
    """Borrar una versión que alguna BD tiene aplicada HOY → 409, y la versión sobrevive.

    El fixture fija ``model_version='0001'``: la BD está parada en esa versión, o sea que
    depende de ella. Con el historial solo no alcanzaría —una fila ``applied`` la escribe
    igual el ``rollback``—, y por eso el guard autoritativo confirma leyendo el motor.
    """
    model_id = _new_model(admin_client, slug="delhist", name="DelHist")
    r = _create_migration(admin_client, model_id)
    mig_id = r.json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5480)
    _insert_history_row(db_id, mig_id, status="applied", db_version="0001")

    with _engine_version("0001"):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "model_migration.version_in_use"
    assert pc["version"] == "0001"
    assert pc["blocking_databases"] == [
        {
            "managed_database_id": db_id,
            "reason": "in_use",
            "current_version": "0001",
        }
    ]

    # Sigue existiendo.
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 200


def test_delete_migration_allowed_when_history_is_only_failed(admin_client):
    """
    Mismo criterio que la EDICIÓN: lo que congela una versión es que alguna BD dependa de
    ella, no que se haya intentado aplicar.

    Antes, el guard miraba "¿tiene alguna fila de historial?" y una versión que reventó en
    la primera sentencia —sin tocar ninguna BD— quedaba imborrable para siempre, porque no
    existe purga de historial. La única salida era editarla y dejarla inerte.
    """
    model_id = _new_model(admin_client, slug="delfail", name="DelFail")
    r = _create_migration(admin_client, model_id)
    mig_id = r.json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5481)
    _insert_history_row(db_id, mig_id, status="failed")

    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 200, r.text
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 404


def test_delete_migration_blocked_by_partial_application_409(admin_client):
    """
    Un intento fallido puede haber dejado sentencias commiteadas a medias. El checkpoint es
    la única evidencia de cuáles, y el CASCADE se lo llevaría en silencio: sin ese guard, la
    BD queda sucia y sin forma de reconciliarla.
    """
    from app.services.db_admin import migration_progress

    model_id = _new_model(admin_client, slug="delpartial", name="DelPartial")
    r = _create_migration(admin_client, model_id)
    mig = r.json()["data"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5482)
    _insert_history_row(db_id, mig["id"], status="failed")
    # 2 de 5 sentencias commitearon: progreso INCOMPLETO.
    migration_progress.record_statement(db_id, mig["id"], "up", 2, 5, mig["checksum"])

    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 409, r.text
    assert "parcial" in r.text.lower()
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 200


def test_delete_last_version_recomputes_current_version(admin_client):
    model_id = _new_model(admin_client, slug="deltip", name="DelTip")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(
        admin_client, model_id, version="0002",
        up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    )
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0002"

    # Borrar la PUNTA (0002, sin historial) → permitido; current_version retrocede.
    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 200, r.text

    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0001"

    # Borrar la única migración restante → current_version cae a "0.0.0".
    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 200, r.text
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0.0.0"


def test_delete_intermediate_version_renumera(admin_client):
    """Borrar una intermedia dejó de ser 409: ahora se renumera lo que sigue.

    Reemplaza a ``test_delete_intermediate_version_409``. La regla "solo la punta" existía
    porque un hueco en la cadena parecía irreconstruible; no lo es —``_write_revision_files``
    encadena en un tempdir los specs que HAY, ordenados numéricamente—, y lo que sí importa
    es que ninguna BD quede apuntando a una etiqueta que dejó de existir. Sin BDs no hay
    puntero que mover, así que esto es una operación puramente local.

    El detalle fino del renumerado con BDs vive en
    ``tests/test_api_migration_delete_renumber.py``.
    """
    model_id = _new_model(admin_client, slug="delmid", name="DelMid")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(
        admin_client, model_id, version="0002",
        up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    )

    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["renumbered"] == [
        {"from_version": "0002", "to_version": "0001"}
    ]

    # La 0002 bajó a 0001 llevándose su SQL; la punta del blueprint la sigue.
    d = admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).json()["data"]
    assert "phone" in d["up_sql"]
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0002"
    ).status_code == 404
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0001"


# --------------------------------------------------------------------------- #
# Historial (lectura, solo BD del gateway)                                     #
# --------------------------------------------------------------------------- #
def _server(admin_client, port, **ov) -> int:
    payload = {
        "name": f"srv{port}", "host": "10.0.0.9", "port": port,
        "engine": "postgresql", "root_username": "root", "root_password": "rootpw",
    }
    payload.update(ov)
    return admin_client.post("/api/v1/servers", json=payload).json()["data"]["id"]


def _owner(admin_client, server_id, username="owner1") -> int:
    return admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    ).json()["data"]["id"]


def test_history_empty_for_fresh_db(admin_client):
    sid = _server(admin_client, 5470)
    oid = _owner(admin_client, sid, "histowner")
    db_id = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": "hist_db"},
    ).json()["data"]["id"]
    r = admin_client.get(f"/api/v1/managed-databases/{db_id}/migrations/history")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_history_missing_db_404(admin_client):
    assert admin_client.get(
        "/api/v1/managed-databases/9999/migrations/history"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Captura de resultados: la aprobación es de una CONSULTA, no de la versión     #
# --------------------------------------------------------------------------- #
def _capture_migration(admin_client, model_id, up_sql="SELECT 1", version="0001"):
    r = _create_migration(
        admin_client, model_id, version=version, up_sql=up_sql, capture_selects=True
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    # Nace SIN revisar: es la primera de las tres llaves de la feature.
    assert data["capture_selects"] is True
    assert data["reviewed"] is False
    return data


def _patch(admin_client, model_id, version, payload):
    return admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/{version}", json=payload
    )


def test_capture_reviewed_se_revoca_al_cambiar_el_up_sql(admin_client):
    """
    El escenario que abría el agujero: aprobar ``SELECT 1`` y después reescribir el ``up_sql``
    a ``SELECT * FROM clientes``. Está permitido editar (no hubo aplicación exitosa), así que
    sin este reset el ``apply`` pasaba el gate de ``reviewed`` sobre una consulta que NADIE
    revisó.
    """
    model_id = _new_model(admin_client, slug="cap1", name="Cap1")
    _capture_migration(admin_client, model_id)

    assert _patch(admin_client, model_id, "0001", {"reviewed": True}).json()["data"][
        "reviewed"
    ] is True

    r = _patch(admin_client, model_id, "0001", {"up_sql": "SELECT * FROM clientes"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reviewed"] is False


def test_capture_reviewed_se_revoca_al_cambiar_el_down_sql(admin_client):
    """El ``down_sql`` también se ejecuta y también captura (el rollback no es un no-op)."""
    model_id = _new_model(admin_client, slug="cap2", name="Cap2")
    _capture_migration(admin_client, model_id)
    _patch(admin_client, model_id, "0001", {"reviewed": True})

    r = _patch(admin_client, model_id, "0001", {"down_sql": "SELECT * FROM clientes"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reviewed"] is False


def test_capture_reviewed_en_la_misma_llamada_que_el_sql_no_aprueba(admin_client):
    """
    Aprobar y reescribir en un solo request no es una revisión verificable: gana el reset,
    mismo criterio que ya se aplicaba al ACTIVAR ``capture_selects``.
    """
    model_id = _new_model(admin_client, slug="cap3", name="Cap3")
    _capture_migration(admin_client, model_id)

    r = _patch(
        admin_client,
        model_id,
        "0001",
        {"reviewed": True, "up_sql": "SELECT * FROM clientes"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reviewed"] is False


def test_sin_captura_el_cambio_de_sql_no_toca_reviewed(admin_client):
    """
    Regresión importante: el reset es SOLO para versiones con captura. Una versión normal
    (o un baseline de snapshot ya aprobado) no puede perder su aprobación por un PATCH de SQL.
    """
    model_id = _new_model(admin_client, slug="cap4", name="Cap4")
    _create_migration(admin_client, model_id, version="0001")
    _patch(admin_client, model_id, "0001", {"reviewed": True})

    r = _patch(
        admin_client, model_id, "0001", {"up_sql": "CREATE TABLE t (id INT PRIMARY KEY)"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reviewed"] is True


# --------------------------------------------------------------------------- #
# Banderas de política: sql_frozen / deletable / block_reason                  #
# --------------------------------------------------------------------------- #
def test_policy_flags_version_limpia(admin_client):
    """Una versión recién creada (y punta) es editable y borrable."""
    model_id = _new_model(admin_client, slug="pol1", name="Pol1")
    _create_migration(admin_client, model_id)

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert (d["sql_frozen"], d["deletable"], d["block_reason"]) == (False, True, None)

    item = admin_client.get(f"/api/v1/database-models/{model_id}/migrations").json()["data"][0]
    assert (item["sql_frozen"], item["deletable"], item["block_reason"]) == (False, True, None)


def test_policy_flags_aplicada_congela_el_sql(admin_client):
    """Una BD parada EN la versión la congela. Las banderas salen de la caché, sin motor."""
    model_id = _new_model(admin_client, slug="pol2", name="Pol2")
    mig_id = _create_migration(admin_client, model_id).json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5490)
    _insert_history_row(db_id, mig_id, status="applied", db_version="0001")

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert d["sql_frozen"] is True
    assert d["deletable"] is False
    # 'in_use' y no 'applied': ``block_reason`` describe el BORRADO, cuyo criterio es la
    # IGUALDAD. Para la edición está ``sql_frozen``, que sigue con criterio ``>=``.
    assert d["block_reason"] == "in_use"


def test_policy_flags_solo_fallida_sigue_editable_y_borrable(admin_client):
    """El caso del incidente: falló en todas las BDs, pero nada depende de ella."""
    model_id = _new_model(admin_client, slug="pol3", name="Pol3")
    mig_id = _create_migration(admin_client, model_id).json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5491)
    _insert_history_row(db_id, mig_id, status="failed")

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert (d["sql_frozen"], d["deletable"], d["block_reason"]) == (False, True, None)


def test_policy_flags_no_ser_punta_ya_no_bloquea_el_borrado(admin_client):
    """`not_tip` salió del vocabulario: sin BDs, una intermedia es tan borrable como la punta.

    Reemplaza a ``test_policy_flags_no_punta_bloquea_borrado_pero_no_edicion``. Es el
    contrato que la UI lee para habilitar el botón, así que el valor viejo no puede quedar
    ni como sinónimo: un cliente que lo siga esperando debe romper de forma visible.
    """
    model_id = _new_model(admin_client, slug="pol4", name="Pol4")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(
        admin_client, model_id, version="0002",
        up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    )

    items = admin_client.get(f"/api/v1/database-models/{model_id}/migrations").json()["data"]
    first, tip = items[0], items[1]
    assert (first["sql_frozen"], first["deletable"], first["block_reason"]) == (
        False, True, None,
    )
    assert (tip["sql_frozen"], tip["deletable"], tip["block_reason"]) == (False, True, None)
    assert all(it["block_reason"] != "not_tip" for it in items)
    # Sin BDs adelante no hay punteros que mover: el borrado no pedirá confirmación.
    assert all(it["delete_requires_stamps"] is False for it in items)


def test_policy_flags_aplicacion_parcial_congela_y_bloquea(admin_client):
    from app.services.db_admin import migration_progress

    model_id = _new_model(admin_client, slug="pol5", name="Pol5")
    mig = _create_migration(admin_client, model_id).json()["data"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5492)
    migration_progress.record_statement(db_id, mig["id"], "up", 2, 5, mig["checksum"])

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert d["sql_frozen"] is True
    assert d["deletable"] is False
    assert d["block_reason"] == "partial"


def test_policy_flags_patch_de_solo_nombre_no_miente(admin_client):
    """
    Regresión del diseño: si el serializador pudiera omitir las banderas, un PATCH de solo el
    nombre sobre una versión congelada respondería `sql_frozen: false` y la UI la creería
    editable — justo el fallo que estos campos vienen a eliminar.
    """
    model_id = _new_model(admin_client, slug="pol6", name="Pol6")
    mig_id = _create_migration(admin_client, model_id).json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5493)
    _insert_history_row(db_id, mig_id, status="applied", db_version="0001")

    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001", json={"name": "otro nombre"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sql_frozen"] is True

# --------------------------------------------------------------------------- #
# Vigencia de una versión: lo que congela es la versión ACTUAL, no el historial #
# --------------------------------------------------------------------------- #
def _blueprint_con_tres_versiones(admin_client, slug, port, db_version):
    """Blueprint 0001/0002/0003 + una BD que las corrió TODAS y hoy está en `db_version`.

    Es el escenario del incidente: el historial afirma que las tres se aplicaron con éxito
    (y eso nunca se revoca), pero la BD puede haber revertido después.
    """
    model_id = _new_model(admin_client, slug=slug, name=slug)
    ids = []
    for v in ("0001", "0002", "0003"):
        r = _create_migration(
            admin_client, model_id, version=v, up_sql=f"CREATE TABLE t{v} (id INT PRIMARY KEY)"
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["data"]["id"])
    db_id = _managed_db_for_model(admin_client, model_id, port=port, name=f"db{port}")
    for i, mig_id in enumerate(ids):
        # La versión de la BD se fija una sola vez, con la última fila de historial.
        _insert_history_row(
            db_id,
            mig_id,
            status="applied",
            db_version=db_version if i == len(ids) - 1 else None,
        )
    return model_id, ids, db_id


def test_version_revertida_vuelve_a_ser_editable_y_borrable(admin_client):
    """El caso que motivó el cambio: revertir en todas las BDs DESCONGELA la versión.

    `database_migration_history` es un log de EVENTOS. `_record_history` escribe
    `status='applied'` tanto desde el `apply` como desde el `rollback`, y ni la tabla ni
    `MigrationResult` tienen columna `direction`: esa fila no se revoca nunca. Con el guard
    viejo —"¿existe alguna fila applied?"— una versión revertida correctamente quedaba
    congelada de por vida, sin ninguna salida: no hay purga de historial, el `DELETE` no
    acepta `force`, y el `CASCADE` que borraría esas filas cuelga del borrado de la
    migración, que es justo lo que el historial bloquea. La única salida era fix-forward.

    Con la BD parada en 0002, la 0003 ya no describe nada del motor: se puede editar y
    borrar.
    """
    model_id, _ids, _db_id = _blueprint_con_tres_versiones(
        admin_client, slug="revert", port=5494, db_version="0002"
    )

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0003").json()["data"]
    assert (d["sql_frozen"], d["deletable"], d["block_reason"]) == (False, True, None)

    with _engine_version("0002"):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0003")
    assert r.status_code == 200, r.text
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0003"
    ).status_code == 404


def test_version_vigente_sigue_congelada(admin_client):
    """Mismo historial, pero la BD SIGUE en 0003: la versión describe lo que hay en el motor.

    Es la mitad que no se puede aflojar. Borrarla no revierte nada: dejaría esa BD con
    objetos que ninguna versión del blueprint describe.
    """
    model_id, _ids, db_id = _blueprint_con_tres_versiones(
        admin_client, slug="vigente", port=5495, db_version="0003"
    )

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0003").json()["data"]
    assert (d["sql_frozen"], d["deletable"], d["block_reason"]) == (True, False, "in_use")

    with _engine_version("0003"):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0003")
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "model_migration.version_in_use"
    assert pc["blocking_databases"] == [
        {
            "managed_database_id": db_id,
            "reason": "in_use",
            "current_version": "0003",
        }
    ]


def test_version_posterior_tambien_congela(admin_client):
    """Las migraciones son forward-only encadenadas: estar en 0003 implica tener la 0001.

    Por eso el criterio es ">= la versión", no "== la versión". Si fuera igualdad, una BD al
    día dejaría borrar todas las versiones intermedias que sí describen su esquema.
    """
    model_id, _ids, _db_id = _blueprint_con_tres_versiones(
        admin_client, slug="posterior", port=5496, db_version="0003"
    )

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert d["sql_frozen"] is True
    # Pero el BORRADO sí distingue: la BD está en 0003, no parada en 0001, así que el
    # borrado le movería el puntero en vez de bloquearse. Los dos criterios divergen a
    # propósito y por eso 'block_reason' ya no repite lo que dice 'sql_frozen'.
    assert d["block_reason"] is None
    assert d["deletable"] is True
    assert d["delete_requires_stamps"] is True

    with _engine_version("0003"):
        r = _patch(admin_client, model_id, "0001", {"up_sql": "CREATE TABLE otra (id INT)"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "model_migration.sql_frozen"


def test_motor_ilegible_es_fail_closed(admin_client):
    """Un motor que no responde NO es permiso para borrar: 409 con `reason='unreadable'`.

    Nótese que la CACHÉ dice que la BD está en 0002 (la versión 0003 se ve borrable en el
    listado), y aun así la mutación se rechaza: el veredicto autoritativo es el del motor, y
    cuando no se puede leer se falla cerrado. Tratar la caída como "ya no la tiene"
    convertiría un corte de red en autorización para destruir metadata.
    """
    model_id, _ids, db_id = _blueprint_con_tres_versiones(
        admin_client, slug="ilegible", port=5497, db_version="0002"
    )

    with _engine_version(_ENGINE_UNREADABLE):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0003")
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "model_migration.unreadable_databases"
    assert pc["blocking_databases"] == [
        {"managed_database_id": db_id, "reason": "unreadable"}
    ]
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0003"
    ).status_code == 200


def test_el_409_no_filtra_el_mensaje_del_motor(admin_client):
    """Criterio R4: el error del motor puede llevar host, usuario o contraseña.

    El `message` y el `public_context` solo pueden nombrar el id de la BD y un motivo del
    vocabulario cerrado; el detalle va al log correlacionado por Request ID.
    """
    model_id, _ids, _db_id = _blueprint_con_tres_versiones(
        admin_client, slug="r4", port=5498, db_version="0002"
    )

    with _engine_version(_ENGINE_UNREADABLE):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0003")
    assert r.status_code == 409, r.text
    cuerpo = r.text.lower()
    for filtracion in ("rootpw", "10.0.0.9", "2003", "can't connect"):
        assert filtracion not in cuerpo, f"se filtró {filtracion!r}: {r.text}"


def test_version_cacheada_ilegible_sigue_congelada(admin_client):
    """`model_version` no numérico ⇒ fail-closed: la versión sigue congelada.

    `version_sort_key` hace `int(version)` y nada garantiza que la caché sea numérica: se
    escribe releyendo el motor, y un `stamp` a mano puede dejar cualquier cosa. Ante un
    valor que no se puede ordenar, la respuesta segura es "sí la alcanza".
    """
    model_id, ids, db_id = _blueprint_con_tres_versiones(
        admin_client, slug="rara", port=5499, db_version="v3-hotfix"
    )
    assert ids  # el blueprint quedó armado

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0003").json()["data"]
    assert d["sql_frozen"] is True
    assert d["deletable"] is False

    with _engine_version("v3-hotfix"):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0003")
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    # Para el BORRADO un puntero no ordenable no se puede ubicar en la secuencia: no se
    # puede decidir si está parada acá ni calcularle un destino de renumerado, así que cae
    # en 'unreadable' (mismo fail-closed, motivo más preciso).
    assert pc["code"] == "model_migration.unreadable_databases"
    assert pc["blocking_databases"] == [
        {"managed_database_id": db_id, "reason": "unreadable"}
    ]


def test_solo_historial_fallido_no_lee_el_motor(admin_client):
    """Anti-regresión: el historial sigue siendo el PRIMER filtro de la EDICIÓN, y es barato.

    Una versión que solo falló no tiene ninguna fila `applied`, así que el PATCH ni llega a
    abrir conexión: si se llegara, fallaría con 409 `unreadable` en vez de 200. Es lo que
    evita que editar una versión nunca aplicada dependa de que el motor esté vivo.

    El BORRADO ya no comparte ese filtro, y no es un descuido. Su criterio pasó a ser la
    POSICIÓN de cada BD, no el resultado de un intento: acá la caché dice que la BD está
    parada en 0001, que es justo la versión que se quiere borrar, así que hay que leer el
    motor para decidir — y con el motor mudo la respuesta segura es negarse. Con el filtro
    viejo esta versión se borraba porque su historial decía "falló", sin mirar nunca dónde
    está parada la BD.

    Lo que el borrado SÍ conserva del filtro barato es el caso limpio: una BD que nunca fue
    posicionada (sin caché y sin historial exitoso) no obliga a abrir conexión. Eso lo fija
    ``test_una_bd_nunca_posicionada_no_obliga_a_leer_el_motor``.
    """
    model_id = _new_model(admin_client, slug="solofail", name="SoloFail")
    mig_id = _create_migration(admin_client, model_id).json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5500)
    _insert_history_row(db_id, mig_id, status="failed", db_version="0001")

    with _engine_version(_ENGINE_UNREADABLE):
        r = _patch(admin_client, model_id, "0001", {"up_sql": "CREATE TABLE ok (id INT)"})
        assert r.status_code == 200, r.text
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 409, r.text
    assert (
        r.json()["detail"]["public_context"]["code"]
        == "model_migration.unreadable_databases"
    )


# --------------------------------------------------------------------------- #
# apply-all: on_failure                                                        #
# --------------------------------------------------------------------------- #
def test_apply_all_valida_on_failure(admin_client):
    """
    Regresión: la ruta de `apply-all` NO exponía `on_failure`, así que el controlador
    recibía siempre el default "auto" y el selector del frontend no hacía nada. Ahora el
    parámetro llega y se valida contra la MISMA lista que el apply por BD.
    """
    model_id = _new_model(admin_client, slug="onfail", name="OnFail")
    _create_migration(admin_client, model_id)

    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/apply-all",
        params={"dry_run": True, "on_failure": "no-existe"},
    )
    assert r.status_code == 422, r.text
    assert "on_failure" in r.text

    # Los tres modos válidos pasan el guard (sin BDs asociadas el resultado es vacío).
    for mode in ("auto", "reconcile", "leave"):
        r = admin_client.post(
            f"/api/v1/database-models/{model_id}/migrations/apply-all",
            params={"dry_run": True, "on_failure": mode},
        )
        assert r.status_code != 422, f"{mode}: {r.text}"


# --------------------------------------------------------------------------- #
# Validación estática del SQL                                                  #
# --------------------------------------------------------------------------- #
def test_validate_detecta_sql_roto(admin_client):
    model_id = _new_model(admin_client, slug="val1", name="Val1")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate",
        json={"up_sql": "CREATE TABLE ((("},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["parse_errors"], data
    assert data["parse_errors"][0]["message"]


def test_validate_marca_siembra_y_collate(admin_client):
    model_id = _new_model(admin_client, slug="val2", name="Val2")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate",
        json={
            "up_sql": (
                "ALTER TABLE t MODIFY c VARCHAR(10) COLLATE utf8mb4_bin; "
                "INSERT INTO t (c) VALUES ('x');"
            )
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_seed"] is True
    assert data["forced_collations"] == ["utf8mb4_bin"]


def test_validate_acepta_una_version_ya_guardada(admin_client):
    model_id = _new_model(admin_client, slug="val3", name="Val3")
    _create_migration(admin_client, model_id)
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate",
        json={"version": "0001"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["statements"]


def test_validate_sin_sql_ni_version_422(admin_client):
    model_id = _new_model(admin_client, slug="val4", name="Val4")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate", json={}
    )
    assert r.status_code == 422, r.text


def test_validate_rechaza_una_bd_de_otro_blueprint(admin_client, server_payload):
    """
    Frontera de autorización, no cosmética: sin esta comprobación se podría sondear el
    catálogo de CUALQUIER BD del gateway pasando su id a un blueprint que no la contiene.
    """
    otro = _new_model(admin_client, slug="val5-otro", name="Val5Otro")
    db_id = _managed_db_for_model(admin_client, otro, port=5494)
    objetivo = _new_model(admin_client, slug="val5", name="Val5")

    r = admin_client.post(
        f"/api/v1/database-models/{objetivo}/migrations/validate",
        json={"up_sql": "SELECT 1", "managed_database_id": db_id},
    )
    assert r.status_code == 422, r.text
    assert "no pertenece" in r.text.lower()


def test_validate_avisa_del_collate_que_difiere_del_blueprint(admin_client):
    r = admin_client.post(
        "/api/v1/database-models",
        json={
            "name": "Val6", "slug": "val6",
            "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci",
        },
    )
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    assert r.json()["data"]["collation"] == "utf8mb4_unicode_ci"

    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate",
        json={"up_sql": "ALTER TABLE t MODIFY c VARCHAR(10) COLLATE utf8mb4_bin"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["blueprint_collation"] == "utf8mb4_unicode_ci"
    assert data["collation_conflicts"] == ["utf8mb4_bin"]


def test_validate_no_marca_conflicto_si_coincide(admin_client):
    r = admin_client.post(
        "/api/v1/database-models",
        json={"name": "Val7", "slug": "val7", "collation": "utf8mb4_bin"},
    )
    model_id = r.json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate",
        json={"up_sql": "ALTER TABLE t MODIFY c VARCHAR(10) COLLATE UTF8MB4_BIN"},
    )
    # La comparación es insensible a mayúsculas: los collations no distinguen caja.
    assert r.json()["data"]["collation_conflicts"] == []


# --------------------------------------------------------------------------- #
# Estado por BD y destinos concretos                                           #
# --------------------------------------------------------------------------- #
def test_databases_trae_el_estado_de_despliegue(admin_client):
    """
    Antes había que pedir `/migrations/status` por cada BD, y cada una abría conexión al
    motor. Ahora la tabla entera sale de este listado, con datos locales.
    """
    model_id = _new_model(admin_client, slug="est1", name="Est1")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(
        admin_client, model_id, version="0002",
        up_sql="ALTER TABLE users ADD COLUMN x INT",
    )
    _managed_db_for_model(admin_client, model_id, port=5495)

    r = admin_client.get(f"/api/v1/database-models/{model_id}/databases")
    assert r.status_code == 200, r.text
    item = r.json()["data"][0]
    # BD recién registrada: sin versión aplicada ⇒ todas pendientes.
    assert item["pending_count"] == 2
    assert item["pending_versions"] == ["0001", "0002"]
    assert item["has_partial_application"] is False


def test_databases_marca_la_aplicacion_parcial(admin_client):
    from app.services.db_admin import migration_progress

    model_id = _new_model(admin_client, slug="est2", name="Est2")
    mig = _create_migration(admin_client, model_id).json()["data"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5496)
    migration_progress.record_statement(db_id, mig["id"], "up", 2, 5, mig["checksum"])

    r = admin_client.get(f"/api/v1/database-models/{model_id}/databases")
    assert r.json()["data"][0]["has_partial_application"] is True


def test_apply_all_rechaza_una_bd_de_otro_blueprint(admin_client):
    """
    Frontera de autorización: `IN` ignoraría el id ajeno en silencio y el lote seguiría. El
    422 con la lista es lo que impide aplicar las migraciones de un blueprint a una BD ajena.
    """
    otro = _new_model(admin_client, slug="dest-otro", name="DestOtro")
    ajena = _managed_db_for_model(admin_client, otro, port=5497)
    model_id = _new_model(admin_client, slug="dest", name="Dest")
    _create_migration(admin_client, model_id)

    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/apply-all",
        params={"dry_run": True, "database_ids": [ajena]},
    )
    assert r.status_code == 422, r.text
    assert str(ajena) in r.text


def test_validate_no_reporta_como_ausentes_las_tablas_que_la_migracion_crea(admin_client):
    """
    Regresión de la auditoría: un baseline es todo `CREATE TABLE`, y el panel mostraba un muro
    de «estas tablas no existen».
    """
    model_id = _new_model(admin_client, slug="val8", name="Val8")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate",
        json={"up_sql": "CREATE TABLE nueva (id INT PRIMARY KEY); ALTER TABLE nueva ADD c INT;"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["referenced_tables"] == []


def test_validate_por_version_usa_el_kind_real(admin_client):
    """
    `resumable` se calculaba con los defaults, así que una migración de datos se anunciaba como
    reanudable cuando nunca lo es.
    """
    model_id = _new_model(admin_client, slug="val9", name="Val9")
    _create_migration(admin_client, model_id, up_sql="INSERT INTO t (a) VALUES (1)")
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/validate", json={"version": "0001"}
    )
    assert r.status_code == 200, r.text
    # La migración escrita a mano nace `kind='schema'`; lo que se comprueba es que el campo
    # viaja y es coherente con el SQL, no un default ciego.
    assert "resumable" in r.json()["data"]


def test_databases_no_tiene_rate_limit_propio(admin_client):
    """
    El límite cubría también el listado local —el 99 % de las llamadas, sin conexiones— y con
    el refetch al reenfocar la ventana podía romper la pantalla con un 429. El refresco 🔌 vive
    ahora en su propia acción POST.
    """
    model_id = _new_model(admin_client, slug="nolimit", name="NoLimit")
    for _ in range(15):
        assert (
            admin_client.get(f"/api/v1/database-models/{model_id}/databases").status_code == 200
        )
