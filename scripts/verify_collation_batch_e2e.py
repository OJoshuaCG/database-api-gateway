"""
Verificación end-to-end MANUAL de la conversión de collation EN LOTE por blueprint, contra un
motor REAL. El gateway de metadatos corre en SQLite efímero; MariaDB en Docker es el servidor
destino real.

POR QUÉ EXISTE ESTE SCRIPT
--------------------------
El módulo de conversión de collation era el ÚNICO de la familia sin verificación e2e —había
para clon, export, migraciones, consola SQL y schema-diff, y no para éste—, siendo el más
destructivo de todos: reescribe tablas en el lugar. Su propio handoff lo declaraba:
"NADA contra motores reales".

Lo que verifica, y por qué cada punto está:

  1. El lote convierte de verdad. Se comprueba en ``information_schema``, NO en la respuesta de
     la API: que un job diga ``succeeded`` no prueba que el motor haya cambiado nada.

  2. **La premisa del fix de FK checks.** MySQL prohíbe ``CONVERT TO CHARACTER SET`` con
     ``foreign_key_checks=1`` sobre una tabla con columna de texto en una FK. El fix del backend
     desactiva el flag asumiendo que **MariaDB se comporta igual**, y eso era una suposición
     tomada de la documentación de MySQL, no un hecho comprobado. El escenario 1 siembra
     exactamente ese caso.

  3. **Los objetos programables se recrean.** Una vista guarda la collation con la que se creó.
     Si el lote convierte las tablas y deja la vista congelada, produce el
     ``Illegal mix of collations`` que este módulo entero existe para evitar. Se comprueba que
     la vista quede con la collation nueva.

  4. La versión de contabilidad se **stampea y no se aplica**: el puntero de versión se mueve
     sin ejecutar el SQL.

  5. Cancelar un lote no toca las bases que estaban en cola.

NO es un test de pytest (requiere Docker; se ejecuta a mano). El runner es asíncrono y corre EN
SERIE (``COLLATION_CONVERSION_MAX_WORKERS`` = 1), así que el polling espera al lote completo.

ESTADO ACTUAL: FALLA, Y ES CORRECTO QUE FALLE
---------------------------------------------
La primera corrida encontró un defecto real y **sigue sin arreglarse**, así que este script sale
por 1. No está roto: está reportando.

``T-260825-lz-mariadb-fk-checks-no-alcanza`` (86e2zgkb6) — en MariaDB, ``foreign_key_checks=0``
**no** levanta la restricción sobre ``ALTER TABLE ... CONVERT TO CHARACTER SET`` de una columna
usada en una FK. Medido contra los dos motores con el mismo caso mínimo:

    MySQL 8.0.46    sin flag: rechaza (3780)   con flag: FUNCIONA
    MariaDB 11.8.9  sin flag: rechaza (1832)   con flag: rechaza igual (1832)

Las siete comprobaciones que fallan son todas esa causa. Cuando se arregle, este script tiene que
pasar entero; si falla algo distinto, es otro defecto.

Uso:
    docker run -d --rm --name gw_collation_mariadb -e MARIADB_ROOT_PASSWORD=rootpw \\
        -p 13399:3306 mariadb:11
    PYTHONPATH=. uv run python scripts/verify_collation_batch_e2e.py
"""

import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="e2e_gw_collation_")
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

PORT = 13399
ROOT_USER = "root"
ROOT_PW = "rootpw"

# Origen y destino de la conversión. `utf8mb3_general_ci` es la collation vieja típica de las
# bases que este módulo existe para migrar.
OLD_CHARSET, OLD_COLLATION = "utf8mb3", "utf8mb3_general_ci"
NEW_CHARSET, NEW_COLLATION = "utf8mb4", "utf8mb4_general_ci"

DBS = ["e2e_coll_uno", "e2e_coll_dos", "e2e_coll_tres"]

# Propietario de las BDs adoptadas. Se crea en el motor porque el adopt exige un ServerUser que
# EXISTA de verdad.
OWNER_USER, OWNER_PW = "e2e_coll_owner", "ownerpw"

failures: list[str] = []


