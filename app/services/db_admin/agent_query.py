"""
Ejecución de las lecturas de DATOS del agente: ``sample_rows``, ``distinct_values``, ``count_rows``.

LA SEGURIDAD ESTÁ EN EL MOTOR, ESTO ES LA CAPA DE ARRIBA
--------------------------------------------------------
Estas funciones corren con la credencial de DATOS de UNA base (cuenta con ``SELECT`` solo sobre ella,
verificada por la sonda), dentro de ``START TRANSACTION READ ONLY`` y con ROLLBACK siempre. Lo que
hace este módulo es lo que el motor no hace por sí solo: acotar filas, tiempo y bytes, auditar y
devolver un sobre que no se pueda leer como instrucción.

**Las tools reciben IDENTIFICADORES, nunca SQL.** El texto se arma acá, sobre un AST con
identificadores cuoteados (``exp.to_identifier``), y AUN ASÍ pasa por ``validate_agent_select``: hay
UN solo camino hacia el motor y una consulta armada por el gateway no tiene una puerta lateral.

DECISIONES QUE PARECEN DETALLES
-------------------------------
- **Los identificadores se validan contra el catálogo ANTES de abrir la conexión de datos** (S12). Un
  identificador desconocido sale como ``UNKNOWN_IDENTIFIER`` sin que la cuenta de datos conecte ni
  ejecute nada. La lectura del catálogo usa la credencial de estructura de siempre
  (``open_readonly``).
- **El timeout se aplica DEL LADO DEL SERVIDOR** (``max_execution_time`` en MySQL,
  ``max_statement_time`` en MariaDB, ``statement_timeout`` en PostgreSQL) y además hay un vigilante:
  un ``threading.Timer`` que, pasado el timeout más 2 s, abre una SEGUNDA conexión con la misma
  credencial y mata la sentencia (``KILL QUERY`` / ``pg_cancel_backend``). Existe porque el límite
  de sesión es de mejor esfuerzo (MySQL solo aplica ``max_execution_time`` a ``SELECT`` de nivel
  superior; un MariaDB dado de alta como MySQL ignora la variable) y un ``SELECT`` que no termina
  retiene una conexión y undo en la base de un tercero.
- **El presupuesto de bytes RECORTA FILAS**, no falla (D11): el tope duro del despacho (512 KiB) sigue
  ahí como red de abajo, pero un resultado grande es un resultado truncado con ``human_query``, no
  un error que el agente reintente en loop.
- **Los errores salen solo con códigos cerrados** (S22). Nunca ``str(exc)``: un error del driver
  puede llevar el valor de una fila. El detalle va al log sin el SQL ni los datos.
- **``record_intent`` ANTES de conectar** y fail-closed (S21): si la auditoría no puede escribir, no
  hay conexión. Después se registra el resultado con el hash de la consulta, no con el texto: el
  texto se guarda enmascarado (literales -> ``?``) y acotado.
"""

import json
import re
import threading
import time
from dataclasses import dataclass

from sqlglot import exp

from app.core import environments as env
from app.core.logger import get_logger
from app.core.remote_engine import database_connection
from app.exceptions import AppHttpException
from app.services import audit
from app.services import mcp_catalog as codes
from app.services.db_admin import agent_sql_policy as policy
from app.services.db_admin.query_policy import READ, StatementPlan
from app.services.db_admin.query_runner import (
    MODE_STORED,
    QueryCredential,
    effective_target,
    run_statements,
)
from app.services.db_admin.statement_limits import apply_postgres_statement_timeout

logger = get_logger(__name__)

#: Cada celda se recorta a esta cantidad de caracteres (``json_value``) antes de medir bytes.
MAX_CELL_CHARS = 512
#: Holgura del vigilante sobre el timeout del motor: da tiempo a que el límite de SESIÓN actúe solo.
WATCHDOG_GRACE_SECONDS = 2
#: Tope de caracteres del SQL enmascarado que se guarda en la auditoría.
AUDIT_SQL_MAX_BYTES = 2048
#: Bytes reservados para las llaves y comas del sobre al medir el presupuesto de filas.
_ENVELOPE_SLACK_BYTES = 256

