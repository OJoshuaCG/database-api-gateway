"""
Copia de DATOS de tabla entre servidores (server-to-server), motor a motor o
cross-engine (MySQL/MariaDB/PostgreSQL). Parte de la feature "clonar base de datos".

A diferencia de ``snapshot_data`` (que RENDERIZA literales SQL con topes de filas/bytes
para un baseline versionado), este módulo hace una copia COMPLETA en streaming:

- LECTURA (origen): cursor en streaming (``stream_results=True`` + ``yield_per``),
  ``SELECT <cols> FROM <tabla>`` ordenado por PK cuando existe (lectura determinista),
  SIN LIMIT ni topes. La memoria se acota al tamaño del lote, nunca a la tabla entera.
  ``_iter_source_rows`` rinde TUPLAS con los valores ya adaptados (``_adapt_value``); es
  el ÚNICO lugar donde se lee el cursor origen (los tres writers lo consumen).
- ESCRITURA (destino): UNA sola conexión en AUTOCOMMIT para toda la fase, con el chequeo
  de FKs DESACTIVADO (y restaurado en ``finally``). El writer se elige por el DIALECTO REAL
  de la conexión destino (``dest_conn.dialect.name``), NO por el string de negocio
  ``dest_engine`` (ver ``copy_tables``):

    * PostgreSQL -> ``_copy_writer_postgres``: ``COPY ... FROM STDIN`` (psycopg3).
    * MySQL/MariaDB -> ``_copy_writer_mysql``: ``LOAD DATA LOCAL INFILE`` vía FIFO.
    * cualquier otro (sqlite en tests, o motor no cubierto) -> ``_copy_writer_insert``:
      INSERT parametrizado por lotes (executemany), la vía legacy.

  El kill switch ``CLONE_BULK_COPY_ENABLED=False`` fuerza el writer legacy para TODO.

SEGURIDAD: identificadores vía ``validate_identifier``/``quote_identifier``; los VALORES
de fila NUNCA se interpolan como literales SQL (INSERT parametrizado, o serializados y
escapados byte a byte para COPY/LOAD DATA, jamás concatenados en el texto de un statement);
credenciales viven solo en ``ServerTarget`` y jamás se loguean; los mensajes de error se
truncan y limpian de secretos (``_clean_error``).

Aislamiento por tabla (best-effort): una tabla que falla se marca ``failed`` con su
``error`` y el bucle CONTINÚA con la siguiente (el chequeo de FKs está apagado, así que
un orden parcial no rompe el resto). Cancelación cooperativa entre lotes.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterator

import pymysql.connections as _pymysql_conn
from pymysql.err import OperationalError as _PyMySQLOperationalError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.context import current_http_identifier
from app.core.environments import CLONE_BULK_COPY_ENABLED, CLONE_CONSISTENT_SNAPSHOT
from app.core.logger import get_logger
from app.core.remote_engine import (
    ServerTarget,
    database_connection,
    pooled_source_scope,
)
from app.exceptions import AppHttpException
from app.services.db_admin.identifiers import quote_identifier, validate_identifier

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Guard anti "rogue MySQL server" para LOAD DATA LOCAL INFILE (B1)             #
# --------------------------------------------------------------------------- #
# pymysql, con ``local_infile=True``, responde a la solicitud LOAD_LOCAL que manda el
# SERVIDOR abriendo y enviando el archivo cuyo path viene EN ESE PAQUETE
# (``LoadLocalPacketWrapper.filename``), NO el que el gateway puso en su SQL. Un servidor
# MySQL comprometido puede exigir, en respuesta a CUALQUIER query, el envío de un archivo
# ARBITRARIO del filesystem del proceso gateway (`.env`, la clave Fernet, /proc/self/environ),
# que comprometería las credenciales de TODOS los servidores gestionados — no solo el destino.
#
# Neutralización: envolvemos ``MySQLResult._read_load_local_packet`` para RECHAZAR (fail-closed)
# cualquier filename que no coincida EXACTAMENTE con el FIFO que el gateway espera para el
# LOAD DATA puntual que él mismo disparó. El path esperado vive en un thread-local que solo
# está "armado" durante la ventana exacta del ``exec_driver_sql(LOAD DATA)`` (mismo hilo que
# lee el paquete); en cualquier otro momento/hilo ``expected is None`` => se rechaza toda
# solicitud LOAD LOCAL inesperada.
#
# Verificado contra el código fuente de pymysql==1.1.2:
#   - ``connections.py::MySQLResult._read_load_local_packet`` usa ``load_packet.filename``
#     (del servidor) para construir ``LoadLocalFile`` -> ``open(filename, "rb")``.
#   - ``protocol.py::LoadLocalPacketWrapper.filename`` == ``packet.get_all_data()[1:]`` => es
#     SIEMPRE ``bytes``. Por eso comparamos normalizando ambos lados a bytes.
_expected_local_infile_path = threading.local()

_original_read_load_local_packet = _pymysql_conn.MySQLResult._read_load_local_packet


def _guarded_read_load_local_packet(self, first_packet):
    from pymysql.connections import LoadLocalPacketWrapper

    expected = getattr(_expected_local_infile_path, "path", None)
    requested = LoadLocalPacketWrapper(first_packet).filename

    # ``requested`` es ``bytes`` en pymysql 1.1.2; normalizamos ambos lados a bytes por
    # robustez ante versiones que devuelvan ``str``. ``expected`` (path del FIFO) es ASCII.
    requested_bytes = (
        requested.encode("utf-8") if isinstance(requested, str) else requested
    )
    expected_bytes = (
        expected.encode("utf-8")
        if isinstance(expected, str)
        else expected
    )

    if expected_bytes is None or requested_bytes != expected_bytes:
        # No dejamos que pymysql abra NADA: cortamos ANTES de tocar el filesystem, sin
        # llamar al método original. El mensaje no filtra el path del atacante.
        raise _PyMySQLOperationalError(
            0,
            "Solicitud LOAD DATA LOCAL rechazada: el servidor pidio un archivo distinto "
            "al esperado por el gateway (posible servidor MySQL comprometido). Ver B1 en "
            "la revision de seguridad del clon.",
        )
    return _original_read_load_local_packet(self, first_packet)


# Parcheamos UNA sola vez el MÉTODO de la clase (idempotente ante recargas del módulo: un
# segundo import no envuelve el wrapper sobre sí mismo). Solo afecta al método, no a
# instancias ya creadas — que además no existen a import-time.
if not getattr(_pymysql_conn.MySQLResult, "_gw_load_local_patched", False):
    _pymysql_conn.MySQLResult._read_load_local_packet = _guarded_read_load_local_packet
    _pymysql_conn.MySQLResult._gw_load_local_patched = True

# Firma común de un "writer" (consumidor de filas de origen que las vuelca al destino).
# ``counter`` es una lista de un elemento con el nº de filas YA volcadas al destino: el
# writer la actualiza tras cada lote/commit y ``_copy_one_table`` la lee para reportar el
# conteo (parcial en fallo/cancelación) de forma uniforme para los tres writers.
_Writer = Callable[
    [Any, str, "TableCopySpec", Iterator[tuple], int,
     Callable[[str, int], None] | None, Callable[[], bool] | None, list[int]],
    None,
]


@dataclass
class TableCopySpec:
    table: str
    columns: list[str]  # nombres de columna a copiar, en orden
    primary_key: list[str]  # [] si la tabla no tiene PK
    upsert: bool = False  # True => ON DUPLICATE KEY UPDATE / ON CONFLICT DO UPDATE
    # ¿La tabla tiene AL MENOS UNA clave única (índice UNIQUE o constraint)? El PK puede o
    # no estar reportado entre ellas y no importa: la decisión de staging es un ``or`` con
    # ``primary_key``, así que el caso "el único unique ES el PK" ya está cubierto.
    #
    # Existe porque sin ella una tabla SIN PK pero CON UNIQUE se cargaba directo a la tabla
    # final, donde el IGNORE implícito de ``LOAD DATA LOCAL`` descarta el conflicto EN
    # SILENCIO. Si algún día se quiere un upsert real sobre una clave única sin PK, va a
    # hacer falta la lista de columnas, no este booleano.
    has_unique_key: bool = False


@dataclass
class TableCopyResult:
    table: str
    status: str  # 'applied' | 'failed' | 'skipped' | 'canceled'
    rows_copied: int = 0
    error: str | None = None
    # Duración de la copia de ESTA tabla. Nunca se había calculado: los ítems de la fase de
    # datos llegaban al historial con ``execution_ms`` en NULL, así que el reporte podía decir
    # cuánto tardó cada sentencia de DDL pero no cuánto tardó copiar una tabla — justo el dato
    # que hace falta para responder «¿por qué tardó?».
    duration_ms: int = 0


class _Canceled(Exception):
    """Centinela interno: cancelación cooperativa. El conteo real vive en ``counter``."""


# --------------------------------------------------------------------------- #
# Helpers de error / valores / cancelación                                     #
# --------------------------------------------------------------------------- #
def _clean_error(exc: Exception) -> str:
    """Mensaje compacto y SIN secretos (misma estrategia que migrations._clean_error)."""
    orig = getattr(exc, "orig", None)
    msg = str(orig) if orig is not None else str(exc)
    return msg[:500]


def _is_canceled(cancel_cb: Callable[[], bool] | None) -> bool:
    return cancel_cb is not None and cancel_cb()


def _adapt_value(value):
    """
    Adapta un valor Python del driver ORIGEN para el driver DESTINO. La mayoría de los
    escalares (int/float/Decimal/bool/str/datetime/date/time) los adapta el DBAPI del
    destino directamente. Solo normalizamos:

    - dict/list  -> texto JSON (ni pymysql ni psycopg saben adaptar un dict/list crudo;
      un JSON válido en texto encaja en columnas JSON/JSONB/TEXT de ambos motores).
    - bytearray/memoryview -> bytes (forma canónica que ambos drivers aceptan para BLOB/BYTEA).

    LIMITACIÓN PREEXISTENTE (no la resuelve este módulo): una columna PostgreSQL de tipo
    ARRAY nativo (``int[]``/``text[]``) alimentada con un ``list`` Python queda aquí
    convertida a texto JSON, que NO es el literal de array de PostgreSQL. Afecta por igual
    al INSERT parametrizado legacy y al ``COPY FROM STDIN`` (ambos reciben el mismo valor
    ya adaptado). No es una regresión del cambio a bulk.

    Si un valor genuinamente no se puede adaptar, el volcado al destino lanzará y la
    tabla se marcará ``failed`` (best-effort), sin abortar el lote completo.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, timedelta):
        # El TIME de MySQL/MariaDB llega como ``timedelta`` y NINGÚN driver lo serializa bien:
        # ``pymysql.escape_timedelta`` aplica el signo solo a las HORAS, así que un
        # ``TIME '-01:30:00'`` se reinserta como ``-2:30:00`` y un ``-00:00:01`` como
        # ``-1:59:59``. Valores válidos, silenciosos, y distintos del original.
        #
        # Se normaliza acá —en el adaptador común a los dos writers— y no en cada uno, para que
        # el camino parametrizado y el de texto no puedan divergir: el destino recibe un str que
        # ambos drivers pasan tal cual. Es la única forma de que despachar una tabla a un writer
        # o al otro no cambie lo que queda escrito.
        return render_time_for_reinsert(value)
    return value


