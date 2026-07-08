"""
Render de datos-semilla de un snapshot (SEGURIDAD CRÍTICA).

Los valores de las filas se persisten como LITERALES SQL y luego se ejecutan con
``op.execute`` (no parametrizado): son una superficie de inyección. Todo valor pasa por
``render_value`` con manejo tipado exhaustivo + ``quote_string_literal``; los tipos
desconocidos fallan cerrado (la tabla se omite, nunca se emite SQL dudoso).

Genera:
- ``up_sql``  : INSERT idempotente por lotes (upsert). MySQL ``ON DUPLICATE KEY UPDATE``;
  PostgreSQL ``ON CONFLICT (pk) DO UPDATE/NOTHING``. Idempotente porque el baseline se
  aplica sobre N bases y puede re-ejecutarse.
- ``down_sql``: ``DELETE ... WHERE (pk) IN (...)`` por PK — rollback exacto de lo
  insertado (posible porque conocemos las filas), sin tocar filas ajenas.

TECHOS DUROS no-override: protegen la BD de metadatos y la memoria del gateway aun si
las variables de entorno se configuran demasiado altas.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal

from app.services.db_admin.dtos import SeedResult
from app.services.db_admin.identifiers import quote_identifier, quote_string_literal

# Techos duros (no override-ables por env var).
HARD_MAX_ROWS = 5000
HARD_MAX_BYTES = 5 * 1024 * 1024

_MODES = ("upsert", "insert_ignore")


class UnsupportedValueError(Exception):
    """Un valor de tipo no soportado para render como literal (fail-closed → skip)."""


def effective_limits(max_rows: int, max_bytes: int) -> tuple[int, int]:
    """Aplica los techos duros a los límites configurados por el admin/env."""
    return min(int(max_rows), HARD_MAX_ROWS), min(int(max_bytes), HARD_MAX_BYTES)


def render_value(value, dialect: str) -> str:
    """
    Renderiza un valor Python como literal SQL seguro para ``dialect``. Tipos no
    soportados → ``UnsupportedValueError`` (fail-closed). NUNCA interpola sin escapar.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if dialect == "postgresql":
            return "TRUE" if value else "FALSE"
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise UnsupportedValueError("float no finito")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedValueError("decimal no finito")
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        hexs = bytes(value).hex()
        # PG: decode(...,'hex') es independiente de standard_conforming_strings.
        return f"decode('{hexs}', 'hex')" if dialect == "postgresql" else f"x'{hexs}'"
    if isinstance(value, datetime):
        return quote_string_literal(value.isoformat(sep=" "), dialect)
    if isinstance(value, (date, time)):
        return quote_string_literal(value.isoformat(), dialect)
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        s = value
    else:
        raise UnsupportedValueError(type(value).__name__)
    # El byte nulo se rechaza como skip (consistente con los tipos no soportados), no
    # como 422 que abortaría toda la petición.
    if "\x00" in s:
        raise UnsupportedValueError("null_byte")
    return quote_string_literal(s, dialect)


