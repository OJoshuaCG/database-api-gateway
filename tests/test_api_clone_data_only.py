"""
Tests de la API del clon en modo **solo datos**, del charset elegible y del guard de alcance.

Todos los tests de este archivo existen porque la revisión del diseño encontró un defecto
concreto; el nombre de cada uno dice cuál. Los cuatro que no se pueden perder:

* ``test_data_only_runs_data_phase_and_emits_no_ddl`` — ejercita el **worker**, no el preview.
  La fase de datos se decidía en una cadena ``if/elif`` gobernada por ``clean_mode``, y el
  modo solo datos exige ``clean_mode='none'``: una sub-fase colgada de ahí no se habría
  ejecutado nunca, mostrándose igual en el preview y en el token.
* ``test_narrowing_type_blocks_execution`` — la polaridad de ``is_narrowing``.
* ``test_adopt_target_survives_declarative_full_selection`` — el auto-adopt se apagaba solo al
  resolver la selección declarativa a lista explícita.
* ``test_gateway_metadata_database_is_rejected_on_both_sides`` — el clon no tenía guard de
  alcance: nada impedía apuntarlo a la propia base del gateway.

El motor se mockea (SQLite de metadatos + adapter falso) y el worker corre SÍNCRONO.
"""

import app.controllers.clone_controller as cc
from app.services.db_admin import clone_spec as cspec
from app.services.db_admin.dtos import ColumnInfo, SchemaSnapshot, TableSchema
from tests.test_api_database_clones import (  # noqa: F401 — se reusa el arnés
    _FakeAdapter,
    _install,
    _managed,
    _owner,
    _server,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _tbl(db, name, cols, pk=None):
    return TableSchema(
        database=db, table=name, columns=cols, primary_key=pk or [],
        foreign_keys=[], indexes=[],
    )


def _col(name, type_="int", *, nullable=True, default=None, collation=None):
    return ColumnInfo(
        name=name, type=type_, nullable=nullable, default=default, collation=collation
    )


def _install_data_only(monkeypatch, *, target_cols=None, source_cols=None, engine="mysql"):
    """Origen y destino EXISTENTES con la tabla 'users'; el destino puede diferir."""
    src_cols = source_cols or [_col("id", "int", nullable=False), _col("name", "varchar(50)")]
    tgt_cols = target_cols if target_cols is not None else list(src_cols)
    snaps = {
        "src_db": SchemaSnapshot(
            database="src_db", source_engine=engine,
            tables=[_tbl("src_db", "users", src_cols, pk=["id"])],
        ),
        "dst_db": SchemaSnapshot(
            database="dst_db", source_engine=engine,
            tables=[_tbl("dst_db", "users", tgt_cols, pk=["id"])],
        ),
    }
    fake = _FakeAdapter(snaps, {"src_db", "dst_db"})
    monkeypatch.setattr(cc, "get_adapter", lambda target: fake)
    from app.services import clone_runner
    from tests.test_api_database_clones import _fake_copy_tables, _FakeRunner
    monkeypatch.setattr(cc, "MigrationRunner", _FakeRunner)
    monkeypatch.setattr(cc, "copy_tables", _fake_copy_tables)
    monkeypatch.setattr(
        clone_runner, "enqueue", lambda job_id: cc.CloneController().run_job(job_id)
    )
    return fake


def _plan(admin_client, sid, *, target_mode="existing", clean_mode="none", src_id=None):
    body = {
        "source_database_id": src_id,
        "target_server_id": sid,
        "target_database_name": "dst_db",
        "target_mode": target_mode,
        "clean_mode": clean_mode,
    }
    r = admin_client.post("/api/v1/database-clones", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _setup(admin_client, port):
    sid = _server(admin_client, port)
    oid = _owner(admin_client, sid)
    src_id = _managed(admin_client, sid, oid, "src_db")
    return sid, oid, src_id


def _codes(response) -> str:
    body = response.json()
    return str(body.get("detail", body))


# =========================================================================== #
# Modo solo datos                                                              #
# =========================================================================== #
def test_data_only_preview_emits_no_structure_statements(admin_client, monkeypatch):
    """La afirmación central de la feature: en 'data_only' no se emite una sola DDL."""
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3700)
    job = _plan(admin_client, sid, src_id=src_id)
    r = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["structure_statements"] == []
    assert data["clean_statements"] == []
    assert [t["table"] for t in data["data_tables"]] == ["users"]
    assert data["copy_intent"] == "data_only"
    assert data["data_on_existing"] == "append"
    assert data["blocking_issues"] == []
    assert data["confirm_token"]


def test_data_only_runs_data_phase_and_emits_no_ddl(admin_client, monkeypatch):
    """
    EL test del archivo: ejercita el **worker**, no el preview.

    Un test de preview no ve el defecto que esto cubre: la fase de datos se decidía en una
    cadena ``if/elif`` sobre ``clean_mode``/``target_mode`` y no sobre el plan, así que un
    plan de solo datos podía previsualizarse perfecto y no copiar nada (o copiar en el modo
    equivocado) al ejecutarse.
    """
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3701)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "upsert"}},
    )
    assert pr.status_code == 200, pr.text
    ex = admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={
            "confirm_target_name": "dst_db",
            "confirm_token": pr.json()["data"]["confirm_token"],
        },
    )
    assert ex.status_code == 200, ex.text
    summary = admin_client.get(f"/api/v1/database-clones/{job}").json()["data"]
    assert summary["status"] == "succeeded", summary
    items = admin_client.get(f"/api/v1/database-clones/{job}/items").json()["data"]
    kinds = {i["kind"] for i in items}
    assert "data" in kinds, items
    assert "structure" not in kinds, items
    assert "clean" not in kinds, items
    assert {i["object_name"] for i in items if i["kind"] == "data"} == {"users"}


