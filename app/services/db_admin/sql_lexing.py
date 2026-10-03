"""
Escáner léxico MÍNIMO de SQL, compartido por la consola y por el validador de SQL de agentes.

Módulo **PURO** (sin motor, sin I/O, sin sqlglot). Reconoce las regiones del texto que NO son
código SQL "plano": literales de cadena, identificadores citados, dollar-quoting de PostgreSQL y
los distintos tipos de comentario. Nada más: no tokeniza ni parsea.

POR QUÉ EXISTE Y NO ESTÁ COPIADO EN CADA LLAMADOR
-------------------------------------------------
``query_policy._scan_normalize`` ya tenía este escáner embebido y su contrato de seguridad
(``#`` solo es comentario en MySQL/MariaDB, ``/*!`` y ``/*M!`` son CÓDIGO, los literales se
vacían) se endureció a base de incidentes. El validador de SQL de agentes necesita las MISMAS
fronteras de literal y de comentario, y un segundo escáner con reglas "casi iguales" es
exactamente como dos capas terminan en desacuerdo sobre dónde empieza un comentario: lo que una
ve como literal la otra lo ve como código, y de ahí sale texto ejecutable que ninguna revisa.
Así que hay UNA implementación y dos políticas de uso:

- **Consola** (``backslash_escapes=False``, ``double_quote_is_string=False``): la barra invertida
  NO escapa comillas y ``"..."`` es un identificador citado que se conserva. Es el criterio
  histórico de ``_scan_normalize`` y de ``split_sql_statements``, y NO cambia.
- **Agente** (``backslash_escapes`` en ambos valores, ``double_quote_is_string`` en MySQL): el
  validador escanea el texto DOS veces, con y sin escape por barra invertida, porque el
  ``sql_mode`` del servidor (``NO_BACKSLASH_ESCAPES``) es desconocido y cambia dónde termina un
  literal. Ver ``agent_sql_policy`` (D7).
"""

import re
from dataclasses import dataclass

# Tipos de región.
STRING = "string"
QUOTED_IDENT = "quoted_ident"
DOLLAR = "dollar"
LINE_COMMENT = "line_comment"
BLOCK_COMMENT = "block_comment"
EXEC_COMMENT = "exec_comment"

COMMENT_KINDS = frozenset({LINE_COMMENT, BLOCK_COMMENT, EXEC_COMMENT})

# Prefijos de comentario EJECUTABLE de la familia MySQL, del más largo al más corto para
# que ``/*M!`` no se confunda nunca con ``/*``. ``/*!`` lo ejecutan MySQL y MariaDB;
# ``/*M!`` es exclusivo de MariaDB (y su ``M`` es sensible a mayúsculas en el motor, pero
# acá se acepta también minúscula: reconocer de más solo sobre-bloquea).
EXECUTABLE_COMMENT_PREFIXES = ("/*M!", "/*m!", "/*!")

_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_0-9]*\$")


@dataclass(frozen=True, slots=True)
class LexSpan:
    """
    Una región NO plana del texto: ``sql[start:end]``.

    ``terminated`` es ``False`` cuando la región llegó al final del texto sin cerrarse (literal,
    identificador citado, comentario de bloque o dollar-quoting abiertos). Un comentario de
    línea termina en el salto de línea (que NO consume) o en el fin del texto, y cuenta como
    terminado en ambos casos.
    """

    kind: str
    start: int
    end: int
    terminated: bool = True


def executable_comment_prefix(sql: str, i: int) -> int:
    """
    Largo del prefijo de comentario ejecutable que abre en ``i``, o ``0`` si no hay.

    Devolver el LARGO y no un booleano es lo que permite que el llamador salte el prefijo
    correcto (3 para ``/*!``, 4 para ``/*M!``) sin duplicar la tabla de prefijos.
    """
    for prefix in EXECUTABLE_COMMENT_PREFIXES:
        if sql.startswith(prefix, i):
            return len(prefix)
    return 0


