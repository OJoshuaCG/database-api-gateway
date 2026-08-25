"""
Hechos derivados del SQL de una migración de blueprint, sin tocar ningún motor.

Responde, con una sola pasada y antes de aplicar nada, a las preguntas que hoy solo se
contestan fallando:

- ¿el SQL parsea? (un punto y coma de menos, un paréntesis sin cerrar)
- ¿se traduce con certeza a PostgreSQL, o hará falta un ``up_sql_postgresql``?
- ¿siembra datos? (``INSERT``/``UPDATE``/``DELETE``/``REPLACE``/``LOAD DATA``/``COPY``)
- ¿fuerza algún ``COLLATE`` / ``CHARACTER SET`` explícito?
- ¿hay sentencias destructivas (``DROP``, ``TRUNCATE``, ``DELETE`` sin ``WHERE``)?
- ¿toca la contabilidad interna del gateway (``_gw_*``)?
- si falla a mitad, ¿se podrá auto-reconciliar?

Todo es PURO: sin motor, sin ORM, sin sesión. Se apoya en las piezas que ya existen
(``split_sql_statements``, ``query_policy``, ``SqlTranslator``, ``identifiers``) en vez de
reimplementar un clasificador. El único análisis propio es el de collations, que no existía.

**El dialecto es SIEMPRE MySQL, y no es un parámetro.** El ``up_sql`` de una migración se
escribe en estilo MySQL por contrato y el gateway lo traduce al aplicar. Cuando este módulo
aceptaba el motor del DESTINO, validar contra una BD PostgreSQL parseaba SQL de MySQL con la
gramática equivocada: sqlglot no fallaba, simplemente no reconocía las tablas, y la
comprobación de catálogo se volvía un no-op silencioso que además decía que todo estaba bien.
El motor del destino solo decide contra qué catálogo se contrasta, y eso vive en el
controlador.

Lo que este módulo NO puede saber: si las tablas que el SQL referencia existen en el destino.
Un ``ALTER TABLE`` sobre una tabla inexistente es sintácticamente impecable. Para eso hace
falta el catálogo del motor, y por eso ``requires_existing_tables`` se expone aparte: es la
entrada de esa comprobación.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import sqlglot
from sqlglot import exp

from app.models.enums import EngineType
from app.services.db_admin import query_policy
from app.services.db_admin.identifiers import references_gateway_internal_table
from app.services.db_admin.migration_progress import is_resumable
from app.services.db_admin.sql_dialect import (
    SqlTranslator,
    mask_quoted_spans,
    split_sql_statements,
)

# Dialecto de AUTORÍA del `up_sql`. Ver el docstring del módulo: no se parametriza a
# propósito.
_DIALECT = "mysql"
_ENGINE = EngineType.mysql.value

# Por encima de este tamaño no se memoiza: el SQL enorme es raro, y es justo el que llenaría
# la caché. Sin la cota, 512 entradas de hasta 256 KB retienen ~128 MB en un proceso de larga
# vida.
_CACHE_MAX_SQL_BYTES = 64 * 1024

# `query_policy` NO marca estas tres como escritura, y las tres siembran datos:
#   - `LOAD DATA` y `COPY` caen en la blocklist (danger=blocked) porque leen archivos del
#     host; para el clasificador son "prohibidas", no "escrituras".
#   - `REPLACE INTO` parsea como `exp.Command` genérico → kind='unknown', danger=ddl.
# Los patrones van ANCLADOS al inicio de la sentencia: buscarlos sueltos por el texto hacía
# que `ON UPDATE CASCADE` de una clave foránea —o un `CREATE TRIGGER ... AFTER UPDATE ON`—
# marcaran como siembra un DDL corriente, y una insignia que se enciende siempre no se mira.
_SEED_FALLBACK_RE = re.compile(
    r"^\s*(?:REPLACE\s+(?:LOW_PRIORITY\s+|DELAYED\s+)?INTO\b"
    r"|LOAD\s+DATA\b"
    r"|COPY\b)",
    re.IGNORECASE,
)

# Escrituras "normales", también ancladas. Se usan en `quick_facts`, que no tiene AST.
_QUICK_SEED_RE = re.compile(
    r"^\s*(?:INSERT\b"
    r"|REPLACE\b"
    r"|UPDATE\b"
    r"|DELETE\s+FROM\b"
    r"|MERGE\s+INTO\b"
    r"|LOAD\s+DATA\b"
    r"|COPY\b)",
    re.IGNORECASE,
)

_QUICK_DESTRUCTIVE_RE = re.compile(
    r"^\s*(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|VIEW|INDEX)\b"
    r"|TRUNCATE\b"
    r"|DELETE\s+FROM\b(?![\s\S]*\bWHERE\b)"
    r"|ALTER\s+TABLE\b[\s\S]*\bDROP\s+(?:COLUMN\b|`|\"|[A-Za-z_]))",
    re.IGNORECASE,
)

_DELETE_RE = re.compile(r"^\s*DELETE\b", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)

# El VALOR se captura sobre el texto enmascarado, que conserva las comillas y la longitud
# exacta: eso permite recortar el nombre real del texto ORIGINAL en las mismas posiciones.
# Sin la máscara, un `COMMENT 'usa COLLATE utf8mb4_bin'` daría un falso positivo.
_COLLATE_RE = re.compile(
    r"\b(?:COLLATE|COLLATION)\s*=?\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[A-Za-z0-9_.]+)",
    re.IGNORECASE,
)
_CHARSET_RE = re.compile(
    r"\b(?:CHARACTER\s+SET|CHARSET)\s*=?\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[A-Za-z0-9_.]+)",
    re.IGNORECASE,
)

# CONVERSIÓN de charset/collation, que es OTRA COSA que MENCIONARLO.
#
# Ojo con la tentación de usar ``forced_charsets`` para esto: ``_CHARSET_RE`` matchea
# CUALQUIER mención de ``CHARACTER SET``/``CHARSET``, incluido el ``DEFAULT CHARSET=utf8mb4``
# que lleva prácticamente todo ``CREATE TABLE`` de MySQL. Un guard basado en esa lista
# marcaría como destructiva casi toda migración existente del parque y bloquearía
# ``apply``/``apply_all`` en producción de golpe. Lo que interesa es la FORMA que reescribe
# datos ya almacenados:
#
#   - ``ALTER TABLE t CONVERT TO CHARACTER SET x``  → re-codifica cada valor de texto de la
#     tabla. Hacia un charset más angosto (utf8mb4 → latin1) REEMPLAZA los caracteres que no
#     son representables: pérdida de datos silenciosa. Además puede cambiar el TIPO de una
#     columna (``TEXT``→``MEDIUMTEXT``) para que entre el texto re-codificado.
#   - ``ALTER DATABASE d CHARACTER SET x`` / ``COLLATE y`` → cambia el default que heredan los
#     objetos nuevos. No re-codifica nada por sí solo, pero es la otra mitad de la misma
#     operación y omitirla dejaría el veredicto a mitad de camino.
#
# El propio repo ya clasifica esto como destructivo en el OTRO camino: ``schema_diff.py``
# marca ``destructive=True, data_conversion=True`` ante un cambio de collation o charset de
# columna, con el comentario "re-encoding físico: destructivo". Sin este detector los dos
# clasificadores del repo se contradicen, y el de migraciones —que es el que gobierna el
# guard de entornos— es el permisivo.
#
# sqlglot NO ayuda acá: degrada ambas sentencias a ``exp.Command`` (verificado), así que
# ``_is_destructive`` no las ve. De ahí que sea un detector de texto sobre la máscara.
_CHARSET_CONVERSION_RE = re.compile(
    r"\bCONVERT\s+TO\s+(?:CHARACTER\s+SET|CHARSET)\b"
    r"|\bALTER\s+(?:DATABASE|SCHEMA)\b[^;]*?\b(?:CHARACTER\s+SET|CHARSET|COLLATE)\b",
    re.IGNORECASE,
)

# Objetos cuyo cuerpo sqlglot suele entregar como `Command` sin analizar. Se declaran sus
# tablas como NO requeridas: preferimos callar a inventar un "esta tabla no existe" sobre un
# cuerpo que no se ha entendido.
_OPAQUE_CREATE_KINDS = frozenset({"TRIGGER", "PROCEDURE", "FUNCTION", "EVENT"})


@dataclass(frozen=True)
class StatementFact:
    """Una sentencia del lote, con lo que se puede afirmar de ella sin ejecutarla."""

    seq: int
    sql: str
    kind: str
    danger: str
    reasons: tuple[query_policy.Reason, ...]
    seeds: bool
    destructive: bool
    collations: tuple[str, ...]
    charsets: tuple[str, ...]
    # True si la sentencia CONVIERTE charset/collation (no si simplemente lo menciona).
    # Ver ``_CHARSET_CONVERSION_RE`` para por qué la distinción no es cosmética.
    charset_conversion: bool
    parse_error: str | None


@dataclass(frozen=True)
class MigrationFacts:
    statements: tuple[StatementFact, ...]
    has_seed: bool
    forced_collations: tuple[str, ...]
    forced_charsets: tuple[str, ...]
    destructive_statements: tuple[int, ...]
    # ``seq`` de las sentencias que CONVIERTEN charset/collation. Lo consume el guard de
    # entornos (``blocks_destructive_migrations``), que sin esto no ve una migración capaz de
    # re-codificar todas las tablas de una base de producción.
    charset_conversion_statements: tuple[int, ...]
    parse_errors: tuple[tuple[int, str], ...]
    gateway_internal_tables: tuple[str, ...]
    requires_existing_tables: tuple[str, ...]
    creates_tables: tuple[str, ...]
    postgresql_blockers: tuple[str, ...]
    resumable: bool


@dataclass(frozen=True)
class QuickFacts:
    """Lo mínimo para pintar insignias en un listado, sin AST."""

    has_seed: bool
    forced_collations: tuple[str, ...]
    destructive: bool


def _extract(pattern: re.Pattern[str], masked: str, original: str) -> list[str]:
    """
    Valores capturados por ``pattern`` sobre el texto ENMASCARADO, recortados del ORIGINAL.

    ``mask_quoted_spans`` preserva la longitud total, así que los offsets de una y otra
    cadena coinciden carácter a carácter. Buscar en la máscara evita los falsos positivos de
    los literales; recortar del original recupera el nombre real cuando el valor venía
    entrecomillado (``COLLATE "es_ES"`` de PostgreSQL, que en la máscara es todo espacios).
    """
    out: list[str] = []
    for match in pattern.finditer(masked):
        start, end = match.span(1)
        value = original[start:end].strip().strip("\"'`")
        if value and value not in out:
            out.append(value)
    return out


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read=_DIALECT)
    except sqlglot.errors.SqlglotError:
        return None


def _parse_error(sql: str) -> str | None:
    """
    Mensaje de sqlglot al no poder parsear la sentencia, o ``None`` si parsea.

    Solo se llama sobre las sentencias que el clasificador ya marcó como no parseables: ahí
    ``query_policy`` se traga la excepción (le basta con fallar cerrado), pero para un
    validador el mensaje ES el producto — es lo que dice dónde falta el punto y coma.
    """
    try:
        sqlglot.parse_one(sql, read=_DIALECT)
    except sqlglot.errors.SqlglotError as exc:
        first = str(exc).strip().splitlines()
        return first[0] if first else "SQL no parseable"
    return None


def _table_name(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Table):
        return node.name or None
    table = node.find(exp.Table)
    return table.name if table is not None else None


def _all_table_names(node: exp.Expression | None) -> list[str]:
    if node is None:
        return []
    out: list[str] = []
    for table in node.find_all(exp.Table):
        if table.name and table.name not in out:
            out.append(table.name)
    return out


def _tables_of(tree: exp.Expression) -> tuple[list[str], list[str]]:
    """
    Tablas que la sentencia CREA y tablas que necesita PREEXISTENTES.

    La distinción es la que hace usable la comprobación de catálogo: sin ella, un baseline
    —que es todo ``CREATE TABLE``— reportaba cada una de sus tablas como inexistente, y el
    aviso que justificaba abrir conexión al motor era el que más ruido producía.

    Ante la duda se declara "no requerida": un falso negativo pasa desapercibido, un falso
    positivo entrena a ignorar el panel entero.
    """
    if isinstance(tree, exp.Create):
        kind = str(tree.args.get("kind") or "").upper()
        created = _table_name(tree.this)
        if kind in _OPAQUE_CREATE_KINDS:
            # Cuerpo procedural: sqlglot suele devolverlo sin analizar. No se afirma nada.
            return ([created] if created else []), []
        if kind == "INDEX":
            # `CREATE INDEX ... ON t`: no crea tabla, la necesita.
            target = tree.args.get("table") or tree.this
            required = [n for n in _all_table_names(target) if n]
            return [], required
        # TABLE / VIEW / MATERIALIZED VIEW: crea un nombre; un `AS SELECT` sí necesita sus
        # fuentes.
        required = [n for n in _all_table_names(tree.expression) if n != created]
        return ([created] if created else []), required

    if isinstance(tree, exp.Drop):
        if tree.args.get("exists"):  # DROP ... IF EXISTS: no exige nada
            return [], []
        name = _table_name(tree.this)
        return [], ([name] if name else [])

    return [], _all_table_names(tree)


def _is_destructive(tree: exp.Expression | None, kind: str, masked: str) -> bool:
    """
    ``DROP`` (de tabla o de columna), ``TRUNCATE`` o ``DELETE`` sin ``WHERE``.

    Se mira el ÁRBOL y no solo el ``kind`` del clasificador porque
    ``ALTER TABLE t DROP COLUMN c`` es un ``alter`` para él y sin embargo pierde datos. Esa
    discrepancia hacía que el listado marcara «destructiva» y el validador no dijera nada
    sobre el mismo SQL.
    """
    if kind in {"drop", "truncate"}:
        return True
    if _DELETE_RE.match(masked) and not _WHERE_RE.search(masked):
        return True
    if tree is None:
        return False
    if isinstance(tree, exp.Alter) and tree.find(exp.Drop) is not None:
        return True
    return isinstance(tree, (exp.Drop,))


def _analyze_uncached(sql: str, kind: str, has_non_portable: bool) -> MigrationFacts:
    statements = split_sql_statements(sql)

    facts: list[StatementFact] = []
    created: list[str] = []
    created_lower: set[str] = set()
    required: list[str] = []

    for seq, stmt in enumerate(statements):
        plan = query_policy.classify_statement(stmt, engine=_ENGINE, seq=seq)
        masked = mask_quoted_spans(stmt)
        codes = {r.code for r in plan.reasons}
        tree = _parse(stmt)

        seeds = plan.danger == query_policy.WRITE or bool(_SEED_FALLBACK_RE.match(masked))
        destructive = _is_destructive(tree, plan.kind, masked)
        parse_error = _parse_error(stmt) if "unparseable" in codes else None

        if tree is not None:
            makes, needs = _tables_of(tree)
            for name in needs:
                # Solo cuenta lo que NO haya creado una sentencia ANTERIOR del mismo script.
                if name.lower() not in created_lower and name not in required:
                    required.append(name)
            for name in makes:
                if name.lower() not in created_lower:
                    created.append(name)
                    created_lower.add(name.lower())

        facts.append(
            StatementFact(
                seq=seq,
                sql=stmt,
                kind=plan.kind,
                danger=plan.danger,
                reasons=plan.reasons,
                seeds=seeds,
                destructive=destructive,
                collations=tuple(_extract(_COLLATE_RE, masked, stmt)),
                charsets=tuple(_extract(_CHARSET_RE, masked, stmt)),
                charset_conversion=bool(_CHARSET_CONVERSION_RE.search(masked)),
                parse_error=parse_error,
            )
        )

    collations: list[str] = []
    charsets: list[str] = []
    for f in facts:
        collations.extend(c for c in f.collations if c not in collations)
        charsets.extend(c for c in f.charsets if c not in charsets)

    return MigrationFacts(
        statements=tuple(facts),
        has_seed=any(f.seeds for f in facts),
        forced_collations=tuple(collations),
        forced_charsets=tuple(charsets),
        destructive_statements=tuple(f.seq for f in facts if f.destructive),
        charset_conversion_statements=tuple(f.seq for f in facts if f.charset_conversion),
        parse_errors=tuple((f.seq, f.parse_error) for f in facts if f.parse_error),
        gateway_internal_tables=tuple(references_gateway_internal_table(sql)),
        requires_existing_tables=tuple(required),
        creates_tables=tuple(created),
        # SIEMPRE contra PostgreSQL: es la única dirección donde la traducción puede fallar
        # (el up_sql se escribe en estilo MySQL). Es el 422 que hoy solo aparece al aplicar
        # contra una BD PostgreSQL concreta.
        postgresql_blockers=tuple(
            SqlTranslator().translation_blockers(sql, EngineType.postgresql)
        ),
        resumable=is_resumable(
            sql, statements, kind=kind, has_non_portable=has_non_portable
        ),
    )


@lru_cache(maxsize=128)
def _analyze_cached(sql: str, kind: str, has_non_portable: bool) -> MigrationFacts:
    return _analyze_uncached(sql, kind, has_non_portable)


def analyze(
    sql: str, kind: str = "schema", has_non_portable: bool = False
) -> MigrationFacts:
    """
    Analiza el SQL de una migración. Memoizado por (sql, kind, portabilidad).

    Los parámetros son POSICIONALES a propósito: con keyword-only, ``analyze(s)`` y
    ``analyze(s, kind='schema')`` ocupaban dos entradas distintas de la caché para el mismo
    resultado.

    ``kind`` y ``has_non_portable`` vienen de la migración y solo afectan a ``resumable``.
    Pasarlos importa: una migración ``kind='data'`` NUNCA es reanudable, y con el default se
    informaba lo contrario.

    El ``up_sql`` es inmutable salvo por un ``PATCH`` —que recalcula el checksum— así que
    cachear por el texto es seguro. Por encima de ``_CACHE_MAX_SQL_BYTES`` no se cachea:
    esas entradas son las que harían crecer la memoria sin techo.
    """
    if len(sql.encode("utf-8")) > _CACHE_MAX_SQL_BYTES:
        return _analyze_uncached(sql, kind, has_non_portable)
    return _analyze_cached(sql, kind, has_non_portable)


def _quick_facts_uncached(sql: str) -> QuickFacts:
    seeds = False
    destructive = False
    for stmt in split_sql_statements(sql):
        masked = mask_quoted_spans(stmt)
        if not seeds and _QUICK_SEED_RE.match(masked):
            seeds = True
        if not destructive and _QUICK_DESTRUCTIVE_RE.match(masked):
            destructive = True
    return QuickFacts(
        has_seed=seeds,
        forced_collations=tuple(_extract(_COLLATE_RE, mask_quoted_spans(sql), sql)),
        destructive=destructive,
    )


@lru_cache(maxsize=256)
def _quick_facts_cached(sql: str) -> QuickFacts:
    return _quick_facts_uncached(sql)


def quick_facts(sql: str) -> QuickFacts:
    """
    Hechos para las insignias de un LISTADO, solo con regex sobre el SQL enmascarado.

    ``analyze`` parsea cada sentencia con sqlglot (dos veces: clasificador y árbol). Eso es
    correcto para el validador, donde el usuario espera por una respuesta y la precisión es
    el producto, pero pagarlo por cada fila de cada página —con ``up_sql`` de hasta 256 KB—
    sería absurdo para decidir si se dibuja una plantita.

    Se parte en sentencias y los patrones van ANCLADOS a su inicio. Escanear el blob entero
    hacía que ``ON UPDATE CASCADE`` o ``AFTER UPDATE ON`` marcaran siembra en DDL corriente.
    Efecto colateral aceptado: un ``INSERT`` dentro del cuerpo de un trigger deja de contar —
    y es lo correcto, la migración crea el trigger, no siembra datos.

    La máscara sigue siendo obligatoria: sin ella, un ``COMMENT 'ver INSERT INTO …'``
    pintaría una siembra que no existe.
    """
    if len(sql.encode("utf-8")) > _CACHE_MAX_SQL_BYTES:
        return _quick_facts_uncached(sql)
    return _quick_facts_cached(sql)
