"""
Hechos derivados del SQL de una migración de blueprint, sin tocar ningún motor.

Responde, con una sola pasada y antes de aplicar nada, a las preguntas que hoy solo se
contestan fallando:

- ¿el SQL parsea? (un punto y coma de menos, un paréntesis sin cerrar)
- ¿se traduce con certeza a PostgreSQL, o hará falta un ``up_sql_postgresql``?
- ¿siembra datos? (``INSERT``/``UPDATE``/``DELETE``/``REPLACE``/``LOAD DATA``/``COPY``)
- ¿fuerza algún ``COLLATE`` / ``CHARACTER SET`` explícito?
- ¿hay sentencias destructivas (``DROP``/``TRUNCATE``/``DELETE`` sin ``WHERE``)?
- ¿toca la contabilidad interna del gateway (``_gw_*``)?
- si falla a mitad, ¿se podrá auto-reconciliar?

Todo es PURO: sin motor, sin ORM, sin sesión. Se apoya en las piezas que ya existen
(``split_sql_statements``, ``query_policy``, ``SqlTranslator``, ``identifiers``) en vez de
reimplementar un clasificador — el único análisis propio es el de collations, que no
existía en ningún lado.

Lo que este módulo NO puede saber: si las tablas que el SQL referencia existen en el
destino. Un ``ALTER TABLE`` sobre una tabla inexistente es sintácticamente impecable. Para
eso hace falta consultar el catálogo del motor, y por eso ``referenced_tables`` se expone
aparte: es la entrada de esa comprobación, que sí abre conexión y vive en el controlador.
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

# `query_policy` NO marca estas tres como escritura, y las tres siembran datos:
#   - `LOAD DATA` y `COPY` caen en la blocklist (danger=blocked, kind=blocked) porque leen
#     archivos del host; para el clasificador son "prohibidas", no "escrituras".
#   - `REPLACE INTO` parsea como `exp.Command` genérico, así que sale como kind='unknown'
#     con danger=ddl.
# Detectarlas por `danger == WRITE` las dejaría fuera y la insignia de siembra mentiría.
_SEED_FALLBACK_RE = re.compile(
    r"^\s*(?:REPLACE\s+(?:LOW_PRIORITY\s+|DELAYED\s+)?INTO\b"
    r"|LOAD\s+DATA\b"
    r"|COPY\b)",
    re.IGNORECASE,
)

# `DELETE` sin `WHERE` vacía la tabla entera. `DROP`/`TRUNCATE` los da el clasificador por
# `kind`, así que aquí solo hace falta el caso que depende de la ausencia de una cláusula.
_DELETE_RE = re.compile(r"^\s*DELETE\b", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)

_DESTRUCTIVE_KINDS = frozenset({"drop", "truncate"})

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
    parse_error: str | None


@dataclass(frozen=True)
class MigrationFacts:
    statements: tuple[StatementFact, ...]
    has_seed: bool
    forced_collations: tuple[str, ...]
    forced_charsets: tuple[str, ...]
    destructive_statements: tuple[int, ...]
    parse_errors: tuple[tuple[int, str], ...]
    gateway_internal_tables: tuple[str, ...]
    referenced_tables: tuple[str, ...]
    postgresql_blockers: tuple[str, ...]
    resumable: bool

    @property
    def is_valid(self) -> bool:
        """Sin errores de parseo, sin tablas internas del gateway y traducible."""
        return not (
            self.parse_errors or self.gateway_internal_tables or self.postgresql_blockers
        )


def _extract(pattern: re.Pattern[str], masked: str, original: str) -> list[str]:
    """
    Valores capturados por ``pattern`` sobre el texto ENMASCARADO, recortados del ORIGINAL.

    `mask_quoted_spans` preserva la longitud total, así que los offsets de una y otra
    cadena coinciden carácter a carácter. Buscar en la máscara evita los falsos positivos
    de los literales; recortar del original recupera el nombre real cuando el valor venía
    entrecomillado (``COLLATE "es_ES"`` de PostgreSQL, que en la máscara es todo espacios).
    """
    out: list[str] = []
    for match in pattern.finditer(masked):
        start, end = match.span(1)
        value = original[start:end].strip().strip("\"'`")
        if value and value not in out:
            out.append(value)
    return out


def _parse_error(sql: str, dialect: str) -> str | None:
    """
    Mensaje de sqlglot al no poder parsear la sentencia, o ``None`` si parsea.

    Solo se llama sobre las sentencias que el clasificador ya marcó como no parseables:
    ahí ``query_policy`` se traga la excepción (le basta con fallar cerrado), pero para un
    validador el mensaje ES el producto — es lo que dice dónde falta el punto y coma.
    """
    try:
        sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        return str(exc).strip().splitlines()[0] if str(exc).strip() else "SQL no parseable"
    return None


def _tables_of(sql: str, dialect: str) -> list[str]:
    """Tablas referenciadas por la sentencia; vacío si no parsea."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return []
    if tree is None:
        return []
    names: list[str] = []
    for node in tree.find_all(exp.Table):
        name = node.name
        if name and name not in names:
            names.append(name)
    return names


