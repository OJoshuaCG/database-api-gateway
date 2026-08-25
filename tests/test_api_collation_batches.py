"""
Tests del LOTE de conversión de collation (todas las BDs de un blueprint en un gesto).

El motor se mockea (SQLite como BD de metadatos + adapter falso) y el worker corre SÍNCRONO:
`enqueue` → `run_job` inline. Con eso los N jobs del lote corren en serie y en orden, sin
hilos reales ni flakiness — que es exactamente el comportamiento de producción con
`COLLATION_CONVERSION_MAX_WORKERS=1`.

Se reusa el adapter/runner falsos de `test_api_collation_conversions`: el lote no introduce
un plano en vivo distinto, solo orquesta N veces el mismo.
"""

import app.controllers.collation_conversion_controller as cc
import app.services.collation_conversion_runner as ccr
from tests.test_api_collation_conversions import (
    TARGET_CO,
    TARGET_CS,
    _FakeAdapter,
    _FakeRunner,
    _inventory,
)

SLUG = "bp-collation"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _install(monkeypatch, *, adapter=None, databases=("db_a", "db_b", "db_c")):
    fake = adapter or _FakeAdapter(_inventory(), databases=list(databases))
    _FakeRunner.fail_substrings = []
    _FakeRunner.executed = []
    _FakeRunner.calls = []
    monkeypatch.setattr(cc, "get_adapter", lambda target: fake)
    monkeypatch.setattr(cc, "MigrationRunner", _FakeRunner)
    monkeypatch.setattr(
        ccr, "enqueue", lambda job_id: cc.CollationConversionController().run_job(job_id)
    )
    return fake


