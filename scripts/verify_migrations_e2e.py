"""
Verificación end-to-end MANUAL del Plan 02 contra motores REALES.

Cubre MySQL 8, MariaDB 11 y PostgreSQL 16. El gateway de metadatos corre en SQLite
(efímero); los contenedores Docker son los servidores DESTINO. Ejercita el camino
COMPLETO de la API: registrar servidor, crear usuario y BD reales (provision), crear
blueprint + migraciones, status / apply / history / rollback (con confirm_version) /
stamp / integridad por checksum, verificando el estado real en cada motor.

NO es un test de pytest (no se recolecta): requiere Docker y se ejecuta a mano. Los
tests de integración canónicos con testcontainers para CI están pendientes (los posee
gateway-testing-qa; ver docs/plans/08).

Uso:
    docker run -d --rm --name gw_mig_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_mig_maria -e MARIADB_ROOT_PASSWORD=rootpw \\
        -e MARIADB_ROOT_HOST=% -p 13400:3306 mariadb:11
    docker run -d --rm --name gw_mig_pg -e POSTGRES_PASSWORD=rootpw \\
        -p 15499:5432 postgres:16
    PYTHONPATH=. uv run python scripts/verify_migrations_e2e.py
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="e2e_gw_")
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

from app.core.database import Database  # noqa: E402
from app.core.limiter import limiter  # noqa: E402
from app.models import Base  # noqa: E402

ENGINES = {
    "mysql": {"port": 13399, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
    "mariadb": {"port": 13400, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
    "postgresql": {"port": 15499, "driver": "postgresql+psycopg", "user": "postgres", "pw": "rootpw"},
}

failures = []


def check(label, cond):
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        failures.append(label)


def target_engine(engine_key, dbname):
    e = ENGINES[engine_key]
    return create_engine(
        f"{e['driver']}://{e['user']}:{e['pw']}@127.0.0.1:{e['port']}/{dbname}"
    )


def server_engine(engine_key):
    e = ENGINES[engine_key]
    base = "postgres" if engine_key == "postgresql" else ""
    suffix = f"/{base}" if base else ""
    return create_engine(
        f"{e['driver']}://{e['user']}:{e['pw']}@127.0.0.1:{e['port']}{suffix}"
    )


def _server_exec(engine_key, statements):
    """Ejecuta DDL a nivel servidor (crear/borrar BDs/usuarios), AUTOCOMMIT."""
    with server_engine(engine_key).connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for s in statements:
            conn.execute(text(s))


def _direct_exec(engine_key, dbname, statements):
    """Ejecuta DDL conectado a una BD concreta (poblar estructura), AUTOCOMMIT."""
    with target_engine(engine_key, dbname).connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for s in statements:
            conn.execute(text(s))


def _tables(engine_key, dbname):
    insp = inspect(target_engine(engine_key, dbname).connect())
    return set(insp.get_table_names() if engine_key != "postgresql"
              else insp.get_table_names(schema="public"))


def run_plan09(c, sid, oid, engine_key):
    """Plan 09 + UX: reconcile, adopt (DB/user), snapshot, from-snapshot + gate
    reviewed, apply secuencial a versión objetivo, rollback secuencial y autoversión."""
    print(f"\n----- PLAN 09 + UX: {engine_key} -----")
    ek = engine_key
    adopt_db, adopt_usr = f"p9adopt_{ek[:2]}", f"p9usr_{ek[:2]}"
    snap_db, seq_db = f"p9snap_{ek[:2]}", f"p9seq_{ek[:2]}"

    # Pre-clean idempotente.
    for db in (adopt_db, snap_db, seq_db):
        try:
            _server_exec(ek, [f"DROP DATABASE IF EXISTS {db}"])
        except Exception as ex:
            print("   (pre-clean p9:", ex, ")")
    try:
        if ek == "postgresql":
            _server_exec(ek, [f"DROP ROLE IF EXISTS {adopt_usr}"])
        else:
            _server_exec(ek, [f"DROP USER IF EXISTS '{adopt_usr}'@'%'"])
    except Exception:
        pass

    # 1) Crear DB + usuario + estructura DIRECTAMENTE en el motor (no gestionados).
    _server_exec(ek, [f"CREATE DATABASE {adopt_db}"])
    if ek == "postgresql":
        _server_exec(ek, [f"CREATE ROLE {adopt_usr} LOGIN PASSWORD 'Pw_123456'"])
    else:
        _server_exec(ek, [f"CREATE USER '{adopt_usr}'@'%' IDENTIFIED BY 'Pw_123456'"])
    _direct_exec(ek, adopt_db, [
        "CREATE TABLE clientes (id INT PRIMARY KEY, nombre VARCHAR(80))",
        "CREATE VIEW v_clientes AS SELECT id FROM clientes",
    ])

    # 2) RECONCILE: el plano en vivo vs el inventario.
    rec = c.get(f"/api/v1/servers/{sid}/reconcile")
    check("p9 reconcile 200", rec.status_code == 200)
    rd = rec.json()["data"]
    dbstate = {d["name"]: d["state"] for d in rd["databases"]}
    ustate = {u["username"]: u["state"] for u in rd["users"]}
    check("p9 reconcile: adopt_db unmanaged", dbstate.get(adopt_db) == "unmanaged")
    check("p9 reconcile: adopt_usr unmanaged", ustate.get(adopt_usr) == "unmanaged")

    # 3) ADOPT usuario (existe) y luego la BD (owner del mismo servidor).
    au = c.post("/api/v1/server-users/adopt",
                json={"server_id": sid, "username": adopt_usr, "host": "%"})
    check("p9 adopt user 201 sin password",
          au.status_code == 201 and au.json()["data"]["has_password"] is False)
    adopt_oid = au.json()["data"]["id"]
    ad = c.post("/api/v1/managed-databases/adopt",
                json={"server_id": sid, "name": adopt_db, "owner_id": adopt_oid})
    check("p9 adopt db 201 origin=adopted",
          ad.status_code == 201 and ad.json()["data"]["origin"] == "adopted")
    check("p9 adopt db idempotente 409",
          c.post("/api/v1/managed-databases/adopt",
                 json={"server_id": sid, "name": adopt_db, "owner_id": adopt_oid}).status_code == 409)
    check("p9 adopt db inexistente 404",
          c.post("/api/v1/managed-databases/adopt",
                 json={"server_id": sid, "name": "no_existe_xyz", "owner_id": adopt_oid}).status_code == 404)

    # 4) SNAPSHOT de adopt_db (tabla + vista). Solo estructura.
    snp = c.get(f"/api/v1/servers/{sid}/databases/{adopt_db}/snapshot")
    check("p9 snapshot 200", snp.status_code == 200)
    sd = snp.json()["data"]
    otypes = {s["object_type"] for s in sd["statements"]}
    check("p9 snapshot captura tabla y vista", "table" in otypes and "view" in otypes)
    check("p9 snapshot source_engine correcto", sd["source_engine"] == ek)

    # 5) FROM-SNAPSHOT → baseline reviewed=false; apply BLOQUEADO; aprobar; aplicar real.
    fs = c.post("/api/v1/database-models/from-snapshot",
                json={"server_id": sid, "database": adopt_db, "name": f"BP9-{ek}", "slug": f"bp9-{ek}"})
    check("p9 from-snapshot 201", fs.status_code == 201)
    snap_mid = fs.json()["data"]["model"]["id"]
    det = c.get(f"/api/v1/database-models/{snap_mid}/migrations/0001").json()["data"]
    check("p9 baseline reviewed=false (R1)", det["reviewed"] is False and det["is_baseline"] is True)
    snap_db_id = c.post("/api/v1/managed-databases?provision=true",
                        json={"server_id": sid, "owner_id": oid, "name": snap_db,
                              "model_id": snap_mid}).json()["data"]["id"]
    check("p9 apply baseline sin revisar -> 409 (R1 gate)",
          c.post(f"/api/v1/managed-databases/{snap_db_id}/migrations/apply").status_code == 409)
    c.patch(f"/api/v1/database-models/{snap_mid}/migrations/0001", json={"reviewed": True})
    okap = c.post(f"/api/v1/managed-databases/{snap_db_id}/migrations/apply")
    check("p9 apply tras aprobar 200",
          okap.status_code == 200 and okap.json()["data"]["failed"] is False)
    check("p9 baseline aplicó la tabla del snapshot", "clientes" in _tables(ek, snap_db))

    # 6) APPLY secuencial a versión objetivo + autoversión + ROLLBACK secuencial.
    seq_mid = c.post("/api/v1/database-models",
                     json={"name": f"Seq-{ek}", "slug": f"seq-{ek}"}).json()["data"]["id"]
    for i in (1, 2, 3):
        # version OMITIDA -> autoasignada (0001, 0002, 0003).
        c.post(f"/api/v1/database-models/{seq_mid}/migrations",
               json={"name": f"m{i}", "up_sql": f"CREATE TABLE s{i} (id INT PRIMARY KEY)",
                     "down_sql": f"DROP TABLE s{i}"})
    vers = sorted(m["version"] for m in
                  c.get(f"/api/v1/database-models/{seq_mid}/migrations?size=50").json()["data"])
    check("p9 autoversión secuencial 0001-0003", vers == ["0001", "0002", "0003"])
    seq_db_id = c.post("/api/v1/managed-databases?provision=true",
                       json={"server_id": sid, "owner_id": oid, "name": seq_db,
                             "model_id": seq_mid}).json()["data"]["id"]
    a = c.post(f"/api/v1/managed-databases/{seq_db_id}/migrations/apply?version=0002").json()["data"]
    check("p9 apply a 0002 en UNA llamada (from None -> to 0002, 2 aplicadas)",
          a["from_version"] is None and a["to_version"] == "0002" and a["applied_count"] == 2)
    t = _tables(ek, seq_db)
    check("p9 apply parcial: s1,s2 sí; s3 no", "s1" in t and "s2" in t and "s3" not in t)
    c.post(f"/api/v1/managed-databases/{seq_db_id}/migrations/apply")  # hasta la última
    rb = c.post(f"/api/v1/managed-databases/{seq_db_id}/migrations/rollback"
                f"?confirm_version=0003&target_version=0001")
    rbd = rb.json()["data"]
    check("p9 rollback secuencial 0003->0001 (2 revertidas)",
          rb.status_code == 200 and rbd["to_version"] == "0001" and rbd["reverted_count"] == 2)
    t2 = _tables(ek, seq_db)
    check("p9 rollback dejó solo s1", "s1" in t2 and "s2" not in t2 and "s3" not in t2)


def run_for(c, engine_key):
    print(f"\n===== ENGINE: {engine_key} =====")
    e = ENGINES[engine_key]
    dbname = f"vts_{engine_key[:2]}"

    owner_user = f"own_{engine_key[:2]}"
    # Pre-clean: drop target DB + owner user left by a previous run (idempotent).
    try:
        with server_engine(engine_key).connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {dbname}"))
            if engine_key == "postgresql":
                conn.execute(text(f"DROP ROLE IF EXISTS {owner_user}"))
            else:
                conn.execute(text(f"DROP USER IF EXISTS '{owner_user}'@'%'"))
    except Exception as ex:
        print("   (pre-clean note:", ex, ")")

    # 1) Register the real container as a target server.
    sid = c.post("/api/v1/servers", json={
        "name": f"srv-{engine_key}", "host": "127.0.0.1", "port": e["port"],
        "engine": engine_key, "root_username": e["user"], "root_password": e["pw"],
    }).json()["data"]["id"]

    # 2) Owner server-user (provisioned in the engine).
    ru = c.post("/api/v1/server-users?provision=true",
                json={"server_id": sid, "username": owner_user, "password": "Owner_pw123"})
    check("create+provision owner user", ru.status_code == 201)
    if ru.status_code != 201:
        print("   owner create response:", ru.status_code, ru.text[:300])
        return
    oid = ru.json()["data"]["id"]

    # 3) Blueprint + 2 migrations (MySQL-style up_sql; PG via sqlglot auto-translation).
    mid = c.post("/api/v1/database-models",
                 json={"name": f"Ventas-{engine_key}", "slug": f"ventas-{engine_key}"}).json()["data"]["id"]
    m1 = c.post(f"/api/v1/database-models/{mid}/migrations", json={
        "version": "0001", "name": "tabla orders",
        "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
        "down_sql": "DROP TABLE orders",
    })
    check("create migration 0001", m1.status_code == 201)
    check("0001 has pg translation w/o AUTO_INCREMENT",
          "AUTO_INCREMENT" not in m1.json()["data"]["translated"]["postgresql"])
    c.post(f"/api/v1/database-models/{mid}/migrations", json={
        "version": "0002", "name": "add col status",
        "up_sql": "ALTER TABLE orders ADD COLUMN status VARCHAR(20)",
        "down_sql": "ALTER TABLE orders DROP COLUMN status",
    })

    # 4) Managed DB on the real engine (provision=true => CREATE DATABASE).
    md = c.post("/api/v1/managed-databases?provision=true", json={
        "server_id": sid, "owner_id": oid, "name": dbname, "model_id": mid,
    })
    check("create+provision managed DB",
          md.status_code in (200, 201) and md.json()["data"]["status"] == "active")
    db_id = md.json()["data"]["id"]

    # 5) Status before apply => 2 pending, current None.
    st = c.get(f"/api/v1/managed-databases/{db_id}/migrations/status").json()["data"]
    check("status: current None", st["current_version"] is None)
    check("status: 2 pending", st["pending_count"] == 2)

    # 5b) Dry-run: devuelve el plan sin tocar el motor.
    dr = c.post(f"/api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true")
    check("dry-run HTTP 200", dr.status_code == 200)
    drd = dr.json()["data"]
    check("dry-run: 2 pendientes, no aplicado",
          drd.get("dry_run") is True and len(drd["pending_versions"]) == 2)
    st_after_dry = c.get(f"/api/v1/managed-databases/{db_id}/migrations/status").json()["data"]
    check("dry-run no mutó la BD (sigue current None)", st_after_dry["current_version"] is None)

    # 6) Apply all pending.
    ap = c.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    check("apply HTTP 200", ap.status_code == 200)
    apd = ap.json()["data"]
    check("apply: 2 applied, no failure", apd["applied_count"] == 2 and apd["failed"] is False)

    # 7) Verify REAL schema on the target engine.
    insp = inspect(target_engine(engine_key, dbname).connect())
    tables = insp.get_table_names() if engine_key != "postgresql" else insp.get_table_names(schema="public")
    check("orders table exists on engine", "orders" in tables)
    cols = [col["name"] for col in (insp.get_columns("orders") if engine_key != "postgresql"
                                    else insp.get_columns("orders", schema="public"))]
    check("status column present on engine", "status" in cols)

    # 8) Version table _gw_v_{slug} present with 0002.
    st2 = c.get(f"/api/v1/managed-databases/{db_id}/migrations/status").json()["data"]
    check("status: current 0002 after apply", st2["current_version"] == "0002")
    check("status: 0 pending after apply", st2["pending_count"] == 0)

    # 8b) History endpoint shows the 2 applied migrations.
    hist = c.get(f"/api/v1/managed-databases/{db_id}/migrations/history")
    check("history HTTP 200", hist.status_code == 200)
    hrows = hist.json()["data"]
    check("history has 2 applied rows", len(hrows) == 2 and all(h["status"] == "applied" for h in hrows))

    # 9) Idempotent re-apply (nothing pending).
    ap2 = c.post(f"/api/v1/managed-databases/{db_id}/migrations/apply").json()["data"]
    check("re-apply is no-op", ap2["applied_count"] == 0)

    # 10) Rollback last (0002 -> 0001), verify column dropped. Requiere confirm_version.
    bad = c.post(f"/api/v1/managed-databases/{db_id}/migrations/rollback")
    check("rollback sin confirm_version -> 422", bad.status_code == 422)
    rb = c.post(f"/api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version=0002")
    check("rollback HTTP 200", rb.status_code == 200)
    # Respuesta de rollback (target-based): from_version -> to_version (Plan 09 UX).
    check("rollback to_version=0001", rb.json()["data"]["to_version"] == "0001")
    insp2 = inspect(target_engine(engine_key, dbname).connect())
    cols2 = [col["name"] for col in (insp2.get_columns("orders") if engine_key != "postgresql"
                                     else insp2.get_columns("orders", schema="public"))]
    check("status column removed after rollback", "status" not in cols2)

    # 11) Checksum tamper => apply blocked. Guardamos el checksum ORIGINAL y lo
    # restauramos verbatim (robusto ante cambios de fórmula del checksum).
    gw = create_engine(f"sqlite:///{os.environ['DB_NAME']}")
    with gw.connect() as conn:
        orig_cs = conn.execute(text("SELECT checksum FROM model_migrations WHERE version='0002' AND model_id=:m"), {"m": mid}).scalar()
        conn.execute(text("UPDATE model_migrations SET checksum='deadbeef' WHERE version='0002' AND model_id=:m"), {"m": mid})
        conn.commit()
    tampered = c.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    check("apply blocked on checksum mismatch (409)", tampered.status_code == 409)
    with gw.connect() as conn:
        conn.execute(text("UPDATE model_migrations SET checksum=:c WHERE version='0002' AND model_id=:m"), {"c": orig_cs, "m": mid})
        conn.commit()

    # 12) Re-apply to 0002, then stamp test: drop version table content via stamp to 0001.
    c.post(f"/api/v1/managed-databases/{db_id}/migrations/apply")
    stmp = c.post(f"/api/v1/managed-databases/{db_id}/migrations/stamp?version=0001")
    check("stamp HTTP 200", stmp.status_code == 200)
    check("stamp sets current=0001 (no SQL run)", stmp.json()["data"]["current_version"] == "0001")
    # column 'status' should STILL exist (stamp didn't run downgrade)
    insp3 = inspect(target_engine(engine_key, dbname).connect())
    cols3 = [col["name"] for col in (insp3.get_columns("orders") if engine_key != "postgresql"
                                     else insp3.get_columns("orders", schema="public"))]
    check("stamp did NOT alter schema (status col remains)", "status" in cols3)

    # 13) ROB1 — cuarentena: una migración que FALLA en el motor pone la BD en
    # cuarentena (status=error) y bloquea el siguiente apply salvo force=true.
    q_slug = f"qtest-{engine_key}"
    q_mid = c.post("/api/v1/database-models",
                   json={"name": f"Q-{engine_key}", "slug": q_slug}).json()["data"]["id"]
    c.post(f"/api/v1/database-models/{q_mid}/migrations", json={
        "version": "0001", "name": "ok", "up_sql": "CREATE TABLE q_ok (id INT)"})
    c.post(f"/api/v1/database-models/{q_mid}/migrations", json={
        "version": "0002", "name": "bad", "up_sql": "CREATE TABLE (((sintaxis invalida"})
    q_db = f"vqt_{engine_key[:2]}"
    try:
        with server_engine(engine_key).connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                text(f"DROP DATABASE IF EXISTS {q_db}"))
    except Exception:
        pass
    q_db_id = c.post("/api/v1/managed-databases?provision=true", json={
        "server_id": sid, "owner_id": oid, "name": q_db, "model_id": q_mid,
    }).json()["data"]["id"]

    qa = c.post(f"/api/v1/managed-databases/{q_db_id}/migrations/apply")
    qad = qa.json()["data"]
    check("quarantine: 0001 aplicada, 0002 falla", qad["applied_count"] == 1 and qad["failed"] is True)
    check("quarantine: respuesta marca quarantined", qad.get("quarantined") is True)
    md_state = c.get(f"/api/v1/managed-databases/{q_db_id}").json()["data"]
    check("quarantine: status=error en inventario", md_state["status"] == "error")
    blocked = c.post(f"/api/v1/managed-databases/{q_db_id}/migrations/apply")
    check("quarantine: re-apply sin force -> 409", blocked.status_code == 409)
    forced = c.post(f"/api/v1/managed-databases/{q_db_id}/migrations/apply?force=true")
    check("quarantine: force bypassa el guard (200, vuelve a fallar)",
          forced.status_code == 200 and forced.json()["data"]["failed"] is True)

    # 14) Plan 09 + UX (adopt/reconcile/snapshot/from-snapshot/secuencial/autoversión).
    run_plan09(c, sid, oid, engine_key)

    return mid


def main():
    db = Database()
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    limiter.enabled = False
    import main as app_main
    with TestClient(app_main.app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        for engine_key in ("mysql", "mariadb", "postgresql"):
            try:
                run_for(c, engine_key)
            except Exception as ex:
                import traceback
                traceback.print_exc()
                failures.append(f"{engine_key}: EXCEPTION {ex}")

    print("\n===== SUMMARY =====")
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
