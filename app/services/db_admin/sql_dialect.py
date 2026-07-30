"""
Utilidades de dialecto SQL para las migraciones de blueprints.

Tres piezas, todas sin estado y sin tocar ningún motor:

- ``split_sql_statements``: separa un script multi-sentencia en sentencias
  individuales, respetando comillas, comentarios y dollar-quoting de PostgreSQL.
  Necesario porque PyMySQL ejecuta UNA sentencia por ``execute()`` y porque cada
  ``op.execute`` de Alembic debe recibir una sola sentencia.

- ``SqlTranslator``: auto-traduce el ``up_sql`` base (dialecto de referencia: MySQL)
  al motor destino con sqlglot. Devuelve ``None`` si sqlglot no puede transpilar
  (el llamador cae al SQL base / override manual).

- ``RollbackGenerator``: infiere el ``down_sql`` para operaciones ADITIVAS simples
  (CREATE TABLE/INDEX/VIEW, ADD COLUMN). Devuelve ``None`` si alguna sentencia es
  destructiva o no invertible — nunca adivina un rollback que pueda perder datos.

El splitter entiende los cuerpos procedurales con ``;`` internos por dos vías
independientes: la directiva ``DELIMITER`` (explícita, gana siempre) y el seguimiento de
bloques ``BEGIN…END`` dentro de una definición de rutina (implícita). Ver
``split_sql_statements``.
"""

from __future__ import annotations

import re
from functools import lru_cache

import sqlglot
from sqlglot import exp

from app.models.enums import EngineType

# Dialecto de negocio -> dialecto de sqlglot.
_SQLGLOT_DIALECT = {
    EngineType.mysql: "mysql",
    EngineType.mariadb: "mysql",
    EngineType.postgresql: "postgres",
}

# El ``up_sql`` base se escribe en estilo MySQL (dialecto de referencia).
_REFERENCE_DIALECT = "mysql"

# Motores cuyos cuerpos procedurales pueden traer el esquema ORIGEN calificado.
_MYSQL_FAMILY_NAMES = frozenset({"mysql", "mariadb"})

# Tipos de objeto cuyo CUERPO puede referenciar tablas calificadas por esquema (MySQL/
# MariaDB inyectan el esquema ORIGEN en las referencias del cuerpo de vistas/rutinas/
# triggers/eventos). Fuente única de verdad para clone y schema-comparison.
BODY_OBJECT_TYPES = frozenset({"view", "materialized_view", "routine", "trigger", "event"})


def requalify_body_schema(sql: str, source_db: str, target_db: str, engine: str) -> str:
    """
    Re-califica el esquema ORIGEN → DESTINO en el cuerpo de un objeto con cuerpo
    (vista/rutina/trigger/evento).

    MySQL/MariaDB inyectan el esquema ORIGEN en las referencias del cuerpo (p. ej.
    ``information_schema.VIEWS.VIEW_DEFINITION`` siempre trae ``from `origen`.`tabla` ``).
    Emitido tal cual contra otra BD (adopción como versión de blueprint, ejecución ad-hoc
    o clonado), el objeto seguiría leyendo de la BD ORIGEN: fuga cross-database, o sentencia
    rota si el origen no es visible / la tabla no existe con ese esquema en el destino.
    Reescribe SOLO el calificador del esquema origen (``` `origen`. ``` → ``` `destino`. ```),
    preservando referencias intencionales a OTRAS bases (el backtick de cierre delimita el
    nombre completo → no hay match por prefijo). Solo aplica a la familia MySQL/MariaDB
    (PostgreSQL no soporta referencias cross-database y comparte el schema ``public``).
    """
    if not sql or source_db == target_db or engine not in _MYSQL_FAMILY_NAMES:
        return sql
    return sql.replace(f"`{source_db}`.", f"`{target_db}`.")


