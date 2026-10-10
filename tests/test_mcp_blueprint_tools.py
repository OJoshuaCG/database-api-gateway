"""
Las tools MCP de blueprints: ``list_blueprints``, ``list_blueprint_migrations`` y
``get_blueprint_migration`` (esta última, bajo el scope ``data.blueprint_sql`` y su kill switch).

QUÉ SE VERIFICA ACÁ
-------------------
Todo lo que decide el gateway y corre real contra la BD de pruebas: la visibilidad por proyecto
(un blueprint compartido, ajeno o inexistente responde IGUAL), los campos exactos de la salida, el
orden numérico de versiones, la paginación por clave, el tope de 100 blueprints, los schemas
cerrados y que ninguna respuesta lleve SQL, autoría ni las subcadenas prohibidas.

Los blueprints y migraciones se siembran por ORM, sin pasar por la API: hace falta controlar
versiones (``0009``, ``0010``, ``10000``) y valores de autoría que la API no deja fijar.

Las listas se llaman por HTTP con un token real. ``get_blueprint_migration`` se llama a
``dispatch.handle`` con un actor de token armado a mano (el scope exige un emisor owner con step-up y
el kill switch encendido al emitir), igual que ``tests.test_mcp_get_definition``: el scope, el gate
y la auditoría corren de verdad sobre la BD de metadatos de test.

QUÉ NO SE VERIFICA ACÁ
----------------------
La redacción de credenciales cubre solo lo que reconoce ``definition_redaction`` (su contrato
completo está en ``tests.test_definition_redaction``); no es una garantía de que no quede ninguna.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_mcp_blueprint_tools``
"""

import dataclasses
import json

import pytest

from app.core import environments
from app.core.actor import admin_actor, token_actor
from app.core.database import Database
from app.exceptions import AppHttpException
from app.mcp import dispatch, jsonrpc, registry, result_budget
from app.models.audit_log import AuditLog
from app.models.database_model import DatabaseModel
from app.models.model_migration import ModelMigration
from app.models.project import ProjectDatabaseModel
from app.schemas import mcp as out
from app.services import audit as audit_mod
from app.services.capability_catalog import GatewayRole
from tests.step_up_helpers import OPEN_WINDOW
from tests.test_mcp_catalog_tools import motor_falso  # noqa: F401  (fixture)
from tests.test_mcp_server import _contenido, _crear_token, _proyecto, _rpc

SOLO_BLUEPRINTS = ["blueprints.read"]
PROHIBIDAS = ("confirm_token", "password", "encrypted", "host", "port")

#: Valores sembrados en columnas que la tool NO puede devolver. Si alguno aparece en una respuesta,
#: el mapeador dejó de ser una lista blanca.
SQL_SENTINEL = "CREATE TABLE sentinel_up_sql_marker (id INT)"
DOWN_SQL_SENTINEL = "DROP TABLE sentinel_down_sql_marker"
AUTHOR_SENTINEL = "autor.sentinel"

CHECKSUM = "a" * 64


@pytest.fixture()
def mcp_on(monkeypatch):
    """Enciende el kill switch del servidor (nace apagado)."""
    import app.core.mcp_auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_ENABLED", True)
    return True


# --------------------------------------------------------------------------- #
# Siembra y llamadas                                                           #
# --------------------------------------------------------------------------- #


def _blueprint(
    slug: str,
    *,
    project_ids: tuple[int, ...],
    description: str | None = None,
    is_active: bool = True,
    charset: str | None = None,
    collation: str | None = None,
) -> int:
    session = Database().get_declarative_base_session()
    try:
        blueprint = DatabaseModel(
            name=f"Blueprint {slug}",
            slug=slug,
            description=description,
            is_active=is_active,
            charset=charset,
            collation=collation,
        )
        session.add(blueprint)
        session.flush()
        for project_id in project_ids:
            session.add(ProjectDatabaseModel(project_id=project_id, model_id=blueprint.id))
        session.commit()
        return blueprint.id
    finally:
        session.close()


def _migration(model_id: int, version: str, **overrides) -> None:
    values = {
        "model_id": model_id,
        "version": version,
        "name": f"Migración {version}",
        "up_sql": SQL_SENTINEL,
        "checksum": CHECKSUM,
        "created_by_admin_id": 7,
        "created_by_username": AUTHOR_SENTINEL,
        "created_by_actor_type": "admin",
    }
    values.update(overrides)
    session = Database().get_declarative_base_session()
    try:
        session.add(ModelMigration(**values))
        session.commit()
    finally:
        session.close()


def _escenario(admin_client, *, scopes=None):
    """Un proyecto y un token acotado a él. ``scopes=None`` deja el default (``blueprints.read``)."""
    project_id = _proyecto(admin_client)
    extra = {} if scopes is None else {"scopes": scopes}
    token = _crear_token(admin_client, project_id=project_id, **extra)["token"]
    return project_id, token


def _llamar(client, token, tool, arguments=None):
    return _rpc(
        client, token, "tools/call", {"name": tool, "arguments": arguments or {}}
    )


def _ok(response) -> dict:
    result = response.json()["result"]
    assert result["isError"] is False, result
    return _contenido(response)


def _error(response) -> dict:
    result = response.json()["result"]
    assert result["isError"] is True, result
    return result["structuredContent"]["error"]


# --------------------------------------------------------------------------- #
# Registro                                                                     #
# --------------------------------------------------------------------------- #


def test_the_two_list_tools_are_always_registered_under_blueprints_read():
    from app.mcp import registry

    for name in ("list_blueprints", "list_blueprint_migrations"):
        spec = registry.BY_NAME[name]
        assert spec.scope == "blueprints.read"
        assert spec.touches_engine is False
        assert spec.annotations["readOnlyHint"] is True
        assert spec.input_schema["additionalProperties"] is False
        assert "data" not in spec.tags


def test_the_list_tools_do_not_depend_on_any_kill_switch():
    from app.mcp import registry

    built = {tool.name for tool in registry._build(data_read_enabled=False)}

    assert {"list_blueprints", "list_blueprint_migrations"} <= built


def test_the_migrations_schema_publishes_the_page_bounds_and_the_version_pattern():
    from app.mcp import registry

    properties = registry.BY_NAME["list_blueprint_migrations"].input_schema["properties"]

    assert properties["blueprint_id"]["minimum"] == 1
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 200
    assert properties["limit"]["default"] == 100
    assert properties["after_version"]["pattern"] == "^[0-9]{1,10}$"
    assert registry.BY_NAME["list_blueprint_migrations"].input_schema["required"] == [
        "blueprint_id"
    ]


