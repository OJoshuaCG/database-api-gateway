"""
Cimientos de la autenticación de los tokens de integración (PR1 de ``integration-api-tokens``).

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
Un token de integración es una credencial bearer que MUTA bases de terceros, así que sus
cimientos se fijan antes de que exista una sola ruta: la configuración que lo acota (kill switch
apagado, TTL, cupos), el pepper que lo separa del MCP, el formato que el parseo nunca rompe, la
atribución en ``audit_log`` y el limitador propio. Los casos de la cadena completa de
autenticación (401 idénticos, 503, 429, etc.) se agregan en la fase 2 sobre este mismo archivo.

Los tests de configuración recargan ``app.core.environments`` (los guards corren al importar),
con la misma disciplina de restauración de ``test_startup_guards.py``.
"""

import importlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.core.actor import Actor, admin_actor, integration_actor
from app.core.crypto import (
    CryptoConfigError,
    api_token_pepper,
    integration_token_pepper,
)
from app.core.integration_token_format import (
    INTEGRATION_TOKEN_PREFIX,
    mint_integration_token,
    parse_integration_bearer,
)
from app.core.database import Database
from app.core.integration_auth import (
    IntegrationCall,
    integration_token_hmac,
    require_integration,
)
from app.core.scope_targets import database as database_target
from app.core.scope_targets import server as server_target
from app.exceptions import AppHttpException
from app.exceptions.HandlerExceptions import app_exception_handler, rate_limit_handler
from app.services.audit import audit_identity
from app.services.capability_catalog import Capability, GatewayRole
from app.services.integration_scope_catalog import (
    CODE_INTEGRATION_DISABLED,
    CODE_INTEGRATION_SCOPE_MISSING,
    CODE_INTEGRATION_SERVER_NOT_ALLOWED,
    CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED,
    CODE_INTEGRATION_TOKEN_INVALID,
    IntegrationScope,
    effective_integration_scopes,
    parse_stored_integration_scopes,
)
from tests.scope_helpers import env_id, sembrar_bd

# --------------------------------------------------------------------------- #
# 1.1 Configuración                                                           #
# --------------------------------------------------------------------------- #

_INTEGRATION_ENV_NAMES = (
    "INTEGRATION_API_ENABLED",
    "INTEGRATION_TOKEN_MAX_TTL_DAYS",
    "INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS",
    "INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS",
    "INTEGRATION_RATE_LIMIT",
    "INTEGRATION_WRITE_RATE_LIMIT",
    "INTEGRATION_DESTRUCTIVE_RATE_LIMIT",
    "INTEGRATION_AUTH_FAILURE_RATE_LIMIT",
)


def _reload_environments(monkeypatch, **env):
    """Recarga la config del proceso con las variables dadas. Devuelve el módulo."""
    import app.core.environments as environments_module

    for variable_name, variable_value in env.items():
        monkeypatch.setenv(variable_name, variable_value)
    return importlib.reload(environments_module)


@pytest.fixture()
def restore_environments():
    """
    Deja ``app.core.environments`` como estaba: lo importan ~40 módulos y, si un test lo dejara
    recargado con valores parcheados, los envenenaría en la misma corrida.
    """
    original_values = {name: os.environ.get(name) for name in _INTEGRATION_ENV_NAMES}
    yield
    for variable_name, original_value in original_values.items():
        if original_value is None:
            os.environ.pop(variable_name, None)
        else:
            os.environ[variable_name] = original_value
    import app.core.environments as environments_module

    importlib.reload(environments_module)


def test_integration_config_defaults_match_the_design(monkeypatch, restore_environments):
    for name in _INTEGRATION_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    environments_module = _reload_environments(monkeypatch)

    # El kill switch nace APAGADO: un default prendido expondría escrituras sobre bases ajenas
    # en un despliegue que nadie configuró.
    assert environments_module.INTEGRATION_API_ENABLED is False
    assert environments_module.INTEGRATION_TOKEN_MAX_TTL_DAYS == 90
    assert environments_module.INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS == 30
    assert environments_module.INTEGRATION_RATE_LIMIT == "120/minute"
    assert environments_module.INTEGRATION_WRITE_RATE_LIMIT == "20/minute"
    assert environments_module.INTEGRATION_AUTH_FAILURE_RATE_LIMIT == "30/minute"
    # Rollback y stamp: vida más corta y cupo más estrecho que cualquier otro tier (CR-1, D17/D22).
    assert environments_module.INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS == 7
    assert environments_module.INTEGRATION_DESTRUCTIVE_RATE_LIMIT == "5/minute"


def test_integration_api_enabled_only_with_explicit_true(monkeypatch, restore_environments):
    environments_module = _reload_environments(monkeypatch, INTEGRATION_API_ENABLED="true")
    assert environments_module.INTEGRATION_API_ENABLED is True


@pytest.mark.parametrize(
    "variable_name",
    [
        "INTEGRATION_TOKEN_MAX_TTL_DAYS",
        "INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS",
        "INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS",
    ],
)
@pytest.mark.parametrize("invalid_value", ["0", "-5", "abc"])
def test_integration_ttl_rejects_non_positive_or_non_integer(
    monkeypatch, restore_environments, variable_name, invalid_value
):
    with pytest.raises(ValueError, match=variable_name):
        _reload_environments(monkeypatch, **{variable_name: invalid_value})


@pytest.mark.parametrize(
    "variable_name",
    [
        "INTEGRATION_RATE_LIMIT",
        "INTEGRATION_WRITE_RATE_LIMIT",
        "INTEGRATION_DESTRUCTIVE_RATE_LIMIT",
        "INTEGRATION_AUTH_FAILURE_RATE_LIMIT",
    ],
)
@pytest.mark.parametrize("invalid_value", ["0/minute", "-1/minute", "lots", "10"])
def test_integration_rate_limits_reject_non_positive_or_malformed(
    monkeypatch, restore_environments, variable_name, invalid_value
):
    with pytest.raises(ValueError, match=variable_name):
        _reload_environments(monkeypatch, **{variable_name: invalid_value})


def test_integration_write_ttl_cannot_exceed_general_ttl(monkeypatch, restore_environments):
    # El token que puede escribir no puede vivir más que el de solo lectura.
    with pytest.raises(ValueError, match="INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS"):
        _reload_environments(
            monkeypatch,
            INTEGRATION_TOKEN_MAX_TTL_DAYS="10",
            INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS="11",
        )


