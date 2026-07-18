"""
Copia de DATOS de tabla entre servidores (server-to-server), motor a motor o
cross-engine (MySQL/MariaDB/PostgreSQL). Parte de la feature "clonar base de datos".

A diferencia de ``snapshot_data`` (que RENDERIZA literales SQL con topes de filas/bytes
para un baseline versionado), este módulo hace una copia COMPLETA en streaming:

- LECTURA (origen): cursor en streaming (``stream_results=True`` + ``yield_per``),
  ``SELECT <cols> FROM <tabla>`` ordenado por PK cuando existe (lectura determinista),
  SIN LIMIT ni topes. La memoria se acota al tamaño del lote, nunca a la tabla entera.
- ESCRITURA (destino): UNA sola conexión en AUTOCOMMIT para toda la fase, con el chequeo
  de FKs DESACTIVADO (y restaurado en ``finally``). Los INSERT son SIEMPRE parametrizados
  (executemany con listas de dicts de bind params); NUNCA se interpola el valor de una
  fila (a prueba de inyección). Los identificadores se validan y quotean por dialecto.

SEGURIDAD: identificadores vía ``validate_identifier``/``quote_identifier``; valores
parametrizados; credenciales viven solo en ``ServerTarget`` y jamás se loguean; los
mensajes de error se truncan y limpian de secretos (``_clean_error``).

Aislamiento por tabla (best-effort): una tabla que falla se marca ``failed`` con su
``error`` y el bucle CONTINÚA con la siguiente (el chequeo de FKs está apagado, así que
un orden parcial no rompe el resto). Cancelación cooperativa entre lotes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.remote_engine import ServerTarget, database_connection
from app.exceptions import AppHttpException
from app.services.db_admin.identifiers import quote_identifier, validate_identifier


@dataclass
class TableCopySpec:
    table: str
    columns: list[str]  # nombres de columna a copiar, en orden
    primary_key: list[str]  # [] si la tabla no tiene PK
    upsert: bool = False  # True => ON DUPLICATE KEY UPDATE / ON CONFLICT DO UPDATE


@dataclass
class TableCopyResult:
    table: str
    status: str  # 'applied' | 'failed' | 'skipped' | 'canceled'
    rows_copied: int = 0
    error: str | None = None


# --------------------------------------------------------------------------- #
# Helpers de error / valores                                                   #
# --------------------------------------------------------------------------- #
def _clean_error(exc: Exception) -> str:
    """Mensaje compacto y SIN secretos (misma estrategia que migrations._clean_error)."""
    orig = getattr(exc, "orig", None)
    msg = str(orig) if orig is not None else str(exc)
    return msg[:500]


def _adapt_value(value):
    """
    Adapta un valor Python del driver ORIGEN para el driver DESTINO. La mayoría de los
    escalares (int/float/Decimal/bool/str/datetime/date/time) los adapta el DBAPI del
    destino directamente. Solo normalizamos:

    - dict/list  -> texto JSON (ni pymysql ni psycopg saben adaptar un dict/list crudo;
      un JSON válido en texto encaja en columnas JSON/JSONB/TEXT de ambos motores).
    - bytearray/memoryview -> bytes (forma canónica que ambos drivers aceptan para BLOB/BYTEA).

    Si un valor genuinamente no se puede adaptar, el ``execute`` del destino lanzará y la
    tabla se marcará ``failed`` (best-effort), sin abortar el lote completo.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


# --------------------------------------------------------------------------- #
# Construcción de SQL (validada + quoteada por dialecto)                        #
# --------------------------------------------------------------------------- #
def _q(name: str, engine_type: str) -> str:
    """Valida (whitelist ampliada, objetos preexistentes) y quotea un identificador."""
    validate_identifier(name, engine_type, "identificador", allow_existing=True)
    return quote_identifier(name, engine_type)


