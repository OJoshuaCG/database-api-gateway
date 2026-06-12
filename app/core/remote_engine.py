"""
Capa de conexión DINÁMICA a servidores de base de datos DESTINO.

A diferencia de `app/core/database.py::Database` (singleton de UNA sola conexión,
reservado a la BD de metadatos del gateway), este módulo construye y cachea un
engine SQLAlchemy POR servidor remoto bajo demanda.

Decisiones:
- `poolclass=NullPool`: las operaciones administrativas (DDL/DCL/introspección)
  son esporádicas y contra MUCHOS servidores. No mantenemos pools persistentes que
  acumularían conexiones `sleep` en cada destino. El cache es del *engine* (caro de
  construir), no de las conexiones.
- AUTOCOMMIT en la conexión a nivel servidor: requerido por PostgreSQL para
  `CREATE/DROP DATABASE` (no admiten bloque transaccional) y consistente para DCL.
- Los errores del driver se traducen a `AppHttpException` con `map_driver_error`,
  sin filtrar jamás la credencial ni la URL de conexión.
"""

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.core.environments import REMOTE_CONNECT_TIMEOUT, REMOTE_STATEMENT_TIMEOUT_MS
from app.exceptions import AppHttpException

# Dialecto de negocio -> dialecto+driver de SQLAlchemy.
_DRIVERS = {
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
    "postgresql": "postgresql+psycopg",
}

# Base de datos a la que conectarse "a nivel servidor" (admin) por dialecto.
# MySQL/MariaDB admiten conexión sin BD (None). PostgreSQL SIEMPRE requiere una.
_ADMIN_DB = {"postgresql": "postgres"}


@dataclass(frozen=True)
class ServerTarget:
    """
    Datos de conexión a un servidor destino. `admin_password` llega YA descifrado
    (la capa que arma el target descifra en memoria); este módulo nunca lo loguea.
    """

    server_id: int
    dialect: str  # "mysql" | "mariadb" | "postgresql"
    host: str
    port: int
    admin_user: str
    admin_password: str
    ssl_mode: str | None = None


# ---------------------------------------------------------------------------
# Cache de engines
# ---------------------------------------------------------------------------

_engines: dict[tuple[int, str], Engine] = {}
_lock = threading.Lock()


def _require_driver(dialect: str) -> str:
    driver = _DRIVERS.get(dialect)
    if driver is None:
        raise AppHttpException(
            message=f"Motor de base de datos no soportado: {dialect}",
            status_code=422,
            context={"dialect": dialect, "supported": list(_DRIVERS)},
        )
    return driver


def _effective_database(dialect: str, database: str | None) -> str | None:
    """BD efectiva en la URL: la pedida, o la admin del dialecto si es None."""
    if database is not None:
        return database
    return _ADMIN_DB.get(dialect)


# Valores que DESHABILITAN TLS (sin cifrado). Cualquier otro valor lo fuerza.
_SSL_DISABLED = {"", "disable", "disabled", "off", "false", "0", "none"}
# sslmode válidos de PostgreSQL/psycopg.
_PG_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def _connect_args(dialect: str, ssl_mode: str | None = None) -> dict[str, Any]:
    mode = (ssl_mode or "").strip().lower()
    ssl_enabled = mode not in _SSL_DISABLED

    if dialect in ("mysql", "mariadb"):
        # pymysql: timeouts a nivel de socket cubren conexión y ejecución.
        stmt_timeout_s = max(1, REMOTE_STATEMENT_TIMEOUT_MS // 1000)
        args: dict[str, Any] = {
            "connect_timeout": REMOTE_CONNECT_TIMEOUT,
            "read_timeout": stmt_timeout_s,
            "write_timeout": stmt_timeout_s,
            "charset": "utf8mb4",
        }
        if ssl_enabled:
            # Un dict ``ssl`` no vacío fuerza TLS en pymysql. Sin material de CA en el
            # inventario, ciframos el transporte sin verificar el certificado
            # (equivalente a 'require'). La verificación de CA (verify-ca/verify-full)
            # requiere modelar el certificado del servidor — ver docs/plans/00.
            args["ssl"] = {"check_hostname": False}
        return args

    # postgresql (psycopg v3): connect_timeout + statement/lock timeout por sesión.
    args = {
        "connect_timeout": REMOTE_CONNECT_TIMEOUT,
        "options": (
            f"-c statement_timeout={REMOTE_STATEMENT_TIMEOUT_MS} "
            "-c lock_timeout=5000 "
            f"-c idle_in_transaction_session_timeout={REMOTE_STATEMENT_TIMEOUT_MS}"
        ),
    }
    if ssl_enabled:
        # psycopg aplica sslmode nativamente. Si el valor no es uno conocido, forzamos
        # 'require' (cifra el transporte) como mínimo seguro.
        args["sslmode"] = mode if mode in _PG_SSLMODES else "require"
    return args


def _build_engine(target: ServerTarget, effective_db: str | None) -> Engine:
    driver = _require_driver(target.dialect)
    url = URL.create(
        drivername=driver,
        username=target.admin_user,
        password=target.admin_password,
        host=target.host,
        port=target.port,
        database=effective_db,
    )
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args=_connect_args(target.dialect, target.ssl_mode),
    )


def get_engine(target: ServerTarget, database: str | None = None) -> Engine:
    """
    Devuelve un engine cacheado por (server_id, BD efectiva). `database=None`
    => conexión a nivel servidor (admin). Construye on-demand con NullPool.
    """
    effective_db = _effective_database(target.dialect, database)
    key = (target.server_id, effective_db or "")
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = _build_engine(target, effective_db)
            _engines[key] = engine
        return engine


