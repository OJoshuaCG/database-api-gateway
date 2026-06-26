"""
Tests de integración de grants contra motores REALES (Plan 07 Fase 1 — cierre).

Marcados ``@pytest.mark.integration``: requieren contenedores de BD alcanzables y se
SALTAN si no lo están. Ejecutar con::

    docker run -d --rm --name gw_it_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13399:3306 mysql:8.0
    docker run -d --rm --name gw_it_maria -e MARIADB_ROOT_PASSWORD=rootpw \\
        -e MARIADB_ROOT_HOST=% -p 13400:3306 mariadb:11
    docker run -d --rm --name gw_it_pg -e POSTGRES_PASSWORD=rootpw -p 15499:5432 postgres:16
    uv run pytest -m integration

Puertos/credenciales son overridables por entorno (GW_IT_<ENGINE>_PORT, _USER, _PW).
El gateway sigue usando SQLite como BD de metadatos (fixture ``admin_client``); solo los
servidores DESTINO son reales.
"""

import os
import socket

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


def _spec(engine_key: str) -> dict:
    defaults = {
        "mysql": {"port": 13399, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
        "mariadb": {"port": 13400, "driver": "mysql+pymysql", "user": "root", "pw": "rootpw"},
        "postgresql": {"port": 15499, "driver": "postgresql+psycopg", "user": "postgres", "pw": "rootpw"},
    }[engine_key]
    pre = f"GW_IT_{engine_key.upper()}"
    return {
        "port": int(os.environ.get(f"{pre}_PORT", defaults["port"])),
        "driver": defaults["driver"],
        "user": os.environ.get(f"{pre}_USER", defaults["user"]),
        "pw": os.environ.get(f"{pre}_PW", defaults["pw"]),
    }


def _reachable(spec: dict) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", spec["port"]), timeout=1.5):
            return True
    except OSError:
        return False


def _root_engine(engine_key: str, spec: dict):
    base = "/postgres" if engine_key == "postgresql" else ""
    url = f"{spec['driver']}://{spec['user']}:{spec['pw']}@127.0.0.1:{spec['port']}{base}"
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _db_engine(engine_key: str, spec: dict, dbname: str):
    url = f"{spec['driver']}://{spec['user']}:{spec['pw']}@127.0.0.1:{spec['port']}/{dbname}"
    return create_engine(url, isolation_level="AUTOCOMMIT")


@pytest.fixture(params=["mysql", "mariadb", "postgresql"])
def target(request, admin_client):
    """
    Prepara un motor real: BD + usuario/rol + una tabla, y registra el servidor + el
    server-user en el gateway. Devuelve el contexto para ejercer GRANT/REVOKE/LIST.
    """
    engine_key = request.param
    spec = _spec(engine_key)
    if not _reachable(spec):
        pytest.skip(f"Motor {engine_key} no alcanzable en 127.0.0.1:{spec['port']}")

    dbname = f"it_{engine_key[:2]}"
    user = f"gu_{engine_key[:2]}"
    user_pw = "userpw1"
    is_pg = engine_key == "postgresql"

    # --- Pre-clean + setup directo (como root) --- #
    root = _root_engine(engine_key, spec)
    with root.connect() as c:
        if is_pg:
            c.execute(text(f"DROP DATABASE IF EXISTS {dbname}"))
            c.execute(text(f"DROP ROLE IF EXISTS {user}"))
            c.execute(text(f"CREATE ROLE {user} LOGIN PASSWORD '{user_pw}'"))
            c.execute(text(f"CREATE DATABASE {dbname}"))
        else:
            c.execute(text(f"DROP DATABASE IF EXISTS {dbname}"))
            c.execute(text(f"DROP USER IF EXISTS '{user}'@'%'"))
            c.execute(text(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{user_pw}'"))
            c.execute(text(f"CREATE DATABASE {dbname}"))
    with _db_engine(engine_key, spec, dbname).connect() as c:
        c.execute(text("CREATE TABLE t (id INT)"))

    # --- Registro en el gateway --- #
    sr = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"it-{engine_key}", "host": "127.0.0.1", "port": spec["port"],
            "engine": engine_key, "root_username": spec["user"], "root_password": spec["pw"],
        },
    )
    assert sr.status_code == 201, sr.text
    server_id = sr.json()["data"]["id"]
    ur = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": user}
    )
    assert ur.status_code == 201, ur.text
    user_id = ur.json()["data"]["id"]

    yield {
        "admin_client": admin_client, "engine": engine_key, "user_id": user_id,
        "dbname": dbname, "username": user, "is_pg": is_pg,
    }

    # --- Teardown best-effort --- #
    try:
        with _root_engine(engine_key, spec).connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {dbname}"))
            if is_pg:
                c.execute(text(f"DROP ROLE IF EXISTS {user}"))
            else:
                c.execute(text(f"DROP USER IF EXISTS '{user}'@'%'"))
    except Exception:  # noqa: BLE001 — limpieza best-effort
        pass


def _list(ctx) -> list[dict]:
    params = {"database": ctx["dbname"]} if ctx["is_pg"] else {}
    r = ctx["admin_client"].get(
        f"/api/v1/server-users/{ctx['user_id']}/grants", params=params
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _has_select_on_t(grants: list[dict]) -> bool:
    return any(
        g["level"] == "table" and "SELECT" in g["privileges"] and g["object"].endswith("t")
        for g in grants
    )


def _revoke(ctx, body, **params):
    return ctx["admin_client"].request(
        "DELETE", f"/api/v1/server-users/{ctx['user_id']}/grants",
        json=body, params=params or None,
    )


def test_grant_list_revoke_roundtrip(target):
    """GRANT SELECT en tabla → aparece en list_grants → REVOKE → desaparece."""
    ctx = target
    obj = {"database": ctx["dbname"], "table": "t"}
    if ctx["is_pg"]:
        obj["schema"] = "public"

    gr = ctx["admin_client"].post(
        f"/api/v1/server-users/{ctx['user_id']}/grants",
        json={"level": "table", "object_ref": obj, "privileges": ["SELECT"]},
    )
    assert gr.status_code == 200, gr.text
    assert _has_select_on_t(_list(ctx)), "el GRANT no se reflejó en list_grants"

    rv = _revoke(ctx, {"level": "table", "object_ref": obj, "privileges": ["SELECT"]})
    assert rv.status_code == 200, rv.text
    assert not _has_select_on_t(_list(ctx)), "el REVOKE no eliminó el privilegio"


def test_pg_revoke_cascade_with_confirmation(target):
    """PostgreSQL: REVOKE ... CASCADE con confirmación correcta tiene éxito."""
    ctx = target
    if not ctx["is_pg"]:
        pytest.skip("CASCADE solo aplica a PostgreSQL")
    obj = {"database": ctx["dbname"], "schema": "public", "table": "t"}

    gr = ctx["admin_client"].post(
        f"/api/v1/server-users/{ctx['user_id']}/grants",
        json={"level": "table", "object_ref": obj, "privileges": ["SELECT"], "with_grant_option": True},
    )
    assert gr.status_code == 200, gr.text

    # Sin confirmación → 422; con confirmación → 200.
    body = {"level": "table", "object_ref": obj, "privileges": ["SELECT"], "cascade": True}
    assert _revoke(ctx, body).status_code == 422
    ok = _revoke(ctx, body, confirm_grantee=ctx["username"])
    assert ok.status_code == 200, ok.text
    assert not _has_select_on_t(_list(ctx))
