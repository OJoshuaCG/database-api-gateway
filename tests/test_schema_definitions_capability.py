"""
``schema.definitions``: el CÓDIGO de vistas, rutinas, triggers y eventos.

Lo heredan ``operator`` y ``owner``; ``viewer`` NO (restricción intencional: sigue viendo la
estructura, ya no los cuerpos). Aplica al snapshot de una base y a las comparaciones de esquema
(``items``, ``export``, ``resolve-selection``). Sin la capacidad el objeto sale igual, pero con el
cuerpo vacío y ``redacted=true`` (nunca un vacío silencioso); las tablas no se tocan.

Cubre el catálogo (pertenencia por rol, invariantes), el módulo puro ``definition_visibility``,
las rutas por rol y por capacidad suelta, y que los consumidores internos (blueprint desde
snapshot) siguen recibiendo el dump completo.
"""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest
from sqlalchemy import text

import app.controllers.server_controller as sc
from app.core.actor import admin_actor
from app.core.database import Database
from app.core.scope_targets import server_database
from app.services import capability_catalog as cc
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    ROLE_CAPABILITIES,
    Capability,
    GatewayRole,
    GlobalCapability,
)
from app.services.db_admin import definition_visibility as dv
from app.services.db_admin.dtos import DumpStatement, StructureDump
from tests.test_api_schema_comparisons import _create, _setup
from tests.test_capability_grant_crud import _insert_cg

CAP = Capability.SCHEMA_DEFINITIONS

_VIEW_BODY = "CREATE VIEW v_top AS SELECT * FROM clientes WHERE pais = 'AR'"
_ROUTINE_BODY = "CREATE PROCEDURE sp_x() BEGIN SELECT 1; END"
_TABLE_DDL = "CREATE TABLE clientes (id INT PRIMARY KEY, pais VARCHAR(2))"


# --------------------------------------------------------------------------- #
# Catálogo                                                                     #
# --------------------------------------------------------------------------- #


def test_spec_shape():
    s = cc.spec(CAP)
    assert (s.module, s.level) == ("schema", "definitions")
    assert (s.mutates, s.discloses, s.requires_step_up, s.scope_axis) == (
        False,
        False,
        False,
        "environment",
    )
    assert not s.destructive and not s.agent_allowed
    assert CAP not in AGENT_ALLOWED
    assert cc.is_grantable(CAP)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (GatewayRole.VIEWER, False),
        (GatewayRole.OPERATOR, True),
        (GatewayRole.OWNER, True),
    ],
    ids=lambda v: getattr(v, "value", str(v)),
)
def test_inheritance_matrix_per_role(role, expected):
    """Viewer PIERDE los cuerpos (restricción intencional); operator y owner los conservan."""
    assert (CAP in ROLE_CAPABILITIES[role]) is expected


def test_no_global_holds_it_and_it_is_not_sensitive():
    for caps in cc.GLOBAL_CAPABILITIES.values():
        assert CAP not in caps
    # Está en operator, así que no es `owner − operator`: otorgarla suelta no pide segundo aprobador.
    assert CAP not in cc.OWNER_ONLY_CAPABILITIES
    assert not cc.is_sensitive(CAP)
    assert "schema.definitions" not in cc._SENSITIVE_POLICY


def test_granting_it_loose_implies_no_other_capability():
    assert CAP not in cc.IMPLIED_READ


def test_a_viewer_with_it_loose_gains_only_it():
    """Una capacidad puntual suma solo esa: no arrastra nada de operator."""
    actor = admin_actor(
        user_id=1,
        username="v",
        role=GatewayRole.VIEWER,
        capability_grants=[(CAP, "environment", 1)],
    )
    assert actor.has(CAP)
    assert actor.capabilities - ROLE_CAPABILITIES[GatewayRole.VIEWER] == {CAP}


def test_it_does_not_disclose_because_operator_may_not_hold_a_disclosing_capability(
    monkeypatch,
):
    """
    El invariante 7b prohíbe que ``operator`` tenga una capacidad que divulga. Como la hereda
    operator, no puede declararse ``discloses=True``: se prueba que el invariante muerde.
    """
    spec = cc.spec(CAP)
    assert not spec.discloses
    disclosing = cc.CapabilitySpec(
        id=spec.id,
        module=spec.module,
        level=spec.level,
        label=spec.label,
        mutates=False,
        discloses=True,
        requires_step_up=True,
        agent_allowed=False,
        scope_axis=spec.scope_axis,
    )
    patched = {**cc._BY_ID, CAP: disclosing}
    monkeypatch.setattr(cc, "_BY_ID", MappingProxyType(patched))
    with pytest.raises(AssertionError, match="operator"):
        cc._assert_invariants()


