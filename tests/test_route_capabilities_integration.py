"""
Route coverage check 9: the integration API surface stays closed and consistent.

Runs the SAME function ``scripts/check_route_capabilities.py`` uses in CI (``integration_errors``),
never a copy, so the rule cannot be relaxed in one place only. Every negative case is exercised on
a synthetic app: the proof that the check detects something cannot depend on the real app being
broken.

Completeness is asserted over the twelve scopes of the catalog, rollback and stamp included.
"""

import importlib.util
import pathlib

import pytest
from fastapi import Depends, FastAPI

from app.core.authz import DatabasesDrop
from app.core.integration_auth import require_integration
from app.services.capability_catalog import Capability
from app.services.integration_scope_catalog import INTEGRATION_ALLOWED, IntegrationScope

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"

SERVERS_LIST_PATH = "/integration/servers"
UNRELATED_CAPABILITY = Capability.ACCESS_ADMIN_CAP


def _load_script():
    spec = importlib.util.spec_from_file_location("check_route_capabilities", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load_script()


def _only_servers_list_expected() -> frozenset[str]:
    return frozenset({IntegrationScope.SERVERS_LIST.value})


def _app_with_servers_list_route() -> FastAPI:
    synthetic_app = FastAPI()

    @synthetic_app.get(SERVERS_LIST_PATH)
    def list_servers(call=Depends(require_integration(IntegrationScope.SERVERS_LIST))):
        return {}

    return synthetic_app


# --- the real app ------------------------------------------------------------ #


def test_the_real_app_passes_check_9(guard):
    from main import app

    assert guard.integration_errors(app) == []


def test_every_scope_of_the_catalog_has_at_least_one_real_route(guard):
    from main import app

    scopes_with_a_route: set[str] = set()
    for path, route in guard._iter_routes(app):
        declared_scope = guard._integration_scope_of(route)
        if declared_scope is not None:
            scopes_with_a_route.add(declared_scope)

    assert scopes_with_a_route == {scope.value for scope in INTEGRATION_ALLOWED}


def test_integration_routes_are_not_step_up_exemptions_listed_by_hand(guard):
    """
    Check 7 stays untouched: the integration marker is the enumerable exemption, so none of these
    routes may need an entry in ``STEP_UP_EXEMPT`` (which is only for ``.../cancel`` routes).
    """
    from main import app

    assert guard.step_up_errors(app, exempt=guard.STEP_UP_EXEMPT) == []
    assert not any("/integration/" in path for _, path in guard.STEP_UP_EXEMPT)


# --- synthetic negatives ----------------------------------------------------- #


def test_a_consistent_integration_route_passes(guard):
    errors = guard.integration_errors(
        _app_with_servers_list_route(), expected_scopes=_only_servers_list_expected()
    )

    assert errors == []


def test_detects_an_integration_route_without_a_scope(guard):
    synthetic_app = FastAPI()

    @synthetic_app.get("/integration/naked")
    def naked_route():
        return {}

    errors = guard.integration_errors(synthetic_app, expected_scopes=frozenset())

    assert len(errors) == 1
    assert "/integration/naked" in errors[0]


def test_detects_a_scope_without_a_route(guard):
    errors = guard.integration_errors(FastAPI(), expected_scopes=_only_servers_list_expected())

    assert len(errors) == 1
    assert IntegrationScope.SERVERS_LIST.value in errors[0]


def test_detects_the_marker_outside_the_integration_prefix(guard):
    synthetic_app = FastAPI()

    @synthetic_app.get("/other/servers")
    def misplaced(call=Depends(require_integration(IntegrationScope.SERVERS_LIST))):
        return {}

    errors = guard.integration_errors(synthetic_app, expected_scopes=_only_servers_list_expected())

    assert any("/other/servers" in error for error in errors)


def test_detects_a_capability_that_differs_from_the_mapped_one(guard):
    synthetic_app = FastAPI()
    tampered_dependency = require_integration(IntegrationScope.SERVERS_LIST)
    tampered_dependency.__gw_capability__ = UNRELATED_CAPABILITY.value

    @synthetic_app.get(SERVERS_LIST_PATH)
    def list_servers(call=Depends(tampered_dependency)):
        return {}

    errors = guard.integration_errors(synthetic_app, expected_scopes=_only_servers_list_expected())

    assert any(UNRELATED_CAPABILITY.value in error for error in errors)


def test_detects_a_scope_outside_the_closed_vocabulary(guard):
    errors = guard.integration_errors(
        _app_with_servers_list_route(),
        allowed_capabilities={},
        expected_scopes=frozenset(),
    )

    assert any(IntegrationScope.SERVERS_LIST.value in error for error in errors)


def test_detects_a_session_guard_on_an_integration_route(guard):
    synthetic_app = FastAPI()

    @synthetic_app.get(SERVERS_LIST_PATH)
    def list_servers(
        session_actor: DatabasesDrop,
        call=Depends(require_integration(IntegrationScope.SERVERS_LIST)),
    ):
        return {}

    errors = guard.integration_errors(synthetic_app, expected_scopes=_only_servers_list_expected())

    assert any("sesión" in error or "session" in error.lower() for error in errors)


def test_a_session_only_route_under_the_prefix_is_reported_as_missing_its_scope(guard):
    synthetic_app = FastAPI()

    @synthetic_app.get("/integration/by-session")
    def by_session(session_actor: DatabasesDrop):
        return {}

    errors = guard.integration_errors(synthetic_app, expected_scopes=frozenset())

    assert any("/integration/by-session" in error for error in errors)