def test_data_only_requires_existing_target(admin_client, monkeypatch):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3702)
    # La BD destino ya existe, así que target_mode='new' se rechaza antes; se usa otro nombre.
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "nueva_db", "target_mode": "new",
    })
    job = r.json()["data"]["id"]
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    assert pr.status_code == 422
    assert cspec.CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET in _codes(pr)


def test_data_only_requires_explicit_on_existing(admin_client, monkeypatch):
    """
    ``auto`` no se hereda acá: con la estructura creándose la fase de datos nunca llegaba a
    una tabla preexistente, así que 'upsert' por default sería un default destructivo nuevo.
    """
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3703)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all"}},
    )
    assert pr.status_code == 422
    assert cspec.CODE_ON_EXISTING_REQUIRED in _codes(pr)


def test_data_only_with_clean_mode_is_rejected(admin_client, monkeypatch):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3704)
    job = _plan(admin_client, sid, clean_mode="objects", src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    assert pr.status_code == 422
    assert cspec.CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET in _codes(pr)


# =========================================================================== #
# Guard de compatibilidad del destino                                          #
# =========================================================================== #
def test_narrowing_type_blocks_execution(admin_client, monkeypatch):
    """
    Origen ``varchar(50)`` → destino ``varchar(20)``. En MySQL ``LOAD DATA LOCAL`` degrada el
    truncado a warning, así que el motor NO falla: este guard es la única defensa.

    El preview responde **200 con blocking_issues y sin token** (hay que poder VER por qué no
    se puede ejecutar) y el execute rechaza.
    """
    _install_data_only(
        monkeypatch,
        target_cols=[_col("id", "int", nullable=False), _col("name", "varchar(20)")],
    )
    sid, _oid, src_id = _setup(admin_client, 3710)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    assert pr.status_code == 200, pr.text
    data = pr.json()["data"]
    assert data["confirm_token"] == ""
    reasons = {i["reason"] for i in data["blocking_issues"]}
    assert cspec.REASON_TYPE_NARROWING in reasons, data["blocking_issues"]
    ex = admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={"confirm_target_name": "dst_db", "confirm_token": "cualquiera"},
    )
    assert ex.status_code == 422
    assert cspec.CODE_TARGET_SCHEMA_INCOMPATIBLE in _codes(ex)


def test_missing_column_in_target_blocks(admin_client, monkeypatch):
    _install_data_only(
        monkeypatch, target_cols=[_col("id", "int", nullable=False)]
    )
    sid, _oid, src_id = _setup(admin_client, 3711)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    assert pr.status_code == 200, pr.text
    reasons = {i["reason"] for i in pr.json()["data"]["blocking_issues"]}
    assert cspec.REASON_COLUMN_MISSING in reasons


