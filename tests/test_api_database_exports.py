"""
Tests de la API de exportación de bases de datos (módulo 10, endpoints 1–5).

El motor se mockea entero: SQLite como BD de metadatos del gateway (fixture ``client``) y
un ``_FakeAdapter`` en memoria para el plano en vivo, instalado con
``monkeypatch.setattr(ec, "get_adapter", ...)`` — mismo patrón que
``tests/test_api_database_clones.py``.

Lo que se cubre acá es exactamente lo que F2 entrega: planificación pura. No hay ejecución,
ni artefacto, ni descarga, así que tampoco hay tests de eso.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import app.controllers.export_controller as ec
from app.services.db_admin.dtos import (
    ColumnInfo,
    ConnectionInfo,
    ForeignKeyInfo,
    RoutineInfo,
    SchemaSnapshot,
    TableSchema,
    TableStat,
    ViewInfo,
)

_DB = "tienda"


# --------------------------------------------------------------------------- #
# Inventario y dobles                                                          #
# --------------------------------------------------------------------------- #
def _server(admin_client, port, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}",
            "host": "10.0.0.5",
            "port": port,
            "engine": engine,
            "root_username": "root",
            "root_password": "pw",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _snapshot(db=_DB, engine="mysql") -> SchemaSnapshot:
    """
    Catálogo mínimo pero representativo: una FK (padre/hija), una vista que lee de una
    tabla, una rutina y la tabla interna del gateway que SIEMPRE debe quedar fuera.
    """
    parent = TableSchema(
        database=db,
        table="parent",
        columns=[ColumnInfo(name="id", type="int", nullable=False, primary_key=True)],
        primary_key=["id"],
        foreign_keys=[],
        indexes=[],
        storage_options={"charset": "utf8mb4", "collation": "utf8mb4_general_ci"},
    )
    child = TableSchema(
        database=db,
        table="child",
        columns=[
            ColumnInfo(name="id", type="int", nullable=False, primary_key=True),
            ColumnInfo(name="pid", type="int", nullable=True),
            ColumnInfo(name="nombre", type="varchar(120)", nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeyInfo(columns=["pid"], referred_table="parent", referred_columns=["id"])
        ],
        indexes=[],
        storage_options={"charset": "utf8mb4", "collation": "utf8mb4_general_ci"},
    )
    logs = TableSchema(
        database=db,
        table="tmp_log",
        columns=[ColumnInfo(name="linea", type="text", nullable=True)],
        primary_key=[],
        foreign_keys=[],
        indexes=[],
    )
    internal = TableSchema(
        database=db,
        table="_gw_v_tienda",
        columns=[ColumnInfo(name="version_num", type="varchar(32)", nullable=False)],
        primary_key=[],
        foreign_keys=[],
        indexes=[],
    )
    return SchemaSnapshot(
        database=db,
        source_engine=engine,
        tables=[parent, child, logs, internal],
        views=[ViewInfo(name="v_parent", definition="select `id` from `parent`")],
        routines=[
            RoutineInfo(name="sp_x", kind="PROCEDURE", body="CREATE PROCEDURE sp_x() BEGIN END")
        ],
    )


class _FakeAdapter:
    """Adapter en memoria: catálogo, estadísticas y versión, sin motor real."""

    def __init__(self, dialect="mysql", db=_DB):
        self.dialect = dialect
        self.db = db
        self.snapshot = _snapshot(db=db, engine=dialect)
        self.databases = [db]

    def list_databases(self):
        return list(self.databases)

    def structural_snapshot(self, database, *, conn=None):
        # ``conn`` es la inyección de conexión del §6.4: el worker lee el catálogo DENTRO de
        # la transacción de consistencia. El doble lo acepta y lo ignora.
        return self.snapshot

    def list_table_stats(self, database):
        return [
            TableStat(table="parent", estimated_rows=10, has_primary_key=True),
            TableStat(table="child", estimated_rows=250, has_primary_key=True),
            # Sin PK a propósito: dispara el aviso de determinismo degradado (§8.3).
            TableStat(table="tmp_log", estimated_rows=5000, has_primary_key=False),
        ]

    def test_connection(self):
        return ConnectionInfo(ok=True, dialect=self.dialect, server_version="8.0.36")

    def export_row_order_by(self, table):
        # Lo usa el writer REAL de los formatos de datos (csv/json/ndjson), que sí corre en
        # el test del ciclo completo con csv: el fake solo sustituye las lecturas al motor.
        return list(table.primary_key)

    def export_supported_types(self):
        if self.dialect == "postgresql":
            return frozenset(
                {"table", "view", "routine", "trigger", "materialized_view",
                 "sequence", "enum_type", "extension"}
            )
        return frozenset({"table", "view", "routine", "trigger", "event"})


def _install(monkeypatch, dialect="mysql") -> _FakeAdapter:
    fake = _FakeAdapter(dialect=dialect)
    monkeypatch.setattr(ec, "get_adapter", lambda target: fake)
    return fake


def _create(admin_client, sid, spec=None, db=_DB):
    return admin_client.post(
        f"/api/v1/servers/{sid}/databases/{db}/database-exports", json=spec or {}
    )


def _plan(admin_client, sid, spec=None, db=_DB) -> int:
    r = _create(admin_client, sid, spec, db)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _public(resp) -> dict:
    return resp.json()["detail"].get("public_context", {})


# =========================================================================== #
# 1) Capacidades                                                              #
# =========================================================================== #
def test_requires_auth(client, monkeypatch):
    _install(monkeypatch)
    assert client.get(f"/api/v1/servers/1/databases/{_DB}/export-capabilities").status_code == 401
    assert client.post(f"/api/v1/servers/1/databases/{_DB}/database-exports", json={}).status_code == 401
    assert client.get("/api/v1/database-exports/1/objects").status_code == 401
    assert client.post("/api/v1/database-exports/1/resolve-selection", json={}).status_code == 401
    assert client.post("/api/v1/database-exports/1/preview", json={}).status_code == 401


def test_capabilities_mysql(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3700)
    r = admin_client.get(f"/api/v1/servers/{sid}/databases/{_DB}/export-capabilities")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["engine"] == "mysql"
    assert data["engine_version"] == "8.0.36"
    assert "event" in data["object_types"]
    assert "materialized_view" not in data["object_types"]
    # La matriz publicada es la que el servidor hace cumplir: no puede venir vacía.
    assert data["compatibility"]
    # El DEFINER sí aplica en la familia MySQL, y el default (``auto``) se publica YA
    # RESUELTO para este motor: el cliente tiene que poder mandarlo tal cual.
    assert data["options"]["sanitize.definer"]["applicable"] is True
    assert data["options"]["sanitize.definer"]["default"] == "omit"
    assert "auto" in data["options"]["sanitize.definer"]["values"]
    assert data["limits"]["inline_max_bytes"] > 0
    assert "export.incompatible_option" in data["error_codes"]


def test_capabilities_postgresql_definer_not_applicable(admin_client, monkeypatch):
    """
    En PostgreSQL el DEFINER no es el mismo concepto y omit/replace son un 422. Si
    capabilities publicara el default global ('omit'), un formulario que no toque la opción
    armaría un spec que el servidor rechaza siempre.
    """
    _install(monkeypatch, dialect="postgresql")
    sid = _server(admin_client, 5432, engine="postgresql")
    r = admin_client.get(f"/api/v1/servers/{sid}/databases/{_DB}/export-capabilities")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["options"]["sanitize.definer"]["applicable"] is False
    assert data["options"]["sanitize.definer"]["default"] == "keep"
    assert "materialized_view" in data["object_types"]
    assert "event" not in data["object_types"]
    assert data["scope"]["scope_note"]


def test_capabilities_unknown_server_404(admin_client, monkeypatch):
    _install(monkeypatch)
    assert admin_client.get(
        f"/api/v1/servers/9999/databases/{_DB}/export-capabilities"
    ).status_code == 404


def test_capabilities_unknown_database_404(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3701)
    assert admin_client.get(
        f"/api/v1/servers/{sid}/databases/no_existe/export-capabilities"
    ).status_code == 404


def test_reserved_database_is_rejected(admin_client, monkeypatch):
    """Exportar `mysql` no es un caso de uso: el guard de BDs de sistema responde 409."""
    _install(monkeypatch)
    sid = _server(admin_client, 3702)
    r = admin_client.get(f"/api/v1/servers/{sid}/databases/mysql/export-capabilities")
    assert r.status_code == 409, r.text


# =========================================================================== #
# 2) Crear plan                                                               #
# =========================================================================== #
def test_create_plan_defaults(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3710)
    r = _create(admin_client, sid)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["status"] == "pending"
    assert data["engine"] == "mysql"
    assert data["format"] == "sql"
    assert data["has_resolved_selection"] is False
    assert data["expired"] is False


def test_create_plan_rejects_matrix_violation(admin_client, monkeypatch):
    """csv no transporta estructura: pedir DDL con ese formato es 422 accionable."""
    _install(monkeypatch)
    sid = _server(admin_client, 3711)
    r = _create(
        admin_client,
        sid,
        {"format": "csv", "structure": {"entity_ddl": "CREATE"}},
    )
    assert r.status_code == 422, r.text
    pub = _public(r)
    assert pub["code"] == "export.incompatible_option"
    assert pub["field"].startswith("structure.")
    assert "NONE" in pub["allowed"]


def test_create_plan_drop_create_requires_matching_confirmation(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3712)
    # Sin el campo: lo bloquea la matriz (requires).
    r = _create(admin_client, sid, {"structure": {"scope_ddl": "DROP_CREATE"}})
    assert r.status_code == 422, r.text
    assert _public(r)["field"] == "structure.confirm_scope_drop"
    # Con el nombre EQUIVOCADO: lo bloquea el controller, que es quien conoce el real.
    r = _create(
        admin_client,
        sid,
        {"structure": {"scope_ddl": "DROP_CREATE", "confirm_scope_drop": "otra"}},
    )
    assert r.status_code == 422, r.text
    assert _public(r)["field"] == "structure.confirm_scope_drop"
    # Con el nombre correcto: pasa.
    r = _create(
        admin_client,
        sid,
        {"structure": {"scope_ddl": "DROP_CREATE", "confirm_scope_drop": _DB}},
    )
    assert r.status_code == 201, r.text


def test_create_plan_rejects_unknown_filename_token(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3713)
    r = _create(admin_client, sid, {"output": {"filename_template": "{database}-{secreto}"}})
    assert r.status_code == 422, r.text
    assert _public(r)["field"] == "output.filename_template"


def test_idempotency_replays_and_conflicts(admin_client, monkeypatch):
    """
    Misma clave + mismo spec ⇒ el MISMO plan (una exportación es cara para el origen).
    Misma clave + spec distinto ⇒ 409, para que el bug del cliente no quede escondido.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3714)
    spec = {"idempotency_key": "k-1", "data": {"mode": "all"}}
    first = _create(admin_client, sid, spec)
    assert first.status_code == 201, first.text
    again = _create(admin_client, sid, spec)
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == first.json()["data"]["id"]

    other = _create(admin_client, sid, {"idempotency_key": "k-1", "data": {"mode": "none"}})
    assert other.status_code == 409, other.text
    assert _public(other)["code"] == "export.idempotency_conflict"