def test_the_catalog_matrix_publishes_the_inheritance():
    row = next(r for r in cc.capability_matrix() if r["id"] == "schema.definitions")
    assert row["roles"] == ["operator", "owner"]
    assert row["global_capabilities"] == []
    assert row["grantable"] is True and row["sensitive"] is False


def test_security_officer_alone_does_not_read_definitions():
    actor = admin_actor(
        user_id=2,
        username="oficial",
        role=GatewayRole.VIEWER,
        globals_=frozenset({GlobalCapability.SECURITY_OFFICER}),
    )
    assert not actor.has(CAP)


# --------------------------------------------------------------------------- #
# Módulo puro: definition_visibility                                           #
# --------------------------------------------------------------------------- #


def _dump() -> StructureDump:
    return StructureDump(
        database="legacy",
        source_engine="mysql",
        statements=[
            DumpStatement(object_type="table", name="clientes", ddl=_TABLE_DDL),
            DumpStatement(object_type="view", name="v_top", ddl=_VIEW_BODY),
            DumpStatement(object_type="materialized_view", name="mv_top", ddl="CREATE MATERIALIZED VIEW mv_top AS SELECT 1"),
            DumpStatement(object_type="routine", name="sp_x", ddl=_ROUTINE_BODY),
            DumpStatement(object_type="trigger", name="trg", ddl="CREATE TRIGGER trg BEFORE INSERT ON clientes FOR EACH ROW SET NEW.pais='AR'"),
            DumpStatement(object_type="event", name="ev", ddl="CREATE EVENT ev ON SCHEDULE EVERY 1 DAY DO SELECT 1"),
            DumpStatement(object_type="sequence", name="seq", ddl="CREATE SEQUENCE seq"),
        ],
        has_non_portable=True,
    )


def test_definition_object_types_are_exactly_the_five_with_code():
    assert dv.DEFINITION_OBJECT_TYPES == {
        "view",
        "materialized_view",
        "routine",
        "trigger",
        "event",
    }


def test_redact_dump_empties_only_the_objects_with_code_and_marks_them():
    redacted = dv.redact_dump(_dump())
    by_name = {s.name: s for s in redacted.statements}

    for name in ("v_top", "mv_top", "sp_x", "trg", "ev"):
        assert by_name[name].ddl == ""
        assert by_name[name].redacted is True
    # La estructura queda completa y sin marca.
    assert by_name["clientes"].ddl == _TABLE_DDL and by_name["clientes"].redacted is False
    assert by_name["seq"].ddl == "CREATE SEQUENCE seq" and by_name["seq"].redacted is False
    # El orden, los nombres y los tipos se conservan.
    assert [s.name for s in redacted.statements] == [s.name for s in _dump().statements]


def test_redact_dump_does_not_mutate_the_original():
    original = _dump()
    dv.redact_dump(original)
    assert next(s for s in original.statements if s.name == "v_top").ddl == _VIEW_BODY


def test_redact_item_hides_both_bodies_and_keeps_the_rest():
    item = {
        "object_type": "view",
        "object_name": "v_top",
        "change_type": "modified",
        "sql": _VIEW_BODY,
        "down_sql": "CREATE VIEW v_top AS SELECT 1",
        "risk": {"destructive": False},
    }
    out = dv.redact_item(item)
    assert out["sql"] == "" and out["down_sql"] == "" and out["redacted"] is True
    assert out["object_name"] == "v_top" and out["risk"] == {"destructive": False}
    assert item["sql"] == _VIEW_BODY, "no muta el original"


def test_redact_item_keeps_an_absent_rollback_absent():
    out = dv.redact_item(
        {"object_type": "routine", "object_name": "p", "sql": _ROUTINE_BODY, "down_sql": None}
    )
    assert out["down_sql"] is None and out["redacted"] is True


def test_redact_item_leaves_tables_and_structure_alone():
    for object_type in ("table", "column", "index", "foreign_key", "sequence", "enum_type"):
        item = {"object_type": object_type, "object_name": "x", "sql": "ALTER ...", "down_sql": "y"}
        out = dv.redact_item(item)
        assert out["sql"] == "ALTER ..." and out["down_sql"] == "y" and out["redacted"] is False


def _actor(role, **kwargs):
    return admin_actor(user_id=1, username="u", role=role, **kwargs)


def test_actor_reads_definitions_by_role():
    target = server_database(1, "legacy")
    assert dv.actor_reads_definitions(_actor(GatewayRole.OWNER), target)
    assert dv.actor_reads_definitions(_actor(GatewayRole.OPERATOR), target)
    assert not dv.actor_reads_definitions(_actor(GatewayRole.VIEWER), target)


