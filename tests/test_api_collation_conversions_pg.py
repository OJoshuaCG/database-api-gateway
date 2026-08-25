"""
Tests de la API de conversión de collation en el modo ``columns`` (PostgreSQL).

Es una operación DISTINTA del modo ``universal`` de MySQL/MariaDB, y estos tests fijan
justamente las diferencias:

- NUNCA se emite un ``ALTER DATABASE`` (el ENCODING/LC_COLLATE de una BD PostgreSQL es
  inmutable tras el ``CREATE DATABASE``).
- NUNCA se toca la fase de objetos: PostgreSQL resuelve la collation dinámicamente, así que
  no hay vistas/funciones/triggers que recrear. El adapter fake hereda del REAL, cuyos
  ``capture_object_ddl``/``routine_grants`` levantan 422: si el pipeline los llamara, el
  ítem quedaría en error y el test lo vería.
- El objetivo se valida contra ``pg_collation`` LEÍDO EN VIVO, no contra el catálogo global
  de charsets/collations del gateway (que describe otra cosa: el locale de creación de BD).

El adapter fake SUBCLASA ``PostgresAdapter`` y solo reemplaza los métodos que tocarían el
motor, así que el DDL que se verifica lo rendea el código de producción.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

import app.controllers.collation_conversion_controller as cc
import app.services.collation_conversion_runner as ccr
from app.services.db_admin.dtos import (
    CollatableForeignKey,
    CollationInventory,
    CollationOptionInfo,
    ColumnCollationInfo,
    TableCollationInfo,
)
from app.services.db_admin.migrations import StatementResult
from app.services.db_admin.postgres_adapter import PostgresAdapter

TARGET_CO = "es-ES-x-icu"
OTHER_CO = "C"


def _col(name, data_type="text", collation=None):
    """Columna de texto; ``collation=None`` = hereda la default de la base."""
    return ColumnCollationInfo(
        name=name, data_type=data_type, current_collation=collation,
        is_default_collation=collation is None,
    )


def _inventory(database="app_db", target=TARGET_CO) -> CollationInventory:
    """
    Inventario base: ``users`` con 2 columnas a convertir (una heredada, una en 'C'),
    ``orders`` con 1, y ``already_ok`` con todas ya en el objetivo.
    """
    tables = [
        TableCollationInfo(
            name="users", charset=None, collation=None, mismatched_columns=2,
            needs_conversion=True,
            columns=[
                _col("email", "character varying(255)"),
                _col("nombre", "text", OTHER_CO),
                _col("ya_ok", "text", target),
            ],
        ),
        TableCollationInfo(
            name="orders", charset=None, collation=None, mismatched_columns=1,
            needs_conversion=True,
            columns=[_col("codigo", "character varying(32)", OTHER_CO)],
        ),
        TableCollationInfo(
            name="already_ok", charset=None, collation=None, mismatched_columns=0,
            needs_conversion=False, columns=[_col("nota", "text", target)],
        ),
        # Tabla sin columnas de texto: no necesita conversión y no genera paso.
        TableCollationInfo(
            name="metrics", charset=None, collation=None, mismatched_columns=0,
            needs_conversion=False, columns=[],
        ),
    ]
    return CollationInventory(
        database=database, engine="postgresql", db_charset="UTF8",
        db_collation="en_US.UTF-8", target_collation=target, tables=tables,
        summary=[], objects=[],
        available_collations=[
            CollationOptionInfo(name="C", provider="c", deterministic=True),
            CollationOptionInfo(name="en_US", provider="c", deterministic=True),
            CollationOptionInfo(name=TARGET_CO, provider="i", deterministic=True),
            CollationOptionInfo(name="nd-icu", provider="i", deterministic=False),
        ],
    )


class _FakePgAdapter(PostgresAdapter):
    """PostgresAdapter real con las lecturas del motor reemplazadas."""

    def __init__(self, inventory: CollationInventory, *, databases=("app_db",)):
        self.target = None
        self.inventory = inventory
        self.databases = list(databases)
        self.foreign_keys: list[CollatableForeignKey] = []

    def list_databases(self):
        return list(self.databases)

    def list_collations(self, database):
        return list(self.inventory.available_collations)

    def collation_inventory(self, database, *, target_collation=None):
        return self.inventory

    def collatable_foreign_keys(self, database):
        return list(self.foreign_keys)


class _FakeRunner:
    """Runner síncrono: todo 'applied' salvo el SQL que el test marque como fallido."""

    fail_substrings: list[str] = []
    executed: list[list[str]] = []

    @contextmanager
    def advisory_lock(self, target, *, engine, lock_key):
        yield

    def execute_adhoc(self, target, *, db_name, engine, lock_key, statements,
                      already_locked=False, stop_on_error=True, disable_fk_checks=False,
                      bulk=False):
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
    fake = adapter or _FakePgAdapter(inventory or _inventory())
    _FakeRunner.fail_substrings = []
    _FakeRunner.executed = []
    monkeypatch.setattr(cc, "get_adapter", lambda target: fake)
    monkeypatch.setattr(cc, "MigrationRunner", _FakeRunner)
    monkeypatch.setattr(
        ccr, "enqueue", lambda job_id: cc.CollationConversionController().run_job(job_id)
    )
    return fake


def _server(admin_client, port) -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={"name": f"pg{port}", "host": "10.0.0.9", "port": port,
              "engine": "postgresql", "root_username": "postgres", "root_password": "pw"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create(admin_client, sid, database="app_db", collation=TARGET_CO, charset=None):
    body = {"target_collation": collation}
    if charset is not None:
        body["target_charset"] = charset
    return admin_client.post(
        f"/api/v1/servers/{sid}/databases/{database}/collation-conversions", json=body
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
# Plan y validación del objetivo                                               #
# =========================================================================== #
def test_plan_uses_columns_mode_and_no_charset(admin_client, monkeypatch):
    """El motor determina el modo: PostgreSQL → columns, sin charset objetivo."""
    _install(monkeypatch)
    sid = _server(admin_client, 5500)
    r = _create(admin_client, sid)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["mode"] == "columns"
    assert data["target_charset"] is None
    assert data["target_collation"] == TARGET_CO
    # El default de la BD se informa como CONTEXTO (es inmutable, no se toca).
    assert data["previous_db_charset"] == "UTF8"
    assert data["previous_db_collation"] == "en_US.UTF-8"


def test_target_charset_is_rejected_for_postgresql(admin_client, monkeypatch):
    """No hay charset por columna en PostgreSQL: pedirlo es un error del cliente."""
    _install(monkeypatch)
    sid = _server(admin_client, 5501)
    r = _create(admin_client, sid, charset="UTF8")
    assert r.status_code == 422, r.text
    msg = r.json()["detail"]["msg"]
    assert "charset" in msg and "inmutable" in msg


def test_unknown_collation_is_rejected_against_live_pg_collation(admin_client, monkeypatch):
    """
    El objetivo se valida contra ``pg_collation`` EN VIVO, no contra el catálogo global de
    charsets/collations (que describe el locale de creación de una BD, otro espacio de
    valores). Una collation que no existe en ESE servidor no puede planearse.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 5502)
    r = _create(admin_client, sid, collation="utf8mb4_unicode_ci")
    assert r.status_code == 422, r.text
    msg = r.json()["detail"]["msg"]
    assert "no existe en este servidor" in msg
    assert "locales instalados" in msg