def strip_self_schema_qualifier(sql: str, database: str, engine: str) -> str:
    """
    Quita del cuerpo el calificador de su PROPIA base de datos (solo MySQL/MariaDB).

    Sirve para COMPARAR dos cuerpos que viven en BDs de nombre distinto. MySQL/MariaDB
    guardan la definición con el esquema calificado —
    ``information_schema.VIEWS.VIEW_DEFINITION`` devuelve siempre
    ``select `midb`.`t`.`col` from `midb`.`t```— así que dos BDs con el MISMO objeto
    lógico tienen cuerpos textualmente distintos: cada una lleva su propio nombre adentro.

    Sin esta normalización, diffear una BD contra su CLON reportaba **toda** vista/rutina/
    trigger/evento como ``modified`` aunque fueran idénticas: un falso positivo garantizado
    en cuanto los nombres de las dos BDs difieren (verificado en MariaDB). Ojo que
    ``normalize_body`` NO alcanza para esto: colapsa whitespace y quita el ``DEFINER``,
    pero el nombre de la BD es parte del texto de la consulta.

    NO enmascara diferencias reales: solo se quita el calificador de la base PROPIA de cada
    lado. Una referencia cruzada a OTRA base (``` `otra_db`.`t` ```) se conserva en ambos
    lados, así que si un lado apunta afuera y el otro no, el diff sigue detectándolo.

    Solo la forma con BACKTICKS, igual que ``requalify_body_schema``: es la que emite el
    servidor al canonicalizar una vista, y mantener el mismo criterio garantiza que lo que
    el clon RE-ESCRIBE y lo que el diff NORMALIZA sean exactamente el mismo concepto.
    """
    if not sql or not database or engine not in _MYSQL_FAMILY_NAMES:
        return sql
    return sql.replace(f"`{database}`.", "")

