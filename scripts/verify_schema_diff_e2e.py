"""
Verificación end-to-end MANUAL del diff de esquema (schema-comparisons) contra
motores REALES (Fase 7 del plan "diff de esquema entre dos BDs + adopt/execute").

El gateway de metadatos corre en SQLite (efímero); los contenedores Docker son los
servidores REALES fuente/destino. Ejercita el camino COMPLETO de la API:
registrar servidor -> crear dos BDs reales -> ``POST /schema-comparisons`` ->
``GET .../items`` -> ``POST .../adopt`` (Opción A) / ``POST .../execute`` (Opción B),
verificando en cada paso el estado REAL en el motor (no solo la respuesta HTTP).

Cubre, por motor (MySQL 8, MariaDB 11, PostgreSQL 16):
  1. Cero-diff entre dos BDs "idénticas" salvo nombres de constraints/índices/FKs
     (valida el matching por DEFINICIÓN, no por nombre autogenerado).
  2. Detección de diferencias reales: tabla nueva, columna agregada/eliminada,
     narrowing/widening de tipo, índice nuevo, FK nueva (en tabla existente),
     vista nueva y rutina nueva.
  3. Opción A (adopt): target CON blueprint -> nueva versión + aplicación real.
  4. Opción B (execute): target SIN blueprint -> los 3 modos (all /
     all_except_destructive / custom), verificando en el motor qué corrió y qué no.
  5. Bloqueo: target CON blueprint -> /execute debe dar 409.
  6. Anti-TOCTOU: modificar el target por fuera del gateway entre comparar y
     adoptar/ejecutar -> debe dar 409 pidiendo recalcular.

NO es un test de pytest (no se recolecta): requiere Docker y se ejecuta a mano.

Uso:
    docker run -d --rm --name gw_diff_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_diff_maria -e MARIADB_ROOT_PASSWORD=rootpw \\
        -e MARIADB_ROOT_HOST=% -p 13400:3306 mariadb:11
    docker run -d --rm --name gw_diff_pg -e POSTGRES_PASSWORD=rootpw \\
        -p 15499:5432 postgres:16
    PYTHONPATH=. uv run python scripts/verify_schema_diff_e2e.py [mysql,mariadb,postgresql]

(Los mismos puertos que ``verify_migrations_e2e.py`` -- puede reusar esos mismos
contenedores si ya están corriendo.)
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="e2e_gw_diff_")
os.environ.update({
    "DB_ENGINE": "sqlite",
    "DB_NAME": os.path.join(_TMP, "gw.db"),
    "SECRET_KEY": "e2e-secret",
    "CRYPTO_KEY_SALT": "e2e-salt",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "admin123",
    "APP_ENV": "development",
    "LOGGER_MIDDLEWARE_ENABLED": "False",
    "LOGGER_EXCEPTIONS_ENABLED": "False",
    "REMOTE_SSRF_GUARD_ENABLED": "False",
    "REMOTE_SSL_MODE": "disable",
})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.controllers.schema_comparison_controller import SchemaComparisonController  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.core.limiter import limiter  # noqa: E402
from app.models import Base  # noqa: E402

ENGINES = {
    "mysql": {"port": 13399, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
    "mariadb": {"port": 13400, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
    "postgresql": {"port": 15499, "driver": "postgresql+psycopg", "user": "postgres", "pw": "rootpw"},
}
_MYSQL_FAMILY = {"mysql", "mariadb"}

failures = []


def check(label, cond):
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        failures.append(label)


def note(msg):
    print(f"  [NOTE] {msg}")


# --------------------------------------------------------------------------- #
# Helpers de conexión directa al motor (fuera del gateway)                     #
# --------------------------------------------------------------------------- #
def target_engine(engine_key, dbname):
    e = ENGINES[engine_key]
    return create_engine(f"{e['driver']}://{e['user']}:{e['pw']}@127.0.0.1:{e['port']}/{dbname}")


def server_engine(engine_key):
    e = ENGINES[engine_key]
    base = "postgres" if engine_key == "postgresql" else ""
    suffix = f"/{base}" if base else ""
    return create_engine(f"{e['driver']}://{e['user']}:{e['pw']}@127.0.0.1:{e['port']}{suffix}")


def _server_exec(engine_key, statements):
    with server_engine(engine_key).connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for s in statements:
            conn.execute(text(s))


def _direct_exec(engine_key, dbname, statements):
    with target_engine(engine_key, dbname).connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for s in statements:
            conn.exec_driver_sql(s)


def _tables(engine_key, dbname):
    insp = inspect(target_engine(engine_key, dbname).connect())
    return set(insp.get_table_names(schema="public") if engine_key == "postgresql" else insp.get_table_names())


def _columns(engine_key, dbname, table):
    insp = inspect(target_engine(engine_key, dbname).connect())
    cols = insp.get_columns(table, schema="public") if engine_key == "postgresql" else insp.get_columns(table)
    return {c["name"]: str(c["type"]) for c in cols}


def _indexes(engine_key, dbname, table):
    insp = inspect(target_engine(engine_key, dbname).connect())
    idx = insp.get_indexes(table, schema="public") if engine_key == "postgresql" else insp.get_indexes(table)
    return {i["name"] for i in idx}


def _views(engine_key, dbname):
    insp = inspect(target_engine(engine_key, dbname).connect())
    return set(insp.get_view_names(schema="public") if engine_key == "postgresql" else insp.get_view_names())


def _routine_count(engine_key, dbname, name):
    with target_engine(engine_key, dbname).connect() as conn:
        if engine_key == "postgresql":
            return conn.execute(
                text("SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                     "WHERE n.nspname='public' AND p.proname=:n"), {"n": name}
            ).scalar()
        return conn.execute(
            text("SELECT COUNT(*) FROM information_schema.ROUTINES "
                 "WHERE ROUTINE_SCHEMA = DATABASE() AND ROUTINE_NAME=:n"), {"n": name}
        ).scalar()


def _clean(engine_key, dbnames):
    for db in dbnames:
        try:
            _server_exec(engine_key, [f"DROP DATABASE IF EXISTS {db}"])
        except Exception as ex:  # noqa: BLE001
            print("   (pre-clean:", db, ex, ")")


# --------------------------------------------------------------------------- #
# DDL de escenario 1: cero-diff (misma definición, nombres distintos)           #
# --------------------------------------------------------------------------- #
def zero_diff_ddl(suffix):
    return [
        f"""CREATE TABLE categoria (
            id INT PRIMARY KEY,
            nombre VARCHAR(60) NOT NULL,
            CONSTRAINT uq_categoria_nombre_{suffix} UNIQUE (nombre)
        )""",
        f"""CREATE TABLE producto (
            id INT PRIMARY KEY,
            categoria_id INT NOT NULL,
            precio DECIMAL(10,2) NOT NULL,
            CONSTRAINT fk_producto_categoria_{suffix} FOREIGN KEY (categoria_id) REFERENCES categoria (id),
            CONSTRAINT chk_precio_positivo_{suffix} CHECK (precio > 0)
        )""",
        f"CREATE INDEX ix_producto_precio_{suffix} ON producto (precio)",
    ]


# --------------------------------------------------------------------------- #
# DDL de escenario 2: diferencias reales (target base vs source deseado)       #
# --------------------------------------------------------------------------- #
def base_target_ddl():
    """Estado ACTUAL (target): lo que existe antes de aplicar el diff."""
    return [
        "CREATE TABLE categoria (id INT PRIMARY KEY, nombre VARCHAR(60) NOT NULL)",
        "CREATE TABLE proveedor (id INT PRIMARY KEY, nombre VARCHAR(60) NOT NULL)",
        """CREATE TABLE producto (
            id INT PRIMARY KEY,
            categoria_id INT NOT NULL,
            nombre VARCHAR(80) NOT NULL,
            precio DECIMAL(10,2) NOT NULL,
            cantidad INT NOT NULL,
            stock SMALLINT NOT NULL,
            legacy_code VARCHAR(10),
            FOREIGN KEY (categoria_id) REFERENCES categoria (id)
        )""",
        "CREATE INDEX ix_producto_nombre ON producto (nombre)",
    ]


def source_ddl(engine_key):
    """Estado DESEADO (source): incluye todos los tipos de cambio a detectar."""
    stmts = [
        "CREATE TABLE categoria (id INT PRIMARY KEY, nombre VARCHAR(60) NOT NULL)",
        "CREATE TABLE proveedor (id INT PRIMARY KEY, nombre VARCHAR(60) NOT NULL)",
        """CREATE TABLE producto (
            id INT PRIMARY KEY,
            categoria_id INT NOT NULL,
            nombre VARCHAR(80) NOT NULL,
            precio DECIMAL(10,2) NOT NULL,
            cantidad SMALLINT NOT NULL,
            stock INT NOT NULL,
            descripcion VARCHAR(200),
            proveedor_id INT,
            FOREIGN KEY (categoria_id) REFERENCES categoria (id),
            FOREIGN KEY (proveedor_id) REFERENCES proveedor (id)
        )""",
        "CREATE INDEX ix_producto_nombre ON producto (nombre)",
        "CREATE INDEX ix_producto_precio ON producto (precio)",
        "CREATE TABLE factura (id INT PRIMARY KEY, producto_id INT NOT NULL, "
        "FOREIGN KEY (producto_id) REFERENCES producto (id))",
        "CREATE VIEW v_producto_caro AS SELECT id, nombre FROM producto WHERE precio > 100",
    ]
    if engine_key in _MYSQL_FAMILY:
        stmts.append(
            "CREATE PROCEDURE sp_marcar_caro() "
            "BEGIN UPDATE producto SET nombre = nombre WHERE precio > 100; END"
        )
    else:
        stmts.append(
            "CREATE FUNCTION fn_marcar_caro() RETURNS void AS $$ "
            "BEGIN UPDATE producto SET nombre = nombre WHERE precio > 100; END; "
            "$$ LANGUAGE plpgsql"
        )
    return stmts


def _routine_object_name(engine_key):
    return "PROCEDURE:sp_marcar_caro" if engine_key in _MYSQL_FAMILY else "FUNCTION:fn_marcar_caro"


# --------------------------------------------------------------------------- #
# Helpers de API                                                               #
# --------------------------------------------------------------------------- #
def _create_server(c, engine_key, sid_name):
    e = ENGINES[engine_key]
    r = c.post("/api/v1/servers", json={
        "name": sid_name, "host": "127.0.0.1", "port": e["port"],
        "engine": engine_key, "root_username": e["user"], "root_password": e["pw"],
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_owner(c, sid, username):
    r = c.post("/api/v1/server-users?provision=true",
               json={"server_id": sid, "username": username, "password": "Owner_pw123"})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_managed_db(c, sid, oid, name, model_id=None):
    body = {"server_id": sid, "owner_id": oid, "name": name}
    if model_id is not None:
        body["model_id"] = model_id
    r = c.post("/api/v1/managed-databases?provision=true", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]["id"]


def _compare(c, src_id, tgt_id):
    return c.post("/api/v1/schema-comparisons",
                  json={"source_database_id": src_id, "target_database_id": tgt_id})


def _items(c, cid, **params):
    r = c.get(f"/api/v1/schema-comparisons/{cid}/items", params={"size": 50, **params})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _resolve_mode(items, mode, selected_ids=None):
    items = sorted(items, key=lambda i: i["seq"])
    if mode == "all":
        chosen = [i for i in items if not i["risk_flags"].get("requires_individual_review")]
    elif mode == "all_except_destructive":
        chosen = [
            i for i in items
            if not i["risk_flags"].get("destructive")
            and not i["risk_flags"].get("requires_individual_review")
        ]
    else:
        idset = set(selected_ids or [])
        chosen = [i for i in items if i["id"] in idset]
    resolved = [{"sql": i["sql"], "risk": i["risk_flags"]} for i in chosen]
    return chosen, resolved


def _token(tgt_id, engine_key, resolved):
    # Reproduce el algoritmo documentado: se llama DIRECTAMENTE a la función real del
    # controller (mismo enfoque que tests/test_api_schema_comparisons.py), no una
    # reimplementación manual -> cero riesgo de desincronización con el servidor.
    return SchemaComparisonController.execution_token(tgt_id, engine_key, resolved)


# --------------------------------------------------------------------------- #
# Escenario 1: cero-diff                                                       #
# --------------------------------------------------------------------------- #
def run_zero_diff(c, sid, oid, ek):
    print(f"\n----- {ek}: cero-diff (misma definición, nombres autogenerados distintos) -----")
    src_db, tgt_db = f"zdsrc_{ek[:2]}", f"zdtgt_{ek[:2]}"
    _clean(ek, [src_db, tgt_db])
    src_id = _create_managed_db(c, sid, oid, src_db)
    tgt_id = _create_managed_db(c, sid, oid, tgt_db)
    _direct_exec(ek, src_db, zero_diff_ddl("a"))
    _direct_exec(ek, tgt_db, zero_diff_ddl("b"))

    r = _compare(c, src_id, tgt_id)
    check("zero-diff: create comparison 201", r.status_code == 201)
    data = r.json()["data"]
    check("zero-diff: item_count == 0 (matching por definición, no por nombre)",
          data.get("item_count") == 0)
    if data.get("item_count"):
        items = _items(c, data["id"])
        note(f"zero-diff items inesperados: {[(i['object_type'], i['object_name'], i['change_type']) for i in items]}")
    return src_id, tgt_id


# --------------------------------------------------------------------------- #
# Escenario 2: detección de diferencias (contra un clon fresco del target)      #
# --------------------------------------------------------------------------- #
def _new_target_clone(c, sid, oid, ek, suffix, *, with_model=False, model_id=None):
    name = f"ddtgt_{ek[:2]}_{suffix}"
    _clean(ek, [name])
    mid = model_id
    if with_model and mid is None:
        mid = c.post("/api/v1/database-models",
                     json={"name": f"BP-{ek}-{suffix}", "slug": f"bp-{ek}-{suffix}"}).json()["data"]["id"]
    tgt_id = _create_managed_db(c, sid, oid, name, model_id=mid)
    _direct_exec(ek, name, base_target_ddl())
    return tgt_id, name, mid


def assert_expected_diff(ek, items):
    by_name = {}
    for i in items:
        by_name.setdefault((i["object_type"], i["object_name"]), []).append(i)

    def has(otype, oname, ctype):
        rows = by_name.get((otype, oname), [])
        return any(r["change_type"] == ctype for r in rows)

    check("diff: tabla nueva 'factura'", has("table", "factura", "new"))
    check("diff: columna nueva 'producto.descripcion'", has("column", "producto.descripcion", "new"))
    check("diff: columna nueva 'producto.proveedor_id'", has("column", "producto.proveedor_id", "new"))
    check("diff: columna eliminada 'producto.legacy_code'", has("column", "producto.legacy_code", "dropped"))
    check("diff: columna modificada 'producto.cantidad' (narrowing)",
          has("column", "producto.cantidad", "modified"))
    check("diff: columna modificada 'producto.stock' (widening)",
          has("column", "producto.stock", "modified"))
    check("diff: índice nuevo 'producto.ix_producto_precio'",
          has("index", "producto.ix_producto_precio", "new"))
    check("diff: vista nueva 'v_producto_caro'", has("view", "v_producto_caro", "new"))
    check(f"diff: rutina nueva '{_routine_object_name(ek)}'",
          has("routine", _routine_object_name(ek), "new"))
    fk_news = [r for (ot, _), rows in by_name.items() if ot == "foreign_key" for r in rows if r["change_type"] == "new"]
    check("diff: al menos 2 FKs nuevas (factura->producto, producto->proveedor)", len(fk_news) >= 2)

    # Riesgo: narrowing es destructivo; widening NO lo es; columna dropped destructiva.
    cantidad_item = by_name[("column", "producto.cantidad")][0]
    stock_item = by_name[("column", "producto.stock")][0]
    legacy_item = by_name[("column", "producto.legacy_code")][0]
    check("diff: narrowing (cantidad int->smallint) marcado destructive",
          cantidad_item["risk_flags"].get("destructive") is True)
    check("diff: widening (stock smallint->int) NO destructive",
          stock_item["risk_flags"].get("destructive") is False)
    check("diff: drop de columna marcado destructive", legacy_item["risk_flags"].get("destructive") is True)
    view_item = by_name[("view", "v_producto_caro")][0]
    routine_item = by_name[("routine", _routine_object_name(ek))][0]
    check("diff: vista nueva requires_individual_review",
          view_item["risk_flags"].get("requires_individual_review") is True)
    check("diff: rutina nueva requires_individual_review",
          routine_item["risk_flags"].get("requires_individual_review") is True)


# --------------------------------------------------------------------------- #
# Escenario 3: Opción A (adopt)                                                #
# --------------------------------------------------------------------------- #
def run_option_a(c, sid, oid, ek, src_id):
    print(f"\n----- {ek}: Opción A (adopt) -----")
    tgt_id, tgt_name, model_id = _new_target_clone(c, sid, oid, ek, "adopt", with_model=True)
    r = _compare(c, src_id, tgt_id)
    check("adopt: create comparison 201", r.status_code == 201)
    cid = r.json()["data"]["id"]
    items = _items(c, cid)
    assert_expected_diff(ek, items)

    by_name = {i["object_name"]: i for i in items}
    # Selección "segura" (sin la rutina para MySQL/MariaDB -- ver limitación documentada
    # sobre split_sql_statements + BEGIN...END abajo). PostgreSQL sí incluye la función
    # (dollar-quoted -> split_sql_statements la respeta correctamente).
    safe_names = ["factura", "producto.descripcion", "producto.ix_producto_precio", "v_producto_caro"]
    selected = [by_name[n]["id"] for n in safe_names if n in by_name]
    # + FK hija de 'factura' (nombre autogenerado -> buscar por object_type/parent).
    fk_child = [i["id"] for i in items if i["object_type"] == "foreign_key"
                and i["change_type"] == "new" and i["object_name"].startswith("factura.")]
    selected += fk_child
    if ek == "postgresql":
        selected.append(by_name[_routine_object_name(ek)]["id"])

    r = c.post(f"/api/v1/schema-comparisons/{cid}/adopt", json={
        "selected_item_ids": selected, "name": "diff-e2e-v1", "execute_immediately": True,
    })
    check("adopt: 200", r.status_code == 200)
    if r.status_code != 200:
        note(f"adopt error: {r.text[:400]}")
        return tgt_id, model_id, cid
    data = r.json()["data"]
    check("adopt: executed=True", data["executed"] is True)
    check("adopt: apply_result sin fallo", data["apply_result"] is not None and not data["apply_result"].get("failed"))

    tables = _tables(ek, tgt_name)
    check("adopt: tabla 'factura' creada en el motor real", "factura" in tables)
    cols = _columns(ek, tgt_name, "producto")
    check("adopt: columna 'descripcion' presente en el motor real", "descripcion" in cols)
    idx = _indexes(ek, tgt_name, "producto")
    check("adopt: índice 'ix_producto_precio' presente en el motor real",
          "ix_producto_precio" in idx)
    views = _views(ek, tgt_name)
    check("adopt: vista 'v_producto_caro' presente en el motor real", "v_producto_caro" in views)
    if ek == "postgresql":
        check("adopt: función fn_marcar_caro presente en el motor real",
              _routine_count(ek, tgt_name, "fn_marcar_caro") > 0)

    return tgt_id, model_id, cid


def run_option_a_mysql_routine_limitation(c, sid, oid, ek, src_id):
    """
    Hallazgo documentado (NO arreglado aquí, ver docs/features/schema-comparison.md):
    ``split_sql_statements`` (Plan 02, ``app/services/db_admin/sql_dialect.py``) no
    reconoce bloques ``BEGIN...END`` de MySQL/MariaDB (solo dollar-quoting de
    PostgreSQL) -- corta CUALQUIER cuerpo de rutina/trigger con un ';' interno en
    fragmentos inválidos. Esto afecta a Opción A (adopt), que ensambla el up_sql y lo
    pasa por Alembic (create_migration -> split_sql_statements). Opción B (execute)
    NO tiene este problema (ejecuta cada ítem ya renderizado, sin volver a partirlo).
    Este check confirma el comportamiento conocido (no un bug nuevo de este plan).
    """
    if ek not in _MYSQL_FAMILY:
        return
    print(f"\n----- {ek}: límite conocido (Plan 02) -- adopt de rutina BEGIN...END -----")
    tgt_id, tgt_name, model_id = _new_target_clone(c, sid, oid, ek, "adoptbug", with_model=True)
    r = _compare(c, src_id, tgt_id)
    cid = r.json()["data"]["id"]
    items = _items(c, cid)
    routine_item = next(i for i in items if i["object_name"] == _routine_object_name(ek))
    r = c.post(f"/api/v1/schema-comparisons/{cid}/adopt", json={
        "selected_item_ids": [routine_item["id"]], "name": "routine-only", "execute_immediately": True,
    })
    # Se espera 200 al crear+ver la versión, pero la APLICACIÓN real debe fallar
    # (o el propio create_migration/Alembic-revision debe rechazar el SQL partido).
    applied_ok = False
    if r.status_code == 200:
        data = r.json()["data"]
        ar = data.get("apply_result") or {}
        applied_ok = bool(ar) and not ar.get("failed", True) and ar.get("failed") is False
    check(
        "límite conocido: adopt de rutina MySQL/MariaDB vía Opción A FALLA "
        "(split_sql_statements corta el BEGIN...END; usar Opción B para rutinas/triggers)",
        r.status_code != 200 or not applied_ok,
    )


# --------------------------------------------------------------------------- #
# Escenario 4: Opción B (execute) -- 3 modos                                   #
# --------------------------------------------------------------------------- #
def run_option_b(c, sid, oid, ek, src_id):
    print(f"\n----- {ek}: Opción B (execute) -- 3 modos -----")
    results = {}
    for suffix, mode in (("b1", "all"), ("b2", "all_except_destructive"), ("b3", "custom")):
        tgt_id, tgt_name, _ = _new_target_clone(c, sid, oid, ek, suffix)
        r = _compare(c, src_id, tgt_id)
        check(f"execute[{mode}]: create comparison 201", r.status_code == 201)
        cid = r.json()["data"]["id"]
        items = _items(c, cid)
        if suffix == "b1":
            assert_expected_diff(ek, items)

        if mode == "custom":
            by_name = {i["object_name"]: i for i in items}
            sel_ids = [by_name["v_producto_caro"]["id"], by_name[_routine_object_name(ek)]["id"]]
            chosen, resolved = _resolve_mode(items, "custom", sel_ids)
        else:
            chosen, resolved = _resolve_mode(items, mode)
        token = _token(tgt_id, ek, resolved)

        r = c.post(f"/api/v1/schema-comparisons/{cid}/execute", json={
            "mode": mode, "selected_item_ids": [i["id"] for i in chosen] if mode == "custom" else None,
            "confirm_target_name": tgt_name, "confirm_token": token,
        })
        check(f"execute[{mode}]: 200", r.status_code == 200)
        if r.status_code != 200:
            note(f"execute[{mode}] error: {r.text[:400]}")
            continue
        data = r.json()["data"]
        check(f"execute[{mode}]: sin fallo", data["failed"] is False)
        check(f"execute[{mode}]: aplicó {len(chosen)} sentencia(s)", data["applied_count"] == len(chosen))

        tables = _tables(ek, tgt_name)
        cols = _columns(ek, tgt_name, "producto")
        views = _views(ek, tgt_name)

        if mode == "all":
            check("execute[all]: 'factura' creada", "factura" in tables)
            check("execute[all]: 'descripcion' agregada", "descripcion" in cols)
            check("execute[all]: 'legacy_code' ELIMINADA (destructivo incluido en 'all')",
                  "legacy_code" not in cols)
            check("execute[all]: 'cantidad' narrowed a SMALLINT",
                  "SMALLINT" in cols.get("cantidad", "").upper())
            check("execute[all]: vista/rutina NO creadas (requires_individual_review)",
                  "v_producto_caro" not in views and _routine_count(ek, tgt_name, _routine_object_name(ek).split(":")[1]) == 0)
        elif mode == "all_except_destructive":
            check("execute[all_except_destructive]: 'factura' creada", "factura" in tables)
            check("execute[all_except_destructive]: 'descripcion' agregada", "descripcion" in cols)
            check("execute[all_except_destructive]: 'legacy_code' CONSERVADA (destructivo excluido)",
                  "legacy_code" in cols)
            check("execute[all_except_destructive]: 'stock' widened a INT (seguro, no destructivo)",
                  "SMALLINT" not in cols.get("stock", "").upper())
            check("execute[all_except_destructive]: 'cantidad' SIN narrowing (destructivo excluido)",
                  "SMALLINT" not in cols.get("cantidad", "").upper())
            check("execute[all_except_destructive]: vista/rutina NO creadas",
                  "v_producto_caro" not in views)
        else:  # custom: solo vista + rutina
            check("execute[custom]: SOLO vista+rutina (nada estructural aplicado)",
                  "factura" not in tables and "descripcion" not in cols)
            check("execute[custom]: vista creada", "v_producto_caro" in views)
            check("execute[custom]: rutina/función creada (Opción B no sufre el límite de Opción A)",
                  _routine_count(ek, tgt_name, _routine_object_name(ek).split(":")[1]) > 0)
        results[mode] = (tgt_id, tgt_name, cid)
    return results


# --------------------------------------------------------------------------- #
# Escenario 5: bloqueo de Opción B con blueprint asignado                       #
# --------------------------------------------------------------------------- #
def run_blocked_execute(c, sid, oid, ek, src_id, adopt_tgt_id, adopt_cid):
    print(f"\n----- {ek}: bloqueo Opción B con blueprint asignado -----")
    r = c.post(f"/api/v1/schema-comparisons/{adopt_cid}/execute", json={
        "mode": "all", "confirm_target_name": "whatever", "confirm_token": "whatever",
    })
    check("blocked: target con blueprint -> execute 409", r.status_code == 409)
    check("blocked: mensaje sugiere usar adopt", "adopt" in r.text.lower())


# --------------------------------------------------------------------------- #
# Escenario 6: anti-TOCTOU (adopt y execute)                                   #
# --------------------------------------------------------------------------- #
def run_toctou(c, sid, oid, ek, src_id):
    print(f"\n----- {ek}: anti-TOCTOU -----")

    # --- Opción B: comparar, modificar el target por fuera, ejecutar -> 409. --- #
    tgt_id, tgt_name, _ = _new_target_clone(c, sid, oid, ek, "toctoub")
    cid = _compare(c, src_id, tgt_id).json()["data"]["id"]
    items = _items(c, cid)
    chosen, resolved = _resolve_mode(items, "all")
    token = _token(tgt_id, ek, resolved)
    _direct_exec(ek, tgt_name, ["CREATE TABLE drift_marker (id INT PRIMARY KEY)"])
    r = c.post(f"/api/v1/schema-comparisons/{cid}/execute", json={
        "mode": "all", "confirm_target_name": tgt_name, "confirm_token": token,
    })
    check("toctou execute: drift externo -> 409", r.status_code == 409)
    check("toctou execute: mensaje pide recalcular",
          "cambió" in r.text.lower() or "recal" in r.text.lower())

    # --- Opción A: comparar (target con blueprint), modificar por fuera, adopt -> 409. --- #
    tgt2_id, tgt2_name, _ = _new_target_clone(c, sid, oid, ek, "toctoua", with_model=True)
    cid2 = _compare(c, src_id, tgt2_id).json()["data"]["id"]
    items2 = _items(c, cid2)
    sel = [items2[0]["id"]]
    _direct_exec(ek, tgt2_name, ["CREATE TABLE drift_marker_a (id INT PRIMARY KEY)"])
    r2 = c.post(f"/api/v1/schema-comparisons/{cid2}/adopt", json={
        "selected_item_ids": sel, "name": "should-fail",
    })
    check("toctou adopt: drift externo -> 409", r2.status_code == 409)
    check("toctou adopt: mensaje pide recalcular",
          "cambió" in r2.text.lower() or "recal" in r2.text.lower())


# --------------------------------------------------------------------------- #
# Orquestación por motor                                                       #
# --------------------------------------------------------------------------- #
_TARGET_CLONE_SUFFIXES = (
    "adopt", "adoptbug", "b1", "b2", "b3", "toctoub", "toctoua",
)


def _all_db_names(ek):
    ek2 = ek[:2]
    names = [f"zdsrc_{ek2}", f"zdtgt_{ek2}", f"ddsrc_{ek2}"]
    names += [f"ddtgt_{ek2}_{s}" for s in _TARGET_CLONE_SUFFIXES]
    return names


def run_for(c, ek):
    print(f"\n===== ENGINE: {ek} =====")
    owner_user = f"diffown_{ek[:2]}"
    # Pre-clean idempotente: en PostgreSQL el ROLE no se puede dropear mientras sea
    # dueño de BDs de una corrida previa -> primero las BDs, DESPUÉS el rol/usuario.
    _clean(ek, _all_db_names(ek))
    try:
        if ek == "postgresql":
            _server_exec(ek, [f"DROP ROLE IF EXISTS {owner_user}"])
        else:
            _server_exec(ek, [f"DROP USER IF EXISTS '{owner_user}'@'%'"])
    except Exception as ex:  # noqa: BLE001
        print("   (pre-clean owner:", ex, ")")
    sid = _create_server(c, ek, f"srv-diff-{ek}")
    oid = _create_owner(c, sid, owner_user)

    run_zero_diff(c, sid, oid, ek)

    src_db = f"ddsrc_{ek[:2]}"
    _clean(ek, [src_db])
    src_id = _create_managed_db(c, sid, oid, src_db)
    _direct_exec(ek, src_db, source_ddl(ek))

    adopt_tgt_id, adopt_model_id, adopt_cid = run_option_a(c, sid, oid, ek, src_id)
    run_option_a_mysql_routine_limitation(c, sid, oid, ek, src_id)
    run_option_b(c, sid, oid, ek, src_id)
    run_blocked_execute(c, sid, oid, ek, src_id, adopt_tgt_id, adopt_cid)
    run_toctou(c, sid, oid, ek, src_id)


def main():
    db = Database()
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    limiter.enabled = False
    import main as app_main
    engines = sys.argv[1].split(",") if len(sys.argv) > 1 else list(ENGINES)
    with TestClient(app_main.app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        for ek in engines:
            try:
                run_for(c, ek)
            except Exception as ex:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                failures.append(f"{ek}: EXCEPTION {ex}")

    print("\n===== SUMMARY =====")
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
