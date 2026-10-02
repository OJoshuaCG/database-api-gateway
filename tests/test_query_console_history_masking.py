"""
``GET /servers/{id}/query/history``: quién ve el ``sql_text`` completo y quién enmascarado.

Regla: el texto completo solo a quien tiene ``sql_console.execute`` EN el destino de la fila
(podría ejecutar la consulta igual); el resto —``viewer``, ``operator``, o un ``owner``
restringido en ese entorno— recibe los literales como ``?`` y ``sql_masked=true``.
``error_message`` se sanea para todos.

Las filas se siembran por ORM: lo que se mide es la lectura, no la ejecución.
"""

import pytest
from sqlalchemy import text

from app.core.database import Database
from app.models.query_execution import QueryExecution
from tests.scope_helpers import env_id, otorgar, sembrar_bd
from tests.test_capability_grant_crud import _insert_cg

_SQL = "INSERT INTO clientes (email, saldo) VALUES ('alice@x.com', 1500)"
_RAW_ERROR = "(1062, \"Duplicate entry 'alice@x.com' for key 'clientes.email'\")"


def _rol(role: str) -> None:
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )


def _fila(server_id: int, database: str, *, error: str | None = None) -> None:
    s = Database().get_declarative_base_session()
    try:
        s.add(
            QueryExecution(
                server_id=server_id,
                database_name=database,
                engine="mysql",
                admin_id=1,
                admin_username="admin",
                connection_mode="admin",
                run_as_username="root",
                sql_text=_SQL,
                sql_hash="0" * 64,
                danger_level="write",
                statement_count=1,
                read_only=False,
                dry_run=False,
                committed=error is None,
                status="error" if error else "success",
                rows_returned=0,
                rows_affected=1,
                duration_ms=3,
                error_code="1062" if error else None,
                error_message=error,
            )
        )
        s.commit()
    finally:
        s.close()


@pytest.fixture()
def sid(admin_client, server_payload):
    r = admin_client.post("/api/v1/servers", json=server_payload(name="hist-mask", port=3555))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _historial(admin_client, sid) -> list[dict]:
    r = admin_client.get(f"/api/v1/servers/{sid}/query/history")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_owner_con_execute_recibe_el_texto_completo(admin_client, sid):
    _fila(sid, "tienda")
    h = _historial(admin_client, sid)[0]
    assert h["sql_text"] == _SQL
    assert h["sql_masked"] is False


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_sin_execute_recibe_literales_enmascarados(admin_client, sid, role):
    _fila(sid, "tienda")
    _rol(role)
    h = _historial(admin_client, sid)[0]
    assert h["sql_masked"] is True
    assert "alice" not in h["sql_text"] and "1500" not in h["sql_text"]
    assert h["sql_text"] == "INSERT INTO clientes (email, saldo) VALUES (?, ?)"


def test_viewer_con_execute_otorgado_en_el_servidor_ve_el_texto_completo(admin_client, sid):
    _fila(sid, "tienda")
    _rol("viewer")
    _insert_cg(1, "sql_console.execute", "server", sid)
    h = _historial(admin_client, sid)[0]
    assert h["sql_masked"] is False
    assert h["sql_text"] == _SQL


def test_viewer_con_execute_otorgado_en_otro_servidor_sigue_enmascarado(admin_client, sid):
    _fila(sid, "tienda")
    _rol("viewer")
    _insert_cg(1, "sql_console.execute", "server", sid + 999)
    h = _historial(admin_client, sid)[0]
    assert h["sql_masked"] is True


def test_owner_restringido_a_viewer_en_produccion_ve_enmascarado_solo_ahi(admin_client, sid):
    prod, dev = env_id("production"), env_id("development")
    sembrar_bd(server_id=sid, environment_id=prod, name="tienda_prod")
    sembrar_bd(server_id=sid, environment_id=dev, name="tienda_dev")
    _fila(sid, "tienda_prod")
    _fila(sid, "tienda_dev")
    otorgar("environment", prod, "viewer")

    por_bd = {h["database_name"]: h for h in _historial(admin_client, sid)}
    assert por_bd["tienda_prod"]["sql_masked"] is True
    assert "alice" not in por_bd["tienda_prod"]["sql_text"]
    assert por_bd["tienda_dev"]["sql_masked"] is False
    assert por_bd["tienda_dev"]["sql_text"] == _SQL


@pytest.mark.parametrize("role", ["owner", "viewer"])
def test_error_message_llega_saneado_para_todos(admin_client, sid, role):
    _fila(sid, "tienda", error=_RAW_ERROR)
    _rol(role)
    h = _historial(admin_client, sid)[0]
    assert "alice" not in h["error_message"]
    assert h["error_message"].startswith("(1062) Duplicate entry '?' for key")
    assert "clientes.email" in h["error_message"]


def test_la_lectura_no_reescribe_la_fila(admin_client, sid):
    _fila(sid, "tienda", error=_RAW_ERROR)
    _rol("viewer")
    _historial(admin_client, sid)
    with Database().engine.begin() as conn:
        sql_text, err = conn.execute(
            text("SELECT sql_text, error_message FROM query_executions WHERE server_id = :s"),
            {"s": sid},
        ).one()
    assert sql_text == _SQL
    assert err == _RAW_ERROR