def test_target_not_null_without_default_blocks(admin_client, monkeypatch):
    _install_data_only(
        monkeypatch,
        target_cols=[
            _col("id", "int", nullable=False),
            _col("name", "varchar(50)"),
            _col("tenant_id", "int", nullable=False),
        ],
    )
    sid, _oid, src_id = _setup(admin_client, 3712)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    reasons = {i["reason"] for i in pr.json()["data"]["blocking_issues"]}
    assert cspec.REASON_TARGET_NOT_NULL_NO_DEFAULT in reasons


def test_compatible_target_has_no_blocking_issues(admin_client, monkeypatch):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3713)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    assert pr.json()["data"]["blocking_issues"] == []


# =========================================================================== #
# Avisos                                                                       #
# =========================================================================== #
def test_upsert_without_primary_key_is_warned(admin_client, monkeypatch):
    """Sin PK el upsert degrada a INSERT simple: reejecutar el job duplicaría filas."""
    fake = _install_data_only(monkeypatch)
    for snap in (fake.snaps["src_db"], fake.snaps["dst_db"]):
        snap.tables[0].primary_key = []
    sid, _oid, src_id = _setup(admin_client, 3720)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "upsert"}},
    )
    codes = {n["code"] for n in pr.json()["data"]["notices"]}
    assert cspec.WARN_UPSERT_WITHOUT_PRIMARY_KEY in codes


def test_target_triggers_are_warned_in_mysql(admin_client, monkeypatch):
    """
    Los triggers del DESTINO disparan durante la copia y en MySQL/MariaDB no hay forma
    portable de desactivarlos (``FOREIGN_KEY_CHECKS=0`` no los apaga).
    """
    from app.services.db_admin.dtos import TriggerInfo
    fake = _install_data_only(monkeypatch)
    fake.snaps["dst_db"].triggers = [
        TriggerInfo(
            name="trg_users_ai", table="users", timing="AFTER", events=["INSERT"],
            action="CREATE TRIGGER trg_users_ai AFTER INSERT ON users FOR EACH ROW BEGIN END",
        )
    ]
    sid, _oid, src_id = _setup(admin_client, 3721)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    notices = pr.json()["data"]["notices"]
    codes = {n["code"] for n in notices}
    assert cspec.WARN_TARGET_TRIGGERS_WILL_FIRE in codes
    msg = next(n["message"] for n in notices if n["code"] == cspec.WARN_TARGET_TRIGGERS_WILL_FIRE)
    assert "trg_users_ai" in msg


# =========================================================================== #
# Guard de alcance                                                             #
# =========================================================================== #
def test_system_database_is_rejected_as_target(admin_client, monkeypatch):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3730)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "mysql", "target_mode": "existing",
    })
    assert r.status_code == 409, r.text


def test_gateway_metadata_database_is_rejected_on_both_sides(admin_client, monkeypatch):
    """
    Nada impedía apuntar un clon a la propia base del gateway: con ``clean_mode='objects'``
    eso dropea ``audit_log``/``servers``/``server_users`` — el inventario, las credenciales
    pseudo-root cifradas y la auditoría, que es el único control compensatorio del repo.

    Se verifica que el guard corra para los DOS lados, que es el defecto real (no corría para
    ninguno).
    """
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3731)
    seen: list[str] = []

    def _fake_guard(*, host, port, database, gateway_host, gateway_port, gateway_database):
        seen.append(database)
        return database == "dst_db"

    monkeypatch.setattr(cc.query_policy, "is_gateway_metadata_target", _fake_guard)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "existing",
    })
    assert r.status_code == 409, r.text
    assert cspec.CODE_SCOPE_NOT_ALLOWED in _codes(r)
    # El guard se consultó para el ORIGEN también, no solo para el destino.
    assert "src_db" in seen


