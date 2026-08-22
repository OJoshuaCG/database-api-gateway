"""
Guard de entorno: bloqueo de DDL destructivo en las BDs de un entorno que lo prohíbe.

Estos tests son los que sostienen la feature. Dos de ellos son la razón por la que el diseño
se rehízo, y si alguna vez se "simplifican" el guard vuelve a ser un adorno:

- ``test_hand_written_drop_is_blocked_without_manifest``: el diseño original leía
  ``ModelMigrationStatement.destructive``, y esas filas SOLO las escribe la adopción de un diff
  estructural. Una migración escrita a mano no genera ninguna, así que el guard pasaba el
  ``DROP TABLE``. Este test falla contra esa implementación.
- ``test_engine_override_drop_is_blocked``: el ``DROP`` vive solo en ``up_sql_postgresql``.
  Mirando ``spec.up_sql`` es invisible justo en el motor donde se ejecuta.

Y dos que verifican dónde está puesto, no solo qué decide:

- ``test_blocked_db_does_not_abort_the_batch``: un guard mal ubicado (fuera del bucle) aborta
  el lote entero. Un test que mire solo la BD bloqueada no lo ve.
- ``test_per_database_apply_is_also_blocked``: sin cubrir el ``apply`` por BD, el guard empuja
  al camino no cubierto (``apply_all`` va siempre al head, así que producción quedaría trabada
  en el lote mientras ``?version=`` aplica lo mismo sin gate).
"""

from app.services.db_admin import migration_facts
from app.services.db_admin.migrations import MigrationRunner

DROP_SQL = "DROP TABLE clientes"
SAFE_SQL = "CREATE TABLE t1 (id INT PRIMARY KEY)"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _env_ids(admin_client) -> dict[str, int]:
    """slug → id de los entornos sembrados por el lifespan."""
    r = admin_client.get("/api/v1/environments?size=50")
    assert r.status_code == 200, r.text
    return {e["slug"]: e["id"] for e in r.json()["data"]}


def _blueprint(admin_client, slug: str, migrations: list[dict]) -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    for m in migrations:
        r = admin_client.post(
            f"/api/v1/database-models/{model_id}/migrations", json=m
        )
        assert r.status_code == 201, r.text
    return model_id


def _server(admin_client, server_payload, name="srv-env", engine="mysql", port=3399) -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json=server_payload(name=name, engine=engine, port=port),
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _owner(admin_client, server_id: int, username="owner1") -> int:
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _db(admin_client, *, server_id, owner_id, model_id, name, environment_id=None) -> int:
    payload = {
        "name": name,
        "server_id": server_id,
        "owner_id": owner_id,
        "model_id": model_id,
    }
    if environment_id is not None:
        payload["environment_id"] = environment_id
    r = admin_client.post("/api/v1/managed-databases", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _fresh_engine(monkeypatch, current=None):
    """El motor no existe: se mockea la lectura de versión y el apply, como el resto de la suite."""
    monkeypatch.setattr(
        MigrationRunner, "get_current_version", lambda self, *a, **k: current
    )
    monkeypatch.setattr(MigrationRunner, "apply", lambda self, *a, **k: [])


# --------------------------------------------------------------------------- #
# EL test del rediseño: el manifiesto no existe y el guard tiene que bloquear   #
# --------------------------------------------------------------------------- #
def test_hand_written_drop_is_blocked_without_manifest(
    admin_client, server_payload, monkeypatch
):
    """
    Una migración escrita a mano con un DROP produce CERO filas de manifiesto y el guard
    tiene que bloquear igual. Es el fail-open que invalidó el diseño original.
    """
    from app.core.database import Database
    from app.models.model_migration_statement import ModelMigrationStatement

    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-drop", [{"version": "0001", "name": "drop", "up_sql": DROP_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="proddb", environment_id=envs["production"],
    )

    # Premisa del test, verificada y no asumida: NO hay manifiesto para esta versión.
    session = Database().get_declarative_base_session()
    try:
        assert session.query(ModelMigrationStatement).count() == 0
    finally:
        session.close()

    _fresh_engine(monkeypatch)
    r = admin_client.post(f"/api/v1/database-models/{model_id}/migrations/apply-all")
    assert r.status_code == 200, r.text
    item = r.json()["data"]["results"][0]
    assert item["ok"] is False
    assert item["error_code"] == "environment.destructive_blocked"
    assert item["blocked_by"] == ["0001"]
    assert item["environment_slug"] == "production"
    # El ítem lleva los dos campos que el zod del cliente exige no-nulos: si faltan, el
    # safeParse descarta la respuesta ENTERA y se pierde el resultado de las demás BDs.
    assert item["database_name"] == "proddb"
    assert item["server_id"] == sid