def _scan_quoted(sql: str, i: int, quote: str, *, backslash_escapes: bool) -> tuple[int, bool]:
    """
    Posición posterior al cierre de la región que abre en ``sql[i] == quote``, y si se cerró.

    La comilla DOBLADA (``''``) es una comilla literal dentro de la región. Con
    ``backslash_escapes`` una barra invertida consume también el carácter siguiente.
    """
    n = len(sql)
    i += 1
    while i < n:
        ch = sql[i]
        if backslash_escapes and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1, True
        i += 1
    return n, False


def lex_spans(
    sql: str,
    *,
    engine: str = "mysql",
    backslash_escapes: bool = False,
    double_quote_is_string: bool = False,
    dollar_quotes: bool = True,
) -> list[LexSpan]:
    """
    Las regiones no planas de ``sql``, en orden y sin solaparse. Todo lo que queda entre dos
    regiones es código plano.

    - ``--`` y ``/* … */`` son comentarios en los tres motores; ``#`` **solo en MySQL/MariaDB**
      (en PostgreSQL es el operador XOR de enteros, y tratarlo como comentario borraba código
      ejecutable del texto que revisa la blocklist). ``/*!`` y ``/*M!`` se reportan aparte
      (``EXEC_COMMENT``) porque el motor los EJECUTA.
    - ``'…'`` es un literal de cadena. ``"…"`` es un identificador citado, salvo con
      ``double_quote_is_string`` (MySQL sin ``ANSI_QUOTES``: ahí es un literal). Las comillas
      invertidas son siempre identificador citado.
    - ``$tag$ … $tag$`` (dollar-quoting) solo con ``dollar_quotes``; es el cuerpo de una rutina de
      PostgreSQL, y la consola lo conserva entero.
    """
    hash_is_comment = engine in ("mysql", "mariadb")
    spans: list[LexSpan] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        # --- comentarios ---
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            end = n if j == -1 else j
            spans.append(LexSpan(LINE_COMMENT, i, end))
            i = end
            continue
        if ch == "#" and hash_is_comment:
            j = sql.find("\n", i)
            end = n if j == -1 else j
            spans.append(LexSpan(LINE_COMMENT, i, end))
            i = end
            continue
        if ch == "/" and sql.startswith("/*", i):
            executable = executable_comment_prefix(sql, i)
            j = sql.find("*/", i + (executable or 2))
            end = n if j == -1 else j + 2
            kind = EXEC_COMMENT if executable else BLOCK_COMMENT
            spans.append(LexSpan(kind, i, end, terminated=j != -1))
            i = end
            continue

        # --- literales de cadena ---
        if ch == "'" or (ch == '"' and double_quote_is_string):
            end, closed = _scan_quoted(sql, i, ch, backslash_escapes=backslash_escapes)
            spans.append(LexSpan(STRING, i, end, terminated=closed))
            i = end
            continue

        # --- identificadores citados ---
        if ch in ('"', "`"):
            # En un identificador citado la barra invertida nunca escapa nada.
            end, closed = _scan_quoted(sql, i, ch, backslash_escapes=False)
            spans.append(LexSpan(QUOTED_IDENT, i, end, terminated=closed))
            i = end
            continue

        # --- dollar-quoting de PostgreSQL ---
        if ch == "$" and dollar_quotes:
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                end = n if j == -1 else j + len(tag)
                spans.append(LexSpan(DOLLAR, i, end, terminated=j != -1))
                i = end
                continue

        i += 1
    return spans


def literal_spans(sql: str, engine: str, backslash_escapes: bool) -> list[tuple[int, int]]:
    """
    ``(inicio, fin)`` de cada literal de cadena de ``sql`` (``'…'``, ``"…"`` en MySQL y
    dollar-quoting en PostgreSQL). Lo que cae FUERA de estos rangos y de los comentarios es
    código plano: ahí un ``;`` separa sentencias y un ``--`` abre un comentario.

    ``backslash_escapes`` decide si ``\\'`` cierra o no el literal; el llamador que no conoce el
    ``sql_mode`` del motor tiene que llamarla con los dos valores y exigir que coincidan.
    """
    family_mysql = engine in ("mysql", "mariadb")
    return [
        (s.start, s.end)
        for s in lex_spans(
            sql,
            engine=engine,
            backslash_escapes=backslash_escapes,
            double_quote_is_string=family_mysql,
            dollar_quotes=not family_mysql,
        )
        if s.kind in (STRING, DOLLAR)
    ]
