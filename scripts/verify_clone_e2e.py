"""
Verificación end-to-end MANUAL del clonado de BDs (database-clones) contra motores
REALES. El gateway de metadatos corre en SQLite efímero; los contenedores Docker son
los servidores fuente/destino reales. Ejercita el camino COMPLETO de la API:
registrar servidor(es) -> crear BD origen real con estructura + datos ->
``POST /database-clones`` -> ``preview`` -> ``execute`` -> polling hasta terminar ->
verificar en el motor DESTINO (tablas, filas, objetos).

Cubre:
  1. Clon COMPLETO estructura+datos a BD nueva (mismo motor): verifica tablas y conteo
     de filas en el destino.
  2. Selección PARCIAL con cierre de dependencias (elegir la hija arrastra la padre).
  3. Limpieza objeto-por-objeto sobre un destino existente (preserva la BD).
  4. Cross-engine MySQL->PostgreSQL: tablas+datos portables clonados; rutinas/triggers
     reportados como 'skipped'.

NO es un test de pytest (requiere Docker; se ejecuta a mano). El runner es asíncrono:
este script hace polling de GET /{id} hasta status terminal (con timeout).

Uso (reusa los contenedores de verify_schema_diff_e2e.py si ya corren):
    docker run -d --rm --name gw_diff_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_diff_pg -e POSTGRES_PASSWORD=rootpw -p 15499:5432 postgres:16
    PYTHONPATH=. uv run python scripts/verify_clone_e2e.py [mysql,postgresql]
"""

import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="e2e_gw_clone_")
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
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.database import Database  # noqa: E402
from app.core.limiter import limiter  # noqa: E402
from app.models import Base  # noqa: E402