# --------------------------------------------------------------------------- #
# Construcción de SQL (validada + quoteada por dialecto)                        #
# --------------------------------------------------------------------------- #
def _q(name: str, engine_type: str) -> str:
    """Valida (whitelist ampliada, objetos preexistentes) y quotea un identificador."""
    validate_identifier(name, engine_type, "identificador", allow_existing=True)
    return quote_identifier(name, engine_type)


def _staging_name() -> str:
    """
    Nombre de tabla staging temporal. Solo sufijo aleatorio (NO el nombre de la tabla
    origen, para no rozar el límite de longitud de identificador de ningún motor): hay una
    sola staging viva por conexión de destino. Pasa la whitelist estricta (arranca con `_`,
    resto alfanumérico) y se quotea igual que cualquier otro identificador.
    """
    return f"_gw_stg_{uuid.uuid4().hex[:12]}"


def _build_select(engine_type: str, spec: TableCopySpec) -> str:
    cols_sql = ", ".join(_q(c, engine_type) for c in spec.columns)
    sql = f"SELECT {cols_sql} FROM {_q(spec.table, engine_type)}"
    if spec.primary_key:
        order = ", ".join(_q(c, engine_type) for c in spec.primary_key)
        sql += f" ORDER BY {order}"
    return sql


def _compose_insert(engine_type: str, spec: TableCopySpec, value_source: str) -> str:
    """
    Arma un ``INSERT INTO t (cols) <value_source>`` con el upsert del dialecto. El
    ``value_source`` es ``VALUES (:p0, ...)`` (INSERT parametrizado legacy) o
    ``SELECT cols FROM stg`` (upsert server-side desde la tabla staging del bulk-load).
    Toda la lógica de ``ON CONFLICT`` / ``ON DUPLICATE KEY`` / ``INSERT IGNORE`` vive
    SOLO aquí (no se duplica entre el INSERT y el INSERT ... SELECT).
    """
    table_q = _q(spec.table, engine_type)
    cols_q = ", ".join(_q(c, engine_type) for c in spec.columns)
    base = f"INSERT INTO {table_q} ({cols_q}) {value_source}"

    if not spec.upsert or not spec.primary_key:
        return base

    non_pk = [c for c in spec.columns if c not in spec.primary_key]
    if engine_type == "postgresql":
        pk_q = ", ".join(_q(c, engine_type) for c in spec.primary_key)
        if not non_pk:
            return f"{base} ON CONFLICT ({pk_q}) DO NOTHING"
        sets = ", ".join(
            f"{_q(c, engine_type)} = EXCLUDED.{_q(c, engine_type)}" for c in non_pk
        )
        return f"{base} ON CONFLICT ({pk_q}) DO UPDATE SET {sets}"

    # MySQL / MariaDB
    if not non_pk:
        return f"INSERT IGNORE INTO {table_q} ({cols_q}) {value_source}"
    updates = ", ".join(
        f"{_q(c, engine_type)} = VALUES({_q(c, engine_type)})" for c in non_pk
    )
    return f"{base} ON DUPLICATE KEY UPDATE {updates}"