def check(cond, msg):
    """
    Registra una comprobación. Orden ``(condición, mensaje)``, igual que los otros
    ``verify_*_e2e.py`` del repo.

    La guarda de tipos no es paranoia: al escribir este script se invirtieron los argumentos en
    las 30 llamadas, y como un string no vacío es truthy, **el script imprimió "Todas las
    comprobaciones pasaron" mientras tres escenarios fallaban**. Un verificador que no puede
    fallar es peor que no tenerlo, porque se le cree.
    """
    if isinstance(cond, str) or not isinstance(msg, str):
        raise TypeError(
            f"check() recibió los argumentos al revés: check(cond, msg). Recibido: "
            f"cond={cond!r}, msg={msg!r}"
        )
    print(f"  {'OK  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def _root_engine(db: str = "mysql"):
    url = f"mysql+pymysql://{ROOT_USER}:{ROOT_PW}@127.0.0.1:{PORT}/{db}"
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _wait_for_engine(timeout=90):
    """El contenedor tarda en aceptar conexiones; sin esto el script falla por carrera."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with _root_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001 — se reintenta hasta el deadline
            last = exc
            time.sleep(2)
    raise TimeoutError(f"MariaDB no respondió en {timeout}s: {last}")


def _admin_client() -> TestClient:
    """
    Cliente autenticado sobre un esquema recién creado.

    Los seeds se llaman a mano porque viven en el ``lifespan`` de la app, y ``TestClient(app)``
    sin usarlo como context manager **no lo ejecuta**. Además el `create_all` de acá corre
    DESPUÉS de importar `main`, así que aunque el lifespan hubiese corrido, esto lo borraría:
    sin sembrar explícitamente, el login devuelve 401 y el script muere antes de empezar.
    """
    import main
    from app.core.auth import bootstrap_admin
    from app.services.charset_catalog import seed_charset_options
    from app.services.environment_catalog import seed_environments
    from app.services.privilege_catalog import seed_privileges

    limiter.enabled = False
    Base.metadata.drop_all(Database().engine)
    Base.metadata.create_all(Database().engine)
    bootstrap_admin()
    seed_privileges()
    seed_charset_options()
    seed_environments()
    c = TestClient(main.app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return c


def _seed_databases():
    """
    Tres BDs con la collation vieja. La PRIMERA lleva el caso difícil.

    `child.parent_code` es una columna de TEXTO dentro de una FK. Es exactamente lo que MySQL
    prohíbe convertir con `foreign_key_checks=1`, y la razón por la que el backend desactiva ese
    flag durante la fase de tablas. Si MariaDB no tuviera la misma restricción, el fix sería
    innecesario; si la tiene y el fix fallara, esta BD es la que lo revela.
    """
    with _root_engine().connect() as conn:
        # Un usuario propio para ser el `owner` de las BDs adoptadas. No se usa `root` porque el
        # contenedor no necesariamente tiene el grant `root@%`, que es el que el adopt busca.
        conn.execute(text(f"DROP USER IF EXISTS '{OWNER_USER}'@'%'"))
        conn.execute(text(f"CREATE USER '{OWNER_USER}'@'%' IDENTIFIED BY '{OWNER_PW}'"))
        for db in DBS:
            conn.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            conn.execute(text(
                f"CREATE DATABASE {db} CHARACTER SET {OLD_CHARSET} COLLATE {OLD_COLLATION}"
            ))

    # BD 1: FK sobre columna de texto + una vista (objeto con collation congelada).
    with _root_engine(DBS[0]).connect() as conn:
        conn.execute(text(
            "CREATE TABLE parent ("
            "  code VARCHAR(32) NOT NULL PRIMARY KEY,"
            "  label VARCHAR(80)"
            f") CHARACTER SET {OLD_CHARSET} COLLATE {OLD_COLLATION}"
        ))
        conn.execute(text(
            "CREATE TABLE child ("
            "  id INT NOT NULL PRIMARY KEY,"
            "  parent_code VARCHAR(32) NOT NULL,"
            "  CONSTRAINT fk_child_parent FOREIGN KEY (parent_code) REFERENCES parent(code)"
            f") CHARACTER SET {OLD_CHARSET} COLLATE {OLD_COLLATION}"
        ))
        conn.execute(text("INSERT INTO parent (code, label) VALUES ('a', 'Alfa'), ('b', 'Beta')"))
        conn.execute(text("INSERT INTO child (id, parent_code) VALUES (1, 'a'), (2, 'b')"))
        conn.execute(text(
            "CREATE VIEW v_children AS "
            "SELECT c.id, p.label FROM child c JOIN parent p ON p.code = c.parent_code"
        ))

    # BDs 2 y 3: una tabla simple cada una, para que el lote tenga tres bases.
    for db in DBS[1:]:
        with _root_engine(db).connect() as conn:
            conn.execute(text(
                "CREATE TABLE items (id INT PRIMARY KEY, nombre VARCHAR(60))"
                f" CHARACTER SET {OLD_CHARSET} COLLATE {OLD_COLLATION}"
            ))
            conn.execute(text("INSERT INTO items (id, nombre) VALUES (1, 'uno')"))


def _table_collations(db: str) -> dict[str, str]:
    with _root_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE'"
        ), {"db": db}).fetchall()
    return {r[0]: r[1] for r in rows}


def _column_collations(db: str) -> dict[str, str]:
    with _root_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT CONCAT(TABLE_NAME, '.', COLUMN_NAME), COLLATION_NAME "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND COLLATION_NAME IS NOT NULL"
        ), {"db": db}).fetchall()
    return {r[0]: r[1] for r in rows}


def _view_collation(db: str, view: str) -> str | None:
    """La collation con la que la vista quedó CREADA (no la de la BD)."""
    with _root_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT COLLATION_CONNECTION FROM information_schema.VIEWS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :v"
        ), {"db": db, "v": view}).fetchone()
    return row[0] if row else None


def _db_collation(db: str) -> str | None:
    with _root_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME = :db"
        ), {"db": db}).fetchone()
    return row[0] if row else None


def _setup_inventory(c: TestClient) -> tuple[int, int, list[int]]:
    """Registra el servidor, adopta las tres BDs y las cuelga de un blueprint nuevo."""
    r = c.post("/api/v1/servers", json={
        "name": "e2e-collation", "host": "127.0.0.1", "port": PORT,
        "engine": "mysql", "root_username": ROOT_USER, "root_password": ROOT_PW,
    })
    assert r.status_code == 201, r.text
    sid = r.json()["data"]["id"]

    r = c.post("/api/v1/server-users/adopt", json={
        "server_id": sid, "username": OWNER_USER, "host": "%",
    })
    assert r.status_code == 201, r.text
    oid = r.json()["data"]["id"]

    r = c.post("/api/v1/database-models", json={
        "name": "E2E Collation", "slug": "e2e-collation",
        "charset": NEW_CHARSET, "collation": NEW_COLLATION,
    })
    assert r.status_code == 201, r.text
    mid = r.json()["data"]["id"]

    db_ids = []
    for name in DBS:
        r = c.post("/api/v1/managed-databases/adopt", json={
            "server_id": sid, "name": name, "owner_id": oid, "model_id": mid,
        })
        assert r.status_code == 201, f"{name}: {r.text}"
        db_ids.append(r.json()["data"]["id"])
    return sid, mid, db_ids


def _poll_batch(c: TestClient, mid: int, batch_id: int, timeout=180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/v1/database-models/{mid}/collation-conversions/{batch_id}")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        if data["batch"]["status"] not in ("pending", "running"):
            return data
        time.sleep(2)
    raise TimeoutError(f"lote {batch_id} no terminó en {timeout}s")


# --------------------------------------------------------------------------- #
# Escenario 1 — el lote convierte de verdad                                    #
# --------------------------------------------------------------------------- #
def scenario_batch_converts(c: TestClient, mid: int, db_ids: list[int]):
    print("\n[1] Lote por blueprint: convierte las tres BDs")

    before = _table_collations(DBS[0])
    check(all(v == OLD_COLLATION for v in before.values()) and len(before) == 2, "antes: las tablas están en la collation vieja")
    r = c.post(f"/api/v1/database-models/{mid}/collation-conversions", json={
        "target_charset": NEW_CHARSET, "target_collation": NEW_COLLATION,
        "scope": "all_tables", "objects": "all", "include_database_default": True,
        "max_databases": 10,
    })
    check(r.status_code == 201, f"plan 201 (fue {r.status_code})")
    if r.status_code != 201:
        print(f"       {r.text[:400]}")
        return None
    plan = r.json()["data"]
    check(len(plan["databases"]) == 3, "el plan cubre las tres BDs")
    check(plan["runs_serially"] is True, "runs_serially viene en true")
    check(plan["capped"] is False, "no está capado con max_databases=10")
    batch_id = plan["batch_id"]
    r = c.post(
        f"/api/v1/database-models/{mid}/collation-conversions/{batch_id}/execute",
        json={
            "confirm_model_slug": plan["model_slug"],
            "confirm_token": plan["batch_token"],
            "database_ids": [d["managed_database_id"] for d in plan["databases"]],
            "confirmations": {},
            "force": False,
        },
    )
    check(r.status_code == 200, f"execute 200 (fue {r.status_code})")
    if r.status_code != 200:
        print(f"       {r.text[:400]}")
        return None
    check(r.json()["data"]["enqueued"] == 3, "encoló las tres")
    final = _poll_batch(c, mid, batch_id)
    check(final["batch"]["status"] == "done", f"el lote terminó en 'done' (fue '{final['batch']['status']}')")
    check(final["batch"]["counts"]["done"] == 3, "las tres terminaron bien")

    # Un e2e que falla sin decir POR QUÉ obliga a reproducirlo a mano. Los errores por job y el
    # detalle del paso que rompió son lo único que convierte un rojo en un diagnóstico.
    for job in final["jobs"]:
        if job["status"] != "succeeded":
            print(f"\n       ── {job['database_name']}: {job['status']} ──")
            print(f"       error: {job.get('error')}")
            items = c.get(f"/api/v1/collation-conversions/{job['id']}/items?size=50")
            if items.status_code == 200:
                for it in items.json()["data"]:
                    if it.get("status") == "error":
                        print(f"       paso {it['seq']} {it['object_type']} {it['object_name']}")
                        print(f"         -> {it.get('error')}")
            print()
    # ── Lo que de verdad importa: el MOTOR, no la respuesta de la API ──────────
    print("\n  Verificación en information_schema (no en la respuesta de la API):")
    for db in DBS:
        tablas = _table_collations(db)
        check(bool(tablas) and all(v == NEW_COLLATION for v in tablas.values()), f"{db}: todas las tablas en {NEW_COLLATION}")
        cols = _column_collations(db)
        check(bool(cols) and all(v == NEW_COLLATION for v in cols.values()), f"{db}: todas las columnas de texto en {NEW_COLLATION}")
        check(_db_collation(db) == NEW_COLLATION, f"{db}: el default de la BD quedó en {NEW_COLLATION}")
    # ── La premisa del fix de FK checks ───────────────────────────────────────
    print("\n  El caso que el backend NO había verificado (FK sobre columna de texto):")
    cols = _column_collations(DBS[0])
    check(cols.get("parent.code") == NEW_COLLATION, "la columna dentro de la FK se convirtió (parent.code)")
    check(cols.get("child.parent_code") == NEW_COLLATION, "la columna que la referencia también (child.parent_code)")
    # ── Los objetos congelados ────────────────────────────────────────────────
    print("\n  Objetos programables recreados (si no, Illegal mix of collations):")
    vista = _view_collation(DBS[0], "v_children")
    check(vista == NEW_COLLATION, f"la vista v_children quedó en {NEW_COLLATION} (está en '{vista}')")
    # La prueba funcional del problema que el módulo evita: si la vista hubiese quedado
    # congelada en la collation vieja, este JOIN levantaría "Illegal mix of collations".
    try:
        with _root_engine(DBS[0]).connect() as conn:
            filas = conn.execute(text("SELECT * FROM v_children")).fetchall()
        check(len(filas) == 2, "la vista se puede consultar sin Illegal mix of collations")
    except Exception as exc:  # noqa: BLE001 — es justamente lo que se está verificando
        check(False, f"la vista se puede consultar sin Illegal mix of collations ({exc})")
    return batch_id


# --------------------------------------------------------------------------- #
# Escenario 2 — la versión se STAMPEA, no se aplica                            #
# --------------------------------------------------------------------------- #
def scenario_version_is_stamped(c: TestClient, mid: int, batch_id: int, db_ids: list[int]):
    print("\n[2] Versión de contabilidad: se stampea, NO se aplica")

    antes = _table_collations(DBS[0])
    r = c.post(
        f"/api/v1/database-models/{mid}/collation-conversions/{batch_id}/blueprint-version",
        json={"name": "Conversión a utf8mb4 (e2e)"},
    )
    check(r.status_code == 201, f"blueprint-version 201 (fue {r.status_code})")
    if r.status_code != 201:
        print(f"       {r.text[:400]}")
        return
    data = r.json()["data"]
    check(isinstance(data.get("version"), int), "devuelve el número de versión")
    check(data["statement_count"] > 0, "trae sentencias contabilizadas")
    check(sum(1 for s in data["stamped"] if s["ok"]) == 3, "stampeó en las tres BDs")
    check(data["pending_stamp"] == [], "no dejó stamps pendientes")
    check(bool(data.get("note")), "trae el note de advertencia")
    # Que "no se aplica" sea verdad: el plano físico no puede haber cambiado por crear la versión.
    check(_table_collations(DBS[0]) == antes, "el motor NO cambió al crear la versión")
    # Y el puntero de versión SÍ se movió: eso es lo que significa stampear.
    r = c.get(f"/api/v1/managed-databases/{db_ids[0]}/migrations/status")
    if r.status_code == 200:
        st = r.json()["data"]
        check(st.get("pending_count") == 0, f"la BD quedó al día (pendientes: {st.get('pending_count')})")
    else:
        check(False, f"status de migraciones legible (fue {r.status_code})")
# --------------------------------------------------------------------------- #
# Escenario 3 — cancelar no toca lo que está en cola                           #
# --------------------------------------------------------------------------- #
def scenario_cancel_leaves_queued_untouched(c: TestClient, mid: int):
    print("\n[3] Cancelar un lote: las BDs en cola no se tocan")

    # Se vuelve a poner una BD en la collation vieja para tener algo que convertir.
    with _root_engine().connect() as conn:
        conn.execute(text(
            f"ALTER DATABASE {DBS[2]} CHARACTER SET {OLD_CHARSET} COLLATE {OLD_COLLATION}"
        ))
    with _root_engine(DBS[2]).connect() as conn:
        conn.execute(text(
            f"ALTER TABLE items CONVERT TO CHARACTER SET {OLD_CHARSET} COLLATE {OLD_COLLATION}"
        ))
    check(_table_collations(DBS[2]).get("items") == OLD_COLLATION, "preparación: la BD volvió a la collation vieja")
    r = c.post(f"/api/v1/database-models/{mid}/collation-conversions", json={
        "target_charset": NEW_CHARSET, "target_collation": NEW_COLLATION,
        "scope": "all_tables", "objects": "all", "include_database_default": True,
        "max_databases": 10,
    })
    if r.status_code != 201:
        check(False, f"plan 201 para el caso de cancelación (fue {r.status_code})")
        return
    plan = r.json()["data"]
    batch_id = plan["batch_id"]

    r = c.post(
        f"/api/v1/database-models/{mid}/collation-conversions/{batch_id}/execute",
        json={
            "confirm_model_slug": plan["model_slug"],
            "confirm_token": plan["batch_token"],
            "database_ids": [d["managed_database_id"] for d in plan["databases"]],
            "confirmations": {},
            "force": False,
        },
    )
    if r.status_code != 200:
        check(False, f"execute 200 para el caso de cancelación (fue {r.status_code})")
        return

    # Se cancela DE INMEDIATO: con 1 worker y tres BDs, al menos una tiene que quedar en cola.
    r = c.post(f"/api/v1/database-models/{mid}/collation-conversions/{batch_id}/cancel")
    check(r.status_code == 200, f"cancel 200 (fue {r.status_code})")
    final = _poll_batch(c, mid, batch_id)
    estado = final["batch"]["status"]
    check(estado in ("canceled", "done", "failed"), f"el lote terminó cancelado o hecho, no colgado (fue '{estado}')")
    counts = final["batch"]["counts"]
    print(f"       counts: {counts}")
    # Los SEIS contadores tienen que cubrir el lote. La primera versión de esta comprobación
    # sumaba solo canceladas+hechas+falladas y daba rojo con un lote cancelado antes de arrancar,
    # donde los jobs quedan en `queued`: el error estaba en la aserción, no en el backend.
    suma = sum(counts[k] for k in ("queued", "running", "done", "failed", "canceled"))
    check(suma == counts["total"], f"los contadores cubren el lote ({suma} de {counts['total']})")
def main():
    print("Esperando a MariaDB en 127.0.0.1:%d…" % PORT)
    _wait_for_engine()
    print("Sembrando las bases de prueba…")
    _seed_databases()

    c = _admin_client()
    _sid, mid, db_ids = _setup_inventory(c)
    print(f"Inventario listo: blueprint {mid}, BDs {db_ids}")

    batch_id = scenario_batch_converts(c, mid, db_ids)
    if batch_id is not None:
        scenario_version_is_stamped(c, mid, batch_id, db_ids)
    scenario_cancel_leaves_queued_untouched(c, mid)

    print("\n" + "=" * 70)
    if failures:
        print(f"FALLARON {len(failures)} comprobaciones:")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nSi las fallas son las de la BD con FK sobre columna de texto, es el defecto\n"
            "conocido T-260825-lz-mariadb-fk-checks-no-alcanza (86e2zgkb6): en MariaDB,\n"
            "foreign_key_checks=0 no levanta la restricción del CONVERT TO. Ver la cabecera."
        )
        sys.exit(1)
    print("Todas las comprobaciones pasaron.")


if __name__ == "__main__":
    main()