# =========================================================================== #
# Selección declarativa                                                        #
# =========================================================================== #
def test_adopt_target_survives_declarative_full_selection(admin_client, monkeypatch):
    """
    ``selection is None`` era el proxy de "clon completo" en dos lugares. Al resolver una
    selección declarativa a lista explícita, ``adopt_target`` daba 422 pidiendo el clon
    completo y ``will_adopt`` quedaba en False para siempre **sin que nada falle**.
    """
    fake = _install_data_only(monkeypatch)
    fake.existing.discard("dst_db")
    del fake.snaps["dst_db"]
    sid = _server(admin_client, 3740)
    oid = _owner(admin_client, sid)
    mr = admin_client.post("/api/v1/database-models", json={
        "name": "bp", "slug": "bp", "description": "x",
    })
    assert mr.status_code == 201, mr.text
    src_id = _managed(admin_client, sid, oid, "src_db", model_id=mr.json()["data"]["id"])
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "new",
        "adopt_target": True, "adopt_owner_id": oid,
    })
    assert r.status_code == 201, r.text
    job = r.json()["data"]["id"]
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"structure": {"mode": "all"}, "copy_intent": "structure_only"},
    )
    assert pr.status_code == 200, pr.text
    assert pr.json()["data"]["will_adopt"] is True, pr.json()["data"]


def test_declarative_exclude_pattern_narrows_the_selection(admin_client, monkeypatch):
    fake = _install_data_only(monkeypatch)
    fake.snaps["src_db"].tables.append(
        _tbl("src_db", "log_events", [_col("id", "int", nullable=False)], pk=["id"])
    )
    sid, _oid, src_id = _setup(admin_client, 3741)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "data_only",
            "data": {"mode": "all_except", "exclude_patterns": ["log_*"],
                     "on_existing": "append"},
        },
    )
    assert pr.status_code == 200, pr.text
    assert [t["table"] for t in pr.json()["data"]["data_tables"]] == ["users"]


def test_unknown_name_in_explicit_selection_is_422(admin_client, monkeypatch):
    """Un nombre mal tecleado daba un job 'succeeded' con 0 filas copiadas."""
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3742)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "data_only",
            "data": {"mode": "include", "names": ["pedidos_2024"], "on_existing": "append"},
        },
    )
    assert pr.status_code == 422
    assert cspec.CODE_UNKNOWN_NAMES in _codes(pr)


def test_data_selection_pulls_the_fk_parent(admin_client, monkeypatch):
    """
    El conjunto de tablas con datos se DERIVABA del cierre de estructura, así que copiar la
    hija arrastraba al padre por construcción. Con el eje de datos independiente esa
    invariante había que reponerla: sin ella se insertan hijos sin padre y no falla, porque la
    fase de datos corre con las FKs desactivadas y el motor nunca las revalida.
    """
    from app.services.db_admin.dtos import ForeignKeyInfo
    fake = _install_data_only(monkeypatch)
    for db in ("src_db", "dst_db"):
        parent = _tbl(db, "parent", [_col("id", "int", nullable=False)], pk=["id"])
        child = _tbl(
            db, "child",
            [_col("id", "int", nullable=False), _col("pid", "int")], pk=["id"],
        )
        child.foreign_keys = [
            ForeignKeyInfo(columns=["pid"], referred_table="parent", referred_columns=["id"])
        ]
        fake.snaps[db].tables = [parent, child]
    sid, _oid, src_id = _setup(admin_client, 3743)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "data_only",
            "data": {"mode": "include", "names": ["child"], "on_existing": "append"},
        },
    )
    assert pr.status_code == 200, pr.text
    tables = [t["table"] for t in pr.json()["data"]["data_tables"]]
    assert "parent" in tables and "child" in tables, tables
    # Y el padre va PRIMERO (orden topológico).
    assert tables.index("parent") < tables.index("child")


def test_preview_without_selection_does_not_wipe_the_frozen_one(admin_client, monkeypatch):
    """
    ``preview`` persistía ``selection = None`` en cada llamada, así que un ``POST /preview {}``
    descartaba la selección armada y devolvía —con token válido— el plan de un clon COMPLETO.
    """
    fake = _install_data_only(monkeypatch)
    fake.snaps["src_db"].tables.append(
        _tbl("src_db", "log_events", [_col("id", "int", nullable=False)], pk=["id"])
    )
    fake.snaps["dst_db"].tables.append(
        _tbl("dst_db", "log_events", [_col("id", "int", nullable=False)], pk=["id"])
    )
    sid, _oid, src_id = _setup(admin_client, 3744)
    job = _plan(admin_client, sid, src_id=src_id)
    first = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "data_only",
            "data": {"mode": "include", "names": ["users"], "on_existing": "append"},
        },
    )
    assert [t["table"] for t in first.json()["data"]["data_tables"]] == ["users"]
    again = admin_client.post(f"/api/v1/database-clones/{job}/preview", json={})
    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert data["copy_intent"] == "data_only"
    assert [t["table"] for t in data["data_tables"]] == ["users"], data["data_tables"]


