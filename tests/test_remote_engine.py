"""Capa de conexión remota: URLs, cache y traducción de errores."""

import pytest
from sqlalchemy.exc import OperationalError

from app.core import remote_engine as re
from app.exceptions import AppHttpException


def _target(dialect="mysql", server_id=1):
    return re.ServerTarget(
        server_id=server_id,
        dialect=dialect,
        host="db.example.com",
        port=3306,
        admin_user="root",
        admin_password="TOPSECRETpw",
    )


def test_url_mysql_has_no_database_and_hides_password():
    engine = re.get_engine(_target("mysql", 10))
    url = engine.url
    assert url.drivername == "mysql+pymysql"
    assert url.database in (None, "")
    rendered = url.render_as_string(hide_password=True)
    assert "TOPSECRETpw" not in rendered
    re.invalidate_server(10)


def test_url_postgres_admin_and_specific_db():
    t = _target("postgresql", 11)
    assert re.get_engine(t).url.database == "postgres"
    assert re.get_engine(t, "midb").url.database == "midb"
    assert re.get_engine(t).url.drivername == "postgresql+psycopg"
    re.invalidate_server(11)


def test_unsupported_dialect_raises_422():
    with pytest.raises(AppHttpException) as exc:
        re.get_engine(_target("oracle", 12))
    assert exc.value.status_code == 422


def test_engine_cache_and_invalidate():
    t = _target("mysql", 99)
    e1 = re.get_engine(t)
    e2 = re.get_engine(t)
    assert e1 is e2  # cacheado
    re.invalidate_server(99)
    e3 = re.get_engine(t)
    assert e3 is not e1  # reconstruido tras invalidar
    re.invalidate_server(99)


# --- _extract_code + map_driver_error -------------------------------------- #
class _Orig(Exception):
    def __init__(self, *, sqlstate=None, code=None):
        self.sqlstate = sqlstate
        super().__init__(code if code is not None else (sqlstate or "err"))


class _Exc(Exception):
    def __init__(self, orig):
        self.orig = orig


@pytest.mark.parametrize(
    "code,expected",
    [
        (2003, 502),  # MySQL no conecta
        (1045, 502),  # access denied admin
        (2013, 504),  # timeout
        (1049, 404),  # unknown database
        (1007, 409),  # db exists
        (1044, 403),  # access denied a la BD
    ],
)
def test_map_mysql_errno(code, expected):
    exc = map = re.map_driver_error(_Exc(_Orig(code=code)), op="x", target=_target())
    assert exc.status_code == expected
    assert exc.context["remote_error_code"] == str(code)


@pytest.mark.parametrize(
    "sqlstate,expected",
    [
        ("08006", 502),
        ("28P01", 502),
        ("57014", 504),
        ("3D000", 404),
        ("42P04", 409),
        ("42501", 403),
    ],
)
def test_map_postgres_sqlstate(sqlstate, expected):
    exc = re.map_driver_error(_Exc(_Orig(sqlstate=sqlstate)), op="x", target=_target("postgresql"))
    assert exc.status_code == expected


def test_map_unknown_operational_error_is_502_without_code():
    # OperationalError real con mensaje largo (típico "could not connect" de psycopg).
    op_err = OperationalError("SELECT 1", {}, Exception("could not connect to server ..."))
    exc = re.map_driver_error(op_err, op="test_connection", target=_target("postgresql"))
    assert exc.status_code == 502
    assert "remote_error_code" not in exc.context  # no se vuelca el mensaje largo


def test_map_does_not_leak_password():
    exc = re.map_driver_error(_Exc(_Orig(code=2003)), op="x", target=_target())
    assert "TOPSECRETpw" not in str(exc.context)
    assert "TOPSECRETpw" not in str(exc.detail)