def test_a_token_without_blueprints_read_is_denied_both_tools(
    client, admin_client, mcp_on, motor_falso  # noqa: F811
):
    _, token = _escenario(admin_client, scopes=["databases.read"])

    for tool, arguments in (
        ("list_blueprints", {}),
        ("list_blueprint_migrations", {"blueprint_id": 1}),
    ):
        assert _error(_llamar(client, token, tool, arguments))["code"] == "mcp.scope_denied"
    assert motor_falso.abiertas == []


def test_an_undeclared_argument_is_rejected_as_invalid_input(client, admin_client, mcp_on):
    _, token = _escenario(admin_client)

    for tool, arguments in (
        ("list_blueprints", {"project_id": 1}),
        ("list_blueprint_migrations", {"blueprint_id": 1, "project_id": 2}),
    ):
        response = _llamar(client, token, tool, arguments)
        assert response.json()["error"]["code"] == -32602, tool


# --------------------------------------------------------------------------- #
# list_blueprints                                                              #
# --------------------------------------------------------------------------- #


def test_list_blueprints_returns_exactly_the_declared_fields(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint(
        "core",
        project_ids=(project_id,),
        description="Esquema base",
        charset="utf8mb4",
        collation="utf8mb4_general_ci",
    )
    _migration(blueprint_id, "0001")
    _migration(blueprint_id, "0002")

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    assert envelope["data"]["count"] == 1
    [item] = envelope["data"]["blueprints"]
    assert set(item) == {
        "blueprint_id",
        "slug",
        "name",
        "description",
        "current_version",
        "is_active",
        "charset",
        "collation",
        "migration_count",
    }
    assert item["blueprint_id"] == blueprint_id
    assert item["slug"] == "core"
    assert item["description"] == "Esquema base"
    assert item["charset"] == "utf8mb4" and item["collation"] == "utf8mb4_general_ci"
    assert item["is_active"] is True
    assert item["migration_count"] == 2


def test_the_envelope_is_a_blueprint_envelope_without_database_and_marks_free_text(
    client, admin_client, mcp_on
):
    project_id, token = _escenario(admin_client)
    _blueprint("core", project_ids=(project_id,), description="Texto de un tercero")

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    assert set(envelope) == {
        "notice",
        "data",
        "source",
        "untrusted_content",
        "untrusted_fields",
        "clipped_fields",
        "warnings",
        "generated_at",
    }
    assert "database" not in envelope
    assert envelope["source"] == "gateway_blueprint"
    assert envelope["untrusted_content"] is True
    assert envelope["notice"] == out.BLUEPRINT_UNTRUSTED_NOTICE
    assert envelope["untrusted_fields"] == [
        "data.blueprints[0].name",
        "data.blueprints[0].description",
    ]
    assert envelope["clipped_fields"] == []


def test_an_overlong_description_is_clipped_and_annotated(client, admin_client, mcp_on):
    from app.mcp.tools._envelope import FREE_TEXT_MAX_CHARS

    project_id, token = _escenario(admin_client)
    _blueprint("core", project_ids=(project_id,), description="x" * (FREE_TEXT_MAX_CHARS + 50))

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    assert len(envelope["data"]["blueprints"][0]["description"]) == FREE_TEXT_MAX_CHARS
    assert envelope["clipped_fields"] == ["data.blueprints[0].description"]


def test_a_blueprint_with_no_reachable_database_is_still_listed(client, admin_client, mcp_on):
    """Es la plantilla lo que se lista: no hay ninguna base sembrada y aparece igual."""
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("sin-bases", project_ids=(project_id,), is_active=False)

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    [item] = envelope["data"]["blueprints"]
    assert item["blueprint_id"] == blueprint_id
    assert item["migration_count"] == 0
    assert item["is_active"] is False


def test_blueprints_are_ordered_by_slug(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    for slug in ("zeta", "alfa", "medio"):
        _blueprint(slug, project_ids=(project_id,))

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    assert [b["slug"] for b in envelope["data"]["blueprints"]] == ["alfa", "medio", "zeta"]


def test_shared_and_foreign_blueprints_are_absent_from_the_list(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    other_project_id = _proyecto(admin_client, nombre="Ajeno")
    third_project_id = _proyecto(admin_client, nombre="Tercero")
    _blueprint("propio", project_ids=(project_id,))
    _blueprint("compartido", project_ids=(project_id, other_project_id))
    _blueprint("ajeno", project_ids=(other_project_id,))
    _blueprint("de-otro-vinculo", project_ids=(third_project_id,))

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    assert [b["slug"] for b in envelope["data"]["blueprints"]] == ["propio"]


def test_an_empty_project_gets_an_empty_list_not_an_error(client, admin_client, mcp_on):
    _, token = _escenario(admin_client)

    envelope = _ok(_llamar(client, token, "list_blueprints"))

    assert envelope["data"] == {"blueprints": [], "count": 0}


def _seed_many_blueprints(project_id: int, amount: int) -> None:
    session = Database().get_declarative_base_session()
    try:
        for index in range(amount):
            blueprint = DatabaseModel(name=f"Bulk {index:04d}", slug=f"bulk-{index:04d}")
            session.add(blueprint)
            session.flush()
            session.add(ProjectDatabaseModel(project_id=project_id, model_id=blueprint.id))
        session.commit()
    finally:
        session.close()


def test_exactly_the_limit_is_listed_and_one_more_fails_without_a_partial_list(
    client, admin_client, mcp_on
):
    from app.controllers.target_resolution import MAX_BLUEPRINTS_PER_LIST

    project_id, token = _escenario(admin_client)
    _seed_many_blueprints(project_id, MAX_BLUEPRINTS_PER_LIST)

    envelope = _ok(_llamar(client, token, "list_blueprints"))
    assert envelope["data"]["count"] == MAX_BLUEPRINTS_PER_LIST

    _blueprint("uno-mas", project_ids=(project_id,))
    response = _llamar(client, token, "list_blueprints")

    structured_content = response.json()["result"]["structuredContent"]
    assert response.json()["result"]["isError"] is True
    assert set(structured_content) == {"error"}
    assert structured_content["error"]["code"] == "mcp.too_many_objects"
    assert "bulk-0000" not in json.dumps(structured_content)


def test_the_too_many_blueprints_cap_is_one_hundred():
    from app.controllers.target_resolution import MAX_BLUEPRINTS_PER_LIST

    assert MAX_BLUEPRINTS_PER_LIST == 100


# --------------------------------------------------------------------------- #
# list_blueprint_migrations                                                    #
# --------------------------------------------------------------------------- #


def test_list_blueprint_migrations_returns_exactly_the_declared_fields(
    client, admin_client, mcp_on
):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        kind="data",
        is_baseline=True,
        reviewed=False,
        source_engine="mysql",
        has_non_portable=True,
        down_sql=DOWN_SQL_SENTINEL,
    )
    _migration(blueprint_id, "0002")

    envelope = _ok(
        _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})
    )

    data = envelope["data"]
    assert set(data) == {"blueprint", "migrations", "count", "total", "next_after_version"}
    assert data["blueprint"] == {
        "blueprint_id": blueprint_id,
        "slug": "core",
        "current_version": "0.0.0",
    }
    assert data["count"] == 2 and data["total"] == 2
    assert data["next_after_version"] is None
    first, second = data["migrations"]
    assert set(first) == {
        "version",
        "name",
        "kind",
        "is_baseline",
        "reviewed",
        "has_rollback",
        "source_engine",
        "has_procedural_objects",
        "checksum",
        "created_at",
    }
    assert first["version"] == "0001" and first["kind"] == "data"
    assert first["is_baseline"] is True and first["reviewed"] is False
    assert first["has_rollback"] is True
    assert first["source_engine"] == "mysql"
    assert first["has_procedural_objects"] is True
    assert first["checksum"] == CHECKSUM
    assert first["created_at"]
    assert second["has_rollback"] is False
    assert second["has_procedural_objects"] is False
    assert second["kind"] == "schema"
    assert second["source_engine"] is None


