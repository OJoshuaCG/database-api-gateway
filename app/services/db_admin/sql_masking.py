"""
Enmascarado de LITERALES de un SQL ya guardado — módulo PURO, sin motor ni I/O.

Por qué existe
--------------
El historial de la consola SQL (``query_executions.sql_text``) guarda el lote COMPLETO que se
envió, con solo las contraseñas redactadas (``query_policy.redact_secrets``). Los literales de un
``WHERE``/``INSERT``/``UPDATE`` son DATOS DE NEGOCIO del tercero (e-mails, documentos, montos), y
``GET /servers/{id}/query/history`` se lee con ``sql_console.history``, una capacidad de
``viewer``: un rol que no muta NI divulga. Quien además tiene ``sql_console.execute`` en ese
destino podría correr la consulta igual, así que a él se le devuelve el texto entero; al resto,
la ESTRUCTURA (palabras clave, identificadores, funciones) con cada literal reemplazado por ``?``.

Se enmascara al LEER, no al guardar: el texto completo es el que la UI recarga para re-ejecutar,
y así las filas viejas dejan de filtrar sin reescribir datos.

Reglas
------
1. **sqlglot por dialecto** (el mismo mapeo que ``query_policy``). Todo nodo de literal —strings,
   números, hex/bit/byte, ``N'…'``, ``_charset'…'``, dollar-quoting, los valores de un ``IN`` y de
   cada fila de ``VALUES``, el texto de un ``DATE '…'``/``INTERVAL '…'``— pasa a ``?``. Se
   conservan ``NULL``/``TRUE``/``FALSE`` (no son datos) y los parámetros de tipo
   (``VARCHAR(255)``, ``DECIMAL(10, 2)``), que son esquema.
2. **Los comentarios se descartan**: pueden llevar datos y no aportan estructura.
3. **Fallback conservador** si sqlglot no parsea (texto truncado al tope del historial, varias
   sentencias unidas sin ``;``, sintaxis que sqlglot no soporta) o si devuelve un
   ``exp.Command`` (sentencia no modelada: su cuerpo viaja como un string opaco). El fallback
   es un escáner léxico que enmascara todo lo entre comillas simples (y dobles en MySQL, donde
   son strings), dollar-quoting y números fuera de identificadores, y descarta comentarios.
   **Nunca** se devuelve el texto crudo ante un fallo: ``mask_literals`` no lanza.
4. Red de seguridad sobre la salida de sqlglot: si un tipo de nodo no contemplado dejó un
   ``'…'`` en el texto generado, se barre con el escáner. Una regresión de sqlglot no puede
   reabrir la fuga.
"""

from __future__ import annotations

import logging
import re
import threading

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

PLACEHOLDER = "?"

# Dialecto de negocio -> dialecto de sqlglot. Mismo mapeo que ``query_policy._SQLGLOT_DIALECT``.
_SQLGLOT_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres"}

# Nodos cuyo valor ES un dato. ``Introducer`` (``_utf8mb4'x'``) se reemplaza entero.
_LITERAL_NODES: tuple[type[exp.Expression], ...] = (
    exp.Literal,
    exp.HexString,
    exp.BitString,
    exp.ByteString,
    exp.RawString,
    exp.National,
    exp.UnicodeString,
    exp.Introducer,
)

# Número suelto: entero, decimal, exponente o hex ``0x…``. El lookbehind evita el interior de
# un identificador (``t1``, ``col_2``) y la parte decimal ya consumida.
_NUMBER = re.compile(
    r"(?<![\w$.])(?:0[xX][0-9A-Fa-f]+|0[bB][01]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)(?![\w$])"
)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _is_postgres(engine: str) -> bool:
    return _SQLGLOT_DIALECT.get(engine) == "postgres"


# Padres cuyo literal NO es un dato: la precisión de un tipo (``VARCHAR(255)``) y el número de un
# parámetro posicional de PostgreSQL (``$1``).
_STRUCTURAL_PARENTS: tuple[type[exp.Expression], ...] = (exp.DataTypeParam, exp.Parameter)


def _replace_literal(node: exp.Expression) -> exp.Expression:
    """
    Literal → ``?``. Se usa ``exp.var("?")`` y no ``exp.Placeholder``: este último se genera como
    ``%s`` en PostgreSQL, y el marcador tiene que ser el mismo en los tres motores.
    """
    if isinstance(node, _LITERAL_NODES) and not isinstance(node.parent, _STRUCTURAL_PARENTS):
        return exp.var(PLACEHOLDER)
    return node


