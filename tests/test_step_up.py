"""
Step-up ("sudo mode"): las capacidades con ``requires_step_up`` exigen una contraseña fresca.

Ver ``app/core/step_up.py`` para el diseño: ventana por sesión, por tiempo, no deslizante; el
login la abre; ``POST /auth/step-up`` la renueva; 403 ``access.step_up_required`` cuando está
cerrada, siempre DESPUÉS de las capas 1 y 2 y antes de cualquier efecto.
"""

import importlib.util
import pathlib
import re
from datetime import timedelta

import pytest

from app.core import session_store, step_up
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    CAPABILITIES,
    CODE_FORBIDDEN,
    CODE_STEP_UP_REQUIRED,
    Capability,
    spec,
)
from tests.step_up_helpers import session_sid

STEP_UP = "/api/v1/auth/step-up"
ME = "/api/v1/auth/me"
# Una ruta con step-up (``policy.admin``, método no seguro) que no toca ningún motor. El cuerpo
# vacío da 422 si pasa el step-up y 403 si no: el step-up corre en la dependencia, antes del
# body.
SENSIBLE = "/api/v1/admin/crypto/rotate"


def _code(r) -> str | None:
    return ((r.json().get("detail") or {}).get("public_context") or {}).get("code")


def _pc(r) -> dict:
    return (r.json().get("detail") or {}).get("public_context") or {}


def _es_step_up(r) -> bool:
    return r.status_code == 403 and _code(r) == CODE_STEP_UP_REQUIRED


def _fila(sid: str):
    from app.models.gateway_session import GatewaySession

    s = session_store._session()
    try:
        return s.get(GatewaySession, sid)
    finally:
        s.close()


def _audit_count(action: str) -> int:
    from app.core.database import Database
    from app.models.audit_log import AuditLog

    s = Database().get_declarative_base_session()
    try:
        return s.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# La ventana                                                                   #
# --------------------------------------------------------------------------- #


def test_login_opens_the_window(admin_client):
    fila = _fila(session_sid(admin_client))
    assert fila.step_up_at is not None
    assert fila.step_up_at == fila.created_at
    assert fila.step_up_failures == 0

    me = admin_client.get(ME).json()["data"]
    assert me["step_up_enforced"] is True
    assert me["step_up_expires_at"] is not None
    assert not _es_step_up(admin_client.post(SENSIBLE, json={}))


def test_an_expired_window_demands_step_up(admin_client, expire_step_up):
    expire_step_up(admin_client)
    r = admin_client.post(SENSIBLE, json={})
    assert _es_step_up(r), r.text
    assert _pc(r)["step_up_ttl_seconds"] == step_up.STEP_UP_TTL_SECONDS