def _chunks(seq: list, size: int):
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _upsert_statement(
    dialect: str, table_q: str, cols_q: str, values_sql: str,
    columns: list[str], pk: list[str], mode: str,
) -> str:
    non_pk = [c for c in columns if c not in pk]
    if dialect == "postgresql":
        pk_q = ", ".join(quote_identifier(c, dialect) for c in pk)
        if mode == "insert_ignore" or not non_pk:
            conflict = f"ON CONFLICT ({pk_q}) DO NOTHING"
        else:
            sets = ", ".join(
                f"{quote_identifier(c, dialect)} = EXCLUDED.{quote_identifier(c, dialect)}"
                for c in non_pk
            )
            conflict = f"ON CONFLICT ({pk_q}) DO UPDATE SET {sets}"
        return f"INSERT INTO {table_q} ({cols_q})\nVALUES\n  {values_sql}\n{conflict}"
    # MySQL/MariaDB
    if mode == "insert_ignore" or not non_pk:
        return f"INSERT IGNORE INTO {table_q} ({cols_q})\nVALUES\n  {values_sql}"
    updates = ", ".join(
        f"{quote_identifier(c, dialect)} = VALUES({quote_identifier(c, dialect)})"
        for c in non_pk
    )
    return (
        f"INSERT INTO {table_q} ({cols_q})\nVALUES\n  {values_sql}\n"
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


def _delete_statements(
    dialect: str, table_q: str, pk: list[str], pk_rendered: list[list[str]], batch_rows: int
) -> str:
    """DELETE por PK (soporta PK compuesta con tuplas). En orden inverso al insert."""
    pk_q = ", ".join(quote_identifier(c, dialect) for c in pk)
    stmts: list[str] = []
    single = len(pk) == 1
    for batch in _chunks(pk_rendered, batch_rows):
        if single:
            in_list = ", ".join(vals[0] for vals in batch)
            stmts.append(f"DELETE FROM {table_q} WHERE {pk_q} IN ({in_list})")
        else:
            tuples = ", ".join("(" + ", ".join(vals) + ")" for vals in batch)
            stmts.append(f"DELETE FROM {table_q} WHERE ({pk_q}) IN ({tuples})")
    return ";\n".join(stmts) + ";"


def build_seed(
    *,
    dialect: str,
    table: str,
    columns: list[str],
    pk: list[str],
    rows,
    mode: str,
    batch_rows: int,
    max_rows: int,
    max_bytes: int,
) -> SeedResult:
    """
    Renderiza las filas como INSERT idempotente + DELETE por PK, con topes de filas/bytes.

    ``rows`` es un ITERABLE de filas (idealmente un resultado en streaming) alineadas con
    ``columns``. Los topes se controlan DURANTE la iteración para acotar la memoria: se
    aborta en cuanto se supera ``max_rows`` (fila nº ``max_rows+1``) o ``max_bytes`` (una
    fila con BLOB/JSON grande puede superarlo con pocas filas), SIN materializar todo el
    resultado. Un valor de tipo no soportado o con byte nulo omite la tabla (fail-closed).
    """
    if mode not in _MODES:
        mode = "upsert"

    pk_idx = [columns.index(c) for c in pk]
    rendered_rows: list[list[str]] = []
    pk_rendered: list[list[str]] = []
    total = 0
    count = 0
    for row in rows:
        count += 1
        if count > max_rows:
            return SeedResult(table=table, included=False, reason="oversize_rows")
        try:
            vals = [render_value(v, dialect) for v in row]
        except UnsupportedValueError as exc:
            return SeedResult(
                table=table, included=False, reason=f"unsupported_type:{exc}"
            )
        total += sum(len(v) + 2 for v in vals)
        if total > max_bytes:
            return SeedResult(table=table, included=False, reason="oversize_bytes")
        rendered_rows.append(vals)
        pk_rendered.append([vals[i] for i in pk_idx])

    if not rendered_rows:
        return SeedResult(table=table, included=False, reason="no_rows")

    table_q = quote_identifier(table, dialect)
    cols_q = ", ".join(quote_identifier(c, dialect) for c in columns)
    up_stmts: list[str] = []
    for batch in _chunks(rendered_rows, batch_rows):
        values_sql = ",\n  ".join("(" + ", ".join(vals) + ")" for vals in batch)
        up_stmts.append(
            _upsert_statement(dialect, table_q, cols_q, values_sql, columns, pk, mode)
        )
    up_sql = ";\n\n".join(up_stmts) + ";"
    # Rollback: borrar en orden inverso (simetría con el insert por lotes).
    down_sql = _delete_statements(dialect, table_q, pk, list(reversed(pk_rendered)), batch_rows)

    return SeedResult(
        table=table, included=True, reason=None, row_count=len(rendered_rows),
        primary_key=pk, up_sql=up_sql, down_sql=down_sql,
    )