def test_the_migration_list_never_carries_sql_or_authorship(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        down_sql=DOWN_SQL_SENTINEL,
        down_sql_suggested=DOWN_SQL_SENTINEL,
        up_sql_mysql=SQL_SENTINEL,
        up_sql_postgresql=SQL_SENTINEL,
    )

    response = _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})

    raw = json.dumps(response.json()["result"])
    for sentinel in ("sentinel_up_sql_marker", "sentinel_down_sql_marker", AUTHOR_SENTINEL):
        assert sentinel not in raw, sentinel
    for key in ("up_sql", "down_sql", "created_by", "username", "actor_type"):
        assert key not in raw, key


def test_the_migration_name_is_marked_as_untrusted_free_text(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001")
    _migration(blueprint_id, "0002")

    envelope = _ok(
        _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})
    )

    assert envelope["source"] == "gateway_blueprint"
    assert "database" not in envelope
    assert envelope["untrusted_fields"] == [
        "data.migrations[0].name",
        "data.migrations[1].name",
    ]


def test_versions_are_ordered_numerically_not_alphabetically(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    # Se insertan desordenadas a propósito. Alfabéticamente ``10000`` iría antes que ``9999``.
    for version in ("0010", "10000", "0009", "9999", "0100"):
        _migration(blueprint_id, version)

    envelope = _ok(
        _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})
    )

    versions = [m["version"] for m in envelope["data"]["migrations"]]
    assert versions == ["0009", "0010", "0100", "9999", "10000"]


def test_keyset_paging_returns_every_migration_once_with_the_full_total(
    client, admin_client, mcp_on
):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    expected_versions = ["0008", "0009", "0010", "0011", "0012"]
    for version in reversed(expected_versions):
        _migration(blueprint_id, version)

    seen: list[str] = []
    after_version = None
    pages = 0
    while True:
        arguments = {"blueprint_id": blueprint_id, "limit": 2}
        if after_version is not None:
            arguments["after_version"] = after_version
        data = _ok(_llamar(client, token, "list_blueprint_migrations", arguments))["data"]
        assert data["total"] == 5
        assert data["count"] == len(data["migrations"])
        seen.extend(m["version"] for m in data["migrations"])
        pages += 1
        after_version = data["next_after_version"]
        if after_version is None:
            break

    assert seen == expected_versions
    assert pages == 3


def test_next_after_version_is_the_last_version_of_a_page_only_when_more_remain(
    client, admin_client, mcp_on
):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    for version in ("0001", "0002", "0003"):
        _migration(blueprint_id, version)

    exact_page = _ok(
        _llamar(
            client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id, "limit": 3}
        )
    )["data"]
    short_page = _ok(
        _llamar(
            client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id, "limit": 2}
        )
    )["data"]

    assert exact_page["next_after_version"] is None
    assert short_page["next_after_version"] == "0002"


def test_after_version_works_with_a_version_that_does_not_exist(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    for version in ("0001", "0003", "0005"):
        _migration(blueprint_id, version)

    data = _ok(
        _llamar(
            client,
            token,
            "list_blueprint_migrations",
            {"blueprint_id": blueprint_id, "after_version": "0002"},
        )
    )["data"]

    assert [m["version"] for m in data["migrations"]] == ["0003", "0005"]
    assert data["total"] == 3


def test_a_blueprint_without_migrations_gets_an_empty_page_not_an_error(
    client, admin_client, mcp_on
):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("vacio", project_ids=(project_id,))

    data = _ok(
        _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})
    )["data"]

    assert data["migrations"] == []
    assert data["count"] == 0 and data["total"] == 0
    assert data["next_after_version"] is None


def test_the_default_page_size_is_one_hundred(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    session = Database().get_declarative_base_session()
    try:
        for number in range(1, 102):
            session.add(
                ModelMigration(
                    model_id=blueprint_id,
                    version=f"{number:04d}",
                    name=f"m{number}",
                    up_sql="SELECT 1",
                    checksum=CHECKSUM,
                )
            )
        session.commit()
    finally:
        session.close()

    data = _ok(
        _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})
    )["data"]

    assert data["count"] == 100 and data["total"] == 101
    assert data["next_after_version"] == "0100"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"blueprint_id": 0},
        {"blueprint_id": -3},
        {"blueprint_id": True},
        {"blueprint_id": "1"},
        {"blueprint_id": 1, "limit": 0},
        {"blueprint_id": 1, "limit": 201},
        {"blueprint_id": 1, "limit": True},
        {"blueprint_id": 1, "limit": "10"},
        {"blueprint_id": 1, "after_version": "abc"},
        {"blueprint_id": 1, "after_version": ""},
        {"blueprint_id": 1, "after_version": "12345678901"},
        {"blueprint_id": 1, "after_version": "0001\n"},
        {"blueprint_id": 1, "after_version": 1},
    ],
)
def test_an_argument_outside_the_published_schema_is_an_invalid_argument(
    client, admin_client, mcp_on, arguments
):
    _, token = _escenario(admin_client)

    error = _error(_llamar(client, token, "list_blueprint_migrations", arguments))

    assert error["code"] == "mcp.invalid_argument"


# --------------------------------------------------------------------------- #
# Alcance por proyecto: no encontrado indistinguible                           #
# --------------------------------------------------------------------------- #