def test_engine_override_drop_is_blocked(admin_client, server_payload, monkeypatch):
    """
    El DROP vive SOLO en ``up_sql_postgresql``. Analizar ``spec.up_sql`` no lo vería.
    """
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client,
        "bp-override",
        [
            {
                "version": "0001",
                "name": "override",
                "up_sql": SAFE_SQL,
                "up_sql_postgresql": DROP_SQL,
            }
        ],
    )
    sid = _server(admin_client, server_payload, name="srv-pg", engine="postgresql", port=5439)
    oid = _owner(admin_client, sid, username="pgowner")
    _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="pgprod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(f"/api/v1/database-models/{model_id}/migrations/apply-all")
    assert r.status_code == 200, r.text
    item = r.json()["data"]["results"][0]
    assert item["ok"] is False, item
    assert item["error_code"] == "environment.destructive_blocked"
    assert item["blocked_by"] == ["0001"]


def test_safe_sql_on_same_blueprint_is_not_blocked(
    admin_client, server_payload, monkeypatch
):
    """Control del test anterior: el mismo SQL base inocuo en MySQL NO se bloquea."""
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client,
        "bp-override2",
        [
            {
                "version": "0001",
                "name": "override",
                "up_sql": SAFE_SQL,
                "up_sql_postgresql": DROP_SQL,
            }
        ],
    )
    sid = _server(admin_client, server_payload, name="srv-my", engine="mysql", port=3398)
    oid = _owner(admin_client, sid, username="myowner")
    _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="myprod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(f"/api/v1/database-models/{model_id}/migrations/apply-all")
    item = r.json()["data"]["results"][0]
    assert item["ok"] is True, item
    assert item.get("error_code") is None


# --------------------------------------------------------------------------- #
# Guard ⊇ insignia: nada que la API llame destructivo puede pasar el guard      #
# --------------------------------------------------------------------------- #
def test_guard_covers_everything_the_badge_calls_destructive():
    """
    El listado publica ``destructive`` con ``quick_facts`` (regex) y el guard decide con
    ``analyze`` (AST). Si divergen, el guard tiene que ser el MÁS estricto: una insignia que
    dice "destructiva" y un guard que la aplica sería la contradicción visible.
    """
    corpus = [
        "DROP TABLE clientes",
        "DROP DATABASE algo",
        "TRUNCATE TABLE ventas",
        "DELETE FROM ventas",
        "ALTER TABLE t DROP COLUMN c",
        "DROP TABLE a; DROP TABLE b",
    ]
    for sql in corpus:
        badge = migration_facts.quick_facts(sql).destructive
        guard = bool(migration_facts.analyze(sql, "schema", False).destructive_statements)
        assert not badge or guard, f"la insignia dice destructivo y el guard no: {sql!r}"