def test_integration_write_ttl_equal_to_general_ttl_is_accepted(monkeypatch, restore_environments):
    environments_module = _reload_environments(
        monkeypatch,
        INTEGRATION_TOKEN_MAX_TTL_DAYS="30",
        INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS="30",
    )
    assert environments_module.INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS == 30


def test_integration_destructive_ttl_cannot_exceed_write_ttl(monkeypatch, restore_environments):
    # El token que puede revertir migraciones no puede vivir más que el que solo escribe.
    with pytest.raises(ValueError, match="INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS"):
        _reload_environments(
            monkeypatch,
            INTEGRATION_TOKEN_MAX_TTL_DAYS="90",
            INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS="10",
            INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS="11",
        )


def test_integration_destructive_ttl_equal_to_write_ttl_is_accepted(monkeypatch, restore_environments):
    environments_module = _reload_environments(
        monkeypatch,
        INTEGRATION_TOKEN_MAX_TTL_DAYS="90",
        INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS="10",
        INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS="10",
    )
    assert environments_module.INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS == 10


def test_lowering_the_write_ttl_below_the_default_destructive_ttl_fails_at_startup(
    monkeypatch, restore_environments
):
    # Con el default de 7 días para el tier destructivo, un write cap de 5 rompe el orden: hay que
    # bajar también el destructivo. Falla al arrancar y no en el primer token emitido.
    monkeypatch.delenv("INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS", raising=False)
    with pytest.raises(ValueError, match="INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS"):
        _reload_environments(monkeypatch, INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS="5")


# --------------------------------------------------------------------------- #
# 1.2 Pepper                                                                  #
# --------------------------------------------------------------------------- #


def test_integration_pepper_is_domain_separated_from_the_mcp_pepper():
    integration_pepper = integration_token_pepper()
    mcp_pepper = api_token_pepper()

    # Con el mismo `info`, un dump de una tabla de tokens verificaría tokens de la otra.
    assert integration_pepper != mcp_pepper
    assert len(integration_pepper) == 32
    assert isinstance(integration_pepper, bytes)


def test_integration_pepper_is_deterministic():
    assert integration_token_pepper() == integration_token_pepper()


def test_integration_pepper_fails_closed_without_secret_key(monkeypatch):
    import app.core.crypto as crypto_module

    monkeypatch.setattr(crypto_module, "SECRET_KEY", "")
    with pytest.raises(CryptoConfigError):
        crypto_module.integration_token_pepper()


# --------------------------------------------------------------------------- #
# 1.3 Formato del bearer                                                      #
# --------------------------------------------------------------------------- #


def test_minted_token_round_trips_through_parse():
    public_id, secret, bearer = mint_integration_token()

    assert bearer == f"{INTEGRATION_TOKEN_PREFIX}.{public_id}.{secret}"
    parsed = parse_integration_bearer(bearer)
    assert parsed is not None
    assert parsed.public_id == public_id
    assert parsed.secret == secret


def test_minted_public_id_fits_the_token_id_column():
    public_id, _, _ = mint_integration_token()
    # `integration_tokens.token_id` es String(24): un id más largo rompe el INSERT en runtime.
    assert len(public_id) == 24


def test_minted_tokens_are_unique():
    first_bearer = mint_integration_token()[2]
    second_bearer = mint_integration_token()[2]
    assert first_bearer != second_bearer


def test_integration_prefix_is_distinct_from_the_mcp_prefixes():
    from app.core.mcp_token_format import ACCEPTED_TOKEN_PREFIXES

    # Un bearer de integración jamás debe parsear como de agente (ni al revés).
    assert INTEGRATION_TOKEN_PREFIX == "datumint"
    assert INTEGRATION_TOKEN_PREFIX not in ACCEPTED_TOKEN_PREFIXES


@pytest.mark.parametrize(
    "malformed_bearer",
    [
        "",
        "datumint",
        "datumint.",
        "datumint..",
        "datumint.abc",
        "datumint.abc.",
        "datumint..secret",
        "datumint.abc.secret.extra",
        "datum.abc.secret",
        "dbgw.abc.secret",
        "DATUMINT.abc.secret",
        "datumint.abc def.secret",
        "datumint.abc.sec ret",
        "datumint." + "a" * 25 + ".secret",
        "datumint.abc." + "s" * 4096,
        "datumint.ab\x00c.secret",
        "datumint.ábc.secret",
    ],
)
def test_parse_returns_none_on_malformed_input(malformed_bearer):
    assert parse_integration_bearer(malformed_bearer) is None


@pytest.mark.parametrize("not_a_string", [None, 123, b"datumint.a.b", ["datumint", "a", "b"]])
def test_parse_never_raises_on_non_string_input(not_a_string):
    assert parse_integration_bearer(not_a_string) is None


# --------------------------------------------------------------------------- #
# 1.13 Atribución en audit_log                                                #
# --------------------------------------------------------------------------- #


def _issuer_actor() -> Actor:
    return admin_actor(user_id=7, username="issuer", role=GatewayRole.OWNER)


def test_integration_actor_audit_identity_attributes_the_issuer_and_the_token():
    issuer = _issuer_actor()
    actor = integration_actor(
        token_pk=42,
        public_id="PUBLICID0123456789abcd",
        name="ci-deploy",
        capabilities=frozenset({Capability.SERVERS_READ}),
        issuer=issuer,
    )

    admin_id, admin_username, api_token_id, integration_token_id = audit_identity(actor)

    # El emisor es el responsable humano de la acción: va en admin_id.
    assert admin_id == issuer.id
    # Prefijo que ningún username real imita sin que se note; solo la parte PÚBLICA del bearer.
    assert admin_username == "integration:PUBLICID0123456789abcd"
    # El PK del token de integración NO se mezcla con el del token de agente.
    assert api_token_id is None
    assert integration_token_id == 42


def test_mcp_agent_audit_identity_does_not_set_integration_token_id():
    from app.core.actor import token_actor

    actor = token_actor(
        token_pk=3, token_id="agenttoken", name="alice", scopes="", project_id=1, issuer=None
    )
    admin_id, admin_username, api_token_id, integration_token_id = audit_identity(actor)

    assert admin_id is None
    assert admin_username == "token:agenttoken"
    assert api_token_id == 3
    assert integration_token_id is None


def test_human_audit_identity_is_unchanged():
    admin_id, admin_username, api_token_id, integration_token_id = audit_identity(
        _issuer_actor()
    )
    assert (admin_id, admin_username, api_token_id, integration_token_id) == (
        7,
        "issuer",
        None,
        None,
    )