_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres"}
_AUDIT_ACTION = "mcp.agent_query"

#: Control fuera (salvo salto de línea y tabulador). Misma regla que ``app.mcp.tools._envelope.clean``:
#: este módulo no puede importar el paquete del MCP (capas), y un test fija que ambas coinciden.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Marcas con las que ``json_value`` recorta una celda de texto o binaria.
_CLIPPED_RE = re.compile(r"… \(truncado, \d+ caracteres\)$|… \(\d+ bytes\)$")

#: Códigos del motor que significan "se pasó del tiempo": 3024 (MySQL max_execution_time), 1969
#: (MariaDB max_statement_time), 1317 (KILL QUERY), 57014 (PostgreSQL query_canceled).
_TIMEOUT_CODES = frozenset({"3024", "1969", "1317", "57014"})
#: Conexión perdida durante la consulta: es un timeout SOLO si pasó (casi) todo el tiempo, porque el
#: timeout de socket de la conexión es el mismo y compite con el del servidor.
_LOST_CONNECTION_CODES = frozenset({"2013", "2006"})
#: El motor rechazó la conexión por el tope de la CUENTA, no porque la credencial sea inválida:
#: 1226 (``MAX_USER_CONNECTIONS``, conexiones simultáneas) y 1203 (conexiones por hora).
_ACCOUNT_BUSY_CODES = frozenset({"1226", "1203"})

_MESSAGES = {
    codes.REASON_QUERY_TIMEOUT: "La consulta superó el tiempo máximo y se canceló.",
    codes.REASON_QUERY_FAILED: "La consulta no se pudo ejecutar.",
    codes.REASON_AUDIT_UNAVAILABLE: "No se pudo registrar la auditoría; la consulta no se ejecutó.",
    codes.REASON_UNKNOWN_IDENTIFIER: (
        "La tabla o la columna no existe en el catálogo de la base. Se pueden listar con "
        "list_objects y get_schema."
    ),
    codes.REASON_MALFORMED_REQUEST: "Los argumentos de la tool no tienen la forma esperada.",
    codes.REASON_PROBE_NOT_GREEN: "La credencial de datos de la base ya no está habilitada.",
    codes.REASON_DATA_ACCOUNT_BUSY: (
        "La cuenta de datos de la base está ocupada: hay demasiadas consultas en curso. "
        "La credencial está bien; reintentá en unos segundos."
    ),
}
_STATUS = {
    codes.REASON_QUERY_TIMEOUT: 504,
    codes.REASON_QUERY_FAILED: 502,
    codes.REASON_AUDIT_UNAVAILABLE: 503,
    codes.REASON_UNKNOWN_IDENTIFIER: 404,
    codes.REASON_MALFORMED_REQUEST: 422,
    codes.REASON_PROBE_NOT_GREEN: 403,
    codes.REASON_DATA_ACCOUNT_BUSY: 429,
}


def query_error(code: str) -> AppHttpException:
    """
    Error de tool con un código PÚBLICO cerrado y un mensaje fijo. Nunca lleva texto del motor, del
    driver ni de las filas (S22): es lo único que sale de este módulo hacia el agente.
    """
    return AppHttpException(
        message=_MESSAGES[code], status_code=_STATUS[code], public_context={"code": code}
    )


# --------------------------------------------------------------------------- #
# Límites                                                                      #
# --------------------------------------------------------------------------- #


