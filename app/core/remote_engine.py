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

import hashlib
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.core.environments import (
    REMOTE_BULK_STATEMENT_TIMEOUT_MS,
    REMOTE_CONNECT_TIMEOUT,
    REMOTE_STATEMENT_TIMEOUT_MS,
)
from app.core.net_guard import validate_remote_host
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

# Clave: (server_id, usuario, HUELLA de la contraseña, BD efectiva, bulk,
#         mysql_local_infile, timeout efectivo cuantizado).
#
# El USUARIO y la CONTRASEÑA son parte de la clave y no son opcionales. Casi todo el
# gateway conecta con la credencial pseudo-root del servidor, pero la consola SQL puede
# conectar como CUALQUIER usuario del motor para probar permisos, y la credencial viaja
# DENTRO de la URL del engine cacheado. Sin ambos en la clave, la herramienta miente en
# las dos direcciones:
#   - sin el usuario: la prueba "como usuario limitado" reusa el engine pseudo-root, corre
#     como root y da verde siempre;
#   - sin la contraseña: probar `app_ro` con una clave INCORRECTA reusa el engine de la
#     clave correcta y responde "conectó" — es decir, valida una credencial inválida.
# La contraseña entra como huella y nunca en claro: el diccionario es visible en cualquier
# volcado del proceso.
_engines: dict[tuple[int, str, str, str, bool, bool, int], Engine] = {}
_lock = threading.Lock()

# Cota del cache. La clave depende de valores que elige el CLIENTE (usuario, contraseña,
# timeout), así que sin techo un cliente puede sembrar engines indefinidamente; cada uno
# arrastra su propio cache de compilación. Con NullPool un engine no mantiene conexiones,
# así que desalojarlo solo cuesta reconstruir la URL.
_MAX_ENGINES = 256

# La huella se sala con un valor por PROCESO: sin sal, el diccionario permitiría confirmar
# una contraseña adivinada comparando hashes.
_PROCESS_SALT = os.urandom(16)


def _password_fingerprint(password: str | None) -> str:
    return hashlib.blake2b(
        (password or "").encode("utf-8"), digest_size=16, key=_PROCESS_SALT
    ).hexdigest()