def _build_insert(engine_type: str, spec: TableCopySpec) -> str:
    """
    INSERT parametrizado con placeholders posicionales ``:p0, :p1, ...`` (los nombres de
    columna NO se usan como bind params porque pueden contener ``. - $``). ``upsert=True``
    emite el upsert del dialecto; sin PK se degrada SIEMPRE a INSERT simple.
    """
    placeholders = ", ".join(f":p{i}" for i in range(len(spec.columns)))
    return _compose_insert(engine_type, spec, f"VALUES ({placeholders})")


def _build_insert_from_staging(engine_type: str, spec: TableCopySpec, staging: str) -> str:
    """
    ``INSERT INTO final (cols) SELECT cols FROM staging`` con el upsert del dialecto.
    UN SOLO statement server-side que mueve la staging (donde el bulk-load descargó las
    filas) a la tabla final resolviendo conflictos de PK. Reusa ``_compose_insert``.
    """
    cols_q = ", ".join(_q(c, engine_type) for c in spec.columns)
    staging_q = _q(staging, engine_type)
    return _compose_insert(engine_type, spec, f"SELECT {cols_q} FROM {staging_q}")


# --------------------------------------------------------------------------- #
# Desactivación / restauración de FKs y aislamiento (nivel sesión, best-effort) #
# --------------------------------------------------------------------------- #
def _set_read_committed(conn, engine_type: str) -> None:
    """
    Baja el aislamiento de la sesión de LECTURA a READ COMMITTED (best-effort). Una copia
    larga en REPEATABLE READ (default de MySQL/InnoDB y de PostgreSQL en una tx) mantiene
    un read-view/snapshot que impide el purge del undo → crece el history list del ORIGEN
    si tiene carga de escritura. No afecta la corrección: ya copiamos tabla por tabla, sin
    consistencia point-in-time cross-tabla. Si el SET falla, se ignora.
    """
    if engine_type in ("mysql", "mariadb"):
        sql = "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
    elif engine_type == "postgresql":
        sql = "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED"
    else:
        return
    try:
        conn.exec_driver_sql(sql)
    except SQLAlchemyError:
        pass  # best-effort


def _set_fk_enforcement(conn, engine_type: str, *, enabled: bool) -> None:
    """
    Activa/desactiva el chequeo de FKs para la SESIÓN de destino. MySQL/MariaDB usan
    ``FOREIGN_KEY_CHECKS``; PostgreSQL ``session_replication_role`` ('replica' apaga los
    triggers/FKs, requiere pseudo-root). Best-effort: si el SET falla (p.ej. motor que no
    lo soporta), se ignora — el orden topológico parent-first es la garantía primaria.
    """
    if engine_type in ("mysql", "mariadb"):
        sql = "SET FOREIGN_KEY_CHECKS=1" if enabled else "SET FOREIGN_KEY_CHECKS=0"
    elif engine_type == "postgresql":
        sql = (
            "SET session_replication_role = 'origin'"
            if enabled
            else "SET session_replication_role = 'replica'"
        )
    else:
        # Motor no soportado: error de programación del llamador (afecta a todo el lote).
        raise AppHttpException(
            message=f"Motor de base de datos no soportado: {engine_type}",
            status_code=422,
            context={"engine": engine_type},
        )
    try:
        conn.exec_driver_sql(sql)
    except SQLAlchemyError:
        pass  # best-effort


def _relax_strict_mode(conn, engine_type: str) -> str | None:
    """
    Quita ``STRICT_TRANS_TABLES``/``STRICT_ALL_TABLES`` del ``sql_mode`` de la sesión de
    ESCRITURA, solo durante la fase de datos del clon. Motivo (decisión de producto,
    ver ``docs/features/database-clone.md``): el origen puede tener filas "grandfathered"
    con un valor de ENUM fuera de su lista de valores permitidos — MySQL representa un
    valor inválido, insertado alguna vez sin modo estricto, como el string vacío ``''``
    (el "valor especial de error" del ENUM, documentado en "13.3.5 The ENUM Type").
    Reinsertar ese mismo ``''`` en un destino con modo estricto activo (default en MySQL
    8/MariaDB moderno) lo rechaza con error (1265) en vez de aceptarlo como hace el origen.

    Relaja SOLO esos dos modos (preserva el resto: ``ONLY_FULL_GROUP_BY``, ``NO_ZERO_DATE``,
    etc.) para reproducir fielmente el dato del origen — a costa de que CUALQUIER OTRO
    truncamiento de datos en CUALQUIER tabla/columna de este mismo job también se coercione
    en silencio en vez de fallar esa tabla (trade-off explícito, no accidental).

    Solo aplica a MySQL/MariaDB: PostgreSQL no tiene ``sql_mode`` ni esta ambigüedad de
    ENUM (sus enums son siempre estrictos). Best-effort: devuelve el ``sql_mode`` original
    (para restaurarlo con ``_restore_sql_mode``) o ``None`` si no aplica o el SET falla —
    en ese caso el comportamiento fail-closed actual se mantiene sin cambios.
    """
    if engine_type not in ("mysql", "mariadb"):
        return None
    try:
        original = conn.exec_driver_sql("SELECT @@SESSION.sql_mode").scalar()
        conn.exec_driver_sql(
            "SET SESSION sql_mode = REPLACE(REPLACE(@@SESSION.sql_mode, "
            "'STRICT_ALL_TABLES', ''), 'STRICT_TRANS_TABLES', '')"
        )
        return original
    except SQLAlchemyError:
        return None


def _restore_sql_mode(conn, original: str | None) -> None:
    """Contraparte de ``_relax_strict_mode``. No-op si no se relajó nada (``None``)."""
    if original is None:
        return
    try:
        conn.execute(text("SET SESSION sql_mode = :m"), {"m": original})
    except SQLAlchemyError:
        pass  # best-effort


# --------------------------------------------------------------------------- #
# Lectura del origen (único punto de lectura del cursor)                        #
# --------------------------------------------------------------------------- #
@contextmanager
def _source_conn_ctx(source_target, source_db, source_engine, src_conn):
    """
    Conexión de lectura del origen: la prestada por ``copy_tables``, o una propia.

    Cuando es prestada NO se cierra acá —es del llamador, y cerrarla tiraría el snapshot y las
    tablas que faltan— y tampoco se re-fija el aislamiento: el scope ya lo dejó como
    corresponde, y un ``SET SESSION`` a mitad de una transacción abierta es error.
    """
    if src_conn is not None:
        yield src_conn
        return
    with database_connection(source_target, source_db, bulk=True) as own:
        _set_read_committed(own, source_engine)
        yield own