def test_collation_names_are_case_sensitive(admin_client, monkeypatch):
    """En PostgreSQL los nombres de collation son case-sensitive: 'c' no es 'C'."""
    _install(monkeypatch)
    sid = _server(admin_client, 5503)
    assert _create(admin_client, sid, collation="c").status_code == 422
    assert _create(admin_client, sid, collation="C").status_code == 201


# =========================================================================== #
# Inventario                                                                   #
# =========================================================================== #
def test_inventory_exposes_columns_and_available_collations(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 5504)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    r = admin_client.get(f"/api/v1/collation-conversions/{job_id}/objects")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["mode"] == "columns"
    assert data["target_charset"] is None
    # Los objetos congelados NO existen en PostgreSQL.
    assert data["objects"] == []
    # Cada tabla trae sus columnas de texto con la collation actual.
    users = next(t for t in data["tables"] if t["name"] == "users")
    cols = {c["name"]: c for c in users["columns"]}
    assert cols["email"]["is_default_collation"] is True
    assert cols["email"]["current_collation"] is None
    assert cols["email"]["data_type"] == "character varying(255)"
    assert cols["nombre"]["current_collation"] == OTHER_CO
    # El catálogo de collations viene del servidor, para armar el selector.
    names = {c["name"] for c in data["available_collations"]}
    assert TARGET_CO in names and "C" in names


def test_inventory_warns_about_nondeterministic_target(admin_client, monkeypatch):
    """Una collation no determinista rompe LIKE/regex en PostgreSQL 12–17: hay que avisar."""
    _install(monkeypatch)
    sid = _server(admin_client, 5505)
    job_id = _create(admin_client, sid, collation="nd-icu").json()["data"]["id"]
    data = admin_client.get(
        f"/api/v1/collation-conversions/{job_id}/objects"
    ).json()["data"]
    assert any("NO DETERMINISTA" in w and "LIKE" in w for w in data["warnings"])