def test_step_up_reopens_the_window_without_rotating_the_sid(admin_client, expire_step_up):
    expire_step_up(admin_client)
    sid = session_sid(admin_client)

    r = admin_client.post(STEP_UP, json={"password": "admin123"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["step_up_expires_at"]
    assert data["step_up_ttl_seconds"] == step_up.STEP_UP_TTL_SECONDS

    assert session_sid(admin_client) == sid, "el step-up no debe rotar el sid"
    assert not _es_step_up(admin_client.post(SENSIBLE, json={}))
    assert admin_client.get(ME).json()["data"]["step_up_expires_at"] is not None
    assert _audit_count("auth.step_up") == 1


def test_the_window_expires_with_the_clock(admin_client, monkeypatch):
    """Con el reloj adelantado más allá del TTL, la misma sesión vuelve a pedir la contraseña."""
    real = session_store._utcnow
    monkeypatch.setattr(
        session_store,
        "_utcnow",
        lambda: real() + timedelta(seconds=step_up.STEP_UP_TTL_SECONDS + 5),
    )
    assert _es_step_up(admin_client.post(SENSIBLE, json={}))


def test_the_window_does_not_slide_with_use(admin_client):
    """Usarla no la estira: ``step_up_at`` queda donde lo dejó el login."""
    sid = session_sid(admin_client)
    antes = _fila(sid).step_up_at
    admin_client.post(SENSIBLE, json={})
    admin_client.get("/api/v1/gateway-users")
    assert _fila(sid).step_up_at == antes


# --------------------------------------------------------------------------- #
# Fallos                                                                       #
# --------------------------------------------------------------------------- #


def test_wrong_password_is_400_not_401_and_audited(admin_client):
    r = admin_client.post(STEP_UP, json={"password": "mala"})
    assert r.status_code == 400, r.text
    assert _code(r) == "auth.step_up_failed"
    assert _pc(r)["attempts_remaining"] == session_store.STEP_UP_MAX_FAILURES - 1
    assert _fila(session_sid(admin_client)).step_up_failures == 1
    assert _audit_count("auth.step_up_failed") == 1
    # La sesión sigue viva.
    assert admin_client.get(ME).status_code == 200


def test_a_success_resets_the_failure_counter(admin_client):
    admin_client.post(STEP_UP, json={"password": "mala"})
    admin_client.post(STEP_UP, json={"password": "mala"})
    assert admin_client.post(STEP_UP, json={"password": "admin123"}).status_code == 200
    assert _fila(session_sid(admin_client)).step_up_failures == 0


def test_five_consecutive_failures_revoke_the_session(admin_client):
    sid = session_sid(admin_client)
    for _ in range(session_store.STEP_UP_MAX_FAILURES - 1):
        assert admin_client.post(STEP_UP, json={"password": "mala"}).status_code == 400
    # La copia de la cookie que se quedó quien la robó.
    copia = admin_client.cookies.get("gw_session")

    r = admin_client.post(STEP_UP, json={"password": "mala"})
    assert r.status_code == 401, r.text
    assert _code(r) == "auth.session_step_up_failed"
    fila = _fila(sid)
    assert fila.revoked_at is not None
    assert fila.revoked_reason == session_store.REASON_STEP_UP_FAILED

    admin_client.cookies.clear()
    admin_client.cookies.set("gw_session", copia)
    r = admin_client.get(ME)
    assert r.status_code == 401
    assert _code(r) == "auth.session_step_up_failed"


def test_step_up_is_rate_limited(admin_client):
    from app.core.limiter import limiter

    limiter.enabled = True
    limiter.reset()
    try:
        for _ in range(5):
            assert admin_client.post(STEP_UP, json={"password": "admin123"}).status_code == 200
        assert admin_client.post(STEP_UP, json={"password": "admin123"}).status_code == 429
    finally:
        limiter.enabled = False
        limiter.reset()


def test_step_up_requires_a_session_and_csrf(client, admin_client):
    from app.core.csrf import CSRF_HEADER

    r = admin_client.post(STEP_UP, json={"password": "admin123"}, headers={CSRF_HEADER: ""})
    assert r.status_code == 403
    admin_client.cookies.clear()
    assert client.post(STEP_UP, json={"password": "admin123"}).status_code == 401


# --------------------------------------------------------------------------- #
# Cobertura: TODA ruta cuya capacidad declarada exige step-up                  #
# --------------------------------------------------------------------------- #

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_route_capabilities.py"


def _guard():
    sp = importlib.util.spec_from_file_location("check_route_capabilities", _SCRIPT)
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    return mod


def _step_up_routes() -> list[tuple[str, str, str]]:
    from main import app

    guard = _guard()
    out = []
    for path, route in guard._iter_routes(app):
        cap = guard._capability_of(route)
        if not cap or not spec(Capability(cap)).requires_step_up:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path, cap))
    return sorted(out)


_ROUTES = _step_up_routes()
_EXEMPT = _guard().STEP_UP_EXEMPT


def test_there_are_step_up_routes_to_check():
    """Si el enumerador devolviera vacío, el parametrizado de abajo pasaría sin probar nada."""
    assert len(_ROUTES) > 20
    assert {cap for _, _, cap in _ROUTES} >= {
        "databases.drop",
        "exports.download",
        "access.admin",
        "policy.admin",
        "sql_console.execute",
    }


