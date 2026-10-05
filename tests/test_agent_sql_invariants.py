"""
Invariantes del validador de SQL de agentes (``agent_sql_policy``): el CENSO contra sqlglot 30.11.

POR QUÉ ESTE ARCHIVO ES LA PIEZA CLAVE DE LA SEGURIDAD DEL VALIDADOR
--------------------------------------------------------------------
El validador es una allowlist de nodos, argumentos y funciones del AST de sqlglot. Una allowlist es
una FOTO de una versión: sqlglot agrega nodos y renombra claves en cada release (``from`` pasó a
``from_``, ``with`` a ``with_``, ``for`` a ``for_``). Dos modos de fallo, y los dos son silenciosos si
nadie los mira:

- una clave renombrada deja en la lista un nombre que ya no existe, y el validador rechaza TODA
  consulta con ``FROM``/``WITH`` sin que nada falle;
- un argumento NUEVO en un nodo permitido (un ``INTO`` nuevo, un modificador de ``SELECT``) se
  acepta por default si la lista se arma con "todo menos lo prohibido".

Por eso estos tests (1) derivan la línea base de ``cls.arg_types`` y NO la hardcodean, (2) afirman
que cada lista permitida es subconjunto de lo que la clase instalada declara y (3) fijan una
FOTO completa (nombres y claves). Subir sqlglot rompe estos tests a propósito: hay que releer el
diff del censo y recién ahí regenerar la foto (``python -c`` al final de este archivo).

Los snapshots de abajo se generaron con sqlglot 30.11.0.
"""

import hashlib
import json
import pathlib
import tomllib

import pytest
import sqlglot
from sqlglot import exp

from app.services import mcp_catalog as codes
from app.services.db_admin import agent_sql_policy as policy
from app.services.db_admin import sql_lexing

DATABASE = "mydb"

_NODE_SNAPSHOT = """
    Add Alias All And Any Between BitwiseAnd BitwiseLeftShift BitwiseNot BitwiseOr
    BitwiseRightShift BitwiseXor Boolean Bracket CTE Collate Column Cube DPipe DataType
    DataTypeParam Distinct Div Dot EQ Escape Except Exists Fetch Filter From GT GTE Group
    GroupingSets Having ILike Identifier In IntDiv Intersect Interval Is JSONPath JSONPathKey
    JSONPathRoot JSONPathSubscript Join LT LTE Like Limit LimitOptions Literal Mod Mul NEQ Neg
    Not Null NullSafeEQ NullSafeNEQ Offset Or Order Ordered Paren RegexpILike RegexpLike
    Rollup Select SimilarTo Star Sub Subquery Table TableAlias Tuple Union Var Where Window
    WindowSpec With WithinGroup Xor
""".split()


_FUNCTION_SNAPSHOT = """
    Abs AnyValue Array ArrayAgg Ascii Atan2 Avg Case Cast Ceil Chr Coalesce Concat ConcatWs
    Cos Cot Count CumeDist CurrentDate CurrentTime CurrentTimestamp DateAdd DateDiff DateSub
    DateTrunc Day DayOfMonth DayOfWeek DayOfYear Dayname Degrees DenseRank Exp Extract
    FirstValue Floor FromBase64 Greatest GroupConcat Hex Hour If Initcap JSONBExtract
    JSONBExtractScalar JSONExtract JSONExtractScalar Lag LastDay LastValue Lead Least Left
    Length Ln Log LogicalAnd LogicalOr Lower MD5 Max Min Minute Month NthValue Ntile Nullif
    NumberToStr PercentRank PercentileCont PercentileDisc Pi Pow Quarter Radians Rand Rank
    RegexpReplace Replace Reverse Right Round RowNumber SHA SHA2 Second Sign Sin Soundex
    SplitPart Sqrt Stddev StddevPop StddevSamp StrPosition StrToDate Stuff Substring Sum Tan
    TimeToStr TimestampDiff TimestampTrunc ToBase64 Trim Trunc TsOrDsToDate TsOrDsToTimestamp
    Unhex UnixToTime Upper Uuid Variance VariancePop Week WeekOfYear Year
""".split()