def _blueprint(admin_client, slug=SLUG) -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _server(admin_client, port, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={"name": f"srv{port}", "host": "10.0.0.5", "port": port, "engine": engine,
              "root_username": "root", "root_password": "pw"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _owner(admin_client, sid, username="own") -> int:
    r = admin_client.post(
        "/api/v1/server-users",
        json={"server_id": sid, "username": f"{username}{sid}", "host": "%"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _managed(admin_client, sid, owner, name, model_id, *, environment_id=None) -> int:
    body = {"name": name, "server_id": sid, "owner_id": owner, "model_id": model_id}
    if environment_id is not None:
        body["environment_id"] = environment_id
    r = admin_client.post("/api/v1/managed-databases", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _activate(db_id: int) -> None:
    """
    Pone la fila en `active`. El lote solo admite BDs activas y `POST /managed-databases`
    (sin `?provision=true`, que tocaría el motor) las deja en `pending`.
    """
    from app.core.database import Database
    from app.models.enums import ProvisionStatus
    from app.models.managed_database import ManagedDatabase

    session = Database().get_declarative_base_session()
    try:
        md = session.get(ManagedDatabase, db_id)
        md.status = ProvisionStatus.active
        session.commit()
    finally:
        session.close()


def _env_ids(admin_client) -> dict[str, int]:
    r = admin_client.get("/api/v1/environments")
    assert r.status_code == 200, r.text
    return {e["slug"]: e["id"] for e in r.json()["data"]}


def _setup(admin_client, port, names=("db_a", "db_b"), *, environments=None):
    """Blueprint + servidor + N BDs activas. Devuelve (model_id, sid, {name: db_id})."""
    model_id = _blueprint(admin_client)
    sid = _server(admin_client, port)
    owner = _owner(admin_client, sid)
    ids = {}
    envs = environments or {}
    for name in names:
        db_id = _managed(
            admin_client, sid, owner, name, model_id, environment_id=envs.get(name)
        )
        _activate(db_id)
        ids[name] = db_id
    return model_id, sid, ids


def _plan(admin_client, model_id, **kw):
    body = {"target_charset": TARGET_CS, "target_collation": TARGET_CO}
    body.update(kw)
    return admin_client.post(
        f"/api/v1/database-models/{model_id}/collation-conversions", json=body
    )


def _exec(admin_client, model_id, plan, **kw):
    body = {
        "confirm_model_slug": SLUG,
        "confirm_token": plan["batch_token"],
        "database_ids": [d["managed_database_id"] for d in plan["databases"] if d["ok"]],
    }
    body.update(kw)
    return admin_client.post(
        f"/api/v1/database-models/{model_id}/collation-conversions/"
        f"{plan['batch_id']}/execute",
        json=body,
    )


# =========================================================================== #
# Planificación                                                                #
# =========================================================================== #
def test_requires_auth(client):
    assert client.post("/api/v1/database-models/1/collation-conversions", json={}).status_code == 401


def test_plan_creates_one_previewed_job_per_active_database(admin_client, monkeypatch):
    """Un job por BD activa, ya previsualizado y con su posición en el lote."""
    _install(monkeypatch)
    model_id, _sid, ids = _setup(admin_client, 3921)

    r = _plan(admin_client, model_id)
    assert r.status_code == 201, r.text
    data = r.json()["data"]

    assert data["total_eligible"] == 2
    assert data["capped"] is False
    assert data["runs_serially"] is True
    assert data["batch_token"]
    assert [d["batch_seq"] for d in data["databases"]] == [1, 2]
    assert {d["database_name"] for d in data["databases"]} == set(ids)
    for d in data["databases"]:
        assert d["ok"] is True, d
        assert d["job_id"] is not None
        assert d["confirm_token"]
        # scope=all_tables se resuelve contra el inventario PROPIO de cada BD.
        assert d["tables_to_convert"] == 2  # users + orders (already_ok se saltea)
        assert d["objects_to_recreate"] == 5


def test_plan_excludes_non_active_databases(admin_client, monkeypatch):
    """
    Solo entran las `active`. Una `pending` no existe en el motor, una `error` está en
    cuarentena y una `archived` fue retirada: convertirlas es, en el mejor caso, un fallo
    ruidoso. (`apply_all` no filtra por estado y eso ya está fichado como defecto.)
    """
    _install(monkeypatch)
    model_id = _blueprint(admin_client)
    sid = _server(admin_client, 3922)
    owner = _owner(admin_client, sid)
    activa = _managed(admin_client, sid, owner, "db_a", model_id)
    _activate(activa)
    _managed(admin_client, sid, owner, "db_b", model_id)  # queda pending

    data = _plan(admin_client, model_id).json()["data"]
    assert data["total_eligible"] == 1
    assert [d["database_name"] for d in data["databases"]] == ["db_a"]


def test_plan_reports_capped_instead_of_silently_truncating(admin_client, monkeypatch):
    """El recorte por tope se REPORTA. Silenciarlo haría creer que se convirtió todo."""
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3923, names=("db_a", "db_b", "db_c"))

    data = _plan(admin_client, model_id, max_databases=2).json()["data"]
    assert data["capped"] is True
    assert data["total_eligible"] == 3
    assert len(data["databases"]) == 2

    estado = admin_client.get(
        f"/api/v1/database-models/{model_id}/collation-conversions/{data['batch_id']}"
    ).json()["data"]
    # Persistido, no solo devuelto al planear: si no, el polling no puede reportarlo.
    assert estado["batch"]["capped"] is True


def test_plan_without_eligible_databases_is_422(admin_client, monkeypatch):
    _install(monkeypatch)
    model_id = _blueprint(admin_client)
    r = _plan(admin_client, model_id)
    assert r.status_code == 422, r.text
    assert (
        r.json()["detail"]["public_context"]["code"]
        == "collation.batch_no_eligible_databases"
    )


# =========================================================================== #
# Confirmación — lo que repone el doble factor que un lote se lleva            #
# =========================================================================== #
def test_execute_rejects_a_different_database_set(admin_client, monkeypatch):
    """
    FAIL-CLOSED: el conjunto echado de vuelta tiene que ser IDÉNTICO al previsualizado.

    Ni se recorta ni se amplía. Recortar convertiría menos de lo confirmado; ampliar metería
    bases que nadie previsualizó.
    """
    _install(monkeypatch)
    model_id, _sid, ids = _setup(admin_client, 3924)
    plan = _plan(admin_client, model_id).json()["data"]

    r = _exec(admin_client, model_id, plan, database_ids=[ids["db_a"]])
    assert r.status_code == 422, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "collation.batch_database_set_mismatch"
    assert sorted(pc["planned_database_ids"]) == sorted(ids.values())
    assert _FakeRunner.calls == [], "no se debe haber tocado el motor"


def test_force_cannot_widen_the_database_set(admin_client, monkeypatch):
    """`force` es override de cuarentena y de drift; NO amplía el conjunto de bases."""
    _install(monkeypatch)
    model_id, _sid, ids = _setup(admin_client, 3925)
    plan = _plan(admin_client, model_id).json()["data"]

    r = _exec(
        admin_client, model_id, plan,
        database_ids=[*ids.values(), 9999], force=True,
    )
    assert r.status_code == 422, r.text
    assert (
        r.json()["detail"]["public_context"]["code"]
        == "collation.batch_database_set_mismatch"
    )


def test_execute_rejects_wrong_slug(admin_client, monkeypatch):
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3926)
    plan = _plan(admin_client, model_id).json()["data"]
    r = _exec(admin_client, model_id, plan, confirm_model_slug="otro-slug")
    assert r.status_code == 422, r.text


def test_execute_rejects_stale_token(admin_client, monkeypatch):
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3927)
    plan = _plan(admin_client, model_id).json()["data"]
    r = _exec(admin_client, model_id, plan, confirm_token="0" * 64)
    assert r.status_code == 422, r.text


def test_execute_requires_retyped_name_in_protected_environment(admin_client, monkeypatch):
    """
    Un lote reemplaza N re-tipeos por uno, y el `batch_token` lo genera el SERVIDOR: aporta
    frescura, no intención. Para las BDs cuyo entorno bloquea migraciones destructivas se
    repone el doble factor por base — exactamente donde `TODO.md` dice que vive.
    """
    _install(monkeypatch)
    envs = _env_ids(admin_client)
    model_id, _sid, ids = _setup(
        admin_client, 3928, environments={"db_a": envs["production"]}
    )
    plan = _plan(admin_client, model_id).json()["data"]

    r = _exec(admin_client, model_id, plan)
    assert r.status_code == 422, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "collation.batch_confirmation_required"
    assert pc["requires_confirmation"] == [ids["db_a"]]
    assert _FakeRunner.calls == []

    # Con el nombre re-tipeado, pasa. La BD de entorno permisivo no lo necesita.
    r = _exec(admin_client, model_id, plan, confirmations={str(ids["db_a"]): "db_a"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["enqueued"] == 2


# =========================================================================== #
# Ejecución y polling                                                          #
# =========================================================================== #
def test_full_batch_runs_every_database_in_order(admin_client, monkeypatch):
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3929)
    plan = _plan(admin_client, model_id).json()["data"]

    r = _exec(admin_client, model_id, plan)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["enqueued"] == 2

    estado = admin_client.get(
        f"/api/v1/database-models/{model_id}/collation-conversions/{plan['batch_id']}"
    ).json()["data"]
    assert estado["batch"]["status"] == "done", estado["batch"]
    assert estado["batch"]["counts"]["done"] == 2
    assert estado["batch"]["counts"]["failed"] == 0
    # batch_seq y los totales congelados son lo que permite pintar "la N de M" con denominador.
    assert [j["batch_seq"] for j in estado["jobs"]] == [1, 2]
    for j in estado["jobs"]:
        assert j["tables_total"] == 2
        assert j["objects_total"] == 5
        assert j["status"] == "succeeded"


def test_batch_status_is_derived_not_written_by_workers(admin_client, monkeypatch):
    """
    `done`/`failed` se DERIVAN al leer. Que cada worker consultara a sus hermanos para saber
    si es el último es una carrera en cuanto MAX_WORKERS deje de ser 1.
    """
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3930)
    plan = _plan(admin_client, model_id).json()["data"]
    _FakeRunner.fail_substrings = ["ALTER DATABASE"]
    assert _exec(admin_client, model_id, plan).status_code == 200

    estado = admin_client.get(
        f"/api/v1/database-models/{model_id}/collation-conversions/{plan['batch_id']}"
    ).json()["data"]
    assert estado["batch"]["status"] == "failed"
    assert estado["batch"]["counts"]["failed"] == 2
    assert estado["batch"]["finished_at"] is not None


def test_cancel_batch_stops_the_queue_without_touching_the_engine(admin_client, monkeypatch):
    """
    Cancelar el lote antes de ejecutarlo no toca ningún motor, y deja los jobs cerrados en
    vez de `pending` eternos (nadie los va a volver a encolar).
    """
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3931)
    plan = _plan(admin_client, model_id).json()["data"]

    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/collation-conversions/"
        f"{plan['batch_id']}/cancel"
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["batch"]["status"] == "canceled"
    assert _FakeRunner.calls == []

    # Y ya no se puede ejecutar.
    r = _exec(admin_client, model_id, plan)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "collation.batch_not_pending"


def test_execute_twice_is_rejected(admin_client, monkeypatch):
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3932)
    plan = _plan(admin_client, model_id).json()["data"]
    assert _exec(admin_client, model_id, plan).status_code == 200
    r = _exec(admin_client, model_id, plan)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "collation.batch_not_pending"


