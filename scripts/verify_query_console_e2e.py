"""
Verificación end-to-end MANUAL de la consola SQL (``query-console``) contra motores
REALES. El gateway de metadatos corre en SQLite efímero; los contenedores Docker son los
servidores destino reales. Ejercita el camino completo de la API: registrar servidor ->
sembrar datos/objetos reales -> ``POST /servers/{id}/query/preview`` ->
``POST /servers/{id}/query/execute`` -> verificar tanto la RESPUESTA de la API como el
estado FÍSICO en el motor (nunca confiar solo en el código HTTP).

Por qué esto no se puede verificar con SQLite (ver ``tests/test_query_policy.py`` y
``tests/test_query_runner_execution.py`` para lo que SÍ está cubierto sin motor real):

  (a) que ``START TRANSACTION READ ONLY`` / ``SET TRANSACTION READ ONLY`` RECHACEN de
      verdad una escritura que la clasificación estática dejó pasar como lectura
      (``SELECT fn_que_escribe()``, y el caso concreto encontrado en la auditoría de QA:
      ``SELECT ... /*!40101 FOR UPDATE*/`` en MySQL/MariaDB, invisible para el AST de
      sqlglot pero real para el motor);
  (b) que ``SET ROLE`` (modo ``impersonate``) aplique los permisos del rol adoptado DE
      VERDAD, incluida Row Level Security en PostgreSQL;
  (c) que el tope de filas (``max_rows`` + ``stream_results``) no traiga la tabla entera
      por red antes de recortarla;
  (d) el mensaje y código NATIVO exacto de un rechazo por permisos en los tres motores;
  (e) el caveat de MySQL/MariaDB documentado pero nunca verificado contra un motor real:
      un DDL en ``dry_run`` hace COMMIT implícito y el ROLLBACK del gateway NO lo revierte
      (en PostgreSQL, con DDL transaccional, si SÍ se revierte).

NO es un test de pytest (requiere Docker; se ejecuta a mano). Reusa los contenedores de
``verify_schema_diff_e2e.py``/``verify_clone_e2e.py`` si ya están corriendo.

Uso:
    docker run -d --rm --name gw_diff_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_diff_pg -e POSTGRES_PASSWORD=rootpw -p 15499:5432 postgres:16
    PYTHONPATH=. uv run python scripts/verify_query_console_e2e.py [mysql,postgresql]
"""