def _iter_source_rows(
    *,
    source_target: ServerTarget,
    source_db: str,
    source_engine: str,
    select_sql: str,
    ncols: int,
    batch_rows: int,
    src_conn=None,
) -> Iterator[tuple]:
    """
    Generador: rinde TUPLAS de valores ya adaptados (``_adapt_value``) desde el ORIGEN en
    streaming. Es el ÚNICO lugar que consume el cursor origen; los writers solo iteran.

    ``bulk=True``: timeout de volcado (leer una tabla grande supera los 15s interactivos).
    ``READ COMMITTED``: una lectura larga en REPEATABLE READ pinnea un read-view e infla el
    undo/history del ORIGEN si tiene carga de escritura; la consistencia cross-tabla ya no
    está garantizada (FKs off, tabla por tabla).

    NOTA de hilos: para el writer de MySQL este generador se itera desde un HILO ESCRITOR
    (no el hilo que lo creó). Es seguro porque un solo hilo accede a la conexión origen a
    la vez; el generador abre y cierra su ``with database_connection`` dentro del mismo
    hilo que lo itera. ``close()`` es idempotente (un segundo ``close()`` no toca la
    conexión).
    """
    # ``src_conn``: la conexión que ``pooled_source_scope`` sostiene para toda la fase (la usa
    # el clon). Evita 103 handshakes y —cuando lleva snapshot consistente— hace que todas las
    # tablas salgan de la misma foto del origen. ``None`` = abrir una propia, que es el camino
    # de cualquier otro llamador y el de los tests.
    with _source_conn_ctx(source_target, source_db, source_engine, src_conn) as src:
        result = src.execution_options(
            stream_results=True, yield_per=batch_rows
        ).execute(text(select_sql))
        agotado = False
        try:
            for row in result:
                yield tuple(_adapt_value(row[i]) for i in range(ncols))
            agotado = True
        finally:
            if agotado or src_conn is not None:
                # Con la conexión COMPARTIDA hay que drenar aunque no se haya agotado:
                # invalidarla mataría el snapshot de las tablas que faltan. Drenar cuesta red en
                # el camino de fallo o cancelación, que es raro; leer filas sobrantes en la
                # tabla siguiente costaría datos mal, que no es negociable.
                result.close()
            else:
                # Salimos SIN drenar el cursor (cancelación, error del writer, o el consumidor
                # cortó). Con ``stream_results`` el driver deja filas pendientes en el socket y
                # ``result.close()`` las LEE Y DESCARTA: sobre una tabla grande es arrastrar
                # megabytes por la red para nada, y sobre una conexión que vuelve al pool es
                # peor — si el drenaje sale mal, la próxima tabla lee filas sobrantes de ésta.
                # No da error: da datos mal.
                #
                # ``invalidate()`` tira ESA conexión sin diálogo de protocolo, que es lo que el
                # ``NullPool`` regalaba al cerrar el socket. Antes esto se hacía con un
                # ``dispose()`` del pool ENTERO desde ``copy_tables``: impreciso, y con más de
                # una conexión habría tirado también las sanas de las otras tablas.
                src.invalidate()


# --------------------------------------------------------------------------- #
# Writer legacy: INSERT parametrizado por lotes (executemany)                   #
# --------------------------------------------------------------------------- #
def _copy_writer_insert(
    dest_conn,
    dest_engine: str,
    spec: TableCopySpec,
    rows_iter: Iterator[tuple],
    batch_rows: int,
    progress_cb: Callable[[str, int], None] | None,
    cancel_cb: Callable[[], bool] | None,
    counter: list[int],
) -> None:
    """
    Vía portable y de compatibilidad (sqlite en tests, motor no cubierto, o kill switch).
    Comportamiento IDÉNTICO al bucle histórico: chequeo de cancelación JUSTO antes de
    escribir cada lote (lote lleno o remanente final).
    """
    insert_sql = _build_insert(dest_engine, spec)
    ncols = len(spec.columns)
    table = spec.table
    batch: list[dict] = []

    def _flush() -> None:
        if _is_canceled(cancel_cb):
            raise _Canceled()
        dest_conn.execute(text(insert_sql), batch)
        counter[0] += len(batch)
        if progress_cb is not None:
            progress_cb(table, counter[0])

    for row in rows_iter:
        batch.append({f"p{i}": row[i] for i in range(ncols)})
        if len(batch) >= batch_rows:
            _flush()
            batch = []
    if batch:
        _flush()


# --------------------------------------------------------------------------- #
# Writer PostgreSQL: COPY ... FROM STDIN (psycopg3)                             #
# --------------------------------------------------------------------------- #
def _copy_writer_postgres(
    dest_conn,
    dest_engine: str,
    spec: TableCopySpec,
    rows_iter: Iterator[tuple],
    batch_rows: int,
    progress_cb: Callable[[str, int], None] | None,
    cancel_cb: Callable[[], bool] | None,
    counter: list[int],
) -> None:
    """
    ``COPY {tabla} ({cols}) FROM STDIN`` con ``cursor.copy().write_row(tupla)``. psycopg3
    usa los MISMOS dumpers que un ``execute`` parametrizado (no hay que pre-serializar más
    allá de ``_adapt_value``). Un fallo a mitad del ``with copy()`` aborta el COPY completo
    (0 filas commiteadas en AUTOCOMMIT, atómico) — dejamos que la excepción se propague.

    Upsert (spec.upsert + PK): COPY va a una TEMP TABLE staging y luego UN
    ``INSERT ... SELECT ... ON CONFLICT`` mueve todo a la final. Sin upsert, COPY va
    directo a la tabla final.
    """
    final_q = _q(spec.table, dest_engine)
    cols_q = ", ".join(_q(c, dest_engine) for c in spec.columns)
    use_staging = bool(spec.upsert and spec.primary_key)

    staging_name: str | None = None
    if use_staging:
        staging_name = _staging_name()
        staging_q = _q(staging_name, dest_engine)
        # TEMP TABLE PLANA (default ON COMMIT PRESERVE ROWS): en AUTOCOMMIT cada statement
        # es su propia transacción, así que ON COMMIT DROP/DELETE ROWS la destruiría/vaciaría
        # inmediatamente tras el CREATE. Se dropea explícitamente en el finally.
        dest_conn.exec_driver_sql(f"CREATE TEMP TABLE {staging_q} (LIKE {final_q})")
        load_target = staging_q
    else:
        load_target = final_q

    # ¿Llegaron las filas a la tabla FINAL? Con staging no basta con que el COPY haya
    # terminado: hasta el ``INSERT ... SELECT`` la final sigue vacía.
    volcado_a_final = not use_staging
    try:
        # Cursor psycopg CRUDO (no el de SQLAlchemy): la API COPY es del driver. En
        # AUTOCOMMIT el COPY es su propia transacción y no interfiere con los
        # exec_driver_sql de la misma conexión (no hay tx abierta).
        raw_conn = dest_conn.connection.driver_connection
        copy_sql = f"COPY {load_target} ({cols_q}) FROM STDIN"
        with raw_conn.cursor() as cur, cur.copy(copy_sql) as copy:
            n = 0
            for row in rows_iter:
                copy.write_row(row)
                n += 1
                counter[0] = n
                if n % batch_rows == 0:
                    # Cancelación: propagar dentro del with dispara el abort del COPY.
                    if _is_canceled(cancel_cb):
                        raise _Canceled()
                    if progress_cb is not None:
                        progress_cb(spec.table, n)

        if use_staging:
            resultado = dest_conn.exec_driver_sql(
                _build_insert_from_staging(dest_engine, spec, staging_name)
            )
            volcado_a_final = True
            # Segundo lugar donde se pueden perder filas. Con ``upsert`` el motor cuenta 2 por
            # fila actualizada, así que ahí solo se exige "no menos".
            _verificar_filas_cargadas(
                spec.table,
                enviadas=counter[0],
                cargadas=getattr(resultado, "rowcount", None),
                etapa="staging->final",
                solo_minimo=spec.upsert,
            )
        if progress_cb is not None:
            progress_cb(spec.table, counter[0])
    except BaseException:
        # Con staging, la tabla FINAL sigue VACÍA hasta que el ``INSERT ... SELECT`` termina.
        # Si salimos antes —por error del motor o por CANCELACIÓN, que salta desde adentro
        # del COPY— ``counter`` conserva las filas enviadas, y reportarlas es mentir: el
        # destino tiene cero.
        if use_staging and not volcado_a_final:
            counter[0] = 0
        raise
    finally:
        if staging_name is not None:
            try:
                dest_conn.exec_driver_sql(
                    f"DROP TABLE IF EXISTS {_q(staging_name, dest_engine)}"
                )
            except SQLAlchemyError:
                pass  # best-effort: la TEMP muere igual al cerrar la conexión


