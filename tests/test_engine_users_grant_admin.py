"""
``engine_users.grant_admin``: delegar privilegios del motor es solo de ``owner``.

Se exige ADEMÁS de ``engine_users.write`` cuando el payload delega: ``with_grant_option`` o un
privilegio sensible (set GATE) en ``POST /server-users/{id}/grants``, y ``provision=true`` en
``POST /managed-databases/{id}/reassign-owner``. Restricción INTENCIONAL: ``operator`` pierde
esos grants (``owner`` los conserva); los grants simples (``SELECT``, ``INSERT``…) no cambian.

Cubre el catálogo (pertenencia por rol, invariantes), el 403 con código cerrado y el escalamiento
por payload en las dos rutas, el step-up y el camino de los grants iniciales de ``/provision``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.controllers.grant_controller import GrantController
from app.core import remote_engine
from app.core.database import Database
from app.exceptions import AppHttpException
from app.services import capability_catalog as cc
from app.services import engine_user_catalog as euc
from app.services.capability_catalog import Capability, GatewayRole
from app.services.db_admin.dtos import GrantLevel
from app.services.db_admin.mysql_adapter import MySQLAdapter
from tests.scope_helpers import env_id, sembrar_bd
from tests.test_capability_grant_crud import _insert_cg

CAP = Capability.ENGINE_USERS_GRANT_ADMIN
CODE = "engine_user.grant_admin_required"

_PLAIN_BODY = {
    "level": "table",
    "object_ref": {"database": "shop", "table": "orders"},
    "privileges": ["SELECT"],
}
_WITH_GRANT_OPTION_BODY = {**_PLAIN_BODY, "with_grant_option": True}
_SENSITIVE_BODY = {**_PLAIN_BODY, "privileges": ["ALL PRIVILEGES"]}


# --------------------------------------------------------------------------- #
# Catálogo: forma, pertenencia por rol, invariantes                            #
# --------------------------------------------------------------------------- #


def test_spec_is_a_server_axis_mutating_step_up_capability():
    s = cc.spec(CAP)
    assert (s.module, s.level) == ("engine_users", "grant_admin")
    assert (s.mutates, s.discloses, s.requires_step_up, s.scope_axis) == (
        True,
        False,
        True,
        "server",
    )
    assert not s.destructive and not s.agent_allowed
    assert cc.is_grantable(CAP)


def test_it_is_sensitive_and_needs_a_second_approver():
    assert cc.is_sensitive(CAP)
    assert cc.needs_second_approver(capability=CAP)
    assert "engine_users.grant_admin" in cc._SENSITIVE_POLICY
    assert len(cc._SENSITIVE_POLICY) == 15
    assert CAP in cc.OWNER_ONLY_CAPABILITIES


def test_granting_it_loose_implies_only_the_engine_users_read():
    assert cc.IMPLIED_READ[CAP] == frozenset({Capability.ENGINE_USERS_READ})


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (GatewayRole.VIEWER, False),
        (GatewayRole.OPERATOR, False),
        (GatewayRole.OWNER, True),
    ],
    ids=lambda v: getattr(v, "value", str(v)),
)
def test_inheritance_matrix_per_role(role, expected):
    """Operator PIERDE el grant de delegación (restricción intencional); owner lo conserva."""
    assert (CAP in cc.ROLE_CAPABILITIES[role]) is expected
    assert (CAP in cc.role_capabilities(role)) is expected


def test_no_global_holds_it():
    for caps in cc.GLOBAL_CAPABILITIES.values():
        assert CAP not in caps


def test_operator_keeps_the_ordinary_engine_user_write():
    """La partición no toca lo que operator hacía: ``write`` sigue en operator."""
    assert Capability.ENGINE_USERS_WRITE in cc.ROLE_CAPABILITIES[GatewayRole.OPERATOR]


def test_import_invariant_rejects_it_losing_its_owner_only_status(monkeypatch):
    """
    Si dejara de ser ``owner − operator`` (p. ej. llegara a operator), ya no sería sensible y el
    invariante 8 lo detecta al importar: el conjunto sensible está fijado a mano.
    """
    monkeypatch.setattr(cc, "OWNER_ONLY_CAPABILITIES", cc.OWNER_ONLY_CAPABILITIES - {CAP})
    with pytest.raises(AssertionError, match="sensible"):
        cc._assert_invariants()


def test_import_invariant_rejects_dropping_it_from_the_sensitive_policy(monkeypatch):
    monkeypatch.setattr(cc, "_SENSITIVE_POLICY", cc._SENSITIVE_POLICY - {"engine_users.grant_admin"})
    with pytest.raises(AssertionError, match="sensible"):
        cc._assert_invariants()


def test_error_code_vocabulary_and_messages_name_the_capability():
    assert euc.CODE_GRANT_ADMIN_REQUIRED == CODE
    assert CODE in euc.ERROR_CODES
    assert euc.GRANT_ADMIN_CAPABILITY_ID == CAP.value
    for reason in euc.GRANT_ADMIN_REASONS:
        assert "engine_users.grant_admin" in euc.grant_admin_required_message(reason)


# --------------------------------------------------------------------------- #
# Qué payload delega                                                           #
# --------------------------------------------------------------------------- #


def test_grant_admin_reason_by_payload():
    reason = GrantController.grant_admin_reason

    def reason_for(privileges, *, with_grant_option=False, dialect="mysql", level=GrantLevel.TABLE):
        return reason(
            dialect=dialect,
            level=level,
            privileges=privileges,
            with_grant_option=with_grant_option,
        )

    assert reason_for(["SELECT"]) is None
    assert reason_for(["SELECT", "INSERT"]) is None
    assert reason_for(["SELECT"], with_grant_option=True) == "with_grant_option"
    assert reason_for(["ALL PRIVILEGES"]) == "sensitive_privilege"
    assert reason_for(["ALL PRIVILEGES"], dialect="postgresql") == "sensitive_privilege"


def test_grant_admin_reason_still_raises_the_422_for_an_invalid_privilege():
    with pytest.raises(AppHttpException) as exc:
        GrantController.grant_admin_reason(
            dialect="mysql",
            level=GrantLevel.TABLE,
            privileges=["NO_EXISTE"],
            with_grant_option=False,
        )
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# Ruta: POST /server-users/{id}/grants                                         #
# --------------------------------------------------------------------------- #


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


def _public_context(r) -> dict:
    return (r.json().get("detail") or {}).get("public_context") or {}


def _set_role(role: str) -> None:
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )


def _make_server(admin_client) -> int:
    payload = {
        "name": "srv-grant-admin",
        "host": "127.0.0.1",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    r = admin_client.post("/api/v1/servers", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _make_user(admin_client, server_id: int, username: str) -> int:
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


@pytest.fixture()
def grantee(admin_client, monkeypatch):
    """Un usuario del motor ya creado (como owner) y un motor que explota si se lo toca."""
    server_id = _make_server(admin_client)
    user_id = _make_user(admin_client, server_id, "beneficiario")
    engine_calls: list[str] = []
    monkeypatch.setattr(
        MySQLAdapter, "can_grant", lambda self, *a, **k: engine_calls.append("can_grant") or True
    )
    monkeypatch.setattr(
        MySQLAdapter, "grant_object", lambda self, *a, **k: engine_calls.append("grant_object")
    )
    return {"server_id": server_id, "user_id": user_id, "engine_calls": engine_calls}


def _grant(admin_client, user_id: int, body: dict):
    return admin_client.post(f"/api/v1/server-users/{user_id}/grants", json=body)


def test_owner_can_grant_with_grant_option(admin_client, grantee):
    r = _grant(admin_client, grantee["user_id"], _WITH_GRANT_OPTION_BODY)
    assert r.status_code == 200, r.text
    assert grantee["engine_calls"] == ["can_grant", "grant_object"]


def test_owner_can_grant_a_sensitive_privilege(admin_client, grantee):
    r = _grant(admin_client, grantee["user_id"], _SENSITIVE_BODY)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (_WITH_GRANT_OPTION_BODY, "with_grant_option"),
        (_SENSITIVE_BODY, "sensitive_privilege"),
    ],
    ids=["with_grant_option", "sensitive_privilege"],
)
def test_operator_is_denied_with_the_named_code_before_touching_the_engine(
    admin_client, grantee, body, reason
):
    _set_role("operator")
    r = _grant(admin_client, grantee["user_id"], body)

    assert r.status_code == 403, r.text
    assert _code(r) == CODE
    context = _public_context(r)
    assert context["required_capability"] == "engine_users.grant_admin"
    assert context["reason"] == reason
    assert "engine_users.grant_admin" in r.json()["detail"]["msg"]
    assert grantee["engine_calls"] == [], "no debe abrir conexión al motor"


def test_operator_keeps_the_ordinary_grant(admin_client, grantee):
    _set_role("operator")
    r = _grant(admin_client, grantee["user_id"], _PLAIN_BODY)
    assert r.status_code == 200, r.text


def test_viewer_is_denied_by_the_route_guard_not_the_named_code(admin_client, grantee):
    """Sin ``engine_users.write`` el guard de la ruta corta antes: 403 opaco."""
    _set_role("viewer")
    r = _grant(admin_client, grantee["user_id"], _WITH_GRANT_OPTION_BODY)
    assert r.status_code == 403
    assert _code(r) == "access.forbidden"


def test_operator_with_a_loose_grant_admin_grant_passes(admin_client, grantee):
    _set_role("operator")
    _insert_cg(1, "engine_users.grant_admin", "server", grantee["server_id"])
    r = _grant(admin_client, grantee["user_id"], _WITH_GRANT_OPTION_BODY)
    assert r.status_code == 200, r.text


def test_a_loose_grant_on_another_server_does_not_help(admin_client, grantee):
    _set_role("operator")
    _insert_cg(1, "engine_users.grant_admin", "server", grantee["server_id"] + 1000)
    r = _grant(admin_client, grantee["user_id"], _WITH_GRANT_OPTION_BODY)
    assert r.status_code == 403
    assert _code(r) == CODE


def test_the_escalation_asks_for_a_fresh_step_up(admin_client, grantee, expire_step_up):
    expire_step_up(admin_client)
    r = _grant(admin_client, grantee["user_id"], _WITH_GRANT_OPTION_BODY)
    assert r.status_code == 403
    assert _code(r) == "access.step_up_required"
    assert grantee["engine_calls"] == []


def test_a_plain_grant_does_not_ask_for_step_up_beyond_the_route(admin_client, grantee, expire_step_up):
    """El escalamiento solo agrega exigencias al payload que delega; el resto no cambia."""
    expire_step_up(admin_client)
    r = _grant(admin_client, grantee["user_id"], _PLAIN_BODY)
    assert _code(r) != CODE


# --------------------------------------------------------------------------- #
# Ruta: POST /server-users/provision (grants iniciales)                        #
# --------------------------------------------------------------------------- #


def test_provision_with_initial_delegating_grants_is_checked_before_creating_the_user(
    admin_client, grantee, monkeypatch
):
    """
    Un portador suelto de ``engine_users.credentials`` sin ``grant_admin`` no deja una cuenta
    creada a medias: el chequeo corre antes del alta.
    """
    _set_role("operator")
    _insert_cg(1, "engine_users.credentials", "server", grantee["server_id"])
    created: list[str] = []
    from app.controllers.server_user_controller import ServerUserController

    original = ServerUserController.create_server_user
    monkeypatch.setattr(
        ServerUserController,
        "create_server_user",
        lambda self, *a, **k: created.append("created") or original(self, *a, **k),
    )
    body = {
        "server_id": grantee["server_id"],
        "username": "nuevo_con_grants",
        "password": "ContraseñaLarga123",
        "initial_grants": [
            {
                "level": "table",
                "object_ref": {"database": "shop", "table": "orders"},
                "privileges": ["SELECT"],
                "with_grant_option": True,
            }
        ],
    }
    r = admin_client.post("/api/v1/server-users/provision", json=body)

    assert r.status_code == 403, r.text
    assert _code(r) == CODE
    assert created == [], "la cuenta no debe crearse si los grants iniciales no están permitidos"


# --------------------------------------------------------------------------- #
# Ruta: POST /managed-databases/{id}/reassign-owner?provision=true             #
# --------------------------------------------------------------------------- #

_MDB = "/api/v1/managed-databases"


@pytest.fixture()
def parque(admin_client, server_payload, monkeypatch):
    """Un servidor y una BD en desarrollo, y un motor que no responde (no se llega a él)."""

    def _boom(*args, **kwargs):
        raise AppHttpException("Motor no disponible en la prueba.", 502)

    monkeypatch.setattr(remote_engine, "get_engine", _boom)
    dev = env_id("development")
    server_id = admin_client.post("/api/v1/servers", json=server_payload()).json()["data"]["id"]
    db_id = sembrar_bd(server_id=server_id, environment_id=dev, name="grant_admin_dev")
    return {"server_id": server_id, "db_id": db_id, "dev": dev}


def _reassign(admin_client, db_id: int, provision: bool):
    return admin_client.post(
        f"{_MDB}/{db_id}/reassign-owner",
        params={"provision": str(provision).lower()},
        json={"owner_id": 999999},
    )


def test_reassign_with_provision_needs_grant_admin_on_top_of_databases_drop(
    admin_client, parque
):
    _set_role("operator")
    _insert_cg(1, "databases.drop", "environment", parque["dev"])
    r = _reassign(admin_client, parque["db_id"], True)

    assert r.status_code == 403, r.text
    assert _code(r) == CODE
    assert _public_context(r)["reason"] == "provision_reassign_owner"
    assert _public_context(r)["required_capability"] == "engine_users.grant_admin"


def test_reassign_with_provision_passes_with_both_capabilities(admin_client, parque):
    _set_role("operator")
    _insert_cg(1, "databases.drop", "environment", parque["dev"])
    _insert_cg(1, "engine_users.grant_admin", "environment", parque["dev"])
    r = _reassign(admin_client, parque["db_id"], True)
    assert _code(r) not in {CODE, "access.forbidden"}, r.text


def test_reassign_without_provision_does_not_need_it(admin_client, parque):
    _set_role("operator")
    r = _reassign(admin_client, parque["db_id"], False)
    assert _code(r) not in {CODE, "access.forbidden"}, r.text


def test_owner_reassigns_with_provision(admin_client, parque):
    r = _reassign(admin_client, parque["db_id"], True)
    assert _code(r) not in {CODE, "access.forbidden"}, r.text


def test_an_operator_without_the_drop_grant_still_gets_the_opaque_403(admin_client, parque):
    """El orden se conserva: primero ``databases.drop`` (403 opaco), después ``grant_admin``."""
    _set_role("operator")
    r = _reassign(admin_client, parque["db_id"], True)
    assert r.status_code == 403
    assert _code(r) == "access.forbidden"