_ANON_SNAPSHOT = {
    "mysql": [
        "adddate",
        "crc32",
        "field",
        "find_in_set",
        "from_days",
        "json_contains",
        "json_length",
        "json_unquote",
        "json_valid",
        "now",
        "octet_length",
        "ord",
        "regexp_substr",
        "std",
        "strcmp",
        "subdate",
        "time_format",
        "timestampadd",
        "unix_timestamp",
    ],
    "postgres": [
        "age",
        "btrim",
        "every",
        "json_build_object",
        "jsonb_agg",
        "jsonb_array_length",
        "jsonb_build_object",
        "now",
    ],
}


_STRUCTURAL_ARG_TYPES = {
    "Alias": ["alias", "this"],
    "CTE": ["alias", "key_expressions", "materialized", "scalar", "this"],
    "Column": ["catalog", "db", "join_mark", "table", "this"],
    "Distinct": ["expressions", "on"],
    "Except": [
        "by_name",
        "cluster",
        "connect",
        "distinct",
        "distribute",
        "expression",
        "for_",
        "format",
        "group",
        "having",
        "joins",
        "kind",
        "laterals",
        "limit",
        "locks",
        "match",
        "offset",
        "on",
        "options",
        "order",
        "pivots",
        "prewhere",
        "qualify",
        "sample",
        "settings",
        "side",
        "sort",
        "this",
        "where",
        "windows",
        "with_",
    ],
    "Fetch": ["count", "direction", "limit_options"],
    "Group": ["all", "cube", "expressions", "grouping_sets", "rollup", "totals"],
    "Intersect": [
        "by_name",
        "cluster",
        "connect",
        "distinct",
        "distribute",
        "expression",
        "for_",
        "format",
        "group",
        "having",
        "joins",
        "kind",
        "laterals",
        "limit",
        "locks",
        "match",
        "offset",
        "on",
        "options",
        "order",
        "pivots",
        "prewhere",
        "qualify",
        "sample",
        "settings",
        "side",
        "sort",
        "this",
        "where",
        "windows",
        "with_",
    ],
    "Join": [
        "directed",
        "expressions",
        "global_",
        "hint",
        "kind",
        "match_condition",
        "method",
        "on",
        "pivots",
        "side",
        "this",
        "using",
    ],
    "Limit": ["expression", "expressions", "limit_options", "offset", "this"],
    "Offset": ["expression", "expressions", "this"],
    "Order": ["expressions", "siblings", "this"],
    "Ordered": ["desc", "nulls_first", "this", "with_fill"],
    "Select": [
        "cluster",
        "connect",
        "distinct",
        "distribute",
        "exclude",
        "expressions",
        "for_",
        "format",
        "from_",
        "group",
        "having",
        "hint",
        "into",
        "joins",
        "kind",
        "laterals",
        "limit",
        "locks",
        "match",
        "offset",
        "operation_modifiers",
        "options",
        "order",
        "pivots",
        "prewhere",
        "qualify",
        "sample",
        "settings",
        "sort",
        "where",
        "windows",
        "with_",
    ],
    "Star": ["except_", "rename", "replace"],
    "Subquery": [
        "alias",
        "cluster",
        "connect",
        "distribute",
        "for_",
        "format",
        "group",
        "having",
        "joins",
        "laterals",
        "limit",
        "locks",
        "match",
        "offset",
        "options",
        "order",
        "pivots",
        "prewhere",
        "qualify",
        "sample",
        "settings",
        "sort",
        "this",
        "where",
        "windows",
        "with_",
    ],
    "Table": [
        "alias",
        "catalog",
        "changes",
        "db",
        "format",
        "hints",
        "indexed",
        "joins",
        "laterals",
        "only",
        "ordinality",
        "partition",
        "pattern",
        "pivots",
        "rows_from",
        "sample",
        "system_time",
        "this",
        "version",
        "when",
    ],
    "TableAlias": ["columns", "this"],
    "Union": [
        "by_name",
        "cluster",
        "connect",
        "distinct",
        "distribute",
        "expression",
        "for_",
        "format",
        "group",
        "having",
        "joins",
        "kind",
        "laterals",
        "limit",
        "locks",
        "match",
        "offset",
        "on",
        "options",
        "order",
        "pivots",
        "prewhere",
        "qualify",
        "sample",
        "settings",
        "side",
        "sort",
        "this",
        "where",
        "windows",
        "with_",
    ],
    "Window": ["alias", "first", "order", "over", "partition_by", "spec", "this"],
    "WindowSpec": ["end", "end_side", "exclude", "kind", "start", "start_side"],
    "With": ["expressions", "recursive", "search"],
}


