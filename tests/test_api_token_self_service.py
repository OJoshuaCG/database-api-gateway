"""
Autoservicio de tokens de agente: ``tokens.own`` administra SOLO los tokens que la persona emitió.

LO QUE SE FIJA
--------------
- Catálogo: ``tokens.own`` la tienen los tres roles (como ``self.read``), no muta, no divulga, no
  pide step-up, no es de agente, no es otorgable ni sensible, y ``access_admin`` sigue siendo
  EXACTAMENTE ``{access.admin}`` (invariante 10). Los invariantes se afirman al importar el
  catálogo; acá se fija que las propiedades se mantengan.
- Aislamiento por dueño (``created_by_admin_id``) en listar, crear, editar y revocar. Un token ajeno
  responde el MISMO 404 que uno inexistente (sin 403 ni 409 que confirmen que existe).
- ``access.admin`` ve y administra todo, igual que antes.
- Lo que protegía a los tokens sigue: step-up en métodos no seguros, techo del emisor, scopes de
  datos solo de ``owner``, proyecto visible para el emisor, auditoría con quién actuó y de quién es.
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import pytest

from app.controllers import api_token_controller as atc
from app.core.actor import Actor
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.api_token import ApiToken
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
from tests.access_request_helpers import client_as, create_user
from tests.step_up_helpers import OPEN_WINDOW
from tests.test_mcp_server import _proyecto

TOKENS = "/api/v1/api-tokens"
DATA_SCOPES = ["blueprints.read", "databases.read", "data.read"]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _person(admin_client, username: str, **extra):
    """``(client, user_id)`` de una cuenta SIN ``access.admin``, ya con su invitación aceptada."""
    datos = create_user(admin_client, username, **extra)
    return client_as(datos, username), datos["id"]


def _issue(client, project_id: int, **extra):
    payload = {"name": "repo-propio", "project_id": project_id, **extra}
    return client.post(TOKENS, json=payload)


def _load_route_guard():
    """El script de cobertura de rutas, cargado por ruta como hace ``test_route_capability_coverage``."""
    import importlib.util
    import pathlib

    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"
    spec = importlib.util.spec_from_file_location("check_route_capabilities", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code(response) -> str:
    return response.json()["detail"]["public_context"]["code"]


def _row(token_pk: int) -> ApiToken:
    session = Database().get_declarative_base_session()
    try:
        row = session.get(ApiToken, token_pk)
        session.expunge(row)
        return row
    finally:
        session.close()


def _audit_rows(action: str) -> list[AuditLog]:
    session = Database().get_declarative_base_session()
    try:
        return session.query(AuditLog).filter(AuditLog.action == action).all()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Catálogo                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(GatewayRole), ids=lambda r: r.value)
def test_every_role_holds_tokens_own(role):
    """Como ``self.read``: nadie necesita que se la asignen."""
    assert Capability.TOKENS_OWN in ROLE_CAPABILITIES[role]
    assert Capability.SELF_READ in ROLE_CAPABILITIES[role]


def test_tokens_own_has_the_flags_the_invariants_require():
    spec = cc.spec(Capability.TOKENS_OWN)

    assert (spec.module, spec.level) == ("tokens", "own")
    assert spec.mutates is False  # invariante 3: está en viewer
    assert spec.discloses is False  # solo los propios, nunca el secreto
    assert spec.requires_step_up is False  # el step-up lo pone la ruta con la spec de access.admin
    assert spec.agent_allowed is False
    assert spec.destructive is False
    assert spec.scope_axis == "global"


def test_tokens_own_is_outside_the_agent_ceiling_and_a_token_cannot_carry_it():
    assert Capability.TOKENS_OWN not in AGENT_ALLOWED
    assert cc.parse_scopes("tokens.own,blueprints.read") == frozenset({Capability.BLUEPRINTS_READ})
    assert cc.parse_stored_scopes("tokens.own") == frozenset()


def test_tokens_own_is_neither_grantable_nor_sensitive_nor_owner_only():
    assert cc.is_grantable(Capability.TOKENS_OWN) is False
    assert cc.is_sensitive(Capability.TOKENS_OWN) is False
    assert Capability.TOKENS_OWN not in cc.OWNER_ONLY_CAPABILITIES
    assert Capability.TOKENS_OWN not in cc.IMPLIED_READ


def test_access_admin_is_still_exactly_access_admin_and_no_global_holds_tokens_own():
    """Invariante 10: no se tocó el contenido de ``access_admin``."""
    assert GLOBAL_CAPABILITIES[GlobalCapability.ACCESS_ADMIN] == frozenset(
        {Capability.ACCESS_ADMIN_CAP}
    )
    for capabilities in GLOBAL_CAPABILITIES.values():
        assert Capability.TOKENS_OWN not in capabilities


def test_catalog_invariants_still_hold_with_the_new_capability():
    """Reafirma en un test lo que se afirma al importar: un relajamiento futuro fallaría acá."""
    cc._assert_invariants()


def test_the_published_matrix_lists_tokens_own_for_all_three_roles():
    row = next(r for r in cc.capability_matrix() if r["id"] == "tokens.own")
    assert row["roles"] == ["operator", "owner", "viewer"]
    assert row["global_capabilities"] == []
    assert row["grantable"] is False and row["sensitive"] is False


# --------------------------------------------------------------------------- #
# Guard de ruta                                                                #
# --------------------------------------------------------------------------- #


def test_api_token_routes_floor_at_access_admin_and_accept_tokens_own_as_alternative():
    from main import app

    guard = _load_route_guard()
    seen = 0
    for path, route in guard._iter_routes(app):
        if not path.startswith(TOKENS):
            continue
        assert guard._capability_of(route) == "access.admin", path
        assert guard._alternatives_of(route) == ("tokens.own",), path
        seen += 1
    assert seen == 4


# --------------------------------------------------------------------------- #
# Listar, crear                                                                #
# --------------------------------------------------------------------------- #


def test_a_person_without_access_admin_can_issue_and_the_token_records_them_as_issuer(
    admin_client,
):
    person, person_id = _person(admin_client, "ana-tokens")
    project_id = _proyecto(admin_client)

    response = _issue(person, project_id, scopes=["blueprints.read"])

    assert response.status_code == 201, response.text
    assert _row(response.json()["data"]["id"]).created_by_admin_id == person_id


def test_listing_is_filtered_by_owner_on_the_server_including_total_and_pagination(admin_client):
    ana, _ = _person(admin_client, "ana-lista")
    beto, _ = _person(admin_client, "beto-lista")
    project_id = _proyecto(admin_client)
    ana_tokens = [_issue(ana, project_id, name=f"ana-{i}").json()["data"]["id"] for i in range(3)]
    beto_token = _issue(beto, project_id, name="beto-0").json()["data"]["id"]

    first_page = ana.get(f"{TOKENS}?size=2&page=1").json()
    second_page = ana.get(f"{TOKENS}?size=2&page=2").json()

    listed = [t["id"] for t in first_page["data"]] + [t["id"] for t in second_page["data"]]
    assert sorted(listed) == sorted(ana_tokens)
    assert beto_token not in listed
    assert first_page["pagination"]["total"] == 3, "el total no puede incluir tokens ajenos"


def test_access_admin_lists_every_token(admin_client):
    ana, _ = _person(admin_client, "ana-todos")
    project_id = _proyecto(admin_client)
    ana_token = _issue(ana, project_id).json()["data"]["id"]
    admin_token = _issue(admin_client, project_id, name="del-admin").json()["data"]["id"]

    listed = {t["id"] for t in admin_client.get(f"{TOKENS}?size=50").json()["data"]}

    assert {ana_token, admin_token} <= listed


# --------------------------------------------------------------------------- #
# Editar, revocar: lo ajeno es un 404 indistinguible de lo inexistente         #
# --------------------------------------------------------------------------- #


def test_patching_someone_elses_token_is_the_same_404_as_a_missing_one(admin_client):
    ana, _ = _person(admin_client, "ana-patch")
    beto, _ = _person(admin_client, "beto-patch")
    project_id = _proyecto(admin_client)
    anas_token = _issue(ana, project_id, scopes=["blueprints.read"]).json()["data"]["id"]

    foreign = beto.patch(f"{TOKENS}/{anas_token}", json={"scopes": ["databases.read"]})
    missing = beto.patch(f"{TOKENS}/999999", json={"scopes": ["databases.read"]})

    assert foreign.status_code == missing.status_code == 404
    assert _code(foreign) == _code(missing) == "api_token.not_found"
    assert foreign.json() == missing.json(), "la respuesta no puede distinguir ajeno de inexistente"
    assert _row(anas_token).scopes == "blueprints.read"


def test_revoking_someone_elses_token_is_the_same_404_and_does_not_revoke_it(admin_client):
    ana, _ = _person(admin_client, "ana-revoke")
    beto, _ = _person(admin_client, "beto-revoke")
    project_id = _proyecto(admin_client)
    anas_token = _issue(ana, project_id).json()["data"]["id"]

    foreign = beto.delete(f"{TOKENS}/{anas_token}")
    missing = beto.delete(f"{TOKENS}/999999")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert _row(anas_token).revoked_at is None


def test_an_already_revoked_foreign_token_is_still_a_404_not_a_409(admin_client):
    """El 409 ``already_revoked`` confirmaría que el id existe: el ajeno tiene que ser 404 antes."""
    ana, _ = _person(admin_client, "ana-409")
    beto, _ = _person(admin_client, "beto-409")
    project_id = _proyecto(admin_client)
    anas_token = _issue(ana, project_id).json()["data"]["id"]
    assert ana.delete(f"{TOKENS}/{anas_token}").status_code == 200

    assert beto.delete(f"{TOKENS}/{anas_token}").status_code == 404
    assert beto.patch(f"{TOKENS}/{anas_token}", json={"scopes": ["blueprints.read"]}).status_code == 404
    # Y para la dueña sigue siendo el 409 de siempre.
    assert ana.delete(f"{TOKENS}/{anas_token}").status_code == 409


def test_a_person_can_edit_and_revoke_their_own_token(admin_client):
    ana, _ = _person(admin_client, "ana-propio")
    project_id = _proyecto(admin_client)
    token_pk = _issue(ana, project_id, scopes=["blueprints.read"]).json()["data"]["id"]

    edited = ana.patch(f"{TOKENS}/{token_pk}", json={"scopes": ["blueprints.read", "databases.read"]})
    revoked = ana.delete(f"{TOKENS}/{token_pk}")

    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["scopes"] == ["blueprints.read", "databases.read"]
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["active"] is False


def test_access_admin_still_edits_and_revokes_any_token(admin_client):
    ana, _ = _person(admin_client, "ana-admin")
    project_id = _proyecto(admin_client)
    token_pk = _issue(ana, project_id, scopes=["blueprints.read"]).json()["data"]["id"]

    edited = admin_client.patch(f"{TOKENS}/{token_pk}", json={"scopes": ["databases.read"]})
    revoked = admin_client.delete(f"{TOKENS}/{token_pk}")

    assert edited.status_code == 200, edited.text
    assert revoked.status_code == 200, revoked.text


# --------------------------------------------------------------------------- #
# El techo del emisor y los scopes de datos                                    #
# --------------------------------------------------------------------------- #


def test_a_viewer_cannot_issue_a_token_with_a_capability_it_does_not_hold(admin_client):
    """``data.read`` es solo de ``owner``: un viewer ni siquiera puede dejarlo escrito (inerte)."""
    viewer, _ = _person(admin_client, "viewer-techo")
    project_id = _proyecto(admin_client)

    response = _issue(viewer, project_id, scopes=DATA_SCOPES)

    assert response.status_code == 403, response.text
    assert _code(response) == "access.forbidden"
    assert viewer.get(TOKENS).json()["data"] == []


def test_a_viewer_cannot_add_a_data_scope_to_its_own_token(admin_client):
    viewer, _ = _person(admin_client, "viewer-agrega")
    project_id = _proyecto(admin_client)
    token_pk = _issue(viewer, project_id, scopes=["blueprints.read"]).json()["data"]["id"]

    response = viewer.patch(f"{TOKENS}/{token_pk}", json={"scopes": DATA_SCOPES})

    assert response.status_code == 403, response.text
    assert _code(response) == "access.forbidden"
    assert "data.read" not in _row(token_pk).scopes


def test_an_operator_cannot_exceed_its_role_but_an_owner_can_issue_data_scopes(
    admin_client, monkeypatch
):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)  # el tope de vida no es el tema
    project_id = _proyecto(admin_client)
    operator, _ = _person(admin_client, "operator-techo", gateway_role="operator")
    owner, _ = _person(admin_client, "owner-techo", gateway_role="owner")

    denied = _issue(operator, project_id, scopes=DATA_SCOPES)
    allowed = _issue(owner, project_id, scopes=DATA_SCOPES)

    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 201, allowed.text
    assert "data.read" in allowed.json()["data"]["scopes"]


def test_a_data_scope_still_asks_for_a_fresh_step_up_in_self_service(
    admin_client, expire_step_up, monkeypatch
):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 0)
    project_id = _proyecto(admin_client)
    owner, _ = _person(admin_client, "owner-stepup-datos", gateway_role="owner")
    expire_step_up(owner)

    response = _issue(owner, project_id, scopes=DATA_SCOPES)

    assert response.status_code == 403, response.text
    assert _code(response) == "access.step_up_required"


def test_the_data_token_ttl_cap_still_applies_in_self_service(admin_client, monkeypatch):
    monkeypatch.setattr(atc, "MCP_DATA_TOKEN_MAX_TTL_DAYS", 7)
    project_id = _proyecto(admin_client)
    owner, _ = _person(admin_client, "owner-ttl", gateway_role="owner")

    too_long = _issue(owner, project_id, scopes=DATA_SCOPES, expires_in_days=8)
    within_cap = _issue(owner, project_id, scopes=DATA_SCOPES, expires_in_days=7)

    assert too_long.status_code == 422, too_long.text
    assert _code(too_long) == "api_token.ttl_too_long"
    assert within_cap.status_code == 201, within_cap.text


def test_the_agent_ceiling_still_rejects_scopes_that_mutate_or_disclose(admin_client):
    viewer, _ = _person(admin_client, "viewer-agent-ceiling")
    project_id = _proyecto(admin_client)

    for forbidden in ("databases.drop", "access.admin", "tokens.own"):
        response = _issue(viewer, project_id, scopes=["blueprints.read", forbidden])
        assert response.status_code == 422, (forbidden, response.text)
        assert _code(response) == "api_token.scope_not_allowed"


# --------------------------------------------------------------------------- #
# Proyecto                                                                     #
# --------------------------------------------------------------------------- #


def test_an_unknown_project_is_still_a_422_in_self_service(admin_client):
    viewer, _ = _person(admin_client, "viewer-sin-proyecto")

    response = _issue(viewer, 999999)

    assert response.status_code == 422, response.text
    assert _code(response) == "project.not_found"


def test_an_issuer_who_cannot_see_projects_cannot_bind_a_token_to_one(admin_client):
    """
    No hay ACL de proyecto por usuario: el "acceso" es ``blueprints.read`` (lo que exige
    ``GET /projects/{id}``). Un actor de autoservicio sin esa capacidad recibe el MISMO 422 que un
    proyecto inexistente, y no se crea nada.
    """
    project_id = _proyecto(admin_client)
    issuer_without_blueprints_read = Actor(
        kind="admin",
        id=987654,
        username="sin-blueprints-read",
        # `databases.read` y no `blueprints.read`: el scope pedido tiene que estar dentro del techo
        # del emisor, o el 403 del techo taparía el 422 del proyecto que este test mide.
        capabilities=frozenset({Capability.TOKENS_OWN, Capability.DATABASES_READ}),
        step_up_until=OPEN_WINDOW,
    )

    with pytest.raises(AppHttpException) as exc:
        atc.ApiTokenController().create_token(
            {"name": "x", "project_id": project_id, "scopes": ["databases.read"]},
            admin=issuer_without_blueprints_read,
            owner_scope=issuer_without_blueprints_read.id,
        )

    assert exc.value.status_code == 422
    assert exc.value.public_context["code"] == "project.not_found"


# --------------------------------------------------------------------------- #
# Step-up                                                                      #
# --------------------------------------------------------------------------- #


def test_self_service_writes_need_a_fresh_step_up_but_listing_does_not(
    admin_client, expire_step_up
):
    """Mismo comportamiento que ``access.admin``: método no seguro => step-up; GET => no."""
    person, _ = _person(admin_client, "ana-stepup")
    project_id = _proyecto(admin_client)
    token_pk = _issue(person, project_id).json()["data"]["id"]
    expire_step_up(person)

    listing = person.get(TOKENS)
    create = _issue(person, project_id, name="otro")
    patch = person.patch(f"{TOKENS}/{token_pk}", json={"scopes": ["databases.read"]})
    revoke = person.delete(f"{TOKENS}/{token_pk}")

    assert listing.status_code == 200
    for response in (create, patch, revoke):
        assert response.status_code == 403, response.text
        assert _code(response) == "access.step_up_required"
    assert _row(token_pk).revoked_at is None


# --------------------------------------------------------------------------- #
# Auditoría                                                                    #
# --------------------------------------------------------------------------- #


def test_audit_rows_say_who_acted_and_whose_token_it_was(admin_client):
    ana, ana_id = _person(admin_client, "ana-audit")
    project_id = _proyecto(admin_client)
    token_pk = _issue(ana, project_id).json()["data"]["id"]

    assert admin_client.patch(f"{TOKENS}/{token_pk}", json={"scopes": ["databases.read"]}).status_code == 200
    assert admin_client.delete(f"{TOKENS}/{token_pk}").status_code == 200

    created = [r for r in _audit_rows("api_token.create") if r.target_id == token_pk][-1]
    updated = [r for r in _audit_rows("api_token.update") if r.target_id == token_pk][-1]
    revoked = [r for r in _audit_rows("api_token.revoke") if r.target_id == token_pk][-1]

    assert created.admin_id == ana_id
    assert f"emisor={ana_id}" in created.detail and "modo=propio" in created.detail
    for row in (updated, revoked):
        assert row.admin_id != ana_id, "actuó el administrador, no la dueña"
        assert f"emisor={ana_id}" in row.detail
        assert "modo=administrador" in row.detail


# --------------------------------------------------------------------------- #
# El filtro del controller falla cerrado                                       #
# --------------------------------------------------------------------------- #


def test_owner_scope_of_is_none_only_for_access_admin():
    admin = Actor(
        kind="admin",
        id=1,
        username="a",
        capabilities=frozenset({Capability.ACCESS_ADMIN_CAP, Capability.TOKENS_OWN}),
    )
    plain = Actor(
        kind="admin", id=2, username="b", capabilities=frozenset({Capability.TOKENS_OWN})
    )

    assert atc.owner_scope_of(admin) is None
    assert atc.owner_scope_of(plain) == 2


def test_owner_scope_of_without_a_resolvable_id_is_a_403_never_everything():
    nameless = Actor(
        kind="admin", id=None, username="c", capabilities=frozenset({Capability.TOKENS_OWN})
    )

    with pytest.raises(AppHttpException) as exc:
        atc.owner_scope_of(nameless)

    assert exc.value.status_code == 403