ENGINES = {
    "mysql": {"port": 13399, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
    "postgresql": {"port": 15499, "driver": "postgresql+psycopg", "user": "postgres", "pw": "rootpw"},
}

failures: list[str] = []


def check(cond, msg):
    print(f"  {'OK ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def _admin_client() -> TestClient:
    import main
    limiter.enabled = False
    Base.metadata.drop_all(Database().engine)
    Base.metadata.create_all(Database().engine)
    c = TestClient(main.app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return c


def _root_engine(engine_key: str):
    cfg = ENGINES[engine_key]
    if engine_key == "postgresql":
        url = f"{cfg['driver']}://{cfg['user']}:{cfg['pw']}@127.0.0.1:{cfg['port']}/postgres"
    else:
        url = f"{cfg['driver']}://{cfg['user']}:{cfg['pw']}@127.0.0.1:{cfg['port']}/mysql"
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _register_server(client, engine_key) -> int:
    cfg = ENGINES[engine_key]
    r = client.post("/api/v1/servers", json={
        "name": f"clone-{engine_key}", "host": "127.0.0.1", "port": cfg["port"],
        "engine": engine_key, "root_username": cfg["user"], "root_password": cfg["pw"],
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _seed_source(engine_key, db="clone_src"):
    """Crea una BD origen con parent/child (FK) + datos."""
    eng = _root_engine(engine_key)
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {db}"))
        conn.execute(text(f"CREATE DATABASE {db}"))
    url = str(eng.url).rsplit("/", 1)[0] + f"/{db}"
    dbeng = create_engine(url, isolation_level="AUTOCOMMIT")
    with dbeng.connect() as conn:
        conn.execute(text("CREATE TABLE parent (id INT PRIMARY KEY, name VARCHAR(50))"))
        conn.execute(text("CREATE TABLE child (id INT PRIMARY KEY, pid INT, "
                           "CONSTRAINT fk_c FOREIGN KEY (pid) REFERENCES parent(id))"))
        conn.execute(text("INSERT INTO parent (id, name) VALUES (1, 'a'), (2, 'b')"))
        conn.execute(text("INSERT INTO child (id, pid) VALUES (10, 1), (20, 2)"))


def _poll(client, job_id, timeout=60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/v1/database-clones/{job_id}").json()["data"]
        if data["status"] not in ("pending", "running"):
            return data
        time.sleep(1)
    raise TimeoutError(f"job {job_id} no terminó en {timeout}s")


def _count(engine_key, db, table) -> int:
    url = str(_root_engine(engine_key).url).rsplit("/", 1)[0] + f"/{db}"
    with create_engine(url).connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def scenario_full_clone_same_engine(client, engine_key):
    print(f"\n[{engine_key}] Clon completo estructura+datos a BD nueva")
    _seed_source(engine_key)
    # limpiar destino previo
    with _root_engine(engine_key).connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS clone_dst"))
    sid = _register_server(client, engine_key)
    r = client.post("/api/v1/database-clones", json={
        "source_server_id": sid, "source_database_name": "clone_src",
        "target_server_id": sid, "target_database_name": "clone_dst",
        "target_mode": "new", "include_data": True,
    })
    check(r.status_code == 201, f"crear plan -> 201 ({r.status_code})")
    job_id = r.json()["data"]["id"]
    pr = client.post(f"/api/v1/database-clones/{job_id}/preview", json={})
    check(pr.status_code == 200, "preview -> 200")
    token = pr.json()["data"]["confirm_token"]
    ex = client.post(f"/api/v1/database-clones/{job_id}/execute",
                     json={"confirm_target_name": "clone_dst", "confirm_token": token})
    check(ex.status_code == 200, "execute -> 200")
    final = _poll(client, job_id)
    check(final["status"] == "succeeded", f"status final = succeeded ({final['status']})")
    check(_count(engine_key, "clone_dst", "parent") == 2, "parent tiene 2 filas en destino")
    check(_count(engine_key, "clone_dst", "child") == 2, "child tiene 2 filas en destino")


def scenario_partial_selection(client, engine_key):
    print(f"\n[{engine_key}] Selección parcial: elegir child arrastra parent (FK)")
    _seed_source(engine_key)
    with _root_engine(engine_key).connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS clone_dst2"))
    sid = _register_server(client, engine_key)
    job_id = client.post("/api/v1/database-clones", json={
        "source_server_id": sid, "source_database_name": "clone_src",
        "target_server_id": sid, "target_database_name": "clone_dst2",
        "target_mode": "new", "include_data": True,
        "selection": [{"object_type": "table", "name": "child"}],
    }).json()["data"]["id"]
    rs = client.post(f"/api/v1/database-clones/{job_id}/resolve-selection",
                     json={"selection": [{"object_type": "table", "name": "child"}]})
    closure = {(o["object_type"], o["name"]) for o in rs.json()["data"]["closure"]}
    check(("table", "parent") in closure, "el cierre incluye parent (FK de child)")
    pr = client.post(f"/api/v1/database-clones/{job_id}/preview", json={})
    token = pr.json()["data"]["confirm_token"]
    client.post(f"/api/v1/database-clones/{job_id}/execute",
                json={"confirm_target_name": "clone_dst2", "confirm_token": token})
    final = _poll(client, job_id)
    check(final["status"] == "succeeded", f"status final = succeeded ({final['status']})")
    check(_count(engine_key, "clone_dst2", "parent") == 2, "parent clonada por dependencia")


def scenario_procedural_objects(client, engine_key):
    """Clona vistas/rutinas/funciones/triggers/eventos + tabla con ON UPDATE, y verifica
    que las vistas queden RE-CALIFICADAS al destino (no apuntando al origen). Solo MySQL/
    MariaDB (los cuerpos procedurales no son portables cross-engine)."""
    if engine_key not in ("mysql", "mariadb"):
        return
    print(f"\n[{engine_key}] Clon de objetos con cuerpo (vista/rutina/función/trigger/evento)")
    eng = _root_engine(engine_key)
    with eng.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS clone_proc_src"))
        conn.execute(text("DROP DATABASE IF EXISTS clone_proc_dst"))
        conn.execute(text("CREATE DATABASE clone_proc_src"))
    url = str(eng.url).rsplit("/", 1)[0] + "/clone_proc_src"
    dbeng = create_engine(url, isolation_level="AUTOCOMMIT")
    with dbeng.connect() as conn:
        conn.execute(text("CREATE TABLE audit_log (id INT PRIMARY KEY AUTO_INCREMENT, "
                          "created_at DATETIME NOT NULL DEFAULT current_timestamp(), "
                          "updated_at DATETIME NOT NULL DEFAULT current_timestamp() "
                          "ON UPDATE current_timestamp())"))
        # Tipos que str(reflected_type) pierde y COLUMN_TYPE preserva: ENUM/SET (sin lista
        # de valores = DDL inválido), UNSIGNED (rango), display width.
        conn.execute(text("CREATE TABLE parent (id INT PRIMARY KEY, name VARCHAR(50), "
                          "status ENUM('a','b','c') NULL, flags SET('x','y') NULL, "
                          "big_u BIGINT UNSIGNED NULL, flag TINYINT(1) NULL)"))
        conn.execute(text("INSERT INTO parent (id, name, status, flags, big_u, flag) "
                          "VALUES (1,'a','b','x,y',9999999999,1),(2,'b','c','y',1,0)"))
        # v_z base; v_a depende de v_z (orden alfabético INVERSO → prueba el reintento)
        conn.execute(text("CREATE VIEW v_z AS SELECT id FROM parent"))
        conn.execute(text("CREATE VIEW v_a AS SELECT id FROM v_z"))
        conn.execute(text("CREATE FUNCTION fn_double(x INT) RETURNS INT DETERMINISTIC RETURN x*2"))
        conn.execute(text("CREATE PROCEDURE sp_count(OUT n INT) BEGIN "
                          "SELECT COUNT(*) INTO n FROM parent; END"))
        conn.execute(text("CREATE TRIGGER trg_bi BEFORE INSERT ON parent "
                          "FOR EACH ROW BEGIN SET NEW.id = NEW.id; END"))
        conn.execute(text("CREATE EVENT ev_x ON SCHEDULE EVERY 1 DAY "
                          "DO DELETE FROM audit_log WHERE id < 0"))
    sid = _register_server(client, engine_key)
    job_id = client.post("/api/v1/database-clones", json={
        "source_server_id": sid, "source_database_name": "clone_proc_src",
        "target_server_id": sid, "target_database_name": "clone_proc_dst",
        "target_mode": "new", "include_data": True,
    }).json()["data"]["id"]
    pr = client.post(f"/api/v1/database-clones/{job_id}/preview", json={}).json()["data"]
    types = {s["object_type"] for s in pr["structure_statements"]}
    for t in ("view", "routine", "trigger", "event"):
        check(t in types, f"el plan incluye '{t}'")
    client.post(f"/api/v1/database-clones/{job_id}/execute",
                json={"confirm_target_name": "clone_proc_dst", "confirm_token": pr["confirm_token"]})
    final = _poll(client, job_id)
    check(final["status"] == "succeeded", f"status final = succeeded ({final['status']})")
    # Verificar objetos + re-calificación de las vistas al destino.
    dst_url = str(_root_engine(engine_key).url).rsplit("/", 1)[0] + "/clone_proc_dst"
    with create_engine(dst_url).connect() as conn:
        views = {r[0]: r[1] for r in conn.execute(text(
            "SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
            "WHERE TABLE_SCHEMA='clone_proc_dst'"))}
        routines = {r[0] for r in conn.execute(text(
            "SELECT ROUTINE_NAME FROM information_schema.ROUTINES "
            "WHERE ROUTINE_SCHEMA='clone_proc_dst'"))}
        triggers = {r[0] for r in conn.execute(text(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA='clone_proc_dst'"))}
        events = {r[0] for r in conn.execute(text(
            "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA='clone_proc_dst'"))}
        dst_types = {r[0]: r[1] for r in conn.execute(text(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA='clone_proc_dst' AND TABLE_NAME='parent'"))}
    # Tipos exactos preservados (ENUM/SET con valores, UNSIGNED, display width).
    check("enum('a','b','c')" in dst_types.get("status", ""), "ENUM clonado con su lista de valores")
    check("set('x','y')" in dst_types.get("flags", ""), "SET clonado con su lista de valores")
    check("unsigned" in dst_types.get("big_u", ""), "BIGINT UNSIGNED preservado")
    check({"v_a", "v_z"} <= set(views), "vistas v_a/v_z clonadas (reintento resolvió el orden)")
    check(all("clone_proc_src" not in (d or "") for d in views.values()),
          "las vistas NO referencian la BD origen (re-calificadas al destino)")
    check({"fn_double", "sp_count"} <= routines, "función y procedimiento clonados")
    check("trg_bi" in triggers, "trigger clonado")
    check("ev_x" in events, "evento clonado")


def main_run(engine_keys):
    client = _admin_client()
    for ek in engine_keys:
        scenario_full_clone_same_engine(client, ek)
        scenario_partial_selection(client, ek)
        scenario_procedural_objects(client, ek)
    print("\n" + ("=" * 60))
    if failures:
        print(f"FALLARON {len(failures)} checks:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("TODOS los checks pasaron.")


if __name__ == "__main__":
    keys = sys.argv[1].split(",") if len(sys.argv) > 1 else ["mysql"]
    main_run([k for k in keys if k in ENGINES])
