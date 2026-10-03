"""
Validador de SQL de AGENTES — módulo **PURO** (sin motor, sin I/O, sin BD).

Decide si un texto SQL que escribió un agente es una consulta de lectura aceptable y, si no, POR QUÉ
no. Lo usan ``draft_query`` hoy y las tools que ejecuten SQL de agentes mañana: UNA sola política
para todas. No reemplaza a ``query_policy`` (la de la consola): la corre como segunda opinión.

TRES CAPAS, Y ESTA NO ES LA MÁS FUERTE
--------------------------------------
1. El LÍMITE real es el motor: cuenta con ``SELECT`` solo sobre UNA base, transacción
   ``READ ONLY`` y timeout del lado del servidor.
2. Este validador, que reduce lo que llega al motor. Es defensa en profundidad, no la barrera.
3. Topes, sobre y auditoría de quien lo llama.

Lo que el parseo NO puede detectar (y por eso la capa 1 existe): vistas con ``DEFINER``, tablas
``FEDERATED``/``CONNECT``/``SPIDER`` y extensiones tipo ``dblink`` que leen otras bases, y las
diferencias entre cómo sqlglot y el motor leen el mismo texto.

POR QUÉ UNA ALLOWLIST DEL AST Y NO UNA BLOCKLIST
------------------------------------------------
La consola clasifica por peligro (``read < write < ddl < blocked``) y por eso tiene una blocklist de
texto. Para un agente la pregunta es otra —"¿es EXACTAMENTE una lectura que entiendo?"— y se
responde con una lista blanca de tipos de nodo, de argumentos de cada nodo y de funciones sobre el
árbol COMPLETO. Una versión nueva de sqlglot agrega nodos que nadie revisó; con una blocklist esos
nodos entran por default, con una allowlist fallan por default. Mismo criterio que
``tests/test_mcp_import_guard.py``. Por eso también hay un guard de versión (``sqlglot>=30.11,
<30.12``) y un test de censo que fija los nodos, los argumentos y las funciones contra la versión
instalada.

QUÉ SE EJECUTA NO ES LO QUE ESCRIBIÓ EL AGENTE
----------------------------------------------
Se ejecuta el RENDER CANÓNICO del árbol validado (``canonical_sql``), nunca el texto crudo, y ese
render vuelve a pasar por el pipeline COMPLETO (incluidos el pre-gate y el doble léxico) y tiene que
dar el MISMO multiconjunto de tipos de nodo. Si el render difiere del árbol, el texto era
ambiguo para algún lector y se rechaza. Es lo que cierra buena parte de los diferenciales
parser/motor sin intentar imitar al motor.

Decisiones que parecen detalles y son agujeros:

- **``/*!`` y ``/*M!`` se rechazan sobre el texto CRUDO, incluso dentro de un literal.** El
  tokenizador de MySQL de sqlglot entrega ``/*!50000 ,2 */`` como un comentario común, así que el
  árbol no ve lo que el motor EJECUTA. Es el único chequeo sobre texto crudo y solo puede
  RECHAZAR; nada se acepta por una palabra clave. Costo aceptado: ``'/*! x'`` como literal se
  rechaza.
- **Comentarios, ``;`` y fronteras de literal salen del escáner compartido** (``sql_lexing``), con
  un segundo lector independiente: el tokenizador de sqlglot. Cualquiera de los dos que vea un
  comentario rechaza.
- **Doble léxico (D7).** El ``sql_mode`` del servidor es desconocido y ``NO_BACKSLASH_ESCAPES``
  cambia dónde termina un literal: ``'a\\'' -- x`` es UN literal más un comentario en MySQL y un
  error en PostgreSQL. Se escanea con y sin escape por barra invertida y, si discrepan, se
  rechaza. Además, en MySQL/MariaDB un literal que CONTENGA ``\\`` (o un salto de línea o tabulador,
  que el generador de sqlglot escribe como ``\\n``/``\\t``) se rechaza: el mismo SQL renderizado
  significaría cosas distintas bajo los dos modos. Falso positivo deliberado
  (``LIKE '%\\_%'``, ``'x\\\\y'``); ver ``docs/development/decisiones-e-incidentes.md``.
- **Un fallo de parseo RECHAZA, sin respaldo por texto.** El ``_READ_FALLBACK_RE`` de la consola es
  una concesión para ``SHOW GRANTS`` que acá no existe.
- **Se parsea SOLO con el dialecto del destino.** Un consenso entre dialectos aceptaría texto que el
  motor real lee distinto.
- **``LIMIT ALL`` de PostgreSQL no se detecta** (el parser lo descarta sin dejar rastro en el
  árbol) y se acepta: la consulta queda acotada igual porque el gateway empuja ``LIMIT n+1``.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.services import mcp_catalog as codes
from app.services.db_admin import query_policy
from app.services.db_admin.identifiers import references_gateway_internal_table
from app.services.db_admin.sql_lexing import (
    BLOCK_COMMENT,
    COMMENT_KINDS,
    DOLLAR,
    EXEC_COMMENT,
    QUOTED_IDENT,
    STRING,
    lex_spans,
)

# --------------------------------------------------------------------------- #
# Guard de versión de sqlglot (D14)                                            #
# --------------------------------------------------------------------------- #

#: Rango soportado: ``[MIN, BELOW)``. Las listas blancas de abajo se confirmaron contra 30.11.
SQLGLOT_MIN = (30, 11)
SQLGLOT_BELOW = (30, 12)


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        m = re.match(r"\d+", chunk)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def assert_supported_sqlglot(version: str) -> None:
    """
    Falla si ``version`` está fuera de ``[30.11, 30.12)``.

    Se afirma AL IMPORTAR: el módulo se importa de forma perezosa (solo cuando una tool recibe SQL),
    así que un sqlglot distinto no tira abajo el arranque del gateway pero sí cierra la tool, que es
    lo correcto: las listas blancas de nodos, argumentos y funciones son una foto de una versión, y
    cambiarla sin revisar el censo (``tests/test_agent_sql_invariants.py``) vuelve a abrir la puerta
    a nodos que nadie vio.
    """
    parsed = _version_tuple(version)
    if not (SQLGLOT_MIN <= parsed < SQLGLOT_BELOW):
        raise RuntimeError(
            f"sqlglot {version} fuera del rango validado "
            f"[{'.'.join(map(str, SQLGLOT_MIN))}, {'.'.join(map(str, SQLGLOT_BELOW))}): revisá el "
            "censo de nodos/argumentos/funciones de agent_sql_policy antes de subir la versión."
        )


assert_supported_sqlglot(getattr(sqlglot, "__version__", "0"))

# --------------------------------------------------------------------------- #
# Configuración por defecto                                                    #
# --------------------------------------------------------------------------- #
#
# Valores del borrador. Cuando exista la ejecución (``MCP_QUERY_*`` en ``environments.py``,
# acotados al cargar) las tools los pasan por parámetro; el validador no lee el entorno.

DEFAULT_MAX_ROWS = 100
DEFAULT_MAX_OFFSET = 10_000
DEFAULT_MAX_SQL_BYTES = 16_384

#: Profundidad máxima del árbol. Un anidamiento mayor no es una consulta humana y lleva al parser a
#: la recursión; se rechaza antes de seguir caminándolo.
MAX_DEPTH = 64

# --------------------------------------------------------------------------- #
# Códigos internos                                                             #
# --------------------------------------------------------------------------- #
#
# NUNCA salen de este módulo: ``codes.public_reason`` los traduce a la vez que arma el veredicto.

UNPARSEABLE = "agent_sql.unparseable"
TOKENIZER_ERROR = "agent_sql.tokenizer_error"
AMBIGUOUS_LITERAL = "agent_sql.ambiguous_literal"
BACKSLASH_IN_LITERAL = "agent_sql.backslash_in_literal"
TOO_LARGE = "agent_sql.too_large"
MULTIPLE_STATEMENTS = "agent_sql.multiple_statements"
NOT_SELECT = "agent_sql.not_select"
CLASSIFY_NOT_READ = "agent_sql.classify_not_read"
DML_IN_CTE = "agent_sql.dml_in_cte"
DML_IN_SUBQUERY = "agent_sql.dml_in_subquery"
SELECT_INTO = "agent_sql.select_into"
LOCKING_READ = "agent_sql.locking_read"
FUNCTION_NOT_ALLOWED = "agent_sql.function_not_allowed"
VARIABLE_ASSIGNMENT = "agent_sql.variable_assignment"
EXECUTABLE_COMMENT = "agent_sql.executable_comment"
COMMENT = "agent_sql.comment"
SYSTEM_SCHEMA = "agent_sql.system_schema"
GATEWAY_INTERNAL_TABLE = "agent_sql.gateway_internal_table"
CROSS_DATABASE = "agent_sql.cross_database"
UNSUPPORTED_CONSTRUCT = "agent_sql.unsupported_construct"
RENDER_MISMATCH = "agent_sql.render_mismatch"
TOO_COMPLEX = "agent_sql.too_complex"
LIMIT_NOT_BOUNDABLE = "agent_sql.limit_not_boundable"
OFFSET_TOO_HIGH = "agent_sql.offset_too_high"

INTERNAL_CODES = frozenset(
    {
        UNPARSEABLE,
        TOKENIZER_ERROR,
        AMBIGUOUS_LITERAL,
        BACKSLASH_IN_LITERAL,
        TOO_LARGE,
        MULTIPLE_STATEMENTS,
        NOT_SELECT,
        CLASSIFY_NOT_READ,
        DML_IN_CTE,
        DML_IN_SUBQUERY,
        SELECT_INTO,
        LOCKING_READ,
        FUNCTION_NOT_ALLOWED,
        VARIABLE_ASSIGNMENT,
        EXECUTABLE_COMMENT,
        COMMENT,
        SYSTEM_SCHEMA,
        GATEWAY_INTERNAL_TABLE,
        CROSS_DATABASE,
        UNSUPPORTED_CONSTRUCT,
        RENDER_MISMATCH,
        TOO_COMPLEX,
        LIMIT_NOT_BOUNDABLE,
        OFFSET_TOO_HIGH,
    }
)

# Clasificación del borrador (cerrada).
READ = "read"
WRITE = "write"
DDL = "ddl"
BLOCKED = "blocked"
INVALID = "invalid"
CLASSIFICATIONS = frozenset({READ, WRITE, DDL, BLOCKED, INVALID})

# --------------------------------------------------------------------------- #
# Listas blancas (confirmadas contra sqlglot 30.11; fijadas por el test de censo) #
# --------------------------------------------------------------------------- #

#: Nodos que NO son función. Tipo EXACTO (``type(node) in …``): una subclase nueva no hereda el permiso.
#: Incluye ``Fetch``/``LimitOptions`` solo para que ``FETCH FIRST`` se reporte como
#: ``LIMIT_NOT_BOUNDABLE`` y no como un nodo desconocido. ``And``/``Or``/``Xor``/``Collate``/
#: ``Exists``/``RegexpLike``/``RegexpILike`` son subclases de ``Func`` en sqlglot pero son operadores.
ALLOWED_NODES: frozenset[type] = frozenset(
    {
        # Estructura de la consulta
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.Subquery,
        exp.With,
        exp.CTE,
        exp.From,
        exp.Join,
        exp.Where,
        exp.Group,
        exp.GroupingSets,
        exp.Rollup,
        exp.Cube,
        exp.Having,
        exp.Order,
        exp.Ordered,
        exp.Limit,
        exp.Offset,
        exp.Fetch,
        exp.LimitOptions,
        exp.Distinct,
        exp.Alias,
        exp.TableAlias,
        exp.Table,
        exp.Column,
        exp.Dot,
        exp.Identifier,
        exp.Star,
        exp.Window,
        exp.WindowSpec,
        exp.Filter,
        exp.WithinGroup,
        # Valores
        exp.Literal,
        exp.Null,
        exp.Boolean,
        exp.Paren,
        exp.Tuple,
        exp.Var,
        exp.DataType,
        exp.DataTypeParam,
        exp.Interval,
        exp.Bracket,
        exp.JSONPath,
        exp.JSONPathKey,
        exp.JSONPathRoot,
        exp.JSONPathSubscript,
        # Operadores
        exp.And,
        exp.Or,
        exp.Xor,
        exp.Not,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.NullSafeEQ,
        exp.NullSafeNEQ,
        exp.Is,
        exp.Like,
        exp.ILike,
        exp.RegexpLike,
        exp.RegexpILike,
        exp.SimilarTo,
        exp.Escape,
        exp.Between,
        exp.In,
        exp.Exists,
        exp.Any,
        exp.All,
        exp.Collate,
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.IntDiv,
        exp.Mod,
        exp.Neg,
        exp.DPipe,
        exp.BitwiseAnd,
        exp.BitwiseOr,
        exp.BitwiseXor,
        exp.BitwiseNot,
        exp.BitwiseLeftShift,
        exp.BitwiseRightShift,
    }
)

#: Funciones TIPADAS permitidas (clase exacta). ``exp.Anonymous`` va aparte (``ANON_ALLOWED``).
#: ``Stuff`` es el ``INSERT(str, pos, len, new)`` de MySQL (sqlglot lo parsea así, no como
#: ``Anonymous``). Se omiten a propósito las que fabrican resultados enormes con un argumento
#: (``REPEAT``, ``SPACE``, ``LPAD``/``RPAD``) y las generadoras de filas (``GENERATE_SERIES``,
#: ``UNNEST``): el timeout acota el tiempo, no la memoria del servidor. ``OVERLAY`` tampoco: su
#: argumento ``for_`` choca con la clave prohibida que protege a ``Select`` y no vale una excepción.
ALLOWED_FUNCTIONS: frozenset[type] = frozenset(
    {
        # Agregados
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.GroupConcat,
        exp.Stddev,
        exp.StddevPop,
        exp.StddevSamp,
        exp.Variance,
        exp.VariancePop,
        exp.AnyValue,
        exp.ArrayAgg,
        exp.LogicalAnd,
        exp.LogicalOr,
        exp.PercentileCont,
        exp.PercentileDisc,
        # Ventana
        exp.RowNumber,
        exp.Rank,
        exp.DenseRank,
        exp.PercentRank,
        exp.CumeDist,
        exp.Ntile,
        exp.Lag,
        exp.Lead,
        exp.FirstValue,
        exp.LastValue,
        exp.NthValue,
        # Texto
        exp.Concat,
        exp.ConcatWs,
        exp.Length,
        exp.Lower,
        exp.Upper,
        exp.Trim,
        exp.Substring,
        exp.Left,
        exp.Right,
        exp.Replace,
        exp.RegexpReplace,
        exp.Stuff,
        exp.Reverse,
        exp.StrPosition,
        exp.Initcap,
        exp.SplitPart,
        exp.Chr,
        exp.Ascii,
        exp.MD5,
        exp.SHA,
        exp.SHA2,
        exp.Hex,
        exp.Unhex,
        exp.ToBase64,
        exp.FromBase64,
        exp.Soundex,
        exp.NumberToStr,
        # Numéricas
        exp.Abs,
        exp.Ceil,
        exp.Floor,
        exp.Round,
        exp.Trunc,
        exp.Sqrt,
        exp.Exp,
        exp.Ln,
        exp.Log,
        exp.Pow,
        exp.Sign,
        exp.Greatest,
        exp.Least,
        exp.Pi,
        exp.Sin,
        exp.Cos,
        exp.Tan,
        exp.Cot,
        exp.Atan2,
        exp.Rand,
        exp.Radians,
        exp.Degrees,
        # Fecha y hora
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentTimestamp,
        exp.Year,
        exp.Month,
        exp.Day,
        exp.Hour,
        exp.Minute,
        exp.Second,
        exp.Week,
        exp.Quarter,
        exp.DayOfWeek,
        exp.DayOfMonth,
        exp.DayOfYear,
        exp.WeekOfYear,
        exp.Dayname,
        exp.DateAdd,
        exp.DateSub,
        exp.DateDiff,
        exp.TimestampDiff,
        exp.TimestampTrunc,
        exp.DateTrunc,
        exp.TsOrDsToDate,
        exp.TsOrDsToTimestamp,
        exp.StrToDate,
        exp.TimeToStr,
        exp.LastDay,
        exp.UnixToTime,
        exp.Extract,
        # Condicionales y conversión
        exp.Coalesce,
        exp.Nullif,
        exp.If,
        exp.Case,
        exp.Cast,
        # JSON y varios
        exp.JSONExtract,
        exp.JSONExtractScalar,
        exp.JSONBExtract,
        exp.JSONBExtractScalar,
        exp.Array,
        exp.Uuid,
    }
)

#: Funciones que sqlglot NO tipifica (llegan como ``exp.Anonymous``) y se permiten, por dialecto
#: sqlglot, en minúsculas. ``NOW()`` llega como ``Anonymous`` en los dos dialectos. Las que sqlglot
#: sí tipifica (``DATE_FORMAT`` -> ``TimeToStr``, ``IFNULL`` -> ``Coalesce``) NO van acá.
ANON_ALLOWED: dict[str, frozenset[str]] = {
    "mysql": frozenset(
        {
            "now",
            "crc32",
            "ord",
            "field",
            "find_in_set",
            "strcmp",
            "octet_length",
            "json_unquote",
            "json_length",
            "json_contains",
            "json_valid",
            "timestampadd",
            "unix_timestamp",
            "from_days",
            "adddate",
            "subdate",
            "time_format",
            "regexp_substr",
            "std",
        }
    ),
    "postgres": frozenset(
        {
            "now",
            "age",
            "jsonb_array_length",
            "jsonb_build_object",
            "json_build_object",
            "btrim",
            "every",
            "jsonb_agg",
        }
    ),
}

#: Funciones con efecto (locks, dormir, secuencias, config, archivos, red, terminar sesiones) o que
#: revelan el servidor. Se afirma al importar que NINGUNA está permitida: una entrada nueva en una
#: allowlist que choque con esta lista rompe el import en vez de abrir un efecto lateral.
SIDE_EFFECT_FUNCTIONS = frozenset(
    {
        "sleep",
        "benchmark",
        "get_lock",
        "release_lock",
        "release_all_locks",
        "is_free_lock",
        "is_used_lock",
        "load_file",
        "master_pos_wait",
        "nextval",
        "setval",
        "currval",
        "lastval",
        "set_config",
        "current_setting",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "txid_current",
        "pg_current_xact_id",
        "pg_notify",
        "query_to_xml",
        "version",
    }
)
#: Familias de funciones con efecto, por prefijo.
SIDE_EFFECT_PREFIXES = ("pg_sleep", "pg_advisory_", "lo_", "dblink")


def is_side_effect_function(name: str) -> bool:
    lowered = name.lower()
    return lowered in SIDE_EFFECT_FUNCTIONS or lowered.startswith(SIDE_EFFECT_PREFIXES)


# Nodos con efecto o que alteran la clase de la sentencia: cada uno tiene su código.
_DML_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
_VARIABLE_NODES = (exp.PropertyEQ, exp.Parameter, exp.Placeholder, exp.SessionParameter)

#: Raíces aceptadas (clase exacta; ``Subquery`` se desenvuelve antes).
_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

_NEVER_ARG_KEYS = frozenset(
    {
        "into",
        "locks",
        "for_",
        "hint",
        "hints",
        "sample",
        "settings",
        "options",
        "operation_modifiers",
        "connect",
        "prewhere",
        "cluster",
        "distribute",
        "sort",
        "match",
        "pivots",
        "laterals",
        "qualify",
    }
)

#: Argumentos permitidos de los nodos con cláusulas (los que cargan ``into``/``locks``/``hint``/
#: ``sample``/``operation_modifiers``…). Claves de sqlglot 30.11 (``from_``, ``with_``, ``for_``:
#: renombradas respecto de versiones anteriores). ``Select`` NO permite ``for_`` ni ``locks`` ni
#: ``operation_modifiers`` (``SQL_CALC_FOUND_ROWS``, ``HIGH_PRIORITY``) ni ``hint`` ni ``sample``.
#: ``Limit``/``Offset`` permiten todos sus argumentos: la cota la juzga la etapa de filas, que da un
#: código más preciso (``LIMIT_NOT_BOUNDABLE``) que un nodo no soportado.
STRUCTURAL_ARGS: dict[type, frozenset[str]] = {
    exp.Select: frozenset(
        {
            "expressions",
            "from_",
            "joins",
            "where",
            "group",
            "having",
            "order",
            "limit",
            "offset",
            "distinct",
            "with_",
            "windows",
        }
    ),
    exp.Union: frozenset({"this", "expression", "distinct", "order", "limit", "offset", "with_"}),
    exp.Intersect: frozenset(
        {"this", "expression", "distinct", "order", "limit", "offset", "with_"}
    ),
    exp.Except: frozenset({"this", "expression", "distinct", "order", "limit", "offset", "with_"}),
    exp.Subquery: frozenset({"this", "alias", "order", "limit", "offset"}),
    exp.Table: frozenset({"this", "alias", "db", "catalog"}),
    exp.Column: frozenset({"this", "table", "db", "catalog"}),
    exp.Join: frozenset({"this", "on", "using", "kind", "side", "method"}),
    exp.With: frozenset({"expressions", "recursive"}),
    exp.CTE: frozenset({"this", "alias", "materialized"}),
    exp.Window: frozenset({"this", "partition_by", "order", "spec", "alias", "over"}),
    exp.WindowSpec: frozenset({"kind", "start", "start_side", "end", "end_side"}),
    exp.Group: frozenset({"expressions", "grouping_sets", "cube", "rollup"}),
    exp.Distinct: frozenset({"expressions", "on"}),
    # ``Order.this`` lo usa ``GROUP_CONCAT(a ORDER BY b)`` para llevar la expresión agrupada.
    exp.Order: frozenset({"this", "expressions"}),
    exp.Ordered: frozenset({"this", "desc", "nulls_first"}),
    exp.Star: frozenset(),
    exp.Alias: frozenset({"this", "alias"}),
    exp.TableAlias: frozenset({"this", "columns"}),
    exp.Limit: frozenset(exp.Limit.arg_types),
    exp.Offset: frozenset(exp.Offset.arg_types),
    exp.Fetch: frozenset(exp.Fetch.arg_types),
}


def _build_allowed_args() -> dict[type, frozenset[str]]:
    allowed: dict[type, frozenset[str]] = {}
    for cls in ALLOWED_NODES | ALLOWED_FUNCTIONS:
        allowed[cls] = STRUCTURAL_ARGS.get(cls, frozenset(cls.arg_types))
    return allowed


#: Argumentos permitidos por clase. Los nodos de ``STRUCTURAL_ARGS`` tienen lista explícita; el
#: resto (operadores y funciones: sus argumentos son sub-expresiones que el recorrido total ya
#: valida) toma las claves de su ``arg_types`` instalada, fijadas por el test de censo.
ALLOWED_ARGS: dict[type, frozenset[str]] = _build_allowed_args()


def _assert_allowlist_invariants() -> None:
    """
    Invariantes de las listas blancas, afirmados al importar (fallar al importar es fallar cerrado).
    """
    overlap = ALLOWED_NODES & ALLOWED_FUNCTIONS
    assert not overlap, f"clases en las dos listas: {sorted(c.__name__ for c in overlap)}"
    # Una clave renombrada por sqlglot no puede dejar una lista con claves inexistentes: sin esto
    # el validador rechazaría TODA consulta con FROM/WITH en silencio.
    for cls, keys in STRUCTURAL_ARGS.items():
        missing = keys - set(cls.arg_types)
        assert not missing, f"{cls.__name__}: argumentos inexistentes en sqlglot: {sorted(missing)}"
    # Ningún nodo "genérico" expone un argumento con efecto.
    for cls, keys in ALLOWED_ARGS.items():
        if cls in STRUCTURAL_ARGS:
            continue
        banned = keys & _NEVER_ARG_KEYS
        assert not banned, f"{cls.__name__} expone argumentos prohibidos: {sorted(banned)}"
    # La lista de efectos no se cruza con lo permitido.
    allowed_names: set[str] = set()
    for cls in ALLOWED_NODES | ALLOWED_FUNCTIONS:
        if issubclass(cls, exp.Func):
            allowed_names.update(n.lower() for n in cls.sql_names())
    for names in ANON_ALLOWED.values():
        allowed_names.update(names)
    clashing = {n for n in allowed_names if is_side_effect_function(n)}
    assert not clashing, f"funciones con efecto en una allowlist: {sorted(clashing)}"


_assert_allowlist_invariants()

# --------------------------------------------------------------------------- #
# Esquemas de sistema                                                          #
# --------------------------------------------------------------------------- #

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres"}
_SYSTEM_SCHEMAS = {
    "mysql": frozenset({"information_schema", "performance_schema", "mysql", "sys"}),
    "postgres": frozenset({"pg_catalog", "information_schema"}),
}
_PG_SYSTEM_SCHEMA_PREFIXES = ("pg_toast", "pg_temp")

_EXEC_COMMENT_RE = re.compile(r"/\*m?!", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Lo que el generador MySQL de sqlglot escribe como secuencia con barra invertida.
_MYSQL_ESCAPED_CHARS = ("\\", "\n", "\r", "\t")


# --------------------------------------------------------------------------- #
# Veredicto                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgentSqlVerdict:
    """
    Resultado del validador. ``reasons``/``warnings`` son códigos PÚBLICOS (``mcp_catalog``).

    Solo un veredicto ``accepted`` (``read`` sin razones) trae ``canonical_sql``/``executed_sql``/
    ``human_query``: es lo único que una tool puede ejecutar. ``human_query`` es el render
    canónico SIN el tope del gateway (con el ``LIMIT`` del agente si lo trajo) y nunca se ejecuta.
    """

    classification: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    canonical_sql: str | None = None
    executed_sql: str | None = None
    human_query: str | None = None
    masked_sql: str | None = None
    sql_hash: str | None = None
    row_bound: query_policy.RowBound | None = None

    @property
    def accepted(self) -> bool:
        return self.classification == READ and not self.reasons


@dataclass
class _Analysis:
    reasons: list[str] = field(default_factory=list)
    #: ``True`` cuando no hay árbol confiable (vacío, enorme, ilegible, varias sentencias): la
    #: clasificación es ``invalid`` y no se mira el peligro.
    invalid: bool = False
    root: exp.Expression | None = None
    danger: str | None = None
    bound: query_policy.RowBound | None = None

    def add(self, code: str) -> None:
        if code not in self.reasons:
            self.reasons.append(code)

    def fail(self, code: str) -> None:
        self.add(code)
        self.invalid = True


# --------------------------------------------------------------------------- #
# Etapas                                                                       #
# --------------------------------------------------------------------------- #


def _populated(value) -> bool:
    if value is None or value is False:
        return False
    return not (isinstance(value, (list, tuple)) and not value)


def _populated_keys(node: exp.Expression) -> set[str]:
    return {k for k, v in node.args.items() if _populated(v)}


def _prepare(sql, max_bytes: int, a: _Analysis) -> str | None:
    """Etapa 0: tamaño, caracteres de control, vacío y UN ``;`` final."""
    if not isinstance(sql, str):
        a.fail(UNPARSEABLE)
        return None
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeEncodeError:
        a.fail(UNPARSEABLE)
        return None
    if size > max_bytes:
        a.fail(TOO_LARGE)
        return None
    if _CONTROL_RE.search(sql):
        a.fail(UNPARSEABLE)
        return None
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        a.fail(UNPARSEABLE)
        return None
    return text


def _lex_gate(text: str, engine: str, dialect: str, a: _Analysis):
    """
    Etapas 1-2: pre-gate de comentarios ejecutables, doble léxico, comentarios, ``;`` y barra
    invertida. Devuelve los tokens de sqlglot (o ``None`` si falló).
    """
    # 1. Pre-gate SOBRE EL TEXTO CRUDO: el tokenizador de MySQL entrega ``/*!…*/`` como comentario
    #    común y el árbol nunca lo ve. Solo rechaza; incluso dentro de un literal.
    if _EXEC_COMMENT_RE.search(text):
        a.add(EXECUTABLE_COMMENT)

    family_mysql = engine in _MYSQL_FAMILY

    def spans(backslash_escapes: bool):
        return lex_spans(
            text,
            engine=engine,
            backslash_escapes=backslash_escapes,
            double_quote_is_string=family_mysql,
            dollar_quotes=not family_mysql,
        )

    plain, escaped = spans(False), spans(True)
    if plain != escaped:
        # El ``sql_mode`` del servidor es desconocido: si los dos lectores no coinciden en dónde
        # termina un literal, hay texto que uno ve como código y el otro como cadena.
        a.fail(AMBIGUOUS_LITERAL)
        return None

    pos = 0
    for span in plain:
        if ";" in text[pos : span.start]:
            a.fail(MULTIPLE_STATEMENTS)
        pos = span.end
        if span.kind in COMMENT_KINDS:
            a.add(EXECUTABLE_COMMENT if span.kind == EXEC_COMMENT else COMMENT)
            if span.kind in (BLOCK_COMMENT, EXEC_COMMENT) and not span.terminated:
                a.fail(UNPARSEABLE)
        elif span.kind in (STRING, QUOTED_IDENT, DOLLAR) and not span.terminated:
            a.fail(UNPARSEABLE)
        if family_mysql and span.kind == STRING and "\\" in text[span.start : span.end]:
            a.fail(BACKSLASH_IN_LITERAL)
    if ";" in text[pos:]:
        a.fail(MULTIPLE_STATEMENTS)
    if a.invalid:
        return None

    # Segundo lector, independiente del escáner: el tokenizador de sqlglot.
    try:
        tokens = sqlglot.tokenize(text, read=dialect)
    except Exception:  # noqa: BLE001 — TokenError y cualquier otro fallo léxico rechazan igual
        a.fail(TOKENIZER_ERROR)
        return None
    if any(token.comments for token in tokens):
        a.add(COMMENT)
    return tokens


def _refine_parse_failure(tokens, a: _Analysis) -> None:
    """
    Agrega a un fallo de parseo la razón MÁS ESPECÍFICA que se lee de los TOKENS (nunca del texto).

    Es solo etiqueta: el veredicto ya es rechazo y no cambia. Existe porque sqlglot no parsea
    ``SELECT … INTO OUTFILE`` ni un DML dentro de un subselect, y el agente entiende
    ``SELECT_INTO``/``DML_IN_SUBQUERY`` mejor que ``PARSE_FAILED``.
    """
    texts = [t.text.upper() for t in tokens]
    for i, word in enumerate(texts):
        if word == "INTO" and i + 1 < len(texts) and texts[i + 1] in ("OUTFILE", "DUMPFILE"):
            a.add(SELECT_INTO)
        if word in ("DELETE", "UPDATE", "INSERT", "MERGE") and i > 0 and texts[i - 1] == "(":
            a.add(DML_IN_SUBQUERY)


def _depth(root: exp.Expression) -> int:
    """Profundidad máxima del árbol, iterativa (no se puede confiar en la recursión acá)."""
    deepest = 0
    stack: list[tuple[exp.Expression, int]] = [(root, 1)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        if deepest > MAX_DEPTH:
            return deepest
        for child in node.iter_expressions():
            stack.append((child, level + 1))
    return deepest


def _qualifier_reasons(node: exp.Table | exp.Column, engine: str, dialect: str, database: str):
    """Etapa 7 para un nombre calificado."""
    found: list[str] = []
    if node.catalog:
        found.append(CROSS_DATABASE)
    schema = node.db
    if schema:
        lowered = schema.lower()
        if lowered in _SYSTEM_SCHEMAS[dialect] or (
            dialect == "postgres" and lowered.startswith(_PG_SYSTEM_SCHEMA_PREFIXES)
        ):
            found.append(SYSTEM_SCHEMA)
        elif engine in _MYSQL_FAMILY and schema != database:
            # En MySQL/MariaDB la "base" es el ``db`` del nombre. En PostgreSQL ``esquema.tabla``
            # es un esquema de la MISMA base y solo el nombre de tres partes (``catalog``) es otra.
            found.append(CROSS_DATABASE)
    elif (
        isinstance(node, exp.Table)
        and dialect == "postgres"
        and node.name.lower().startswith("pg_")
    ):
        # ``pg_class``, ``pg_user``… sin calificar: el ``search_path`` los resuelve a pg_catalog.
        found.append(SYSTEM_SCHEMA)
    return found


def inspect_tree(root: exp.Expression, *, engine: str, database: str) -> list[str]:
    """
    Etapas 5-7 sobre el árbol COMPLETO: devuelve los códigos internos (sin repetir, en orden).

    Es pública para que el test de censo pueda probar un nodo que ninguna sintaxis produce (S6) sin
    pasar por el parser. No mira la raíz ni la cota de filas: eso es de ``_analyze_text``.
    """
    dialect = _DIALECT[engine]
    family_mysql = engine in _MYSQL_FAMILY
    anon_allowed = ANON_ALLOWED[dialect]
    out: list[str] = []

    def add(code: str) -> None:
        if code not in out:
            out.append(code)

    for node in root.walk():
        cls = type(node)

        if isinstance(node, _DML_NODES):
            add(DML_IN_CTE if node.find_ancestor(exp.CTE) is not None else DML_IN_SUBQUERY)
            continue
        if isinstance(node, exp.Into):
            add(SELECT_INTO)
            continue
        if isinstance(node, exp.Lock):
            add(LOCKING_READ)
            continue
        if isinstance(node, _VARIABLE_NODES):
            add(VARIABLE_ASSIGNMENT)
            continue

        if cls is exp.Anonymous:
            name = node.name.lower()
            if name not in anon_allowed or is_side_effect_function(name):
                add(FUNCTION_NOT_ALLOWED)
                continue
            allowed_keys = frozenset({"this", "expressions"})
        elif cls in ALLOWED_NODES or cls in ALLOWED_FUNCTIONS:
            allowed_keys = ALLOWED_ARGS[cls]
        elif isinstance(node, exp.Func):
            add(FUNCTION_NOT_ALLOWED)
            continue
        else:
            add(UNSUPPORTED_CONSTRUCT)
            continue

        # Llamada calificada (``db.f(x)``, ``pg_catalog.f(x)``): sqlglot la arma como ``Dot``
        # con la función a la derecha. Ninguna se permite: nombra otro esquema u otra base.
        parent = node.parent
        if isinstance(node, exp.Func) and isinstance(parent, exp.Dot) and parent.expression is node:
            add(FUNCTION_NOT_ALLOWED)

        if not _populated_keys(node) <= allowed_keys:
            add(UNSUPPORTED_CONSTRUCT)

        if isinstance(node, (exp.Table, exp.Column)):
            for code in _qualifier_reasons(node, engine, dialect, database):
                add(code)

        if family_mysql and isinstance(node, exp.Literal) and node.is_string:
            if any(ch in str(node.this) for ch in _MYSQL_ESCAPED_CHARS):
                add(BACKSLASH_IN_LITERAL)
    return out


def _check_row_bound(root: exp.Expression, max_offset: int, a: _Analysis) -> None:
    """Etapa 9: ``LIMIT``/``OFFSET`` literales y acotables."""
    limit = root.args.get("limit")
    if limit is not None:
        if type(limit) is not exp.Limit or _populated_keys(limit) - {"expression"}:
            a.add(LIMIT_NOT_BOUNDABLE)
        else:
            value = limit.expression
            if not (isinstance(value, exp.Literal) and value.is_int):
                a.add(LIMIT_NOT_BOUNDABLE)
    offset = root.args.get("offset")
    if offset is not None:
        if type(offset) is not exp.Offset or _populated_keys(offset) - {"expression"}:
            a.add(LIMIT_NOT_BOUNDABLE)
        else:
            value = offset.expression
            if not (isinstance(value, exp.Literal) and value.is_int):
                a.add(LIMIT_NOT_BOUNDABLE)
            else:
                try:
                    too_high = int(value.name) > max_offset
                except (TypeError, ValueError):
                    too_high = True
                if too_high:
                    a.add(OFFSET_TOO_HIGH)


def _analyze_text(
    sql,
    *,
    engine: str,
    database: str,
    max_rows: int,
    max_offset: int,
    max_bytes: int,
    check_bounds: bool,
) -> _Analysis:
    """Etapas 0-9 sobre un texto. No hace el round trip (lo hace ``_validate``)."""
    a = _Analysis()
    dialect = _DIALECT[engine]

    text = _prepare(sql, max_bytes, a)
    if text is None:
        return a

    tokens = _lex_gate(text, engine, dialect, a)
    if tokens is None:
        return a

    # 3. Parseo, SOLO con el dialecto del destino y sin respaldo por texto.
    try:
        parsed = sqlglot.parse(text, read=dialect)
    except RecursionError:
        a.fail(TOO_COMPLEX)
        return a
    except Exception:  # noqa: BLE001 — sqlglot lanza varias familias de error
        a.fail(UNPARSEABLE)
        _refine_parse_failure(tokens, a)
        return a
    trees = [t for t in parsed if t is not None]
    if not trees:
        a.fail(UNPARSEABLE)
        return a
    if len(trees) > 1:
        a.fail(MULTIPLE_STATEMENTS)
        return a

    # 4. Raíz: Select o set-op (un ``Subquery`` sin nada más se desenvuelve).
    root = trees[0]
    wrapped_ok = True
    while type(root) is exp.Subquery:
        if _populated_keys(root) - {"this"}:
            wrapped_ok = False
            break
        root = root.this
    if not wrapped_ok:
        a.add(UNSUPPORTED_CONSTRUCT)
    elif type(root) not in _ROOTS:
        a.add(NOT_SELECT)
    else:
        a.root = root
        if _depth(root) > MAX_DEPTH:
            a.add(TOO_COMPLEX)
        else:
            # 5-7. Recorrido total del árbol.
            for code in inspect_tree(root, engine=engine, database=database):
                a.add(code)
            if references_gateway_internal_table(text):
                a.add(GATEWAY_INTERNAL_TABLE)
            # 9. Cota de filas, sobre la raíz.
            if check_bounds:
                _check_row_bound(root, max_offset, a)
                if not any(
                    c in a.reasons for c in (LIMIT_NOT_BOUNDABLE, OFFSET_TOO_HIGH, TOO_COMPLEX)
                ):
                    a.bound = query_policy.bound_select(
                        root, dialect, max_rows, include_set_ops=True
                    )
                    if a.bound.kind == query_policy.UNBOUNDABLE:
                        a.add(LIMIT_NOT_BOUNDABLE)

    # 8. Segunda opinión: la política de la consola tiene que decir ``read``.
    if not wrapped_ok:
        # Un ``(SELECT …) LIMIT n`` es una lectura que ya rechazamos arriba con su código; la
        # consola lo marcaría ``ddl`` por "tipo de raíz no mapeado" y la etiqueta engañaría.
        a.danger = query_policy.READ
        return a
    probe = text
    if a.root is not None and type(trees[0]) not in (exp.Select, exp.Union):
        # La consola solo mapea como lectura las raíces ``Select``/``Union``: un
        # ``INTERSECT``/``EXCEPT`` o una consulta entre paréntesis salen ``ddl`` ("raíz no
        # mapeada"). Ampliar eso cambiaría la consola, que NO se toca; en su lugar se le pide la
        # opinión sobre la misma consulta envuelta en un ``SELECT`` (que SÍ mapea) y recorre el
        # árbol entero igual.
        try:
            probe = f"SELECT * FROM ({a.root.sql(dialect=dialect)}) AS _agent_probe"
        except Exception:  # noqa: BLE001 — sin render no hay segunda opinión: no se acepta
            probe = text
    try:
        a.danger = query_policy.classify(probe, engine=engine, database=database).danger
    except Exception:  # noqa: BLE001 — si la segunda opinión falla, no se acepta
        a.danger = query_policy.BLOCKED
    if a.danger != query_policy.READ and not a.reasons:
        a.add(CLASSIFY_NOT_READ)
    return a


def _node_multiset(root: exp.Expression) -> Counter:
    return Counter(type(node) for node in root.walk())


def _masked(root: exp.Expression, dialect: str) -> str | None:
    """El render canónico con cada literal reemplazado por ``?`` (para auditoría)."""
    try:
        masked = root.copy().transform(
            lambda n: exp.Var(this="?") if isinstance(n, exp.Literal) else n
        )
        return masked.sql(dialect=dialect)
    except Exception:  # noqa: BLE001 — sin texto enmascarado queda solo el hash
        return None


def _round_trip(
    a: _Analysis,
    root: exp.Expression,
    canonical: str,
    *,
    engine,
    database,
    max_rows,
    max_offset,
    max_bytes,
) -> None:
    """
    Etapa 10 (D4): el render canónico vuelve a pasar por el pipeline completo y tiene que producir
    el mismo multiconjunto de nodos. Lo mismo, más liviano, para el texto que se ejecuta.
    """
    again = _analyze_text(
        canonical,
        engine=engine,
        database=database,
        max_rows=max_rows,
        max_offset=max_offset,
        max_bytes=max_bytes,
        check_bounds=False,
    )
    if (
        again.invalid
        or again.reasons
        or again.root is None
        or again.danger != query_policy.READ
        or _node_multiset(again.root) != _node_multiset(root)
    ):
        a.add(RENDER_MISMATCH)
        return

    executed = a.bound.sql if a.bound is not None else None
    if executed is not None and executed != canonical:
        pushed = _analyze_text(
            executed,
            engine=engine,
            database=database,
            max_rows=max_rows,
            max_offset=max_offset,
            max_bytes=max_bytes,
            check_bounds=False,
        )
        if (
            pushed.invalid
            or pushed.reasons
            or pushed.root is None
            or pushed.danger != query_policy.READ
            or type(pushed.root) is not type(root)
        ):
            a.add(RENDER_MISMATCH)


def _failure_verdict(code: str, classification: str = BLOCKED) -> AgentSqlVerdict:
    return AgentSqlVerdict(classification=classification, reasons=(codes.public_reason(code),))


def _validate(
    sql, *, engine: str, database: str, max_rows: int, max_offset: int, max_bytes: int
) -> AgentSqlVerdict:
    if engine not in _DIALECT:
        return _failure_verdict(UNSUPPORTED_CONSTRUCT)
    dialect = _DIALECT[engine]
    a = _analyze_text(
        sql,
        engine=engine,
        database=database,
        max_rows=max_rows,
        max_offset=max_offset,
        max_bytes=max_bytes,
        check_bounds=True,
    )

    canonical: str | None = None
    if not a.invalid and a.root is not None and not a.reasons:
        try:
            canonical = a.root.sql(dialect=dialect)
        except Exception:  # noqa: BLE001 — un árbol que no se puede renderizar no se acepta
            a.add(RENDER_MISMATCH)
        if canonical is not None:
            _round_trip(
                a,
                a.root,
                canonical,
                engine=engine,
                database=database,
                max_rows=max_rows,
                max_offset=max_offset,
                max_bytes=max_bytes,
            )

    reasons = tuple(dict.fromkeys(codes.public_reason(code) for code in a.reasons))
    if a.invalid:
        classification = INVALID
    elif (a.danger or query_policy.READ) == query_policy.READ:
        classification = BLOCKED if reasons else READ
    else:
        classification = a.danger
    warnings: tuple[str, ...] = ()
    if classification == WRITE:
        warnings = (codes.WARN_WRITE_NOT_EXECUTED,)
    elif classification == DDL:
        warnings = (codes.WARN_DDL_NOT_EXECUTED,)

    if classification != READ or reasons or canonical is None or a.root is None:
        return AgentSqlVerdict(classification=classification, reasons=reasons, warnings=warnings)

    bound = a.bound
    return AgentSqlVerdict(
        classification=READ,
        reasons=(),
        warnings=(),
        canonical_sql=canonical,
        executed_sql=bound.sql if bound is not None else None,
        human_query=canonical,
        masked_sql=_masked(a.root, dialect),
        sql_hash=query_policy.sql_hash(canonical),
        row_bound=bound,
    )


def validate_agent_select(
    sql,
    *,
    engine: str,
    database: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_offset: int = DEFAULT_MAX_OFFSET,
    max_bytes: int = DEFAULT_MAX_SQL_BYTES,
) -> AgentSqlVerdict:
    """
    El veredicto para un texto SQL de agente. **Nunca levanta**: cualquier fallo inesperado es un
    rechazo (``blocked`` + ``UNSUPPORTED_NODE``), jamás una lectura aceptada.

    ``engine`` es el del inventario (``mysql``/``mariadb``/``postgresql``) y ``database`` la base
    FIJADA de la conexión, no un string del agente. El texto se evalúa entero y se juntan TODAS las
    razones (un borrador las muestra todas); quien ejecute puede quedarse con la primera.
    """
    try:
        return _validate(
            sql,
            engine=engine,
            database=database,
            max_rows=max_rows,
            max_offset=max_offset,
            max_bytes=max_bytes,
        )
    except Exception:  # noqa: BLE001 — fail-closed: lo imprevisto nunca se acepta
        from app.core.logger import get_logger

        # Sin el texto del agente en el log: puede traer datos de terceros.
        get_logger(__name__).exception("El validador de SQL de agente falló de forma inesperada")
        return _failure_verdict(TOO_COMPLEX)


# --------------------------------------------------------------------------- #
# Sobre de ``draft_query``                                                     #
# --------------------------------------------------------------------------- #


def clip_text(text: str, max_bytes: int) -> str:
    """
    ``text`` sin caracteres de control (salvo salto de línea y tabulador) y recortado a
    ``max_bytes`` bytes UTF-8. Es lo que se le devuelve al agente cuando el texto NO es una lectura
    aceptada: se le repite lo suyo, saneado y acotado, nunca más.
    """
    cleaned = _CONTROL_RE.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))
    return cleaned.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def build_draft_envelope(verdict: AgentSqlVerdict, agent_text, *, max_bytes: int) -> dict:
    """
    El sobre de ``draft_query``: ``{classification, reasons, warnings, query_text, touches_engine}``.

    ``query_text`` es el render canónico para una lectura aceptada y el texto del agente (saneado y
    recortado) para todo lo demás. ``touches_engine`` es SIEMPRE ``False``: redactar nunca
    ejecuta, ni siquiera una lectura.
    """
    if verdict.accepted and verdict.canonical_sql is not None:
        query_text = verdict.canonical_sql
    else:
        query_text = clip_text(agent_text if isinstance(agent_text, str) else "", max_bytes)
    return {
        "classification": verdict.classification,
        "reasons": list(verdict.reasons),
        "warnings": list(verdict.warnings),
        "query_text": query_text,
        "touches_engine": False,
    }
