"""
Prefijo del bearer de agente: ``datum.`` al emitir, ``dbgw.`` (legado) aceptado al validar.

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
El cambio de nombre del producto cambió el prefijo de los tokens NUEVOS, pero los agentes que ya
tienen un ``dbgw.<id>.<secreto>`` configurado no pueden romperse. Lo que se fija acá: la emisión
usa el prefijo nuevo, el legado sigue autenticando con el MISMO token (el HMAC cubre solo el
secreto y el índice es el ``token_id``), un prefijo desconocido y un secreto equivocado reciben el
mismo 401 opaco, y el ``key_func`` del limitador reconoce los dos prefijos.
"""

from types import SimpleNamespace

import pytest

from app.core.limiter import agent_token_key
from app.core.mcp_token_format import (
    ACCEPTED_TOKEN_PREFIXES,
    LEGACY_TOKEN_PREFIX,
    TOKEN_PREFIX,
)

CODE_TOKEN_INVALID = "mcp.token_invalid"
UNKNOWN_PREFIX = "otro"


@pytest.fixture()
def mcp_on(monkeypatch):
    """El kill switch nace apagado y se lee por nombre en ``mcp_auth``."""
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", True)


def _rpc(client, bearer: str):
    return client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"Authorization": f"Bearer {bearer}"},
    )


def _issue_bearer(admin_client) -> str:
    project_id = admin_client.post("/api/v1/projects", json={"name": "Prefijo"}).json()["data"]["id"]
    response = admin_client.post(
        "/api/v1/api-tokens", json={"name": "agente", "project_id": project_id}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["token"]


def _split(bearer: str) -> tuple[str, str, str]:
    prefix, token_id, secret = bearer.split(".")
    return prefix, token_id, secret


def _fake_request(bearer: str):
    return SimpleNamespace(headers={"authorization": f"Bearer {bearer}"}, client=SimpleNamespace(host="203.0.113.7"))


def test_the_prefix_constants_are_the_documented_ones():
    assert TOKEN_PREFIX == "datum"
    assert LEGACY_TOKEN_PREFIX == "dbgw"
    assert ACCEPTED_TOKEN_PREFIXES == ("datum", "dbgw")


def test_a_generated_bearer_uses_the_new_prefix_and_authenticates(client, admin_client, mcp_on):
    bearer = _issue_bearer(admin_client)

    prefix, _, _ = _split(bearer)
    assert prefix == TOKEN_PREFIX
    assert _rpc(client, bearer).status_code == 200


def test_a_legacy_prefixed_bearer_of_the_same_token_still_authenticates(
    client, admin_client, mcp_on
):
    _, token_id, secret = _split(_issue_bearer(admin_client))
    legacy_bearer = f"{LEGACY_TOKEN_PREFIX}.{token_id}.{secret}"

    assert _rpc(client, legacy_bearer).status_code == 200


def test_an_unknown_prefix_gets_the_opaque_401(client, admin_client, mcp_on):
    _, token_id, secret = _split(_issue_bearer(admin_client))

    response = _rpc(client, f"{UNKNOWN_PREFIX}.{token_id}.{secret}")

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["public_context"]["code"] == CODE_TOKEN_INVALID


@pytest.mark.parametrize("prefix", ACCEPTED_TOKEN_PREFIXES)
def test_a_wrong_secret_is_rejected_with_the_same_opaque_401(client, admin_client, mcp_on, prefix):
    _, token_id, _ = _split(_issue_bearer(admin_client))

    response = _rpc(client, f"{prefix}.{token_id}.secreto-equivocado")

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["public_context"]["code"] == CODE_TOKEN_INVALID


@pytest.mark.parametrize("prefix", ACCEPTED_TOKEN_PREFIXES)
def test_the_limiter_key_accepts_both_prefixes(prefix):
    key = agent_token_key(_fake_request(f"{prefix}.abc123.secreto"))

    assert key == "agent:abc123"


def test_the_limiter_key_falls_back_to_the_ip_for_an_unknown_prefix():
    key = agent_token_key(_fake_request(f"{UNKNOWN_PREFIX}.abc123.secreto"))

    assert key.startswith("ip:")
