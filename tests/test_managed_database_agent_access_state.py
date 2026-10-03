"""
El estado de acceso de agentes de una base se LEE en el mismo envelope donde se escribe.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
``PUT /managed-databases/{id}/agent-access`` escribía ``agent_access_allowed`` y
``agent_access_blocked`` pero ninguna respuesta las devolvía: el opt-in se podía hacer, no
confirmar. Ahora ``ManagedDatabaseOut`` las lleva (default ``False``, retrocompatible). Acá se
fija que la base nace cerrada, que el ``PUT`` responde con el estado nuevo, que detalle y lista
coinciden con él y que el veto se refleja aunque el opt-in siga en true.
"""

from tests.scope_helpers import env_id, sembrar_bd
from tests.test_environments_write_policy import _set_actor

_BASE = "/api/v1/managed-databases"
_KEYS = ("agent_access_allowed", "agent_access_blocked")


def _estado(d: dict) -> tuple[bool, bool]:
    return tuple(d[k] for k in _KEYS)


def _en_lista(admin_client, db_id: int) -> dict:
    data = admin_client.get(_BASE).json()["data"]
    return next(d for d in data if d["id"] == db_id)


def test_a_new_database_is_closed_to_agents_in_post_detail_and_list(admin_client):
    sid = admin_client.post(
        "/api/v1/servers",
        json={
            "name": "srv_agent_state",
            "host": "10.0.0.9",
            "port": 5450,
            "engine": "postgresql",
            "root_username": "root",
            "root_password": "rootpw",
        },
    ).json()["data"]["id"]
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner_agent_state"}
    ).json()["data"]["id"]

    created = admin_client.post(
        _BASE, json={"server_id": sid, "owner_id": oid, "name": "agent_state_db"}
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert _estado(data) == (False, False)

    detail = admin_client.get(f"{_BASE}/{data['id']}").json()["data"]
    assert _estado(detail) == (False, False)
    assert _estado(_en_lista(admin_client, data["id"])) == (False, False)


def test_put_returns_the_new_state_and_it_matches_detail_and_list(admin_client):
    db_id = sembrar_bd(environment_id=env_id("development"))
    _set_actor("viewer", ("security_officer",))

    r = admin_client.put(f"{_BASE}/{db_id}/agent-access", json={"allowed": True, "blocked": False})
    assert r.status_code == 200, r.text
    assert _estado(r.json()["data"]) == (True, False)

    detail = admin_client.get(f"{_BASE}/{db_id}").json()["data"]
    assert _estado(detail) == (True, False)
    assert _estado(_en_lista(admin_client, db_id)) == (True, False)


def test_blocked_is_reflected_even_when_allowed_stays_true(admin_client):
    db_id = sembrar_bd(environment_id=env_id("development"))
    _set_actor("viewer", ("security_officer",))

    r = admin_client.put(f"{_BASE}/{db_id}/agent-access", json={"allowed": True, "blocked": True})
    assert r.status_code == 200, r.text
    # El veto gana en el gate del MCP, pero el estado crudo muestra los dos ejes tal cual.
    assert _estado(r.json()["data"]) == (True, True)
    assert _estado(admin_client.get(f"{_BASE}/{db_id}").json()["data"]) == (True, True)
    assert _estado(_en_lista(admin_client, db_id)) == (True, True)