# =========================================================================== #
# Charset / collation / owner                                                  #
# =========================================================================== #
def test_charset_override_passes_canonical_values_to_create_database(admin_client, monkeypatch):
    fake = _install_data_only(monkeypatch)
    fake.existing.discard("dst_db")
    del fake.snaps["dst_db"]
    sid, _oid, src_id = _setup(admin_client, 3750)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "new",
    })
    job = r.json()["data"]["id"]
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "structure_only",
            "target_charset": {"mode": "override", "charset": "utf8mb4",
                               "collation": "utf8mb4_general_ci"},
        },
    )
    assert pr.status_code == 200, pr.text
    data = pr.json()["data"]
    assert data["target_charset"] == "utf8mb4"
    assert data["target_collation"] == "utf8mb4_general_ci"
    ex = admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={"confirm_target_name": "dst_db", "confirm_token": data["confirm_token"]},
    )
    assert ex.status_code == 200, ex.text
    call = fake.create_calls[-1]
    assert call["charset"] == "utf8mb4"
    assert call["collation"] == "utf8mb4_general_ci"


def test_charset_combination_absent_from_catalog_is_422(admin_client, monkeypatch):
    fake = _install_data_only(monkeypatch)
    fake.existing.discard("dst_db")
    del fake.snaps["dst_db"]
    sid, _oid, src_id = _setup(admin_client, 3751)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "new",
    })
    job = r.json()["data"]["id"]
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "structure_only",
            "target_charset": {"mode": "override", "collation": "no_existe_ci"},
        },
    )
    assert pr.status_code == 422, pr.text
    assert cspec.CODE_CHARSET_COMBINATION_DISABLED in _codes(pr)
    # Y no se tocó el motor: la BD destino no se creó.
    assert fake.created == []


def test_charset_rejected_by_the_engine_is_422_before_touching_it(admin_client, monkeypatch):
    """
    El catálogo es necesario y NO suficiente: MySQL y MariaDB comparten familia sin compartir
    todas las collations. Y esto tiene que fallar en el preview: con
    ``clean_mode='drop_database'`` el worker hace DROP y después CREATE, así que un par que el
    motor rechaza dejaría el destino BORRADO.
    """
    fake = _install_data_only(monkeypatch)
    fake.charset_supported = False
    fake.existing.discard("dst_db")
    del fake.snaps["dst_db"]
    sid, _oid, src_id = _setup(admin_client, 3752)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "new",
    })
    job = r.json()["data"]["id"]
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "structure_only",
            "target_charset": {"mode": "override", "charset": "utf8mb4",
                               "collation": "utf8mb4_general_ci"},
        },
    )
    assert pr.status_code == 422, pr.text
    assert cspec.CODE_CHARSET_UNSUPPORTED_BY_ENGINE in _codes(pr)
    assert fake.dropped == [] and fake.created == []


def test_charset_override_on_a_job_that_does_not_create_the_database_is_422(
    admin_client, monkeypatch
):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3753)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={
            "copy_intent": "data_only",
            "data": {"mode": "all", "on_existing": "append"},
            "target_charset": {"mode": "override", "charset": "utf8mb4"},
        },
    )
    assert pr.status_code == 422
    assert cspec.CODE_CHARSET_NOT_APPLICABLE in _codes(pr)


def test_owner_is_rejected_for_a_mysql_target(admin_client, monkeypatch):
    fake = _install_data_only(monkeypatch)
    fake.existing.discard("dst_db")
    del fake.snaps["dst_db"]
    sid, oid, src_id = _setup(admin_client, 3754)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "new",
    })
    job = r.json()["data"]["id"]
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "structure_only", "target_owner_user_id": oid},
    )
    assert pr.status_code == 422
    assert cspec.CODE_OWNER_NOT_APPLICABLE in _codes(pr)


# =========================================================================== #
# Token, fingerprint y ciclo de vida                                            #
# =========================================================================== #
def test_changing_the_intent_invalidates_the_token(admin_client, monkeypatch):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3760)
    job = _plan(admin_client, sid, src_id=src_id)
    first = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    stale = first.json()["data"]["confirm_token"]
    second = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "upsert"}},
    )
    assert second.json()["data"]["confirm_token"] != stale
    ex = admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={"confirm_target_name": "dst_db", "confirm_token": stale},
    )
    assert ex.status_code == 422
    assert cspec.CODE_TOKEN_MISMATCH in _codes(ex)