def test_build_persists_integration_identity_and_actor_type():
    from app.services.audit import _build

    actor = integration_actor(
        token_pk=42,
        public_id="PUBLICID0123456789abcd",
        name="ci-deploy",
        capabilities=frozenset({Capability.SERVERS_READ}),
        issuer=_issuer_actor(),
    )
    row = _build(
        "integration.call",
        status="success",
        admin=actor,
        target_type=None,
        target_id=None,
        server_id=None,
        touched_engine=False,
        detail=None,
        grantee=None,
        privilege=None,
        object_level=None,
        object_name=None,
        with_grant_option=None,
        grantor=None,
    )

    assert row.actor_type == "integration"
    assert row.admin_id == 7
    assert row.admin_username == "integration:PUBLICID0123456789abcd"
    assert row.integration_token_id == 42
    assert row.api_token_id is None


# --------------------------------------------------------------------------- #
# 1.14 Limitador propio                                                       #
# --------------------------------------------------------------------------- #


class _FakeRequest:
    """Lo mínimo que lee ``integration_token_key``: headers y cliente."""

    def __init__(self, authorization: str | None):
        self.headers = {"authorization": authorization} if authorization else {}
        self.client = type("Client", (), {"host": "203.0.113.9"})()


def test_integration_limiter_is_a_separate_instance_from_the_others():
    from app.core.limiter import integration_limiter, limiter, mcp_limiter

    assert integration_limiter is not limiter
    assert integration_limiter is not mcp_limiter


def test_integration_key_is_per_token_and_falls_back_to_the_ip():
    from app.core.limiter import integration_token_key

    keyed = integration_token_key(_FakeRequest("Bearer datumint.PUBLICID.secretvalue"))
    assert keyed == "integration:PUBLICID"

    anonymous = integration_token_key(_FakeRequest(None))
    assert anonymous.startswith("ip:")

    # Un bearer de AGENTE no debe ocupar un cupo de integración.
    mcp_bearer = integration_token_key(_FakeRequest("Bearer datum.PUBLICID.secretvalue"))
    assert mcp_bearer.startswith("ip:")


def test_integration_buckets_are_isolated_by_tier_and_by_token():
    from app.core.limiter import (
        INTEGRATION_BUCKET_BASE,
        INTEGRATION_BUCKET_DESTRUCTIVE,
        INTEGRATION_BUCKET_WRITE,
        hit_or_429,
        integration_limiter,
    )
    from slowapi.errors import RateLimitExceeded

    previously_enabled = integration_limiter.enabled
    integration_limiter.enabled = True
    try:
        one_per_minute = "1/minute"
        token_a = "isolation-token-a"
        token_b = "isolation-token-b"

        hit_or_429(integration_limiter, one_per_minute, "integration", INTEGRATION_BUCKET_BASE, token_a)
        with pytest.raises(RateLimitExceeded):
            hit_or_429(
                integration_limiter, one_per_minute, "integration", INTEGRATION_BUCKET_BASE, token_a
            )

        # Agotar el cupo base de A no toca el de escritura de A ni el base de B.
        hit_or_429(integration_limiter, one_per_minute, "integration", INTEGRATION_BUCKET_WRITE, token_a)
        hit_or_429(integration_limiter, one_per_minute, "integration", INTEGRATION_BUCKET_BASE, token_b)
        # El bucket destructivo (5.6(c): `hit_or_429` acepta cualquier clave) es independiente.
        hit_or_429(
            integration_limiter, one_per_minute, "integration", INTEGRATION_BUCKET_DESTRUCTIVE, token_a
        )
        with pytest.raises(RateLimitExceeded):
            hit_or_429(
                integration_limiter,
                one_per_minute,
                "integration",
                INTEGRATION_BUCKET_DESTRUCTIVE,
                token_a,
            )
        hit_or_429(
            integration_limiter, one_per_minute, "integration", INTEGRATION_BUCKET_DESTRUCTIVE, token_b
        )
    finally:
        integration_limiter.enabled = previously_enabled
        integration_limiter.reset()


# --------------------------------------------------------------------------- #
# 2.1 Cadena de autenticación: ``require_integration``                         #
# --------------------------------------------------------------------------- #
#
# Las rutas reales de /integration llegan en la fase 3, así que la dependencia se prueba sobre
# una app mínima con rutas "probe" (mismos handlers de excepción que la app real: el 401, el 429
# y su Retry-After salen por ellos). El esquema de la BD lo resetea la fixture ``client``.

PROBE_READ_PATH = "/probe/read"
PROBE_WRITE_PATH = "/probe/write"
PROBE_SERVER_PATH = "/probe/servers/{server_id}"
PROBE_DATABASE_READ_PATH = "/probe/databases/{db_id}/read"
PROBE_DATABASE_WRITE_PATH = "/probe/databases/{db_id}/write"
PROBE_DESTRUCTIVE_PATH = "/probe/destructive"
PROBE_SECOND_DESTRUCTIVE_PATH = "/probe/destructive-stamp"

GENEROUS_FAILURE_CAP = "1000/minute"
ISSUER_PASSWORD_PLACEHOLDER = "not-a-real-hash"


def _describe_call(call: IntegrationCall) -> dict[str, Any]:
    return {
        "scope": call.scope.value,
        "actor_kind": call.actor.kind,
        "actor_capabilities": sorted(capability.value for capability in call.actor.capabilities),
        "allowed_server_ids": sorted(call.allowed_server_ids),
        "allowed_blueprint_ids": sorted(call.allowed_blueprint_ids),
    }