@contextmanager
def server_connection(target: ServerTarget):
    """
    Conexión a NIVEL SERVIDOR (listar/crear/borrar BDs y usuarios). AUTOCOMMIT.
    MySQL: sin BD en la URL. PostgreSQL: conectado a 'postgres'.
    """
    engine = get_engine(target, None)
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def database_connection(target: ServerTarget, database: str):
    """Conexión a una BD CONCRETA (introspección de tablas/schema)."""
    engine = get_engine(target, database)
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def invalidate_server(server_id: int) -> None:
    """Descarta los engines de un servidor (rotación de credencial / borrado)."""
    with _lock:
        for key in [k for k in _engines if k[0] == server_id]:
            try:
                _engines[key].dispose()
            except Exception:
                pass
            del _engines[key]


def dispose_all() -> None:
    """Descarta todos los engines remotos (shutdown / lifespan)."""
    with _lock:
        for engine in _engines.values():
            try:
                engine.dispose()
            except Exception:
                pass
        _engines.clear()


# ---------------------------------------------------------------------------
# Traducción de errores del driver -> AppHttpException
# ---------------------------------------------------------------------------

_CONNECTION_FAILED = (502, "No se pudo conectar al servidor de base de datos destino.")
_TIMEOUT = (504, "La operación en el servidor destino excedió el tiempo de espera.")
_NOT_FOUND = (404, "El recurso solicitado no existe en el servidor destino.")
_CONFLICT = (409, "El recurso ya existe o tiene dependencias en el servidor destino.")
_FORBIDDEN = (403, "La credencial del gateway no tiene permisos para esta operación.")
_GENERIC = (500, "Ocurrió un error inesperado en el servidor destino.")

# MySQL/MariaDB: errno (int). PostgreSQL: SQLSTATE (str).
_ERROR_TABLE: dict[Any, tuple[int, str]] = {
    # --- MySQL / MariaDB (errno) ---
    2002: _CONNECTION_FAILED,
    2003: _CONNECTION_FAILED,
    2005: _CONNECTION_FAILED,
    1045: _CONNECTION_FAILED,  # access denied del propio admin: mala config del gateway
    2013: _TIMEOUT,           # lost connection during query (incl. timeouts)
    3024: _TIMEOUT,           # query execution interrupted (max_execution_time)
    1049: _NOT_FOUND,         # unknown database
    1008: _NOT_FOUND,         # can't drop database; doesn't exist
    1007: _CONFLICT,          # database exists
    1396: _CONFLICT,          # operation CREATE/DROP USER failed
    1044: _FORBIDDEN,
    1142: _FORBIDDEN,
    1143: _FORBIDDEN,
    1227: _FORBIDDEN,
    # --- PostgreSQL (SQLSTATE) ---
    "08000": _CONNECTION_FAILED,
    "08001": _CONNECTION_FAILED,
    "08004": _CONNECTION_FAILED,
    "08006": _CONNECTION_FAILED,
    "28000": _CONNECTION_FAILED,
    "28P01": _CONNECTION_FAILED,
    "57014": _TIMEOUT,        # query_canceled (statement_timeout)
    "3D000": _NOT_FOUND,      # invalid_catalog_name
    "42P04": _CONFLICT,       # duplicate_database
    "42710": _CONFLICT,       # duplicate_object
    "2BP01": _CONFLICT,       # dependent_objects_still_exist
    "42501": _FORBIDDEN,      # insufficient_privilege
}


def _extract_code(exc: Exception) -> Any | None:
    """
    SQLSTATE (psycopg) o errno (pymysql) del error original, si existe.
    Solo devuelve códigos "limpios" (errno int o SQLSTATE corto) para no volcar
    mensajes largos del driver (p.ej. el texto de "connection refused" de psycopg)
    dentro de remote_error_code.
    """
    orig = getattr(exc, "orig", None) or exc
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate:
        return sqlstate
    args = getattr(orig, "args", None)
    if args:
        code = args[0]
        if isinstance(code, int):
            return code
        if isinstance(code, str) and len(code) <= 5 and code.isalnum():
            return code
    return None


def map_driver_error(
    exc: Exception,
    *,
    op: str,
    target: ServerTarget | None = None,
    extra: dict[str, Any] | None = None,
) -> AppHttpException:
    """
    Traduce un error de driver/SQLAlchemy a AppHttpException con status code
    adecuado. `extra` SOLO debe contener claves no sensibles (db_name, username...).
    Nunca incluye password ni la URL de conexión.
    """
    code = _extract_code(exc)
    status, msg = _ERROR_TABLE.get(code, (None, None))

    if status is None:
        if isinstance(exc, TimeoutError):
            status, msg = _TIMEOUT
        elif isinstance(exc, OperationalError):
            # Fallo de conexión sin código claro (p.ej. psycopg "could not connect").
            status, msg = _CONNECTION_FAILED
        else:
            status, msg = _GENERIC

    context: dict[str, Any] = {"op": op}
    if target is not None:
        context.update(
            {
                "server_id": target.server_id,
                "host": target.host,
                "port": target.port,
                "dialect": target.dialect,
            }
        )
    if code is not None:
        context["remote_error_code"] = str(code)
    if extra:
        context.update(extra)

    return AppHttpException(message=msg, status_code=status, context=context)


# Re-export para que los adapters capturen un único tipo base.
DriverError = SQLAlchemyError
