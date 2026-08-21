"""Endpoints de migraciones de blueprints: CRUD, checksum, traducción, rollback sugerido."""


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


def _insert_history_row(managed_db_id, migration_id, status="applied"):
    """Inserta una fila de historial directamente (sin apply real end-to-end)."""
    from datetime import datetime

    from app.core.database import Database
    from app.models.database_migration_history import DatabaseMigrationHistory
    from app.models.enums import MigrationStatus

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
        s.commit()
    finally:
        s.close()


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
    """Borrar una migración ya aplicada CON ÉXITO en alguna BD → 409 (alguna BD depende de ella)."""
    model_id = _new_model(admin_client, slug="delhist", name="DelHist")
    r = _create_migration(admin_client, model_id)
    mig_id = r.json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5480)
    _insert_history_row(db_id, mig_id, status="applied")

    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 409, r.text
    assert "aplicada con éxito" in r.text.lower()

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


def test_delete_intermediate_version_409(admin_client):
    model_id = _new_model(admin_client, slug="delmid", name="DelMid")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(
        admin_client, model_id, version="0002",
        up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    )

    # 0001 es intermedia (existe 0002 posterior) → 409.
    r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0001")
    assert r.status_code == 409, r.text
    assert "última versión" in r.text.lower() or "solo se puede eliminar" in r.text.lower()

    # No se tocó nada.
    assert admin_client.get(
        f"/api/v1/database-models/{model_id}/migrations/0001"
    ).status_code == 200
    m = admin_client.get(f"/api/v1/database-models/{model_id}").json()["data"]
    assert m["current_version"] == "0002"


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
    model_id = _new_model(admin_client, slug="pol2", name="Pol2")
    mig_id = _create_migration(admin_client, model_id).json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5490)
    _insert_history_row(db_id, mig_id, status="applied")

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert d["sql_frozen"] is True
    assert d["deletable"] is False
    assert d["block_reason"] == "applied"


def test_policy_flags_solo_fallida_sigue_editable_y_borrable(admin_client):
    """El caso del incidente: falló en todas las BDs, pero nada depende de ella."""
    model_id = _new_model(admin_client, slug="pol3", name="Pol3")
    mig_id = _create_migration(admin_client, model_id).json()["data"]["id"]
    db_id = _managed_db_for_model(admin_client, model_id, port=5491)
    _insert_history_row(db_id, mig_id, status="failed")

    d = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert (d["sql_frozen"], d["deletable"], d["block_reason"]) == (False, True, None)


def test_policy_flags_no_punta_bloquea_borrado_pero_no_edicion(admin_client):
    """`not_tip` es la única razón que NO congela el SQL."""
    model_id = _new_model(admin_client, slug="pol4", name="Pol4")
    _create_migration(admin_client, model_id, version="0001")
    _create_migration(
        admin_client, model_id, version="0002",
        up_sql="ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    )

    items = admin_client.get(f"/api/v1/database-models/{model_id}/migrations").json()["data"]
    first, tip = items[0], items[1]
    assert (first["sql_frozen"], first["deletable"], first["block_reason"]) == (
        False, False, "not_tip",
    )
    assert (tip["sql_frozen"], tip["deletable"], tip["block_reason"]) == (False, True, None)


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
    _insert_history_row(db_id, mig_id, status="applied")

    r = admin_client.patch(
        f"/api/v1/database-models/{model_id}/migrations/0001", json={"name": "otro nombre"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sql_frozen"] is True


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
