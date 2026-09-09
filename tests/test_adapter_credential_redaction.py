"""
Tests unitarios PUROS (sin cliente ni motor) de los dos guards de fuga silenciosa de los
adapters:

- ``MySQLAdapter._redact_embedded_credentials``: la contraseña de una tabla
  FEDERATED/CONNECT no puede terminar en el artefacto de snapshot, y el DDL sin credencial
  no se toca.
- ``ServerAdapter._catalog_fetch``: una consulta de catálogo opcional distingue
  ``denied`` (falta un privilegio: el vacío MIENTE) de ``unsupported`` (la feature no
  existe en esta versión), sin cambiar el ``[]`` que ven los llamadores de hoy.

Los códigos nativos se inyectan con excepciones falsas construidas como las de los
drivers: ``sqlstate`` para psycopg, ``args[0]`` int para pymysql. Lo que NO se verifica acá
es el comportamiento contra un motor real (hace falta una tabla FEDERATED/CONNECT viva y un
rol sin privilegio); eso queda para los ``verify_*_e2e.py``.
"""

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.services.db_admin.base_adapter import CatalogFetch, ServerAdapter
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter


# --------------------------------------------------------------------------- #
# FIX A — redacción de credenciales embebidas en el DDL de una tabla           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ddl, expected",
    [
        # FEDERATED: el caso del incidente. Host/puerto/usuario/base/tabla siguen legibles.
        (
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED "
            "CONNECTION='mysql://usr:s3cr3t@10.0.0.5:3306/remota/tbl'",
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED "
            "CONNECTION='mysql://usr:***@10.0.0.5:3306/remota/tbl'",
        ),
        # MariaDB/CONNECT: la contraseña va en OPTION_LIST, no en la URI.
        (
            "CREATE TABLE `c` (`id` int) ENGINE=CONNECT "
            "OPTION_LIST='host=h,user=u,password=p@ss w0rd,port=3306'",
            "CREATE TABLE `c` (`id` int) ENGINE=CONNECT "
            "OPTION_LIST='host=h,user=u,password=***,port=3306'",
        ),
        # CONNECT sobre ODBC: la clave es PWD y el separador es ';'.
        (
            "CREATE TABLE `c` (`id` int) ENGINE=CONNECT "
            "CONNECTION='DRIVER={MySQL};SERVER=h;UID=u;PWD=hunter2;'",
            "CREATE TABLE `c` (`id` int) ENGINE=CONNECT "
            "CONNECTION='DRIVER={MySQL};SERVER=h;UID=u;PWD=***;'",
        ),
        # Nombre de opción en minúsculas (el motor lo emite en mayúsculas, pero el DDL
        # puede venir de otro lado).
        (
            "CREATE TABLE `f` (`id` int) engine=FEDERATED connection='mysql://u:p@h/d/t'",
            "CREATE TABLE `f` (`id` int) engine=FEDERATED connection='mysql://u:***@h/d/t'",
        ),
        # Escape por backslash (sql_mode por defecto): la contraseña se redacta COMPLETA,
        # no se corta en la comilla escapada.
        (
            r"CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://u:pa\'ss@h/d/t'",
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://u:***@h/d/t'",
        ),
        # Escape por comilla duplicada (válido en cualquier sql_mode).
        (
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://u:pa''ss@h/d/t'",
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://u:***@h/d/t'",
        ),
        # Una coma dentro de la contraseña de la URI no la corta.
        (
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://u:pa,ss@h/d/t'",
            "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://u:***@h/d/t'",
        ),
    ],
)
def test_redacta_la_contrasena_y_deja_el_resto_del_ddl(ddl, expected):
    out, redacted = MySQLAdapter._redact_embedded_credentials(ddl)
    assert redacted is True
    assert out == expected
    assert "s3cr3t" not in out and "hunter2" not in out