def _build_probe_app() -> FastAPI:
    probe_app = FastAPI()
    probe_app.add_exception_handler(AppHttpException, app_exception_handler)
    probe_app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    @probe_app.get(PROBE_READ_PATH)
    def probe_read(
        call: IntegrationCall = Depends(require_integration(IntegrationScope.SERVERS_LIST)),
    ) -> dict[str, Any]:
        return _describe_call(call)

    @probe_app.post(PROBE_WRITE_PATH)
    def probe_write(
        call: IntegrationCall = Depends(require_integration(IntegrationScope.DATABASES_CREATE)),
    ) -> dict[str, Any]:
        return _describe_call(call)

    @probe_app.get(PROBE_SERVER_PATH)
    def probe_server(
        server_id: int,
        call: IntegrationCall = Depends(
            require_integration(IntegrationScope.DATABASES_LIST, target=server_target)
        ),
    ) -> dict[str, Any]:
        return _describe_call(call)

    @probe_app.get(PROBE_DATABASE_READ_PATH)
    def probe_database_read(
        db_id: int,
        call: IntegrationCall = Depends(
            require_integration(IntegrationScope.BLUEPRINT_READ_ASSIGNED, target=database_target)
        ),
    ) -> dict[str, Any]:
        return _describe_call(call)

    @probe_app.post(PROBE_DATABASE_WRITE_PATH)
    def probe_database_write(
        db_id: int,
        call: IntegrationCall = Depends(
            require_integration(IntegrationScope.DATABASES_ASSIGN_BLUEPRINT, target=database_target)
        ),
    ) -> dict[str, Any]:
        return _describe_call(call)

    @probe_app.post(PROBE_DESTRUCTIVE_PATH)
    def probe_destructive(
        call: IntegrationCall = Depends(require_integration(IntegrationScope.MIGRATIONS_ROLLBACK)),
    ) -> dict[str, Any]:
        return _describe_call(call)

    @probe_app.post(PROBE_SECOND_DESTRUCTIVE_PATH)
    def probe_second_destructive(
        call: IntegrationCall = Depends(require_integration(IntegrationScope.MIGRATIONS_STAMP)),
    ) -> dict[str, Any]:
        return _describe_call(call)

    return probe_app


@pytest.fixture()
def probe(client):
    """Cliente de la app probe, con el kill switch PRENDIDO (el caso normal de estos tests)."""
    from fastapi.testclient import TestClient

    import app.core.integration_auth as integration_auth

    previous_flag = integration_auth.INTEGRATION_API_ENABLED
    integration_auth.INTEGRATION_API_ENABLED = True
    try:
        with TestClient(_build_probe_app()) as probe_client:
            yield probe_client
    finally:
        integration_auth.INTEGRATION_API_ENABLED = previous_flag


@pytest.fixture()
def failure_cap(monkeypatch):
    """Fija el tope de rechazos por IP; se importa por nombre, así que se parchea en el módulo."""
    import app.core.integration_auth as integration_auth

    def _set_cap(rate: str) -> None:
        monkeypatch.setattr(integration_auth, "INTEGRATION_AUTH_FAILURE_RATE_LIMIT", rate)

    return _set_cap


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _create_issuer(username: str, gateway_role: str = "owner", *, is_active: bool = True) -> int:
    from app.models.user_model import UserModel

    return UserModel().create(
        {
            "username": username,
            "email": f"{username}@gateway.local",
            "hashed_password": ISSUER_PASSWORD_PLACEHOLDER,
            "full_name": "Emisor de tests",
            "notes": None,
            "is_active": is_active,
            "gateway_role": gateway_role,
        }
    )


def _set_user_column(user_id: int, column: str, value: Any) -> None:
    assert column in {"gateway_role", "is_active"}, column
    with Database().engine.begin() as connection:
        connection.execute(
            text(f"UPDATE users SET {column} = :value WHERE id = :id"),
            {"value": value, "id": user_id},
        )


@dataclass(frozen=True)
class MintedToken:
    bearer: str
    pk: int
    public_id: str
    secret: str


def _make_token(
    issuer_id: int,
    scopes: list[str],
    *,
    server_ids: tuple[int, ...] = (),
    blueprint_ids: tuple[int, ...] = (),
    expires_in: timedelta = timedelta(days=10),
    revoked: bool = False,
    name: str = "ci-pipeline",
) -> MintedToken:
    from app.models.integration_token import (
        IntegrationToken,
        IntegrationTokenBlueprint,
        IntegrationTokenServer,
    )

    public_id, secret, bearer = mint_integration_token()
    session = Database().get_declarative_base_session()
    try:
        row = IntegrationToken(
            token_id=public_id,
            secret_hmac=integration_token_hmac(secret),
            name=name,
            scopes=",".join(scopes),
            created_by_admin_id=issuer_id,
            expires_at=_utc_now() + expires_in,
            revoked_at=_utc_now() if revoked else None,
        )
        session.add(row)
        session.flush()
        for server_id in server_ids:
            session.add(IntegrationTokenServer(token_pk=row.id, server_id=server_id))
        for blueprint_id in blueprint_ids:
            session.add(IntegrationTokenBlueprint(token_pk=row.id, model_id=blueprint_id))
        session.commit()
        return MintedToken(bearer=bearer, pk=row.id, public_id=public_id, secret=secret)
    finally:
        session.close()