# --------------------------------------------------------------------------- #
# Writer MySQL/MariaDB: LOAD DATA LOCAL INFILE vía FIFO                         #
# --------------------------------------------------------------------------- #
def _fifo_dir() -> str:
    """Directorio para el FIFO: /dev/shm (tmpfs, no toca disco) si existe, si no tmp."""
    if os.path.isdir("/dev/shm"):
        return "/dev/shm"
    return tempfile.gettempdir()


def _escape_mysql_field(raw: bytes) -> bytes:
    """
    Escapa un valor (ya en bytes) para LOAD DATA con ``ESCAPED BY '\\'``. Orden CRÍTICO:
    backslash primero (si no, re-escaparíamos los que insertan tab/newline/cr).
    """
    raw = raw.replace(b"\\", b"\\\\")
    raw = raw.replace(b"\t", b"\\t")
    raw = raw.replace(b"\n", b"\\n")
    raw = raw.replace(b"\r", b"\\r")
    return raw


def render_time_for_reinsert(value: timedelta) -> str:
    """
    ``timedelta`` -> literal ``TIME`` **sin pérdida**: ``[-]HH:MM:SS[.ffffff]``.

    Es distinto de ``value_json.format_timedelta`` a propósito, y la diferencia importa. Aquel
    es el criterio de **presentación** del proyecto —consola SQL, resultados de migración,
    render de literales— y hace ``int(total_seconds())``, o sea **tira los microsegundos**. Para
    mostrarle un TIME a un humano eso es defendible; para COPIAR una columna ``TIME(3)`` o
    ``TIME(6)`` de una base a otra, no: ``01:02:03.123456`` se escribía como ``01:02:03`` y, peor,
    ``-00:00:00.500000`` se escribía como ``00:00:00``, perdiendo el valor **y el signo**. Como la
    fase de datos relaja ``STRICT_TRANS_TABLES`` a propósito, el motor lo aceptaba y la tabla se
    reportaba ``applied``.

    No se arregló ``format_timedelta`` porque tiene otros tres consumidores de presentación a los
    que cambiarles la salida sería una regresión visible en pantalla.

    El signo se aplica al TOTAL, nunca a las horas por separado — ése es justamente el defecto de
    ``pymysql.escape_timedelta``, que rendea ``-01:30:00`` como ``-2:30:00``. Y las horas NO se
    normalizan a 24: ``838:00:00`` es un valor legal del tipo.
    """
    negativo = value < timedelta(0)
    total = abs(value)
    horas, resto = divmod(total.seconds, 3600)
    horas += total.days * 24
    minutos, segundos = divmod(resto, 60)
    signo = "-" if negativo else ""
    base = f"{signo}{horas:02d}:{minutos:02d}:{segundos:02d}"
    # La fracción solo se emite si existe: un TIME(0) no debe salir con `.000000` de más.
    return f"{base}.{total.microseconds:06d}" if total.microseconds else base


def _render_mysql_field(value) -> bytes:
    """
    Serializa un valor Python (ya adaptado por ``_adapt_value``) al formato de campo de
    LOAD DATA. ``None`` -> ``\\N`` literal (sin escapar). El resto se codifica a bytes y se
    escapa byte a byte (incl. BLOB crudos). dict/list ya vienen como str JSON del adapter.
    """
    if value is None:
        return b"\\N"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _escape_mysql_field(bytes(value))
    if isinstance(value, bool):
        # MySQL espera 0/1 en texto (no 'True'/'False').
        return b"1" if value else b"0"
    if isinstance(value, str):
        return _escape_mysql_field(value.encode("utf-8"))
    if isinstance(value, timedelta):
        return render_time_for_reinsert(value).encode("ascii")
    # int/float/Decimal/datetime/date/time y demás escalares: repr textual canónico.
    return _escape_mysql_field(str(value).encode("utf-8"))