def test_shared_foreign_and_nonexistent_blueprints_answer_with_the_identical_not_found(
    client, admin_client, mcp_on, motor_falso  # noqa: F811
):
    """
    La igualdad se PRUEBA, no se supone: código y mensaje byte a byte, y mismo estado de la
    respuesta, para un blueprint compartido, uno ajeno y uno que no existe.
    """
    project_id, token = _escenario(admin_client)
    other_project_id = _proyecto(admin_client, nombre="Ajeno")
    shared_id = _blueprint("compartido", project_ids=(project_id, other_project_id))
    foreign_id = _blueprint("ajeno", project_ids=(other_project_id,))
    nonexistent_id = 99999
    _migration(shared_id, "0001")
    _migration(foreign_id, "0001")

    responses = [
        _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id})
        for blueprint_id in (shared_id, foreign_id, nonexistent_id)
    ]

    errors = [_error(response) for response in responses]
    assert errors[0]["code"] == "mcp.not_found"
    assert errors[0] == errors[1] == errors[2]
    assert responses[0].json()["result"] == responses[1].json()["result"]
    assert responses[1].json()["result"] == responses[2].json()["result"]
    assert motor_falso.abiertas == []


def test_an_unlinked_blueprint_is_not_found_too(client, admin_client, mcp_on):
    """Un blueprint sin ningún vínculo a proyecto no es de nadie: tampoco se ve."""
    _, token = _escenario(admin_client)
    orphan_id = _blueprint("huerfano", project_ids=())

    error = _error(_llamar(client, token, "list_blueprint_migrations", {"blueprint_id": orphan_id}))

    assert error["code"] == "mcp.not_found"


# --------------------------------------------------------------------------- #
# Subcadenas prohibidas                                                        #
# --------------------------------------------------------------------------- #


def test_no_response_contains_a_forbidden_substring(client, admin_client, mcp_on):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,), description="Esquema base")
    _migration(blueprint_id, "0001", has_non_portable=True, source_engine="mysql")

    responses = {
        "list_blueprints": _llamar(client, token, "list_blueprints"),
        "list_blueprint_migrations": _llamar(
            client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id}
        ),
        "not_found": _llamar(client, token, "list_blueprint_migrations", {"blueprint_id": 4040}),
    }

    for label, response in responses.items():
        raw = json.dumps(response.json()["result"]).lower()
        for forbidden in PROHIBIDAS:
            assert forbidden not in raw, (label, forbidden)


def test_no_blueprint_output_field_name_contains_a_forbidden_substring():
    models = (
        out.BlueprintOut,
        out.BlueprintListOut,
        out.BlueprintRefOut,
        out.BlueprintMigrationOut,
        out.BlueprintMigrationListOut,
        out.BlueprintMigrationSqlOut,
        out.BlueprintEnvelope,
    )

    offending = [
        f"{model.__name__}.{field_name}"
        for model in models
        for field_name in model.model_fields
        if any(forbidden in field_name for forbidden in PROHIBIDAS)
    ]

    assert offending == []


def test_the_notice_and_the_tool_descriptions_contain_no_forbidden_substring():
    from app.mcp import registry

    texts = [out.BLUEPRINT_UNTRUSTED_NOTICE] + [
        registry.BY_NAME[name].description
        for name in ("list_blueprints", "list_blueprint_migrations")
    ]

    for text in texts:
        for forbidden in ("host", "port"):
            assert forbidden not in text.lower(), (text, forbidden)


# --------------------------------------------------------------------------- #
# Sin motor y sin escritura                                                    #
# --------------------------------------------------------------------------- #


def test_the_list_tools_open_no_engine_connection_and_mutate_nothing(
    client, admin_client, mcp_on, motor_falso  # noqa: F811
):
    project_id, token = _escenario(admin_client)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001")

    _ok(_llamar(client, token, "list_blueprints"))
    _ok(_llamar(client, token, "list_blueprint_migrations", {"blueprint_id": blueprint_id}))

    assert motor_falso.abiertas == []
    session = Database().get_declarative_base_session()
    try:
        assert session.query(DatabaseModel).count() == 1
        assert session.query(ModelMigration).count() == 1
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# El helper de exclusividad                                                    #
# --------------------------------------------------------------------------- #


def test_the_exclusivity_helper_keeps_only_blueprints_linked_to_exactly_one_project(admin_client):
    from app.controllers.target_resolution import _exclusive_model_ids

    first_project_id = _proyecto(admin_client, nombre="Uno")
    second_project_id = _proyecto(admin_client, nombre="Dos")
    exclusive_id = _blueprint("exclusivo", project_ids=(first_project_id,))
    _blueprint("compartido", project_ids=(first_project_id, second_project_id))
    _blueprint("huerfano", project_ids=())

    session = Database().get_declarative_base_session()
    try:
        exclusive_ids = _exclusive_model_ids(session)
        found = [row[0] for row in session.query(exclusive_ids.c.model_id).all()]
    finally:
        session.close()

    assert found == [exclusive_id]


# =========================================================================== #
# get_blueprint_migration (scope data.blueprint_sql + kill switch)             #
# =========================================================================== #

SQL_TOOL = "get_blueprint_migration"
SQL_SCOPES = "blueprints.read,data.blueprint_sql"
LISTS_ONLY_SCOPES = "blueprints.read"
SQL_AUDIT_ACTION = "mcp.get_blueprint_migration"
SQL_FIELDS = {
    "blueprint",
    "version",
    "name",
    "kind",
    "is_baseline",
    "reviewed",
    "source_engine",
    "has_procedural_objects",
    "checksum",
    "up_sql",
    "down_sql",
    "down_sql_suggested",
    "sql_bytes",
    "redactions",
    "created_at",
}
SECRET_LITERAL = "S3cretoDeLaMigracion"


def _install_registry(monkeypatch, *, blueprint_sql_enabled: bool) -> tuple:
    """
    Reemplaza el registro como si el kill switch hubiera estado en ese estado AL IMPORTAR (el
    registro se evalúa una sola vez al arrancar). ``BY_NAME`` es el mismo dict que importa el
    despachador, así que ``setitem`` es lo que lo alcanza.
    """
    tools = registry._build(blueprint_sql_enabled=blueprint_sql_enabled)
    monkeypatch.setattr(registry, "TOOLS", tools)
    for tool in tools:
        monkeypatch.setitem(registry.BY_NAME, tool.name, tool)
    return tools


@pytest.fixture()
def sql_registry(monkeypatch):
    return _install_registry(monkeypatch, blueprint_sql_enabled=True)


def _sql_actor(project_id: int, scopes: str):
    """Actor de token cuyo emisor es owner con step-up abierto (el scope exige las dos cosas)."""
    return token_actor(
        token_pk=1,
        token_id="t",
        name="agente",
        scopes=scopes,
        project_id=project_id,
        issuer=admin_actor(
            user_id=1, username="o", role=GatewayRole.OWNER, step_up_until=OPEN_WINDOW
        ),
    )


