"""
Vocabulario cerrado de errores DEL MOTOR destino + saneado del mensaje que se persiste/expone.

Por qué existe
--------------
El texto nativo de un error de MySQL/MariaDB/PostgreSQL incrusta VALORES de filas:
``Duplicate entry 'alice@x.com' for key 'users.email'``, ``Key (email)=(alice@x.com) already
exists``, ``Incorrect integer value: 'abc' for column 'age' at row 12``, el fragmento de SQL de un
``near '…'``. Persistir ese texto en el historial de migraciones —que se lee con
``blueprints.read``, una capacidad de viewer y ``agent_allowed``— convertía un log de auditoría en
una fuga de datos de negocio por la puerta de atrás (mismo criterio que ``export_writer.ExportItemStat``
y ``ExportController._failure_reason``: "nunca ``str(exc)`` del motor").

Qué se expone en su lugar
-------------------------
- ``code``: un código de ESTE catálogo (``engine.*``), derivado del errno (MySQL/MariaDB) o del
  SQLSTATE (PostgreSQL). Es lo que un cliente debe usar para clasificar; nunca la prosa.
- ``message``: el texto del motor SANEADO, en la forma canónica ``(<código nativo>) <mensaje>``.

Reglas de saneado (``sanitize_message``)
----------------------------------------
1. Solo la PRIMERA línea. En PostgreSQL ``DETAIL:``/``HINT:``/``CONTEXT:``/``LINE n:`` van en
   líneas aparte y son justo las que llevan la fila (``Failing row contains (…)``,
   ``Key (email)=(…)``) o el eco de la sentencia con sus literales.
2. Todo literal entre comillas simples o dobles se reemplaza por ``'?'``/``"?"``, SALVO que la
   palabra inmediatamente anterior sea un sustantivo estructural (``_STRUCTURAL_WORDS``: key,
   table, column, constraint, relation, index…). Los nombres de objetos son METADATA del esquema
   —el operador los necesita para diagnosticar y no son datos de negocio—; usuario, host y
   rol NO están en la lista a propósito (CLAUDE.md: pueden llevar host o usuario).
   Los identificadores entre backticks (MySQL) se conservan: los backticks solo delimitan
   identificadores.
3. Los mensajes de MySQL NO escapan las comillas internas del valor (``Duplicate entry 'O'Brien'``),
   así que los casos conocidos se enmascaran con un patrón codicioso hasta el ancla que los
   cierra (`` for key ``, `` for column ``, `` at line ``) ANTES del barrido genérico. Una
   comilla que no cierra (mensaje truncado) enmascara hasta el final.
4. ``row N`` → ``row ?``, ``=(…)`` → ``=(?)``, ``value N is out of range`` → ``value ?``; y como
   red de seguridad, cualquier e-mail o IPv4 que haya quedado sin comillas → ``?``.
5. Tope de 500 caracteres DESPUÉS de sanear (truncar antes podría dejar una comilla abierta).

El saneado es IDEMPOTENTE: ``from_text(from_text(x).message) == from_text(x)``. Eso es lo que
permite aplicarlo también AL LEER el historial, de modo que las filas viejas —que guardaron el
texto crudo antes de este módulo— dejan de filtrar sin reescribir datos con una migración.

El texto CRUDO va solo al log del gateway, con el Request ID (ver ``log_raw``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.core.context import current_http_identifier
from app.core.remote_engine import extract_driver_error_code

# --------------------------------------------------------------------------- #
# Códigos                                                                      #
# --------------------------------------------------------------------------- #

#: Clave única/primaria duplicada (MySQL 1062/1586, PG 23505).
CODE_DUPLICATE_KEY = "engine.duplicate_key"
#: Violación de FK al insertar/actualizar (padre ausente) o al borrar (hijos presentes).
CODE_FK_VIOLATION = "engine.fk_violation"
#: NOT NULL / CHECK / EXCLUDE (MySQL 1048/1364/3819/4025, PG 23502/23514/23P01).
CODE_CONSTRAINT_VIOLATION = "engine.constraint_violation"
#: Valor que no entra en la columna: truncado, demasiado largo o fuera de rango.
CODE_DATA_TRUNCATED = "engine.data_truncated"
#: Valor con formato inválido para el tipo (``Incorrect integer value``, ``invalid input syntax``).
CODE_INVALID_DATA = "engine.invalid_data"
#: SQL mal formado (MySQL 1064/1149, PG 42601).
CODE_SYNTAX_ERROR = "engine.syntax_error"
#: El objeto referenciado no existe (tabla, columna, índice, tipo, función, base).
CODE_UNDEFINED_OBJECT = "engine.undefined_object"
#: El objeto a crear ya existe (tabla, columna, índice, base).
CODE_DUPLICATE_OBJECT = "engine.duplicate_object"
#: Falta un privilegio o la autenticación fue rechazada.
CODE_PERMISSION_DENIED = "engine.permission_denied"
#: Se agotó la espera de un lock (MySQL 1205, PG 55P03).
CODE_LOCK_TIMEOUT = "engine.lock_timeout"
#: Deadlock o fallo de serialización (MySQL 1213, PG 40P01/40001).
CODE_DEADLOCK = "engine.deadlock"
#: La sentencia superó su tiempo máximo (MySQL 3024, MariaDB 1969, PG 57014).
CODE_QUERY_TIMEOUT = "engine.query_timeout"
#: Se perdió (o no se pudo abrir) la conexión con el motor.
CODE_CONNECTION_LOST = "engine.connection_lost"
#: Cualquier otro: el detalle está en el log del gateway (buscar por Request ID).
CODE_UNKNOWN = "engine.unknown"

ERROR_CODES = frozenset({
    CODE_DUPLICATE_KEY,
    CODE_FK_VIOLATION,
    CODE_CONSTRAINT_VIOLATION,
    CODE_DATA_TRUNCATED,
    CODE_INVALID_DATA,
    CODE_SYNTAX_ERROR,
    CODE_UNDEFINED_OBJECT,
    CODE_DUPLICATE_OBJECT,
    CODE_PERMISSION_DENIED,
    CODE_LOCK_TIMEOUT,
    CODE_DEADLOCK,
    CODE_QUERY_TIMEOUT,
    CODE_CONNECTION_LOST,
    CODE_UNKNOWN,
})

# MySQL/MariaDB: errno. PostgreSQL: SQLSTATE. Ambos como STRING, que es como los normaliza
# ``remote_engine.extract_driver_error_code`` (el único extractor del repo).
_NATIVE_TABLE: dict[str, str] = {
    # --- MySQL / MariaDB ---
    "1062": CODE_DUPLICATE_KEY, "1586": CODE_DUPLICATE_KEY, "1022": CODE_DUPLICATE_KEY,
    "1169": CODE_DUPLICATE_KEY,
    "1451": CODE_FK_VIOLATION, "1452": CODE_FK_VIOLATION, "1216": CODE_FK_VIOLATION,
    "1217": CODE_FK_VIOLATION,
    "1048": CODE_CONSTRAINT_VIOLATION, "1364": CODE_CONSTRAINT_VIOLATION,
    "3819": CODE_CONSTRAINT_VIOLATION, "4025": CODE_CONSTRAINT_VIOLATION,
    "1265": CODE_DATA_TRUNCATED, "1406": CODE_DATA_TRUNCATED, "1264": CODE_DATA_TRUNCATED,
    "1366": CODE_INVALID_DATA, "1292": CODE_INVALID_DATA, "1411": CODE_INVALID_DATA,
    "1064": CODE_SYNTAX_ERROR, "1149": CODE_SYNTAX_ERROR,
    "1146": CODE_UNDEFINED_OBJECT, "1054": CODE_UNDEFINED_OBJECT, "1049": CODE_UNDEFINED_OBJECT,
    "1091": CODE_UNDEFINED_OBJECT, "1051": CODE_UNDEFINED_OBJECT, "1305": CODE_UNDEFINED_OBJECT,
    "1050": CODE_DUPLICATE_OBJECT, "1060": CODE_DUPLICATE_OBJECT, "1061": CODE_DUPLICATE_OBJECT,
    "1007": CODE_DUPLICATE_OBJECT, "1304": CODE_DUPLICATE_OBJECT, "1359": CODE_DUPLICATE_OBJECT,
    "1044": CODE_PERMISSION_DENIED, "1045": CODE_PERMISSION_DENIED,
    "1142": CODE_PERMISSION_DENIED, "1143": CODE_PERMISSION_DENIED,
    "1227": CODE_PERMISSION_DENIED, "1370": CODE_PERMISSION_DENIED,
    "1205": CODE_LOCK_TIMEOUT,
    "1213": CODE_DEADLOCK,
    "3024": CODE_QUERY_TIMEOUT, "1969": CODE_QUERY_TIMEOUT,
    "2002": CODE_CONNECTION_LOST, "2003": CODE_CONNECTION_LOST, "2005": CODE_CONNECTION_LOST,
    "2006": CODE_CONNECTION_LOST, "2013": CODE_CONNECTION_LOST, "2055": CODE_CONNECTION_LOST,
    # --- PostgreSQL ---
    "23505": CODE_DUPLICATE_KEY,
    "23503": CODE_FK_VIOLATION,
    "23502": CODE_CONSTRAINT_VIOLATION, "23514": CODE_CONSTRAINT_VIOLATION,
    "23P01": CODE_CONSTRAINT_VIOLATION,
    "22001": CODE_DATA_TRUNCATED, "22003": CODE_DATA_TRUNCATED,
    "22P02": CODE_INVALID_DATA, "22007": CODE_INVALID_DATA, "22008": CODE_INVALID_DATA,
    "42601": CODE_SYNTAX_ERROR,
    "42P01": CODE_UNDEFINED_OBJECT, "42703": CODE_UNDEFINED_OBJECT,
    "42704": CODE_UNDEFINED_OBJECT, "42883": CODE_UNDEFINED_OBJECT,
    "3D000": CODE_UNDEFINED_OBJECT, "3F000": CODE_UNDEFINED_OBJECT,
    "42P07": CODE_DUPLICATE_OBJECT, "42701": CODE_DUPLICATE_OBJECT,
    "42710": CODE_DUPLICATE_OBJECT, "42P06": CODE_DUPLICATE_OBJECT,
    "42P04": CODE_DUPLICATE_OBJECT, "42723": CODE_DUPLICATE_OBJECT,
    "42501": CODE_PERMISSION_DENIED, "28000": CODE_PERMISSION_DENIED,
    "28P01": CODE_PERMISSION_DENIED,
    "55P03": CODE_LOCK_TIMEOUT,
    "40P01": CODE_DEADLOCK, "40001": CODE_DEADLOCK,
    "57014": CODE_QUERY_TIMEOUT,
    "57P01": CODE_CONNECTION_LOST, "57P02": CODE_CONNECTION_LOST,
    "57P03": CODE_CONNECTION_LOST,
}

# Clase SQLSTATE (2 primeros caracteres) como respaldo de un SQLSTATE que no está en la tabla.
_SQLSTATE_CLASS: dict[str, str] = {
    "08": CODE_CONNECTION_LOST,
    "22": CODE_INVALID_DATA,
    "23": CODE_CONSTRAINT_VIOLATION,
    "28": CODE_PERMISSION_DENIED,
    "40": CODE_DEADLOCK,
}

# Respaldo por TEXTO: cubre las filas viejas del historial guardadas sin código nativo (el
# ``str()`` de psycopg no lo incluye) y excepciones sin errno. El orden importa: lo más
# específico primero ("Duplicate entry" antes que "Duplicate key name").
_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), c)
    for p, c in (
        (r"duplicate key value violates unique constraint|Duplicate entry ", CODE_DUPLICATE_KEY),
        (r"violates foreign key constraint|foreign key constraint fails", CODE_FK_VIOLATION),
        (
            r"violates (?:not-null|check|exclusion) constraint|cannot be null|"
            r"Check constraint .* is violated|doesn't have a default value",
            CODE_CONSTRAINT_VIOLATION,
        ),
        (r"value too long|Data too long|Data truncated|out of range", CODE_DATA_TRUNCATED),
        (r"invalid input (?:syntax|value)|Incorrect \w+ value", CODE_INVALID_DATA),
        (r"syntax error|error in your SQL syntax", CODE_SYNTAX_ERROR),
        (r"permission denied|Access denied|command denied", CODE_PERMISSION_DENIED),
        (r"Lock wait timeout|lock timeout|could not obtain lock", CODE_LOCK_TIMEOUT),
        (r"deadlock|could not serialize access", CODE_DEADLOCK),
        (
            r"statement timeout|canceling statement|max_execution_time|max_statement_time",
            CODE_QUERY_TIMEOUT,
        ),
        (
            r"Lost connection|server has gone away|Can't connect|could not connect|"
            r"server closed the connection|connection .* failed",
            CODE_CONNECTION_LOST,
        ),
        (r"does not exist|doesn't exist|Unknown (?:column|table|database)", CODE_UNDEFINED_OBJECT),
        (r"already exists|Duplicate (?:column|key) name", CODE_DUPLICATE_OBJECT),
    )
)

_MAX_LEN = 500

# --------------------------------------------------------------------------- #
# Saneado                                                                      #
# --------------------------------------------------------------------------- #

#: Palabra que, inmediatamente antes de un literal entre comillas, lo marca como NOMBRE de un
#: objeto del esquema (se conserva). Usuario, host y rol quedan fuera a propósito.
_STRUCTURAL_WORDS = frozenset({
    "key", "table", "column", "constraint", "relation", "index", "database", "schema",
    "view", "trigger", "function", "procedure", "routine", "sequence", "type", "extension",
    "event", "domain", "collation",
})

# Forma canónica que produce este módulo: ``(1062) …`` / ``(23505) …``.
_CANONICAL = re.compile(r"^\((\d{3,5}|[0-9A-Z]{5})\) ?(.*)$", re.DOTALL)
# ``str()`` crudo de pymysql: ``(1062, "Duplicate entry 'x' for key 'k'")``. El cierre es
# opcional porque las filas viejas se truncaron a 500 caracteres.
_PYMYSQL_REPR = re.compile(r"^\((\d{3,5}),\s*([\"'])(.*?)(?:\2\))?\s*$", re.DOTALL)
_NATIVE_SHAPE = re.compile(r"^(?:\d{3,5}|[0-9A-Z]{5})$")

# Valores conocidos de MySQL cuyo contenido puede traer comillas sin escapar: se enmascaran
# con un patrón CODICIOSO hasta el ancla que los cierra, antes del barrido genérico.
_GREEDY_VALUES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(Duplicate entry )'.*'( for key )", re.IGNORECASE),
    re.compile(r"(value: )'.*'( for (?:column|function) )", re.IGNORECASE),
    re.compile(r"(near )'.*'( at line \d+)", re.IGNORECASE),
)
_PAREN_VALUES = re.compile(r"\)=\(.*\)")
_ROW_NUMBER = re.compile(r"\b(rows?) \d+", re.IGNORECASE)
_OUT_OF_RANGE_VALUE = re.compile(r"\b(value) -?[\w.+-]+( is out of range)", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_PRECEDING_WORD = re.compile(r"(\w+)\s*$")


def _mask_quoted(text: str) -> str:
    """Enmascara cada literal ``'…'``/``"…"`` salvo los precedidos por una palabra estructural."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "`":
            # Identificador MySQL: se conserva entero.
            j = text.find("`", i + 1)
            j = n - 1 if j == -1 else j
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if ch not in ("'", '"'):
            out.append(ch)
            i += 1
            continue
        j = text.find(ch, i + 1)
        if j == -1:
            # Comilla sin cerrar (mensaje truncado): lo que sigue puede ser un valor.
            out.append(f"{ch}?{ch}")
            break
        before = _PRECEDING_WORD.search("".join(out))
        if before and before.group(1).lower() in _STRUCTURAL_WORDS:
            out.append(text[i : j + 1])
        else:
            out.append(f"{ch}?{ch}")
        i = j + 1
    return "".join(out)