def test_row_estimate_does_not_invalidate_the_token(admin_client, monkeypatch):
    """
    La estimación de filas NO entra al hash del plan: si entrara, un ``ANALYZE`` de fondo
    entre el preview y el execute invalidaría el token sin que el plan haya cambiado.
    """
    fake = _install_data_only(monkeypatch)
    fake.row_estimates = {"users": 10}
    sid, _oid, src_id = _setup(admin_client, 3761)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    token = pr.json()["data"]["confirm_token"]
    assert pr.json()["data"]["data_tables"][0]["row_estimate"] == 10
    fake.row_estimates = {"users": 999_999}  # como si hubiera corrido un ANALYZE
    ex = admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={"confirm_target_name": "dst_db", "confirm_token": token},
    )
    assert ex.status_code == 200, ex.text


def test_unknown_row_estimate_is_reported_as_unknown_not_zero(admin_client, monkeypatch):
    """Una tabla PG sin ANALYZE informaba '0 filas'; ahora dice que no se sabe."""
    fake = _install_data_only(monkeypatch)
    fake.unknown_estimates = {"users"}
    sid, _oid, src_id = _setup(admin_client, 3762)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    table = pr.json()["data"]["data_tables"][0]
    assert table["row_estimate_known"] is False


def test_target_schema_change_between_preview_and_execute_is_409(admin_client, monkeypatch):
    """
    En 'data_only' la validez del plan depende del esquema del DESTINO tanto como del origen,
    y hasta ahora nadie fijaba su fingerprint.
    """
    fake = _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3763)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    token = pr.json()["data"]["confirm_token"]
    # Alguien agrega una columna nullable al destino: el plan sigue siendo ejecutable, pero
    # ya no es el que se confirmó.
    fake.snaps["dst_db"].tables[0].columns.append(_col("extra", "int"))
    ex = admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={"confirm_target_name": "dst_db", "confirm_token": token},
    )
    assert ex.status_code == 409, ex.text
    assert cspec.CODE_TARGET_FINGERPRINT_CHANGED in _codes(ex)


def test_preview_after_execution_is_409(admin_client, monkeypatch):
    """Re-previsualizar un job ya ejecutado sobrescribiría el plan que realmente corrió."""
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3764)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "append"}},
    )
    admin_client.post(
        f"/api/v1/database-clones/{job}/execute",
        json={"confirm_target_name": "dst_db",
              "confirm_token": pr.json()["data"]["confirm_token"]},
    )
    again = admin_client.post(f"/api/v1/database-clones/{job}/preview", json={})
    assert again.status_code == 409
    assert cspec.CODE_ALREADY_EXECUTED in _codes(again)


def test_unknown_field_in_the_request_is_rejected(admin_client, monkeypatch):
    """Un typo caía al default en silencio y el operador ejecutaba otra cosa."""
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3765)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only",
              "data": {"mode": "all", "on_existng": "append"}},
    )
    assert pr.status_code == 422


def test_selection_and_structure_together_are_rejected(admin_client, monkeypatch):
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3766)
    job = _plan(admin_client, sid, src_id=src_id)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"selection": None, "structure": {"mode": "all"}},
    )
    assert pr.status_code == 422


# =========================================================================== #
# El atajo LEGACY (include_data) — P0                                          #
# =========================================================================== #
# Estos tests existen porque los de la primera pasada NO los tenían y por eso se shipeó un
# 422 que rompía todo clon con datos. La causa de la ceguera fue el arnés: el helper
# `_preview_and_execute` manda `json={}`, que es la ÚNICA forma del cuerpo que la SPA nunca
# usa (`use-database-clones.ts:96` manda siempre `{selection: …}`).