def _bearer(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


def _unknown_bearer() -> str:
    """Un bearer bien formado con un ``public_id`` NUEVO cada vez: el ataque que se cierra."""
    _, _, bearer = mint_integration_token()
    return bearer


def _public_context(response) -> dict[str, Any]:
    return (response.json().get("detail") or {}).get("public_context") or {}


def _audit_rows(action: str, status: str | None = None) -> list[Any]:
    from app.models.audit_log import AuditLog

    session = Database().get_declarative_base_session()
    try:
        query = session.query(AuditLog).filter(AuditLog.action == action)
        if status is not None:
            query = query.filter(AuditLog.status == status)
        rows = query.order_by(AuditLog.id).all()
        session.expunge_all()
        return rows
    finally:
        session.close()


def _last_used_at(token_pk: int) -> datetime | None:
    from app.models.integration_token import IntegrationToken

    session = Database().get_declarative_base_session()
    try:
        return session.get(IntegrationToken, token_pk).last_used_at
    finally:
        session.close()


@pytest.fixture()
def owner_issuer(client) -> int:
    return _create_issuer("emisor-owner", "owner")


READ_SCOPE = IntegrationScope.SERVERS_LIST.value
CREATE_SCOPE = IntegrationScope.DATABASES_CREATE.value


def test_a_valid_token_authenticates_and_yields_a_machine_actor_for_the_called_scope(
    probe, owner_issuer
):
    minted = _make_token(owner_issuer, [READ_SCOPE, CREATE_SCOPE], server_ids=(7, 9))

    response = probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == READ_SCOPE
    assert body["actor_kind"] == "integration"
    # Capa 1 estricta: el actor lleva SOLO la capacidad del scope llamado, no todas las del token.
    assert body["actor_capabilities"] == [Capability.SERVERS_READ.value]
    assert body["allowed_server_ids"] == [7, 9]
    assert body["allowed_blueprint_ids"] == []


# --- 401 opaco ------------------------------------------------------------- #


def test_every_credential_failure_returns_the_same_opaque_401(probe, owner_issuer, failure_cap):
    """
    Inexistente, malformado, secreto erróneo, revocado, vencido, emisor inactivo, emisor
    inexistente y sin header: mismo status Y mismos bytes. Distinguirlos es un oráculo del estado
    de los tokens que alguien haya adivinado.
    """
    failure_cap(GENEROUS_FAILURE_CAP)
    inactive_issuer = _create_issuer("emisor-inactivo", "owner", is_active=False)
    valid = _make_token(owner_issuer, [READ_SCOPE])
    revoked = _make_token(owner_issuer, [READ_SCOPE], revoked=True)
    expired = _make_token(owner_issuer, [READ_SCOPE], expires_in=timedelta(seconds=-5))
    with_inactive_issuer = _make_token(inactive_issuer, [READ_SCOPE])
    with_phantom_issuer = _make_token(987_654, [READ_SCOPE])

    attempts: dict[str, dict[str, str]] = {
        "missing_header": {},
        "not_a_bearer": {"Authorization": "Basic Zm9vOmJhcg=="},
        "malformed": _bearer("datumint.only-two-parts"),
        "wrong_prefix": _bearer(f"datum.{valid.public_id}.{valid.secret}"),
        "unknown_token": _bearer(_unknown_bearer()),
        "wrong_secret": _bearer(f"datumint.{valid.public_id}.{'x' * 43}"),
        "revoked": _bearer(revoked.bearer),
        "expired": _bearer(expired.bearer),
        "inactive_issuer": _bearer(with_inactive_issuer.bearer),
        "phantom_issuer": _bearer(with_phantom_issuer.bearer),
    }
    responses = {name: probe.get(PROBE_READ_PATH, headers=h) for name, h in attempts.items()}

    assert {name: r.status_code for name, r in responses.items()} == {
        name: 401 for name in attempts
    }
    distinct_bodies = {r.content for r in responses.values()}
    assert len(distinct_bodies) == 1, {name: r.text for name, r in responses.items()}
    assert _public_context(responses["unknown_token"])["code"] == CODE_INTEGRATION_TOKEN_INVALID


def test_a_session_cookie_never_authenticates_an_integration_route(
    probe, admin_client, owner_issuer, failure_cap
):
    failure_cap(GENEROUS_FAILURE_CAP)
    probe.cookies.set("gw_session", admin_client.cookies.get("gw_session"))

    response = probe.get(PROBE_READ_PATH)

    assert response.status_code == 401
    assert _public_context(response)["code"] == CODE_INTEGRATION_TOKEN_INVALID


def test_deactivating_the_issuer_kills_the_token_on_the_next_call(probe, owner_issuer, failure_cap):
    failure_cap(GENEROUS_FAILURE_CAP)
    minted = _make_token(owner_issuer, [READ_SCOPE])
    assert probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer)).status_code == 200

    _set_user_column(owner_issuer, "is_active", 0)

    response = probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))
    assert response.status_code == 401
    assert _public_context(response)["code"] == CODE_INTEGRATION_TOKEN_INVALID


def test_rejections_never_put_the_secret_in_the_audit_trail(probe, owner_issuer, failure_cap):
    failure_cap(GENEROUS_FAILURE_CAP)
    valid = _make_token(owner_issuer, [READ_SCOPE])

    probe.get(PROBE_READ_PATH, headers=_bearer(f"datumint.{valid.public_id}.{'y' * 43}"))

    rejection_rows = _audit_rows("integration.auth", "failure")
    assert len(rejection_rows) == 1
    detail = rejection_rows[0].detail or ""
    assert "rejection=bad_hmac" in detail
    assert valid.public_id in detail
    assert valid.secret not in detail
    assert "y" * 43 not in detail
    assert rejection_rows[0].actor_type == "anonymous"


# --- kill switch ----------------------------------------------------------- #


def test_the_kill_switch_is_off_by_default_and_answers_503_even_with_a_valid_token(
    client, owner_issuer, monkeypatch
):
    from fastapi.testclient import TestClient

    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", False)
    minted = _make_token(owner_issuer, [READ_SCOPE])

    with TestClient(_build_probe_app()) as probe_client:
        with_token = probe_client.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))
        without_token = probe_client.get(PROBE_READ_PATH)

    assert with_token.status_code == without_token.status_code == 503
    assert _public_context(with_token)["code"] == CODE_INTEGRATION_DISABLED
    # Auditado y agregado como el resto: dos intentos con el servidor apagado, UNA fila.
    disabled_rows = [
        row for row in _audit_rows("integration.auth", "failure") if "rejection=disabled" in row.detail
    ]
    assert len(disabled_rows) == 1


def test_the_same_token_works_once_the_kill_switch_is_on(probe, owner_issuer):
    minted = _make_token(owner_issuer, [READ_SCOPE])

    assert probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer)).status_code == 200


# --- tope por IP, ANTES de la BD ------------------------------------------- #


def test_the_per_ip_failure_cap_answers_429_before_touching_the_database(
    probe, failure_cap, monkeypatch
):
    import app.core.integration_auth as integration_auth

    failure_cap("3/minute")
    database_lookups = {"count": 0}
    real_session_factory = integration_auth._session

    def counting_session_factory():
        database_lookups["count"] += 1
        return real_session_factory()

    monkeypatch.setattr(integration_auth, "_session", counting_session_factory)

    statuses = [
        probe.get(PROBE_READ_PATH, headers=_bearer(_unknown_bearer())).status_code
        for _ in range(6)
    ]

    # Tres rechazos agotan el cupo; los siguientes son 429 y ni llegan a la BD.
    assert statuses == [401, 401, 401, 429, 429, 429], statuses
    assert database_lookups["count"] == 3


def test_the_failure_cap_429_carries_retry_after(probe, failure_cap):
    failure_cap("1/minute")
    probe.get(PROBE_READ_PATH, headers=_bearer(_unknown_bearer()))

    response = probe.get(PROBE_READ_PATH, headers=_bearer(_unknown_bearer()))

    assert response.status_code == 429
    retry_after_seconds = int(response.headers["Retry-After"])
    assert 1 <= retry_after_seconds <= 60


def test_authenticated_requests_do_not_spend_the_failure_quota(probe, owner_issuer, failure_cap):
    failure_cap("2/minute")
    minted = _make_token(owner_issuer, [READ_SCOPE])

    statuses = [probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer)).status_code for _ in range(5)]

    assert statuses == [200] * 5
    assert _audit_rows("integration.auth", "failure") == []