def _sql_scenario(admin_client, monkeypatch, *, scopes: str = SQL_SCOPES, switch: bool = True):
    """
    Un proyecto y un actor de token acotado a él. El actor se arma con el switch ENCENDIDO (apagado,
    el scope sería inerte y el despachador negaría por scope) y recién después se fija el estado a
    medir: así se prueba "se apagó con el token ya emitido".
    """
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", True)
    project_id = _proyecto(admin_client)
    actor = _sql_actor(project_id, scopes)
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", switch)
    return actor, project_id


def _dispatch(actor, tool: str, arguments: dict | None = None) -> dict:
    response = dispatch.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
        actor,
        {},
    )
    assert response.status == 200, response.body
    return response.body


def _sql_call(actor, blueprint_id: int, version: str) -> dict:
    return _dispatch(actor, SQL_TOOL, {"blueprint_id": blueprint_id, "version": version})


def _sql_ok(body: dict) -> dict:
    assert "result" in body, f"error JSON-RPC: {body.get('error')}"
    assert body["result"]["isError"] is False, body
    return body["result"]["structuredContent"]


def _sql_error(body: dict) -> dict:
    assert "result" in body, f"error JSON-RPC: {body.get('error')}"
    assert body["result"]["isError"] is True, body
    return body["result"]["structuredContent"]["error"]


def _intent_rows() -> list[tuple]:
    """
    Filas que escribe ``read_blueprint_migration`` (``target_type=database_model``). La fila del
    despachador comparte la acción pero cuelga de ``api_token``: se excluye.
    """
    session = Database().get_declarative_base_session()
    try:
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.action == SQL_AUDIT_ACTION, AuditLog.target_type == "database_model")
            .order_by(AuditLog.id)
        )
        return [(row.status, row.touched_engine, row.target_id, row.detail or "") for row in rows]
    finally:
        session.close()


def _dispatcher_rows() -> list[tuple]:
    session = Database().get_declarative_base_session()
    try:
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.action == SQL_AUDIT_ACTION, AuditLog.target_type == "api_token")
            .order_by(AuditLog.id)
        )
        return [(row.status, row.touched_engine, row.detail or "") for row in rows]
    finally:
        session.close()


def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys |= _all_keys(nested)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# --------------------------------------------------------------------------- #
# Registro y kill switch                                                       #
# --------------------------------------------------------------------------- #


def test_the_sql_tool_is_registered_only_with_the_kill_switch_on():
    switched_off = {tool.name for tool in registry._build(blueprint_sql_enabled=False)}
    switched_on = {tool.name for tool in registry._build(blueprint_sql_enabled=True)}

    assert SQL_TOOL not in switched_off
    assert switched_on - switched_off == {SQL_TOOL}
    assert {"list_blueprints", "list_blueprint_migrations"} <= switched_off


def test_the_real_registry_follows_the_setting_loaded_at_import():
    registered = {tool.name for tool in registry.TOOLS}

    assert (SQL_TOOL in registered) == bool(environments.MCP_BLUEPRINT_SQL_ENABLED)


def test_the_sql_tool_spec_is_a_closed_read_only_metadata_data_tool(sql_registry):
    spec = registry.BY_NAME[SQL_TOOL]

    assert spec.scope == "data.blueprint_sql"
    assert spec.touches_engine is False
    assert "data" in spec.tags
    assert spec.annotations["readOnlyHint"] is True
    assert spec.annotations["destructiveHint"] is False
    assert spec.input_schema["additionalProperties"] is False
    assert spec.input_schema["required"] == ["blueprint_id", "version"]
    properties = spec.input_schema["properties"]
    assert set(properties) == {"blueprint_id", "version"}
    assert properties["blueprint_id"]["minimum"] == 1
    assert properties["version"]["pattern"] == "^[0-9]{1,10}$"
    description = spec.description.lower()
    assert "no confiable" in description and "terceros" in description
    for forbidden in ("host", "port"):
        assert forbidden not in description


def test_the_registry_with_the_sql_tool_satisfies_every_invariant(sql_registry):
    registry._assert_invariants(sql_registry)


def test_a_token_with_blueprints_read_only_does_not_see_the_sql_tool(
    admin_client, monkeypatch, sql_registry
):
    list_tools = {"list_blueprints", "list_blueprint_migrations"}
    reader, _ = _sql_scenario(admin_client, monkeypatch, scopes=LISTS_ONLY_SCOPES)
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", True)
    privileged = _sql_actor(reader.project_id, SQL_SCOPES)

    reader_tool_names = {tool.name for tool in registry.tools_for(reader)}
    privileged_tool_names = {tool.name for tool in registry.tools_for(privileged)}

    assert list_tools <= reader_tool_names
    assert SQL_TOOL not in reader_tool_names
    assert list_tools | {SQL_TOOL} <= privileged_tool_names


def test_with_the_kill_switch_off_the_tool_is_absent_even_for_a_token_with_the_scope(
    admin_client, monkeypatch
):
    actor, _ = _sql_scenario(admin_client, monkeypatch)
    _install_registry(monkeypatch, blueprint_sql_enabled=False)

    assert SQL_TOOL not in {tool.name for tool in registry.tools_for(actor)}
    monkeypatch.delitem(registry.BY_NAME, SQL_TOOL, raising=False)
    body = _dispatch(actor, SQL_TOOL, {"blueprint_id": 1, "version": "0001"})
    assert body["error"]["code"] == -32602


def test_an_undeclared_argument_is_rejected_as_invalid_input_by_the_sql_tool(
    admin_client, monkeypatch, sql_registry
):
    actor, _ = _sql_scenario(admin_client, monkeypatch)

    body = _dispatch(
        actor, SQL_TOOL, {"blueprint_id": 1, "version": "0001", "up_sql_mysql": True}
    )

    assert body["error"]["code"] == -32602


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"version": "0001"},
        {"blueprint_id": 1},
        {"blueprint_id": 0, "version": "0001"},
        {"blueprint_id": True, "version": "0001"},
        {"blueprint_id": "1", "version": "0001"},
        {"blueprint_id": 1, "version": "abc"},
        {"blueprint_id": 1, "version": ""},
        {"blueprint_id": 1, "version": "12345678901"},
        {"blueprint_id": 1, "version": "0001\n"},
        {"blueprint_id": 1, "version": 1},
    ],
)
def test_an_argument_outside_the_published_schema_is_an_invalid_argument_for_the_sql_tool(
    admin_client, monkeypatch, sql_registry, arguments
):
    actor, _ = _sql_scenario(admin_client, monkeypatch)

    error = _sql_error(_dispatch(actor, SQL_TOOL, arguments))

    assert error["code"] == "mcp.invalid_argument"