def _concreta(path: str) -> str:
    # Parámetros de path con un valor que pasa la validación de tipos (int o str).
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.mark.parametrize(("method", "path", "cap"), _ROUTES, ids=lambda v: str(v))
def test_every_step_up_route_is_enforced(admin_client, expire_step_up, method, path, cap):
    expire_step_up(admin_client)
    url = _concreta(path)
    kwargs = {} if method in ("GET", "DELETE") else {"json": {}}
    if method == "GET" and path.endswith("/download"):
        kwargs["params"] = {"ticket": "x"}
    r = admin_client.request(method, url, **kwargs)

    if (method, path) in _EXEMPT:
        # Las cancelaciones de la lista: pasan con la ventana vencida.
        assert not _es_step_up(r), f"{method} {path} es una cancelación eximida y pidió step-up"
    elif method not in ("GET", "HEAD", "OPTIONS") or spec(Capability(cap)).discloses:
        assert _es_step_up(r), (
            f"{method} {path} ({cap}) no exigió step-up: {r.status_code} {r.text[:200]}"
        )
    else:
        # GET de una capacidad que no divulga (listar usuarios, versiones de blueprint): no se
        # interrumpe a nadie por leer.
        assert not _es_step_up(r), f"{method} {path} ({cap}) pidió step-up en un GET que no divulga"


# --------------------------------------------------------------------------- #
# Exención de las cancelaciones                                                #
# --------------------------------------------------------------------------- #


def test_the_exempt_list_is_exactly_the_step_up_cancels():
    """
    STEP_UP_EXEMPT == todo ``POST .../cancel`` cuya capacidad exige step-up. Ni una
    cancelación prompteada ni una exención que no sea cancelar.
    """
    cancels = {(m, p) for m, p, _ in _ROUTES if m == "POST" and p.endswith("/cancel")}
    assert cancels == set(_EXEMPT)
    assert len(_EXEMPT) == 4
    assert all(motivo for motivo in _EXEMPT.values())


def test_the_registry_check_passes_on_the_real_app():
    from main import app

    assert _guard().step_up_errors(app, exempt=_EXEMPT) == []


def test_an_unlisted_opt_out_is_detected():
    """Un ``step_up=False`` nuevo sin entrada en STEP_UP_EXEMPT rompe el chequeo 7."""
    from fastapi import Depends, FastAPI

    from app.core.authz import require

    app = FastAPI()

    @app.post("/algo/execute")
    def ejecutar(actor=Depends(require(Capability.DATABASES_DROP, step_up=False))):
        return {}

    errores = _guard().step_up_errors(app, exempt={})
    assert any("no está en STEP_UP_EXEMPT" in e for e in errores)
    assert any("no es un POST .../cancel" in e for e in errores)


def test_cancel_still_runs_the_capability_layers(admin_client, expire_step_up):
    """Eximir del step-up NO exime de la capacidad: un viewer recibe access.forbidden."""
    from tests.test_api_gateway_users import _cliente_como, _crear

    viewer = _cliente_como(_crear(admin_client, "cancela", gateway_role="viewer"), "cancela")
    expire_step_up(viewer)
    for _, path in _EXEMPT:
        r = viewer.post(_concreta(path), json={})
        assert r.status_code == 403, (path, r.text)
        assert _code(r) == CODE_FORBIDDEN


# --------------------------------------------------------------------------- #
# Retry-safety, orden y apagado                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def job_listo(admin_client, monkeypatch):
    from tests.test_api_database_exports import (
        _execute,
        _install,
        _install_execution,
        _ready,
        _server,
    )

    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["-- x\n", "CREATE TABLE t (id int);\n"])
    sid = _server(admin_client, 3995)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    return job


def test_export_content_is_not_consumed_by_a_step_up_403(admin_client, expire_step_up, job_listo):
    """
    El 403 sale de la dependencia, ANTES de ``prepare_download``/``finish_delivery``: después del
    prompt, el reintento entrega el artefacto. Si el 403 llegara tarde, el artefacto de un solo
    uso estaría consumido y el reintento daría 404/409.
    """
    expire_step_up(admin_client)
    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/content")
    assert _es_step_up(r), r.text

    assert admin_client.post(STEP_UP, json={"password": "admin123"}).status_code == 200
    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/content")
    assert r.status_code == 200, r.text
    assert "CREATE TABLE" in r.text


