"""
Tests de la API de conversión de charset/collation (feature collation-conversion).

El motor se mockea (SQLite como BD de metadatos + adapter falso para el plano en vivo).
El worker asíncrono se ejecuta SÍNCRONO en el test (``enqueue`` → ``run_job`` inline) y
``MigrationRunner`` se sustituye por un fake que devuelve 'applied' salvo para las
sentencias que el test marca como fallidas. Así se ejercita todo el pipeline del controller
sin motores reales.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

import app.controllers.collation_conversion_controller as cc
import app.services.collation_conversion_runner as ccr
from app.services.db_admin.dtos import (
    CollationGroup,
    CollationInventory,
    CollationObjectInfo,
    ExternalFkDependent,
    RoutineGrantInfo,
    TableCollationInfo,
)
from app.services.db_admin.migrations import StatementResult

TARGET_CS = "utf8mb4"
TARGET_CO = "utf8mb4_unicode_ci"
OLD_CO = "utf8mb4_general_ci"


# --------------------------------------------------------------------------- #
# Helpers de inventario                                                        #
# --------------------------------------------------------------------------- #
def _server(admin_client, port, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={"name": f"srv{port}", "host": "10.0.0.5", "port": port, "engine": engine,
              "root_username": "root", "root_password": "pw"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _inventory(database="app_db", target=TARGET_CO) -> CollationInventory:
    """
    Inventario base: 2 tablas desactualizadas + 1 ya al día, y los 5 tipos de objeto
    congelados en la collation vieja.
    """
    tables = [
        TableCollationInfo(name="users", charset="utf8mb3", collation="utf8mb3_general_ci",
                           mismatched_columns=3, needs_conversion=True),
        TableCollationInfo(name="orders", charset="utf8mb3", collation="utf8mb3_general_ci",
                           mismatched_columns=1, needs_conversion=True),
        TableCollationInfo(name="already_ok", charset=TARGET_CS, collation=target,
                           mismatched_columns=0, needs_conversion=False),
    ]
    objects = [
        CollationObjectInfo(object_type="procedure", name="sp_x", collation_connection=OLD_CO,
                            database_collation=OLD_CO, is_outdated=True),
        CollationObjectInfo(object_type="function", name="fn_y", collation_connection=OLD_CO,
                            database_collation=OLD_CO, is_outdated=True),
        CollationObjectInfo(object_type="trigger", name="tg_z", collation_connection=OLD_CO,
                            database_collation=OLD_CO, is_outdated=True),
        CollationObjectInfo(object_type="event", name="ev_w", collation_connection=OLD_CO,
                            database_collation=OLD_CO, is_outdated=True),
        # VIEWS nunca trae DATABASE_COLLATION (information_schema.VIEWS no la expone).
        CollationObjectInfo(object_type="view", name="v_v", collation_connection=OLD_CO,
                            database_collation=None, is_outdated=True),
    ]
    summary = [
        CollationGroup(charset="utf8mb3", collation="utf8mb3_general_ci", table_count=2),
        CollationGroup(charset=TARGET_CS, collation=target, table_count=1),
    ]
    return CollationInventory(
        database=database, engine="mysql", db_charset="utf8mb3",
        db_collation="utf8mb3_general_ci", target_collation=target,
        tables=tables, summary=summary, objects=objects,
    )


class _FakeAdapter:
    """Adapter en memoria: inventario, captura de DDL y grants de rutina."""

    dialect = "mysql"
    supports_collation_conversion = True

    def __init__(self, inventory: CollationInventory, *, databases=("app_db",)):
        self.inventory = inventory
        self.databases = list(databases)
        self.captured: list[tuple[str, str]] = []
        self.grants_read: list[tuple[str, str]] = []
        self.grants_applied: list[list[RoutineGrantInfo]] = []
        self.external_fks: list[ExternalFkDependent] = []
        # Fallos inyectables por el test.
        self.capture_fails: set[tuple[str, str]] = set()
        self.grants_read_fails: set[tuple[str, str]] = set()
        self.grants_apply_fails = False
        self.routine_grants_by_name: dict[str, list[RoutineGrantInfo]] = {}

    def list_databases(self):
        return list(self.databases)

    def collation_inventory(self, database, *, target_collation=None):
        return self.inventory

    def capture_object_ddl(self, database, object_type, name):
        if (object_type, name) in self.capture_fails:
            from app.exceptions import AppHttpException

            raise AppHttpException(message="no se pudo capturar", status_code=409)
        self.captured.append((object_type, name))
        return f"CREATE {object_type.upper()} `{name}`() BEGIN SELECT 1; END"

    def routine_grants(self, database, routine_type, name):
        if (routine_type.lower(), name) in self.grants_read_fails:
            raise RuntimeError("mysql.procs_priv ilegible")
        self.grants_read.append((routine_type.lower(), name))
        return list(self.routine_grants_by_name.get(name, []))

    def apply_routine_grants(self, database, grants):
        if self.grants_apply_fails:
            raise RuntimeError("GRANT rechazado")
        self.grants_applied.append(list(grants))
        return len(grants)

    def external_fk_dependents(self, database):
        return list(self.external_fks)


class _FakeRunner:
    """Runner síncrono: todo 'applied' salvo el SQL que el test marque como fallido."""

    fail_substrings: list[str] = []
    executed: list[list[str]] = []

    @contextmanager
    def advisory_lock(self, target, *, engine, lock_key):
        yield  # no-op en test (sin motor real que lockear)

    def execute_adhoc(self, target, *, db_name, engine, lock_key, statements,
                      already_locked=False, stop_on_error=True, disable_fk_checks=False):
        type(self).executed.append(list(statements))
        out = []
        for i, stmt in enumerate(statements):
            bad = any(s in stmt for s in type(self).fail_substrings)
            out.append(
                StatementResult(
                    index=i, status="failed" if bad else "applied",
                    error="error simulado del motor" if bad else None,
                    execution_ms=1, executed_at=datetime.now(timezone.utc),
                )
            )
            if bad and stop_on_error:
                break
        return out


def _install(monkeypatch, inventory=None, *, adapter=None):
    """Instala el adapter fake + runner síncrono. Devuelve el fake para inspección."""
    fake = adapter or _FakeAdapter(inventory or _inventory())
    _FakeRunner.fail_substrings = []
    _FakeRunner.executed = []
    monkeypatch.setattr(cc, "get_adapter", lambda target: fake)
    monkeypatch.setattr(cc, "MigrationRunner", _FakeRunner)
    monkeypatch.setattr(
        ccr, "enqueue", lambda job_id: cc.CollationConversionController().run_job(job_id)
    )
    return fake


def _create(admin_client, sid, database="app_db", charset=TARGET_CS, collation=TARGET_CO):
    return admin_client.post(
        f"/api/v1/servers/{sid}/databases/{database}/collation-conversions",
        json={"target_charset": charset, "target_collation": collation},
    )


def _preview(admin_client, job_id, *, tables=None, objects=None, **kw):
    body = {"tables": tables or [], "objects": objects or []}
    body.update(kw)
    return admin_client.post(f"/api/v1/collation-conversions/{job_id}/preview", json=body)


def _execute(admin_client, job_id, token, *, name="app_db", force=False):
    return admin_client.post(
        f"/api/v1/collation-conversions/{job_id}/execute",
        json={"confirm_target_name": name, "confirm_token": token, "force": force},
    )


def _items(admin_client, job_id):
    r = admin_client.get(f"/api/v1/collation-conversions/{job_id}/items?size=50")
    assert r.status_code == 200, r.text
    return r.json()["data"]


# =========================================================================== #
# Tests                                                                        #
# =========================================================================== #
def test_requires_auth(client):
    r = client.post(
        "/api/v1/servers/1/databases/app_db/collation-conversions",
        json={"target_charset": TARGET_CS, "target_collation": TARGET_CO},
    )
    assert r.status_code == 401


def test_postgresql_does_not_use_the_universal_mode(admin_client, monkeypatch):
    """
    PostgreSQL NO entra al modo ``universal``: no tiene collation congelada (la resuelve
    dinámicamente) ni ``ALTER DATABASE`` de encoding posible. Su conversión es otra
    operación, acotada por columna, con su propio modo — y su ``target_charset`` no aplica,
    así que enviarlo (obligatorio en universal) es un 422.

    El pipeline del modo ``columns`` se prueba en ``test_api_collation_conversions_pg.py``.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3700, engine="postgresql")
    r = _create(admin_client, sid)  # manda target_charset, que es de MySQL/MariaDB
    assert r.status_code == 422, r.text
    msg = r.json()["detail"]["msg"]
    assert "charset" in msg
    assert "inmutable" in msg