def effective_limit(raw) -> tuple[int, list[str]]:
    """
    Filas que se piden: ausente -> ``MCP_QUERY_DEFAULT_ROWS``; por encima de ``MCP_QUERY_MAX_ROWS``
    -> se RECORTA al máximo y vuelve ``LIMIT_TOO_HIGH`` como advertencia (S15). El agente nunca sube
    un tope. No entero, bool o no positivo -> ``MALFORMED_REQUEST``.
    """
    if raw is None:
        return min(env.MCP_QUERY_DEFAULT_ROWS, env.MCP_QUERY_MAX_ROWS), []
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise query_error(codes.REASON_MALFORMED_REQUEST)
    ceiling = min(env.MCP_QUERY_MAX_ROWS, env.MCP_QUERY_ROWS_CEILING)
    if raw > ceiling:
        return ceiling, [codes.WARN_LIMIT_TOO_HIGH]
    return raw, []


def effective_timeout_ms(raw: int | None = None) -> int:
    """Timeout de sentencia: el pedido (si lo hay) o el configurado, nunca por encima del techo."""
    base = env.MCP_QUERY_TIMEOUT_MS if raw is None else raw
    return max(1, min(int(base), env.MCP_QUERY_TIMEOUT_CEILING_MS))


# --------------------------------------------------------------------------- #
# Identificadores y armado de la sentencia                                     #
# --------------------------------------------------------------------------- #


def resolve_identifiers(facade, table, columns) -> tuple[str, list[str]]:
    """
    Resuelve ``table`` y ``columns`` contra el CATÁLOGO (façade de solo lectura) y devuelve los
    nombres EXACTOS del catálogo. Cualquier desconocido -> ``UNKNOWN_IDENTIFIER`` (S12). Corre antes
    de que la cuenta de datos conecte.

    Solo tablas (no vistas): una vista puede tener ``DEFINER`` con más alcance que la cuenta de
    datos, y es justo lo que el análisis no detecta. La contabilidad interna del gateway
    (``_gw_v_``/``_gw_stg_``) nunca es candidata.
    """
    from app.services.db_admin.identifiers import exclude_gateway_internal_tables

    if not isinstance(table, str) or not table:
        raise query_error(codes.REASON_UNKNOWN_IDENTIFIER)
    tablas = exclude_gateway_internal_tables(list(facade.object_index().get("table", [])))
    if table not in tablas:
        raise query_error(codes.REASON_UNKNOWN_IDENTIFIER)
    pedidas = list(dict.fromkeys(columns or []))
    if not pedidas:
        return table, []
    esquema = facade.table_schemas([table])
    existentes = {c.name for c in esquema[0].columns} if esquema else set()
    if any((not isinstance(c, str)) or c not in existentes for c in pedidas):
        raise query_error(codes.REASON_UNKNOWN_IDENTIFIER)
    return table, pedidas


def _table(name: str) -> exp.Table:
    return exp.Table(this=exp.to_identifier(name, quoted=True))


def _column(name: str) -> exp.Column:
    return exp.Column(this=exp.to_identifier(name, quoted=True))


def build_sample_rows(engine: str, table: str, columns: list[str] | None = None) -> str:
    """``SELECT <cols|*> FROM <t>``: sin ``LIMIT``; lo empuja ``bound_select`` (cap + 1)."""
    select = exp.select(*[_column(c) for c in columns]) if columns else exp.select(exp.Star())
    return select.from_(_table(table)).sql(dialect=_DIALECT[engine])


def build_distinct_values(engine: str, table: str, column: str) -> str:
    """``SELECT DISTINCT <c> FROM <t> ORDER BY <c>``."""
    select = exp.select(_column(column)).distinct().from_(_table(table)).order_by(_column(column))
    return select.sql(dialect=_DIALECT[engine])


def build_count_rows(engine: str, table: str) -> str:
    """``SELECT COUNT(*) FROM <t>``: una fila, acotada por el timeout."""
    return exp.select(exp.Count(this=exp.Star())).from_(_table(table)).sql(
        dialect=_DIALECT[engine]
    )