_STRUCTURAL_ALLOWED = {
    "Alias": ["alias", "this"],
    "CTE": ["alias", "materialized", "this"],
    "Column": ["catalog", "db", "table", "this"],
    "Distinct": ["expressions", "on"],
    "Except": ["distinct", "expression", "limit", "offset", "order", "this", "with_"],
    "Fetch": ["count", "direction", "limit_options"],
    "Group": ["cube", "expressions", "grouping_sets", "rollup"],
    "Intersect": ["distinct", "expression", "limit", "offset", "order", "this", "with_"],
    "Join": ["kind", "method", "on", "side", "this", "using"],
    "Limit": ["expression", "expressions", "limit_options", "offset", "this"],
    "Offset": ["expression", "expressions", "this"],
    "Order": ["expressions", "this"],
    "Ordered": ["desc", "nulls_first", "this"],
    "Select": [
        "distinct",
        "expressions",
        "from_",
        "group",
        "having",
        "joins",
        "limit",
        "offset",
        "order",
        "where",
        "windows",
        "with_",
    ],
    "Star": [],
    "Subquery": ["alias", "limit", "offset", "order", "this"],
    "Table": ["alias", "catalog", "db", "this"],
    "TableAlias": ["columns", "this"],
    "Union": ["distinct", "expression", "limit", "offset", "order", "this", "with_"],
    "Window": ["alias", "order", "over", "partition_by", "spec", "this"],
    "WindowSpec": ["end", "end_side", "kind", "start", "start_side"],
    "With": ["expressions", "recursive"],
}


_ARG_DIGEST = "11153e36ea31db0b013cd569b2b6762dad4f5a2cf21190e53632371db3778bdf"

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _verdict(sql, engine="mysql", **kwargs):
    return policy.validate_agent_select(sql, engine=engine, database=DATABASE, **kwargs)


# --------------------------------------------------------------------------- #
# Guard de versión (D14)                                                       #
# --------------------------------------------------------------------------- #
def test_the_installed_sqlglot_is_inside_the_validated_range():
    policy.assert_supported_sqlglot(sqlglot.__version__)


@pytest.mark.parametrize("version", ["30.11.0", "30.11.7", "30.11"])
def test_the_version_guard_accepts_the_validated_range(version):
    policy.assert_supported_sqlglot(version)


@pytest.mark.parametrize("version", ["30.12.0", "30.10.9", "31.0.0", "29.99.0", "0", "garbage"])
def test_the_version_guard_rejects_everything_else(version):
    with pytest.raises(RuntimeError):
        policy.assert_supported_sqlglot(version)


def test_the_dependency_pin_matches_the_guard():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [d for d in pyproject["project"]["dependencies"] if d.startswith("sqlglot")]

    assert pins == ["sqlglot>=30.11,<30.12"]
    assert policy.SQLGLOT_MIN == (30, 11) and policy.SQLGLOT_BELOW == (30, 12)


# --------------------------------------------------------------------------- #
# Censo de nodos y funciones                                                   #
# --------------------------------------------------------------------------- #
def test_node_census_snapshot():
    assert sorted(c.__name__ for c in policy.ALLOWED_NODES) == _NODE_SNAPSHOT, (
        "La allowlist de nodos cambió: si es a propósito, revisá el diff y regenerá la foto."
    )


def test_function_census_snapshot():
    assert sorted(c.__name__ for c in policy.ALLOWED_FUNCTIONS) == _FUNCTION_SNAPSHOT, (
        "La allowlist de funciones cambió: si es a propósito, revisá el diff y regenerá la foto."
    )


def test_anonymous_function_census_snapshot():
    assert {d: sorted(v) for d, v in policy.ANON_ALLOWED.items()} == _ANON_SNAPSHOT