# =========================================================================== #
# 3) Catálogo                                                                 #
# =========================================================================== #
def test_objects_lists_catalog_and_hides_gateway_tables(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3720)
    job = _plan(admin_client, sid)
    r = admin_client.get(f"/api/v1/database-exports/{job}/objects")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    names = {o["name"] for o in data["objects"]}
    assert {"parent", "child", "tmp_log", "v_parent", "sp_x"} <= names
    # La contabilidad interna del gateway NUNCA es esquema del usuario.
    assert "_gw_v_tienda" not in names
    assert "_gw_v_tienda" in data["excluded_internal"]
    child = next(o for o in data["objects"] if o["name"] == "child")
    assert child["estimated_rows"] == 250
    assert child["has_primary_key"] is True
    assert child["collation"] == "utf8mb4_general_ci"
    assert child["size_bytes"] is None  # sin fuente en el adapter; se declara, no se inventa


def test_objects_filters_by_type_and_name(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3721)
    job = _plan(admin_client, sid)
    r = admin_client.get(f"/api/v1/database-exports/{job}/objects?object_type=view")
    assert r.status_code == 200
    assert [o["name"] for o in r.json()["data"]["objects"]] == ["v_parent"]
    r = admin_client.get(f"/api/v1/database-exports/{job}/objects?name_like=CHIL")
    assert [o["name"] for o in r.json()["data"]["objects"]] == ["child"]


def test_objects_unknown_job_404(admin_client, monkeypatch):
    _install(monkeypatch)
    assert admin_client.get("/api/v1/database-exports/4242/objects").status_code == 404


def test_expired_plan_is_410(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3722)
    job = _plan(admin_client, sid)
    _expire(job)
    r = admin_client.get(f"/api/v1/database-exports/{job}/objects")
    assert r.status_code == 410, r.text
    assert _public(r)["code"] == "export.artifact_expired"
    assert admin_client.post(f"/api/v1/database-exports/{job}/preview", json={}).status_code == 410


def _expire(job_id: int) -> None:
    """Envejece el plan directamente en la BD del gateway."""
    from app.models.export_job import ExportJob

    controller = ec.ExportController()
    session = controller._session()
    try:
        job = session.get(ExportJob, job_id)
        job.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        session.commit()
    finally:
        session.close()


# =========================================================================== #
# 4) Resolver selección                                                       #
# =========================================================================== #
def test_resolve_rejects_data_outside_structure(admin_client, monkeypatch):
    """Datos de una tabla que no está en la estructura = INSERTs sin su tabla."""
    _install(monkeypatch)
    sid = _server(admin_client, 3730)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection",
        json={
            "selection": {"mode": "include", "names": ["parent"]},
            "data": {"mode": "include", "names": ["child"]},
        },
    )
    assert r.status_code == 422, r.text
    pub = _public(r)
    assert pub["code"] == "export.data_without_structure"
    assert pub["data_without_structure"] == ["child"]