def _copy_writer_mysql(
    dest_conn,
    dest_engine: str,
    spec: TableCopySpec,
    rows_iter: Iterator[tuple],
    batch_rows: int,
    progress_cb: Callable[[str, int], None] | None,
    cancel_cb: Callable[[], bool] | None,
    counter: list[int],
) -> None:
    """
    ``LOAD DATA LOCAL INFILE`` alimentando al driver por un FIFO (no un archivo en disco):
    pymysql lee el "archivo" secuencialmente sin ``seek()``, así que un FIFO funciona como
    sustituto en tmpfs.

    Coordinación de hilos: abrir el FIFO en escritura BLOQUEA hasta que el lector (pymysql,
    dentro de ``cur.execute(LOAD DATA)``) lo abre. Por eso lanzamos el hilo escritor y el
    ``execute`` CONCURRENTEMENTE: el hilo escritor abre el FIFO (bloquea), el hilo principal
    ejecuta el LOAD DATA (pymysql abre el FIFO para lectura y desbloquea al escritor).

    STAGING SIEMPRE que haya PK (no solo con upsert). Motivo (doc oficial MySQL 8.0, "LOAD
    DATA … Duplicate-Key Handling"): el modificador LOCAL SIEMPRE se comporta como IGNORE
    ante duplicados (el servidor no puede cortar la transmisión del archivo a mitad), sin
    sintaxis para abortar. Cargar directo a la final "saltearía" en silencio un PK ya
    existente en destino, divergiendo del INSERT legacy (que falla la tabla con ER_DUP_ENTRY).
    Solución: cargar a una TEMPORARY staging que nace vacía en cada tabla → como las filas del
    ORIGEN ya son únicas entre sí (vienen de una tabla con su PK), el LOAD DATA hacia la
    staging JAMÁS choca (no hay filas previas), así que el IGNORE de LOCAL nunca se activa
    ahí. El conflicto REAL (contra datos ya existentes en la final) lo decide el statement
    final ``staging -> final`` de ``_build_insert_from_staging``:
      * upsert=False -> ``INSERT INTO final SELECT … FROM staging`` PLANO -> aborta con
        ER_DUP_ENTRY ante PK existente (idéntico al INSERT legacy).
      * upsert=True  -> ``… ON DUPLICATE KEY UPDATE``.
    Sin NINGUNA clave única (ni PK ni UNIQUE) no hay concepto de conflicto -> carga directo
    a la final, sin staging. Ojo que la condición es "PK **o** UNIQUE", no solo PK: una tabla
    sin PK pero con un índice UNIQUE secundario SÍ puede conflictuar, y cargarla directo
    dejaba que el IGNORE implícito de LOCAL descartara la fila en silencio (el INSERT legacy,
    en cambio, falla con 1062 — era una divergencia entre los dos writers). (En PostgreSQL, en cambio, COPY directo ya aborta atómicamente
    ante conflicto, así que allí staging sigue siendo solo-si-upsert.)
    """
    final_q = _q(spec.table, dest_engine)
    cols_q = ", ".join(_q(c, dest_engine) for c in spec.columns)
    use_staging = bool(spec.primary_key) or spec.has_unique_key

    staging_name: str | None = None
    if use_staging:
        staging_name = _staging_name()
        staging_q = _q(staging_name, dest_engine)
        # TEMPORARY vive toda la sesión y se autodestruye al cerrar la conexión; igual la
        # dropeamos explícitamente en el finally (la conexión se reusa para TODAS las tablas).
        dest_conn.exec_driver_sql(f"CREATE TEMPORARY TABLE {staging_q} LIKE {final_q}")
        load_target = staging_q
    else:
        load_target = final_q

    volcado_a_final = not use_staging
    try:
        cargadas = _load_data_via_fifo(
            dest_conn=dest_conn,
            load_target=load_target,
            cols_q=cols_q,
            table=spec.table,
            rows_iter=rows_iter,
            batch_rows=batch_rows,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
            counter=counter,
        )
        # El LOAD DATA ya terminó: acá se sabe si el motor se quedó con todas las filas.
        _verificar_filas_cargadas(spec.table, enviadas=counter[0], cargadas=cargadas)
        if use_staging:
            resultado = dest_conn.exec_driver_sql(
                _build_insert_from_staging(dest_engine, spec, staging_name)
            )
            volcado_a_final = True
            # El viaje staging -> final es el segundo lugar donde se pueden perder filas, y
            # con ``upsert`` el motor cuenta 2 por fila actualizada: solo se exige "no menos".
            _verificar_filas_cargadas(
                spec.table,
                enviadas=counter[0],
                cargadas=getattr(resultado, "rowcount", None),
                etapa="staging->final",
                solo_minimo=spec.upsert,
            )
        if progress_cb is not None:
            progress_cb(spec.table, counter[0])
    except BaseException:
        # Con staging, la tabla FINAL sigue VACÍA hasta que el ``INSERT ... SELECT`` termina.
        # Si salimos antes —por error del motor o por CANCELACIÓN, que salta desde adentro del
        # LOAD DATA— ``counter`` conserva las filas escritas al FIFO, y reportarlas es mentir:
        # el destino tiene cero. El camino de ERROR ya estaba cubierto; el de CANCELACIÓN no,
        # y es el más frecuente de los dos.
        if use_staging and not volcado_a_final:
            counter[0] = 0
        raise
    finally:
        if staging_name is not None:
            try:
                dest_conn.exec_driver_sql(
                    f"DROP TEMPORARY TABLE IF EXISTS {_q(staging_name, dest_engine)}"
                )
            except SQLAlchemyError:
                pass  # best-effort


class _FilasPerdidas(Exception):
    """Faltan filas en el destino. Se traduce a ``failed`` con el conteo en el mensaje."""


def _verificar_filas_cargadas(
    table: str,
    *,
    enviadas: int,
    cargadas: int | None,
    etapa: str = "LOAD DATA",
    solo_minimo: bool = False,
) -> None:
    """
    ¿El motor se quedó con todas las filas que le mandamos?

    **Por qué hace falta.** El resultado del ``LOAD DATA`` se descartaba sin mirarlo, y
    ``rows_copied`` cuenta filas escritas al FIFO, no filas insertadas. Con ``LOAD DATA LOCAL``
    comportándose SIEMPRE como ``IGNORE`` y con ``STRICT_TRANS_TABLES`` relajado durante toda
    la fase, un truncado de string, un DECIMAL redondeado, un ENUM que el destino no tiene o
    una colisión de clave única se convierten en warnings o filas descartadas **sin error**: el
    job reportaba éxito con datos perdidos.

    **El caso que nadie había visto**, y que motiva que esto valga también con staging: la
    staging se crea ``LIKE final``, así que hereda la collation del DESTINO. Si el índice único
    del destino es case-insensitive y el del origen case-sensitive, dos filas legítimas del
    origen (``'A'`` y ``'a'``) colisionan **dentro de la staging** y el IGNORE implícito
    descarta una. El docstring de ``_copy_writer_mysql`` afirma que "las filas del ORIGEN ya son
    únicas entre sí", y eso no es cierto cuando el destino angosta la collation.

    ``solo_minimo`` para el viaje staging→final con ``upsert``: MySQL cuenta **2** por fila
    actualizada con ``ON DUPLICATE KEY UPDATE``, así que exigir igualdad daría falsos
    positivos; ahí solo se exige que no falten.

    ``cargadas is None`` (el driver no expone ``rowcount``) no se trata como error: se prefiere
    no verificar antes que fallar una copia buena por una limitación del driver.
    """
    if cargadas is None:
        return
    suficiente = cargadas >= enviadas if solo_minimo else cargadas == enviadas
    if suficiente:
        return
    raise _FilasPerdidas(
        f"{etapa}: se enviaron {enviadas} filas de {table} y el motor registró {cargadas}. "
        "Faltan filas: probablemente descartadas en silencio por una clave única del destino "
        "o por una conversión de tipo."
    )