# --------------------------------------------------------------------------- #
# Detección de cuerpos procedurales (para no cortarlos en su primer ``;``)      #
# --------------------------------------------------------------------------- #
# Una sentencia que ABRE una definición de rutina: su cuerpo puede ser un bloque
# compuesto ``BEGIN…END`` con ``;`` internos. Solo dentro de una de estas sentencias se
# activa el seguimiento de bloques — fuera, un ``BEGIN`` es el inicio de una TRANSACCIÓN
# y contarlo como apertura dejaría el resto del script pegado en una sola sentencia.
# NOTA: sin ``^``. Se usa con ``.match(sql, i)``, que YA ancla en ``i``; un ``^`` solo
# casaría en el inicio real del string y la detección nunca dispararía a media entrada.
_ROUTINE_START_RE = re.compile(
    r"""CREATE\s+
        (?:OR\s+REPLACE\s+)?
        (?:DEFINER\s*=\s*(?:\S+|`[^`]*`(?:@`[^`]*`)?|'[^']*'(?:@'[^']*')?)\s+)?
        (?:AGGREGATE\s+)?
        (?:PROCEDURE|FUNCTION|TRIGGER|EVENT)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Directiva de cliente ``DELIMITER <tok>``: no se envía al motor, cambia el terminador.
# Igual que ``_ROUTINE_START_RE``: sin ``^``, se ancla con el ``pos`` de ``.match``.
_DELIMITER_RE = re.compile(r"[ \t]*DELIMITER[ \t]+(\S+)[ \t]*(?:\r?\n|$)", re.IGNORECASE)

# Palabras que ABREN un bloque y se cierran con un ``END`` **sin sufijo o con ``CASE``**:
#
# - ``BEGIN … END``            (bloque compuesto de la rutina)
# - ``CASE … END CASE``        (CASE *statement*)
# - ``CASE … END``             (CASE *expresión*, p.ej. ``SELECT CASE WHEN … END AS x``)
#
# Deliberadamente NO se cuentan ``IF``/``WHILE``/``LOOP``/``REPEAT``: sus cierres llevan
# sufijo obligatorio (``END IF``, ``END WHILE``, …) y se neutralizan abajo, mientras que
# contarlos como aperturas sería ambiguo — ``IF (c) THEN`` y ``WHILE (c) DO`` son
# statements aunque lleven paréntesis, pero ``IF(a,b,c)`` y ``REPEAT('x',3)`` son
# FUNCIONES, y no hay forma barata de distinguirlos sin parsear la expresión.
_BLOCK_OPENERS = frozenset({"BEGIN", "CASE"})

# ``END <sufijo>`` que cierra un constructo NO contado como apertura => profundidad igual.
_NEUTRAL_END_SUFFIXES = frozenset({"IF", "LOOP", "WHILE", "REPEAT"})

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# Blancos y comentarios (``--``, ``#``, ``/* … */``) al inicio de lo acumulado. Sirve para
# saber si una sentencia todavía no arrancó de verdad: tanto la directiva ``DELIMITER``
# como el reconocimiento de ``CREATE PROCEDURE`` deben seguir funcionando cuando el script
# trae comentarios antes (el caso normal de un dump o de SQL escrito a mano).
_LEADING_NOISE_RE = re.compile(r"(?:\s+|--[^\n]*\n?|\#[^\n]*\n?|/\*.*?\*/)*", re.DOTALL)


def _only_noise(text: str) -> bool:
    """True si ``text`` es solo blancos y/o comentarios (no hay SQL ejecutable)."""
    return _LEADING_NOISE_RE.match(text).end() == len(text)


def _word_at(sql: str, i: int) -> str | None:
    """Palabra que empieza EXACTAMENTE en ``i`` (o None si ahí no arranca una)."""
    if i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$"):
        return None  # estamos a mitad de un identificador (p.ej. ELSEIF, my_end)
    m = _WORD_RE.match(sql, i)
    return m.group(0) if m else None


def _next_word(sql: str, i: int) -> tuple[str | None, int]:
    """Siguiente palabra desde ``i`` saltando espacios/comentarios; devuelve (palabra, pos)."""
    n = len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
            continue
        if sql.startswith("--", i) or sql[i] == "#":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        break
    m = _WORD_RE.match(sql, i)
    return (m.group(0), m.start()) if m else (None, i)


def split_sql_statements(sql: str) -> list[str]:
    """
    Divide un script SQL en sentencias por ``;`` de nivel superior.

    Respeta: literales ``'…'`` y ``"…"``, identificadores ``` `…` ```, dollar-quoting
    ``$tag$…$tag$`` (PostgreSQL), comentarios de línea ``--`` y ``#`` y de bloque
    ``/* … */``. Descarta sentencias vacías.

    **Cuerpos procedurales con ``;`` internos.** Un ``CREATE PROCEDURE/FUNCTION/TRIGGER/
    EVENT`` cuyo cuerpo es un bloque ``BEGIN … END`` contiene ``;`` que NO terminan la
    sentencia. Cortar ahí produce SQL truncado que el motor rechaza — el síntoma típico es
    un ``CREATE PROCEDURE`` que muere en su primer ``DECLARE`` con
    ``(1064, "…syntax… near '' at line N")``. Se maneja por dos vías independientes:

    1. **``DELIMITER <tok>``** (explícita, la de cualquier dump de ``mysqldump``): cambia el
       terminador de sentencia. Es una directiva de CLIENTE: se consume acá y nunca se
       envía al motor. Sirve cualquier token (``//``, ``$$``, ``;;``, …) y se reconoce
       aunque vengan comentarios antes; mientras el terminador esté activo, un token que
       arranca con ``$`` NO se lee como dollar-quoting de PostgreSQL (ver abajo).
    2. **Bloques ``BEGIN…END``** (implícita): dentro de una sentencia que abre una rutina
       (``_ROUTINE_START_RE``) se lleva la cuenta de bloques abiertos y un ``;`` solo
       termina la sentencia con la cuenta en cero. Se cuentan como aperturas ``BEGIN``,
       ``CASE``, ``IF``, ``LOOP``, ``WHILE`` y ``REPEAT``, y ``END`` consume el sufijo que
       le corresponda (``END IF``, ``END CASE``, …) — así se equilibran tanto el ``CASE``
       *statement* (``END CASE``) como el ``CASE`` *expresión* de un ``SELECT``
       (``… END``), que de otro modo cerraría el bloque de más.

    El seguimiento se activa **solo** dentro de una definición de rutina: fuera, un
    ``BEGIN`` es el arranque de una transacción y tratarlo como apertura dejaría todo el
    resto del script pegado en una sola sentencia.

    PostgreSQL ya venía cubierto por el dollar-quoting (``$$…$$``, ``$body$…$body$``) de
    sus funciones ``plpgsql``; la vía 2 le agrega los cuerpos ``BEGIN ATOMIC`` de SQL/PSM
    (PostgreSQL 14+), que **no** llevan dollar-quoting y antes también se partían.

    **Límite conocido**: un script no puede usar ``$$`` como terminador (vía 1) y como
    dollar-quoting (PostgreSQL) A LA VEZ — el token es ambiguo y gana el terminador. No es
    una limitación práctica: ``DELIMITER`` es una directiva del cliente ``mysql`` y no
    existe en PostgreSQL, donde el ``;`` por defecto ya basta.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    delimiter = ";"
    # Profundidad de bloques del cuerpo procedural en curso; None = no estamos dentro de
    # una definición de rutina (no se cuentan bloques).
    depth: int | None = None

    def _at_statement_start() -> bool:
        # Tolerante a comentarios: lo acumulado puede ser blancos y/o comentarios y la
        # sentencia sigue sin arrancar. Sin esto, un ``-- comentario`` antes de
        # ``DELIMITER $$`` hacía que la directiva se enviara al motor (1064) y uno antes
        # de ``CREATE PROCEDURE`` desactivaba el conteo de bloques ``BEGIN…END``.
        return _only_noise("".join(buf))

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # Directiva de cliente DELIMITER: solo al principio de una sentencia (si no,
        # ``DELIMITER`` podría ser una palabra dentro de un literal o un identificador).
        if (ch in ("D", "d")) and _at_statement_start():
            m = _DELIMITER_RE.match(sql, i)
            if m:
                delimiter = m.group(1)
                i = m.end()
                # La directiva NO se emite: es del cliente, no del motor. Los comentarios
                # ya acumulados se conservan (documentan la sentencia que sigue); si al
                # final no queda SQL real, el filtro de ``_only_noise`` los descarta.
                continue

        # ¿Esta sentencia abre una definición de rutina? Solo ahí se cuentan bloques.
        if depth is None and _at_statement_start() and (ch in ("C", "c")):
            if _ROUTINE_START_RE.match(sql, i):
                depth = 0

        # Comentario de línea: -- ... \n  o  # ... \n
        if (ch == "-" and nxt == "-") or ch == "#":
            j = sql.find("\n", i)
            if j == -1:
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i : j + 1])
                i = j + 1
            continue

        # Comentario de bloque: /* ... */
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i : j + 2])
                i = j + 2
            continue

        # Literal/identificador delimitado por ' " `
        if ch in ("'", '"', "`"):
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == ch:
                    # Comilla duplicada => escape, sigue dentro del literal.
                    if i + 1 < n and sql[i + 1] == ch:
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Dollar-quoting de PostgreSQL: $tag$ ... $tag$
        #
        # OJO: si la directiva ``DELIMITER`` fijó un terminador que empieza con ``$`` (el
        # ``DELIMITER $$`` idiomático de MySQL/MariaDB, tan común como ``//``), ese token
        # es el TERMINADOR de sentencia, no la apertura de un literal. Hay que dejarlo
        # pasar al chequeo de fin de sentencia: si no, ``$$`` abría un dollar-quote que se
        # cerraba en el ``$$`` SIGUIENTE y pegaba dos sentencias en una sola
        # (``DROP PROCEDURE …$$ CREATE PROCEDURE …``), que el motor rechaza con
        # ``(1064, "…syntax… near '$$\\n\\nCREATE PROCEDURE …'")``. Con ``//`` no pasaba
        # nada porque ``//`` no colisiona con ninguna sintaxis del scanner.
        if ch == "$" and not (delimiter != ";" and sql.startswith(delimiter, i)):
            tag_end = sql.find("$", i + 1)
            if tag_end != -1 and sql[i + 1 : tag_end].replace("_", "").isalnum() or (
                tag_end == i + 1
            ):
                if tag_end != -1:
                    tag = sql[i : tag_end + 1]  # p.ej. "$$" o "$body$"
                    close = sql.find(tag, tag_end + 1)
                    if close != -1:
                        buf.append(sql[i : close + len(tag)])
                        i = close + len(tag)
                        continue

        # Conteo de bloques BEGIN…END / CASE…END dentro de una definición de rutina.
        if depth is not None and (ch.isalpha() or ch == "_"):
            word = _word_at(sql, i)
            if word is not None:
                upper = word.upper()
                if upper == "END":
                    nxt_word, nxt_pos = _next_word(sql, i + len(word))
                    suffix = nxt_word.upper() if nxt_word else ""
                    if suffix in _NEUTRAL_END_SUFFIXES:
                        # ``END IF`` / ``END WHILE`` / … : cierra un constructo que no
                        # contamos como apertura. Se consume el sufijo (para no leerlo
                        # como palabra suelta) sin tocar la profundidad.
                        buf.append(sql[i : nxt_pos + len(nxt_word)])
                        i = nxt_pos + len(nxt_word)
                    elif suffix == "CASE":
                        # ``END CASE`` cierra el CASE *statement* que sí contamos.
                        buf.append(sql[i : nxt_pos + len(nxt_word)])
                        i = nxt_pos + len(nxt_word)
                        depth = max(0, depth - 1)
                    else:
                        # ``END`` a secas: cierra el BEGIN del cuerpo o un CASE expresión.
                        buf.append(word)
                        i += len(word)
                        depth = max(0, depth - 1)
                    continue
                if upper in _BLOCK_OPENERS:
                    depth += 1
                    buf.append(word)
                    i += len(word)
                    continue

        # Fin de sentencia (terminador actual; ``;`` salvo que DELIMITER lo haya cambiado).
        # Con un cuerpo procedural abierto (depth > 0) el ``;`` es interno: no termina nada.
        if sql.startswith(delimiter, i) and not depth:
            stmt = "".join(buf).strip()
            if stmt and not _only_noise(stmt):
                statements.append(stmt)
            buf = []
            depth = None  # la próxima sentencia se re-evalúa desde cero
            i += len(delimiter)
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    # Una "sentencia" que solo tiene comentarios (típico: el ``DELIMITER ;`` final de un
    # dump precedido de comentarios, o comentarios al pie del script) no se emite: el motor
    # la rechazaría con ``(1065, 'Query was empty')``.
    if tail and not _only_noise(tail):
        statements.append(tail)
    return statements