def test_data_only_export_skips_the_subset_rule(admin_client, monkeypatch):
    """
    Sin DDL de ningún nivel la exportación es "solo datos" (§5.3): recargar una tabla que
    ya existe en el destino es legítimo, y es la única forma en que csv/json pueden existir.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3731)
    job = _plan(
        admin_client,
        sid,
        {"structure": {"scope_ddl": "NONE", "entity_ddl": "NONE"}},
    )
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection",
        json={
            "selection": {"mode": "include", "names": ["parent"]},
            "data": {"mode": "include", "names": ["child"]},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["data"] == ["child"]


def test_explicit_selection_reports_missing_dependencies(admin_client, monkeypatch):
    """
    Elegir la hija sin la padre rompe la FK. Con selección EXPLÍCITA no se recorta en
    silencio: 422 con lo que falta y la selección sugerida.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3732)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection",
        json={"selection": {"mode": "include", "names": ["child"]}},
    )
    assert r.status_code == 422, r.text
    pub = _public(r)
    assert pub["code"] == "export.missing_dependencies"
    assert {"object_type": "table", "name": "parent"} in pub["missing_dependencies"]
    assert "parent" in pub["suggested_names"]


def test_auto_resolve_adds_the_missing_dependency(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3733)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection",
        json={
            "selection": {"mode": "include", "names": ["child"]},
            "auto_resolve_dependencies": True,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert {"object_type": "table", "name": "parent"} in data["added"]
    assert {o["name"] for o in data["structure"]} == {"child", "parent"}
    assert data["excluded_by_dependency"] == []


def test_automatic_selection_prunes_instead_of_failing(admin_client, monkeypatch):
    """
    En un modo automático el usuario describió un CRITERIO, no una lista: completar el
    cierre agregando lo que su criterio excluye sería desobedecerlo. Se poda y se informa.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3734)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection",
        json={"selection": {"mode": "all_except", "names": ["parent"]}},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    pruned = {o["name"] for o in data["excluded_by_dependency"]}
    assert "child" in pruned
    assert "child" not in {o["name"] for o in data["structure"]}


def test_resolve_reports_unknown_names(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3735)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection",
        json={"selection": {"mode": "all_except", "names": ["no_existe"]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["unknown_names"] == ["no_existe"]


# =========================================================================== #
# 5) Preview                                                                  #
# =========================================================================== #
def test_preview_orders_objects_and_emits_token(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3740)
    job = _plan(admin_client, sid, {"data": {"mode": "all"}})
    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["confirm_token"]
    names = [o["name"] for o in data["objects"]]
    # La tabla referida sale ANTES de la que la referencia, y los objetos con cuerpo van
    # después de todas las tablas.
    assert names.index("parent") < names.index("child")
    assert names.index("child") < names.index("v_parent")
    assert names.index("child") < names.index("sp_x")
    assert [o["seq"] for o in data["objects"]] == list(range(1, len(names) + 1))
    assert data["estimated_rows"] > 0
    assert set(data["data_tables"]) == {"parent", "child", "tmp_log"}


def test_preview_token_is_stable_and_selection_sensitive(admin_client, monkeypatch):
    """
    El token prueba que lo que se confirma es EXACTAMENTE lo que se previsualizó: con el
    mismo plan no cambia, y con otra selección sí.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3741)
    job = _plan(admin_client, sid)
    a = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={}).json()["data"]
    b = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={}).json()["data"]
    assert a["confirm_token"] == b["confirm_token"]
    c = admin_client.post(
        f"/api/v1/database-exports/{job}/preview",
        json={"spec": {"selection": {"mode": "include", "names": ["parent"]}}},
    ).json()["data"]
    assert c["confirm_token"] != a["confirm_token"]


def test_preview_dry_run_does_not_freeze_anything(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3742)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/preview", json={"dry_run_only": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["confirm_token"] is None


def test_preview_warns_about_mysql_structural_consistency(admin_client, monkeypatch):
    """
    §6.2: en MySQL/MariaDB el punto único en el tiempo cubre datos pero no estructura. Es
    un límite del motor y hay que reportarlo, no taparlo.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3743)
    job = _plan(admin_client, sid)
    data = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={}).json()["data"]
    assert any("ESTRUCTURA" in w for w in data["warnings"])


def test_preview_marks_tables_without_primary_key_as_non_deterministic(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3744)
    job = _plan(admin_client, sid, {"data": {"mode": "all"}})
    data = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={}).json()["data"]
    tmp = next(o for o in data["objects"] if o["name"] == "tmp_log")
    assert tmp["deterministic"] is False
    assert any("clave primaria" in w for w in data["warnings"])


def test_preview_reports_inline_viability_instead_of_failing(admin_client, monkeypatch):
    """
    El modo en línea sobre el tope se INFORMA en el preview para que el cliente lo sepa
    antes de lanzar el job. Truncar en silencio sería peor que fallar.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3745)
    job = _plan(
        admin_client,
        sid,
        {"data": {"mode": "all"}, "output": {"delivery": "inline"}},
    )
    data = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={}).json()["data"]
    assert data["inline_max_bytes"] > 0
    assert isinstance(data["inline_delivery_viable"], bool)