def test_summary_groups_by_column_collation(admin_client, monkeypatch):
    """
    El resumen del modo columns agrupa por collation de COLUMNA (no de tabla, que en
    PostgreSQL no existe): cuántas columnas y en cuántas tablas.
    """
    inv = _inventory()
    inv.summary = PostgresAdapter._collation_summary(inv.tables)
    _install(monkeypatch, inv)
    sid = _server(admin_client, 5506)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    data = admin_client.get(
        f"/api/v1/collation-conversions/{job_id}/objects"
    ).json()["data"]
    by_coll = {g["collation"]: g for g in data["summary"]}
    # 'C' aparece en users.nombre y orders.codigo → 2 columnas en 2 tablas.
    assert by_coll[OTHER_CO]["column_count"] == 2
    assert by_coll[OTHER_CO]["table_count"] == 2
    # La collation heredada (default de la BD) se reporta como null.
    assert by_coll[None]["column_count"] == 1


# =========================================================================== #
# Preview                                                                      #
# =========================================================================== #
def test_preview_emits_one_alter_per_table_with_all_its_columns(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 5507)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    r = _preview(admin_client, job_id, tables=["users", "orders", "already_ok"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["mode"] == "columns"
    # NUNCA hay ALTER DATABASE, aunque include_database_default venga en su default (True).
    assert data["include_database_default"] is False
    assert all(s["object_type"] != "database" for s in data["steps"])
    assert not any("ALTER DATABASE" in (s["sql"] or "") for s in data["steps"])

    steps = {s["object_name"]: s for s in data["steps"]}
    # Una sola sentencia por tabla, con TODAS sus columnas pendientes (y solo esas).
    assert steps["users"]["action"] == "convert_columns"
    assert steps["users"]["columns"] == ["email", "nombre"]  # 'ya_ok' ya está al día
    sql = steps["users"]["sql"]
    assert sql.count("ALTER TABLE") == 1
    assert sql.count("ALTER COLUMN") == 2
    assert f'COLLATE "{TARGET_CO}"' in sql
    assert 'SET DATA TYPE character varying(255) COLLATE' in sql
    # Una tabla con todas sus columnas al día se saltea.
    assert steps["already_ok"]["action"] == "skip"
    assert data["tables_to_convert"] == 2
    assert data["tables_skipped"] == 1
    assert data["columns_to_convert"] == 3


def test_preview_rejects_object_selection(admin_client, monkeypatch):
    """PostgreSQL no recrea objetos: una selección de objetos se rechaza, no se ignora."""
    _install(monkeypatch)
    sid = _server(admin_client, 5508)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    r = _preview(
        admin_client, job_id, tables=["users"],
        objects=[{"object_type": "view", "name": "v_x"}],
    )
    assert r.status_code == 422, r.text
    assert "dinámicamente" in r.json()["detail"]["msg"]


def test_preview_warns_about_lock_and_index_rebuild(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 5509)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    data = _preview(admin_client, job_id, tables=["users"]).json()["data"]
    assert any("ACCESS EXCLUSIVE" in w for w in data["warnings"])
    assert any("RECONSTRUYE" in w for w in data["warnings"])
    # Y explica que el ENCODING/LC_COLLATE no se toca.
    assert any("ALTER DATABASE" in w and "inmutable" not in w.lower() or "ENCODING" in w
               for w in data["warnings"])


def test_preview_warns_partial_selection_with_postgres_semantics(admin_client, monkeypatch):
    """
    El aviso de conversión parcial tiene la semántica de PostgreSQL: el DDL NO falla (a
    diferencia de MySQL/MariaDB), el error aparece al CONSULTAR (42P22 / 42P21).
    """
    _install(monkeypatch)
    sid = _server(admin_client, 5510)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    data = _preview(admin_client, job_id, tables=["users"]).json()["data"]  # falta 'orders'
    hit = next(w for w in data["warnings"] if "orders" in w)
    assert "42P22" in hit and "42P21" in hit
    assert "no rechaza el DDL" in hit


def test_preview_warns_when_a_text_fk_ends_with_mixed_collations(admin_client, monkeypatch):
    """
    FK de texto con un lado convertido y el otro no: PostgreSQL (≤17) no lo valida ni
    revalida el constraint, así que el aviso es la única señal.
    """
    fake = _install(monkeypatch)
    fake.foreign_keys = [
        CollatableForeignKey(
            constraint="fk_orders_users", table="orders", column="codigo",
            referenced_table="users", referenced_column="nombre",
        )
    ]
    sid = _server(admin_client, 5511)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    # Solo 'orders' → la punta 'users.nombre' queda en 'C' y la otra en el objetivo.
    data = _preview(admin_client, job_id, tables=["orders"]).json()["data"]
    hit = next((w for w in data["warnings"] if "FOREIGN KEY" in w), None)
    assert hit is not None, data["warnings"]
    assert "orders" in hit and "users" in hit
    assert "18" in hit  # PostgreSQL 18 sí lo exige (y pg_upgrade fallaría)

    # Con las DOS tablas, la FK queda consistente y el aviso desaparece.
    data2 = _preview(admin_client, job_id, tables=["orders", "users"]).json()["data"]
    assert not any("FOREIGN KEY" in w for w in data2["warnings"])


def test_preview_reports_missing_tables_without_aborting(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 5512)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    data = _preview(admin_client, job_id, tables=["users", "fantasma"]).json()["data"]
    assert data["missing_tables"] == ["fantasma"]
    assert data["tables_to_convert"] == 1


# =========================================================================== #
# Ejecución                                                                    #
# =========================================================================== #
def test_full_run_converts_columns_and_never_touches_objects(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 5513)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, tables=["users", "orders", "already_ok"]
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    summary = admin_client.get(f"/api/v1/collation-conversions/{job_id}").json()["data"]
    assert summary["status"] == "succeeded", summary
    assert summary["phase"] == "done"
    assert summary["mode"] == "columns"

    items = {i["object_name"]: i for i in _items(admin_client, job_id)}
    assert items["users"]["status"] == "ok"
    assert items["users"]["columns_affected"] == 2
    assert items["orders"]["columns_affected"] == 1
    assert items["already_ok"]["status"] == "skipped"
    # Ningún ítem de base de datos ni de objeto: esas fases no existen en este modo.
    assert all(i["object_type"] == "table" for i in _items(admin_client, job_id))

    # NADA de SET NAMES (es MySQL/MariaDB) ni de ALTER DATABASE, y un solo lote por tabla.
    flat = [s for batch in _FakeRunner.executed for s in batch]
    assert flat, _FakeRunner.executed
    assert all(len(batch) == 1 for batch in _FakeRunner.executed)
    assert not any("SET NAMES" in s for s in flat)
    assert not any("ALTER DATABASE" in s for s in flat)
    assert all(s.startswith('ALTER TABLE "public".') for s in flat)


def test_one_failing_table_does_not_abort_the_rest(admin_client, monkeypatch):
    """Best-effort por tabla, igual que en el modo universal."""
    _install(monkeypatch)
    _FakeRunner.fail_substrings = ['"orders"']
    sid = _server(admin_client, 5514)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(
        admin_client, job_id, tables=["orders", "users"]
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job_id, token).status_code == 200

    items = {i["object_name"]: i for i in _items(admin_client, job_id)}
    assert items["orders"]["status"] == "error"
    assert items["users"]["status"] == "ok"
    assert admin_client.get(
        f"/api/v1/collation-conversions/{job_id}"
    ).json()["data"]["status"] == "failed"


def test_execute_requires_both_confirmations(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 5515)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(admin_client, job_id, tables=["users"]).json()["data"]["confirm_token"]

    assert _execute(admin_client, job_id, token, name="otra_db").status_code == 422
    assert _execute(admin_client, job_id, "0" * 64).status_code == 422
    assert _execute(admin_client, job_id, token).status_code == 200


def test_empty_plan_is_rejected(admin_client, monkeypatch):
    """
    Sin tablas seleccionadas no hay NADA que ejecutar: en este modo no existe el
    ``ALTER DATABASE`` que en universal alcanzaba para tener un paso.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 5516)
    job_id = _create(admin_client, sid).json()["data"]["id"]
    token = _preview(admin_client, job_id).json()["data"]["confirm_token"]
    r = _execute(admin_client, job_id, token)
    assert r.status_code == 422, r.text
    assert "ningún paso" in r.json()["detail"]["msg"]


def test_stale_fingerprint_detects_a_new_text_column(admin_client, monkeypatch):
    """
    La huella anti-TOCTOU incluye las COLUMNAS: si aparece una columna de texto nueva entre
    el plan y el preview, el plan que el operador vio ya no describe la BD.
    """
    fake = _install(monkeypatch)
    sid = _server(admin_client, 5517)
    job_id = _create(admin_client, sid).json()["data"]["id"]

    users = next(t for t in fake.inventory.tables if t.name == "users")
    users.columns.append(_col("apellido", "text", OTHER_CO))

    r = _preview(admin_client, job_id, tables=["users"])
    assert r.status_code == 409, r.text
    assert "cambió" in r.json()["detail"]["msg"]
    # force=true adopta el inventario nuevo y la columna nueva entra en el plan.
    r2 = _preview(admin_client, job_id, tables=["users"], force=True)
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["steps"][0]["columns"] == ["email", "nombre", "apellido"]
