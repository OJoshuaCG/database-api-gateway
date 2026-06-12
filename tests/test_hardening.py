"""
Endurecimientos de producción (bloqueantes de seguridad):
- S1: CORS no permite credenciales con orígenes comodín.
- S2: la política TLS (ssl_mode) se cablea a connect_args por dialecto.
- B1: el identificador 'user'@'host' de MySQL pasa por quoting (doble defensa).
"""

import pytest

from app.core.remote_engine import ServerTarget, _connect_args
from app.core.versioned_app import cors_allow_credentials
from app.services.db_admin.mysql_adapter import MySQLAdapter


# --------------------------- S1: CORS + credenciales --------------------------- #
@pytest.mark.parametrize(
    "origins,expected",
    [
        (["*"], False),
        ([], False),
        (["https://panel.example.com"], True),
        (["https://a.example.com", "https://b.example.com"], True),
        (["https://a.example.com", "*"], False),
    ],
)
def test_cors_allow_credentials(origins, expected):
    assert cors_allow_credentials(origins) is expected


# --------------------------- S2: TLS hacia los motores ------------------------- #
def test_connect_args_postgres_sslmode_passthrough():
    args = _connect_args("postgresql", "require")
    assert args["sslmode"] == "require"
    args2 = _connect_args("postgresql", "verify-full")
    assert args2["sslmode"] == "verify-full"


def test_connect_args_postgres_unknown_mode_defaults_require():
    args = _connect_args("postgresql", "on")
    assert args["sslmode"] == "require"


def test_connect_args_postgres_disabled_has_no_sslmode():
    for mode in (None, "", "disable", "off"):
        assert "sslmode" not in _connect_args("postgresql", mode)


def test_connect_args_mysql_enables_ssl():
    args = _connect_args("mysql", "require")
    assert args["ssl"] == {"check_hostname": False}
    # Deshabilitado por defecto (no rompe el camino sin TLS verificado).
    for mode in (None, "", "disable"):
        assert "ssl" not in _connect_args("mysql", mode)


# --------------------------- B1: quoting 'user'@'host' ------------------------- #
def _adapter() -> MySQLAdapter:
    target = ServerTarget(
        server_id=1,
        dialect="mysql",
        host="10.0.0.1",
        port=3306,
        admin_user="root",
        admin_password="x",
    )
    return MySQLAdapter(target)


def test_mysql_user_at_host_is_quoted_as_string_literals():
    assert _adapter()._user_at_host("appuser", "%") == "'appuser'@'%'"
    assert _adapter()._user_at_host("u2", "10.0.0.5") == "'u2'@'10.0.0.5'"