@pytest.mark.parametrize("name", ["Stuff", "Replace", "RegexpReplace"])
def test_verb_named_read_functions_are_typed_and_allowed(name):
    """
    H2: ``INSERT(str,pos,len,new)`` de MySQL es ``exp.Stuff`` y ``REPLACE(…)`` es ``exp.Replace``:
    funciones de LECTURA con nombre de verbo. Si sqlglot las tipificara distinto, A6 dejaría de
    aceptarse y este test lo dice antes que un usuario.
    """
    assert getattr(exp, name) in policy.ALLOWED_FUNCTIONS


def test_the_insert_function_parses_as_stuff_and_replace_as_replace():
    stuff = sqlglot.parse_one("SELECT INSERT('abc', 1, 1, 'x')", read="mysql").expressions[0]
    replace = sqlglot.parse_one("SELECT REPLACE(c, 'a', 'b') FROM t", read="mysql").expressions[0]

    assert type(stuff) is exp.Stuff and type(replace) is exp.Replace


def test_node_and_function_lists_do_not_overlap_and_hold_only_nodes():
    assert not (policy.ALLOWED_NODES & policy.ALLOWED_FUNCTIONS)
    for cls in policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS:
        assert issubclass(cls, exp.Expression), cls
    # Un nodo con efecto o que cambia la clase de la sentencia JAMÁS está permitido.
    for cls in (
        exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command,
        exp.Into, exp.Lock, exp.Hint, exp.Set, exp.Use, exp.Copy, exp.Parameter, exp.Placeholder,
        exp.SessionParameter, exp.PropertyEQ, exp.Anonymous,
    ):  # fmt: skip
        assert cls not in policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS, cls.__name__


def test_the_roots_are_exactly_the_four_read_shapes():
    assert set(policy._ROOTS) == {exp.Select, exp.Union, exp.Intersect, exp.Except}


# --------------------------------------------------------------------------- #
# Censo de argumentos: la línea base sale de ``arg_types``, no se hardcodea     #
# --------------------------------------------------------------------------- #
def test_select_arg_keys_are_the_ones_sqlglot_declares():
    """Las claves de 30.11 son ``from_``/``with_``/``for_``; si se renombran, esto falla fuerte."""
    declared = set(exp.Select.arg_types)

    assert {"from_", "with_", "for_", "locks", "into", "hint", "sample"} <= declared
    allowed = policy.ALLOWED_ARGS[exp.Select]
    assert allowed <= declared
    assert {
        "expressions",
        "from_",
        "joins",
        "where",
        "group",
        "having",
        "order",
        "limit",
    } <= allowed
    assert {"offset", "distinct", "with_", "windows"} <= allowed
    # Lo que carga efectos o modificadores NO está permitido.
    assert not (allowed & {"for_", "locks", "into", "hint", "sample", "operation_modifiers"})
    assert not (allowed & {"settings", "options", "connect", "prewhere", "format", "kind"})


@pytest.mark.parametrize("name", sorted(_STRUCTURAL_ARG_TYPES))
def test_structural_arg_allowlists_are_subsets_of_the_declared_keys(name):
    cls = getattr(exp, name)

    assert policy.STRUCTURAL_ARGS[cls] <= set(cls.arg_types), name
    # La foto completa de lo que sqlglot declara: una clave nueva o renombrada rompe acá.
    assert sorted(cls.arg_types) == _STRUCTURAL_ARG_TYPES[name], (
        f"{name}: sqlglot cambió sus argumentos; decidí si el nuevo se permite y regenerá la foto."
    )
    assert sorted(policy.STRUCTURAL_ARGS[cls]) == _STRUCTURAL_ALLOWED[name], name


def test_every_allowed_class_has_an_arg_entry_inside_its_declared_keys():
    for cls in policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS:
        assert cls in policy.ALLOWED_ARGS, cls.__name__
        assert policy.ALLOWED_ARGS[cls] <= set(cls.arg_types), cls.__name__


def test_generic_classes_take_their_installed_keys_and_none_is_effectful():
    for cls in policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS:
        if cls in policy.STRUCTURAL_ARGS:
            continue
        assert policy.ALLOWED_ARGS[cls] == frozenset(cls.arg_types), cls.__name__
        assert not (policy.ALLOWED_ARGS[cls] & policy._NEVER_ARG_KEYS), cls.__name__


