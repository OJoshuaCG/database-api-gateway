"""
Guards de arranque del PROCESO (``app/core/environments.py``).

OJO CON EL NOMBRE, que es la trampa que ``CLAUDE.md`` documenta: acá se prueba la config del
proceso (``APP_ENV``, secretos, rate limit, workers), **no** la tabla ``environments`` que
clasifica las BDs de terceros. Esa vive en ``test_api_environments.py`` y
``test_environment_guard.py``.

Los guards corren al IMPORTAR el módulo, así que cada test recarga
``app.core.environments`` con el entorno parcheado. Es el único modo de ejercitarlos: un
``raise`` en tiempo de import no se puede provocar de otra forma.
"""

import importlib

import pytest


def _reload(monkeypatch, **env):
    """Recarga la config del proceso con las variables dadas. Devuelve el módulo."""
    import app.core.environments as mod

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(mod)


# Variables que estos tests parchean. Se limpian EXPLÍCITAMENTE en el teardown en vez de
# confiar en el de `monkeypatch`: el orden de teardown entre una fixture autouse y una pedida
# por el test no está garantizado, y si se recargara el módulo ANTES de que `monkeypatch`
# deshaga los `setenv`, la config quedaría con los valores del test — envenenando a los ~40
# módulos que la importan, en la misma corrida de pytest.
_PATCHED = (
    "APP_ENV",
    "TRUSTED_PROXY_IPS",
    "WORKERS",
    "RATE_LIMIT_REDIS_ENABLED",
    "RATE_LIMIT_REDIS_URL",
    "SECRET_KEY",
    "SESSION_SECRET",
    "ADMIN_PASSWORD",
    "CORS_ORIGINS",
)


@pytest.fixture(autouse=True)
def _restore_config():
    """Deja el módulo como estaba: lo importan ~40 módulos y quedaría envenenado."""
    import os

    original = {k: os.environ.get(k) for k in _PATCHED}
    yield
    for k, v in original.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import app.core.environments as mod

    importlib.reload(mod)


# --------------------------------------------------------------------------- #
# Proxies confiables                                                          #
# --------------------------------------------------------------------------- #
def test_trusted_proxy_wildcard_is_rejected_in_production(monkeypatch):
    """
    ``TRUSTED_PROXY_IPS='*'`` es lo que hacía ficticio al rate limit por IP.

    Confiando en ``X-Forwarded-For`` de cualquier origen, el cliente elige su propia clave
    de rate limit y la rota: los 5/min del login y los 3/min del ``DROP DATABASE`` se evaden
    con un header.
    """
    with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
        _reload(
            monkeypatch,
            APP_ENV="production",
            TRUSTED_PROXY_IPS="*",
            SECRET_KEY="x" * 32,
            SESSION_SECRET="y" * 32,
            ADMIN_PASSWORD="z" * 12,
            CORS_ORIGINS="https://panel.example.com",
        )


def test_trusted_proxy_cidr_is_accepted_in_production(monkeypatch):
    """Un CIDR explícito es la configuración correcta y no debe bloquear el arranque."""
    mod = _reload(
        monkeypatch,
        APP_ENV="production",
        TRUSTED_PROXY_IPS="10.0.0.0/24",
        SECRET_KEY="x" * 32,
        SESSION_SECRET="y" * 32,
        ADMIN_PASSWORD="z" * 12,
        CORS_ORIGINS="https://panel.example.com",
    )
    assert mod.TRUSTED_PROXY_IPS == "10.0.0.0/24"


def test_trusted_proxy_defaults_to_localhost_only(monkeypatch):
    """
    Sin configurar, solo se confía en localhost — el default de uvicorn.

    Es el lado seguro: si el proxy no está declarado, `X-Forwarded-For` se ignora y todos
    comparten la clave del proxy. Peor para la granularidad, pero no spoofeable.
    """
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    mod = _reload(monkeypatch, APP_ENV="development")
    assert mod.TRUSTED_PROXY_IPS == "127.0.0.1"
