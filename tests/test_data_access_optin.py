"""
Opt-in de DATOS por base (segundo aprobador) y el gate ``resolve_agent_data_database``.

LO QUE SE FIJA
--------------
- ``request_data_access``: en ``production`` (y sin entorno) el pedido queda PENDIENTE; en los demás
  entornos abre en el acto. Abrir se audita fail-closed (si el rastro cae, la fila no cambia).
- ``approve_data_access``: lo aprueba OTRO owner; el solicitante recibe 403
  ``data_access.self_approval_forbidden``; sin pedido pendiente, 409 ``data_access.not_pending``.
- ``revoke_data_access``: inmediato, idempotente, cancela pedidos pendientes.
- Un viewer no pide ni aprueba (capa 1: necesita ``data.read``, que es solo de owner).
- El gate (S13, S29): kill switch por capacidad, credencial presente, sonda verde reciente, opt-in
  abierto Y con aprobador registrado. Cada falla es su código interno ``mcp.data_*``, que
  ``public_reason`` traduce a ``DATA_DISABLED`` / ``PROBE_NOT_GREEN``.
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.controllers import target_resolution as tr
from app.core import environments
from app.core.actor import admin_actor, token_actor
from app.core.crypto import encrypt
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.models.managed_database_data_credential import ManagedDatabaseDataCredential
from app.services import audit as audit_mod
from app.services import mcp_catalog as codes
from app.services.capability_catalog import Capability, GatewayRole
from tests.step_up_helpers import OPEN_WINDOW
from tests.test_data_credential_provisioning import (  # noqa: F401 — arnés de la credencial de datos
    PROVISION,
    _database,
    _row,
    motor,
)
from tests.test_mcp_catalog_tools import _credencial_ro, _server_de, mcp_on  # noqa: F401
from tests.test_mcp_server import _bd_alcanzable, _crear_token, _proyecto

REQUEST = "/api/v1/managed-databases/{db}/data-access/request"
APPROVE = "/api/v1/managed-databases/{db}/data-access/approve"
REVOKE = "/api/v1/managed-databases/{db}/data-access"
STATUS = "/api/v1/managed-databases/{db}/data-credential"


def _codigo(r) -> str:
    return r.json()["detail"]["public_context"]["code"]


def _en_entorno(db_id: int, slug: str) -> None:
    with Database().engine.begin() as conn:
        env_id = conn.execute(
            text("SELECT id FROM environments WHERE slug = :s"), {"s": slug}
        ).scalar()
        conn.execute(
            text("UPDATE managed_databases SET environment_id = :e WHERE id = :i"),
            {"e": env_id, "i": db_id},
        )


def _con_credencial(admin_client, motor, *, slug="production") -> int:
    db_id = _database(admin_client)
    _en_entorno(db_id, slug)
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    return db_id


def _filas(prefijo="managed_database.data_access_"):
    s = Database().get_declarative_base_session()
    try:
        return [(a.action, a.status) for a in s.query(AuditLog).all() if a.action.startswith(prefijo)]
    finally:
        s.close()


def _viewer_client(admin_client):
    from tests.access_request_helpers import client_as, create_user

    return client_as(create_user(admin_client, "viewer-datos"), "viewer-datos")


# --------------------------------------------------------------------------- #
# request                                                                       #
# --------------------------------------------------------------------------- #


def test_in_production_the_request_stays_pending_until_a_second_owner_approves(
    admin_client, motor
):
    db_id = _con_credencial(admin_client, motor, slug="production")

    r = admin_client.post(REQUEST.format(db=db_id))

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["data_access_state"] == "pending"
    assert data["data_access_allowed"] is False
    assert data["data_access_second_approver_required"] is True
    row = _row(db_id)
    assert row.data_access_requested_by_id is not None
    assert row.data_access_approved_by_id is None and row.data_access_allowed is False
    assert ("managed_database.data_access_request", "attempt") in _filas()


def test_outside_production_the_request_opens_immediately(admin_client, motor):
    db_id = _con_credencial(admin_client, motor, slug="development")

    data = admin_client.post(REQUEST.format(db=db_id)).json()["data"]

    assert data["data_access_state"] == "open" and data["data_access_allowed"] is True
    assert data["data_access_second_approver_required"] is False
    row = _row(db_id)
    assert row.data_access_approved_by_id == row.data_access_requested_by_id
    assert row.data_access_approved_at is not None
    assert ("managed_database.data_access_open", "attempt") in _filas()


def test_a_database_without_environment_requires_a_second_approver(admin_client, motor):
    db_id = _database(admin_client)  # sin entorno
    assert admin_client.post(PROVISION.format(db=db_id)).status_code == 200
    data = admin_client.post(REQUEST.format(db=db_id)).json()["data"]
    assert data["data_access_state"] == "pending"
    assert data["data_access_second_approver_required"] is True


def test_the_request_without_a_credential_is_a_409(admin_client, motor):
    db_id = _database(admin_client)
    r = admin_client.post(REQUEST.format(db=db_id))
    assert r.status_code == 409, r.text
    assert _codigo(r) == "data_credential.missing"


def test_requesting_an_already_open_access_is_a_409(admin_client, motor):
    db_id = _con_credencial(admin_client, motor, slug="development")
    assert admin_client.post(REQUEST.format(db=db_id)).status_code == 200
    r = admin_client.post(REQUEST.format(db=db_id))
    assert r.status_code == 409 and _codigo(r) == "data_access.already_open"


def test_an_unknown_database_is_a_404(admin_client, motor):
    assert admin_client.post(REQUEST.format(db=9999)).status_code == 404


def test_the_audit_intent_is_fail_closed(admin_client, motor, monkeypatch):
    db_id = _con_credencial(admin_client, motor, slug="development")

    def _cae(action, **kw):
        raise AppHttpException(message="auditoría caída", status_code=500)

    monkeypatch.setattr(audit_mod, "record_intent", _cae)
    r = admin_client.post(REQUEST.format(db=db_id))

    assert r.status_code == 500, r.text
    row = _row(db_id)
    assert row.data_access_allowed is False and row.data_access_requested_by_id is None


def test_a_viewer_can_neither_request_nor_approve_nor_revoke(admin_client, motor):
    db_id = _con_credencial(admin_client, motor)
    viewer = _viewer_client(admin_client)
    for metodo, url in (
        (viewer.post, REQUEST),
        (viewer.post, APPROVE),
        (viewer.delete, REVOKE),
    ):
        r = metodo(url.format(db=db_id))
        assert r.status_code == 403, (url, r.text)


def test_unauthenticated_calls_are_401(client):
    assert client.post(REQUEST.format(db=1)).status_code == 401
    assert client.post(APPROVE.format(db=1)).status_code == 401
    assert client.delete(REVOKE.format(db=1)).status_code == 401


# --------------------------------------------------------------------------- #
# approve                                                                       #
# --------------------------------------------------------------------------- #


def test_the_requester_cannot_approve_their_own_request(admin_client, motor):
    db_id = _con_credencial(admin_client, motor)
    assert admin_client.post(REQUEST.format(db=db_id)).status_code == 200

    r = admin_client.post(APPROVE.format(db=db_id))

    assert r.status_code == 403, r.text
    assert _codigo(r) == "data_access.self_approval_forbidden"
    assert _row(db_id).data_access_allowed is False


def test_a_different_owner_approves_and_the_access_opens(admin_client, owner_client, motor):
    db_id = _con_credencial(admin_client, motor)
    assert admin_client.post(REQUEST.format(db=db_id)).status_code == 200

    r = owner_client.post(APPROVE.format(db=db_id))

    assert r.status_code == 200, r.text
    assert r.json()["data"]["data_access_state"] == "open"
    row = _row(db_id)
    assert row.data_access_allowed is True
    assert row.data_access_approved_by_id not in (None, row.data_access_requested_by_id)
    assert row.data_access_approved_at is not None
    assert ("managed_database.data_access_open", "attempt") in _filas()


def test_approving_without_a_pending_request_is_a_409(admin_client, owner_client, motor):
    db_id = _con_credencial(admin_client, motor)
    r = owner_client.post(APPROVE.format(db=db_id))
    assert r.status_code == 409 and _codigo(r) == "data_access.not_pending"


def test_approving_an_already_open_access_is_a_409(admin_client, owner_client, motor):
    db_id = _con_credencial(admin_client, motor, slug="development")
    assert admin_client.post(REQUEST.format(db=db_id)).status_code == 200
    r = owner_client.post(APPROVE.format(db=db_id))
    assert r.status_code == 409 and _codigo(r) == "data_access.not_pending"


def test_the_approval_audit_is_fail_closed(admin_client, owner_client, motor, monkeypatch):
    db_id = _con_credencial(admin_client, motor)
    assert admin_client.post(REQUEST.format(db=db_id)).status_code == 200

    def _cae(action, **kw):
        raise AppHttpException(message="auditoría caída", status_code=500)

    monkeypatch.setattr(audit_mod, "record_intent", _cae)
    assert owner_client.post(APPROVE.format(db=db_id)).status_code == 500
    assert _row(db_id).data_access_allowed is False


# --------------------------------------------------------------------------- #
# revoke                                                                        #
# --------------------------------------------------------------------------- #


def test_revoke_closes_immediately_and_is_idempotent(admin_client, owner_client, motor):
    db_id = _con_credencial(admin_client, motor)
    admin_client.post(REQUEST.format(db=db_id))
    owner_client.post(APPROVE.format(db=db_id))
    assert _row(db_id).data_access_allowed is True

    r = admin_client.delete(REVOKE.format(db=db_id))

    assert r.status_code == 200, r.text
    assert r.json()["data"]["data_access_state"] == "closed"
    row = _row(db_id)
    assert row.data_access_allowed is False
    assert row.data_access_requested_by_id is None and row.data_access_approved_by_id is None
    assert row.data_access_approved_at is None
    assert ("managed_database.data_access_close", "success") in _filas()
    assert admin_client.delete(REVOKE.format(db=db_id)).status_code == 200  # idempotente


def test_revoke_cancels_a_pending_request(admin_client, owner_client, motor):
    db_id = _con_credencial(admin_client, motor)
    admin_client.post(REQUEST.format(db=db_id))
    assert admin_client.delete(REVOKE.format(db=db_id)).status_code == 200
    r = owner_client.post(APPROVE.format(db=db_id))
    assert r.status_code == 409 and _codigo(r) == "data_access.not_pending"


def test_revoke_without_a_credential_is_a_no_op(admin_client, motor):
    db_id = _database(admin_client)
    r = admin_client.delete(REVOKE.format(db=db_id))
    assert r.status_code == 200 and r.json()["data"]["has_data_credential"] is False


def test_clearing_the_credential_also_closes_the_opt_in(admin_client, owner_client, motor):
    db_id = _con_credencial(admin_client, motor)
    admin_client.post(REQUEST.format(db=db_id))
    owner_client.post(APPROVE.format(db=db_id))
    assert admin_client.delete(STATUS.format(db=db_id)).status_code == 200
    assert _row(db_id) is None
    assert admin_client.get(STATUS.format(db=db_id)).json()["data"]["data_access_state"] == "closed"


def test_the_status_endpoint_exposes_no_secret(admin_client, motor):
    db_id = _con_credencial(admin_client, motor)
    data = admin_client.get(STATUS.format(db=db_id)).json()["data"]
    assert data["has_data_credential"] is True and data["data_access_state"] == "closed"
    assert not ({"username", "password", "password_encrypted"} & set(data))
    assert admin_client.get(STATUS.format(db=9999)).status_code == 404


# --------------------------------------------------------------------------- #
# El gate de datos (S13, S29)                                                   #
# --------------------------------------------------------------------------- #


def _escenario(admin_client, monkeypatch, *, leer=True, query=True):
    """Base alcanzable por un token del proyecto, con credencial de estructura y de DATOS."""
    # El actor se arma con los dos switches ENCENDIDOS (si no, el scope sería inerte y el gate
    # negaría por scope) y recién después se fija el estado que el test quiere medir: así se prueba
    # "se apagó con el token ya emitido".
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", True)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", True)
    pid = _proyecto(admin_client)
    _crear_token(admin_client, project_id=pid, scopes=["databases.read"])
    db_id = _bd_alcanzable(admin_client, project_id=pid)
    _credencial_ro(admin_client, _server_de(db_id))
    actor = token_actor(
        token_pk=1,
        token_id="t",
        name="agente",
        scopes="databases.read,data.read,data.query",
        project_id=pid,
        issuer=admin_actor(
            user_id=1, username="o", role=GatewayRole.OWNER, step_up_until=OPEN_WINDOW
        ),
    )
    monkeypatch.setattr(environments, "MCP_DATA_READ_ENABLED", leer)
    monkeypatch.setattr(environments, "MCP_DATA_QUERY_ENABLED", query)
    return actor, db_id


def _sembrar_credencial(db_id, *, verificada_hace_dias: float | None = 0, abierto=True,
                        aprobador: int | None = 2, usuario="mcp_d_1"):
    s = Database().get_declarative_base_session()
    try:
        s.add(
            ManagedDatabaseDataCredential(
                managed_database_id=db_id,
                username=usuario,
                account_host="%",
                password_encrypted=encrypt("pw-datos"),
                verified_at=(
                    None
                    if verificada_hace_dias is None
                    else datetime.now(UTC).replace(tzinfo=None) - timedelta(days=verificada_hace_dias)
                ),
                data_access_allowed=abierto,
                data_access_requested_by_id=1,
                data_access_approved_by_id=aprobador,
            )
        )
        s.commit()
    finally:
        s.close()


def _codigo_de(exc: pytest.ExceptionInfo) -> str:
    return exc.value.public_context["code"]


def test_the_gate_opens_when_every_axis_is_green(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch)
    _sembrar_credencial(db_id)
    resuelta = tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert resuelta.database.database_id == db_id
    assert tr.resolve_agent_data_database(actor, db_id, Capability.DATA_QUERY)


def test_the_gate_denies_when_the_kill_switch_is_off(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch, leer=False)
    _sembrar_credencial(db_id)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_DATA_DISABLED
    assert codes.public_reason(_codigo_de(exc)) == "DATA_DISABLED"


def test_each_capability_has_its_own_kill_switch(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch, leer=True, query=False)
    _sembrar_credencial(db_id)
    assert tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_QUERY)
    assert _codigo_de(exc) == codes.CODE_DATA_DISABLED


def test_the_gate_denies_without_a_data_credential(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_DATA_CREDENTIAL_MISSING
    assert codes.public_reason(_codigo_de(exc)) == "DATA_DISABLED"


@pytest.mark.parametrize("hace_dias", [None, 8])
def test_the_gate_denies_an_unverified_or_stale_probe(admin_client, mcp_on, monkeypatch, hace_dias):
    actor, db_id = _escenario(admin_client, monkeypatch)
    _sembrar_credencial(db_id, verificada_hace_dias=hace_dias)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_DATA_PROBE_STALE
    assert codes.public_reason(_codigo_de(exc)) == "PROBE_NOT_GREEN"


def test_the_gate_denies_without_the_opt_in(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch)
    _sembrar_credencial(db_id, abierto=False, aprobador=None)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_DATA_NOT_OPTED_IN


def test_an_opt_in_flag_without_a_recorded_approver_opens_nothing(admin_client, mcp_on, monkeypatch):
    """Un ``UPDATE`` a mano del flag no abre: tiene que haber aprobador registrado."""
    actor, db_id = _escenario(admin_client, monkeypatch)
    _sembrar_credencial(db_id, abierto=True, aprobador=None)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_DATA_NOT_OPTED_IN


def test_revoking_cuts_the_gate_immediately(admin_client, mcp_on, monkeypatch, motor):
    """S29: la revocación del opt-in niega en la llamada siguiente."""
    actor, db_id = _escenario(admin_client, monkeypatch)
    _sembrar_credencial(db_id)
    assert tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)

    from app.controllers.managed_database_controller import ManagedDatabaseController

    ManagedDatabaseController().revoke_data_access(db_id, admin=None)
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(actor, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_DATA_NOT_OPTED_IN


def test_a_token_without_the_scope_is_denied_before_anything_else(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch)
    sin_scope = token_actor(
        token_pk=3, token_id="t3", name="sin-datos", scopes="databases.read",
        project_id=actor.project_id,
        issuer=admin_actor(user_id=1, username="o", role=GatewayRole.OWNER),
    )
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(sin_scope, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_SCOPE_DENIED


def test_a_database_of_another_project_is_not_found_not_denied(admin_client, mcp_on, monkeypatch):
    actor, db_id = _escenario(admin_client, monkeypatch)
    _sembrar_credencial(db_id)
    ajeno = token_actor(
        token_pk=2, token_id="t2", name="otro", scopes="databases.read,data.read",
        project_id=actor.project_id + 100,
        issuer=admin_actor(user_id=1, username="o", role=GatewayRole.OWNER),
    )
    with pytest.raises(AppHttpException) as exc:
        tr.resolve_agent_data_database(ajeno, db_id, Capability.DATA_READ)
    assert _codigo_de(exc) == codes.CODE_NOT_FOUND