def test_preview_rejects_a_row_filter_that_touches_another_table(admin_client, monkeypatch):
    """
    El ``where`` por objeto es la única entrada libre que roza una consulta: se valida con
    query_policy y el conjunto de tablas del AST debe ser exactamente esa tabla.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3746)
    job = _plan(
        admin_client,
        sid,
        {
            "data": {
                "mode": "include",
                "names": ["child"],
                "per_object": {"child": {"where": "pid IN (SELECT id FROM parent)"}},
            }
        },
    )
    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 422, r.text
    assert _public(r)["code"] == "export.invalid_row_filter"


def test_preview_accepts_a_simple_row_filter(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3747)
    job = _plan(
        admin_client,
        sid,
        {
            "data": {
                "mode": "include",
                "names": ["child"],
                "per_object": {"child": {"where": "nombre IS NOT NULL", "limit": 50}},
            }
        },
    )
    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 200, r.text
    child = next(o for o in r.json()["data"]["objects"] if o["name"] == "child")
    assert child["with_data"] is True
    assert child["estimated_rows"] == 50  # el limit acota la estimación


def test_preview_freezes_the_selection_in_the_job(admin_client, monkeypatch):
    """
    El preview es el punto de congelación del §5.2: la lista explícita de objetos y el
    fingerprint quedan guardados, y son los que el execute va a comparar.
    """
    import json

    from app.models.export_job import ExportJob

    _install(monkeypatch)
    sid = _server(admin_client, 3749)
    job = _plan(admin_client, sid, {"selection": {"mode": "include", "names": ["parent"]}})
    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 200, r.text

    session = ec.ExportController()._session()
    try:
        row = session.get(ExportJob, job)
        frozen = json.loads(row.resolved_selection)
        assert [o["name"] for o in frozen["objects"]] == ["parent"]
        assert row.confirm_token == r.json()["data"]["confirm_token"]
        assert row.source_fingerprint
        # El spec persistido es AUTOSUFICIENTE: se revive sin depender de defaults del
        # código y produce exactamente el mismo spec normalizado.
        from app.services.db_admin.export_spec import ExportSpec

        revived = ExportSpec.from_dict(json.loads(row.spec))
        assert ec.ExportController._spec_json(revived) == row.spec
    finally:
        session.close()


def test_kill_switch_closes_every_endpoint(admin_client, monkeypatch):
    """
    Una exportación es una EXTRACCIÓN de datos en claro: la vía de salida tiene que poder
    cerrarse sin re-desplegar código, y con el switch en False no debe quedar ni una
    puerta abierta.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3750)
    job = _plan(admin_client, sid)
    monkeypatch.setattr(ec, "EXPORT_ENABLED", False)
    assert admin_client.get(
        f"/api/v1/servers/{sid}/databases/{_DB}/export-capabilities"
    ).status_code == 409
    assert _create(admin_client, sid).status_code == 409
    assert admin_client.get(f"/api/v1/database-exports/{job}/objects").status_code == 409
    assert admin_client.post(
        f"/api/v1/database-exports/{job}/resolve-selection", json={}
    ).status_code == 409
    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 409
    assert _public(r)["code"] == "export.disabled"


def test_preview_revalidates_the_matrix_for_a_replacement_spec(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 3748)
    job = _plan(admin_client, sid)
    r = admin_client.post(
        f"/api/v1/database-exports/{job}/preview",
        json={"spec": {"format": "csv", "structure": {"entity_ddl": "CREATE"}}},
    )
    assert r.status_code == 422, r.text
    assert _public(r)["code"] == "export.incompatible_option"


# =========================================================================== #
# 6-12) Ejecución, artefacto y entrega (F4)                                   #
# =========================================================================== #
# Estos tests cubren el PIPELINE de F4 —reclamo del job, spool, artefacto, estados,
# auditoría y entrega—, no el contenido del artefacto: la generación del SQL tiene sus
# propios tests puros (``tests/test_export_writer.py``) y volver a ejercitarla acá, con un
# adapter falso, solo probaría el doble. Por eso el writer se sustituye por un generador de
# trozos controlado: lo que se verifica es qué hace el gateway con lo que el writer produce.


class _FakeSession:
    """Doble de ``ExportSession``: ni conexión ni transacción, la misma interfaz."""

    def __init__(self, *, consistent=False, rows=None):
        self.conn = None
        self.degradations = []
        self.supports_consistent_structure = consistent
        self.deadline_hits = 0
        # Filas por tabla, para los tests que ejercitan el writer REAL (formatos de datos).
        self.rows = rows or {}

    def check_deadline(self):
        self.deadline_hits += 1

    def iter_rows(self, select_sql, *, batch_rows=1000):
        table = select_sql.split(" FROM ")[1].split()[0].strip('`"')
        return iter(self.rows.get(table, ()))

    def scalar(self, sql, params=None):
        return None


def _fake_session_factory(sessions, rows=None):
    from contextlib import contextmanager

    @contextmanager
    def _factory(target, database, *, engine, **kwargs):
        session = _FakeSession(rows=rows)
        sessions.append(session)
        yield session

    return _factory


def _fake_writer(chunks, *, failing_object: str | None = None, rows: int = 0):
    """
    Generador que imita a ``iter_sql``: rinde trozos y va llenando ``stats``.

    ``failing_object`` agrega un ítem en ``error`` con un motivo de vocabulario cerrado —
    exactamente lo que hace el writer real ante un tipo de valor que no puede representar—
    para ejercitar el camino de "artefacto parcial marcado".
    """
    from app.services.db_admin.export_writer import ExportItemStat

    def iter_sql(spec, target, snapshot, adapter, source, stats):
        for seq, obj in enumerate(target.objects, start=1):
            stats.items.append(
                ExportItemStat(
                    seq=seq,
                    object_type=obj.object_type,
                    object_name=obj.name,
                    phase="structure",
                    status="ok",
                    bytes_written=0,
                )
            )
            stats.objects_exported += 1
        if failing_object:
            stats.items.append(
                ExportItemStat(
                    seq=len(stats.items) + 1,
                    object_type="table_data",
                    object_name=failing_object,
                    phase="data",
                    status="error",
                    reason="unsupported_type:Foo",
                    rows_exported=0,
                )
            )
        for chunk in chunks:
            stats.bytes_written += len(chunk.encode("utf-8"))
            yield chunk
        stats.rows_exported = rows
        stats.complete = all(i.status != "error" for i in stats.items)

    return iter_sql


def _install_execution(monkeypatch, *, chunks=None, failing_object=None, rows=0,
                       source_rows=None):
    """
    Deja el pipeline de F4 ejecutable EN LÍNEA dentro del test.

    El runner se vuelve síncrono (``enqueue`` corre el job en el hilo del test) para que el
    ciclo completo sea observable sin dormir ni sondear; el resto del código —reclamo
    atómico, guard, spool, estados— es exactamente el de producción.
    """
    import app.services.export_runner as export_runner

    sessions: list[_FakeSession] = []
    monkeypatch.setattr(
        ec, "export_session", _fake_session_factory(sessions, rows=source_rows)
    )
    monkeypatch.setattr(
        ec.ewriter,
        "iter_sql",
        _fake_writer(
            chunks if chunks is not None else ["-- artefacto\nSELECT 1;\n"],
            failing_object=failing_object,
            rows=rows,
        ),
    )
    monkeypatch.setattr(
        export_runner, "enqueue", lambda job_id: ec.ExportController().run_job(job_id)
    )
    return sessions


def _ready(admin_client, sid, spec=None) -> tuple[int, str]:
    """Plan + preview: devuelve ``(job_id, confirm_token)`` listos para ejecutar."""
    job = _plan(admin_client, sid, spec)
    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 200, r.text
    return job, r.json()["data"]["confirm_token"]


def _execute(admin_client, job, token, name=_DB):
    return admin_client.post(
        f"/api/v1/database-exports/{job}/execute",
        json={"confirm_target_name": name, "confirm_token": token},
    )


def _artifact_row(job_id):
    from app.models.export_job import ExportArtifact

    session = ec.ExportController()._session()
    try:
        return (
            session.query(ExportArtifact)
            .filter(ExportArtifact.job_id == job_id)
            .one_or_none()
        )
    finally:
        session.close()


def _expire_artifact(job_id):
    from app.models.export_job import ExportArtifact

    session = ec.ExportController()._session()
    try:
        row = session.query(ExportArtifact).filter(ExportArtifact.job_id == job_id).one()
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=1
        )
        session.commit()
    finally:
        session.close()