def test_missing_capability_is_forbidden_not_step_up(admin_client, expire_step_up):
    """Capa 1 primero: nadie tipea su contraseña para enterarse después de que no podía."""
    from tests.test_api_gateway_users import _cliente_como, _crear

    viewer = _cliente_como(_crear(admin_client, "vic", gateway_role="viewer"), "vic")
    expire_step_up(viewer)
    r = viewer.post(SENSIBLE, json={})
    assert r.status_code == 403
    assert _code(r) == CODE_FORBIDDEN


def test_imperative_capture_check_is_enforced(admin_client, expire_step_up):
    """``capture_selects`` escala a ``blueprints.captures`` en el handler: el step-up lo cubre."""
    r = admin_client.post("/api/v1/database-models", json={"name": "SU", "slug": "su"})
    assert r.status_code == 201, r.text
    mid = r.json()["data"]["id"]
    expire_step_up(admin_client)

    base = {"version": "0001", "name": "m1", "up_sql": "SELECT 1;"}
    r = admin_client.post(
        f"/api/v1/database-models/{mid}/migrations", json={**base, "capture_selects": True}
    )
    assert _es_step_up(r), r.text
    # Sin la captura, ``blueprints.write`` no pide step-up.
    r = admin_client.post(f"/api/v1/database-models/{mid}/migrations", json=base)
    assert r.status_code == 201, r.text


def test_disabled_step_up_never_prompts(admin_client, expire_step_up, monkeypatch):
    monkeypatch.setattr(step_up, "STEP_UP_ENFORCED", False)
    expire_step_up(admin_client)
    assert not _es_step_up(admin_client.post(SENSIBLE, json={}))
    assert admin_client.get(ME).json()["data"]["step_up_enforced"] is False


# --------------------------------------------------------------------------- #
# Agentes                                                                      #
# --------------------------------------------------------------------------- #


def test_invariant_step_up_excludes_agent_allowed():
    """Invariante 11 del catálogo: ninguna capacidad con step-up está en el techo de agente."""
    for s in CAPABILITIES:
        assert not (s.requires_step_up and s.agent_allowed), s.id.value
    assert not any(spec(c).requires_step_up for c in AGENT_ALLOWED)


def _token(caps):
    from app.core.actor import Actor

    return Actor(
        kind="api_token",
        id=1,
        username="bot",
        capabilities=frozenset(caps),
        token_id="t",
        project_id=1,
    )


def test_agent_tokens_never_get_step_up_required():
    from app.core.scope import ScopeTarget, assert_at
    from app.exceptions import AppHttpException

    agente = _token(AGENT_ALLOWED)
    for cap in AGENT_ALLOWED:
        step_up.assert_step_up(agente, cap, method="POST")  # no levanta

    # Defensivo: un token que llegara a una capacidad con step-up recibe access.forbidden.
    forzado = _token({Capability.DATABASES_DROP})
    with pytest.raises(AppHttpException) as exc:
        assert_at(forzado, Capability.DATABASES_DROP, ScopeTarget("database", (1,)))
    assert exc.value.status_code == 403
    assert exc.value.public_context["code"] == CODE_FORBIDDEN


def test_unknown_method_fails_closed():
    """Fuera de un request (sin método) el step-up se exige: fail-closed."""
    from app.core.actor import admin_actor
    from app.exceptions import AppHttpException
    from app.services.capability_catalog import GatewayRole

    actor = admin_actor(user_id=1, username="a", role=GatewayRole.OWNER)
    with pytest.raises(AppHttpException) as exc:
        step_up.assert_step_up(actor, Capability.BLUEPRINTS_APPLY, method=None)
    assert exc.value.public_context["code"] == CODE_STEP_UP_REQUIRED