def test_actor_reads_definitions_fails_closed_for_a_non_actor():
    target = server_database(1, "legacy")
    assert not dv.actor_reads_definitions(None, target)
    assert not dv.actor_reads_definitions({"id": 1, "role": "owner"}, target)


# --------------------------------------------------------------------------- #
# Ruta: GET /servers/{id}/databases/{db}/snapshot                              #
# --------------------------------------------------------------------------- #


class _FakeSnapshotAdapter:
    def __init__(self, dump):
        self._dump = dump

    def dump_structure(self, database):
        return self._dump


def _set_role(role: str) -> None:
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = :r WHERE username = 'admin'"), {"r": role}
        )


def _server(admin_client, server_payload) -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload())
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


@pytest.fixture()
def snapshot_server(admin_client, server_payload, monkeypatch):
    server_id = _server(admin_client, server_payload)
    monkeypatch.setattr(sc, "get_adapter", lambda target: _FakeSnapshotAdapter(_dump()))
    return server_id


def _snapshot(admin_client, server_id):
    return admin_client.get(f"/api/v1/servers/{server_id}/databases/legacy/snapshot")


@pytest.mark.parametrize("role", ["owner", "operator"])
def test_snapshot_returns_full_definitions_to_operator_and_owner(
    admin_client, snapshot_server, role
):
    _set_role(role)
    r = _snapshot(admin_client, snapshot_server)
    assert r.status_code == 200, r.text
    statements = {s["name"]: s for s in r.json()["data"]["statements"]}
    assert statements["v_top"]["ddl"] == _VIEW_BODY
    assert statements["sp_x"]["ddl"] == _ROUTINE_BODY
    assert not any(s["redacted"] for s in statements.values())


def test_snapshot_redacts_definitions_for_a_viewer_but_keeps_the_structure(
    admin_client, snapshot_server
):
    _set_role("viewer")
    r = _snapshot(admin_client, snapshot_server)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    statements = {s["name"]: s for s in data["statements"]}

    for name in ("v_top", "mv_top", "sp_x", "trg", "ev"):
        assert statements[name]["ddl"] == ""
        assert statements[name]["redacted"] is True
    assert statements["clientes"]["ddl"] == _TABLE_DDL
    assert statements["clientes"]["redacted"] is False
    assert statements["seq"]["redacted"] is False
    assert _VIEW_BODY not in r.text and _ROUTINE_BODY not in r.text
    # Los metadatos del dump no se alteran.
    assert data["has_non_portable"] is True
    assert len(data["statements"]) == len(_dump().statements)


def test_a_viewer_with_a_loose_grant_on_that_server_sees_the_definitions(
    admin_client, snapshot_server
):
    _set_role("viewer")
    _insert_cg(1, "schema.definitions", "server", snapshot_server)
    r = _snapshot(admin_client, snapshot_server)
    statements = {s["name"]: s for s in r.json()["data"]["statements"]}
    assert statements["v_top"]["ddl"] == _VIEW_BODY
    assert statements["v_top"]["redacted"] is False


def test_a_loose_grant_on_another_server_does_not_help_a_viewer(admin_client, snapshot_server):
    _set_role("viewer")
    _insert_cg(1, "schema.definitions", "server", snapshot_server + 1000)
    r = _snapshot(admin_client, snapshot_server)
    statements = {s["name"]: s for s in r.json()["data"]["statements"]}
    assert statements["v_top"]["redacted"] is True


def test_the_snapshot_guard_stays_databases_read_so_a_viewer_is_not_forbidden(
    admin_client, snapshot_server
):
    _set_role("viewer")
    assert _snapshot(admin_client, snapshot_server).status_code == 200


# --------------------------------------------------------------------------- #
# Consumidor interno: blueprint desde snapshot (dump COMPLETO)                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["owner", "operator"])
def test_from_snapshot_keeps_working_with_the_full_definitions(
    admin_client, snapshot_server, role
):
    _set_role(role)
    r = admin_client.post(
        "/api/v1/database-models/from-snapshot",
        json={
            "server_id": snapshot_server,
            "database": "legacy",
            "name": f"Legacy {role}",
            "slug": f"legacy-{role}",
        },
    )
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["model"]["id"]
    migration = admin_client.get(f"/api/v1/database-models/{model_id}/migrations/0001").json()["data"]
    assert _VIEW_BODY in migration["up_sql"]
    assert _ROUTINE_BODY in migration["up_sql"]


# --------------------------------------------------------------------------- #
# Rutas: comparaciones de esquema                                              #
# --------------------------------------------------------------------------- #


def _items(admin_client, comparison_id):
    r = admin_client.get(f"/api/v1/schema-comparisons/{comparison_id}/items?size=50")
    assert r.status_code == 200, r.text
    return {i["object_name"]: i for i in r.json()["data"]}