# --------------------------------------------------------------------------- #
# DDL específico de MySQL que sqlglot NO traduce a PostgreSQL                   #
# --------------------------------------------------------------------------- #
# sqlglot transpila bien las expresiones y los tipos, pero varias formas de DDL de
# MySQL las emite VERBATIM al escribir PostgreSQL — produciendo SQL sintácticamente
# inválido que solo revienta al ejecutarse contra el motor. Verificado invocando
# ``sqlglot.transpile(read='mysql', write='postgres')``:
#
#   'DROP INDEX `i` ON `t`'                 -> 'DROP INDEX "i" ON "t"'          (PG no acepta ON)
#   'ALTER TABLE `t` DROP INDEX `u`'        -> 'ALTER TABLE "t" DROP INDEX "u"' (PG: DROP CONSTRAINT)
#   'ALTER TABLE `t` DROP FOREIGN KEY `f`'  -> '… DROP FOREIGN KEY "f"'         (PG: DROP CONSTRAINT)
#   'ALTER TABLE `t` DROP CHECK `c`'        -> '… DROP CHECK `c`'               (¡deja backticks!)
#   'ALTER TABLE `t` MODIFY COLUMN …'       -> '… MODIFY COLUMN …'              (PG: ALTER COLUMN)
#   'ALTER TABLE `t` DROP PRIMARY KEY'      -> '… DROP PRIMARY KEY'             (PG: DROP CONSTRAINT)
#
# Las cuatro primeras tienen una reescritura EXACTA y se aplican acá. Las dos últimas NO:
# ``MODIFY COLUMN`` hay que partirlo en ``ALTER COLUMN … TYPE`` + ``SET/DROP NOT NULL`` +
# ``SET/DROP DEFAULT`` (semántica, no textual) y ``DROP PRIMARY KEY`` necesita el NOMBRE del
# constraint, que en PostgreSQL es convencionalmente ``<tabla>_pkey`` pero no está
# garantizado. Adivinar cualquiera de las dos sería peor que fallar: se reportan como
# BLOQUEANTES para que el admin provea un ``up_sql_postgresql`` explícito.
# Un identificador SQL: `backtick`, "doble comilla" o desnudo.
_IDENT = r'(?:`(?:[^`]|``)+`|"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'

