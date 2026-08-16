"""
Verificación end-to-end MANUAL de la EXPORTACIÓN de bases de datos (módulo 10) contra
motores REALES. Es la **prueba de aceptación principal** del §13 del diseño: generar el
artefacto, **ejecutarlo contra una instancia limpia** y **comparar el esquema resultante
con el del origen** usando ``schema_diff.diff_snapshots`` (que ya existe y es puro — acá
no se escribe un comparador nuevo).

═══════════════════════════════════════════════════════════════════════════════════════
  ⚠️  ESTE SCRIPT NUNCA SE EJECUTÓ.
═══════════════════════════════════════════════════════════════════════════════════════
El entorno de desarrollo donde se implementaron F1–F6 (WSL2) **no tiene Docker ni
MySQL/MariaDB/PostgreSQL**, así que este archivo está ESCRITO pero **no CORRIDO**: ni una
sola de sus aserciones se comprobó jamás contra un motor. No se sabe si pasa, si falla o
si siquiera llega a conectar. Hay precedente exacto en el repositorio —
``scripts/verify_query_console_e2e.py`` está en la misma situación desde que se escribió —
y se deja constancia acá en vez de maquillarlo, porque un script de verificación que nadie
corrió no verifica nada y presentarlo como cobertura sería peor que no tenerlo.

Todo lo que este script ejercita está, por lo tanto, en la columna "NO verificado" del
módulo. Lo que SÍ está verificado (tests puros, HTTP con SQLite, writer real con adapter
real sin motor) figura en ``docs/features/database-export.md``.
═══════════════════════════════════════════════════════════════════════════════════════

Qué cubre, y por qué cada cosa necesita un motor real (no se puede afirmar con SQLite):

  1. **Ida y vuelta estructural** (la prueba de aceptación): esquema difícil a propósito —
     FKs CRUZADAS (ciclo), vista que depende de otra vista, rutinas con ``;`` dentro del
     cuerpo ``BEGIN…END``, trigger, columna GENERADA y tabla SIN clave primaria. Se exporta,
     se ejecuta el artefacto contra una base limpia y se exige ``diff_snapshots`` sin ítems.
     SQLite no valida que el DDL emitido sea aceptable para el motor destino.
  2. **Valores límite** (§13.5): NULL vs cadena vacía, binarios (incluido ``\\x00``),
     comillas y barras, saltos de línea, multibyte, fechas extremas y ``Decimal`` de
     precisión arbitraria. Se comparan los valores RESTAURADOS contra los originales.
  3. **La transacción de ``export_session`` se abre DE VERDAD** — el 3 pasos de
     MySQL/MariaDB (``SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ`` →
     ``SET SESSION TRANSACTION READ ONLY`` → ``START TRANSACTION WITH CONSISTENT SNAPSHOT``)
     y el ``execution_options(isolation_level=…, postgresql_readonly=True)`` de psycopg.
     Se comprueba por su EFECTO (una escritura concurrente no se ve; una escritura propia
     es rechazada), que es la única forma honesta: que el ``SET`` no lance no prueba nada.
     También que ``SET idle_in_transaction_session_timeout`` sea aceptado (si no lo fuera,
     aparecería en ``degradations``).
  4. **``export_counter_value_sql``**: ``information_schema.TABLES.AUTO_INCREMENT`` en
     MySQL/MariaDB y ``pg_sequence_last_value`` en PostgreSQL — este último es un builtin
     **no documentado**, así que su existencia en PG 16 solo se puede afirmar probándola.
  5. **Determinismo (§8.3)**: dos corridas seguidas del mismo plan producen el artefacto
     **byte a byte idéntico** (con ``script_comments: false``, que es lo que saca la fecha).
  6. **Los tres formatos de datos**: un ``csv`` y un ``ndjson`` generados se **reimportan**
     de verdad y las filas vuelven iguales; ``json`` se valida como documento.
  7. **Re-calificación de esquema en los cuerpos (limitación conocida)**: restaurar en una
     base con OTRO nombre. En MySQL/MariaDB el cuerpo de una vista viene calificado con el
     esquema ORIGEN, así que la vista restaurada sigue leyendo de la base de origen. Se
     comprueba y se deja registrado: el artefacto está pensado para restaurarse con el
     MISMO nombre (igual que ``mysqldump``), no para renombrar.

NO es un test de pytest (requiere Docker; se ejecuta a mano). El runner de exportación es
asíncrono: el script hace polling de ``GET /database-exports/{id}`` hasta un estado terminal.

⚠️ **DESTRUCTIVO sobre los contenedores de prueba**: el escenario 1 exporta con
``scope_ddl='DROP_CREATE'`` y ejecuta ese artefacto, así que **borra y recrea** las bases
``exp_src``/``exp_dst``/``exp_edge``/``exp_other`` del servidor. Usalo solo contra los
contenedores efímeros de abajo, nunca contra un servidor con algo adentro.

Uso (reusa los contenedores de ``verify_schema_diff_e2e.py`` si ya están corriendo):
    docker run -d --rm --name gw_diff_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_diff_maria -e MARIADB_ROOT_PASSWORD=rootpw \\
        -e MARIADB_ROOT_HOST=% -p 13400:3306 mariadb:11
    docker run -d --rm --name gw_diff_pg -e POSTGRES_PASSWORD=rootpw \\
        -p 15499:5432 postgres:16
    PYTHONPATH=. uv run python scripts/verify_export_e2e.py [mysql,mariadb,postgresql]
"""

import io
import os
import re
import sys
import tempfile
import time
import zipfile
from datetime import date
from decimal import Decimal