def test_rejections_are_audited_once_per_ip_per_window_and_the_next_row_counts_the_rest(
    probe, failure_cap, monkeypatch
):
    import app.core.integration_auth as integration_auth

    failure_cap(GENEROUS_FAILURE_CAP)
    for _ in range(10):
        assert probe.get(PROBE_READ_PATH, headers=_bearer(_unknown_bearer())).status_code == 401
    first_window_rows = _audit_rows("integration.auth", "failure")
    assert len(first_window_rows) == 1
    assert "aggregated=0" in first_window_rows[0].detail

    advanced_clock = integration_auth.monotonic() + integration_auth.AUDIT_WINDOW_SECONDS + 1
    monkeypatch.setattr(integration_auth, "monotonic", lambda: advanced_clock)
    probe.get(PROBE_READ_PATH, headers=_bearer(_unknown_bearer()))

    rows = _audit_rows("integration.auth", "failure")
    assert len(rows) == 2
    assert "aggregated=9" in rows[1].detail


# --- cupos por token ------------------------------------------------------- #


def test_a_token_over_its_base_quota_gets_429_with_retry_after_and_other_tokens_are_unaffected(
    probe, owner_issuer, monkeypatch
):
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_RATE_LIMIT", "2/minute")
    token_a = _make_token(owner_issuer, [READ_SCOPE], name="token-a")
    token_b = _make_token(owner_issuer, [READ_SCOPE], name="token-b")

    statuses_a = [probe.get(PROBE_READ_PATH, headers=_bearer(token_a.bearer)).status_code for _ in range(3)]
    throttled = probe.get(PROBE_READ_PATH, headers=_bearer(token_a.bearer))

    assert statuses_a == [200, 200, 429]
    assert 1 <= int(throttled.headers["Retry-After"]) <= 60
    assert probe.get(PROBE_READ_PATH, headers=_bearer(token_b.bearer)).status_code == 200


def test_a_mutating_scope_also_spends_the_write_bucket_and_reads_do_not(
    probe, owner_issuer, monkeypatch
):
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_WRITE_RATE_LIMIT", "1/minute")
    writer = _make_token(owner_issuer, [READ_SCOPE, CREATE_SCOPE], name="writer")
    other_writer = _make_token(owner_issuer, [CREATE_SCOPE], name="other-writer")

    first_write = probe.post(PROBE_WRITE_PATH, headers=_bearer(writer.bearer))
    second_write = probe.post(PROBE_WRITE_PATH, headers=_bearer(writer.bearer))

    assert first_write.status_code == 200, first_write.text
    assert second_write.status_code == 429
    assert 1 <= int(second_write.headers["Retry-After"]) <= 60
    # El cupo de escritura agotado no consume el de lectura del mismo token ni el de otro token.
    assert probe.get(PROBE_READ_PATH, headers=_bearer(writer.bearer)).status_code == 200
    assert probe.post(PROBE_WRITE_PATH, headers=_bearer(other_writer.bearer)).status_code == 200


def test_a_destructive_scope_spends_the_destructive_bucket_with_retry_after(
    probe, owner_issuer, monkeypatch
):
    import app.core.integration_auth as integration_auth

    five_per_minute = "5/minute"
    monkeypatch.setattr(integration_auth, "INTEGRATION_DESTRUCTIVE_RATE_LIMIT", five_per_minute)
    destructive_token = _make_token(
        owner_issuer, [IntegrationScope.MIGRATIONS_ROLLBACK.value], name="destructive"
    )

    statuses = [
        probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(destructive_token.bearer)).status_code
        for _ in range(6)
    ]
    throttled = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(destructive_token.bearer))

    # Cinco llamadas pasan y la sexta dentro del minuto es 429 con Retry-After.
    assert statuses == [200, 200, 200, 200, 200, 429], statuses
    assert throttled.status_code == 429
    assert 1 <= int(throttled.headers["Retry-After"]) <= 60


def test_the_destructive_bucket_is_shared_by_rollback_and_stamp_of_the_same_token(
    probe, owner_issuer, monkeypatch
):
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_DESTRUCTIVE_RATE_LIMIT", "2/minute")
    token = _make_token(
        owner_issuer,
        [IntegrationScope.MIGRATIONS_ROLLBACK.value, IntegrationScope.MIGRATIONS_STAMP.value],
        name="both-destructive",
    )

    first_rollback = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token.bearer))
    first_stamp = probe.post(PROBE_SECOND_DESTRUCTIVE_PATH, headers=_bearer(token.bearer))
    over_the_shared_cap = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token.bearer))

    # Un cupo por token para el tier, no uno por scope: alternar rollback y stamp no duplica el tope.
    assert [first_rollback.status_code, first_stamp.status_code] == [200, 200]
    assert over_the_shared_cap.status_code == 429


def test_the_destructive_bucket_is_isolated_between_tokens_and_from_the_other_buckets(
    probe, owner_issuer, monkeypatch
):
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_DESTRUCTIVE_RATE_LIMIT", "1/minute")
    rollback_scope = IntegrationScope.MIGRATIONS_ROLLBACK.value
    token_a = _make_token(owner_issuer, [rollback_scope, READ_SCOPE, CREATE_SCOPE], name="dest-a")
    token_b = _make_token(owner_issuer, [rollback_scope], name="dest-b")

    first = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token_a.bearer))
    second = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token_a.bearer))

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    # Agotar el cupo destructivo de A no toca sus cupos base/escritura ni el destructivo de B.
    assert probe.get(PROBE_READ_PATH, headers=_bearer(token_a.bearer)).status_code == 200
    assert probe.post(PROBE_WRITE_PATH, headers=_bearer(token_a.bearer)).status_code == 200
    assert probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token_b.bearer)).status_code == 200


def test_a_destructive_call_also_spends_the_base_and_write_buckets(probe, owner_issuer, monkeypatch):
    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_WRITE_RATE_LIMIT", "1/minute")
    token = _make_token(owner_issuer, [IntegrationScope.MIGRATIONS_ROLLBACK.value], name="dest-write")

    first = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token.bearer))
    second = probe.post(PROBE_DESTRUCTIVE_PATH, headers=_bearer(token.bearer))

    # El tier destructivo SE SUMA a los cupos base y de escritura: no los reemplaza.
    assert first.status_code == 200, first.text
    assert second.status_code == 429


# --- scopes: suspensión y capa 1 -------------------------------------------- #