def test_arg_key_digest_of_every_allowed_class_is_unchanged():
    """
    Foto de TODAS las claves de ``arg_types`` de las clases permitidas (las estructurales ya tienen
    foto legible arriba). Un argumento nuevo en cualquier función u operador cambia el digest.
    """
    classes = policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS
    digest = hashlib.sha256(
        json.dumps({c.__name__: sorted(c.arg_types) for c in classes}, sort_keys=True).encode()
    ).hexdigest()

    assert digest == _ARG_DIGEST, (
        "Cambió alguna arg_types de una clase permitida. Revisá cuál (compará contra la foto "
        "anterior) y regenerá _ARG_DIGEST."
    )


# --------------------------------------------------------------------------- #
# Denylist de efectos: disjunta de las allowlists                              #
# --------------------------------------------------------------------------- #
def test_side_effect_functions_are_disjoint_from_every_allowlist():
    names = set()
    for cls in policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS:
        if issubclass(cls, exp.Func):
            names.update(n.lower() for n in cls.sql_names())
    for allowed in policy.ANON_ALLOWED.values():
        names.update(allowed)

    assert not {n for n in names if policy.is_side_effect_function(n)}
    assert not (names & policy.SIDE_EFFECT_FUNCTIONS)


@pytest.mark.parametrize(
    "name",
    ["pg_sleep", "pg_sleep_for", "pg_advisory_lock", "pg_advisory_xact_lock", "lo_import",
     "lo_unlink", "dblink", "dblink_exec", "SLEEP", "Get_Lock", "nextval", "VERSION"],
)  # fmt: skip
def test_the_side_effect_matcher_is_case_insensitive_and_covers_families(name):
    assert policy.is_side_effect_function(name)


@pytest.mark.parametrize("name", ["lower", "now", "replace", "concat", "log", "lpad_x"])
def test_the_side_effect_matcher_does_not_overreach(name):
    assert not policy.is_side_effect_function(name)


# --------------------------------------------------------------------------- #
# S6: un nodo desconocido se rechaza                                           #
# --------------------------------------------------------------------------- #
def test_a_node_class_unknown_to_the_allowlist_is_rejected():
    """
    S6. Un ``StubNode`` que ninguna sintaxis produce simula el nodo que una versión futura de
    sqlglot agregaría: tiene que fallar por default (``UNSUPPORTED_NODE``), no pasar.
    """

    class StubNode(exp.Expression):
        arg_types = {"this": False}

    tree = sqlglot.parse_one("SELECT 1", read="mysql")
    tree.append("expressions", StubNode())

    found = policy.inspect_tree(tree, engine="mysql", database=DATABASE)

    assert found == [policy.UNSUPPORTED_CONSTRUCT]
    assert codes.public_reason(found[0]) == codes.REASON_UNSUPPORTED_NODE


def test_a_function_class_unknown_to_the_allowlist_is_rejected_as_a_function():
    # Una clase real de sqlglot y no una subclase local de `exp.Func`: sqlglot no sabe recorrer una
    # subclase definida fuera de su paquete (`iter_expressions` levanta NotImplementedError), así
    # que el test fallaba antes de llegar al validador. `NextValueFor` (nextval) además es una
    # función con efecto lateral que no está en la lista permitida.
    tree = sqlglot.parse_one("SELECT 1", read="postgres")
    tree.append("expressions", exp.NextValueFor())

    assert policy.inspect_tree(tree, engine="postgresql", database=DATABASE) == [
        policy.FUNCTION_NOT_ALLOWED
    ]


def test_an_argument_outside_the_allowlist_on_an_allowed_node_is_rejected():
    tree = sqlglot.parse_one("SELECT a FROM t", read="mysql")
    tree.set("operation_modifiers", [exp.Var(this="SQL_CALC_FOUND_ROWS")])

    assert policy.UNSUPPORTED_CONSTRUCT in policy.inspect_tree(
        tree, engine="mysql", database=DATABASE
    )