def _build_select(engine_type: str, spec: TableCopySpec) -> str:
    cols_sql = ", ".join(_q(c, engine_type) for c in spec.columns)
    sql = f"SELECT {cols_sql} FROM {_q(spec.table, engine_type)}"
    if spec.primary_key:
        order = ", ".join(_q(c, engine_type) for c in spec.primary_key)
        sql += f" ORDER BY {order}"
    return sql


def _build_insert(engine_type: str, spec: TableCopySpec) -> str:
    """
    INSERT parametrizado con placeholders posicionales ``:p0, :p1, ...`` (los nombres de
    columna NO se usan como bind params porque pueden contener ``. - $``). ``upsert=True``
    emite el upsert del dialecto; sin PK se degrada SIEMPRE a INSERT simple.
    """
    table_q = _q(spec.table, engine_type)
    cols_q = ", ".join(_q(c, engine_type) for c in spec.columns)
    placeholders = ", ".join(f":p{i}" for i in range(len(spec.columns)))
    base = f"INSERT INTO {table_q} ({cols_q}) VALUES ({placeholders})"

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
        return f"INSERT IGNORE INTO {table_q} ({cols_q}) VALUES ({placeholders})"
    updates = ", ".join(
        f"{_q(c, engine_type)} = VALUES({_q(c, engine_type)})" for c in non_pk
    )
    return f"{base} ON DUPLICATE KEY UPDATE {updates}"


# --------------------------------------------------------------------------- #
# Desactivación / restauración de FKs (nivel sesión, best-effort)              #
# --------------------------------------------------------------------------- #
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
    batch_rows: int,
    progress_cb: Callable[[str, int], None] | None,
    cancel_cb: Callable[[], bool] | None,
) -> TableCopyResult:
    table = spec.table
    try:
        select_sql = _build_select(source_engine, spec)
        insert_sql = _build_insert(dest_engine, spec)
    except AppHttpException as exc:
        # Identificador anómalo => tabla fallida (fail-closed), no aborta el lote.
        return TableCopyResult(table=table, status="failed", error=_clean_error(exc))

    ncols = len(spec.columns)
    rows_copied = 0

    def _canceled() -> bool:
        return cancel_cb is not None and cancel_cb()

    try:
        # Conexión de ORIGEN por tabla: aísla el cursor en streaming ante un fallo.
        with database_connection(source_target, source_db) as src:
            result = src.execution_options(
                stream_results=True, yield_per=batch_rows
            ).execute(text(select_sql))
            try:
                batch: list[dict] = []
                for row in result:
                    batch.append(
                        {f"p{i}": _adapt_value(row[i]) for i in range(ncols)}
                    )
                    if len(batch) >= batch_rows:
                        if _canceled():
                            return TableCopyResult(
                                table=table, status="canceled", rows_copied=rows_copied
                            )
                        dest_conn.execute(text(insert_sql), batch)
                        rows_copied += len(batch)
                        batch = []
                        if progress_cb is not None:
                            progress_cb(table, rows_copied)
                if batch:
                    if _canceled():
                        return TableCopyResult(
                            table=table, status="canceled", rows_copied=rows_copied
                        )
                    dest_conn.execute(text(insert_sql), batch)
                    rows_copied += len(batch)
                    if progress_cb is not None:
                        progress_cb(table, rows_copied)
            finally:
                result.close()
    except Exception as exc:  # noqa: BLE001 - best-effort: aislar el fallo por tabla
        return TableCopyResult(
            table=table, status="failed", rows_copied=rows_copied, error=_clean_error(exc)
        )

    return TableCopyResult(table=table, status="applied", rows_copied=rows_copied)


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
    with database_connection(dest_target, dest_db) as dest_conn:
        dest_conn = dest_conn.execution_options(isolation_level="AUTOCOMMIT")
        _set_fk_enforcement(dest_conn, dest_engine, enabled=False)
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
                    batch_rows=batch_rows,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
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
            _set_fk_enforcement(dest_conn, dest_engine, enabled=True)

    return results