def _legacy_plan_with_data(admin_client, port):
    """Plan creado con el atajo legacy `include_data=true` sobre un destino existente."""
    sid, _oid, src_id = _setup(admin_client, port)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "existing",
        "clean_mode": "none", "include_data": True,
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_legacy_plan_accepts_every_body_shape_the_spa_sends(admin_client, monkeypatch):
    """
    EL test del P0. La SPA manda SIEMPRE `{selection: <array|null>}`, así que `_apply_spec`
    corre en cada preview. Mientras `create_plan` persistía el `on_existing` derivado, dos de
    estas tres formas devolvían 422 clone.conflicting_options y no había forma de salir: lo
    único que limpia esa columna es `data.mode='none'`, que además elimina los datos.
    """
    _install_data_only(monkeypatch)
    job = _legacy_plan_with_data(admin_client, 3800)
    for body in (
        {},                                                          # lo que cubrían los tests
        {"selection": None},                                         # la SPA: clon completo
        {"selection": [{"object_type": "table", "name": "users"}]},   # la SPA: clon parcial
    ):
        pr = admin_client.post(f"/api/v1/database-clones/{job}/preview", json=body)
        assert pr.status_code == 200, f"cuerpo {body} -> {pr.status_code} {pr.text}"


def test_legacy_plan_still_upserts_over_a_preserved_target(admin_client, monkeypatch):
    """
    No regresión de lo EJECUTADO: quitar la persistencia del valor derivado no puede cambiar
    lo que la fase de datos hace. Destino existente + clean_mode='none' seguía siendo upsert
    antes del arreglo y tiene que seguir siéndolo.
    """
    _install_data_only(monkeypatch)
    job = _legacy_plan_with_data(admin_client, 3801)
    pr = admin_client.post(f"/api/v1/database-clones/{job}/preview", json={"selection": None})
    assert pr.status_code == 200, pr.text
    data = pr.json()["data"]
    assert data["copy_intent"] == "structure_and_data"
    assert [t["upsert"] for t in data["data_tables"]] == [True]
    # Y el campo efectivo dice la verdad: la columna guarda NULL (nadie lo eligió) pero lo
    # que va a pasar con las filas del destino es un upsert.
    assert data["data_on_existing"] == "upsert"


def test_legacy_plan_on_a_fresh_target_appends(admin_client, monkeypatch):
    """La otra mitad de la derivación histórica: destino que este job crea => append."""
    fake = _install_data_only(monkeypatch)
    fake.existing.discard("dst_db")
    del fake.snaps["dst_db"]
    sid, _oid, src_id = _setup(admin_client, 3802)
    r = admin_client.post("/api/v1/database-clones", json={
        "source_database_id": src_id, "target_server_id": sid,
        "target_database_name": "dst_db", "target_mode": "new", "include_data": True,
    })
    job = r.json()["data"]["id"]
    pr = admin_client.post(f"/api/v1/database-clones/{job}/preview", json={"selection": None})
    assert pr.status_code == 200, pr.text
    data = pr.json()["data"]
    assert [t["upsert"] for t in data["data_tables"]] == [False]
    assert data["data_on_existing"] == "append"


def test_switching_away_from_data_only_clears_on_existing(admin_client, monkeypatch):
    """
    La segunda puerta del mismo defecto: un preview EXITOSO con 'data_only' + 'upsert' deja el
    valor persistido, y el siguiente preview que cambia de intención lo arrastraba a
    `validate_spec` y daba el mismo 422 irrecuperable.

    Tiene que ser un preview exitoso: si el primero falla, la sesión no commitea y el valor
    nunca llega a la BD — el camino no se ejercita.
    """
    _install_data_only(monkeypatch)
    sid, _oid, src_id = _setup(admin_client, 3803)
    job = _plan(admin_client, sid, src_id=src_id)
    first = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all", "on_existing": "upsert"}},
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["data_on_existing"] == "upsert"

    second = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "structure_and_data", "data": {"mode": "all"}},
    )
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["copy_intent"] == "structure_and_data"
    # Sigue copiando datos, así que el efectivo NO es null: es el derivado del contenedor.
    assert data["data_on_existing"] == "upsert"


def test_data_only_still_requires_an_explicit_on_existing(admin_client, monkeypatch):
    """
    El arreglo NO puede aflojar el requisito: limpiar la columna fuera de 'data_only' no
    debe convertir la obligación de elegir en un default silencioso.
    """
    _install_data_only(monkeypatch)
    job = _legacy_plan_with_data(admin_client, 3804)
    pr = admin_client.post(
        f"/api/v1/database-clones/{job}/preview",
        json={"copy_intent": "data_only", "data": {"mode": "all"}},
    )
    assert pr.status_code == 422
    assert cspec.CODE_ON_EXISTING_REQUIRED in _codes(pr)