# --------------------------------------------------------------------------- #
# Mapeo interno -> público: total y cerrado (S5-S11, spec de códigos)           #
# --------------------------------------------------------------------------- #
_PUBLIC_REASONS = {
    "PARSE_FAILED", "MULTIPLE_STATEMENTS", "NOT_SELECT", "DML_IN_CTE", "DML_IN_SUBQUERY",
    "SELECT_INTO", "LOCKING_READ", "FUNCTION_NOT_ALLOWED", "VARIABLE_ASSIGNMENT",
    "EXECUTABLE_COMMENT", "COMMENT_NOT_ALLOWED", "SYSTEM_SCHEMA", "CROSS_DATABASE",
    "UNSUPPORTED_NODE", "LIMIT_TOO_HIGH", "OFFSET_TOO_HIGH", "UNKNOWN_IDENTIFIER", "DATA_DISABLED",
    "PROBE_NOT_GREEN", "PII_BLOCKED", "QUERY_TIMEOUT", "AUDIT_UNAVAILABLE", "CREDENTIAL_TOO_BROAD",
    "WRITE_PRIVILEGE_PRESENT", "FEDERATED_TABLE_PRESENT", "SQL_TOO_LARGE", "LIMIT_NOT_BOUNDABLE",
    "QUERY_FAILED", "MALFORMED_REQUEST", "DATA_ACCOUNT_BUSY",
}  # fmt: skip


def test_the_public_reason_set_is_closed_and_exact():
    assert codes.REASON_CODES == frozenset(_PUBLIC_REASONS)
    assert codes.WARNING_CODES == frozenset(
        {"WRITE_NOT_EXECUTED", "DDL_NOT_EXECUTED", "LIMIT_TOO_HIGH"}
    )


def test_every_internal_code_maps_to_a_public_one():
    for internal in policy.INTERNAL_CODES:
        assert internal in codes.INTERNAL_TO_PUBLIC, internal
    assert set(codes.INTERNAL_TO_PUBLIC.values()) <= codes.REASON_CODES


def test_every_internal_code_declared_in_the_module_is_listed():
    declared = {
        v
        for k, v in vars(policy).items()
        if isinstance(v, str) and v.startswith("agent_sql.") and k.isupper()
    }

    assert declared == set(policy.INTERNAL_CODES)


def test_an_untranslated_internal_code_fails_closed_never_leaks():
    assert codes.public_reason("agent_sql.nuevo") == codes.REASON_UNSUPPORTED_NODE
    assert codes.public_reason("") == codes.REASON_UNSUPPORTED_NODE


def test_internal_names_never_reach_the_agent():
    samples = [
        "SELECT SLEEP(1)",
        "DELETE FROM t",
        "SELECT 1 -- x",
        "SELECT * FROM (",
        "SELECT 1; SELECT 2",
    ]
    for sql in samples:
        verdict = _verdict(sql)
        envelope = policy.build_draft_envelope(verdict, sql, max_bytes=16_384)

        assert "agent_sql" not in json.dumps(envelope), sql
        assert set(envelope["reasons"]) <= codes.REASON_CODES
        assert set(envelope["warnings"]) <= codes.WARNING_CODES


# --------------------------------------------------------------------------- #
# D4: el render vuelve a pasar por el pipeline completo                         #
# --------------------------------------------------------------------------- #
def test_the_round_trip_reruns_the_full_pipeline_on_the_canonical_and_the_executed_text(
    monkeypatch,
):
    calls = []
    original = policy._analyze_text

    def spy(sql, **kwargs):
        calls.append((sql, kwargs["check_bounds"]))
        return original(sql, **kwargs)

    monkeypatch.setattr(policy, "_analyze_text", spy)

    verdict = _verdict("select * from t")

    assert verdict.accepted
    assert calls == [
        ("select * from t", True),  # el texto del agente
        ("SELECT * FROM t", False),  # el render canónico: pipeline completo otra vez
        ("SELECT * FROM t LIMIT 101", False),  # lo que se ejecutaría, con el tope empujado
    ]