def test_execution_endpoints_require_auth(client, monkeypatch):
    _install(monkeypatch)
    assert client.post("/api/v1/database-exports/1/execute", json={}).status_code == 401
    assert client.get("/api/v1/database-exports/1").status_code == 401
    assert client.get("/api/v1/database-exports/1/items").status_code == 401
    assert client.post("/api/v1/database-exports/1/cancel").status_code == 401
    assert client.get("/api/v1/database-exports/1/manifest").status_code == 401
    assert client.get("/api/v1/database-exports/1/download").status_code == 401
    assert client.get("/api/v1/database-exports/1/content").status_code == 401


def test_execute_requires_a_previous_preview(admin_client, monkeypatch):
    """Sin preview no hay selección congelada: no hay nada que confirmar."""
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3760)
    job = _plan(admin_client, sid)
    r = _execute(admin_client, job, "0" * 64)
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.not_previewed"


def test_execute_checks_both_confirmation_factors(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3761)
    job, token = _ready(admin_client, sid)

    wrong_name = _execute(admin_client, job, token, name="otra")
    assert wrong_name.status_code == 422, wrong_name.text
    assert _public(wrong_name)["field"] == "confirm_target_name"

    wrong_token = _execute(admin_client, job, "f" * 64)
    assert wrong_token.status_code == 422, wrong_token.text
    assert _public(wrong_token)["field"] == "confirm_token"


def test_execute_rejects_a_changed_schema(admin_client, monkeypatch):
    """
    Anti-TOCTOU: entre el preview y el execute alguien tocó el esquema. El plan congelado
    describe objetos que ya no son los mismos, así que se rechaza en vez de exportar otra
    cosa de la que el operador confirmó.
    """
    fake = _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3762)
    job, token = _ready(admin_client, sid)

    fake.snapshot.tables[0].columns.append(
        ColumnInfo(name="nuevo", type="int", nullable=True)
    )
    r = _execute(admin_client, job, token)
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.fingerprint_changed"


def test_full_cycle_generates_downloads_and_consumes(admin_client, monkeypatch):
    """
    Ciclo completo: plan → preview → execute → polling → manifiesto → descarga → consumido.
    """
    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["-- cabecera\n", "CREATE TABLE t (id int);\n"])
    sid = _server(admin_client, 3763)
    job, token = _ready(admin_client, sid)

    r = _execute(admin_client, job, token)
    assert r.status_code == 200, r.text

    status = admin_client.get(f"/api/v1/database-exports/{job}").json()["data"]
    assert status["status"] == "succeeded"
    assert status["progress"]["bytes"] > 0
    assert status["progress"]["generator_version"]
    assert status["finished_at"]

    items = admin_client.get(f"/api/v1/database-exports/{job}/items").json()
    assert items["pagination"]["total"] >= 1
    assert all(i["status"] == "ok" for i in items["data"])

    manifest = admin_client.get(f"/api/v1/database-exports/{job}/manifest").json()["data"]
    assert manifest["complete"] is True
    assert manifest["sha256"] and manifest["byte_size"] > 0
    assert manifest["objects"]

    row = _artifact_row(job)
    assert row.state == "available"

    dl = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert dl.status_code == 200, dl.text
    assert dl.text == "-- cabecera\nCREATE TABLE t (id int);\n"
    assert dl.headers["x-export-complete"] == "true"
    assert dl.headers["etag"] == f'"{manifest["sha256"]}"'
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.headers["accept-ranges"] == "bytes"

    # Un solo uso: el archivo se borró y el segundo intento es 410 accionable.
    assert _artifact_row(job).state == "consumed"
    again = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert again.status_code == 410, again.text
    assert _public(again)["code"] == "export.artifact_consumed"


def test_partial_artifact_is_always_marked(admin_client, monkeypatch):
    """
    §14: un artefacto parcial NUNCA se entrega sin marca. Tres marcas a la vez, porque cada
    una cubre a un consumidor distinto: el estado del job, el manifiesto, la cabecera y el
    banner dentro del propio archivo (para quien solo mira el .sql).
    """
    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["INSERT INTO t VALUES (1);\n"],
                       failing_object="child")
    sid = _server(admin_client, 3764)
    job, token = _ready(admin_client, sid, {"data": {"mode": "all"}})
    assert _execute(admin_client, job, token).status_code == 200

    status = admin_client.get(f"/api/v1/database-exports/{job}").json()["data"]
    assert status["status"] == "failed"
    assert "incompleto" in (status["error"] or "")

    manifest = admin_client.get(f"/api/v1/database-exports/{job}/manifest").json()["data"]
    assert manifest["complete"] is False
    failed = [o for o in manifest["objects"] if o["status"] == "error"]
    assert failed and failed[0]["reason"] == "unsupported_type:Foo"

    dl = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert dl.status_code == 200, dl.text
    assert dl.headers["x-export-complete"] == "false"
    assert f"EXPORTACIÓN INCOMPLETA — ver el reporte de incidencias del job {job}" in dl.text


def test_download_delivers_nothing_if_the_audit_fails(admin_client, monkeypatch):
    """
    El punto crítico del §9.4: la intención se audita ANTES de abrir el archivo y es
    FAIL-CLOSED. Si el rastro no se persiste, no sale un solo byte — y el artefacto sigue
    intacto, porque tampoco se consumió.
    """
    from app.exceptions import AppHttpException

    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3765)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    storage_name = _artifact_row(job).storage_name

    def _boom(*args, **kwargs):
        raise AppHttpException(message="auditoría caída", status_code=500)

    monkeypatch.setattr(ec.audit, "record_intent", _boom)
    r = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert r.status_code == 500, r.text
    # Ni entregado ni consumido: el artefacto sigue disponible en disco.
    import app.services.export_storage as storage

    assert _artifact_row(job).state == "available"
    assert storage.path_for(storage_name).exists()


def test_inline_content_is_plain_text_and_never_truncates(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["SELECT 1;\n"])
    sid = _server(admin_client, 3766)
    job, token = _ready(admin_client, sid, {"output": {"delivery": "inline"}})
    assert _execute(admin_client, job, token).status_code == 200

    r = admin_client.get(f"/api/v1/database-exports/{job}/content")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    # SIN envoltorio ApiResponse: el cliente lo copia tal cual.
    assert r.text == "SELECT 1;\n"


def test_inline_over_the_cap_is_an_actionable_409(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["SELECT 1;\n" * 50])
    sid = _server(admin_client, 3767)
    job, token = _ready(admin_client, sid, {"output": {"delivery": "inline"}})
    assert _execute(admin_client, job, token).status_code == 200

    monkeypatch.setattr(ec, "EXPORT_INLINE_MAX_BYTES", 10)
    r = admin_client.get(f"/api/v1/database-exports/{job}/content")
    assert r.status_code == 409, r.text
    pub = _public(r)
    assert pub["code"] == "export.inline_too_large"
    assert pub["byte_size"] > 10
    # No se consumió nada: el 409 es previo a la entrega.
    assert _artifact_row(job).state == "available"