# ``DROP INDEX x ON t`` (MySQL) -> ``DROP INDEX x`` (PostgreSQL). Solo cuando la sentencia
# ARRANCA con DROP INDEX: dentro de un ALTER TABLE, ``DROP INDEX`` significa otra cosa
# (soltar una constraint) y lo maneja la reescritura de abajo.
_PG_DROP_INDEX_ON_RE = re.compile(
    rf"^(\s*DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?{_IDENT})\s+ON\s+{_IDENT}\s*$",
    re.IGNORECASE | re.DOTALL,
)
# ``ALTER TABLE t DROP {FOREIGN KEY|INDEX|KEY|CHECK} x`` -> ``DROP CONSTRAINT x``.
_PG_DROP_CONSTRAINT_RE = re.compile(
    rf"\bDROP\s+(?:FOREIGN\s+KEY|INDEX|KEY|CHECK)\s+({_IDENT})", re.IGNORECASE
)
_ALTER_TABLE_RE = re.compile(r"^\s*ALTER\s+TABLE\b", re.IGNORECASE)


def _pg_requote(ident: str) -> str:
    """Normaliza un identificador a comillas dobles (PostgreSQL)."""
    if ident.startswith("`") and ident.endswith("`"):
        return '"' + ident[1:-1].replace("``", "`").replace('"', '""') + '"'
    return ident