def sanitize_message(body: str) -> str:
    """Aplica las reglas 1-5 del docstring del módulo a un mensaje SIN prefijo nativo."""
    lines = body.strip().splitlines()
    text = lines[0].strip() if lines else ""
    for pat in _GREEDY_VALUES:
        text = pat.sub(r"\1'?'\2", text)
    text = _PAREN_VALUES.sub(")=(?)", text)
    text = _mask_quoted(text)
    text = _ROW_NUMBER.sub(r"\1 ?", text)
    text = _OUT_OF_RANGE_VALUE.sub(r"\1 ?\2", text)
    text = _EMAIL.sub("?", text)
    text = _IPV4.sub("?", text)
    return re.sub(r"\s+", " ", text).strip()[:_MAX_LEN]


def _split_native(text: str) -> tuple[str | None, str]:
    """Separa el código nativo del cuerpo, para la forma canónica y para el ``str()`` de pymysql."""
    t = text.strip()
    m = _PYMYSQL_REPR.match(t)
    if m:
        return m.group(1), m.group(3).replace("\\'", "'").replace('\\"', '"')
    m = _CANONICAL.match(t)
    if m:
        return m.group(1), m.group(2)
    return None, t


def classify(native_code: str | None, text: str = "") -> str:
    """Código del catálogo para un código nativo (y, en su defecto, por el texto)."""
    if native_code:
        if native_code in _NATIVE_TABLE:
            return _NATIVE_TABLE[native_code]
        cls = _SQLSTATE_CLASS.get(native_code[:2]) if len(native_code) == 5 else None
        if cls:
            return cls
    for pat, code in _TEXT_RULES:
        if pat.search(text):
            return code
    return CODE_UNKNOWN


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PublicEngineError:
    """Lo ÚNICO de un error del motor que puede persistirse o salir por la API."""

    code: str
    message: str
    native_code: str | None = None