# --------------------------------------------------------------------------- #
# Ubicación del guard                                                          #
# --------------------------------------------------------------------------- #
def test_blocked_db_does_not_abort_the_batch(admin_client, server_payload, monkeypatch):
    """
    Una BD bloqueada NO impide que las demás del lote se apliquen. Es lo que distingue un
    guard bien ubicado (dentro del bucle, con el error contenido por ítem) de uno que aborta
    el lote entero.
    """
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-batch", [{"version": "0001", "name": "drop", "up_sql": DROP_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    prod_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="db_prod", environment_id=envs["production"],
    )
    dev_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="db_dev", environment_id=envs["development"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(f"/api/v1/database-models/{model_id}/migrations/apply-all")
    assert r.status_code == 200, r.text
    by_id = {i["managed_database_id"]: i for i in r.json()["data"]["results"]}
    assert by_id[prod_id]["ok"] is False
    assert by_id[prod_id]["error_code"] == "environment.destructive_blocked"
    # La de desarrollo se aplicó: el lote NO se abortó.
    assert by_id[dev_id]["ok"] is True, by_id[dev_id]
    assert by_id[dev_id].get("error_code") is None


def test_per_database_apply_is_also_blocked(admin_client, server_payload, monkeypatch):
    """
    El ``apply`` por BD tiene que estar cubierto, o el guard empuja al camino sin gate.
    """
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-single", [{"version": "0001", "name": "drop", "up_sql": DROP_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    db_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="single_prod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"]["public_context"]["code"] == "environment.destructive_blocked"
    assert body["detail"]["public_context"]["blocked_versions"] == ["0001"]


# --------------------------------------------------------------------------- #
# dry-run, force y el NULL permisivo                                           #
# --------------------------------------------------------------------------- #
def test_dry_run_informs_but_does_not_block(admin_client, server_payload, monkeypatch):
    """
    El dry-run es la llamada con la que el operador descubre qué lo frena: no puede fallar.
    Precedente del criterio: ``_guard_quarantine`` también se saltea en dry-run.
    """
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-dry", [{"version": "0001", "name": "drop", "up_sql": DROP_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    db_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="dry_prod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true"
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["dry_run"] is True
    assert data["pending_versions"] == ["0001"]
    assert data["blocked_by"] == ["0001"]
    assert data["environment_slug"] == "production"


def test_force_does_not_bypass_the_guard(admin_client, server_payload, monkeypatch):
    """``force`` es override de CUARENTENA. Si algún día saltea esto, la barrera se abre con un click."""
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-force", [{"version": "0001", "name": "drop", "up_sql": DROP_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    db_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="force_prod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/migrations/apply?force=true"
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "environment.destructive_blocked"


def test_unclassified_database_is_permissive(admin_client, server_payload, monkeypatch):
    """
    NO REGRESIÓN: una BD sin entorno se comporta igual que antes de esta feature.

    Es el compromiso de compatibilidad de la entrega: romper todos los apply-all que hoy
    funcionan no era aceptable. La asimetría con el gate de agentes (donde NULL debe negar)
    es deliberada y está anotada en ``_env_policy_for``.
    """
    model_id = _blueprint(
        admin_client, "bp-null", [{"version": "0001", "name": "drop", "up_sql": DROP_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    # Se crea con entorno y se DESCLASIFICA explícitamente (null ⇒ vaciar).
    db_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id, name="orphan"
    )
    r = admin_client.patch(
        f"/api/v1/managed-databases/{db_id}", json={"environment_id": None}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["environment_id"] is None

    _fresh_engine(monkeypatch)
    r = admin_client.post(f"/api/v1/database-models/{model_id}/migrations/apply-all")
    item = r.json()["data"]["results"][0]
    assert item["ok"] is True, item
    assert item["environment_slug"] is None


# --------------------------------------------------------------------------- #
# Acotar el lote por entorno                                                   #
# --------------------------------------------------------------------------- #
def test_environment_id_scopes_the_batch(admin_client, server_payload, monkeypatch):
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-scope", [{"version": "0001", "name": "ok", "up_sql": SAFE_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    dev_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="s_dev", environment_id=envs["development"],
    )
    _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="s_prod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/apply-all"
        f"?environment_id={envs['development']}"
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [i["managed_database_id"] for i in data["results"]] == [dev_id]
    # total = TODAS las del blueprint; matched = las que pasaron el filtro. Sin el segundo,
    # "1 de 2 procesadas" no distingue "sobró 1" de "en ese entorno solo había 1".
    assert data["total_databases"] == 2
    assert data["matched_databases"] == 1
    assert data["processed"] == 1


def test_database_ids_outside_environment_is_422(
    admin_client, server_payload, monkeypatch
):
    """
    Un id del blueprint que NO está en el entorno pedido devuelve 422 con la lista, en vez de
    desaparecer del lote en silencio. Mismo criterio fail-closed que ya rige para los ids
    ajenos al blueprint.
    """
    envs = _env_ids(admin_client)
    model_id = _blueprint(
        admin_client, "bp-cross", [{"version": "0001", "name": "ok", "up_sql": SAFE_SQL}]
    )
    sid = _server(admin_client, server_payload)
    oid = _owner(admin_client, sid)
    prod_id = _db(
        admin_client, server_id=sid, owner_id=oid, model_id=model_id,
        name="c_prod", environment_id=envs["production"],
    )
    _fresh_engine(monkeypatch)
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/apply-all"
        f"?environment_id={envs['development']}&database_ids={prod_id}"
    )
    assert r.status_code == 422, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "environment.databases_outside_environment"
    assert pc["database_ids_outside"] == [prod_id]