def _load_data_via_fifo(
    *,
    dest_conn,
    load_target: str,
    cols_q: str,
    table: str,
    rows_iter: Iterator[tuple],
    batch_rows: int,
    progress_cb: Callable[[str, int], None] | None,
    cancel_cb: Callable[[], bool] | None,
    counter: list[int],
) -> int | None:
    """
    Vuelca ``rows_iter`` al destino vía FIFO + ``LOAD DATA LOCAL``.

    Devuelve las filas que el motor dice haber cargado (``rowcount`` del paquete OK), o
    ``None`` si el driver no lo expone. Quien llama lo compara contra ``counter[0]``: ver
    ``_verificar_filas_cargadas``.
    """
    fifo_path = os.path.join(_fifo_dir(), f"gw_clone_{uuid.uuid4().hex}.tsv")
    os.mkfifo(fifo_path, 0o600)

    # El path es controlado por el gateway (uuid), no input de usuario; aun así lo pasamos
    # como literal escapado. El escape/terminadores son explícitos (no confiar en defaults):
    # campo=TAB, escape=backslash, línea=LF; deben coincidir con _render_mysql_field.
    # ``CHARACTER SET utf8mb4`` NO es decorativo y NO se puede omitir. ``LOAD DATA`` ignora el
    # charset de la CONEXIÓN (el ``SET NAMES`` de pymysql) e interpreta el archivo con
    # ``character_set_database``, o sea el default de la BASE destino. ``_render_mysql_field``
    # siempre emite UTF-8, así que contra una base creada ``latin1`` —y el gateway deja elegir
    # ``target_charset`` al crearla— el servidor reinterpretaba nuestros bytes y guardaba
    # mojibake: sin error, sin warning y con los conteos de filas cuadrando, o sea invisible
    # también para ``_verificar_filas_cargadas``.
    #
    # Reproducido contra MySQL 8.0 y MariaDB 11 con destino latin1: 'canción ñandú' quedaba
    # como 'canciÃ³n Ã±andÃº'. Lo cubre ``scripts/verify_data_writers_e2e.py``, cuyo destino se
    # crea latin1 a propósito.
    load_sql = (
        f"LOAD DATA LOCAL INFILE '{fifo_path}' INTO TABLE {load_target} "
        f"CHARACTER SET utf8mb4 "
        f"FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\' "
        f"LINES TERMINATED BY '\\n' ({cols_q})"
    )

    writer_exc: list[BaseException | None] = [None]
    canceled_flag = [False]
    # contextvars NO se propagan a hilos nuevos: capturamos el Request ID en ESTE hilo y lo
    # re-fijamos en el escritor para que cualquier log de progress_cb/cancel_cb lo lleve.
    req_id = current_http_identifier.get()

    def _writer() -> None:
        current_http_identifier.set(req_id)
        try:
            # 'wb': bloquea hasta que pymysql abra el FIFO para lectura (dentro del execute).
            with open(fifo_path, "wb") as fh:
                n = 0
                for row in rows_iter:
                    if n and n % batch_rows == 0:
                        if _is_canceled(cancel_cb):
                            # Cerrar el FIFO (salir del with) => EOF => LOAD DATA termina con
                            # las filas ya enviadas. Se trata como cancelación (parcial).
                            canceled_flag[0] = True
                            break
                        if progress_cb is not None:
                            progress_cb(table, n)
                    fh.write(b"\t".join(_render_mysql_field(v) for v in row) + b"\n")
                    n += 1
                    counter[0] = n
        except BaseException as exc:  # noqa: BLE001 - se re-lanza en el hilo principal
            writer_exc[0] = exc
        finally:
            # Cerrar el generador origen EN ESTE hilo (mismo hilo que lo iteró) para no
            # tocar la conexión origen desde el hilo principal.
            try:
                rows_iter.close()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_writer, name="gw-clone-fifo-writer", daemon=True)
    main_exc: BaseException | None = None
    # ARMAMOS el guard anti "rogue server" (B1): pymysql lee el paquete LOAD_LOCAL en ESTE
    # hilo (dentro de ``exec_driver_sql``), así que fijar el path esperado en este thread-local
    # justo antes basta. Fuera de esta ventana ``expected is None`` => cualquier solicitud
    # LOAD LOCAL se rechaza. Se limpia SIEMPRE en el finally (fail-closed).
    _expected_local_infile_path.path = fifo_path
    cargadas: list[int | None] = [None]
    try:
        thread.start()
        # Dispara el protocolo LOAD LOCAL: pymysql abre el FIFO para lectura y consume.
        resultado = dest_conn.exec_driver_sql(load_sql)
        # ``rowcount`` del paquete OK del LOAD DATA = Records - Skipped. Es lo ÚNICO que
        # distingue "se insertaron las filas" de "el IGNORE implícito descartó algunas".
        # Se lee acá y se compara afuera, para no confundir un fallo del motor con un faltante.
        cargadas[0] = getattr(resultado, "rowcount", None)
    except BaseException as exc:  # noqa: BLE001 - error del motor => tabla failed
        main_exc = exc
    finally:
        _expected_local_infile_path.path = None
        if thread.is_alive():
            # Si el LOAD DATA falló ANTES de abrir el FIFO (p. ej. tabla inexistente),
            # el escritor sigue bloqueado en open('wb'). Abrimos el extremo de lectura para
            # desbloquearlo; su write posterior fallará con BrokenPipe (capturado en _writer).
            try:
                fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass
        thread.join()
        try:
            os.unlink(fifo_path)
        except OSError:
            pass

    # Prioridad de errores: el del motor (LOAD DATA) primero; luego el del escritor.
    if main_exc is not None:
        raise main_exc
    if writer_exc[0] is not None:
        raise writer_exc[0]
    if canceled_flag[0]:
        raise _Canceled()
    return cargadas[0]