def test_batch_of_another_blueprint_is_404(admin_client, monkeypatch):
    """El batch_id se valida contra el model_id del path, no se confía en el cliente."""
    _install(monkeypatch)
    model_id, _sid, _ids = _setup(admin_client, 3933)
    plan = _plan(admin_client, model_id).json()["data"]
    otro = _blueprint(admin_client, slug="bp-otro")
    r = admin_client.get(
        f"/api/v1/database-models/{otro}/collation-conversions/{plan['batch_id']}"
    )
    assert r.status_code == 404, r.text


# =========================================================================== #
# Barrido tras un reinicio                                                     #
# =========================================================================== #
def test_sweep_closes_queued_jobs_of_a_running_batch(admin_client, monkeypatch):
    """
    Un reinicio a mitad de un lote dejaba los jobs en cola `pending` PARA SIEMPRE (la cola del
    ThreadPoolExecutor no es durable) y el lote `running` eterno. El barrido los cierra.

    No se re-encolan a propósito: el fingerprint pudo cambiar y el token autorizaba un plan
    sobre un inventario que ya no es el actual.
    """
    from app.core.database import Database
    from app.models.collation_conversion_batch import CollationConversionBatch
    from app.models.collation_conversion_job import CollationConversionJob

    _install(monkeypatch)
    # `enqueue` a no-op: simula el proceso que muere antes de que el worker levante la cola.
    monkeypatch.setattr(ccr, "enqueue", lambda job_id: None)
    model_id, _sid, _ids = _setup(admin_client, 3934)
    plan = _plan(admin_client, model_id).json()["data"]
    assert _exec(admin_client, model_id, plan).status_code == 200

    barridos = cc.CollationConversionController().sweep_interrupted()
    assert barridos == 2, barridos

    session = Database().get_declarative_base_session()
    try:
        estados = {
            j.id: j.status
            for j in session.query(CollationConversionJob)
            .filter(CollationConversionJob.batch_id == plan["batch_id"])
            .all()
        }
        batch = session.get(CollationConversionBatch, plan["batch_id"])
        batch_status, batch_fin = batch.status, batch.finished_at
    finally:
        session.close()
    assert set(estados.values()) == {"interrupted"}, estados
    assert batch_status == "failed"
    assert batch_fin is not None


def test_sweep_leaves_standalone_pending_jobs_alone(admin_client, monkeypatch):
    """
    Un job `pending` SIN lote es un plan creado y no ejecutado: un estado perfectamente
    legítimo (es lo que deja `create_plan`) que el barrido NO debe tocar. Sin la condición del
    `batch_id`, el barrido se comería todos los planes abiertos en cada reinicio.
    """
    from app.core.database import Database
    from app.models.collation_conversion_job import CollationConversionJob

    _install(monkeypatch, databases=("app_db",))
    sid = _server(admin_client, 3935)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases/app_db/collation-conversions",
        json={"target_charset": TARGET_CS, "target_collation": TARGET_CO},
    )
    assert r.status_code == 201, r.text
    job_id = r.json()["data"]["id"]

    assert cc.CollationConversionController().sweep_interrupted() == 0

    session = Database().get_declarative_base_session()
    try:
        estado = session.get(CollationConversionJob, job_id).status
    finally:
        session.close()
    assert estado == "pending", "un plan suelto sin ejecutar no se debe cerrar"