def test_inventory_groups_tables_by_collation(admin_client, monkeypatch):
    """El inventario dice cuántos pares (charset, collation) hay y cuántas tablas en cada uno."""
    _install(monkeypatch)
    sid = _server(admin_client, 3701)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    r = admin_client.get(f"/api/v1/collation-conversions/{job_id}/objects")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["target_charset"] == TARGET_CS
    assert data["target_collation"] == TARGET_CO
    summary = {(g["charset"], g["collation"]): g["table_count"] for g in data["summary"]}
    assert summary[("utf8mb3", "utf8mb3_general_ci")] == 2
    assert summary[(TARGET_CS, TARGET_CO)] == 1
    # Los 5 tipos congelados salen con su collation_connection y marcados desactualizados.
    types = {o["object_type"] for o in data["objects"]}
    assert types == {"procedure", "function", "trigger", "event", "view"}
    assert all(o["is_outdated"] for o in data["objects"])
    view = next(o for o in data["objects"] if o["object_type"] == "view")
    assert view["database_collation"] is None  # information_schema.VIEWS no la expone


def test_preview_skips_tables_already_at_target(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3702)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    r = _preview(admin_client, job_id, tables=["users", "orders", "already_ok"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["tables_to_convert"] == 2
    assert data["tables_skipped"] == 1
    actions = {(s["object_type"], s["object_name"]): s["action"] for s in data["steps"]}
    assert actions[("table", "already_ok")] == "skip"
    assert actions[("table", "users")] == "convert_table"
    assert actions[("database", "app_db")] == "alter_database"


def test_preview_reports_missing_selection_without_aborting(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3703)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    r = _preview(
        admin_client, job_id,
        tables=["users", "ghost_table"],
        objects=[{"object_type": "procedure", "name": "sp_x"},
                 {"object_type": "procedure", "name": "sp_ghost"}],
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["missing_tables"] == ["ghost_table"]
    assert data["missing"] == [{"object_type": "procedure", "name": "sp_ghost"}]
    # Lo que sí existe entra igual en el plan.
    assert data["tables_to_convert"] == 1
    assert data["objects_to_recreate"] == 1


def test_preview_stale_fingerprint_returns_409(admin_client, monkeypatch):
    """Si el inventario cambió desde que se creó el plan, el preview corta con 409."""
    fake = _install(monkeypatch)
    sid = _server(admin_client, 3704)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    # Alguien agrega una tabla en el motor entre el plan y el preview.
    fake.inventory.tables.append(
        TableCollationInfo(name="nueva", charset="utf8mb3", collation="utf8mb3_general_ci",
                           needs_conversion=True)
    )
    r = _preview(admin_client, job_id, tables=["users"])
    assert r.status_code == 409, r.text
    assert "cambió" in r.json()["detail"]["msg"]

    # force=true adopta el inventario nuevo y deja seguir.
    r2 = _preview(admin_client, job_id, tables=["users"], force=True)
    assert r2.status_code == 200, r2.text


def test_execute_requires_both_confirmations(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3705)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(admin_client, job_id, tables=["users"]).json()["data"]["confirm_token"]

    # Nombre equivocado → 422 (aunque el token sea correcto).
    bad_name = _execute(admin_client, job_id, token, name="otra_db")
    assert bad_name.status_code == 422, bad_name.text
    assert "confirm_target_name" in bad_name.json()["detail"]["msg"]

    # Token equivocado → 422 (aunque el nombre sea correcto).
    bad_token = _execute(admin_client, job_id, "0" * 64)
    assert bad_token.status_code == 422, bad_token.text
    assert "confirm_token" in bad_token.json()["detail"]["msg"]

    # Ambos correctos → encola y corre.
    ok = _execute(admin_client, job_id, token)
    assert ok.status_code == 200, ok.text


def test_execute_without_preview_is_rejected(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3706)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    r = _execute(admin_client, job_id, "x" * 64)
    assert r.status_code == 409, r.text
    assert "previsualizar" in r.json()["detail"]["msg"]


def test_full_run_alters_database_tables_and_recreates_objects(admin_client, monkeypatch):
    fake = _install(monkeypatch)
    sid = _server(admin_client, 3707)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    objects = [{"object_type": "procedure", "name": "sp_x"},
               {"object_type": "view", "name": "v_v"}]
    token = _preview(
        admin_client, job_id, tables=["users", "orders"], objects=objects
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    summary = admin_client.get(f"/api/v1/collation-conversions/{job_id}").json()["data"]
    assert summary["status"] == "succeeded", summary
    assert summary["phase"] == "done"

    items = _items(admin_client, job_id)
    by_key = {(i["object_type"], i["object_name"]): i for i in items}
    assert by_key[("database", "app_db")]["status"] == "ok"
    assert by_key[("table", "users")]["status"] == "ok"
    assert by_key[("procedure", "sp_x")]["status"] == "ok"
    assert by_key[("view", "v_v")]["status"] == "ok"
    # El DDL capturado queda persistido como copia de recuperación.
    assert fake.captured == [("procedure", "sp_x"), ("view", "v_v")]

    # LA GARANTÍA CENTRAL: cada DROP+CREATE viaja precedido por el SET NAMES objetivo, en el
    # mismo lote/conexión. Sin eso el objeto recreado volvería a congelar la collation vieja.
    recreate_batches = [b for b in _FakeRunner.executed if len(b) == 3]
    assert recreate_batches, _FakeRunner.executed
    for batch in recreate_batches:
        assert batch[0] == f"SET NAMES {TARGET_CS} COLLATE {TARGET_CO}"
        assert batch[1].startswith("DROP ")
        assert " IF EXISTS " in batch[1]
        assert batch[2].startswith("CREATE ")
    # El ALTER DATABASE va PRIMERO (antes de tablas y objetos).
    assert items[0]["object_type"] == "database"


def test_one_failing_object_does_not_abort_the_rest(admin_client, monkeypatch):
    """Best-effort: un objeto que falla se reporta y los demás siguen convirtiéndose."""
    fake = _install(monkeypatch)
    fake.capture_fails.add(("function", "fn_y"))
    sid = _server(admin_client, 3708)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    objects = [{"object_type": "procedure", "name": "sp_x"},
               {"object_type": "function", "name": "fn_y"},
               {"object_type": "view", "name": "v_v"}]
    token = _preview(
        admin_client, job_id, tables=["users"], objects=objects
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    summary = admin_client.get(f"/api/v1/collation-conversions/{job_id}").json()["data"]
    assert summary["status"] == "failed"  # hubo un fallo...
    items = {(i["object_type"], i["object_name"]): i for i in _items(admin_client, job_id)}
    assert items[("function", "fn_y")]["status"] == "error"
    # ...pero el resto SÍ se procesó (no abortó en el primer fallo).
    assert items[("procedure", "sp_x")]["status"] == "ok"
    assert items[("view", "v_v")]["status"] == "ok"
    assert items[("table", "users")]["status"] == "ok"


def test_failed_alter_database_stops_the_pipeline(admin_client, monkeypatch):
    """El ALTER DATABASE es el único fallo que corta: seguir dejaría la conversión a medias."""
    _install(monkeypatch)
    _FakeRunner.fail_substrings = ["ALTER DATABASE"]
    sid = _server(admin_client, 3709)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, tables=["users"],
        objects=[{"object_type": "procedure", "name": "sp_x"}],
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    summary = admin_client.get(f"/api/v1/collation-conversions/{job_id}").json()["data"]
    assert summary["status"] == "failed"
    items = _items(admin_client, job_id)
    assert len(items) == 1
    assert items[0]["object_type"] == "database"
    assert items[0]["status"] == "error"


def test_routine_grants_are_captured_and_reapplied(admin_client, monkeypatch):
    """Dropear una rutina BORRA sus privilegios: hay que leerlos antes y reaplicarlos después."""
    fake = _install(monkeypatch)
    fake.routine_grants_by_name["sp_x"] = [
        RoutineGrantInfo(username="app", host="%", routine_type="PROCEDURE",
                         routine_name="sp_x", privileges=["EXECUTE"]),
        RoutineGrantInfo(username="report", host="10.%", routine_type="PROCEDURE",
                         routine_name="sp_x", privileges=["EXECUTE", "ALTER ROUTINE"],
                         grant_option=True),
    ]
    sid = _server(admin_client, 3710)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, objects=[{"object_type": "procedure", "name": "sp_x"}]
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    assert fake.grants_read == [("procedure", "sp_x")]
    assert len(fake.grants_applied) == 1
    assert {g.username for g in fake.grants_applied[0]} == {"app", "report"}
    item = next(
        i for i in _items(admin_client, job_id) if i["object_type"] == "procedure"
    )
    assert item["status"] == "ok"
    assert item["grants_captured"] == 2
    assert item["grants_reapplied"] == 2

    # TRIGGER/EVENT/VIEW no tienen grants propios: no se consultan.
    assert all(t == "procedure" for t, _ in fake.grants_read)


def test_unreadable_routine_grants_skip_the_drop_fail_closed(admin_client, monkeypatch):
    """
    FAIL-CLOSED: si no se pueden leer los privilegios de la rutina, NO se dropea. Dropear a
    ciegas destruiría privilegios que después no habría forma de restaurar.
    """
    fake = _install(monkeypatch)
    fake.grants_read_fails.add(("procedure", "sp_x"))
    sid = _server(admin_client, 3711)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, objects=[{"object_type": "procedure", "name": "sp_x"},
                                       {"object_type": "view", "name": "v_v"}]
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    items = {(i["object_type"], i["object_name"]): i for i in _items(admin_client, job_id)}
    sp = items[("procedure", "sp_x")]
    assert sp["status"] == "skipped"
    assert "procs_priv" in sp["grants_error"]
    # No se ejecutó ningún DROP de la rutina.
    assert not any(
        any("DROP PROCEDURE" in s for s in batch) for batch in _FakeRunner.executed
    )
    # La vista, que no tiene grants propios, sí se recreó.
    assert items[("view", "v_v")]["status"] == "ok"


def test_recreated_routine_with_failed_regrant_is_reported_as_error(admin_client, monkeypatch):
    """El objeto existe pero perdió permisos: no se puede reportar como éxito."""
    fake = _install(monkeypatch)
    fake.routine_grants_by_name["sp_x"] = [
        RoutineGrantInfo(username="app", host="%", routine_type="PROCEDURE",
                         routine_name="sp_x", privileges=["EXECUTE"]),
    ]
    fake.grants_apply_fails = True
    sid = _server(admin_client, 3712)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, objects=[{"object_type": "procedure", "name": "sp_x"}]
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    item = next(i for i in _items(admin_client, job_id) if i["object_type"] == "procedure")
    assert item["status"] == "error"
    assert "RECREÓ" in item["grants_error"]
    assert admin_client.get(
        f"/api/v1/collation-conversions/{job_id}"
    ).json()["data"]["status"] == "failed"


def test_create_ddl_failure_after_drop_flags_recovery_path(admin_client, monkeypatch):
    """Si el DROP pasó y el CREATE no, el objeto ya no existe: hay que decirlo y dejar el DDL."""
    _install(monkeypatch)
    _FakeRunner.fail_substrings = ["CREATE PROCEDURE"]
    sid = _server(admin_client, 3713)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, objects=[{"object_type": "procedure", "name": "sp_x"}]
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    item = next(i for i in _items(admin_client, job_id) if i["object_type"] == "procedure")
    assert item["status"] == "error"
    assert "captured_ddl" in item["error"]
    assert "NO existe" in item["error"]


def test_external_fk_dependents_warning(admin_client, monkeypatch):
    """Una FK desde OTRA BD del servidor exige la misma collation en ambos lados."""
    fake = _install(monkeypatch)
    fake.external_fks = [
        ExternalFkDependent(schema_name="otra_db", table="pedidos", column="user_id",
                            constraint="fk_u", referenced_table="users",
                            referenced_column="id"),
    ]
    sid = _server(admin_client, 3714)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    inv = admin_client.get(f"/api/v1/collation-conversions/{job_id}/objects").json()["data"]
    assert any("otra_db" in w for w in inv["warnings"])

    pr = _preview(admin_client, job_id, tables=["users", "orders"]).json()["data"]
    assert any("otra_db" in w for w in pr["warnings"])


def test_partial_table_selection_warns_about_fk_collation_mismatch(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3715)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    pr = _preview(admin_client, job_id, tables=["users"]).json()["data"]  # falta 'orders'
    assert any("sin convertir" in w and "orders" in w for w in pr["warnings"])
    # Y avisa de los objetos congelados que quedan sin recrear.
    assert any("collation vieja congelada" in w for w in pr["warnings"])


def test_cancel_marks_job_and_stops_pending_steps(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3716)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    r = admin_client.post(f"/api/v1/collation-conversions/{job_id}/cancel")
    assert r.status_code == 200, r.text
    # Un job ya cancelado/terminado no se puede re-cancelar.
    _preview(admin_client, job_id, tables=["users"])


def test_empty_plan_is_rejected(admin_client, monkeypatch):
    """Sin ALTER DATABASE, sin tablas y sin objetos no hay nada que ejecutar."""
    _install(monkeypatch)
    sid = _server(admin_client, 3717)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, include_database_default=False
    ).json()["data"]["confirm_token"]
    r = _execute(admin_client, job_id, token)
    assert r.status_code == 422, r.text
    assert "ningún paso" in r.json()["detail"]["msg"]


def test_unknown_charset_collation_pair_is_rejected(admin_client, monkeypatch):
    """La combinación debe estar HABILITADA en el catálogo global antes de tocar el motor."""
    _install(monkeypatch)
    sid = _server(admin_client, 3718)
    r = _create(admin_client, sid, charset="latin1", collation="latin1_swedish_ci")
    assert r.status_code == 422, r.text


def test_universal_mode_requires_target_charset(admin_client, monkeypatch):
    """
    Omitir target_charset en MySQL/MariaDB da un 422 CLARO ("target_charset es
    obligatorio"), no el mensaje ambiguo del catálogo.

    Regresión: antes de este fix, resolve_enabled_combination(dialect, None, collation)
    resuelve legítimamente la rama "solo collation" y devuelve (None, <collation
    habilitada>) — un ÉXITO real. El chequeo posterior `if not charset or not collation`
    lo interpretaba como fallo con un mensaje que no explicaba nada (a diferencia del 422
    de catálogo, que sí trae `allowed` en public_context).
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3719)
    r = admin_client.post(
        f"/api/v1/servers/{sid}/databases/app_db/collation-conversions",
        json={"target_collation": TARGET_CO},  # sin target_charset
    )
    assert r.status_code == 422, r.text
    assert "target_charset" in r.json()["detail"]["msg"]
    assert "obligatorio" in r.json()["detail"]["msg"]


def test_execute_force_survives_drift_the_worker_would_otherwise_catch(
    admin_client, monkeypatch
):
    """
    Regresión: execute(force=true) con un inventario que cambió desde el preview
    devolvía 200 pero el job moría en 'failed' segundos después, porque el WORKER
    revalida el fingerprint de forma INCONDICIONAL al arrancar (red de seguridad final)
    y no se enteraba de ese `force`. El fix hace que execute(force=true) adopte el
    fingerprint actual como base — igual que preview(force=true) — así el worker ya no
    encuentra drift alguno.
    """
    fake = _install(monkeypatch)
    sid = _server(admin_client, 3720)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(admin_client, job_id, tables=["users"]).json()["data"]["confirm_token"]

    # Drift DESPUÉS del preview: una tabla nueva no seleccionada aparece en el motor.
    fake.inventory.tables.append(
        TableCollationInfo(
            name="nueva", charset="utf8mb3", collation="utf8mb3_general_ci",
            needs_conversion=True,
        )
    )

    r = _execute(admin_client, job_id, token, force=True)
    assert r.status_code == 200, r.text

    final = admin_client.get(f"/api/v1/collation-conversions/{job_id}").json()["data"]
    assert final["status"] == "succeeded", (
        f"force=true no sobrevivió al drift: status={final['status']} error={final['error']}"
    )