def test_a_token_with_blueprints_read_only_is_denied_the_sql_tool_and_gets_no_sql(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch, scopes=LISTS_ONLY_SCOPES)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001")

    body = _sql_call(actor, blueprint_id, "0001")

    assert _sql_error(body)["code"] == "mcp.scope_denied"
    assert "sentinel_up_sql_marker" not in json.dumps(body)
    assert _intent_rows() == []


def test_with_the_switch_off_at_call_time_the_answer_is_403_and_nothing_is_read_or_audited(
    admin_client, monkeypatch, sql_registry, motor_falso  # noqa: F811
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch, switch=False)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001")

    body = _sql_call(actor, blueprint_id, "0001")

    error = _sql_error(body)
    assert error["code"] == "mcp.blueprint_sql_disabled"
    assert "sentinel_up_sql_marker" not in json.dumps(body)
    assert motor_falso.abiertas == []
    assert _intent_rows() == []


def test_the_kill_switch_wins_over_argument_validation(admin_client, monkeypatch, sql_registry):
    """Apagado, ni siquiera se validan los argumentos: la respuesta es la misma 403."""
    actor, _ = _sql_scenario(admin_client, monkeypatch, switch=False)

    for arguments in ({"blueprint_id": "no-es-entero", "version": "x"}, {"blueprint_id": 1}):
        assert _sql_error(_dispatch(actor, SQL_TOOL, arguments))["code"] == (
            "mcp.blueprint_sql_disabled"
        )


def test_the_disabled_error_is_http_403_at_the_context_level(admin_client, monkeypatch):
    from app.mcp.context import ToolContext
    from app.services.capability_catalog import Capability

    actor, _ = _sql_scenario(admin_client, monkeypatch, switch=False)
    ctx = ToolContext(actor=actor, capability=Capability.DATA_BLUEPRINT_SQL)

    with pytest.raises(AppHttpException) as refused:
        ctx.get_blueprint_migration(1, "0001")

    assert refused.value.status_code == 403
    assert refused.value.public_context["code"] == "mcp.blueprint_sql_disabled"


# --------------------------------------------------------------------------- #
# Camino feliz                                                                 #
# --------------------------------------------------------------------------- #


def test_the_happy_path_returns_the_declared_fields_and_marks_the_sql_as_untrusted(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        is_baseline=True,
        reviewed=True,
        source_engine="mysql",
        has_non_portable=True,
        down_sql=DOWN_SQL_SENTINEL,
        down_sql_suggested="DROP TABLE sugerido_marker",
        up_sql_mysql="CREATE TABLE variante_mysql_marker (id INT)",
        up_sql_postgresql="CREATE TABLE variante_pg_marker (id INT)",
    )

    envelope = _sql_ok(_sql_call(actor, blueprint_id, "0001"))

    assert set(envelope) == {
        "notice",
        "data",
        "source",
        "untrusted_content",
        "untrusted_fields",
        "clipped_fields",
        "warnings",
        "generated_at",
    }
    assert envelope["source"] == "gateway_blueprint"
    assert "database" not in envelope
    assert envelope["untrusted_content"] is True
    assert envelope["notice"] == out.BLUEPRINT_UNTRUSTED_NOTICE
    assert envelope["warnings"] == []
    assert envelope["clipped_fields"] == []
    assert envelope["untrusted_fields"] == [
        "data.name",
        "data.up_sql",
        "data.down_sql",
        "data.down_sql_suggested",
    ]
    data = envelope["data"]
    assert set(data) == SQL_FIELDS
    assert data["blueprint"] == {
        "blueprint_id": blueprint_id,
        "slug": "core",
        "current_version": "0.0.0",
    }
    assert data["version"] == "0001"
    assert data["name"] == "Migración 0001"
    assert data["kind"] == "schema"
    assert data["is_baseline"] is True and data["reviewed"] is True
    assert data["source_engine"] == "mysql"
    assert data["has_procedural_objects"] is True
    assert data["checksum"] == CHECKSUM
    assert data["up_sql"] == SQL_SENTINEL
    assert data["down_sql"] == DOWN_SQL_SENTINEL
    assert data["down_sql_suggested"] == "DROP TABLE sugerido_marker"
    assert data["created_at"]
    assert data["redactions"] == []
    expected_sql_bytes = sum(
        len(text.encode("utf-8"))
        for text in (SQL_SENTINEL, DOWN_SQL_SENTINEL, "DROP TABLE sugerido_marker")
    )
    assert data["sql_bytes"] == expected_sql_bytes


def test_the_response_has_no_engine_variants_no_translation_and_no_authorship(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        up_sql_mysql="CREATE TABLE variante_mysql_marker (id INT)",
        up_sql_postgresql="CREATE TABLE variante_pg_marker (id INT)",
    )

    body = _sql_call(actor, blueprint_id, "0001")

    raw = json.dumps(body["result"])
    for marker in ("variante_mysql_marker", "variante_pg_marker", AUTHOR_SENTINEL):
        assert marker not in raw, marker
    keys = _all_keys(body["result"]["structuredContent"])
    for forbidden_key in (
        "up_sql_mysql",
        "up_sql_postgresql",
        "translated",
        "created_by_username",
        "created_by_actor_type",
        "created_by_admin_id",
    ):
        assert forbidden_key not in keys, forbidden_key