def test_a_scope_the_issuer_lost_is_suspended_while_the_other_scopes_keep_working(
    probe, client, failure_cap
):
    failure_cap(GENEROUS_FAILURE_CAP)
    issuer_id = _create_issuer("emisor-degradable", "owner")
    minted = _make_token(issuer_id, [READ_SCOPE, CREATE_SCOPE])
    assert probe.post(PROBE_WRITE_PATH, headers=_bearer(minted.bearer)).status_code == 200

    _set_user_column(issuer_id, "gateway_role", "viewer")

    suspended = probe.post(PROBE_WRITE_PATH, headers=_bearer(minted.bearer))
    assert suspended.status_code == 403
    assert _public_context(suspended)["code"] == CODE_INTEGRATION_SCOPE_MISSING
    assert probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer)).status_code == 200

    # Recuperar la capacidad reactiva el scope sin reemitir el token.
    _set_user_column(issuer_id, "gateway_role", "owner")
    assert probe.post(PROBE_WRITE_PATH, headers=_bearer(minted.bearer)).status_code == 200


def test_a_scope_the_token_never_had_is_403_and_does_not_name_it(probe, owner_issuer):
    minted = _make_token(owner_issuer, [READ_SCOPE])

    response = probe.post(PROBE_WRITE_PATH, headers=_bearer(minted.bearer))

    assert response.status_code == 403
    public_context = _public_context(response)
    assert public_context == {"code": CODE_INTEGRATION_SCOPE_MISSING}


def test_an_out_of_vocabulary_stored_scope_is_inert_and_does_not_break_the_token(
    probe, owner_issuer
):
    minted = _make_token(owner_issuer, [READ_SCOPE, "access.admin", "legacy.thing"])

    assert probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer)).status_code == 200
    # Un string de capacidad en la fila no vale como scope de integración.
    assert probe.post(PROBE_WRITE_PATH, headers=_bearer(minted.bearer)).status_code == 403


# --- destino: listas de permitidos y capa 2 ---------------------------------- #


def test_the_server_allowlist_narrows_the_target(probe, owner_issuer):
    minted = _make_token(
        owner_issuer, [IntegrationScope.DATABASES_LIST.value], server_ids=(1,)
    )
    headers = _bearer(minted.bearer)

    allowed_server = probe.get(PROBE_SERVER_PATH.format(server_id=1), headers=headers)
    other_server = probe.get(PROBE_SERVER_PATH.format(server_id=2), headers=headers)

    assert allowed_server.status_code == 200, allowed_server.text
    assert other_server.status_code == 403
    assert _public_context(other_server)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED


def test_an_unknown_target_is_indistinguishable_from_one_outside_the_allowlist(
    probe, owner_issuer
):
    outside_database = sembrar_bd(server_id=2, name="outside_db")
    minted = _make_token(
        owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED.value], server_ids=(1,)
    )
    headers = _bearer(minted.bearer)

    outside = probe.get(PROBE_DATABASE_READ_PATH.format(db_id=outside_database), headers=headers)
    nonexistent = probe.get(PROBE_DATABASE_READ_PATH.format(db_id=424_242), headers=headers)

    assert outside.status_code == nonexistent.status_code == 403
    assert outside.content == nonexistent.content
    assert _public_context(nonexistent)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED


def test_a_token_without_server_allowlist_reaches_no_server(probe, owner_issuer):
    database_id = sembrar_bd(server_id=1, name="no_allowlist_db")
    minted = _make_token(owner_issuer, [IntegrationScope.BLUEPRINT_READ_ASSIGNED.value])

    response = probe.get(
        PROBE_DATABASE_READ_PATH.format(db_id=database_id), headers=_bearer(minted.bearer)
    )

    assert response.status_code == 403
    assert _public_context(response)["code"] == CODE_INTEGRATION_SERVER_NOT_ALLOWED


def test_the_blueprint_allowlist_narrows_databases_that_have_a_blueprint(probe, owner_issuer):
    from app.models.database_model import DatabaseModel

    session = Database().get_declarative_base_session()
    try:
        allowed_blueprint = DatabaseModel(name="Allowed", slug="allowed-blueprint")
        other_blueprint = DatabaseModel(name="Other", slug="other-blueprint")
        session.add_all([allowed_blueprint, other_blueprint])
        session.commit()
        allowed_blueprint_id, other_blueprint_id = allowed_blueprint.id, other_blueprint.id
    finally:
        session.close()
    database_with_allowed = sembrar_bd(server_id=1, name="db_allowed_bp")
    database_with_other = sembrar_bd(server_id=1, name="db_other_bp")
    database_without_blueprint = sembrar_bd(server_id=1, name="db_no_bp")
    with Database().engine.begin() as connection:
        for database_id, blueprint_id in (
            (database_with_allowed, allowed_blueprint_id),
            (database_with_other, other_blueprint_id),
        ):
            connection.execute(
                text("UPDATE managed_databases SET model_id = :m WHERE id = :d"),
                {"m": blueprint_id, "d": database_id},
            )
    minted = _make_token(
        owner_issuer,
        [IntegrationScope.BLUEPRINT_READ_ASSIGNED.value],
        server_ids=(1,),
        blueprint_ids=(allowed_blueprint_id,),
    )
    headers = _bearer(minted.bearer)

    allowed = probe.get(PROBE_DATABASE_READ_PATH.format(db_id=database_with_allowed), headers=headers)
    outside = probe.get(PROBE_DATABASE_READ_PATH.format(db_id=database_with_other), headers=headers)
    unassigned = probe.get(
        PROBE_DATABASE_READ_PATH.format(db_id=database_without_blueprint), headers=headers
    )

    assert allowed.status_code == 200, allowed.text
    assert outside.status_code == 403
    assert _public_context(outside)["code"] == CODE_INTEGRATION_BLUEPRINT_NOT_ALLOWED
    # Sin blueprint asignado no hay nada que la lista restrinja: la asignación la valida la fase 3.
    assert unassigned.status_code == 200