def validate_built(sql: str, *, engine: str, database: str, max_rows: int):
    """
    Pasa una sentencia armada por el gateway por el validador de agentes. Que NO la acepte es un
    ``policy_miss`` del propio armado (un identificador raro, un cambio de sqlglot): se registra y
    sale como ``QUERY_FAILED``, jamás se ejecuta una sentencia que el validador no aceptó.
    """
    verdict = policy.validate_agent_select(
        sql,
        engine=engine,
        database=database,
        max_rows=max_rows,
        max_offset=env.MCP_QUERY_MAX_OFFSET,
        max_bytes=env.MCP_QUERY_MAX_SQL_BYTES,
    )
    if not verdict.accepted or verdict.executed_sql is None:
        logger.error(
            "El validador rechazó una sentencia armada por el gateway (reasons=%s)",
            list(verdict.reasons),
        )
        raise query_error(codes.REASON_QUERY_FAILED)
    return verdict


# --------------------------------------------------------------------------- #
# Saneamiento y presupuesto de bytes                                           #
# --------------------------------------------------------------------------- #


def clean_text(value):
    """Caracteres de control fuera (salvo ``\\n`` y ``\\t``) y finales de línea normalizados."""
    if value is None:
        return None
    return _CONTROL_RE.sub("", str(value).replace("\r\n", "\n").replace("\r", "\n"))


def _clean_cell(value, counter: list[int]):
    """Saneado recursivo de una celda ya normalizada a JSON; cuenta las recortadas por ``json_value``."""
    if isinstance(value, str):
        if _CLIPPED_RE.search(value):
            counter[0] += 1
        return clean_text(value)
    if isinstance(value, list):
        return [_clean_cell(v, counter) for v in value]
    if isinstance(value, dict):
        return {clean_text(k): _clean_cell(v, counter) for k, v in value.items()}
    return value


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def fit_rows(rows: list[list], budget_bytes: int) -> tuple[list[list], bool]:
    """
    Las filas que entran en ``budget_bytes`` (JSON UTF-8), FILA POR FILA y sin partir ninguna.
    Devuelve ``(filas, recortado)``. Un presupuesto agotado devuelve las que entraron: nunca un
    error ni una fila a medias (D11).
    """
    kept: list[list] = []
    used = 2  # corchetes
    for row in rows:
        # ", " entre filas: es el separador de ``json.dumps`` por defecto, el que usa el despacho.
        size = _json_size(row) + 2
        if used + size > budget_bytes:
            return kept, True
        kept.append(row)
        used += size
    return kept, False


@dataclass
class _Window:
    """Lo que devuelve la ejecución, ya saneado y antes del recorte por bytes."""

    columns: list[str]
    rows: list[list]
    row_cap_hit: bool
    clipped_cells: int
    duration_ms: int


def build_envelope(
    *,
    database_ref: dict,
    executed_sql: str,
    human_query: str | None,
    window: _Window,
    warnings: list[str],
    byte_budget: int,
) -> dict:
    """
    El sobre de las lecturas de datos. ``data.columns`` y ``data.rows`` son texto de TERCEROS
    (``untrusted_fields``): las filas van como arreglos, sin claves que un dato pueda inventar.
    """
    from app.schemas.mcp import UNTRUSTED_NOTICE

    envelope = {
        "notice": UNTRUSTED_NOTICE,
        "data": {"columns": window.columns, "rows": []},
        "row_count": 0,
        "truncated": False,
        "truncation_reason": None,
        "clipped_cells": window.clipped_cells,
        "executed_sql": clean_text(executed_sql),
        "human_query": clean_text(human_query),
        "duration_ms": window.duration_ms,
        "warnings": list(warnings),
        "untrusted_fields": ["data.columns", "data.rows"],
        "untrusted_content": True,
        "source": "managed_database",
        "database": database_ref,
    }
    rows_budget = byte_budget - _json_size(envelope) - _ENVELOPE_SLACK_BYTES
    kept, byte_truncated = fit_rows(window.rows, max(0, rows_budget))
    envelope["data"]["rows"] = kept
    envelope["row_count"] = len(kept)
    envelope["truncated"] = bool(window.row_cap_hit or byte_truncated)
    if byte_truncated:
        envelope["truncation_reason"] = "byte_budget"
    elif window.row_cap_hit:
        envelope["truncation_reason"] = "row_cap"
    return envelope