def test_cancel_stops_the_worker_and_discards_the_artifact(admin_client, monkeypatch):
    """
    La cancelación es cooperativa: el worker corta en el próximo punto seguro, la sesión de
    consistencia se cierra en su ``finally`` y el spool a medio escribir se BORRA — un
    artefacto parcial sin registro es exactamente el residuo sensible que hay que evitar.
    """
    import app.services.export_storage as storage

    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["-- x\n"] * 500)
    sid = _server(admin_client, 3768)
    job, token = _ready(admin_client, sid)

    assert admin_client.post(f"/api/v1/database-exports/{job}/cancel").status_code == 200
    assert _execute(admin_client, job, token).status_code == 200

    status = admin_client.get(f"/api/v1/database-exports/{job}").json()["data"]
    assert status["status"] == "canceled"
    assert _artifact_row(job) is None
    assert not any(storage.artifact_dir().iterdir())

    r = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.no_artifact"


def test_download_is_409_while_the_job_is_pending(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3769)
    job, _token = _ready(admin_client, sid)
    r = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.not_ready"


def test_expired_artifact_is_410_and_the_purge_removes_the_file(admin_client, monkeypatch):
    """
    El TTL no es una etiqueta: al vencer, la purga BORRA el archivo y deja la fila en
    ``purged`` — la fila sobrevive porque es el rastro de que ese job produjo un artefacto.
    """
    import app.services.export_storage as storage

    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3770)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    storage_name = _artifact_row(job).storage_name
    assert storage.path_for(storage_name).exists()

    _expire_artifact(job)
    r = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert r.status_code == 410, r.text
    assert _public(r)["code"] == "export.artifact_expired"

    assert storage.purge_expired() == 1
    assert not storage.path_for(storage_name).exists()
    assert _artifact_row(job).state == "purged"
    # Idempotente: una segunda pasada no encuentra nada que purgar.
    assert storage.purge_expired() == 0


def test_sweep_orphans_removes_files_without_a_live_row(admin_client, monkeypatch):
    """
    Sin este barrido, un ``kill -9`` a mitad de la generación deja en disco un archivo con
    los datos del origen EN CLARO para siempre. Es la contracara de que el artefacto sea un
    objeto sensible en reposo.
    """
    import app.services.export_storage as storage

    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3771)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    live = storage.path_for(_artifact_row(job).storage_name)

    junk = storage.artifact_dir() / "huerfano-sin-fila"
    junk.write_text("datos", encoding="utf-8")
    partial = storage.artifact_dir() / f"abandonado{storage.PARTIAL_SUFFIX}"
    partial.write_text("a medio escribir", encoding="utf-8")

    assert storage.sweep_orphans() == 2
    assert not junk.exists()
    assert not partial.exists()
    assert live.exists()  # el artefacto vivo NO se toca


def test_kill_switch_closes_execution_and_delivery(admin_client, monkeypatch):
    """
    Con el switch apagado no puede quedar ninguna vía de EXTRACCIÓN abierta. ``cancel`` sí
    sigue disponible a propósito: apagar la exportación y a la vez impedir detener la que
    está corriendo sería lo contrario de lo que el switch persigue.
    """
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3772)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200

    monkeypatch.setattr(ec, "EXPORT_ENABLED", False)
    assert _execute(admin_client, job, token).status_code == 409
    assert admin_client.get(f"/api/v1/database-exports/{job}/download").status_code == 409
    assert admin_client.get(f"/api/v1/database-exports/{job}/content").status_code == 409


def test_execute_is_rejected_when_the_job_already_ran(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3773)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    again = _execute(admin_client, job, token)
    assert again.status_code == 409, again.text
    assert _public(again)["code"] == "export.already_executed"


def test_expired_plan_cannot_be_executed(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3774)
    job, token = _ready(admin_client, sid)
    _expire(job)
    r = _execute(admin_client, job, token)
    assert r.status_code == 410, r.text
    assert _public(r)["code"] == "export.artifact_expired"


def test_download_rejects_an_artifact_of_another_admin(admin_client, monkeypatch):
    """
    Hoy hay un solo admin, así que la comprobación es trivialmente cierta; se prueba igual
    porque el día que exista multiusuario esto no puede ser el agujero que nadie recuerda.
    """
    from app.models.export_job import ExportJob

    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3775)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200

    session = ec.ExportController()._session()
    try:
        session.get(ExportJob, job).created_by_admin_id = 9999
        session.commit()
    finally:
        session.close()

    r = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert r.status_code == 403, r.text
    assert _public(r)["code"] == "export.not_owner"


def test_interrupted_jobs_are_swept_at_startup(admin_client, monkeypatch):
    """Un reinicio a mitad deja el job en ``interrupted``, no colgado en ``running``."""
    from app.models.export_job import ExportJob

    _install(monkeypatch)
    sid = _server(admin_client, 3776)
    job = _plan(admin_client, sid)
    controller = ec.ExportController()
    session = controller._session()
    try:
        session.get(ExportJob, job).status = "running"
        session.commit()
    finally:
        session.close()

    assert controller.sweep_interrupted() == 1
    data = admin_client.get(f"/api/v1/database-exports/{job}").json()["data"]
    assert data["status"] == "interrupted"
    assert data["error"]


def test_cancel_is_rejected_once_the_job_finished(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3777)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    r = admin_client.post(f"/api/v1/database-exports/{job}/cancel")
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.not_cancellable"


def test_concurrency_cap_rejects_a_second_running_export(admin_client, monkeypatch):
    """
    Sin techo, un cliente puede encolar cientos de jobs y dejar al origen leyéndose durante
    días: es un vector de degradación y de exfiltración lenta (§9.7).
    """
    from app.models.export_job import ExportJob

    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3778)
    busy = _plan(admin_client, sid)
    session = ec.ExportController()._session()
    try:
        session.get(ExportJob, busy).status = "running"
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(ec, "EXPORT_MAX_CONCURRENT_GLOBAL", 1)
    job, token = _ready(admin_client, sid)
    r = _execute(admin_client, job, token)
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.quota_exceeded"


# =========================================================================== #
# F5) La matriz, exigida en el SERVIDOR                                       #
# =========================================================================== #
# §17: publicar la matriz no alcanza — cada combinación prohibida tiene que ser rechazada
# por el servidor, con un código estable y la opción culpable en ``public_context`` (que es
# lo único visible en producción; ``context`` solo se ve en development).

_CSV_BASE = {
    "format": "csv",
    "structure": {"scope_ddl": "NONE", "entity_ddl": "NONE"},
    "data": {"mode": "all", "insert_variant": "none"},
    "sanitize": {"session_preamble": False},
    "output": {"organization": "per_object"},
}