def _rewrite_pg_statement(stmt: str) -> str:
    """
    Corrige, en UNA sentencia ya transpilada, el DDL que sqlglot dejó en sintaxis MySQL.

    Se hace por sentencia y con contexto: aplicar las reglas sobre el script completo
    hacía que la segunda pisara el resultado de la primera (``DROP INDEX i ON t`` quedaba
    convertido en ``DROP CONSTRAINT i``, que no es lo mismo ni es válido suelto).
    """
    m = _PG_DROP_INDEX_ON_RE.match(stmt)
    if m:
        return m.group(1)
    if _ALTER_TABLE_RE.match(stmt):
        return _PG_DROP_CONSTRAINT_RE.sub(
            lambda mm: f"DROP CONSTRAINT {_pg_requote(mm.group(1))}", stmt
        )
    return stmt

# Construcciones que NO se pueden traducir con certeza: bloquean la aplicación.
_PG_BLOCKERS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bMODIFY\s+COLUMN\b", re.IGNORECASE),
     "MODIFY COLUMN (PostgreSQL usa ALTER COLUMN … TYPE / SET NOT NULL / SET DEFAULT)"),
    (re.compile(r"\bCHANGE\s+COLUMN\b", re.IGNORECASE),
     "CHANGE COLUMN (PostgreSQL usa RENAME COLUMN + ALTER COLUMN)"),
    (re.compile(r"\bDROP\s+PRIMARY\s+KEY\b", re.IGNORECASE),
     "DROP PRIMARY KEY (PostgreSQL necesita el nombre del constraint: DROP CONSTRAINT …)"),
    (re.compile(r"\bAUTO_INCREMENT\s*=", re.IGNORECASE),
     "AUTO_INCREMENT = n (PostgreSQL usa ALTER SEQUENCE … RESTART)"),
    (re.compile(r"\bENGINE\s*=", re.IGNORECASE),
     "ENGINE = … (no existe en PostgreSQL)"),
    (re.compile(r"`", re.IGNORECASE),
     "identificadores entre backticks que la traducción no convirtió"),
)