# --------------------------------------------------------------------------- #
# Vigilante (KILL / cancel desde una segunda conexión)                         #
# --------------------------------------------------------------------------- #


class _Watchdog:
    """
    Hook de sesión + temporizador de respaldo del timeout del motor.

    ``hook`` corre dentro de la conexión de datos (después del ``READ ONLY``): fija el timeout exacto
    en PostgreSQL, anota el id de la conexión y arma el ``Timer``. Si vence, ``_kill`` abre otra
    conexión con la MISMA credencial y cancela esa sentencia. Mejor esfuerzo: que el kill falle no
    cambia la respuesta (el error del motor o del socket igual llega), solo queda en el log.
    """

    def __init__(self, target, *, database: str, engine: str, credential, timeout_ms: int):
        self.target = effective_target(target, credential)
        self.database = database
        self.engine = engine
        self.timeout_ms = timeout_ms
        self.conn_id = None
        self.fired = False
        self._timer: threading.Timer | None = None

    def hook(self, conn) -> None:
        if self.engine == "postgresql":
            apply_postgres_statement_timeout(conn, self.timeout_ms)
            self.conn_id = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
        else:
            self.conn_id = conn.exec_driver_sql("SELECT CONNECTION_ID()").scalar()
        delay = min(
            self.timeout_ms / 1000.0 + WATCHDOG_GRACE_SECONDS, float(env.MCP_SESSION_MAX_SECONDS)
        )
        self._timer = threading.Timer(delay, self._kill)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def kill_now(self) -> None:
        """
        Cancela la sentencia YA, sin esperar al ``Timer``. Lo usa ``run_agent_select`` cuando el
        cliente se rindió primero (timeout de SOCKET: ``2013``): cerrar el socket NO detiene un
        ``SELECT`` que el servidor sigue calculando, y como ``run_statements`` ya cortó el ``Timer``,
        sin esto la sentencia quedaría viva en la base de un tercero hasta terminar.
        """
        if not self.fired:
            self._kill()

    def _kill(self) -> None:
        self.fired = True
        try:
            conn_id = int(self.conn_id)
            with database_connection(
                self.target, self.database, statement_timeout_ms=5000
            ) as conn:
                if self.engine == "postgresql":
                    conn.exec_driver_sql(f"SELECT pg_cancel_backend({conn_id})")
                else:
                    conn.exec_driver_sql(f"KILL QUERY {conn_id}")
        except Exception as exc:  # noqa: BLE001 — el vigilante nunca rompe nada; queda en el log
            # Sin `exc_info`: el traceback de SQLAlchemy y del driver lleva el texto de la
            # sentencia (con los literales del agente) y a veces valores de filas de terceros. El
            # tipo de la excepción alcanza para diagnosticar; el detalle sale por la auditoría.
            logger.error(
                "El vigilante no pudo cancelar la consulta del agente (%s)", type(exc).__name__
            )


# --------------------------------------------------------------------------- #
# Ejecución                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuditContext:
    """Quién pregunta y sobre qué base: lo que la auditoría necesita y nada más."""

    actor: object
    tool: str
    database_id: int
    server_id: int


def _audit_detail(ctx: AuditContext, verdict, extra: str = "") -> str:
    masked = clean_text(verdict.masked_sql or "") or ""
    masked = masked.encode("utf-8")[:AUDIT_SQL_MAX_BYTES].decode("utf-8", errors="ignore")
    actor = ctx.actor
    base = (
        f"token={getattr(actor, 'token_id', None)} tool={ctx.tool} "
        f"hash={verdict.sql_hash} sql={masked}"
    )
    return f"{base} {extra}".strip()