def _merge(base: dict, patch: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def _csv_spec(**patch) -> dict:
    return _merge(_CSV_BASE, patch)


def _data_spec(fmt: str, **patch) -> dict:
    base = {
        "format": fmt,
        "structure": {"scope_ddl": "NONE", "entity_ddl": "NONE"},
        "data": {"mode": "all", "insert_variant": "none"},
        "sanitize": {"session_preamble": False},
    }
    return _merge(base, patch)


@pytest.mark.parametrize(
    "spec,campo",
    [
        # csv: ni una sola opción de script
        (_csv_spec(structure={"entity_ddl": "CREATE"}), "structure.entity_ddl"),
        (_csv_spec(structure={"scope_ddl": "CREATE"}), "structure.scope_ddl"),
        (_csv_spec(data={"insert_variant": "insert"}), "data.insert_variant"),
        (_csv_spec(sanitize={"session_preamble": True}), "sanitize.session_preamble"),
        (_csv_spec(sanitize={"transaction_wrap": True}), "sanitize.transaction_wrap"),
        (
            _csv_spec(
                sanitize={
                    "charset_override": {
                        "mode": "override", "charset": "utf8mb4",
                        "collation": "utf8mb4_general_ci",
                    }
                }
            ),
            "sanitize.charset_override.mode",
        ),
        # csv: fuerza un archivo por tabla, y por lo tanto no admite la entrega en línea
        (_csv_spec(output={"organization": "single"}), "output.organization"),
        (_csv_spec(output={"delivery": "inline"}), "output.delivery"),
        (_csv_spec(output={"schema_manifest": True}), "output.schema_manifest"),
        # json/ndjson: estructura solo como manifiesto, nunca ejecutable
        (_data_spec("json", structure={"entity_ddl": "CREATE"}), "structure.entity_ddl"),
        (_data_spec("ndjson", structure={"entity_ddl": "DROP_CREATE"}), "structure.entity_ddl"),
        (_data_spec("json", data={"insert_variant": "replace"}), "data.insert_variant"),
        (_data_spec("ndjson", sanitize={"transaction_wrap": True}), "sanitize.transaction_wrap"),
        # json no se puede partir: un trozo de un documento JSON no es JSON
        (_data_spec("json", output={"split_max_bytes": 4096}), "output.split_max_bytes"),
        # el manifiesto es de json/ndjson: en sql la estructura ya viaja como DDL
        ({"output": {"schema_manifest": True}}, "output.schema_manifest"),
        # entrega en línea: un solo artefacto, sin comprimir (§10.2)
        ({"output": {"delivery": "inline", "organization": "per_object"}}, "output.organization"),
        ({"output": {"delivery": "inline", "split_max_bytes": 4096}}, "output.split_max_bytes"),
        ({"output": {"delivery": "inline", "compression": "gzip"}}, "output.compression"),
        # gzip comprime un flujo: no es un contenedor
        ({"output": {"compression": "gzip", "organization": "per_object"}}, "output.organization"),
    ],
)
def test_la_matriz_rechaza_cada_combinacion_prohibida(admin_client, monkeypatch, spec, campo):
    _install(monkeypatch)
    sid = _server(admin_client, 3900 + abs(hash(campo + str(spec))) % 800)
    r = _create(admin_client, sid, spec)
    assert r.status_code == 422, r.text
    public = _public(r)
    assert public["code"] == "export.incompatible_option"
    assert campo in public["fields"], public


@pytest.mark.parametrize(
    "csv_opts,campo",
    [
        ({"delimiter": ";;"}, "csv.delimiter"),
        ({"delimiter": ""}, "csv.delimiter"),
        ({"quote_char": ","}, "csv.quote_char"),
        ({"escape_char": '"'}, "csv.escape_char"),
        ({"null_representation": "a,b"}, "csv.null_representation"),
    ],
)
def test_un_dialecto_delimitado_ambiguo_se_rechaza_con_la_opcion_culpable(
    admin_client, monkeypatch, csv_opts, campo
):
    """
    Un separador de dos caracteres o un escape igual a la comilla producen un archivo que no
    se puede volver a leer. Se rechaza al planear, no al escribir la primera fila.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 4700 + abs(hash(campo + str(csv_opts))) % 200)
    r = _create(admin_client, sid, _csv_spec(csv=csv_opts))
    assert r.status_code == 422, r.text
    assert _public(r)["code"] == "export.incompatible_option"
    assert campo in _public(r)["fields"]


def test_un_csv_bien_configurado_si_se_acepta(admin_client, monkeypatch):
    """La contracara obligatoria: la matriz no puede rechazar el uso legítimo del formato."""
    _install(monkeypatch)
    sid = _server(admin_client, 4901)
    r = _create(admin_client, sid, _csv_spec(csv={"delimiter": ";", "bom": True}))
    assert r.status_code == 201, r.text
    assert r.json()["data"]["format"] == "csv"


def test_capabilities_publica_el_dialecto_y_el_empaquetado(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 4902)
    caps = admin_client.get(
        f"/api/v1/servers/{sid}/databases/{_DB}/export-capabilities"
    ).json()["data"]
    assert caps["csv_dialect"]["delimiter"] == ","
    assert "delimiter" in caps["csv_dialect"]["single_char_options"]
    assert caps["packaging"]["container"] == "zip"
    assert caps["packaging"]["part_naming"] == "{base}.part{NN}{ext}"
    assert caps["packaging"]["entry_extension"]["ndjson"] == ".ndjson"
    assert caps["options"]["output.schema_manifest"]["default"] is False
    assert caps["limits"]["max_parts"] >= 1
    # Lo publicado y lo exigido son la MISMA lista: las reglas nuevas también salen acá.
    prohibido = {
        f for regla in caps["compatibility"] for f in regla["forbids"]
    }
    assert "output.schema_manifest" in prohibido
    assert "output.split_max_bytes" in prohibido


def test_el_preview_avisa_del_contenedor_y_del_artefacto_vacio(admin_client, monkeypatch):
    _install(monkeypatch)
    sid = _server(admin_client, 4903)
    job = _plan(admin_client, sid, _data_spec("ndjson", data={"mode": "none"},
                                              output={"organization": "per_object"}))
    avisos = " ".join(
        admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
        .json()["data"]["warnings"]
    )
    assert "no va a llevar ninguna fila" in avisos
    assert "zip" in avisos


def test_ciclo_completo_en_csv_entrega_un_zip_con_un_archivo_por_tabla(
    admin_client, monkeypatch
):
    """
    Ciclo real de F5 con el writer de verdad: el artefacto es un contenedor, la descarga se
    llama ``.zip`` y adentro hay un archivo por tabla con su encabezado.
    """
    import io
    import zipfile

    _install(monkeypatch)
    _install_execution(
        monkeypatch,
        source_rows={"parent": [(1,)], "child": [(1, 1, "hola, mundo"), (2, None, "")]},
    )
    sid = _server(admin_client, 4904)
    job = _plan(admin_client, sid, _csv_spec())
    token = admin_client.post(
        f"/api/v1/database-exports/{job}/preview", json={}
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job, token).status_code == 200

    estado = admin_client.get(f"/api/v1/database-exports/{job}").json()["data"]
    assert estado["status"] == "succeeded", estado

    manifiesto = admin_client.get(f"/api/v1/database-exports/{job}/manifest").json()["data"]
    assert manifiesto["format"] == "csv"
    assert manifiesto["part_count"] >= 3

    dl = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"] == "application/zip"
    assert ".zip" in dl.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert "child.csv" in zf.namelist()
        cuerpo = zf.read("child.csv").decode("utf-8").splitlines()
    assert cuerpo[0] == "id,pid,nombre"
    assert cuerpo[1] == '1,1,"hola, mundo"'
    # NULL (campo vacío sin comillas) y cadena vacía (cuoteada) siguen siendo distintos.
    assert cuerpo[2] == '2,,""'


def test_ciclo_completo_en_ndjson_entrega_un_registro_por_linea(admin_client, monkeypatch):
    _install(monkeypatch)
    _install_execution(monkeypatch, source_rows={"child": [(1, 1, "a"), (2, 2, "b")]})
    sid = _server(admin_client, 4905)
    job = _plan(
        admin_client,
        sid,
        _data_spec("ndjson", data={"mode": "include", "names": ["child"]}),
    )
    token = admin_client.post(
        f"/api/v1/database-exports/{job}/preview", json={}
    ).json()["data"]["confirm_token"]
    assert _execute(admin_client, job, token).status_code == 200

    dl = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"].startswith("application/x-ndjson")
    lineas = [json.loads(x) for x in dl.text.splitlines()]
    assert [x["row"]["id"] for x in lineas] == [1, 2]
    assert all(x["table"] == "child" for x in lineas)


# =========================================================================== #
# Endurecimientos de la revisión de seguridad (B3, R1, R2, R5)                #
# =========================================================================== #
def test_no_se_puede_exportar_la_base_de_metadatos_del_gateway(admin_client, monkeypatch):
    """
    B3: si la BD del gateway vive en un servidor del inventario, el artefacto se llevaría
    ``servers`` (con ``root_password_encrypted``), ``server_users`` y el ``audit_log``
    COMPLETO — que es el único control compensatorio de una exportación sin enmascarado.
    Es el mismo destino que ya bloquea la consola SQL, y se reusa su guard.
    """
    fake = _install(monkeypatch)
    fake.db = "gwmeta"
    fake.databases = ["gwmeta"]
    fake.snapshot = _snapshot(db="gwmeta")
    monkeypatch.setattr(ec, "DB_HOST", "10.0.0.5")
    monkeypatch.setattr(ec, "DB_PORT", 3999)
    monkeypatch.setattr(ec, "DB_NAME", "gwmeta")
    sid = _server(admin_client, 3999)

    for resp in (
        admin_client.get(f"/api/v1/servers/{sid}/databases/gwmeta/export-capabilities"),
        _create(admin_client, sid, db="gwmeta"),
    ):
        assert resp.status_code == 409, resp.text
        assert _public(resp)["code"] == "export.scope_not_allowed"


def test_otra_base_del_mismo_servidor_si_se_puede_exportar(admin_client, monkeypatch):
    """El guard es por BASE, no por servidor: bloquear el servidor entero sería de más."""
    _install(monkeypatch)
    monkeypatch.setattr(ec, "DB_HOST", "10.0.0.5")
    monkeypatch.setattr(ec, "DB_PORT", 3998)
    monkeypatch.setattr(ec, "DB_NAME", "gwmeta")
    sid = _server(admin_client, 3998)
    assert _create(admin_client, sid).status_code == 201


def test_preview_is_rejected_once_the_job_ran(admin_client, monkeypatch):
    """
    R2: un preview sobre un job ya ejecutado sobrescribía ``spec``/``resolved_selection``/
    ``fingerprint``/``token``, y el ``GET /manifest`` pasaba a describir una selección que
    NO es la del artefacto entregado.
    """
    _install(monkeypatch)
    _install_execution(monkeypatch)
    sid = _server(admin_client, 3990)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200

    r = admin_client.post(f"/api/v1/database-exports/{job}/preview", json={})
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.already_executed"


def test_un_range_que_cubre_todo_consume_el_artefacto(admin_client, monkeypatch):
    """
    R1: ``Range: bytes=0-`` bajaba el archivo ENTERO y no lo consumía — el "un solo uso"
    se anulaba con una cabecera. Y el contador se incrementa en los dos casos.
    """
    _install(monkeypatch)
    _install_execution(monkeypatch)
    monkeypatch.setattr(ec, "EXPORT_SINGLE_USE_DOWNLOAD", True)
    sid = _server(admin_client, 3991)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200

    # Parcial de verdad: ni consume, pero SÍ cuenta.
    parcial = admin_client.get(
        f"/api/v1/database-exports/{job}/download", headers={"Range": "bytes=0-3"}
    )
    assert parcial.status_code == 206, parcial.text
    assert _artifact_row(job).state == "available"
    assert _artifact_row(job).download_count == 1

    completo = admin_client.get(
        f"/api/v1/database-exports/{job}/download", headers={"Range": "bytes=0-"}
    )
    assert completo.status_code in (200, 206), completo.text
    assert _artifact_row(job).download_count == 2
    assert _artifact_row(job).state == "consumed"

    otra = admin_client.get(f"/api/v1/database-exports/{job}/download")
    assert otra.status_code == 410, otra.text
    assert _public(otra)["code"] == "export.artifact_consumed"


def test_el_techo_de_concurrencia_cuenta_la_cola(admin_client, monkeypatch):
    """
    R5: con un solo worker ``running`` nunca pasa de 1, así que contar solo eso dejaba la
    COLA sin acotar — se podían admitir miles de jobs que el worker iba a ejecutar igual.
    """
    from app.services import export_runner

    _install(monkeypatch)
    _install_execution(monkeypatch)
    monkeypatch.setattr(ec, "EXPORT_MAX_CONCURRENT_GLOBAL", 2)
    # Dos jobs ya ADMITIDOS (encolados y sin terminar), ninguno ``running`` en la base.
    monkeypatch.setattr(export_runner, "inflight_count", lambda: 2)

    sid = _server(admin_client, 3992)
    job, token = _ready(admin_client, sid)
    r = _execute(admin_client, job, token)
    assert r.status_code == 409, r.text
    assert _public(r)["code"] == "export.quota_exceeded"


def test_una_codificacion_de_archivo_con_estado_se_rechaza(admin_client, monkeypatch):
    """
    R4 por HTTP: el artefacto se escribe por trozos, así que ``utf-16`` incrusta un BOM en
    cada uno y sale corrupto — con un sha256 que igual lo declara íntegro.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3993)
    r = _create(admin_client, sid, {"output": {"file_encoding": "utf-16"}})
    assert r.status_code == 422, r.text
    pub = _public(r)
    assert pub["code"] == "export.incompatible_option"
    assert "output.file_encoding" in pub["fields"], pub