def test_a_migration_without_rollback_succeeds_with_null_down_fields(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001", down_sql=None, down_sql_suggested=None)

    data = _sql_ok(_sql_call(actor, blueprint_id, "0001"))["data"]

    assert data["down_sql"] is None
    assert data["down_sql_suggested"] is None
    assert data["up_sql"] == SQL_SENTINEL
    assert data["checksum"] == CHECKSUM
    assert data["sql_bytes"] == len(SQL_SENTINEL.encode("utf-8"))


def test_a_data_migration_carries_the_seed_data_warning(admin_client, monkeypatch, sql_registry):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001", kind="data", up_sql="INSERT INTO t (a) VALUES (1)")
    _migration(blueprint_id, "0002", kind="schema")

    data_envelope = _sql_ok(_sql_call(actor, blueprint_id, "0001"))
    schema_envelope = _sql_ok(_sql_call(actor, blueprint_id, "0002"))

    assert [w["code"] for w in data_envelope["warnings"]] == ["mcp.warn.blueprint_data_migration"]
    assert data_envelope["data"]["kind"] == "data"
    assert schema_envelope["warnings"] == []


def test_the_migration_name_is_untrusted_free_text_and_the_sql_is_never_clipped(
    admin_client, monkeypatch, sql_registry
):
    from app.mcp.tools._envelope import FREE_TEXT_MAX_CHARS

    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    long_sql = "SELECT 1;\n" * (FREE_TEXT_MAX_CHARS * 2)
    _migration(blueprint_id, "0001", up_sql=long_sql, name="n" * 200)

    envelope = _sql_ok(_sql_call(actor, blueprint_id, "0001"))

    assert envelope["data"]["up_sql"] == long_sql
    assert envelope["clipped_fields"] == []
    assert "data.up_sql" in envelope["untrusted_fields"]


# --------------------------------------------------------------------------- #
# Alcance por proyecto                                                         #
# --------------------------------------------------------------------------- #


def test_shared_foreign_nonexistent_blueprints_and_unknown_versions_answer_identically(
    admin_client, monkeypatch, sql_registry
):
    """
    La igualdad se PRUEBA: código y mensaje byte a byte, y el mismo ``result`` completo, para un
    blueprint compartido, uno ajeno, uno inexistente y una versión que no existe. Además, el mismo
    error que devuelve ``list_blueprint_migrations`` para un blueprint que no ve.
    """
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    other_project_id = _proyecto(admin_client, nombre="Ajeno")
    visible_id = _blueprint("propio", project_ids=(project_id,))
    shared_id = _blueprint("compartido", project_ids=(project_id, other_project_id))
    foreign_id = _blueprint("ajeno", project_ids=(other_project_id,))
    nonexistent_id = 99999
    for blueprint_id in (visible_id, shared_id, foreign_id):
        _migration(blueprint_id, "0001")

    requests = [
        (shared_id, "0001"),
        (foreign_id, "0001"),
        (nonexistent_id, "0001"),
        (visible_id, "9999"),
    ]
    bodies = [_sql_call(actor, blueprint_id, version) for blueprint_id, version in requests]

    errors = [_sql_error(body) for body in bodies]
    assert errors[0]["code"] == "mcp.not_found"
    assert errors[0] == errors[1] == errors[2] == errors[3]
    assert [body["result"] for body in bodies].count(bodies[0]["result"]) == len(bodies)
    list_error = _sql_error(
        _dispatch(actor, "list_blueprint_migrations", {"blueprint_id": foreign_id})
    )
    assert list_error == errors[0]
    assert "sentinel_up_sql_marker" not in json.dumps(bodies)


def test_a_blueprint_with_no_project_link_is_not_found_for_the_sql_tool(
    admin_client, monkeypatch, sql_registry
):
    actor, _ = _sql_scenario(admin_client, monkeypatch)
    orphan_id = _blueprint("huerfano", project_ids=())
    _migration(orphan_id, "0001")

    assert _sql_error(_sql_call(actor, orphan_id, "0001"))["code"] == "mcp.not_found"


# --------------------------------------------------------------------------- #
# Redacción de credenciales                                                    #
# --------------------------------------------------------------------------- #


def test_credential_literals_are_redacted_in_every_sql_field_and_counted(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        up_sql=f"CREATE USER 'app' IDENTIFIED BY '{SECRET_LITERAL}';",
        down_sql=f"ALTER USER 'app' IDENTIFIED BY '{SECRET_LITERAL}';",
        down_sql_suggested="DROP USER 'app';",
    )

    body = _sql_call(actor, blueprint_id, "0001")

    envelope = _sql_ok(body)
    assert SECRET_LITERAL not in json.dumps(body)
    assert envelope["data"]["redactions"] == [{"category": "identified_by", "count": 2}]
    assert [w["code"] for w in envelope["warnings"]] == ["mcp.warn.blueprint_sql_redacted"]
    assert envelope["data"]["down_sql_suggested"] == "DROP USER 'app';"


def test_the_notice_and_the_warnings_contain_no_forbidden_substring(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        kind="data",
        up_sql=f"INSERT INTO t (c) VALUES ('x'); CREATE USER 'a' IDENTIFIED BY '{SECRET_LITERAL}';",
    )

    envelope = _sql_ok(_sql_call(actor, blueprint_id, "0001"))

    assert len(envelope["warnings"]) == 2
    texts = [envelope["notice"]] + [warning["message"] for warning in envelope["warnings"]]
    for text in texts:
        for forbidden in ("host", "port"):
            assert forbidden not in text.lower(), (text, forbidden)


def test_no_sql_response_key_contains_a_forbidden_substring(
    admin_client, monkeypatch, sql_registry
):
    """
    Solo las CLAVES: los valores son SQL de terceros y pueden contener cualquier palabra (``port``,
    ``host``) legítimamente.
    """
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        kind="data",
        has_non_portable=True,
        up_sql=f"INSERT INTO t (host, port) VALUES ('h', 1); CREATE USER 'a' IDENTIFIED BY '{SECRET_LITERAL}';",
        down_sql="DELETE FROM t WHERE host = 'h';",
    )

    success_keys = _all_keys(
        _sql_ok(_sql_call(actor, blueprint_id, "0001"))
    )
    error_keys = _all_keys(_sql_call(actor, 99999, "0001")["result"]["structuredContent"])

    for keys in (success_keys, error_keys):
        for key in keys:
            for forbidden in PROHIBIDAS:
                assert forbidden not in key.lower(), (key, forbidden)


def test_the_sql_dto_field_set_is_frozen_and_has_no_forbidden_substring_in_its_names():
    assert set(out.BlueprintMigrationSqlOut.model_fields) == SQL_FIELDS
    assert out.BlueprintMigrationSqlOut.model_config.get("extra") == "forbid"
    for field_name in out.BlueprintMigrationSqlOut.model_fields:
        for forbidden in PROHIBIDAS:
            assert forbidden not in field_name, (field_name, forbidden)
    for excluded in (
        "up_sql_mysql",
        "up_sql_postgresql",
        "translated",
        "created_by_username",
        "created_by_actor_type",
    ):
        assert excluded not in out.BlueprintMigrationSqlOut.model_fields


# --------------------------------------------------------------------------- #
# Tamaño: error con detalles, nunca recorte                                    #
# --------------------------------------------------------------------------- #