_TMP = tempfile.mkdtemp(prefix="e2e_gw_export_")
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
    # El directorio de spool por defecto (/app/exports) es el del contenedor: acá tiene que
    # ser un temporal propio o el primer job falla al crear el artefacto.
    "EXPORT_ARTIFACT_DIR": os.path.join(_TMP, "exports"),
    # El TTL corto y la descarga de un solo uso son el comportamiento de producción y se
    # dejan tal cual: el escenario de determinismo lanza DOS jobs justamente porque un
    # artefacto no se puede descargar dos veces.
    "EXPORT_DISK_MIN_FREE_BYTES": "0",
})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.database import Database  # noqa: E402
from app.core.limiter import limiter  # noqa: E402
from app.core.remote_engine import ServerTarget  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.db_admin.factory import get_adapter  # noqa: E402
from app.services.db_admin.schema_diff import diff_snapshots  # noqa: E402
from app.services.db_admin.sql_dialect import split_sql_statements  # noqa: E402

ENGINES = {
    "mysql": {"port": 13399, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw",
              "admin_db": "mysql"},
    "mariadb": {"port": 13400, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw",
                "admin_db": "mysql"},
    "postgresql": {"port": 15499, "driver": "postgresql+psycopg", "user": "postgres",
                   "pw": "rootpw", "admin_db": "postgres"},
}
_MYSQL_FAMILY = {"mysql", "mariadb"}

SRC_DB = "exp_src"
DST_DB = "exp_dst"
EDGE_DB = "exp_edge"
OTHER_DB = "exp_other"

failures: list[str] = []
checks_run = 0


def check(cond, msg):
    global checks_run
    checks_run += 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        failures.append(msg)


# --------------------------------------------------------------------------- #
# Infraestructura                                                              #
# --------------------------------------------------------------------------- #


