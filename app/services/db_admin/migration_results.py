"""
Captura de RESULTADOS de sentencias ``SELECT`` ejecutadas dentro de una migración de
blueprint (``apply``/``rollback``), disponible tanto si la migración termina bien como si
falla a mitad de camino.

**El problema.** Una migración puede necesitar VERIFICAR algo en la BD destino (cuántas
filas quedaron sin backfill, qué valores violan la constraint que se está por crear, qué
duplicados bloquean un UNIQUE). Ese ``SELECT`` hoy se ejecuta y su resultado se tira:
Alembic no devuelve nada y el gateway solo informa "aplicada / falló". Justo cuando más
importa —una migración que murió en la sentencia k— el estado que se quería mirar ya no es
el mismo.

**Contrato de este módulo (leer antes de tocarlo).**

1. **NUNCA aborta la migración.** El ``SELECT`` se ejecuta primero; si la CAPTURA falla
   (tipo no serializable, encoding roto, la BD del gateway no responde), se registra
   ``status='error'`` con un motivo acotado y la migración sigue su curso. Capturar es un
   informe best-effort para un humano, no maquinaria de la que dependa la correctitud.
2. **NUNCA reescribe el SQL.** A diferencia de la consola SQL (que empuja un ``LIMIT`` al
   motor con ``query_policy._limited_sql``), acá el texto que corre contra el motor tiene
   que ser byte a byte el del ``checksum`` de la migración. Los topes se aplican al
   CAPTURAR (``fetchmany(max_rows + 1)`` para poder marcar ``truncated`` con certeza).
3. **NUNCA toca la lista de sentencias.** El índice que se persiste es el mismo que usa el
   checkpoint (``migration_progress``), y la lista sale siempre de
   ``MigrationRunner.statement_lists``. Si la captura re-partiera el ``up_sql`` por su
   cuenta, el conteo cambiaría y ``_resolve_resume_offset`` dispararía un 409 espurio.
4. **Es un contrato DISTINTO al de ``migration_progress``.** Ese módulo es maquinaria
   fail-closed que decide si RESUMIR es seguro; este es un informe. Por eso son módulos
   separados aunque compartan el índice de sentencia.

**Persistencia híbrida por motor** (la decide ``MigrationRunner.use_transactional_ddl``,
no se reimplementa el criterio acá):

- **MySQL/MariaDB (AUTOCOMMIT).** Cada captura se escribe DE INMEDIATO en la BD del
  gateway, con su propia sesión corta (mismo patrón que
  ``migration_progress.record_statement``) → ``durability='committed'`` desde el origen.
  Es lo correcto ahí: el DDL hace commit implícito, así que si la migración muere en la
  sentencia k lo capturado hasta k describe cambios que quedaron en disco.
- **PostgreSQL (transaccional).** La migración corre dentro de UNA transacción del motor
  destino. Escribir en la BD del gateway mientras esa transacción sigue abierta es un
  riesgo REAL, no teórico: la conexión al destino queda ``idle in transaction`` y
  ``idle_in_transaction_session_timeout`` (ver ``remote_engine``) puede abortar la
  migración por culpa de una escritura lenta a la BD de metadatos. Así que las capturas se
  ACUMULAN en un buffer en memoria del proceso y se vuelcan en un solo lote cuando
  ``command.upgrade()``/``downgrade()`` retorna o lanza — momento en el que el runner ya
  sabe si la transacción confirmó (``committed``) o se revirtió (``rolled_back``). Nunca
  queda un estado intermedio ambiguo.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core import crypto
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    MIGRATION_CAPTURE_ENABLED,
    MIGRATION_CAPTURE_MAX_BYTES,
    MIGRATION_CAPTURE_MAX_CELL_CHARS,
    MIGRATION_CAPTURE_MAX_ROWS,
    MIGRATION_CAPTURE_SQL_MAX_CHARS,
)
from app.core.logger import get_logger
from app.models.migration_select_result import MigrationSelectResult
from app.services.db_admin import query_policy, sql_dialect
from app.services.db_admin.value_json import json_value

logger = get_logger(__name__)

# Valores de ``status`` y ``durability`` (espejo de los comentarios del modelo).
STATUS_OK = "ok"
STATUS_ERROR = "error"
DURABILITY_COMMITTED = "committed"
DURABILITY_ROLLED_BACK = "rolled_back"
DURABILITY_UNKNOWN = "unknown"

# Pre-filtro BARATO: solo estas formas pueden llegar a ser una lectura pura. Existe para no
# pagar el parse de sqlglot en cientos de sentencias DDL de un baseline. Un
# ``CREATE PROCEDURE … BEGIN … SELECT … END`` es UNA sentencia del splitter que arranca con
# ``CREATE``: no pasa el filtro y por eso no necesita ningún caso especial.
#
# Se aplica sobre el primer texto EJECUTABLE de la sentencia, no sobre el texto crudo:
# ``split_sql_statements`` conserva los comentarios DENTRO de la sentencia que emite (solo
# descarta las que son solo comentarios), y una sentencia de verificación real casi siempre
# viene precedida por el comentario que explica qué verifica. Anclado en blancos únicamente,
# el filtro rechazaba justo el caso de uso más común, en silencio (ver
# ``sql_dialect.strip_leading_noise``).
_CAPTURE_PREFILTER_RE = re.compile(r"^\s*(?:select|with|table|values)\b", re.IGNORECASE)

# Motores donde ``#`` inicia un comentario. En PostgreSQL es el XOR de enteros, así que
# saltearlo como si fuera un comentario cambiaría el significado de la sentencia.
_HASH_COMMENT_ENGINES = frozenset({"mysql", "mariadb"})


def executable_head(sql: str, *, engine: str) -> str:
    """La sentencia sin sus comentarios/blancos INICIALES (ver ``strip_leading_noise``)."""
    return sql_dialect.strip_leading_noise(
        sql, hash_is_comment=engine in _HASH_COMMENT_ENGINES
    )


# --------------------------------------------------------------------------- #
# Clasificación (PURA)                                                         #
# --------------------------------------------------------------------------- #
def is_capturable(sql: str, *, engine: str) -> bool:
    """
    ¿Esta sentencia es candidata a que se capture su resultado?

    Compuerta en dos pasos: el pre-filtro de forma (barato) y, solo si pasa,
    ``query_policy.classify_statement`` — el mismo clasificador AST-first (sqlglot, nunca
    por palabra clave) que usa la consola SQL. Se exige lectura PURA: un
    ``WITH d AS (DELETE … RETURNING *) SELECT * FROM d`` tiene raíz ``Select`` y el
    clasificador lo marca como escritura, así que no se captura.

    **El veredicto ``blocked`` significa acá ÚNICAMENTE "no capturar".** Nunca "rechazar la
    migración": un ``GRANT`` o un ``SET FOREIGN_KEY_CHECKS=0`` dentro de un blueprint tiene
    que seguir ejecutándose exactamente como hoy. Esta función solo decide si el resultado
    se guarda.

    El pre-filtro ignora los comentarios INICIALES (``-- …``, ``/* … */`` y ``#`` solo en
    MySQL/MariaDB). El clasificador AST posterior recibe el SQL COMPLETO —comentarios
    incluidos—: sqlglot los parsea sin problema y el texto que se ejecuta y se hashea nunca
    se altera.
    """
    if not sql or not _CAPTURE_PREFILTER_RE.match(executable_head(sql, engine=engine)):
        return False
    try:
        plan = query_policy.classify_statement(sql, engine=engine)
    except Exception:  # noqa: BLE001 — clasificar nunca puede romper una migración
        logger.debug("No se pudo clasificar una sentencia para captura", exc_info=True)
        return False
    return plan.danger == query_policy.READ and plan.kind not in ("empty", "blocked")


# --------------------------------------------------------------------------- #
# Empaquetado de filas (PURO)                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CapturedPayload:
    """Resultado ya recortado y listo para serializar/cifrar."""

    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    payload_bytes: int


def pack_rows(
    columns: list[str],
    fetched: list,
    *,
    max_rows: int = MIGRATION_CAPTURE_MAX_ROWS,
    max_cell_chars: int = MIGRATION_CAPTURE_MAX_CELL_CHARS,
    max_bytes: int = MIGRATION_CAPTURE_MAX_BYTES,
) -> CapturedPayload:
    """
    Recorta y normaliza el resultado. ``fetched`` viene de ``fetchmany(max_rows + 1)``: esa
    fila DE MÁS es la única forma de distinguir "había exactamente max_rows" de "había más
    y se recortó".

    Las filas se emiten como LISTAS, no como dicts: una consulta puede devolver dos
    columnas con el mismo nombre (``SELECT a.id, b.id FROM …``) y un dict perdería una.

    El tope de bytes se evalúa fila por fila y corta en cuanto se pasa: un ``SELECT`` de
    pocas filas con un JSONB gigante no debe poder inflar la BD del gateway.

    **El tope se mide en BYTES UTF-8, no en caracteres.** ``len()`` sobre el ``str`` del JSON
    cuenta code points: con contenido CJK o emoji el JSON real pesa 3-4× lo medido, así que un
    tope de 256 KB dejaba pasar ~1 MB y el ``payload_bytes`` que viaja por la API reportaba
    caracteres con nombre de bytes. Cada fila se codifica UNA vez (nunca se re-codifica el
    acumulado: eso volvería el bucle O(n²)).
    """
    safe_cols = [str(c) for c in columns]
    truncated = len(fetched) > max_rows
    candidate = fetched[:max_rows]

    rows: list[list] = []
    # Presupuesto aproximado del envelope (corchetes/comas del JSON de columnas y filas).
    used = len(json.dumps(safe_cols, ensure_ascii=False).encode("utf-8")) + 2
    for raw in candidate:
        row = [json_value(v, max_cell_chars) for v in raw]
        size = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")) + 1
        if rows and used + size > max_bytes:
            truncated = True
            break
        rows.append(row)
        used += size
        if used > max_bytes:
            # La PRIMERA fila se conserva aunque exceda el tope: devolver un payload vacío
            # sin explicación sería peor que devolver una fila grande recortada por celda.
            # Pero ``truncated`` se marca SIEMPRE: el campo significa "el resultado real
            # tenía más filas/bytes que los topes" (ver el comentario de la columna), y acá
            # el presupuesto de bytes se rebasó con certeza.
            truncated = True
            break

    payload_bytes = len(
        json.dumps(
            {"columns": safe_cols, "rows": rows}, ensure_ascii=False, default=str
        ).encode("utf-8")
    )
    return CapturedPayload(
        columns=safe_cols,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        payload_bytes=payload_bytes,
    )


def finalize_status(*, buffered: bool, committed: bool | None) -> str:
    """
    Durabilidad de una captura según el desenlace REAL de la operación (PURA).

    - No bufferizada (MySQL/MariaDB, AUTOCOMMIT): la fila ya se escribió con el dato
      commiteado en el destino → ``committed``.
    - Bufferizada (PostgreSQL): ``committed``/``rolled_back`` según lo que hizo la
      transacción de la migración. ``committed=None`` = el runner no pudo determinarlo →
      ``unknown`` (fail-closed: nunca se afirma "committed" sin saberlo).
    """
    if not buffered:
        return DURABILITY_COMMITTED
    if committed is None:
        return DURABILITY_UNKNOWN
    return DURABILITY_COMMITTED if committed else DURABILITY_ROLLED_BACK


def capture_sql_text(sql: str) -> str:
    """SQL que se persiste junto a la captura: contraseñas redactadas y recortado."""
    return query_policy.redact_secrets(sql)[:MIGRATION_CAPTURE_SQL_MAX_CHARS]


# --------------------------------------------------------------------------- #
# Buffer en memoria (solo PostgreSQL / modo transaccional)                      #
# --------------------------------------------------------------------------- #
@dataclass
class _Pending:
    """Captura pendiente de volcado: todo menos la durabilidad, que aún no se conoce."""

    managed_database_id: int
    model_migration_id: int
    direction: str
    statement_index: int
    migration_checksum: str
    sql_hash: str
    sql_text: str
    status: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    payload_bytes: int = 0
    error_message: str | None = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_buffer_lock = threading.Lock()
_buffer: dict[tuple[int, int, str], list[_Pending]] = {}
# Filas que la CORRIDA EN CURSO escribió de verdad, por migración/dirección. No se deriva de
# un ``COUNT`` posterior a propósito: la tabla acumula las capturas de corridas anteriores
# (una versión aplicada con captura y luego revertida con un ``down_sql`` sin lecturas dejaba
# al ``rollback`` informando "1 capturada" y auditando una escritura que nunca ocurrió).
_written: dict[tuple[int, int, str], int] = {}


def _buffer_key(managed_database_id: int, model_migration_id: int, direction: str):
    return (managed_database_id, model_migration_id, direction)


def buffered_count(managed_database_id: int, model_migration_id: int, direction: str) -> int:
    """Capturas pendientes de volcado (diagnóstico/tests)."""
    with _buffer_lock:
        return len(_buffer.get(_buffer_key(managed_database_id, model_migration_id, direction), []))


def discard_buffer(managed_database_id: int, model_migration_id: int, direction: str) -> None:
    """Descarta el buffer sin persistir (limpieza defensiva)."""
    with _buffer_lock:
        key = _buffer_key(managed_database_id, model_migration_id, direction)
        _buffer.pop(key, None)
        _written.pop(key, None)


def begin(managed_database_id: int, model_migration_id: int, direction: str) -> None:
    """
    Arranca una corrida limpia para esta migración/dirección: descarta cualquier buffer y
    cualquier contador que hubiera quedado.

    Barrido DEFENSIVO. En el camino normal ``finalize`` limpia todo (se llama tanto en el
    éxito como en el ``except``), pero un ``BaseException`` entre una captura y el
    ``finalize`` —``KeyboardInterrupt``, el apagado del worker— dejaría filas de un intento
    viejo en memoria del proceso, y la corrida siguiente las volcaría con la durabilidad
    equivocada. Nunca descarta nada ya PERSISTIDO: solo toca estado en memoria.
    """
    discard_buffer(managed_database_id, model_migration_id, direction)


def _bump_written(key: tuple[int, int, str], rows: int) -> None:
    with _buffer_lock:
        _written[key] = _written.get(key, 0) + rows


def finalize(
    managed_database_id: int,
    model_migration_id: int,
    direction: str,
    *,
    committed: bool | None,
) -> int:
    """
    Vuelca el buffer de una migración/dirección con la durabilidad que corresponde y lo
    limpia. Devuelve cuántas filas escribió ESTA corrida (las bufferizadas que acaba de
    volcar, más las que en AUTOCOMMIT ya se habían escrito una por una).

    Ese número es el que viaja a la respuesta y a la auditoría: es la única forma de que
    ``captured_select_count`` signifique "capturado por esta corrida" y no "lo que haya en la
    tabla para estas versiones" (que incluye corridas anteriores).

    Lo llama el runner en el camino de ÉXITO y en el ``except``: recién ahí se sabe si la
    transacción del destino confirmó. Nunca lanza — un fallo al persistir el informe no
    puede cambiar el desenlace de una migración.
    """
    key = _buffer_key(managed_database_id, model_migration_id, direction)
    with _buffer_lock:
        pending = _buffer.pop(key, [])
        written = _written.pop(key, 0)
    if not pending:
        return written
    durability = finalize_status(buffered=True, committed=committed)
    try:
        _write_rows(pending, durability)
        return written + len(pending)
    except Exception:  # noqa: BLE001 — persistir el informe es best-effort
        logger.warning(
            "No se pudieron persistir %d captura(s) de SELECT de la migración %s (BD %s, %s)",
            len(pending),
            model_migration_id,
            managed_database_id,
            direction,
            exc_info=True,
        )
        return written


# --------------------------------------------------------------------------- #
# Ejecución + captura (lo que invoca el archivo de revisión generado)           #
# --------------------------------------------------------------------------- #
def capture_statement(
    conn,
    sql: str,
    *,
    managed_database_id: int,
    model_migration_id: int,
    direction: str,
    statement_index: int,
    migration_checksum: str,
    buffered: bool,
) -> None:
    """
    Ejecuta ``sql`` en la conexión de la migración y captura su resultado.

    Lo llama el archivo de revisión Alembic generado por ``MigrationRunner``, en el lugar
    exacto donde una sentencia no capturable haría ``op.get_bind().exec_driver_sql(...)``.
    El ORDEN es: ejecutar → capturar (best-effort) → (el codegen agrega después el
    ``migration_progress.record_statement`` de siempre, sin cambios).

    Un error de EJECUCIÓN se propaga tal cual (la migración debe fallar igual que hoy). Un
    error de CAPTURA se registra y se sigue adelante.

    No recibe el motor: la decisión de capturar (que SÍ depende del dialecto, ver
    ``is_capturable``) ya la tomó el codegen, y acá el SQL se ejecuta y se empaqueta igual en
    los tres motores. Lo que cambia por motor es ``buffered``, y eso lo decide
    ``MigrationRunner.use_transactional_ddl``.
    """
    # El escapado de ``%`` es EL MISMO que aplica el resto del runner (los drivers pyformat
    # parsean placeholders en cuanto reciben params distilados a ``()``). Import diferido
    # para no crear un ciclo: ``migrations`` importa este módulo.
    from app.services.db_admin.migrations import MigrationRunner

    result = conn.exec_driver_sql(MigrationRunner._escape_percent(sql))

    if not MIGRATION_CAPTURE_ENABLED:
        # Kill switch: la sentencia se ejecutó igual (nunca se altera lo que corre en el
        # motor), solo no se guarda nada.
        _close_quietly(result, statement_index)
        return

    payload: CapturedPayload | None = None
    error_message: str | None = None
    try:
        if result.returns_rows:
            columns = list(result.keys())
            # Una fila DE MÁS para poder marcar ``truncated`` con certeza.
            fetched = result.fetchmany(MIGRATION_CAPTURE_MAX_ROWS + 1)
            payload = pack_rows(columns, list(fetched))
        else:
            # Clasificada como lectura pero sin filas (p. ej. un motor que no expone el
            # cursor como tal): se registra la ejecución sin datos, no es un error.
            payload = pack_rows([], [])
    except Exception as exc:  # noqa: BLE001 — capturar nunca aborta la migración
        # NUNCA ``str(exc)``: un UnicodeDecodeError arrastra bytes de una fila del cliente
        # y el mensaje del motor puede traer datos. El detalle va al log con el Request ID.
        logger.exception(
            "Falló la captura del resultado de la sentencia %d de la migración %s (BD %s)",
            statement_index,
            model_migration_id,
            managed_database_id,
        )
        error_message = (
            f"No se pudo capturar el resultado ({type(exc).__name__}). La sentencia SÍ se "
            "ejecutó; el detalle está en el log del gateway con el Request ID."
        )
    finally:
        _close_quietly(result, statement_index)

    entry = _Pending(
        managed_database_id=managed_database_id,
        model_migration_id=model_migration_id,
        direction=direction,
        statement_index=statement_index,
        migration_checksum=migration_checksum,
        sql_hash=query_policy.sql_hash(sql),
        sql_text=capture_sql_text(sql),
        status=STATUS_OK if error_message is None else STATUS_ERROR,
        columns=payload.columns if payload else [],
        rows=payload.rows if payload else [],
        row_count=payload.row_count if payload else 0,
        truncated=payload.truncated if payload else False,
        payload_bytes=payload.payload_bytes if payload else 0,
        error_message=error_message,
    )

    key = _buffer_key(managed_database_id, model_migration_id, direction)
    if buffered:
        # PostgreSQL: no se toca la BD del gateway mientras la transacción del destino esté
        # abierta (ver el docstring del módulo). El conteo de la corrida lo hace ``finalize``
        # sobre el buffer que vuelca: hasta entonces no hay nada escrito.
        with _buffer_lock:
            _buffer.setdefault(key, []).append(entry)
        return

    try:
        _write_rows([entry], DURABILITY_COMMITTED)
        _bump_written(key, 1)
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning(
            "No se pudo persistir la captura de la sentencia %d de la migración %s (BD %s)",
            statement_index,
            model_migration_id,
            managed_database_id,
            exc_info=True,
        )


def _close_quietly(result, statement_index: int) -> None:
    try:
        result.close()
    except Exception:  # noqa: BLE001 — cerrar el cursor nunca debe tapar nada
        logger.debug("No se pudo cerrar el cursor de la sentencia %d", statement_index)


# --------------------------------------------------------------------------- #
# Persistencia / lectura (BD de METADATOS del gateway)                          #
# --------------------------------------------------------------------------- #
def _session():
    """Sesión corta y dedicada — nunca la del request, nunca la del motor destino."""
    return Database(
        DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT
    ).get_declarative_base_session()


def _write_rows(pending: list[_Pending], durability: str) -> None:
    """
    Upsert de las capturas por su clave única. El upsert (en vez de insert a secas) es lo
    que hace idempotente un RESUME: la sentencia ya ejecutada no se vuelve a correr, pero si
    por cualquier motivo se re-capturara el mismo índice, se sobreescribe en vez de violar
    la constraint y perder el lote entero.

    El payload va CIFRADO con la DEK (``app/core/crypto.py``): no es legible por SQL directo
    contra la BD del gateway, así que todo acceso pasa por el endpoint auditado.
    """
    session = _session()
    try:
        for entry in pending:
            columns_json = crypto.encrypt(
                json.dumps(entry.columns, ensure_ascii=False, default=str)
            )
            rows_json = crypto.encrypt(
                json.dumps(entry.rows, ensure_ascii=False, default=str)
            )
            row = (
                session.query(MigrationSelectResult)
                .filter(
                    MigrationSelectResult.managed_database_id == entry.managed_database_id,
                    MigrationSelectResult.model_migration_id == entry.model_migration_id,
                    MigrationSelectResult.direction == entry.direction,
                    MigrationSelectResult.statement_index == entry.statement_index,
                )
                .first()
            )
            if row is None:
                row = MigrationSelectResult(
                    managed_database_id=entry.managed_database_id,
                    model_migration_id=entry.model_migration_id,
                    direction=entry.direction,
                    statement_index=entry.statement_index,
                )
                session.add(row)
            row.migration_checksum = entry.migration_checksum
            row.sql_hash = entry.sql_hash
            row.sql_text = entry.sql_text
            row.status = entry.status
            row.durability = durability
            row.columns_json = columns_json
            row.rows_json = rows_json
            row.row_count = entry.row_count
            row.truncated = entry.truncated
            row.payload_bytes = entry.payload_bytes
            row.error_message = entry.error_message
            # ``captured_at`` es cuándo corrió en el DESTINO; en PostgreSQL la fila se
            # escribe después (al cerrar la transacción), así que no puede derivarse de
            # ``created_at``. Naive UTC: el resto de las columnas DateTime del esquema lo son.
            row.captured_at = entry.captured_at.replace(tzinfo=None)
        session.commit()
    finally:
        session.close()


def read_results(
    managed_database_id: int, model_migration_id: int, *, direction: str | None = None
) -> list[dict]:
    """
    Capturas de una BD/versión, ya DESCIFRADAS, ordenadas por dirección ('up' antes que
    'down') e índice de sentencia.

    El llamador (controller) es el responsable de haber auditado la lectura ANTES de
    invocar esto: acá se descifra contenido de negocio.
    """
    session = _session()
    try:
        q = session.query(MigrationSelectResult).filter(
            MigrationSelectResult.managed_database_id == managed_database_id,
            MigrationSelectResult.model_migration_id == model_migration_id,
        )
        if direction is not None:
            q = q.filter(MigrationSelectResult.direction == direction)
        # El orden se resuelve en Python, no en SQL: alfabéticamente 'down' iría antes que
        # 'up', y el informe se lee al revés (primero lo que se aplicó). Los conjuntos son
        # chicos por construcción (una fila por sentencia de lectura de UNA versión).
        rows = sorted(
            q.all(),
            key=lambda r: (0 if r.direction == "up" else 1, r.statement_index),
        )
        return [_serialize_row(r) for r in rows]
    finally:
        session.close()


def _serialize_row(row: MigrationSelectResult) -> dict:
    """
    Una captura lista para la API. Un payload que no se puede DESCIFRAR (DEK rotada más
    allá de la ventana de MultiFernet, token corrupto) no se oculta ni tumba la respuesta:
    la fila viaja con ``status='error'`` y el motivo, para que el operador entienda por qué
    no hay datos en vez de creer que el SELECT no devolvió nada.
    """
    columns: list[str] = []
    rows: list[list] = []
    error = row.error_message
    status = row.status
    try:
        columns = json.loads(crypto.decrypt(row.columns_json))
        rows = json.loads(crypto.decrypt(row.rows_json))
    except Exception:  # noqa: BLE001 — incluye CryptoError y JSON corrupto
        logger.warning(
            "No se pudo descifrar la captura id=%s (migración %s, BD %s)",
            row.id,
            row.model_migration_id,
            row.managed_database_id,
            exc_info=True,
        )
        status = STATUS_ERROR
        error = (
            "El payload capturado no se pudo descifrar (¿clave de datos rotada?). "
            "No hay filas que mostrar."
        )
    return {
        "statement_index": row.statement_index,
        "direction": row.direction,
        "sql": row.sql_text,
        "sql_hash": row.sql_hash,
        "status": status,
        "durability": row.durability,
        "columns": columns,
        "rows": rows,
        "row_count": row.row_count,
        "truncated": row.truncated,
        "payload_bytes": row.payload_bytes,
        "error": error,
        "captured_at": row.captured_at,
        "migration_checksum": row.migration_checksum,
    }


def purge(managed_database_id: int, model_migration_id: int) -> int:
    """Borra las capturas de UNA BD/versión (purga manual por endpoint)."""
    session = _session()
    try:
        deleted = (
            session.query(MigrationSelectResult)
            .filter(
                MigrationSelectResult.managed_database_id == managed_database_id,
                MigrationSelectResult.model_migration_id == model_migration_id,
            )
            .delete(synchronize_session=False)
        )
        session.commit()
        return int(deleted or 0)
    finally:
        session.close()


def purge_for_migration(model_migration_id: int, *, session=None) -> int:
    """
    Borra las capturas de una versión en TODAS las BDs.

    Lo usa el ``PATCH`` que edita el ``up_sql``: una captura describe el resultado de una
    sentencia de un SQL que dejó de existir, y su ``statement_index`` ya no apunta a lo
    mismo. Igual que con el manifiesto (``model_migration_statements``), se BORRA en vez de
    intentar re-alinearla. Acepta una ``session`` externa para que el borrado ocurra en la
    MISMA transacción que la edición.
    """
    own = session is None
    session = session or _session()
    try:
        deleted = (
            session.query(MigrationSelectResult)
            .filter(MigrationSelectResult.model_migration_id == model_migration_id)
            .delete(synchronize_session=False)
        )
        if own:
            session.commit()
        return int(deleted or 0)
    finally:
        if own:
            session.close()


def rekey_checksum(session, model_migration_id: int, checksum: str) -> int:
    """
    Reapunta las capturas de una versión a un ``checksum`` nuevo, sin tocar su contenido.

    Existe para UN caso y no debe usarse fuera de él: el **renumerado** que hace
    ``ModelMigrationController._apply_renumber`` al eliminar una versión intermedia. Ahí
    cambia el ``version`` de las migraciones posteriores y, como ``compute_checksum`` incluye
    la versión, cambia también su checksum — pero el SQL es exactamente el mismo, así que las
    capturas siguen describiendo lo que describían.

    Sin esto, ``GET .../select-results`` las marcaría ``stale`` (compara el checksum guardado
    contra el de la migración, ``managed_migration_controller`` § ``stale``) por un renombre
    que no alteró ni una sentencia. Es lo OPUESTO a ``purge_for_migration``, que se usa cuando
    el SQL sí cambió y la captura dejó de corresponder.

    Exige una ``session`` externa a propósito: solo tiene sentido dentro de la misma
    transacción que renumera, y con su propio commit podría quedar reapuntando capturas de un
    renumerado que después revierte.
    """
    updated = (
        session.query(MigrationSelectResult)
        .filter(MigrationSelectResult.model_migration_id == model_migration_id)
        .update({MigrationSelectResult.migration_checksum: checksum}, synchronize_session=False)
    )
    return int(updated or 0)


def purge_expired(ttl_hours: int) -> int:
    """
    Borra las capturas más viejas que el TTL (retención de datos de negocio).

    ``ttl_hours <= 0`` desactiva la purga (retención indefinida — hay que pedirlo
    explícitamente). Nunca lanza: se invoca en el arranque y un fallo acá no puede impedir
    que el gateway levante.
    """
    if ttl_hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=ttl_hours)
    try:
        session = _session()
        try:
            deleted = (
                session.query(MigrationSelectResult)
                .filter(MigrationSelectResult.captured_at < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
            if deleted:
                logger.info(
                    "Purga por TTL: %d captura(s) de SELECT de migraciones eliminadas.",
                    deleted,
                )
            return int(deleted or 0)
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — la purga no puede romper el arranque
        logger.warning("No se pudo purgar las capturas expiradas", exc_info=True)
        return 0


__all__ = [
    "CapturedPayload",
    "DURABILITY_COMMITTED",
    "DURABILITY_ROLLED_BACK",
    "DURABILITY_UNKNOWN",
    "STATUS_ERROR",
    "STATUS_OK",
    "begin",
    "capture_sql_text",
    "capture_statement",
    "discard_buffer",
    "executable_head",
    "finalize",
    "finalize_status",
    "is_capturable",
    "pack_rows",
    "purge",
    "purge_expired",
    "purge_for_migration",
    "read_results",
]