def _quantize_timeout(timeout_ms: int) -> int:
    """
    Redondea el timeout hacia arriba en tramos de 5 s para que NO sea un eje libre de la
    clave del cache: ``timeout_ms`` lo elige el cliente en cada request y, sin cuantizar,
    cada valor distinto crea un engine nuevo.
    """
    if timeout_ms <= 0:
        return 0
    step = 5000
    return ((timeout_ms + step - 1) // step) * step


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


def _effective_timeout_ms(bulk: bool, statement_timeout_ms: int | None) -> int:
    """
    Timeout de sentencia efectivo. ``statement_timeout_ms`` explícito manda sobre el par
    interactivo/bulk: lo usa la consola SQL, cuyo tope es configurable por despliegue y
    no coincide ni con los 15s interactivos ni con la hora del volcado masivo.
    """
    if statement_timeout_ms is not None:
        return max(0, int(statement_timeout_ms))
    return REMOTE_BULK_STATEMENT_TIMEOUT_MS if bulk else REMOTE_STATEMENT_TIMEOUT_MS


def _connect_args(
    dialect: str,
    ssl_mode: str | None = None,
    *,
    bulk: bool = False,
    mysql_local_infile: bool = False,
    statement_timeout_ms: int | None = None,
) -> dict[str, Any]:
    mode = (ssl_mode or "").strip().lower()
    ssl_enabled = mode not in _SSL_DISABLED
    # ``bulk=True`` (copia de datos del clon): usa un timeout mucho mayor que el
    # interactivo (15s cancelaría lotes de tablas grandes). ``0`` = sin límite.
    stmt_timeout_ms = _effective_timeout_ms(bulk, statement_timeout_ms)

    if dialect in ("mysql", "mariadb"):
        # pymysql: timeouts a nivel de socket cubren conexión y ejecución.
        args: dict[str, Any] = {
            "connect_timeout": REMOTE_CONNECT_TIMEOUT,
            "charset": "utf8mb4",
        }
        if mysql_local_infile:
            # LOAD DATA LOCAL INFILE (vía FIFO) exige habilitar explícitamente el envío de
            # archivos locales o pymysql lanza RuntimeError al recibir la solicitud del
            # servidor. Es un flag DEDICADO (desacoplado de ``bulk``): SOLO la conexión de
            # ESCRITURA del destino en la copia de datos lo pide. La de LECTURA del origen
            # también es ``bulk=True`` pero solo hace SELECT y NO debe habilitarlo — con
            # ``local_infile=True`` un servidor ORIGEN comprometido podría exigir el envío de
            # un archivo local del gateway en respuesta a CUALQUIER query (ataque "rogue
            # MySQL server"). Minimizar la superficie: el flag se activa en la MENOR cantidad
            # de conexiones posible. Aun cuando está activo, el guard de
            # ``data_copy._guarded_read_load_local_packet`` neutraliza el ataque restringiendo
            # el archivo servible al FIFO esperado.
            args["local_infile"] = True
        # 0 => sin read/write_timeout (socket sin límite de tiempo de operación).
        if stmt_timeout_ms > 0:
            stmt_timeout_s = max(1, stmt_timeout_ms // 1000)
            args["read_timeout"] = stmt_timeout_s
            args["write_timeout"] = stmt_timeout_s
        if ssl_enabled:
            # Un dict ``ssl`` no vacío fuerza TLS en pymysql. Sin material de CA en el
            # inventario, ciframos el transporte sin verificar el certificado
            # (equivalente a 'require'). La verificación de CA (verify-ca/verify-full)
            # requiere modelar el certificado del servidor — ver docs/plans/00.
            args["ssl"] = {"check_hostname": False}
        return args

    # postgresql (psycopg v3): connect_timeout + statement/lock timeout por sesión.
    # statement_timeout=0 => sin límite (lo usa el modo bulk para tablas grandes).
    args = {
        "connect_timeout": REMOTE_CONNECT_TIMEOUT,
        "options": (
            f"-c statement_timeout={stmt_timeout_ms} "
            "-c lock_timeout=5000 "
            f"-c idle_in_transaction_session_timeout={stmt_timeout_ms}"
        ),
    }
    if ssl_enabled:
        # psycopg aplica sslmode nativamente. Si el valor no es uno conocido, forzamos
        # 'require' (cifra el transporte) como mínimo seguro.
        args["sslmode"] = mode if mode in _PG_SSLMODES else "require"
    return args


def _build_engine(
    target: ServerTarget,
    effective_db: str | None,
    *,
    bulk: bool = False,
    mysql_local_infile: bool = False,
    statement_timeout_ms: int | None = None,
) -> Engine:
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
        connect_args=_connect_args(
            target.dialect,
            target.ssl_mode,
            bulk=bulk,
            mysql_local_infile=mysql_local_infile,
            statement_timeout_ms=statement_timeout_ms,
        ),
    )


def get_engine(
    target: ServerTarget,
    database: str | None = None,
    *,
    bulk: bool = False,
    mysql_local_infile: bool = False,
    statement_timeout_ms: int | None = None,
) -> Engine:
    """
    Devuelve un engine cacheado por (server_id, usuario, BD efectiva, bulk,
    mysql_local_infile).
    `database=None` => conexión a nivel servidor (admin). `bulk=True` => timeouts de
    volcado masivo (copia de datos del clon). `mysql_local_infile=True` => habilita
    LOAD DATA LOCAL INFILE en pymysql (solo la conexión de ESCRITURA del clon lo pide).
    Ambos flags entran en la clave de cache para que un engine con ``local_infile``
    habilitado NUNCA se reuse en una conexión que no lo pidió (ni viceversa); el usuario
    entra por la razón de seguridad documentada en ``_engines``.
    """
    effective_db = _effective_database(target.dialect, database)
    timeout = _quantize_timeout(_effective_timeout_ms(bulk, statement_timeout_ms))
    key = (
        target.server_id,
        target.admin_user or "",
        _password_fingerprint(target.admin_password),
        effective_db or "",
        bulk,
        mysql_local_infile,
        timeout,
    )
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = _build_engine(
                target,
                effective_db,
                bulk=bulk,
                mysql_local_infile=mysql_local_infile,
                statement_timeout_ms=timeout if statement_timeout_ms is not None else None,
            )
            # Desalojo FIFO al llegar al techo (ver ``_MAX_ENGINES``).
            while len(_engines) >= _MAX_ENGINES:
                oldest_key, evicted = next(iter(_engines.items()))
                del _engines[oldest_key]
                try:
                    evicted.dispose()
                except Exception:  # noqa: BLE001 — desalojar nunca rompe la operación
                    pass
            _engines[key] = engine
        return engine


@contextmanager
def server_connection(target: ServerTarget):
    """
    Conexión a NIVEL SERVIDOR (listar/crear/borrar BDs y usuarios). AUTOCOMMIT.
    MySQL: sin BD en la URL. PostgreSQL: conectado a 'postgres'.

    SEGURIDAD (anti-SSRF, R2): se revalida el host JUSTO antes de conectar, no solo al
    registrar el servidor. El driver re-resuelve DNS en cada conexión (NullPool), así que
    validar aquí cierra la ventana de DNS-rebinding para TODOS los endpoints de motor.
    """
    validate_remote_host(target.host)
    engine = get_engine(target, None)
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def database_connection(
    target: ServerTarget,
    database: str,
    *,
    bulk: bool = False,
    mysql_local_infile: bool = False,
    statement_timeout_ms: int | None = None,
):
    """Conexión a una BD CONCRETA (introspección/migraciones). Revalida el host (anti-SSRF, R2).
    ``bulk=True`` usa timeouts de volcado masivo (copia de datos del clon).
    ``mysql_local_infile=True`` habilita LOAD DATA LOCAL INFILE (solo la conexión de
    ESCRITURA del clon lo pide; ver ``data_copy``).
    ``statement_timeout_ms`` fuerza un timeout explícito (lo usa la consola SQL)."""
    validate_remote_host(target.host)
    engine = get_engine(
        target,
        database,
        bulk=bulk,
        mysql_local_infile=mysql_local_infile,
        statement_timeout_ms=statement_timeout_ms,
    )
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