class _MuteWhileMasking(logging.Filter):
    """
    Silencia el logger de ``sqlglot`` SOLO en el hilo que está enmascarando.

    Ante sintaxis no soportada sqlglot loguea un WARNING con la sentencia COMPLETA ("… contains
    unsupported syntax"). En la ejecución eso ya pasa una vez; al LEER el historial se repetiría
    en cada página para cada fila, llevando al log del gateway justo los literales que este
    módulo existe para no divulgar. Es por hilo porque las rutas síncronas corren en un pool.
    """

    _local = threading.local()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — API de logging
        return not getattr(self._local, "active", False)


_MUTE = _MuteWhileMasking()
logging.getLogger("sqlglot").addFilter(_MUTE)


def _mask_with_sqlglot(sql: str, dialect: str) -> str | None:
    """Texto enmascarado, o ``None`` si sqlglot no lo modela con fidelidad (va al fallback)."""
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except Exception:  # noqa: BLE001 — sqlglot lanza varias familias de error
        return None
    trees = [t for t in trees if t is not None]
    if not trees:
        return None
    partes: list[str] = []
    for tree in trees:
        if any(isinstance(n, exp.Command) for n in tree.walk()):
            return None
        try:
            masked = tree.transform(_replace_literal, copy=True)
            partes.append(masked.sql(dialect=dialect, comments=False))
        except Exception:  # noqa: BLE001 — un generador que falla no puede devolver el crudo
            return None
    return ";\n".join(partes)


def _scan_mask(sql: str, *, postgres: bool) -> str:
    """
    Fallback léxico: enmascara strings, dollar-quoting y números; descarta comentarios.

    Conserva los identificadores entre backticks y, en PostgreSQL, entre comillas dobles (en
    MySQL las dobles son strings y se enmascaran). Una comilla que no cierra —texto truncado al
    tope del historial— enmascara hasta el final.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        # Comentarios: ``--``, ``#`` (MySQL) y ``/* … */``.
        if (ch == "-" and nxt == "-") or (ch == "#" and not postgres):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue
        # Identificadores: se conservan enteros.
        if ch == "`" or (ch == '"' and postgres):
            j = sql.find(ch, i + 1)
            j = n - 1 if j == -1 else j
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        # Dollar-quoting de PostgreSQL: ``$$…$$`` / ``$tag$…$tag$``.
        if ch == "$" and postgres:
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                cierre = sql.find(m.group(0), m.end())
                i = n if cierre == -1 else cierre + len(m.group(0))
                out.append(PLACEHOLDER)
                continue
        # Strings: ``'…'`` siempre; ``"…"`` en MySQL. Comilla duplicada y backslash escapan.
        if ch == "'" or ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == "\\" and not postgres:
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            # Prefijo pegado (``X'…'``, ``B'…'``, ``E'…'``, ``N'…'``, ``_utf8mb4'…'``): fuera.
            del out[len(out) - _literal_prefix_len(out) :]
            out.append(PLACEHOLDER)
            i = n if j >= n else j + 1
            continue
        out.append(ch)
        i += 1
    texto = _NUMBER.sub(PLACEHOLDER, "".join(out))
    return re.sub(r"[ \t]+", " ", texto).strip()


def _literal_prefix_len(out: list[str]) -> int:
    """Largo del prefijo de literal pegado a la comilla (``X``, ``E``, ``_utf8mb4``), o 0."""
    palabra = re.search(r"\w+$", "".join(out[-64:]))
    if not palabra:
        return 0
    w = palabra.group(0)
    return len(w) if w.lower() in {"x", "b", "e", "n", "u"} or w.startswith("_") else 0


def mask_literals(sql: str | None, engine: str) -> str:
    """
    El SQL con cada literal reemplazado por ``?`` y sin comentarios. No lanza nunca.

    ``engine`` es el valor de negocio (``mysql``/``mariadb``/``postgresql``); uno desconocido se
    trata como MySQL, igual que ``query_policy``.
    """
    if not sql:
        return sql or ""
    dialect = _SQLGLOT_DIALECT.get(engine, "mysql")
    postgres = dialect == "postgres"
    try:
        _MUTE._local.active = True
        try:
            masked = _mask_with_sqlglot(sql, dialect)
        finally:
            _MUTE._local.active = False
        if masked is None:
            return _scan_mask(sql, postgres=postgres)
        if "'" in masked:
            # Red de seguridad: un nodo no contemplado dejó un string en la salida.
            return _scan_mask(masked, postgres=postgres)
        return masked
    except Exception:  # noqa: BLE001 — el enmascarado nunca devuelve el crudo
        logger.warning("Fallo enmascarando SQL del historial", exc_info=True)
        return PLACEHOLDER