def test_layer_two_follows_the_issuer_current_role_at_the_target(probe, client):
    """
    El token ejerce el rol REAL del emisor en el destino: ``owner`` solo en desarrollo, ``viewer``
    en producción. Y como el emisor se relee por request, quitarle el grant corta al token en la
    llamada siguiente.
    """
    issuer_id = _create_issuer("emisor-por-entorno", "viewer")
    development_database = sembrar_bd(
        server_id=1, environment_id=env_id("development"), name="layer2_dev_db"
    )
    production_database = sembrar_bd(
        server_id=1, environment_id=env_id("production"), name="layer2_prod_db"
    )
    with Database().engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO access_grants (user_id, scope_type, scope_id, role, created_at) "
                "VALUES (:u, 'environment', :e, 'owner', CURRENT_TIMESTAMP)"
            ),
            {"u": issuer_id, "e": env_id("development")},
        )
    minted = _make_token(
        issuer_id, [IntegrationScope.DATABASES_ASSIGN_BLUEPRINT.value], server_ids=(1,)
    )
    headers = _bearer(minted.bearer)

    in_development = probe.post(
        PROBE_DATABASE_WRITE_PATH.format(db_id=development_database), headers=headers
    )
    in_production = probe.post(
        PROBE_DATABASE_WRITE_PATH.format(db_id=production_database), headers=headers
    )

    assert in_development.status_code == 200, in_development.text
    assert in_production.status_code == 403
    assert _public_context(in_production)["code"] == "access.forbidden"

    with Database().engine.begin() as connection:
        connection.execute(
            text("DELETE FROM access_grants WHERE user_id = :u"), {"u": issuer_id}
        )
    after_losing_the_grant = probe.post(
        PROBE_DATABASE_WRITE_PATH.format(db_id=development_database), headers=headers
    )
    assert after_losing_the_grant.status_code == 403


# --- last_used_at y auditoría por llamada ------------------------------------ #


def test_last_used_at_is_written_on_first_use_and_throttled_to_one_write_per_minute(
    probe, owner_issuer
):
    minted = _make_token(owner_issuer, [READ_SCOPE])
    assert _last_used_at(minted.pk) is None

    probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))
    first_use = _last_used_at(minted.pk)
    assert first_use is not None

    probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))
    assert _last_used_at(minted.pk) == first_use

    stale_use = first_use - timedelta(minutes=2)
    with Database().engine.begin() as connection:
        connection.execute(
            text("UPDATE integration_tokens SET last_used_at = :t WHERE id = :id"),
            {"t": stale_use, "id": minted.pk},
        )
    probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))
    assert _last_used_at(minted.pk) > stale_use


def test_each_successful_call_is_audited_with_the_integration_identity_and_no_secret(
    probe, owner_issuer
):
    minted = _make_token(owner_issuer, [READ_SCOPE])

    probe.get(PROBE_READ_PATH, headers=_bearer(minted.bearer))

    call_rows = _audit_rows("integration.call", "success")
    assert len(call_rows) == 1
    call_row = call_rows[0]
    assert call_row.actor_type == "integration"
    assert call_row.integration_token_id == minted.pk
    assert call_row.admin_id == owner_issuer
    assert call_row.admin_username == f"integration:{minted.public_id}"
    assert READ_SCOPE in (call_row.detail or "")
    assert minted.secret not in (call_row.detail or "")


# --- el token no vale fuera de /integration ---------------------------------- #


def test_an_integration_token_is_rejected_by_the_mcp_endpoint(client, owner_issuer, monkeypatch):
    import app.core.mcp_auth as mcp_auth

    monkeypatch.setattr(mcp_auth, "MCP_ENABLED", True)
    minted = _make_token(owner_issuer, [READ_SCOPE])

    response = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers=_bearer(minted.bearer),
    )

    assert response.status_code == 401


def test_an_integration_token_does_not_authenticate_session_routes(client, owner_issuer):
    minted = _make_token(owner_issuer, [READ_SCOPE])

    response = client.get("/api/v1/servers", headers=_bearer(minted.bearer))

    assert response.status_code == 401


# --- marcadores y helpers del catálogo ---------------------------------------- #


def test_require_integration_stamps_the_enumerable_markers():
    from app.core.integration_auth import declared_integration_scope
    from app.core.authz import declared_capability, declared_scope
    from app.services.integration_scope_catalog import INTEGRATION_ALLOWED

    with_target = require_integration(IntegrationScope.BLUEPRINT_READ_ASSIGNED, target=database_target)
    without_target = require_integration(IntegrationScope.SERVERS_LIST)

    assert declared_integration_scope(with_target) == IntegrationScope.BLUEPRINT_READ_ASSIGNED.value
    assert declared_capability(with_target) == INTEGRATION_ALLOWED[
        IntegrationScope.BLUEPRINT_READ_ASSIGNED
    ].value
    assert declared_scope(with_target) == "database"
    assert declared_integration_scope(without_target) == IntegrationScope.SERVERS_LIST.value
    assert declared_scope(without_target) is None


def test_require_integration_never_asks_the_machine_for_step_up(monkeypatch, owner_issuer):
    """
    D6: ``assert_step_up`` falla cerrado para una máquina, y ``blueprints.apply`` lo pide. El
    token no hace step-up nunca (lo hizo el emisor al otorgarle el scope), así que la
    dependencia no puede llamarlo: si lo hiciera, este 200 sería un 403 ``access.forbidden``.
    """
    from fastapi.testclient import TestClient

    import app.core.integration_auth as integration_auth

    monkeypatch.setattr(integration_auth, "INTEGRATION_API_ENABLED", True)
    probe_app = FastAPI()
    probe_app.add_exception_handler(AppHttpException, app_exception_handler)

    @probe_app.post("/probe/apply/{db_id}")
    def probe_apply(
        db_id: int,
        call: IntegrationCall = Depends(
            require_integration(IntegrationScope.MIGRATIONS_APPLY_FORWARD, target=database_target)
        ),
    ) -> dict[str, Any]:
        return _describe_call(call)

    database_id = sembrar_bd(server_id=1, name="apply_db")
    minted = _make_token(
        owner_issuer, [IntegrationScope.MIGRATIONS_APPLY_FORWARD.value], server_ids=(1,)
    )

    with TestClient(probe_app) as probe_client:
        response = probe_client.post(f"/probe/apply/{database_id}", headers=_bearer(minted.bearer))

    assert response.status_code == 200, response.text


def test_parse_stored_scopes_keeps_unknown_values_and_effective_scopes_drop_them():
    stored = parse_stored_integration_scopes(" servers.list, legacy.thing ,servers.list,,databases.create")

    assert stored == ["databases.create", "legacy.thing", "servers.list"]
    assert parse_stored_integration_scopes(None) == []
    assert parse_stored_integration_scopes("") == []

    effective = effective_integration_scopes(stored, frozenset({Capability.SERVERS_READ}))
    assert effective == frozenset({IntegrationScope.SERVERS_LIST})