def test_an_oversized_up_sql_is_a_413_error_with_the_three_sizes_and_no_sql(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    # El JSON de la respuesta lleva el SQL dos veces (bloque de texto y ``structuredContent``), así
    # que ~300 KB de SQL ya superan el tope de 512 KiB.
    oversized_sql = "SELECT 1;\n" * 30_000
    _migration(blueprint_id, "0001", up_sql=oversized_sql)

    body = _sql_call(actor, blueprint_id, "0001")

    error = _sql_error(body)
    assert error["code"] == "mcp.blueprint_sql_too_large"
    assert set(error["details"]) == {"sql_bytes", "response_bytes", "max_response_bytes"}
    assert error["details"]["sql_bytes"] == len(oversized_sql.encode("utf-8"))
    assert error["details"]["max_response_bytes"] == dispatch.MAX_RESULT_BYTES
    assert error["details"]["response_bytes"] > error["details"]["max_response_bytes"]
    assert "SELECT 1" not in json.dumps(body)
    assert [status for status, _, _ in _dispatcher_rows()] == ["failure"]


def test_a_large_sql_that_fits_the_budget_is_returned_whole(
    admin_client, monkeypatch, sql_registry
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    large_sql = "SELECT 1;\n" * 20_000
    _migration(blueprint_id, "0001", up_sql=large_sql)

    body = _sql_call(actor, blueprint_id, "0001")

    envelope = _sql_ok(body)
    assert envelope["data"]["up_sql"] == large_sql
    assert result_budget.serialized_result_bytes(envelope) <= dispatch.MAX_RESULT_BYTES


def test_the_size_check_uses_the_same_formula_as_the_dispatcher():
    sample_result = {"data": {"up_sql": "SELECT 'ñandú';\n"}, "n": 3}
    dispatcher_serialization = json.dumps(
        jsonrpc.tool_result_payload(sample_result), ensure_ascii=False, default=str
    )

    assert result_budget.serialized_result_bytes(sample_result) == len(
        dispatcher_serialization.encode("utf-8")
    )
    assert dispatch.MAX_RESULT_BYTES == result_budget.MAX_RESULT_BYTES == 512 * 1024


# --------------------------------------------------------------------------- #
# tool_error_result y details                                                  #
# --------------------------------------------------------------------------- #


def test_tool_error_result_keeps_its_shape_without_details_and_adds_them_only_as_a_dict():
    without_details = jsonrpc.tool_error_result("mcp.x", "mensaje")
    assert without_details["structuredContent"] == {"error": {"code": "mcp.x", "message": "mensaje"}}

    with_details = jsonrpc.tool_error_result("mcp.x", "mensaje", details={"a": 1})
    assert with_details["structuredContent"]["error"]["details"] == {"a": 1}
    assert json.loads(with_details["content"][0]["text"]) == with_details["structuredContent"]
    assert with_details["isError"] is True

    for not_a_dict in ("texto", ["x"], 3, None):
        result = jsonrpc.tool_error_result("mcp.x", "mensaje", details=not_a_dict)
        assert "details" not in result["structuredContent"]["error"], not_a_dict


def test_the_dispatcher_forwards_details_only_when_it_is_a_dict(
    admin_client, monkeypatch, sql_registry
):
    actor, _ = _sql_scenario(admin_client, monkeypatch)
    original_spec = registry.BY_NAME[SQL_TOOL]
    arguments = {"blueprint_id": 1, "version": "0001"}

    for details, expected in (({"a": 1}, {"a": 1}), ("texto", None), (["x"], None), (None, None)):

        def _failing_handler(ctx, params, details=details):
            raise AppHttpException(
                message="falla",
                status_code=422,
                public_context={"code": "mcp.invalid_argument", "details": details},
            )

        monkeypatch.setitem(
            registry.BY_NAME, SQL_TOOL, dataclasses.replace(original_spec, handler=_failing_handler)
        )

        error = _sql_error(_dispatch(actor, SQL_TOOL, arguments))

        assert error.get("details") == expected, details


# --------------------------------------------------------------------------- #
# Auditoría fail-closed                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "failure",
    [
        AppHttpException(message="auditoría caída", status_code=500),
        RuntimeError("base de auditoría inalcanzable"),
    ],
)
def test_when_the_intent_audit_fails_the_answer_is_AUDIT_UNAVAILABLE_and_no_sql(
    admin_client, monkeypatch, sql_registry, failure
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001")

    def _fails(action, **kwargs):
        raise failure

    monkeypatch.setattr(audit_mod, "record_intent", _fails)

    body = _sql_call(actor, blueprint_id, "0001")

    assert _sql_error(body)["code"] == "AUDIT_UNAVAILABLE"
    raw = json.dumps(body)
    assert "sentinel_up_sql_marker" not in raw
    assert "inalcanzable" not in raw
    assert _intent_rows() == []


def test_the_intent_row_carries_the_token_and_version_and_never_a_body(
    admin_client, monkeypatch, sql_registry, motor_falso  # noqa: F811
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(blueprint_id, "0001", up_sql=f"CREATE USER 'a' IDENTIFIED BY '{SECRET_LITERAL}';")

    _sql_ok(_sql_call(actor, blueprint_id, "0001"))

    [(status, touched_engine, target_id, detail)] = _intent_rows()
    assert status == "attempt"
    assert touched_engine is False
    assert target_id == blueprint_id
    assert "version=0001" in detail and "token=t" in detail
    assert "CREATE" not in detail and SECRET_LITERAL not in detail
    assert [(status, touched) for status, touched, _ in _dispatcher_rows()] == [("success", False)]
    assert motor_falso.abiertas == []


# --------------------------------------------------------------------------- #
# Solo lectura                                                                 #
# --------------------------------------------------------------------------- #


def test_the_sql_tool_opens_no_engine_connection_and_mutates_nothing(
    admin_client, monkeypatch, sql_registry, motor_falso  # noqa: F811
):
    actor, project_id = _sql_scenario(admin_client, monkeypatch)
    blueprint_id = _blueprint("core", project_ids=(project_id,))
    _migration(
        blueprint_id,
        "0001",
        up_sql=f"CREATE USER 'a' IDENTIFIED BY '{SECRET_LITERAL}';",
        down_sql=DOWN_SQL_SENTINEL,
    )

    _sql_ok(_sql_call(actor, blueprint_id, "0001"))

    assert motor_falso.abiertas == []
    session = Database().get_declarative_base_session()
    try:
        assert session.query(DatabaseModel).count() == 1
        [stored] = session.query(ModelMigration).all()
        # La redacción es de la RESPUESTA: lo guardado no se toca.
        assert SECRET_LITERAL in stored.up_sql
        assert stored.down_sql == DOWN_SQL_SENTINEL
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# El scope no viaja por defecto                                                #
# --------------------------------------------------------------------------- #


def test_a_token_created_without_scopes_never_gets_the_sql_scope_even_with_the_switch_on(
    admin_client, monkeypatch
):
    """
    Se crea el token por la API real, sin ``scopes``: el default es ``blueprints.read`` y el
    scope del SQL no se arrastra aunque el switch esté encendido.
    """
    monkeypatch.setattr(environments, "MCP_BLUEPRINT_SQL_ENABLED", True)
    project_id = _proyecto(admin_client)

    created = _crear_token(admin_client, project_id=project_id)

    assert created["scopes"] == ["blueprints.read"]