@lru_cache(maxsize=256)
def _transpile_cached(sql: str, read: str, write: str) -> tuple[str, ...] | None:
    """
    ``sqlglot.transpile`` memoizado. ``None`` si sqlglot no pudo parsear.

    El transpilado es CARO (parsea y re-genera todo el script) y se pedía varias veces por
    el mismo SQL en una sola operación: ``select_up_sql`` en el codegen, otra vez al contar
    sentencias, otra en el guard de traducibilidad. El ``up_sql`` de una migración es
    inmutable (cualquier cambio pasa por un ``PATCH`` que recalcula el checksum), así que
    cachear por (sql, dialectos) es seguro. Acotado a 256 entradas para no crecer sin
    límite con scripts grandes.
    """
    try:
        return tuple(sqlglot.transpile(sql, read=read, write=write))
    except sqlglot.errors.SqlglotError:
        return None


class SqlTranslator:
    """Auto-traduce el ``up_sql`` base (MySQL) al motor destino con sqlglot."""

    def translate(self, sql: str, to_engine: EngineType) -> str | None:
        """
        Devuelve el SQL transpilado al motor destino, o ``None`` si sqlglot falla.

        Para MySQL/MariaDB devuelve el SQL base sin tocar (es el dialecto de
        referencia): así se preserva exactamente lo que escribió el admin.

        Para PostgreSQL, además de sqlglot se aplican las reescrituras de ``_PG_REWRITES``:
        sin ellas, un ``DROP INDEX … ON …`` o un ``DROP FOREIGN KEY`` salían del transpilado
        con sintaxis MySQL intacta y solo fallaban contra el motor.
        """
        if to_engine in (EngineType.mysql, EngineType.mariadb):
            return sql
        write = _SQLGLOT_DIALECT.get(to_engine)
        if write is None:
            return None
        parts = _transpile_cached(sql, _REFERENCE_DIALECT, write)
        if not parts:
            return None
        cleaned = [p.strip() for p in parts if p.strip()]
        if to_engine == EngineType.postgresql:
            cleaned = [_rewrite_pg_statement(p) for p in cleaned]
        return ";\n".join(cleaned)

    def translation_blockers(self, sql: str, to_engine: EngineType) -> list[str]:
        """
        Construcciones del SQL que NO se pueden traducir con certeza al motor destino.

        Lista vacía = la traducción es confiable. Se evalúa sobre el resultado YA
        traducido (y reescrito): lo que sobreviva ahí es genuinamente intraducible.

        Existe porque ``translate`` devolviendo ``None`` no alcanza como defensa: el
        llamador cae al SQL base, que en dialecto MySQL crudo es igual de inválido contra
        PostgreSQL. Lo correcto es detectarlo ANTES de tocar el motor y exigir un override
        explícito (``up_sql_postgresql``) — fail-closed, y accionable para el admin.
        """
        if to_engine != EngineType.postgresql:
            return []
        translated = self.translate(sql, to_engine)
        if translated is None:
            return ["sqlglot no pudo transpilar el SQL a PostgreSQL"]
        found: list[str] = []
        for pattern, label in _PG_BLOCKERS:
            if pattern.search(translated) and label not in found:
                found.append(label)
        return found

    def translate_all(self, sql: str) -> dict[str, str]:
        """
        Traduce a todos los motores soportados para mostrar en la API.
        Solo incluye las traducciones que sqlglot pudo producir.
        """
        out: dict[str, str] = {}
        for engine in (EngineType.mysql, EngineType.postgresql):
            translated = self.translate(sql, engine)
            if translated is not None:
                out[engine.value] = translated
        return out