def test_a_render_that_does_not_re_validate_cleanly_is_rejected(monkeypatch):
    original = policy._analyze_text

    def poisoned(sql, **kwargs):
        analysis = original(sql, **kwargs)
        if kwargs["check_bounds"] is False and sql == "SELECT * FROM t":
            analysis.add(policy.COMMENT)  # el render "ganó" un comentario que el original no tenía
        return analysis

    monkeypatch.setattr(policy, "_analyze_text", poisoned)

    verdict = _verdict("SELECT * FROM t")

    assert not verdict.accepted
    assert verdict.reasons == (codes.REASON_UNSUPPORTED_NODE,)
    assert verdict.executed_sql is None


def test_a_render_with_a_different_node_multiset_is_rejected(monkeypatch):
    monkeypatch.setattr(
        policy, "_node_multiset", lambda root: {"call": id(root)}
    )  # cada llamada devuelve algo distinto: el árbol original y el re-parseado no coinciden

    verdict = _verdict("SELECT * FROM t")

    assert not verdict.accepted
    assert verdict.reasons == (codes.REASON_UNSUPPORTED_NODE,)


# --------------------------------------------------------------------------- #
# Léxico compartido (D6/D7)                                                    #
# --------------------------------------------------------------------------- #
_AMBIGUOUS = r"SELECT 'a\'' -- x"


def test_the_two_backslash_modes_disagree_on_the_probe_and_the_validator_rejects_it():
    plain = sql_lexing.lex_spans(_AMBIGUOUS, engine="mysql", double_quote_is_string=True)
    escaped = sql_lexing.lex_spans(
        _AMBIGUOUS, engine="mysql", backslash_escapes=True, double_quote_is_string=True
    )

    assert plain != escaped
    # En modo con escape hay UN literal y un comentario; sin escape, un literal sin cerrar.
    assert [s.kind for s in escaped] == [sql_lexing.STRING, sql_lexing.LINE_COMMENT]
    assert not plain[-1].terminated
    assert _verdict(_AMBIGUOUS).reasons == (codes.REASON_PARSE_FAILED,)


def test_literal_spans_cover_comment_markers_inside_strings():
    sql = "SELECT 'a--b', 'x#y', '/* z */'"
    spans = sql_lexing.literal_spans(sql, "mysql", False)

    assert [sql[s:e] for s, e in spans] == ["'a--b'", "'x#y'", "'/* z */'"]
    assert not [s for s in sql_lexing.lex_spans(sql) if s.kind in sql_lexing.COMMENT_KINDS]


def test_hash_is_a_comment_in_mysql_but_an_operator_in_postgres():
    sql = "SELECT 1 # 2"

    assert [s.kind for s in sql_lexing.lex_spans(sql, engine="mysql")] == [sql_lexing.LINE_COMMENT]
    assert sql_lexing.lex_spans(sql, engine="postgresql") == []


def test_double_quotes_are_strings_in_mysql_and_identifiers_in_postgres():
    sql = 'SELECT "a--b"'

    mysql = sql_lexing.lex_spans(sql, engine="mysql", double_quote_is_string=True)
    pg = sql_lexing.lex_spans(sql, engine="postgresql", double_quote_is_string=False)

    assert [s.kind for s in mysql] == [sql_lexing.STRING]
    assert [s.kind for s in pg] == [sql_lexing.QUOTED_IDENT]


def test_executable_comments_are_reported_apart_from_ordinary_ones():
    kinds = [s.kind for s in sql_lexing.lex_spans("SELECT /*! 1 */ 2 /*M! 3 */ /* 4 */")]

    assert kinds == [sql_lexing.EXEC_COMMENT, sql_lexing.EXEC_COMMENT, sql_lexing.BLOCK_COMMENT]


# --------------------------------------------------------------------------- #
# Regeneración de la foto                                                      #
# --------------------------------------------------------------------------- #
def _snapshot_digest() -> str:
    """
    Para regenerar ``_ARG_DIGEST`` tras revisar un cambio de sqlglot::

        .venv/bin/python -c "from tests.test_agent_sql_invariants import _snapshot_digest as d; print(d())"
    """
    classes = policy.ALLOWED_NODES | policy.ALLOWED_FUNCTIONS
    return hashlib.sha256(
        json.dumps({c.__name__: sorted(c.arg_types) for c in classes}, sort_keys=True).encode()
    ).hexdigest()