# --------------------------------------------------------------------------- #
# Copia de una tabla                                                           #
# --------------------------------------------------------------------------- #
def _copy_one_table(
    spec: TableCopySpec,
    *,
    source_target: ServerTarget,
    source_db: str,
    source_engine: str,
    dest_conn,
    dest_engine: str,
    writer: _Writer,
    batch_rows: int,
    progress_cb: Callable[[str, int], None] | None,
    cancel_cb: Callable[[], bool] | None,
    src_conn=None,
) -> TableCopyResult:
    table = spec.table
    try:
        # Validar/armar identificadores EAGERLY (fail-closed antes de tocar ningún motor).
        select_sql = _build_select(source_engine, spec)
        _build_insert(dest_engine, spec)
    except AppHttpException as exc:
        # Identificador anómalo => tabla fallida (fail-closed), no aborta el lote.
        return TableCopyResult(table=table, status="failed", error=_clean_error(exc))

    # Cronómetro de la tabla. Arranca acá y no antes: la validación de identificadores de
    # arriba no toca ningún motor, así que lo que se mide es el trabajo real (abrir la
    # conexión al origen, leer, serializar y escribir al destino).
    t_inicio = time.perf_counter()

    def _ms() -> int:
        return int((time.perf_counter() - t_inicio) * 1000)

    counter = [0]  # filas volcadas al destino (lo actualiza el writer)
    rows_iter = _iter_source_rows(
        source_target=source_target,
        source_db=source_db,
        source_engine=source_engine,
        select_sql=select_sql,
        ncols=len(spec.columns),
        batch_rows=batch_rows,
        src_conn=src_conn,
    )
    try:
        writer(
            dest_conn,
            dest_engine,
            spec,
            rows_iter,
            batch_rows,
            progress_cb,
            cancel_cb,
            counter,
        )
    except _Canceled:
        return TableCopyResult(
            table=table, status="canceled", rows_copied=counter[0], duration_ms=_ms()
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: aislar el fallo por tabla
        # La duración se reporta TAMBIÉN cuando la tabla falla o se cancela: una tabla que
        # tardó tres minutos antes de reventar es un dato de diagnóstico, y descartarlo dejaría
        # el reporte ciego justo en el caso que más se investiga.
        return TableCopyResult(
            table=table, status="failed", rows_copied=counter[0],
            error=_clean_error(exc), duration_ms=_ms(),
        )
    finally:
        # Cierra el cursor/conexión origen. Idempotente: si el writer de MySQL ya lo cerró
        # en su hilo, este close() es un no-op (no toca la conexión desde este hilo).
        try:
            rows_iter.close()
        except Exception:  # noqa: BLE001
            pass

    return TableCopyResult(
        table=table, status="applied", rows_copied=counter[0], duration_ms=_ms()
    )


# --------------------------------------------------------------------------- #
# Selección del writer (por el DIALECTO REAL de la conexión de destino)         #
# --------------------------------------------------------------------------- #
def _mysql_local_infile_enabled(dest_conn) -> bool:
    """
    ``SHOW VARIABLES LIKE 'local_infile'`` == 'ON' en el SERVIDOR destino. Es una
    comprobación de CAPACIDAD determinística (no un fallback oportunista ante fallos de
    datos): si el servidor no la tiene habilitada, LOAD DATA LOCAL fallaría siempre.
    """
    try:
        row = dest_conn.exec_driver_sql(
            "SHOW VARIABLES LIKE 'local_infile'"
        ).fetchone()
    except SQLAlchemyError:
        return False
    return bool(row) and str(row[1]).upper() == "ON"


def _resolve_writer(dest_conn) -> _Writer:
    """
    Elige el writer por ``dest_conn.dialect.name`` (dialecto REAL de la conexión), NO por
    el string de negocio ``dest_engine``: en tests con SQLite (que ejercen el camino de
    quoting/ON CONFLICT de PostgreSQL pasando ``dest_engine='postgresql'``) el dialecto
    real es 'sqlite' → cae en el writer legacy sin tocar el motor real. En producción,
    MySQL y MariaDB comparten driver (pymysql) → ambos dan 'mysql'.
    """
    if not CLONE_BULK_COPY_ENABLED:
        return _copy_writer_insert

    driver_dialect = dest_conn.dialect.name
    if driver_dialect == "postgresql":
        return _copy_writer_postgres
    if driver_dialect == "mysql":
        if _mysql_local_infile_enabled(dest_conn):
            return _copy_writer_mysql
        logger.warning(
            "Clon: 'local_infile' deshabilitado en el destino; se usa la copia por "
            "INSERT (legacy) en vez de LOAD DATA LOCAL INFILE."
        )
        return _copy_writer_insert
    # sqlite (tests) o cualquier motor no cubierto por un writer bulk.
    return _copy_writer_insert


# --------------------------------------------------------------------------- #
# Interfaz pública                                                             #
# --------------------------------------------------------------------------- #
def copy_tables(
    *,
    source_target: ServerTarget,
    source_db: str,
    source_engine: str,
    dest_target: ServerTarget,
    dest_db: str,
    dest_engine: str,
    specs: list[TableCopySpec],  # ya ordenadas topológicamente (padre primero)
    batch_rows: int = 1000,
    progress_cb: Callable[[str, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> list[TableCopyResult]:
    """
    Copia los datos de ``specs`` (en orden) del ORIGEN al DESTINO. Devuelve un resultado
    por tabla. No lanza por fallos de tabla (se capturan en el resultado); solo lanza
    ``AppHttpException`` ante errores de programación (p.ej. ``dest_engine`` no soportado).
    """
    batch_rows = max(1, int(batch_rows))
    results: list[TableCopyResult] = []

    # UNA conexión de destino para toda la fase, en AUTOCOMMIT, con FKs desactivadas.
    # ``bulk=True``: timeout de volcado (COPY/LOAD DATA de una tabla grande supera los 15s).
    # ``mysql_local_infile=True``: SOLO la conexión de ESCRITURA (destino) usa LOAD DATA LOCAL
    # INFILE. La de LECTURA del origen (``_iter_source_rows``) queda sin el flag para no ampliar
    # la superficie del ataque "rogue server" a los SELECT del origen (B1). En PostgreSQL el
    # flag es inerte (``_connect_args`` solo lo aplica en la rama mysql/mariadb).
    # UNA conexión de ORIGEN para toda la fase, vía pool acotado a este bloque. Antes se abría
    # una por tabla: con 103 tablas eso eran 103 handshakes completos —DNS sin caché incluido—
    # contra la base de producción de un tercero, y el costo fijo por tabla dominaba el tiempo
    # de la copia por encima de los datos en sí. El ``dispose()` del scope garantiza que no
    # quede una conexión ``sleep`` cuando el clon termina.
    with (
        pooled_source_scope(
            source_target, source_db, bulk=True,
            consistent=CLONE_CONSISTENT_SNAPSHOT,
        ) as src_conn,
        database_connection(
            dest_target, dest_db, bulk=True, mysql_local_infile=True
        ) as dest_conn,
    ):
        dest_conn = dest_conn.execution_options(isolation_level="AUTOCOMMIT")
        # El writer se resuelve UNA vez por job (el probe de local_infile es a nivel de
        # conexión, no por tabla) y se pasa hacia abajo ya resuelto.
        writer = _resolve_writer(dest_conn)
        _set_fk_enforcement(dest_conn, dest_engine, enabled=False)
        # Best-effort, solo MySQL/MariaDB: ver docstring de _relax_strict_mode. Devuelve
        # None (no-op en _restore_sql_mode) si no aplica al motor o si el SET falla.
        original_sql_mode = _relax_strict_mode(dest_conn, dest_engine)
        try:
            for idx, spec in enumerate(specs):
                if cancel_cb is not None and cancel_cb():
                    # Cancelado antes de empezar esta tabla: marca esta y el resto.
                    for pending in specs[idx:]:
                        results.append(
                            TableCopyResult(table=pending.table, status="canceled")
                        )
                    break

                res = _copy_one_table(
                    spec,
                    source_target=source_target,
                    source_db=source_db,
                    source_engine=source_engine,
                    dest_conn=dest_conn,
                    dest_engine=dest_engine,
                    writer=writer,
                    batch_rows=batch_rows,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                    src_conn=src_conn,
                )
                results.append(res)


                if res.status == "canceled":
                    # Cancelado a mitad de esta tabla: marca las RESTANTES como canceladas.
                    for pending in specs[idx + 1 :]:
                        results.append(
                            TableCopyResult(table=pending.table, status="canceled")
                        )
                    break
        finally:
            _restore_sql_mode(dest_conn, original_sql_mode)
            _set_fk_enforcement(dest_conn, dest_engine, enabled=True)

    return results