@pytest.mark.parametrize(
    "ddl",
    [
        # Tabla común: no hay nada que redactar.
        "CREATE TABLE `t` (`id` int) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        # FEDERATED sin contraseña en la URI.
        "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://usr@10.0.0.5/d/t'",
        # CONNECTION que nombra un servidor de mysql.servers, no una URI.
        "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='fedlink'",
        # Contraseña vacía: no se inventa un '***' donde no había secreto.
        "CREATE TABLE `f` (`id` int) ENGINE=FEDERATED CONNECTION='mysql://usr:@h/d/t'",
        "CREATE TABLE `c` (`id` int) ENGINE=CONNECT OPTION_LIST='host=h,password=,port=3306'",
        # El barrido está acotado al literal de CONNECTION/OPTION_LIST: un COMMENT que
        # menciona 'password=' es DDL del usuario y no se toca.
        "CREATE TABLE `t` (`id` int COMMENT 'password=notasecret') ENGINE=InnoDB",
    ],
)
def test_sin_credencial_el_ddl_queda_identico(ddl):
    out, redacted = MySQLAdapter._redact_embedded_credentials(ddl)
    assert redacted is False
    assert out == ddl


def test_redacta_todas_las_opciones_no_solo_la_primera():
    ddl = (
        "CREATE TABLE `c` (`id` int) ENGINE=CONNECT "
        "CONNECTION='mysql://u:uno@h/d/t' OPTION_LIST='password=dos'"
    )
    out, redacted = MySQLAdapter._redact_embedded_credentials(ddl)
    assert redacted is True
    assert "uno" not in out and "dos" not in out
    assert out.count("***") == 2


# --------------------------------------------------------------------------- #
# FIX B — clasificación de una consulta de catálogo OPCIONAL                   #
# --------------------------------------------------------------------------- #
class _PgOrig(Exception):
    """Excepción de psycopg: el código nativo viaja en ``sqlstate``."""

    def __init__(self, sqlstate):
        super().__init__(f"error {sqlstate}")
        self.sqlstate = sqlstate


class _FakeConn:
    """Conexión mínima: devuelve filas o levanta la excepción que se le pase."""

    def __init__(self, *, exc=None, rows=None):
        self._exc = exc
        self._rows = rows or []

    def execute(self, *_args, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def _driver_error(orig):
    return ProgrammingError("SELECT 1", {}, orig)


def test_consulta_que_corre_devuelve_ok():
    res = ServerAdapter._catalog_fetch(_FakeConn(rows=[("v1",)]), "SELECT 1")
    assert isinstance(res, CatalogFetch)
    assert res.availability == "ok"
    assert list(res.rows) == [("v1",)]


@pytest.mark.parametrize(
    "orig",
    [
        _PgOrig("42501"),                       # PostgreSQL insufficient_privilege
        Exception(1142, "SELECT command denied"),  # MySQL ER_TABLEACCESS_DENIED_ERROR
        Exception(1227, "Access denied; you need SHOW_ROUTINE"),  # MySQL/MariaDB global
    ],
)
def test_falta_de_privilegio_se_marca_denied(orig):
    res = ServerAdapter._catalog_fetch(_FakeConn(exc=_driver_error(orig)), "SELECT 1")
    assert res.availability == "denied"
    # El contrato de compatibilidad: sigue siendo una lista vacía, no una excepción.
    assert list(res.rows) == []


@pytest.mark.parametrize(
    "exc",
    [
        _driver_error(_PgOrig("42P01")),        # undefined_table: la feature no existe
        _driver_error(Exception(1146, "Table doesn't exist")),
        OperationalError("SELECT 1", {}, Exception("sin codigo")),
    ],
)
def test_cualquier_otro_fallo_se_marca_unsupported(exc):
    res = ServerAdapter._catalog_fetch(_FakeConn(exc=exc), "SELECT 1")
    assert res.availability == "unsupported"
    assert list(res.rows) == []


def test_catalog_fetch_desempaqueta_como_tupla():
    """La forma es NamedTuple para que un llamador pueda migrar sin reescribir el bucle."""
    rows, availability = ServerAdapter._catalog_fetch(_FakeConn(rows=[(1,)]), "SELECT 1")
    assert list(rows) == [(1,)] and availability == "ok"


@pytest.mark.parametrize(
    "exc",
    [
        _driver_error(_PgOrig("42501")),
        _driver_error(_PgOrig("42P01")),
    ],
)
def test_safe_fetch_de_pg_sigue_devolviendo_lista_vacia(exc):
    """
    Compatibilidad EXIGIDA: el commit agrega la señal, no cambia el comportamiento. Los
    ~11 sitios del snapshot de PG que llaman a ``_safe_fetch`` tienen que seguir viendo
    ``[]`` tanto ante un error de privilegio como ante un catálogo ausente.
    """
    assert list(PostgresAdapter._safe_fetch(_FakeConn(exc=exc), "SELECT 1")) == []