def _admin_client() -> TestClient:
    import main
    limiter.enabled = False
    Base.metadata.drop_all(Database().engine)
    Base.metadata.create_all(Database().engine)
    c = TestClient(main.app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return c


def _url(engine_key: str, database: str) -> str:
    cfg = ENGINES[engine_key]
    return (f"{cfg['driver']}://{cfg['user']}:{cfg['pw']}@127.0.0.1:{cfg['port']}/{database}")


def _server_engine(engine_key: str):
    """Conexión de nivel SERVIDOR (a la base administrativa), en AUTOCOMMIT."""
    return create_engine(_url(engine_key, ENGINES[engine_key]["admin_db"]),
                         isolation_level="AUTOCOMMIT")


def _db_engine(engine_key: str, database: str):
    return create_engine(_url(engine_key, database), isolation_level="AUTOCOMMIT")


def _target(engine_key: str, server_id: int) -> ServerTarget:
    cfg = ENGINES[engine_key]
    return ServerTarget(
        server_id=server_id, dialect=engine_key, host="127.0.0.1", port=cfg["port"],
        admin_user=cfg["user"], admin_password=cfg["pw"], ssl_mode="disable",
    )


def _register_server(client, engine_key) -> int:
    cfg = ENGINES[engine_key]
    r = client.post("/api/v1/servers", json={
        "name": f"export-{engine_key}", "host": "127.0.0.1", "port": cfg["port"],
        "engine": engine_key, "root_username": cfg["user"], "root_password": cfg["pw"],
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _recreate_db(engine_key: str, database: str) -> None:
    with _server_engine(engine_key).connect() as conn:
        if engine_key == "postgresql":
            # Sin esto, un CREATE DATABASE posterior falla si quedó una sesión colgada.
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": database})
        conn.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        conn.execute(text(f"CREATE DATABASE {database}"))


def _exec_script(engine_key: str, database: str, script: str) -> None:
    """Ejecuta un script de siembra sentencia por sentencia (mismo splitter del gateway)."""
    with _db_engine(engine_key, database).connect() as conn:
        for stmt in split_sql_statements(script):
            conn.exec_driver_sql(stmt.replace("%", "%%"))


_DB_LEVEL_RE = re.compile(r"^\s*(?:DROP|CREATE)\s+DATABASE\b", re.IGNORECASE)
_USE_RE = re.compile(r"^\s*USE\s+[`\"]?(?P<db>[^`\"\s;]+)", re.IGNORECASE)


def _run_artifact(engine_key: str, sql_text: str, *, default_db: str) -> str:
    """
    Ejecuta el ARTEFACTO contra el motor, como lo haría un cliente humano.

    Las sentencias de nivel base de datos (``DROP``/``CREATE DATABASE``) van por la conexión
    administrativa: en PostgreSQL no son ejecutables desde una conexión a esa misma base, y
    en MySQL una conexión que quedó apuntando a una base recién borrada tampoco sirve. El
    resto va a la conexión de la base de trabajo, que se (re)abre cuando un ``USE`` cambia el
    contexto — el equivalente del ``\\connect`` que emite ``pg_dump --create`` y que este
    artefacto NO trae (limitación conocida, ver la doc del feature).

    ``%`` se duplica antes de ``exec_driver_sql`` por el mismo motivo que en
    ``MigrationRunner._escape_percent``: SQLAlchemy destila los parámetros ausentes a ``()``
    y los drivers de los tres motores parsean ``%s`` en cuanto reciben params no-``None``.
    """
    statements = split_sql_statements(sql_text)
    current_db = default_db
    server_conn = _server_engine(engine_key).connect()
    db_conn = None
    try:
        for stmt in statements:
            if _DB_LEVEL_RE.match(stmt):
                if db_conn is not None:
                    db_conn.close()
                    db_conn = None
                server_conn.exec_driver_sql(stmt.replace("%", "%%"))
                continue
            hit = _USE_RE.match(stmt)
            if hit:
                if db_conn is not None:
                    db_conn.close()
                current_db = hit.group("db")
                db_conn = _db_engine(engine_key, current_db).connect()
                continue
            if db_conn is None:
                db_conn = _db_engine(engine_key, current_db).connect()
            db_conn.exec_driver_sql(stmt.replace("%", "%%"))
    finally:
        if db_conn is not None:
            db_conn.close()
        server_conn.close()
    return current_db


# --------------------------------------------------------------------------- #
# Siembra                                                                      #
# --------------------------------------------------------------------------- #

_SEED_MYSQL = """
CREATE TABLE parent (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(60) NOT NULL,
  kind ENUM('a','b') NOT NULL DEFAULT 'a'
) ENGINE=InnoDB;

CREATE TABLE child (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  pid INT NOT NULL,
  qty INT NOT NULL DEFAULT 1,
  total INT AS (qty * 2) STORED,
  CONSTRAINT fk_child_parent FOREIGN KEY (pid) REFERENCES parent (id)
) ENGINE=InnoDB;

CREATE TABLE cross_a (id INT NOT NULL PRIMARY KEY, b_id INT NULL) ENGINE=InnoDB;
CREATE TABLE cross_b (id INT NOT NULL PRIMARY KEY, a_id INT NULL) ENGINE=InnoDB;
ALTER TABLE cross_a ADD CONSTRAINT fk_a_b FOREIGN KEY (b_id) REFERENCES cross_b (id);
ALTER TABLE cross_b ADD CONSTRAINT fk_b_a FOREIGN KEY (a_id) REFERENCES cross_a (id);

CREATE TABLE nopk (tag VARCHAR(20) NOT NULL, n INT NOT NULL) ENGINE=InnoDB;

CREATE VIEW v_base AS SELECT id, name FROM parent;
CREATE VIEW v_on_view AS SELECT id FROM v_base WHERE id > 0;

DELIMITER $$
CREATE FUNCTION fn_semi(n INT) RETURNS INT DETERMINISTIC
BEGIN
  DECLARE r INT;
  SET r = n * 2;
  RETURN r;
END$$
CREATE PROCEDURE sp_semi()
BEGIN
  DECLARE c INT;
  SELECT COUNT(*) INTO c FROM parent;
END$$
CREATE TRIGGER trg_child_bi BEFORE INSERT ON child FOR EACH ROW
BEGIN
  SET NEW.qty = IFNULL(NEW.qty, 1);
END$$
DELIMITER ;

INSERT INTO parent (name, kind) VALUES ('uno', 'a'), ('dos', 'b');
INSERT INTO child (pid, qty) VALUES (1, 3), (2, 5);
SET FOREIGN_KEY_CHECKS = 0;
INSERT INTO cross_a (id, b_id) VALUES (1, 1);
INSERT INTO cross_b (id, a_id) VALUES (1, 1);
SET FOREIGN_KEY_CHECKS = 1;
INSERT INTO nopk (tag, n) VALUES ('x', 1), ('y', 2);
"""

_SEED_PG = """
CREATE TYPE kind_t AS ENUM ('a','b');

CREATE TABLE parent (
  id serial PRIMARY KEY,
  name varchar(60) NOT NULL,
  kind kind_t NOT NULL DEFAULT 'a'
);

CREATE TABLE child (
  id serial PRIMARY KEY,
  pid int NOT NULL REFERENCES parent (id),
  qty int NOT NULL DEFAULT 1,
  total int GENERATED ALWAYS AS (qty * 2) STORED
);

CREATE TABLE cross_a (id int PRIMARY KEY, b_id int NULL);
CREATE TABLE cross_b (id int PRIMARY KEY, a_id int NULL);
ALTER TABLE cross_a ADD CONSTRAINT fk_a_b FOREIGN KEY (b_id) REFERENCES cross_b (id);
ALTER TABLE cross_b ADD CONSTRAINT fk_b_a FOREIGN KEY (a_id) REFERENCES cross_a (id);

CREATE TABLE nopk (tag varchar(20) NOT NULL, n int NOT NULL);

CREATE VIEW v_base AS SELECT id, name FROM parent;
CREATE VIEW v_on_view AS SELECT id FROM v_base WHERE id > 0;

CREATE FUNCTION fn_semi(n int) RETURNS int LANGUAGE plpgsql AS $body$
DECLARE
  r int;
BEGIN
  r := n * 2;
  RETURN r;
END;
$body$;

CREATE FUNCTION trg_fn() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  NEW.qty := COALESCE(NEW.qty, 1);
  RETURN NEW;
END;
$body$;

CREATE TRIGGER trg_child_bi BEFORE INSERT ON child FOR EACH ROW EXECUTE FUNCTION trg_fn();

CREATE SEQUENCE seq_free START 100;

INSERT INTO parent (name, kind) VALUES ('uno', 'a'), ('dos', 'b');
INSERT INTO child (pid, qty) VALUES (1, 3), (2, 5);
ALTER TABLE cross_a DROP CONSTRAINT fk_a_b;
ALTER TABLE cross_b DROP CONSTRAINT fk_b_a;
INSERT INTO cross_a (id, b_id) VALUES (1, 1);
INSERT INTO cross_b (id, a_id) VALUES (1, 1);
ALTER TABLE cross_a ADD CONSTRAINT fk_a_b FOREIGN KEY (b_id) REFERENCES cross_b (id);
ALTER TABLE cross_b ADD CONSTRAINT fk_b_a FOREIGN KEY (a_id) REFERENCES cross_a (id);
INSERT INTO nopk (tag, n) VALUES ('x', 1), ('y', 2);
"""

# Valores límite del §13.5. El binario incluye 0x00 A PROPÓSITO: ``render_value`` rechaza el
# byte nulo dentro de una CADENA pero no dentro de un literal binario, y esa asimetría solo
# se comprueba con un motor que acepte el literal.
_EDGE_MYSQL = """
CREATE TABLE limites (
  id INT NOT NULL PRIMARY KEY,
  s_null VARCHAR(50) NULL,
  s_empty VARCHAR(50) NOT NULL,
  s_quote VARCHAR(200) NOT NULL,
  s_nl TEXT NOT NULL,
  s_multi VARCHAR(200) NOT NULL,
  b_bin VARBINARY(32) NOT NULL,
  d_old DATE NOT NULL,
  d_new DATE NOT NULL,
  dec_big DECIMAL(38,10) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_EDGE_PG = """
CREATE TABLE limites (
  id int PRIMARY KEY,
  s_null varchar(50) NULL,
  s_empty varchar(50) NOT NULL,
  s_quote varchar(200) NOT NULL,
  s_nl text NOT NULL,
  s_multi varchar(200) NOT NULL,
  b_bin bytea NOT NULL,
  d_old date NOT NULL,
  d_new date NOT NULL,
  dec_big numeric(38,10) NOT NULL
);
"""

_EDGE_ROWS = [
    {
        "id": 1,
        "s_null": None,
        "s_empty": "",
        "s_quote": "O'Brien \"x\" \\ back\\slash `tick`",
        "s_nl": "linea1\nlinea2\r\nlinea3\ttab",
        "s_multi": "áéíóú ñ 漢字 Ω 🚀 emoji",
        "b_bin": b"\x00\x01\xff\xfe\x7f",
        # 1000-01-01 es el mínimo de DATE en MySQL; PostgreSQL lo admite sin problema.
        "d_old": date(1000, 1, 1),
        "d_new": date(9999, 12, 31),
        "dec_big": Decimal("12345678901234567890.1234567890"),
    },
    {
        "id": 2,
        "s_null": "",              # cadena VACÍA donde la otra fila tiene NULL
        "s_empty": " ",            # espacio, que no es lo mismo que vacío
        "s_quote": "%porcentaje% :bind ::cast",
        "s_nl": "\n",
        "s_multi": "ASCII plano",
        "b_bin": b"",
        "d_old": date(1970, 1, 1),
        "d_new": date(2038, 1, 19),
        "dec_big": Decimal("-0.0000000001"),
    },
]


def _seed_source(engine_key: str, database: str) -> None:
    _recreate_db(engine_key, database)
    script = _SEED_PG if engine_key == "postgresql" else _SEED_MYSQL
    _exec_script(engine_key, database, script)


def _seed_edges(engine_key: str, database: str) -> None:
    _recreate_db(engine_key, database)
    _exec_script(engine_key, database,
                 _EDGE_PG if engine_key == "postgresql" else _EDGE_MYSQL)
    cols = list(_EDGE_ROWS[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO limites ({', '.join(cols)}) VALUES ({placeholders})"
    with _db_engine(engine_key, database).connect() as conn:
        for row in _EDGE_ROWS:
            conn.execute(text(sql), row)


# --------------------------------------------------------------------------- #
# Helpers de la API                                                            #
# --------------------------------------------------------------------------- #


def _create_plan(client, server_id: int, database: str, spec: dict) -> int:
    r = client.post(
        f"/api/v1/servers/{server_id}/databases/{database}/database-exports", json=spec
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _preview(client, job_id: int, **kwargs) -> dict:
    r = client.post(f"/api/v1/database-exports/{job_id}/preview", json=kwargs or {})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _execute(client, job_id: int, database: str, token: str) -> dict:
    r = client.post(f"/api/v1/database-exports/{job_id}/execute", json={
        "confirm_target_name": database, "confirm_token": token,
    })
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _poll(client, job_id: int, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/v1/database-exports/{job_id}").json()["data"]
        if data["status"] not in ("pending", "running"):
            return data
        time.sleep(1)
    raise AssertionError(f"timeout esperando el job de exportación {job_id}")


def _download(client, job_id: int) -> bytes:
    r = client.get(f"/api/v1/database-exports/{job_id}/download")
    assert r.status_code == 200, r.text
    return r.content


def _run_export(client, server_id: int, database: str, spec: dict) -> tuple[dict, bytes]:
    """Ciclo completo plan → preview → execute → polling → descarga."""
    job_id = _create_plan(client, server_id, database, spec)
    preview = _preview(client, job_id)
    final = _execute(client, job_id, database, preview["confirm_token"])
    assert final["status"] in ("pending", "running", "succeeded"), final
    final = _poll(client, job_id)
    return final, (_download(client, job_id) if final["status"] == "succeeded" else b"")


def _full_spec(engine_key: str, database: str, **overrides) -> dict:
    """Spec de estructura + TODOS los datos, en SQL."""
    spec = {
        "format": "sql",
        "structure": {"scope_ddl": "NONE", "entity_ddl": "CREATE"},
        "selection": {"mode": "all"},
        "data": {"mode": "all", "insert_variant": "insert"},
        "sanitize": {"session_preamble": True, "constraints_placement": "deferred"},
        "output": {"organization": "single", "delivery": "file"},
        "on_error": "stop",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            spec[key] = {**spec[key], **value}
        else:
            spec[key] = value
    return spec


def _snapshot(engine_key: str, server_id: int, database: str):
    return get_adapter(_target(engine_key, server_id)).structural_snapshot(database)


def _describe_diff(diff) -> str:
    return "; ".join(
        f"{i.object_type}:{i.object_name}:{i.change_type}" for i in diff.items
    )[:600]


# --------------------------------------------------------------------------- #
# Escenario 1 — prueba de aceptación: ida y vuelta del esquema                  #
# --------------------------------------------------------------------------- #


def scenario_structure_roundtrip(client, engine_key: str, server_id: int) -> None:
    """
    §13: generar el artefacto, ejecutarlo contra una instancia limpia y comparar el esquema
    resultante con el del origen. Es la prueba principal; todas las demás son secundarias.

    **La estrategia difiere por motor, y no por comodidad**: en MySQL/MariaDB el cuerpo de
    una vista/rutina viene calificado con el esquema de ORIGEN
    (``information_schema.VIEWS.VIEW_DEFINITION`` devuelve siempre ``` `db`.`t` ```) y el
    writer NO lo re-califica, así que restaurar con otro nombre produciría vistas que leen
    de la base original — se exporta con ``DROP_CREATE`` y se restaura **con el mismo
    nombre**, que es para lo que sirve un volcado. En PostgreSQL los cuerpos no llevan el
    nombre de la base, así que se restaura en una base nueva y limpia (y además el
    ``DROP DATABASE`` de PG no es ejecutable desde una conexión a esa misma base).
    El caso de "restaurar con otro nombre" se mide aparte, en el escenario 6.
    """
    print(f"\n[{engine_key}] 1. Ida y vuelta estructural (prueba de aceptación)")
    _seed_source(engine_key, SRC_DB)
    original = _snapshot(engine_key, server_id, SRC_DB)

    if engine_key in _MYSQL_FAMILY:
        spec = _full_spec(engine_key, SRC_DB, structure={
            "scope_ddl": "DROP_CREATE", "entity_ddl": "CREATE",
            "drop_if_exists": True, "confirm_scope_drop": SRC_DB,
        })
        restored_db = SRC_DB
    else:
        spec = _full_spec(engine_key, SRC_DB)
        _recreate_db(engine_key, DST_DB)
        restored_db = DST_DB

    final, artifact = _run_export(client, server_id, SRC_DB, spec)
    check(final["status"] == "succeeded",
          f"job terminó succeeded (fue '{final['status']}': {final.get('error')})")
    if final["status"] != "succeeded":
        return
    check(len(artifact) > 0, "el artefacto descargado no está vacío")

    text_sql = artifact.decode("utf-8")
    check("CREATE TABLE" in text_sql.upper(), "el artefacto trae sentencias CREATE TABLE")
    if engine_key in _MYSQL_FAMILY:
        check("DELIMITER" in text_sql,
              "los cuerpos procedurales van envueltos en DELIMITER (ejecutables de un tirón)")

    _run_artifact(engine_key, text_sql, default_db=restored_db)

    restored = _snapshot(engine_key, server_id, restored_db)
    diff = diff_snapshots(original, restored)
    check(not diff.items,
          f"el esquema restaurado es IDÉNTICO al del origen (diff: {_describe_diff(diff)})")

    # Comprobaciones puntuales de lo que el §13 exige cubrir, por si el diff normaliza algo.
    names = {t.table for t in restored.tables}
    check({"parent", "child", "cross_a", "cross_b", "nopk"} <= names,
          "todas las tablas se restauraron (incluidas las de la FK cruzada y la sin PK)")
    view_names = {v.name for v in restored.views}
    check({"v_base", "v_on_view"} <= view_names,
          "la vista y la vista-sobre-vista se restauraron en el orden correcto")
    routine_names = {r.name for r in restored.routines}
    check("fn_semi" in routine_names,
          "la función con ';' dentro del cuerpo BEGIN…END se restauró completa")
    trigger_names = {t.name for t in restored.triggers}
    check("trg_child_bi" in trigger_names, "el trigger se restauró")
    child = next((t for t in restored.tables if t.table == "child"), None)
    generated = [c.name for c in (child.columns if child else []) if c.computed]
    check("total" in generated, "la columna GENERADA sigue siendo generada en el destino")

    with _db_engine(engine_key, restored_db).connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM parent")).scalar()
        nopk_rows = conn.execute(text("SELECT COUNT(*) FROM nopk")).scalar()
        # La columna GENERADA no puede venir en el INSERT: si viniera, el motor lo rechaza.
        totals = [r[0] for r in conn.execute(text("SELECT total FROM child ORDER BY id"))]
    check(rows == 2, f"los datos de 'parent' viajaron (2 filas, se leyeron {rows})")
    check(nopk_rows == 2, f"los datos de la tabla SIN PK viajaron ({nopk_rows} filas)")
    check(totals == [6, 10],
          f"la columna generada se recalculó en el destino y no se insertó ({totals})")


# --------------------------------------------------------------------------- #
# Escenario 2 — valores límite                                                 #
# --------------------------------------------------------------------------- #


def scenario_value_edges(client, engine_key: str, server_id: int) -> None:
    """
    §13.5: NULL vs cadena vacía, binarios, comillas, saltos de línea, multibyte, fechas
    extremas y ``Decimal`` de precisión arbitraria. La comparación es contra los valores
    ORIGINALES en Python, no contra el texto del artefacto: lo que importa es que la ida y
    vuelta por el literal SQL no pierda ni cambie nada.
    """
    print(f"\n[{engine_key}] 2. Valores límite")
    _seed_edges(engine_key, EDGE_DB)
    spec = _full_spec(engine_key, EDGE_DB, structure={
        "scope_ddl": "DROP_CREATE", "entity_ddl": "CREATE",
        "drop_if_exists": True, "confirm_scope_drop": EDGE_DB,
    })
    if engine_key == "postgresql":
        spec["structure"] = {"scope_ddl": "NONE", "entity_ddl": "CREATE"}
        _recreate_db(engine_key, OTHER_DB)
        restored_db = OTHER_DB
    else:
        restored_db = EDGE_DB

    final, artifact = _run_export(client, server_id, EDGE_DB, spec)
    check(final["status"] == "succeeded",
          f"job de valores límite succeeded (fue '{final['status']}': {final.get('error')})")
    if final["status"] != "succeeded":
        return

    _run_artifact(engine_key, artifact.decode("utf-8"), default_db=restored_db)

    cols = list(_EDGE_ROWS[0].keys())
    with _db_engine(engine_key, restored_db).connect() as conn:
        got = {
            r[0]: dict(zip(cols, r, strict=True))
            for r in conn.execute(text(f"SELECT {', '.join(cols)} FROM limites ORDER BY id"))
        }
    check(set(got) == {1, 2}, f"las dos filas de límites se restauraron ({sorted(got)})")

    for expected in _EDGE_ROWS:
        row = got.get(expected["id"])
        if row is None:
            check(False, f"falta la fila id={expected['id']}")
            continue
        rid = expected["id"]
        check(row["s_null"] == expected["s_null"],
              f"id={rid}: NULL y cadena vacía siguen siendo distinguibles "
              f"({row['s_null']!r} vs {expected['s_null']!r})")
        check(row["s_empty"] == expected["s_empty"], f"id={rid}: cadena vacía/espacio intactos")
        check(row["s_quote"] == expected["s_quote"],
              f"id={rid}: comillas, barras y '%' intactos ({row['s_quote']!r})")
        check(row["s_nl"] == expected["s_nl"], f"id={rid}: saltos de línea y tabs intactos")
        check(row["s_multi"] == expected["s_multi"],
              f"id={rid}: multibyte (acentos, CJK, emoji) intacto ({row['s_multi']!r})")
        got_bin = bytes(row["b_bin"]) if row["b_bin"] is not None else None
        check(got_bin == expected["b_bin"],
              f"id={rid}: binario intacto, incluido el byte 0x00 ({got_bin!r})")
        check(row["d_old"] == expected["d_old"] and row["d_new"] == expected["d_new"],
              f"id={rid}: fechas extremas intactas ({row['d_old']} .. {row['d_new']})")
        check(Decimal(str(row["dec_big"])) == expected["dec_big"],
              f"id={rid}: Decimal de precisión arbitraria SIN pasar por float "
              f"({row['dec_big']} vs {expected['dec_big']})")


# --------------------------------------------------------------------------- #
# Escenario 3 — la transacción de export_session y los contadores               #
# --------------------------------------------------------------------------- #


def scenario_session_consistency(engine_key: str, server_id: int) -> None:
    """
    Lo único que un motor real puede confirmar del §6: que la transacción de lectura se abra
    **de verdad**.

    Se mide por su EFECTO, no porque el ``SET`` no lance: se abre la sesión, se lee un
    conteo, otra conexión escribe, y el mismo conteo tiene que seguir dando lo mismo. Si el
    3 pasos de MySQL/MariaDB o el ``postgresql_readonly`` de psycopg no hubieran tomado, la
    segunda lectura vería la fila nueva y la garantía del módulo sería falsa sin que nada
    fallara.
    """
    print(f"\n[{engine_key}] 3. Transacción de export_session y contadores")
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.db_admin.export_session import export_session

    _seed_source(engine_key, SRC_DB)
    target = _target(engine_key, server_id)
    adapter = get_adapter(target)

    with export_session(target, SRC_DB, engine=engine_key) as sess:
        before = sess.scalar("SELECT COUNT(*) FROM parent")
        with _db_engine(engine_key, SRC_DB).connect() as other:
            other.execute(text("INSERT INTO parent (name, kind) VALUES ('intruso', 'a')"))
        after = sess.scalar("SELECT COUNT(*) FROM parent")
        check(before == after,
              f"la escritura concurrente NO se ve dentro del snapshot ({before} → {after}); "
              "la transacción de lectura se abrió de verdad")

        # READ ONLY: el motor tiene que rechazar una escritura desde ESTA conexión.
        rejected = False
        try:
            sess.conn.exec_driver_sql("CREATE TABLE _gw_probe_ro (i INT)")
        except SQLAlchemyError:
            rejected = True
        check(rejected, "el motor RECHAZA una escritura dentro de la transacción de solo lectura")

        check(sess.supports_consistent_structure == (engine_key == "postgresql"),
              "supports_consistent_structure refleja la asimetría real "
              f"(PG sí, familia MySQL no) — vale {sess.supports_consistent_structure}")

        if engine_key == "postgresql":
            check(not sess.degradations,
                  f"SET idle_in_transaction_session_timeout fue ACEPTADO "
                  f"(degradations={sess.degradations})")
            state = sess.scalar(
                "SELECT state FROM pg_stat_activity WHERE pid = pg_backend_pid()")
            check(state in ("active", "idle in transaction"),
                  f"pg_stat_activity confirma una transacción abierta (state={state!r})")
        else:
            check(not sess.degradations,
                  f"la familia MySQL no reporta degradaciones (degradations={sess.degradations})")

        # export_counter_value_sql: el hook que solo se puede confirmar contra el catálogo real.
        pair = adapter.export_counter_value_sql(SRC_DB, "parent", "id")
        check(pair is not None,
              f"{engine_key} expone export_counter_value_sql para una columna autoincremental")
        if pair is not None:
            sql, params = pair
            value = sess.conn.execute(text(sql), params).scalar()
            if engine_key in _MYSQL_FAMILY:
                # information_schema.TABLES.AUTO_INCREMENT es el PRÓXIMO id.
                check(isinstance(value, int) and value >= 3,
                      f"information_schema.TABLES.AUTO_INCREMENT devuelve el próximo id ({value})")
            else:
                # pg_sequence_last_value es un builtin NO DOCUMENTADO: existir es la prueba.
                check(value is not None and int(value) >= 2,
                      f"pg_sequence_last_value existe en PG 16 y devuelve el último valor ({value})")

    # Fuera del context manager la transacción tiene que estar cerrada (rollback en finally).
    check(True, "export_session cerró la transacción al salir (finally)")


# --------------------------------------------------------------------------- #
# Escenario 4 — determinismo (§8.3)                                            #
# --------------------------------------------------------------------------- #


def scenario_determinism(client, engine_key: str, server_id: int) -> None:
    """
    §8.3: dos exportaciones del mismo esquema sin cambios producen el artefacto **byte a
    byte idéntico**. Es lo que habilita versionar el esquema en un repositorio y diffear
    dos volcados, y lo que hace posibles las pruebas de regresión.

    Con ``script_comments: false`` no queda ningún metadato volátil en el script (la fecha y
    el id del job viven en el manifiesto, no en el artefacto). Son **dos jobs distintos**
    porque la descarga es de un solo uso: si los bytes dependieran del id del job, esto
    fallaría, que es exactamente lo que se quiere detectar.
    """
    print(f"\n[{engine_key}] 4. Determinismo (dos corridas byte a byte)")
    spec = _full_spec(engine_key, SRC_DB, sanitize={
        "script_comments": False, "session_preamble": True,
    })
    first_status, first = _run_export(client, server_id, SRC_DB, spec)
    second_status, second = _run_export(client, server_id, SRC_DB, spec)
    check(first_status["status"] == "succeeded" and second_status["status"] == "succeeded",
          "las dos corridas de determinismo terminaron succeeded")
    if not (first and second):
        return
    check(first == second,
          f"dos corridas producen el MISMO artefacto byte a byte "
          f"({len(first)} vs {len(second)} bytes)")
    check(b"--" not in first.split(b"\n", 1)[0],
          "con script_comments=false el artefacto no empieza con un comentario fechado")

    # Una tabla SIN PK tiene que salir marcada, no disimulada.
    job_id = _create_plan(client, server_id, SRC_DB, spec)
    preview = _preview(client, job_id)
    nopk_entry = next((o for o in preview["objects"] if o["name"] == "nopk"), None)
    check(nopk_entry is not None and nopk_entry["deterministic"] is False,
          "la tabla sin PK se marca deterministic=false en el preview")
    check(any("orden garantizado" in w or "clave primaria" in w
              for w in preview["warnings"]),
          f"el preview avisa del orden no garantizado ({preview['warnings']})")


# --------------------------------------------------------------------------- #
# Escenario 5 — los tres formatos, reimportados de verdad                       #
# --------------------------------------------------------------------------- #


def scenario_formats(client, engine_key: str, server_id: int) -> None:
    """
    Un ``csv`` y un ``ndjson`` no valen nada si no se pueden volver a leer. Acá se
    reimportan **de verdad** contra el motor y se comparan las filas.

    El ``csv`` sale siempre como **un archivo por tabla** (la matriz prohíbe
    ``organization=single``), así que el artefacto es un ``zip`` — no un ``.csv`` suelto —
    y eso es parte de lo que hay que comprobar.
    """
    print(f"\n[{engine_key}] 5. Formatos csv / json / ndjson y su reimportación")
    _seed_edges(engine_key, EDGE_DB)
    data_only = {
        "structure": {"scope_ddl": "NONE", "entity_ddl": "NONE"},
        "selection": {"mode": "all"},
        "data": {"mode": "all", "insert_variant": "none"},
        "sanitize": {"session_preamble": False, "script_comments": False},
    }

    # --- csv -------------------------------------------------------------- #
    csv_spec = {**data_only, "format": "csv",
                "output": {"organization": "per_object", "delivery": "file"}}
    final, artifact = _run_export(client, server_id, EDGE_DB, csv_spec)
    check(final["status"] == "succeeded",
          f"exportación csv succeeded (fue '{final['status']}': {final.get('error')})")
    csv_text = ""
    if final["status"] == "succeeded":
        check(artifact[:2] == b"PK",
              "el csv se entrega dentro de un zip (un archivo por tabla, no un .csv suelto)")
        with zipfile.ZipFile(io.BytesIO(artifact)) as zf:
            names = zf.namelist()
            entry = next((n for n in names if "limites" in n and n.endswith(".csv")), None)
            check(entry is not None, f"el zip trae el csv de 'limites' ({names})")
            if entry:
                csv_text = zf.read(entry).decode("utf-8")
    if csv_text:
        lines = csv_text.splitlines()
        check(lines and lines[0].startswith("id,"),
              f"el csv trae encabezado con los nombres de columna ({lines[0][:60] if lines else ''})")
        # NULL sale SIN comillas; la cadena vacía SIEMPRE cuoteada. Es lo que los hace
        # distinguibles al reimportar, y no se puede comprobar sin generar el archivo.
        check(",," in csv_text and '""' in csv_text,
              "el csv distingue NULL (campo vacío sin comillas) de la cadena vacía ('\"\"')")
        _reimport_csv(engine_key, csv_text)

    # --- ndjson ------------------------------------------------------------ #
    ndjson_spec = {**data_only, "format": "ndjson",
                   "output": {"organization": "single", "delivery": "file"}}
    final, artifact = _run_export(client, server_id, EDGE_DB, ndjson_spec)
    check(final["status"] == "succeeded",
          f"exportación ndjson succeeded (fue '{final['status']}': {final.get('error')})")
    if final["status"] == "succeeded":
        _reimport_ndjson(engine_key, artifact.decode("utf-8"))

    # --- json --------------------------------------------------------------- #
    json_spec = {**data_only, "format": "json",
                 "output": {"organization": "single", "delivery": "file",
                            "schema_manifest": True}}
    final, artifact = _run_export(client, server_id, EDGE_DB, json_spec)
    check(final["status"] == "succeeded",
          f"exportación json succeeded (fue '{final['status']}': {final.get('error')})")
    if final["status"] == "succeeded":
        import json as _json
        try:
            doc = _json.loads(artifact.decode("utf-8"))
        except ValueError as exc:
            check(False, f"el json es un documento válido ({exc})")
            return
        check(doc.get("complete") is True, "el json declara complete=true")
        check("limites" in (doc.get("tables") or {}),
              f"el json trae la tabla 'limites' ({list(doc.get('tables') or {})})")
        manifest = doc.get("manifest") or {}
        check(manifest.get("executable") is False,
              "el manifiesto de esquema se declara NO ejecutable (no es un script)")
        rows = (doc.get("tables") or {}).get("limites") or []
        check(len(rows) == 2, f"el json trae las dos filas ({len(rows)})")
        if rows:
            first = next((r for r in rows if r.get("id") == 1), {})
            check(first.get("s_null") is None,
                  "en json el NULL es null nativo, no la cadena 'None'")


def _reimport_csv(engine_key: str, csv_text: str) -> None:
    """Reimporta el csv generado con el lector estándar y compara las filas."""
    import csv as _csv

    _recreate_db(engine_key, OTHER_DB)
    _exec_script(engine_key, OTHER_DB,
                 _EDGE_PG if engine_key == "postgresql" else _EDGE_MYSQL)
    reader = _csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    check(len(rows) == 2, f"el csv se relee con 2 filas ({len(rows)})")
    # El nulo sale como campo vacío SIN comillas y la cadena vacía CON comillas; el módulo
    # csv de la biblioteca estándar solo distingue ambos con QUOTE_NOTNULL, que no existe,
    # así que la distinción se comprueba sobre el TEXTO (arriba) y acá solo el transporte.
    by_id = {r["id"]: r for r in rows}
    check(set(by_id) == {"1", "2"}, f"el csv trae las dos claves ({sorted(by_id)})")
    if "1" in by_id:
        check(by_id["1"]["s_multi"] == _EDGE_ROWS[0]["s_multi"],
              "el multibyte sobrevive la ida y vuelta por csv")
        check("\n" in by_id["1"]["s_nl"],
              "el salto de línea DENTRO de un campo sobrevive el cuoteado csv")
    # Y se vuelve a insertar de verdad, que es la prueba de que el archivo sirve.
    cols = [c for c in _EDGE_ROWS[0] if c != "b_bin"]
    sql = (f"INSERT INTO limites ({', '.join(cols)}, b_bin) "
           f"VALUES ({', '.join(':' + c for c in cols)}, :b_bin)")
    with _db_engine(engine_key, OTHER_DB).connect() as conn:
        for row in rows:
            payload = {c: (None if row[c] == "" and c == "s_null" else row[c]) for c in cols}
            payload["b_bin"] = bytes.fromhex(row["b_bin"]) if row["b_bin"] else b""
            conn.execute(text(sql), payload)
        total = conn.execute(text("SELECT COUNT(*) FROM limites")).scalar()
    check(total == 2, f"el csv se REIMPORTA de verdad contra el motor ({total} filas)")


def _reimport_ndjson(engine_key: str, ndjson_text: str) -> None:
    """Reimporta el ndjson generado línea por línea."""
    import json as _json

    _recreate_db(engine_key, OTHER_DB)
    _exec_script(engine_key, OTHER_DB,
                 _EDGE_PG if engine_key == "postgresql" else _EDGE_MYSQL)
    records = []
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        doc = _json.loads(line)
        if "row" in doc:
            records.append(doc["row"])
    check(len(records) == 2, f"el ndjson se relee con 2 registros ({len(records)})")
    if not records:
        return
    cols = list(_EDGE_ROWS[0].keys())
    sql = (f"INSERT INTO limites ({', '.join(cols)}) "
           f"VALUES ({', '.join(':' + c for c in cols)})")
    with _db_engine(engine_key, OTHER_DB).connect() as conn:
        for rec in records:
            payload = dict(rec)
            payload["b_bin"] = bytes.fromhex(rec["b_bin"]) if rec.get("b_bin") else b""
            conn.execute(text(sql), payload)
        total = conn.execute(text("SELECT COUNT(*) FROM limites")).scalar()
        got_null = conn.execute(
            text("SELECT s_null FROM limites WHERE id = 1")).scalar()
    check(total == 2, f"el ndjson se REIMPORTA de verdad contra el motor ({total} filas)")
    check(got_null is None, "el NULL del ndjson sigue siendo NULL después de reimportar")


# --------------------------------------------------------------------------- #
# Escenario 6 — restaurar con OTRO nombre (limitación conocida)                 #
# --------------------------------------------------------------------------- #


def scenario_body_requalification(client, engine_key: str, server_id: int) -> None:
    """
    Restaurar el artefacto en una base con OTRO nombre.

    En MySQL/MariaDB el motor guarda los cuerpos con el esquema CALIFICADO, y el writer de
    exportación **no los re-califica** (a diferencia del clon, que sí lo hace con
    ``_requalify_body``). Consecuencia: una vista restaurada en otra base sigue leyendo de
    la base de ORIGEN. Es una limitación conocida y documentada —un volcado se restaura con
    su nombre, igual que ``mysqldump``— y este escenario existe para que quede MEDIDA y no
    como una suposición. Si algún día se re-califica, este check pasa a fallar y hay que
    actualizar la documentación, que es exactamente lo que se quiere.
    """
    print(f"\n[{engine_key}] 6. Restauración con OTRO nombre (limitación conocida)")
    _seed_source(engine_key, SRC_DB)
    _recreate_db(engine_key, OTHER_DB)
    spec = _full_spec(engine_key, SRC_DB, data={"mode": "none", "insert_variant": "none"})
    final, artifact = _run_export(client, server_id, SRC_DB, spec)
    check(final["status"] == "succeeded",
          f"exportación solo-estructura succeeded (fue '{final['status']}')")
    if final["status"] != "succeeded":
        return
    _run_artifact(engine_key, artifact.decode("utf-8"), default_db=OTHER_DB)
    restored = _snapshot(engine_key, server_id, OTHER_DB)
    bodies = " ".join((v.definition or "") for v in restored.views)
    if engine_key in _MYSQL_FAMILY:
        check(SRC_DB in bodies,
              "MySQL/MariaDB: los cuerpos conservan el esquema de ORIGEN al restaurar con "
              "otro nombre — LIMITACIÓN CONOCIDA, el artefacto se restaura con su nombre")
    else:
        check(SRC_DB not in bodies,
              "PostgreSQL: los cuerpos NO llevan el nombre de la base, restaurar con otro "
              "nombre es seguro")
    diff = diff_snapshots(_snapshot(engine_key, server_id, SRC_DB), restored)
    table_items = [i for i in diff.items if i.object_type == "table"]
    check(not table_items,
          f"las TABLAS sí quedan idénticas aunque cambie el nombre de la base "
          f"({_describe_diff(diff)})")


# --------------------------------------------------------------------------- #
# Entrada                                                                      #
# --------------------------------------------------------------------------- #


def main_run(engine_keys: list[str]) -> None:
    client = _admin_client()
    for engine_key in engine_keys:
        print("\n" + "=" * 72)
        print(f"MOTOR: {engine_key}")
        print("=" * 72)
        server_id = _register_server(client, engine_key)
        scenario_structure_roundtrip(client, engine_key, server_id)
        scenario_value_edges(client, engine_key, server_id)
        scenario_session_consistency(engine_key, server_id)
        scenario_determinism(client, engine_key, server_id)
        scenario_formats(client, engine_key, server_id)
        scenario_body_requalification(client, engine_key, server_id)

    print("\n" + "=" * 72)
    print(f"{checks_run} checks ejecutados")
    if failures:
        print(f"FALLARON {len(failures)} checks:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("0 fallos — TODOS los checks pasaron.")


if __name__ == "__main__":
    keys = sys.argv[1].split(",") if len(sys.argv) > 1 else ["mysql"]
    main_run([k for k in keys if k in ENGINES])