import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="e2e_gw_query_console_")
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
    # MariaDB necesita SU PROPIO contenedor/puerto (no reusar el de mysql): ajustar el
    # puerto acá si se corre, p.ej. ``docker run ... -p 13309:3306 mariadb:11``.
    "mariadb": {"port": 13309, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
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


def _root_engine(engine_key: str, database: str | None = None):
    cfg = ENGINES[engine_key]
    db = database or ("postgres" if engine_key == "postgresql" else "mysql")
    url = f"{cfg['driver']}://{cfg['user']}:{cfg['pw']}@127.0.0.1:{cfg['port']}/{db}"
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _register_server(client, engine_key, *, name_suffix="") -> int:
    cfg = ENGINES[engine_key]
    r = client.post("/api/v1/servers", json={
        "name": f"qc-{engine_key}{name_suffix}", "host": "127.0.0.1", "port": cfg["port"],
        "engine": engine_key, "root_username": cfg["user"], "root_password": cfg["pw"],
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _recreate_db(engine_key, db):
    with _root_engine(engine_key).connect() as conn:
        if engine_key == "postgresql":
            conn.execute(text(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{db}' AND pid <> pg_backend_pid()"
            ))
        conn.execute(text(f"DROP DATABASE IF EXISTS {db}"))
        conn.execute(text(f"CREATE DATABASE {db}"))


def _preview(client, sid, sql, database, **kw):
    payload = {"database": database, "sql": sql}
    payload.update(kw)
    return client.post(f"/api/v1/servers/{sid}/query/preview", json=payload)


def _execute(client, sid, sql, database, **kw):
    payload = {"database": database, "sql": sql}
    payload.update(kw)
    return client.post(f"/api/v1/servers/{sid}/query/execute", json=payload)


# --------------------------------------------------------------------------- #
# (a) READ ONLY rechaza de verdad una escritura que la clasificación dejó pasar   #
# --------------------------------------------------------------------------- #
def scenario_read_only_txn_rejects_write_hidden_in_a_function(client, engine_key):
    """
    ``SELECT fn_que_escribe()`` clasifica ``read`` (la raíz es un SELECT; la política
    no puede saber que la función escribe). La garantía la debe dar el MOTOR.
    """
    print(f"\n[{engine_key}] READ ONLY rechaza un SELECT de función que escribe")
    db = "qc_readonly"
    _recreate_db(engine_key, db)
    eng = create_engine(str(_root_engine(engine_key).url).rsplit("/", 1)[0] + f"/{db}")
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE t (id SERIAL PRIMARY KEY, val INT)")
                     if engine_key == "postgresql"
                     else text("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, val INT)"))
        conn.commit()
        if engine_key == "postgresql":
            conn.execute(text(
                "CREATE FUNCTION fn_writes() RETURNS int AS "
                "$$ INSERT INTO t(val) VALUES (1) RETURNING 1 $$ LANGUAGE sql"
            ))
        else:
            # log_bin_trust_function_creators: crear funciones con DML sin ser SUPER.
            conn.execute(text("SET GLOBAL log_bin_trust_function_creators = 1"))
            conn.execute(text(
                "CREATE FUNCTION fn_writes() RETURNS INT DETERMINISTIC "
                "BEGIN INSERT INTO t(val) VALUES (1); RETURN 1; END"
            ))
        conn.commit()

    sid = _register_server(client, engine_key, name_suffix="-ro")
    r = _execute(client, sid, "SELECT fn_writes()", db)
    check(r.status_code == 200, f"execute -> 200 ({r.status_code})")
    data = r.json()["data"]
    check(data["danger"] == "read", f"clasificado como read (root=SELECT): {data['danger']}")
    check(data["success"] is False, "el motor RECHAZA la escritura dentro de READ ONLY")
    err = data["statements"][0]["error"]
    print(f"    mensaje nativo: {err}")
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM t")).scalar()
    check(n == 0, f"ninguna fila quedó insertada en la BD real (t tiene {n})")


def scenario_for_update_hidden_in_mysql_comment_is_rejected_at_runtime(client, engine_key):
    """
    Hallazgo de la auditoría de cobertura (ver ``tests/test_query_policy.py``): sqlglot
    NO tokeniza el contenido de ``/*!... */`` (comentario ejecutable de MySQL), así que
    ``SELECT ... /*!40101 FOR UPDATE*/`` clasifica ``read`` (el ``exp.Lock`` nunca
    aparece en el AST). Este escenario prueba que, aun así, MySQL/MariaDB RECHAZAN el
    ``FOR UPDATE`` real dentro de la transacción de solo lectura (errno 1792) — la
    garantía la da el motor, no la política. Solo aplica a MySQL/MariaDB (PostgreSQL no
    tiene esta sintaxis de comentario).
    """
    if engine_key not in ("mysql", "mariadb"):
        return
    print(f"\n[{engine_key}] FOR UPDATE escondido en comentario ejecutable -> lo rechaza el motor")
    db = "qc_forupdate"
    _recreate_db(engine_key, db)
    eng = create_engine(str(_root_engine(engine_key).url).rsplit("/", 1)[0] + f"/{db}")
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INT PRIMARY KEY, val INT)"))
        conn.execute(text("INSERT INTO t VALUES (1, 10)"))
        conn.commit()

    sid = _register_server(client, engine_key, name_suffix="-fu")
    sql = "SELECT * FROM t /*!40101 FOR UPDATE*/"
    r = _execute(client, sid, sql, db)
    data = r.json()["data"]
    check(data["danger"] == "read", f"la política lo clasifica read (hueco conocido): {data['danger']}")
    check(data["success"] is False, "el motor rechaza el FOR UPDATE dentro de READ ONLY")
    err = data["statements"][0]["error"] or {}
    check(str(err.get("code")) == "1792", f"errno 1792 (READ ONLY transaction): {err}")


# --------------------------------------------------------------------------- #
# (b) SET ROLE aplica los permisos del rol adoptado, incluida RLS                #
# --------------------------------------------------------------------------- #
def scenario_impersonate_applies_row_level_security(client):
    print("\n[postgresql] impersonate + SET ROLE respeta Row Level Security")
    db = "qc_rls"
    _recreate_db("postgresql", db)
    eng = create_engine(str(_root_engine("postgresql").url).rsplit("/", 1)[0] + f"/{db}")
    with eng.connect() as conn:
        conn.execute(text("CREATE ROLE alice LOGIN PASSWORD 'x'"))
        conn.execute(text("CREATE TABLE secret (id serial PRIMARY KEY, owner text, val text)"))
        conn.execute(text("GRANT SELECT ON secret TO alice"))
        conn.execute(text("ALTER TABLE secret ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("CREATE POLICY p_owner ON secret USING (owner = current_user)"))
        conn.execute(text(
            "INSERT INTO secret (owner, val) VALUES ('alice','solo de alice'), "
            "('bob','solo de bob')"
        ))
        conn.commit()

    sid = _register_server(client, "postgresql", name_suffix="-rls")
    # Como pseudo-root (superusuario): RLS no aplica, ve las DOS filas.
    r_admin = _execute(client, sid, "SELECT owner FROM secret ORDER BY owner", db)
    admin_owners = [row[0] for row in r_admin.json()["data"]["statements"][0]["rows"]]
    check(admin_owners == ["alice", "bob"], f"admin (superuser) ve todo, RLS no aplica: {admin_owners}")

    # Impersonando a 'alice': RLS SÍ debe aplicar, solo su fila.
    r_alice = _execute(
        client, sid, "SELECT owner FROM secret", db,
        connection={"mode": "impersonate", "role": "alice"},
    )
    check(r_alice.status_code == 200, f"execute como alice -> 200 ({r_alice.status_code})")
    alice_owners = [row[0] for row in r_alice.json()["data"]["statements"][0]["rows"]]
    check(alice_owners == ["alice"], f"RLS filtra a solo la fila de alice: {alice_owners}")


# --------------------------------------------------------------------------- #
# (c) El tope de filas no trae la tabla entera por red                          #
# --------------------------------------------------------------------------- #
def scenario_max_rows_does_not_download_the_whole_table(client, engine_key):
    print(f"\n[{engine_key}] max_rows no baja la tabla entera por red")
    db = "qc_bigtable"
    _recreate_db(engine_key, db)
    eng = create_engine(str(_root_engine(engine_key).url).rsplit("/", 1)[0] + f"/{db}")
    n_rows = 200_000
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE big (id INT PRIMARY KEY, payload TEXT)"))
        conn.commit()
        batch = [{"id": i, "payload": "x" * 500} for i in range(n_rows)]
        for i in range(0, n_rows, 5000):
            conn.execute(
                text("INSERT INTO big (id, payload) VALUES (:id, :payload)"),
                batch[i : i + 5000],
            )
        conn.commit()

    sid = _register_server(client, engine_key, name_suffix="-big")
    started = time.monotonic()
    r = _execute(client, sid, "SELECT * FROM big", db, max_rows=10)
    elapsed = time.monotonic() - started
    data = r.json()["data"]
    stmt = data["statements"][0]
    check(stmt["truncated"] is True, "la respuesta se marca truncated=true")
    check(stmt["row_count"] == 10, f"solo 10 filas en la respuesta ({stmt['row_count']})")
    payload_bytes = len(r.content)
    check(payload_bytes < 50_000, f"la respuesta HTTP es chica ({payload_bytes} bytes) pese a {n_rows} filas reales")
    check(elapsed < 5.0, f"responde en {elapsed:.2f}s (no escanea/serializa {n_rows} filas)")


# --------------------------------------------------------------------------- #
# (d) Mensaje/código nativo exacto de un rechazo por permisos                    #
# --------------------------------------------------------------------------- #
def scenario_native_permission_denied_message(client, engine_key):
    print(f"\n[{engine_key}] mensaje nativo de un rechazo por permisos")
    db = "qc_perms"
    _recreate_db(engine_key, db)
    eng = create_engine(str(_root_engine(engine_key).url).rsplit("/", 1)[0] + f"/{db}")
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE pagos (id INT PRIMARY KEY, monto INT)"))
        conn.execute(text("INSERT INTO pagos VALUES (1, 100)"))
        conn.execute(text("CREATE TABLE otros (id INT PRIMARY KEY)"))
        if engine_key == "postgresql":
            conn.execute(text("CREATE ROLE app_ro LOGIN PASSWORD 'x'"))
            conn.execute(text("GRANT SELECT ON otros TO app_ro"))
        else:
            conn.execute(text("CREATE USER 'app_ro'@'%' IDENTIFIED BY 'x'"))
            conn.execute(text(f"GRANT SELECT ON {db}.otros TO 'app_ro'@'%'"))
            conn.execute(text("FLUSH PRIVILEGES"))
        conn.commit()

    sid = _register_server(client, engine_key, name_suffix="-perm")
    r = _execute(
        client, sid, "SELECT * FROM pagos", db,
        connection={"mode": "provided", "username": "app_ro", "password": "x"},
    )
    check(r.status_code == 200, f"un rechazo de permisos es 200, no 4xx/5xx ({r.status_code})")
    data = r.json()["data"]
    check(data["success"] is False, "success=false")
    err = data["statements"][0]["error"]
    print(f"    código={err['code']!r} sqlstate={err['sqlstate']!r} mensaje={err['message']!r}")
    if engine_key in ("mysql", "mariadb"):
        check(str(err["code"]) == "1142", f"errno 1142 (command denied): {err['code']}")
        check("command denied" in err["message"].lower(), "mensaje contiene 'command denied'")
    else:
        check(err["sqlstate"] == "42501", f"SQLSTATE 42501 (insufficient_privilege): {err['sqlstate']}")
        check("permission denied" in err["message"].lower(), "mensaje contiene 'permission denied'")


# --------------------------------------------------------------------------- #
# Defensa en profundidad: lo bloqueado NUNCA toca el motor (verificado en vivo)  #
# --------------------------------------------------------------------------- #
def scenario_blocked_statement_never_touches_the_engine(client, engine_key):
    print(f"\n[{engine_key}] una sentencia bloqueada no deja rastro en el motor")
    db = "qc_blocked"
    _recreate_db(engine_key, db)
    sid = _register_server(client, engine_key, name_suffix="-blk")
    grant_sql = (
        "GRANT SELECT ON *.* TO 'nadie'@'%'" if engine_key in ("mysql", "mariadb")
        else "GRANT SELECT ON ALL TABLES IN SCHEMA public TO PUBLIC"
    )
    r = _execute(client, sid, grant_sql, db)
    check(r.status_code == 403, f"bloqueado -> 403 sin tocar el motor ({r.status_code})")
    eng = _root_engine(engine_key)
    with eng.connect() as conn:
        if engine_key in ("mysql", "mariadb"):
            exists = conn.execute(text(
                "SELECT COUNT(*) FROM mysql.user WHERE User='nadie'"
            )).scalar()
            check(exists == 0, "el usuario 'nadie' NUNCA se creó en el motor")


# --------------------------------------------------------------------------- #
# (e) DDL en dry_run: MySQL/MariaDB hacen commit implícito, PostgreSQL no        #
# --------------------------------------------------------------------------- #
def scenario_dry_run_ddl_commit_behavior(client, engine_key):
    print(f"\n[{engine_key}] dry_run + DDL: commit implícito (MySQL/MariaDB) vs transaccional (PG)")
    db = "qc_dryrun"
    _recreate_db(engine_key, db)
    sid = _register_server(client, engine_key, name_suffix="-dr")
    sql = "CREATE TABLE nueva (id INT PRIMARY KEY)"
    pr = _preview(client, sid, sql, db)
    token = pr.json()["data"]["confirm_token"]
    r = _execute(client, sid, sql, db, confirm_target_name=db, confirm_token=token, dry_run=True)
    data = r.json()["data"]
    check(data["committed"] is False, "la respuesta reporta committed=false")

    eng = create_engine(str(_root_engine(engine_key).url).rsplit("/", 1)[0] + f"/{db}")
    with eng.connect() as conn:
        if engine_key in ("mysql", "mariadb"):
            existe = conn.execute(text(
                f"SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_schema='{db}' AND table_name='nueva'"
            )).scalar()
            check(existe == 1, "MySQL/MariaDB: la tabla SÍ quedó creada (commit implícito de DDL)")
            check(
                any("COMMIT implícito" in w for w in data["warnings"]),
                "la respuesta avisa que el dry-run no revierte DDL en este motor",
            )
        else:
            existe = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='nueva'"
            )).scalar()
            check(existe == 0, "PostgreSQL: la tabla NO quedó creada (DDL transaccional, sí se revierte)")


def main_run(engine_keys):
    client = _admin_client()
    for ek in engine_keys:
        scenario_read_only_txn_rejects_write_hidden_in_a_function(client, ek)
        scenario_for_update_hidden_in_mysql_comment_is_rejected_at_runtime(client, ek)
        scenario_max_rows_does_not_download_the_whole_table(client, ek)
        scenario_native_permission_denied_message(client, ek)
        scenario_blocked_statement_never_touches_the_engine(client, ek)
        scenario_dry_run_ddl_commit_behavior(client, ek)
    if "postgresql" in engine_keys:
        scenario_impersonate_applies_row_level_security(client)
    print("\n" + ("=" * 60))
    if failures:
        print(f"FALLARON {len(failures)} checks:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("TODOS los checks pasaron.")


if __name__ == "__main__":
    keys = sys.argv[1].split(",") if len(sys.argv) > 1 else ["mysql", "postgresql"]
    main_run([k for k in keys if k in ENGINES])