@lru_cache(maxsize=128)
def analyze(sql: str, engine: str, *, kind: str = "schema", has_non_portable: bool = False) -> MigrationFacts:
    """
    Analiza el SQL de una migración. Memoizado por (sql, motor, kind, portabilidad).

    El ``up_sql`` de una migración es inmutable salvo por un ``PATCH`` —que recalcula el
    checksum— así que cachear por el texto es seguro. Es el mismo criterio y el mismo tope
    de orden de magnitud que ``sql_dialect._transpile_cached``: sin caché, cada listado de
    versiones volvería a parsear todo el SQL de la página.
    """
    statements = split_sql_statements(sql)
    dialect = "postgres" if engine == EngineType.postgresql.value else "mysql"

    facts: list[StatementFact] = []
    tables: list[str] = []
    for seq, stmt in enumerate(statements):
        plan = query_policy.classify_statement(stmt, engine=engine, seq=seq)
        masked = mask_quoted_spans(stmt)
        codes = {r.code for r in plan.reasons}

        seeds = plan.danger == query_policy.WRITE or bool(_SEED_FALLBACK_RE.match(masked))
        destructive = plan.kind in _DESTRUCTIVE_KINDS or (
            bool(_DELETE_RE.match(masked)) and not _WHERE_RE.search(masked)
        )
        parse_error = _parse_error(stmt, dialect) if "unparseable" in codes else None

        for name in _tables_of(stmt, dialect):
            if name not in tables:
                tables.append(name)

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
        parse_errors=tuple((f.seq, f.parse_error) for f in facts if f.parse_error),
        gateway_internal_tables=tuple(references_gateway_internal_table(sql)),
        referenced_tables=tuple(tables),
        # SIEMPRE contra PostgreSQL: es la única dirección donde la traducción puede
        # fallar (el up_sql se escribe en estilo MySQL), y es el 422 que hoy solo aparece
        # al aplicar contra una BD PostgreSQL concreta. Pasar `engine` aquí devolvería
        # lista vacía al validar desde el formulario, que es cuando más sirve saberlo.
        postgresql_blockers=tuple(
            SqlTranslator().translation_blockers(sql, EngineType.postgresql)
        ),
        resumable=is_resumable(
            sql, statements, kind=kind, has_non_portable=has_non_portable
        ),
    )


@dataclass(frozen=True)
class QuickFacts:
    """Lo mínimo para pintar insignias en un listado, sin AST."""

    has_seed: bool
    forced_collations: tuple[str, ...]
    destructive: bool


# Prefiltros baratos. Mismo criterio que `_MYSQLISM_RE` y `_CAPTURE_PREFILTER_RE` del resto
# del repo: descartar por texto antes de pagar un parseo.
_QUICK_SEED_RE = re.compile(
    r"\b(?:INSERT\s+(?:LOW_PRIORITY\s+|DELAYED\s+|HIGH_PRIORITY\s+|IGNORE\s+)*INTO"
    r"|REPLACE\s+(?:LOW_PRIORITY\s+|DELAYED\s+)?INTO"
    r"|UPDATE\s+\w"
    r"|DELETE\s+FROM"
    r"|LOAD\s+DATA"
    r"|MERGE\s+INTO"
    r"|^\s*COPY\s)",
    re.IGNORECASE | re.MULTILINE,
)
_QUICK_DESTRUCTIVE_RE = re.compile(
    r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|COLUMN|INDEX|VIEW)|TRUNCATE\s+(?:TABLE\s+)?\w)",
    re.IGNORECASE,
)


@lru_cache(maxsize=512)
def quick_facts(sql: str) -> QuickFacts:
    """
    Hechos para las insignias de un LISTADO, solo con regex sobre el SQL enmascarado.

    ``analyze`` parsea cada sentencia con sqlglot (dos veces: clasificador y extracción de
    tablas). Eso es correcto para el validador, donde el usuario espera por una respuesta y
    la precisión es el producto, pero pagarlo por cada fila de cada página de versiones —con
    ``up_sql`` de hasta 256 KB— sería absurdo para decidir si se dibuja una plantita.

    La contrapartida es precisión: aquí no se distingue un `DELETE` con `WHERE` de uno sin
    él, ni se detectan construcciones raras. Para una insignia informativa alcanza; el
    veredicto fino lo da ``analyze`` cuando el usuario pulsa «Validar».

    La máscara sigue siendo obligatoria: sin ella, un `COMMENT 'ver INSERT INTO …'` pintaría
    una siembra que no existe.
    """
    masked = mask_quoted_spans(sql)
    return QuickFacts(
        has_seed=bool(_QUICK_SEED_RE.search(masked)),
        forced_collations=tuple(_extract(_COLLATE_RE, masked, sql)),
        destructive=bool(_QUICK_DESTRUCTIVE_RE.search(masked)),
    )