def from_text(text: str | None) -> PublicEngineError | None:
    """
    Sanea un texto YA guardado (crudo de antes de este módulo, o canónico). Idempotente.

    Es la puerta de LECTURA del historial: así una fila vieja deja de filtrar sin reescribirla.
    """
    if text is None or not str(text).strip():
        return None
    native, body = _split_native(str(text))
    if native is not None and not _NATIVE_SHAPE.match(native):
        native = None
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    code = classify(native, first_line)
    message = sanitize_message(body)
    if native:
        message = f"({native}) {message}".strip()
    return PublicEngineError(code=code, message=message[:_MAX_LEN], native_code=native)


def _raw_body(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None) or exc
    args = getattr(orig, "args", None) or ()
    # pymysql: ``args == (errno, mensaje)``. Se toma el mensaje directo en vez del ``repr``
    # de la tupla, que escapa comillas y cambia según el contenido.
    if len(args) >= 2 and isinstance(args[0], int) and isinstance(args[1], str):
        return args[1]
    return str(orig)


def from_exception(exc: BaseException) -> PublicEngineError:
    """Versión pública de una excepción del motor (o de cualquier fallo del runner)."""
    native = extract_driver_error_code(exc) if isinstance(exc, Exception) else None
    if native is not None and not _NATIVE_SHAPE.match(native):
        # ``extract_driver_error_code`` acepta cualquier ``args[0]`` alfanumérico corto; un
        # ``OperationalError("boom")`` no es un errno.
        native = None
    body = _raw_body(exc)
    canonical = f"({native}) {body}" if native else body
    return from_text(canonical) or PublicEngineError(
        code=CODE_UNKNOWN, message=type(exc).__name__, native_code=native
    )


def log_raw(
    logger: logging.Logger,
    exc: BaseException,
    public: PublicEngineError,
    *,
    where: str,
    level: int = logging.WARNING,
) -> None:
    """El texto CRUDO del motor va SOLO acá: al log, con el Request ID para encontrarlo."""
    try:
        request_id = current_http_identifier.get()
    except LookupError:
        request_id = None
    logger.log(
        level,
        "Error del motor en %s (request_id=%s, error_code=%s, native=%s): %s",
        where, request_id, public.code, public.native_code, _raw_body(exc),
    )