class RollbackGenerator:
    """Infiere ``down_sql`` para operaciones aditivas; ``None`` si no es seguro."""

    def generate(self, up_sql: str) -> str | None:
        """
        Genera el rollback (en estilo MySQL de referencia) invirtiendo cada sentencia
        en ORDEN INVERSO. Si CUALQUIER sentencia no es invertible de forma segura,
        devuelve ``None`` (no se arriesga un rollback parcial o destructivo).
        """
        statements = split_sql_statements(up_sql)
        if not statements:
            return None

        reversed_stmts: list[str] = []
        for stmt in statements:
            try:
                parsed = sqlglot.parse_one(stmt, read=_REFERENCE_DIALECT)
            except sqlglot.errors.SqlglotError:
                return None
            inverse = self._invert(parsed)
            if inverse is None:
                return None
            reversed_stmts.append(inverse)

        reversed_stmts.reverse()
        return ";\n".join(reversed_stmts) + ";"

    def _invert(self, node: exp.Expression) -> str | None:
        if isinstance(node, exp.Create):
            return self._invert_create(node)
        if isinstance(node, exp.Alter):
            return self._invert_alter(node)
        # DROP / INSERT / UPDATE / DELETE / TRUNCATE / etc.: no invertible sin pérdida.
        return None

    def _invert_create(self, node: exp.Create) -> str | None:
        kind = (node.args.get("kind") or "").upper()
        this = node.this

        if kind == "TABLE":
            name = self._object_name(this)
            return f"DROP TABLE IF EXISTS {name}" if name else None

        if kind == "VIEW":
            name = self._object_name(this)
            return f"DROP VIEW IF EXISTS {name}" if name else None

        if kind == "INDEX":
            # this es un exp.Index con nombre y tabla.
            idx_name = None
            table_name = None
            if isinstance(this, exp.Index):
                ident = this.this
                idx_name = ident.name if ident is not None else None
                table = this.args.get("table")
                if table is not None:
                    table_name = table.name
            if not idx_name:
                return None
            # Estilo MySQL: DROP INDEX name ON table (la traducción a PG quita el ON).
            if table_name:
                return f"DROP INDEX {idx_name} ON {table_name}"
            return f"DROP INDEX {idx_name}"

        if kind in ("PROCEDURE", "FUNCTION"):
            name = self._object_name(this)
            return f"DROP {kind} IF EXISTS {name}" if name else None

        return None

    def _invert_alter(self, node: exp.Alter) -> str | None:
        kind = (node.args.get("kind") or "TABLE").upper()
        if kind != "TABLE":
            return None
        table = node.this.name if node.this is not None else None
        if not table:
            return None
        actions = node.args.get("actions") or []
        if not actions:
            return None

        inverses: list[str] = []
        for action in actions:
            # ADD COLUMN -> DROP COLUMN (única acción que invertimos con seguridad).
            if isinstance(action, exp.ColumnDef):
                inverses.append(
                    f"ALTER TABLE {table} DROP COLUMN {action.name}"
                )
            else:
                # ADD CONSTRAINT, MODIFY, DROP, RENAME, etc.: no invertible con certeza.
                return None
        return ";\n".join(inverses)

    @staticmethod
    def _object_name(this: exp.Expression | None) -> str | None:
        if this is None:
            return None
        if isinstance(this, exp.Schema):  # CREATE TABLE -> Schema(this=Table)
            this = this.this
        if isinstance(this, exp.Table):
            return this.name
        name = getattr(this, "name", None)
        return name or None
