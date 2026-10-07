"""
Partición ``policy.admin`` -> ``audit.read`` (revisor) + ``crypto.rotate`` (actor).

Cubre: la forma de las dos capacidades y su pertenencia (solo ``security_officer``), que
``policy.admin`` quedó retirada (y que un lector descarta una fila que la nombre), qué guard declara
cada ruta, y el enmascarado de los literales de SQL en el ``detail`` de las filas
``query_console.*`` de ``GET /audit-log``.

Los tests de enmascarado ejercen ``AuditLogController`` con actores en memoria (sin servidor real);
los de ruta usan los clientes de ``conftest`` (``so_client``, ``aa_client``, ``owner_client``).
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from app.controllers.audit_log_controller import AuditLogController
from app.core.actor import admin_actor
from app.core.capability_resolution import parse_access_context
from app.models.audit_log import AuditLog
from app.services import capability_catalog as cc
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    GLOBAL_CAPABILITIES,
    ROLE_CAPABILITIES,
    Capability,
    GatewayRole,
    GlobalCapability,
)

_GUARD_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"
AUDIT = "/api/v1/audit-log"
ROTATE = "/api/v1/admin/crypto/rotate"

#: ``target_type`` exclusivo de las filas que siembra este archivo (la BD de test ya tiene filas).
_T = "zz_split_audit_test"

#: SQL con literales de negocio: lo que el enmascarado tiene que borrar.
_LITERAL_EMAIL = "ana.perez@cliente.com"
_LITERAL_DOC = "30123456"
_EXECUTE_SQL = f"UPDATE clientes SET email = '{_LITERAL_EMAIL}' WHERE documento = {_LITERAL_DOC}"
_EXECUTE_DETAIL = f"facturacion as app_rw (known) [medium]: {_EXECUTE_SQL}"


def _load_guard_script():
    spec = importlib.util.spec_from_file_location("check_route_capabilities", _GUARD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


def _officer():
    return admin_actor(
        user_id=1,
        username="oficial",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.SECURITY_OFFICER}),
    )


def _owner():
    return admin_actor(user_id=2, username="duenio", role=GatewayRole.OWNER)


# --------------------------------------------------------------------------- #
# Catálogo                                                                     #
# --------------------------------------------------------------------------- #


def test_audit_read_is_a_global_non_mutating_non_disclosing_capability():
    s = cc.spec(Capability.AUDIT_READ)
    assert (s.mutates, s.discloses, s.requires_step_up, s.scope_axis) == (
        False,
        False,
        False,
        "global",
    )
    assert not s.agent_allowed and not s.destructive
    assert not cc.is_grantable(Capability.AUDIT_READ)
    assert not cc.is_sensitive(Capability.AUDIT_READ)
    assert Capability.AUDIT_READ not in AGENT_ALLOWED


def test_crypto_rotate_is_a_global_mutating_step_up_capability():
    s = cc.spec(Capability.CRYPTO_ROTATE)
    assert (s.mutates, s.discloses, s.requires_step_up, s.scope_axis) == (
        True,
        False,
        True,
        "global",
    )
    assert not s.agent_allowed and not s.destructive
    assert not cc.is_grantable(Capability.CRYPTO_ROTATE)
    assert Capability.CRYPTO_ROTATE not in AGENT_ALLOWED


@pytest.mark.parametrize("cap", [Capability.AUDIT_READ, Capability.CRYPTO_ROTATE])
def test_only_security_officer_holds_the_split_capabilities(cap):
    """Ownership/inheritance matrix: ningún rol, ni access_admin; solo security_officer."""
    for role, caps in ROLE_CAPABILITIES.items():
        assert cap not in caps, role.value
    holders = {g for g, caps in GLOBAL_CAPABILITIES.items() if cap in caps}
    assert holders == {GlobalCapability.SECURITY_OFFICER}


def test_security_officer_keeps_exactly_what_policy_admin_gave_it():
    """Sin cambio de comportamiento: las dos mitades suman lo que era ``policy.admin``."""
    officer = GLOBAL_CAPABILITIES[GlobalCapability.SECURITY_OFFICER]
    assert {Capability.AUDIT_READ, Capability.CRYPTO_ROTATE} <= officer
    actor = _officer()
    assert actor.has(Capability.AUDIT_READ) and actor.has(Capability.CRYPTO_ROTATE)


@pytest.mark.parametrize(
    "role", [GatewayRole.VIEWER, GatewayRole.OPERATOR, GatewayRole.OWNER], ids=lambda r: r.value
)
def test_no_role_alone_reads_the_audit_or_rotates(role):
    actor = admin_actor(user_id=3, username="x", role=role)
    assert not actor.has(Capability.AUDIT_READ)
    assert not actor.has(Capability.CRYPTO_ROTATE)


def test_access_admin_alone_neither_reads_the_audit_nor_rotates():
    actor = admin_actor(
        user_id=4,
        username="aa",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.ACCESS_ADMIN}),
    )
    assert not actor.has(Capability.AUDIT_READ)
    assert not actor.has(Capability.CRYPTO_ROTATE)


def test_policy_admin_is_retired_and_cannot_come_back(monkeypatch):
    assert "policy.admin" in cc.RETIRED_CAPABILITIES
    assert "policy.admin" not in {c.value for c in Capability}
    assert cc.parse_scopes("policy.admin") == frozenset()
    assert not cc.is_grantable("policy.admin")
    # El invariante 12 muerde si alguien la reintroduce en el enum: se simula con un enum ad hoc.
    monkeypatch.setattr(cc, "RETIRED_CAPABILITIES", frozenset({"gateway.admin", "audit.read"}))
    with pytest.raises(AssertionError, match="retiradas"):
        cc._assert_invariants()


def test_readers_discard_a_capability_grant_row_naming_policy_admin():
    """Una fila legada o editada a mano con la retirada no acuña nada (fail-closed en el lector)."""
    ctx = {
        "role": "viewer",
        "globals": [],
        "grants": [],
        "capability_grants": [("policy.admin", "environment", 1, 10)],
    }
    parsed = parse_access_context(ctx)
    assert parsed.capability_grants == ()


# --------------------------------------------------------------------------- #
# Guards de ruta                                                               #
# --------------------------------------------------------------------------- #


def test_routes_declare_the_right_half():
    from main import app

    guard = _load_guard_script()
    declared = {}
    for path, route in guard._iter_routes(app):
        if path.startswith(("/api/v1/audit-log", "/api/v1/admin/crypto")):
            declared[path] = guard._capability_of(route)
    assert declared == {
        "/api/v1/audit-log": "audit.read",
        "/api/v1/audit-log/{entry_id}": "audit.read",
        "/api/v1/admin/crypto/rotate": "crypto.rotate",
    }


def test_access_admin_gets_the_opaque_403_on_both_routes(aa_client):
    assert aa_client.get(AUDIT).status_code == 403
    r = aa_client.post(ROTATE, json={})
    assert r.status_code == 403
    assert _code(r) == "access.forbidden"


def test_owner_without_security_officer_cannot_read_the_audit(owner_client):
    r = owner_client.get(AUDIT)
    assert r.status_code == 403, r.text
    assert _code(r) == "access.forbidden"


def test_security_officer_passes_the_capability_layer_on_both_routes(so_client):
    assert so_client.get(AUDIT).status_code == 200
    r = so_client.post(ROTATE, json={})
    assert _code(r) != "access.forbidden", r.text


# --------------------------------------------------------------------------- #
# Enmascarado del SQL de la consola en la auditoría                            #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def no_server_lookup(monkeypatch):
    """Sin BD: todo servidor es MySQL. El enmascarado en sí es lo que se prueba."""
    monkeypatch.setattr(
        AuditLogController,
        "_engines_by_server",
        staticmethod(lambda server_ids: {server_id: "mysql" for server_id in server_ids}),
    )


def _entry(action: str, detail: str | None, server_id: int | None = 7) -> dict:
    return {
        "id": 1,
        "action": action,
        "server_id": server_id,
        "detail": detail,
        "detail_json": None,
    }


def test_console_sql_literals_are_masked_for_a_reader_without_sql_console_execute(no_server_lookup):
    entry = _entry("query_console.execute", _EXECUTE_DETAIL)
    AuditLogController._apply_detail_masking([entry], reader=_officer())

    assert entry["detail_masked"] is True
    assert _LITERAL_EMAIL not in entry["detail"]
    assert _LITERAL_DOC not in entry["detail"]
    # Se conserva la estructura y el prefijo (bd, usuario, modo, peligro): sirve para revisar.
    assert entry["detail"].startswith("facturacion as app_rw (known) [medium]: ")
    assert "UPDATE" in entry["detail"] and "?" in entry["detail"]


def test_a_reader_who_can_execute_sql_at_the_target_sees_the_full_text(no_server_lookup):
    entry = _entry("query_console.execute", _EXECUTE_DETAIL)
    AuditLogController._apply_detail_masking([entry], reader=_owner())

    assert entry["detail_masked"] is False
    assert entry["detail"] == _EXECUTE_DETAIL


def test_a_console_row_without_a_server_is_masked_even_for_an_owner(no_server_lookup):
    """Sin servidor no hay destino al que anclar ``sql_console.execute``: fail-closed."""
    entry = _entry("query_console.execute", _EXECUTE_DETAIL, server_id=None)
    AuditLogController._apply_detail_masking([entry], reader=_owner())

    assert entry["detail_masked"] is True
    assert _LITERAL_EMAIL not in entry["detail"]


def test_an_unknown_console_detail_format_is_masked_whole(no_server_lookup):
    """Formato sin el prefijo conocido: se enmascara entero (un formato nuevo no abre la fuga)."""
    entry = _entry("query_console.future_action", f"SELECT * FROM t WHERE a = '{_LITERAL_EMAIL}'")
    AuditLogController._apply_detail_masking([entry], reader=_officer())

    assert entry["detail_masked"] is True
    assert _LITERAL_EMAIL not in entry["detail"]


def test_other_actions_are_never_touched(no_server_lookup):
    original = "texto libre con 'comillas' y 123"
    entry = _entry("access.approved", original)
    AuditLogController._apply_detail_masking([entry], reader=_officer())

    assert entry["detail_masked"] is False
    assert entry["detail"] == original


def test_masked_detail_recomputes_detail_json_from_the_masked_text(no_server_lookup):
    entry = _entry("query_console.execute", _EXECUTE_DETAIL)
    entry["detail_json"] = {"stale": _LITERAL_EMAIL}
    AuditLogController._apply_detail_masking([entry], reader=_officer())

    assert entry["detail_json"] is None


def test_list_entries_requires_the_reader_keyword():
    """Sin ``reader`` falla con ``TypeError`` en vez de devolver el SQL completo."""
    with pytest.raises(TypeError):
        AuditLogController().list_entries({}, limit=1, offset=0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AuditLogController().get_entry(1)  # type: ignore[call-arg]


def _seed_console_row(detail: str) -> int:
    from app.core.database import Database

    session = Database().get_declarative_base_session()
    try:
        row = AuditLog(
            action="query_console.execute",
            status="attempt",
            actor_type="admin",
            touched_engine=True,
            target_type=_T,
            detail=detail,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_get_audit_log_masks_console_sql_for_the_security_officer(so_client):
    entry_id = _seed_console_row(_EXECUTE_DETAIL)

    listing = so_client.get(AUDIT, params={"target_type": _T})
    assert listing.status_code == 200, listing.text
    [row] = listing.json()["data"]
    assert row["id"] == entry_id
    assert row["detail_masked"] is True
    assert _LITERAL_EMAIL not in row["detail"] and _LITERAL_DOC not in row["detail"]

    single = so_client.get(f"{AUDIT}/{entry_id}").json()["data"]
    assert single["detail_masked"] is True
    assert _LITERAL_EMAIL not in single["detail"]


def test_get_audit_log_marks_non_console_rows_as_not_masked(so_client):
    from app.core.database import Database

    session = Database().get_declarative_base_session()
    try:
        row = AuditLog(
            action="access.approved",
            status="success",
            actor_type="admin",
            touched_engine=False,
            target_type=_T,
            detail="texto libre, no JSON",
        )
        session.add(row)
        session.commit()
        entry_id = row.id
    finally:
        session.close()

    single = so_client.get(f"{AUDIT}/{entry_id}").json()["data"]
    assert single["detail_masked"] is False
    assert single["detail"] == "texto libre, no JSON"