@pytest.fixture()
def comparison(admin_client, monkeypatch):
    src_id, tgt_id, _, _ = _setup(admin_client, monkeypatch, port=3601)
    created = _create(admin_client, src_id, tgt_id)
    assert created.status_code == 201, created.text
    summary = created.json()["data"]
    return {"id": summary["id"], "server_id": summary["source_server_id"]}


@pytest.mark.parametrize("role", ["owner", "operator"])
def test_comparison_items_carry_the_bodies_for_operator_and_owner(
    admin_client, comparison, role
):
    _set_role(role)
    items = _items(admin_client, comparison["id"])
    assert items["PROCEDURE:sp_x"]["sql"] == _ROUTINE_BODY
    assert items["PROCEDURE:sp_x"]["redacted"] is False
    assert items["new_t"]["redacted"] is False


def test_comparison_items_redact_the_bodies_for_a_viewer_but_not_the_tables(
    admin_client, comparison
):
    _set_role("viewer")
    items = _items(admin_client, comparison["id"])

    routine = items["PROCEDURE:sp_x"]
    assert routine["sql"] == "" and routine["redacted"] is True
    assert routine["object_type"] == "routine" and routine["change_type"] == "new"
    assert items["new_t"]["sql"] == "CREATE TABLE new_t (id INT PRIMARY KEY)"
    assert items["new_t"]["redacted"] is False
    assert items["old_t"]["sql"] == "DROP TABLE `old_t`"


def test_comparison_export_replaces_the_bodies_with_a_hidden_marker_for_a_viewer(
    admin_client, comparison
):
    _set_role("viewer")
    r = admin_client.get(f"/api/v1/schema-comparisons/{comparison['id']}/export")
    assert r.status_code == 200, r.text
    assert "CREATE PROCEDURE sp_x" not in r.text
    assert "contenido oculto" in r.text
    assert "CREATE TABLE new_t" in r.text


def test_comparison_export_is_complete_for_an_operator(admin_client, comparison):
    _set_role("operator")
    r = admin_client.get(f"/api/v1/schema-comparisons/{comparison['id']}/export")
    assert r.status_code == 200, r.text
    assert "CREATE PROCEDURE sp_x" in r.text
    assert "contenido oculto" not in r.text


def test_comparison_resolve_selection_still_answers_a_viewer(admin_client, comparison):
    _set_role("owner")
    selected = [_items(admin_client, comparison["id"])["new_t"]["id"]]
    _set_role("viewer")
    r = admin_client.post(
        f"/api/v1/schema-comparisons/{comparison['id']}/resolve-selection",
        json={"selected_item_ids": selected},
    )
    assert r.status_code == 200, r.text
    assert _ROUTINE_BODY not in r.text


def _added_row(object_type: str):
    return SimpleNamespace(
        id=7,
        object_type=object_type,
        object_name="PROCEDURE:sp_x" if object_type == "routine" else "new_t",
        change_type="new",
        sql=_ROUTINE_BODY if object_type == "routine" else "CREATE TABLE new_t (id INT)",
    )


def test_added_by_dependency_items_are_redacted_without_the_capability():
    from app.controllers.schema_comparison_controller import SchemaComparisonController

    view = SchemaComparisonController._added_item_view
    routine = view(_added_row("routine"), sees_definitions=False)
    assert routine["sql"] == "" and routine["redacted"] is True
    table = view(_added_row("table"), sees_definitions=False)
    assert table["sql"] == "CREATE TABLE new_t (id INT)" and table["redacted"] is False


def test_added_by_dependency_items_keep_the_body_with_the_capability():
    from app.controllers.schema_comparison_controller import SchemaComparisonController

    routine = SchemaComparisonController._added_item_view(
        _added_row("routine"), sees_definitions=True
    )
    assert routine["sql"] == _ROUTINE_BODY and routine["redacted"] is False


def test_a_viewer_with_a_loose_grant_on_the_comparison_server_sees_the_bodies(
    admin_client, comparison
):
    _set_role("viewer")
    _insert_cg(1, "schema.definitions", "server", comparison["server_id"])
    items = _items(admin_client, comparison["id"])
    assert items["PROCEDURE:sp_x"]["sql"] == _ROUTINE_BODY
    assert items["PROCEDURE:sp_x"]["redacted"] is False


def test_the_comparison_listing_requires_the_reader_keyword():
    """Sin ``reader`` falla con ``TypeError`` en vez de devolver el código de las vistas."""
    from app.controllers.schema_comparison_controller import SchemaComparisonController

    with pytest.raises(TypeError):
        SchemaComparisonController().list_items(1, limit=1, offset=0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SchemaComparisonController().resolve_selection(1, [1])  # type: ignore[call-arg]