def _record_intent(ctx: AuditContext, verdict) -> None:
    try:
        audit.record_intent(
            _AUDIT_ACTION,
            admin=ctx.actor,
            target_type="managed_database",
            target_id=ctx.database_id,
            server_id=ctx.server_id,
            touched_engine=True,
            detail=_audit_detail(ctx, verdict),
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed: la auditoría cae => no se conecta
        logger.error("Auditoría de intención caída; no se ejecuta la consulta del agente")
        raise query_error(codes.REASON_AUDIT_UNAVAILABLE) from exc


def _record_result(ctx: AuditContext, verdict, *, ok: bool, extra: str) -> None:
    audit.record(
        _AUDIT_ACTION,
        status="success" if ok else "failure",
        admin=ctx.actor,
        target_type="managed_database",
        target_id=ctx.database_id,
        server_id=ctx.server_id,
        touched_engine=True,
        detail=_audit_detail(ctx, verdict, extra),
    )


def _classify_failure(stmt, *, watchdog_fired: bool, timeout_ms: int) -> tuple[str, str]:
    """``(código público, detalle de auditoría)`` de una sentencia que falló. Sin texto del motor."""
    err = stmt.error
    code = (err.code if err else None) or ""
    sqlstate = (err.sqlstate if err else None) or ""
    if watchdog_fired or code in _TIMEOUT_CODES or sqlstate == "57014":
        return codes.REASON_QUERY_TIMEOUT, "timeout"
    if code in _LOST_CONNECTION_CODES and stmt.duration_ms >= timeout_ms * 0.9:
        return codes.REASON_QUERY_TIMEOUT, "timeout"
    if stmt.policy_miss:
        return codes.REASON_QUERY_FAILED, "policy_miss"
    return codes.REASON_QUERY_FAILED, f"engine_error:{code or 'unknown'}"


def run_agent_select(
    ctx: AuditContext,
    *,
    resolved,
    target,
    credential: QueryCredential,
    verdict,
    max_rows: int,
    warnings: list[str] | None = None,
    timeout_ms: int | None = None,
    byte_budget: int | None = None,
) -> dict:
    """
    Ejecuta un veredicto ACEPTADO del validador y devuelve el sobre de datos.

    Orden (cada paso corta el siguiente): veredicto aceptado -> ``record_intent`` (fail-closed) ->
    conexión de datos READ ONLY con vigilante -> saneado y recorte por bytes -> ``record`` del
    resultado. ``resolved`` es la fila de inventario (``ReachableDatabase``): motor y nombre de la
    base salen de ahí. Los errores salen como ``AppHttpException`` con código público cerrado.
    """
    if not verdict.accepted or verdict.executed_sql is None or verdict.row_bound is None:
        # Defensa en profundidad: quien llame con un veredicto no aceptado no ejecuta nada.
        raise query_error(codes.REASON_QUERY_FAILED)
    if credential.mode != MODE_STORED:
        raise query_error(codes.REASON_QUERY_FAILED)

    timeout = effective_timeout_ms(timeout_ms)
    budget = env.MCP_DATA_MAX_RESULT_BYTES if byte_budget is None else byte_budget
    engine = resolved.engine
    database = resolved.database

    _record_intent(ctx, verdict)

    plan = StatementPlan(
        seq=1, sql=verdict.executed_sql, kind="select", danger=READ
    )
    watchdog = _Watchdog(
        target, database=database, engine=engine, credential=credential, timeout_ms=timeout
    )
    started = time.monotonic()
    try:
        outcome = run_statements(
            target,
            database=database,
            engine=engine,
            statements=[plan],
            credential=credential,
            read_only=True,
            max_rows=max_rows,
            max_cell_chars=MAX_CELL_CHARS,
            timeout_ms=timeout,
            session_hook=watchdog.hook,
        )
    except Exception as exc:  # noqa: BLE001 — jamás str(exc): puede llevar host, usuario o una fila
        # Sin `exc_info`, por lo mismo que en el vigilante: el traceback embebe el SQL y valores.
        logger.error("Falló la ejecución de la lectura del agente (%s)", type(exc).__name__)
        _record_result(
            ctx,
            verdict,
            ok=False,
            extra=f"status={codes.REASON_QUERY_FAILED} detail=connect_or_driver_error "
            f"duration_ms={int((time.monotonic() - started) * 1000)}",
        )
        raise query_error(codes.REASON_QUERY_FAILED) from None
    finally:
        watchdog.stop()
    duration_ms = int((time.monotonic() - started) * 1000)

    if outcome.connection_error is not None:
        if outcome.connection_error.code in _ACCOUNT_BUSY_CODES:
            # La cuenta es válida pero llegó a su tope de conexiones: no está revocada. Decirle al
            # agente "credencial deshabilitada" lo mandaría a pedir algo que ya está bien.
            _record_result(
                ctx,
                verdict,
                ok=False,
                extra=f"status={codes.REASON_DATA_ACCOUNT_BUSY} detail=account_busy "
                f"duration_ms={duration_ms}",
            )
            raise query_error(codes.REASON_DATA_ACCOUNT_BUSY)
        # Credencial de datos rechazada por el motor (revocada o cambiada a mano).
        _record_result(
            ctx,
            verdict,
            ok=False,
            extra=f"status={codes.REASON_PROBE_NOT_GREEN} detail=connection_refused "
            f"duration_ms={duration_ms}",
        )
        raise query_error(codes.REASON_PROBE_NOT_GREEN)

    stmt = outcome.statements[0] if outcome.statements else None
    if stmt is None or not stmt.success or not outcome.success:
        if stmt is not None and not stmt.success:
            public, detail = _classify_failure(
                stmt, watchdog_fired=watchdog.fired, timeout_ms=timeout
            )
        else:
            public, detail = codes.REASON_QUERY_FAILED, "rollback_or_close_failed"
        if detail == "policy_miss":
            logger.error("policy_miss: el motor rechazó una lectura que el validador aceptó")
        if (
            public == codes.REASON_QUERY_TIMEOUT
            and stmt is not None
            and stmt.error is not None
            and stmt.error.code in _LOST_CONNECTION_CODES
        ):
            watchdog.kill_now()
        _record_result(
            ctx,
            verdict,
            ok=False,
            extra=f"status={public} detail={detail} duration_ms={duration_ms}",
        )
        raise query_error(public)

    counter = [0]
    rows = [[_clean_cell(v, counter) for v in row] for row in stmt.rows]
    window = _Window(
        columns=[clean_text(c) for c in stmt.columns],
        rows=rows,
        row_cap_hit=bool(stmt.truncated),
        clipped_cells=counter[0],
        duration_ms=duration_ms,
    )
    envelope = build_envelope(
        database_ref={"database_id": resolved.database_id, "engine": engine},
        executed_sql=verdict.executed_sql,
        human_query=verdict.human_query,
        window=window,
        warnings=list(warnings or []),
        byte_budget=budget,
    )
    _record_result(
        ctx,
        verdict,
        ok=True,
        extra=(
            f"status=ok rows={envelope['row_count']} truncated={envelope['truncated']} "
            f"duration_ms={duration_ms} bytes={_json_size(envelope)}"
        ),
    )
    return envelope


__all__ = [
    "AuditContext",
    "MAX_CELL_CHARS",
    "build_count_rows",
    "build_distinct_values",
    "build_envelope",
    "build_sample_rows",
    "clean_text",
    "effective_limit",
    "effective_timeout_ms",
    "fit_rows",
    "query_error",
    "resolve_identifiers",
    "run_agent_select",
    "validate_built",
]
