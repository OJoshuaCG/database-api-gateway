"""
Corpus del validador de SQL de agentes (``app/services/db_admin/agent_sql_policy.py``).

DOS DIRECCIONES, Y LAS DOS SON UN CONTRATO
------------------------------------------
- ``MUST_REJECT`` (direction A, R1-R15 de la spec más las sondas de la revisión): todo texto que
  NO puede salir como lectura aceptada, con el código PÚBLICO que lo explica.
- ``MUST_ACCEPT`` (direction B, A1-A8): lecturas legítimas que un enfoque por palabras clave
  rechazaría (columnas que se llaman ``update``, literales que dicen ``DELETE FROM``…). Se aceptan
  porque la decisión sale del AST y no de palabras sueltas.

Cada fila de ``MUST_REJECT`` trae un CONJUNTO de códigos aceptables y alcanza con que UNO esté en
``reasons``. No es laxitud: sqlglot 30.11 no parsea algunas formas que la spec daba por parseables
(``SELECT * FROM (DELETE …)``, ``SELECT … INTO OUTFILE``, ``DO 1`` en MySQL), así que el rechazo es
``PARSE_FAILED`` y no el código más específico. El validador agrega el específico cuando lo puede
leer de los tokens (``SELECT_INTO``, ``DML_IN_SUBQUERY``), y estas filas aceptan los dos.

El motor de cada fila es el de la base fijada (``DATABASE``). El arnés directo no multiplica fixtures
por parámetro, por eso el corpus se expande acá en una lista plana de casos (``MUST_REJECT_CASES``).
"""

from dataclasses import dataclass

MYSQL = "mysql"
MARIADB = "mariadb"
PG = "postgresql"

#: La base fijada de la conexión: contra ella se resuelven los nombres calificados.
DATABASE = "mydb"

ALL_ENGINES = (MYSQL, MARIADB, PG)
MYSQL_FAMILY = (MYSQL, MARIADB)


@dataclass(frozen=True)
class Case:
    """Un caso del corpus ya expandido por motor."""

    id: str
    engine: str
    sql: str
    codes: frozenset = frozenset()


def _expand(rows) -> list[Case]:
    cases = []
    for row_id, engines, sql, codes in rows:
        for engine in engines:
            cases.append(Case(f"{row_id}:{engine}", engine, sql, frozenset(codes)))
    return cases


# --------------------------------------------------------------------------- #
# Direction A — MUST_REJECT                                                    #
# --------------------------------------------------------------------------- #
_BOTH = (MYSQL, PG)

_MUST_REJECT_ROWS = [
    # R1 — DML dentro de un CTE (también UPDATE/INSERT/MERGE).
    ("R1-delete", _BOTH, "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", {"DML_IN_CTE"}),
    (
        "R1-update",
        _BOTH,
        "WITH d AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM d",
        {"DML_IN_CTE"},
    ),
    (
        "R1-insert",
        _BOTH,
        "WITH d AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM d",
        {"DML_IN_CTE"},
    ),
    (
        "R1-merge",
        _BOTH,
        "WITH d AS (MERGE INTO t USING u ON t.id = u.id WHEN MATCHED THEN DELETE) SELECT 1",
        {"DML_IN_CTE"},
    ),
    # R2 — DML en subconsulta. sqlglot no los parsea: PARSE_FAILED es un rechazo válido.
    (
        "R2-derived",
        _BOTH,
        "SELECT * FROM (DELETE FROM t RETURNING *) x",
        {"DML_IN_SUBQUERY", "PARSE_FAILED"},
    ),
    ("R2-scalar", _BOTH, "SELECT (DELETE FROM t), 1", {"DML_IN_SUBQUERY", "PARSE_FAILED"}),
    (
        "R2-exists",
        _BOTH,
        "SELECT 1 WHERE EXISTS (DELETE FROM t RETURNING 1)",
        {"DML_IN_SUBQUERY", "PARSE_FAILED"},
    ),
    (
        "R2-in",
        _BOTH,
        "SELECT 1 WHERE 1 IN (DELETE FROM t RETURNING id)",
        {"DML_IN_SUBQUERY", "PARSE_FAILED"},
    ),
    # R3 — varias sentencias.
    ("R3-delete", ALL_ENGINES, "SELECT 1; DELETE FROM t", {"MULTIPLE_STATEMENTS"}),
    ("R3-two-selects", _BOTH, "SELECT 1; SELECT 2", {"MULTIPLE_STATEMENTS"}),
    ("R3-double-semicolon", _BOTH, "SELECT 1;;", {"MULTIPLE_STATEMENTS"}),
    # R4 — SELECT INTO. ``INTO OUTFILE`` no parsea en sqlglot: SELECT_INTO sale de los tokens.
    (
        "R4-outfile",
        (MYSQL, MARIADB),
        "SELECT a INTO OUTFILE '/x' FROM t",
        {"SELECT_INTO", "PARSE_FAILED"},
    ),
    (
        "R4-outfile-tail",
        (MYSQL, MARIADB),
        "SELECT a FROM t INTO OUTFILE '/x'",
        {"SELECT_INTO", "PARSE_FAILED"},
    ),
    (
        "R4-dumpfile",
        (MYSQL, MARIADB),
        "SELECT a INTO DUMPFILE '/x' FROM t",
        {"SELECT_INTO", "PARSE_FAILED"},
    ),
    ("R4-variable", (MYSQL,), "SELECT a INTO @v FROM t", {"SELECT_INTO"}),
    ("R4-table", (PG,), "SELECT a INTO new_t FROM t", {"SELECT_INTO"}),
    # R5 — CTAS.
    ("R5-ctas", _BOTH, "CREATE TABLE n AS SELECT * FROM t", {"NOT_SELECT"}),
    # R6 — lecturas con locks.
    ("R6-for-update", _BOTH, "SELECT * FROM t FOR UPDATE", {"LOCKING_READ"}),
    ("R6-for-share", _BOTH, "SELECT * FROM t FOR SHARE", {"LOCKING_READ"}),
    ("R6-lock-in-share-mode", (MYSQL,), "SELECT * FROM t LOCK IN SHARE MODE", {"LOCKING_READ"}),
    # R7 — sentencias que no son SELECT.
    ("R7-explain-analyze-delete", _BOTH, "EXPLAIN ANALYZE DELETE FROM t", {"NOT_SELECT"}),
    ("R7-explain-analyze-select", _BOTH, "EXPLAIN ANALYZE SELECT * FROM t", {"NOT_SELECT"}),
    ("R7-call", _BOTH, "CALL p()", {"NOT_SELECT"}),
    ("R7-prepare", _BOTH, "PREPARE s FROM 'select 1'", {"NOT_SELECT"}),
    ("R7-execute", _BOTH, "EXECUTE s", {"NOT_SELECT"}),
    ("R7-do", _BOTH, "DO 1", {"NOT_SELECT", "PARSE_FAILED"}),
    ("R7-set", _BOTH, "SET a = 1", {"NOT_SELECT"}),
    ("R7-use", _BOTH, "USE x", {"NOT_SELECT"}),
    ("R7-copy", _BOTH, "COPY t TO '/x'", {"NOT_SELECT"}),
    # R8 — funciones con efecto.
    ("R8-sleep", (MYSQL,), "SELECT SLEEP(1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-benchmark", (MYSQL,), "SELECT BENCHMARK(1000, MD5('a'))", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-get-lock", (MYSQL,), "SELECT GET_LOCK('a', 1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-release-lock", (MYSQL,), "SELECT RELEASE_LOCK('a')", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-load-file", (MYSQL,), "SELECT LOAD_FILE('/etc/passwd')", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-sys-exec", (MYSQL,), "SELECT sys_exec('id')", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-nextval", (PG,), "SELECT nextval('s')", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-setval", (PG,), "SELECT setval('s', 1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-set-config", (PG,), "SELECT set_config('a', 'b', false)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-terminate", (PG,), "SELECT pg_terminate_backend(1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-advisory", (PG,), "SELECT pg_advisory_lock(1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-lo-import", (PG,), "SELECT lo_import('/x')", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-pg-sleep", (PG,), "SELECT pg_sleep(1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-version", _BOTH, "SELECT version()", {"FUNCTION_NOT_ALLOWED"}),
    ("R8-current-user", _BOTH, "SELECT CURRENT_USER", {"FUNCTION_NOT_ALLOWED"}),
    # R9 — función desconocida o calificada: fail-closed por allowlist.
    ("R9-udf", _BOTH, "SELECT my_udf(1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R9-qualified", _BOTH, "SELECT db.f(1)", {"FUNCTION_NOT_ALLOWED"}),
    ("R9-qualified-allowed-name", _BOTH, "SELECT pg_catalog.now()", {"FUNCTION_NOT_ALLOWED"}),
    (
        "R9-table-function",
        (PG,),
        "SELECT * FROM generate_series(1, 3) g",
        {"FUNCTION_NOT_ALLOWED", "UNSUPPORTED_NODE"},
    ),
    # R10 — variables y asignaciones.
    ("R10-assign", (MYSQL,), "SELECT @a := 1", {"VARIABLE_ASSIGNMENT"}),
    ("R10-assign-nospace", (MYSQL,), "SELECT @a:=1", {"VARIABLE_ASSIGNMENT"}),
    ("R10-read-variable", (MYSQL,), "SELECT @a", {"VARIABLE_ASSIGNMENT"}),
    ("R10-system-variable", (MYSQL,), "SELECT @@version", {"VARIABLE_ASSIGNMENT"}),
    ("R10-placeholder", (MYSQL,), "SELECT ?", {"VARIABLE_ASSIGNMENT"}),
    ("R10-positional", (PG,), "SELECT $1", {"VARIABLE_ASSIGNMENT"}),
    # R11 — comentarios ejecutables (también dentro de un literal: falso positivo aceptado).
    ("R11-bare", ALL_ENGINES, "/*! DELETE FROM t */", {"EXECUTABLE_COMMENT"}),
    ("R11-bare-m", ALL_ENGINES, "/*M! DELETE FROM t */", {"EXECUTABLE_COMMENT"}),
    ("R11-version-gated", ALL_ENGINES, "SELECT 1 /*!50000 ,2 */", {"EXECUTABLE_COMMENT"}),
    ("R11-m-version-gated", ALL_ENGINES, "SELECT 1 /*M!100000 ,2 */", {"EXECUTABLE_COMMENT"}),
    ("R11-in-literal", ALL_ENGINES, "SELECT '/*! x */'", {"EXECUTABLE_COMMENT"}),
    ("R11-lowercase-m", MYSQL_FAMILY, "SELECT 1 /*m! ,2 */", {"EXECUTABLE_COMMENT"}),
    # R12 — contrabando por mayúsculas, espacios y comentarios.
    ("R12-mixed-case", _BOTH, "dElEtE  FROM t", {"NOT_SELECT", "COMMENT_NOT_ALLOWED"}),
    (
        "R12-comment-between",
        _BOTH,
        "DELETE/**/FROM t",
        {"NOT_SELECT", "COMMENT_NOT_ALLOWED", "PARSE_FAILED"},
    ),
    (
        "R12-quoted-keyword",
        _BOTH,
        '"delete" from t',
        {"NOT_SELECT", "COMMENT_NOT_ALLOWED", "PARSE_FAILED"},
    ),
    ("R12-whitespace", _BOTH, "DELETE\n\tFROM\tt", {"NOT_SELECT"}),
    ("R12-leading-comment", _BOTH, "/* c */ DELETE FROM t", {"COMMENT_NOT_ALLOWED"}),
    # R13 — ilegible o truncado, y vacío.
    ("R13-truncated-from", _BOTH, "SELECT * FROM", {"PARSE_FAILED"}),
    ("R13-truncated-where", _BOTH, "SELECT a FROM t WHERE", {"PARSE_FAILED"}),
    ("R13-open-subquery", _BOTH, "SELECT * FROM (SELECT", {"PARSE_FAILED"}),
    ("R13-open-paren", _BOTH, "SELECT (1", {"PARSE_FAILED"}),
    ("R13-dangling-operator", _BOTH, "SELECT 1 +", {"PARSE_FAILED"}),
    ("R13-unterminated-string", _BOTH, "SELECT 'unterminated", {"PARSE_FAILED"}),
    ("R13-empty", _BOTH, "", {"PARSE_FAILED"}),
    ("R13-whitespace", _BOTH, "   \n\t ", {"PARSE_FAILED"}),
    ("R13-only-semicolon", _BOTH, ";", {"PARSE_FAILED"}),
    # R14 — esquemas del sistema.
    (
        "R14-information-schema",
        MYSQL_FAMILY,
        "SELECT * FROM information_schema.tables",
        {"SYSTEM_SCHEMA"},
    ),
    (
        "R14-performance-schema",
        MYSQL_FAMILY,
        "SELECT * FROM performance_schema.threads",
        {"SYSTEM_SCHEMA"},
    ),
    ("R14-mysql", MYSQL_FAMILY, "SELECT * FROM mysql.user", {"SYSTEM_SCHEMA"}),
    ("R14-sys", MYSQL_FAMILY, "SELECT * FROM sys.version", {"SYSTEM_SCHEMA"}),
    ("R14-pg-catalog", (PG,), "SELECT * FROM pg_catalog.pg_class", {"SYSTEM_SCHEMA"}),
    (
        "R14-pg-information-schema",
        (PG,),
        "SELECT * FROM information_schema.tables",
        {"SYSTEM_SCHEMA"},
    ),
    ("R14-pg-unqualified", (PG,), "SELECT * FROM pg_class", {"SYSTEM_SCHEMA"}),
    ("R14-gateway-internal", _BOTH, "SELECT * FROM _gw_v_core", {"SYSTEM_SCHEMA"}),
    # R15 — otra base distinta de la fijada.
    ("R15-table", MYSQL_FAMILY, "SELECT * FROM otra.t", {"CROSS_DATABASE"}),
    ("R15-column", MYSQL_FAMILY, "SELECT otra.t.c FROM otra.t", {"CROSS_DATABASE"}),
    ("R15-pg-three-part", (PG,), "SELECT * FROM otra.public.t", {"CROSS_DATABASE"}),
    # Sin excepción para el nombre de tres partes aunque ``catalog`` sea la base fijada.
    ("R15-pg-three-part-same-db", (PG,), f"SELECT * FROM {DATABASE}.public.t", {"CROSS_DATABASE"}),
    # Sondas de la revisión (D5-D7): lo que el parser y el motor leen distinto.
    (
        "P-quote-escape-then-comment",
        ALL_ENGINES,
        r"SELECT 'a\'' -- x",
        {"PARSE_FAILED"},
    ),
    ("P-backslash-in-literal", MYSQL_FAMILY, r"SELECT 'x\\y'", {"PARSE_FAILED"}),
    (
        "P-backslash-underscore",
        MYSQL_FAMILY,
        r"SELECT * FROM t WHERE a LIKE '%\_%'",
        {"PARSE_FAILED"},
    ),
    ("P-calc-found-rows", (MYSQL,), "SELECT SQL_CALC_FOUND_ROWS * FROM t", {"UNSUPPORTED_NODE"}),
    ("P-high-priority", (MYSQL,), "SELECT HIGH_PRIORITY * FROM t", {"UNSUPPORTED_NODE"}),
    ("P-index-hint", (MYSQL,), "SELECT * FROM t FORCE INDEX (i)", {"UNSUPPORTED_NODE"}),
    ("P-tablesample", (PG,), "SELECT * FROM t TABLESAMPLE SYSTEM (10)", {"UNSUPPORTED_NODE"}),
    (
        "P-optimizer-hint",
        MYSQL_FAMILY,
        "SELECT /*+ MAX_EXECUTION_TIME(1) */ 1",
        {"COMMENT_NOT_ALLOWED"},
    ),
    ("P-line-comment", ALL_ENGINES, "SELECT 1 -- x", {"COMMENT_NOT_ALLOWED"}),
    ("P-block-comment", ALL_ENGINES, "SELECT 1 /* x */", {"COMMENT_NOT_ALLOWED"}),
    ("P-hash-comment", MYSQL_FAMILY, "SELECT 1 # x", {"COMMENT_NOT_ALLOWED"}),
    ("P-unterminated-block-comment", ALL_ENGINES, "SELECT 1 /* x", {"PARSE_FAILED"}),
    ("P-subquery-with-limit-root", _BOTH, "(SELECT 1) LIMIT 5", {"UNSUPPORTED_NODE"}),
    ("P-values-root", _BOTH, "VALUES (1)", {"NOT_SELECT"}),
    ("P-show", _BOTH, "SHOW TABLES", {"NOT_SELECT"}),
    ("P-insert", _BOTH, "INSERT INTO t SELECT 1", {"NOT_SELECT"}),
    ("P-grant", _BOTH, "GRANT ALL ON *.* TO x", {"NOT_SELECT"}),
]

MUST_REJECT_CASES: list[Case] = _expand(_MUST_REJECT_ROWS)

# --------------------------------------------------------------------------- #
# Direction B — MUST_ACCEPT                                                    #
# --------------------------------------------------------------------------- #
_MYSQL_ONLY = MYSQL_FAMILY

_MUST_ACCEPT_ROWS = [
    # A1 — columnas con nombre de verbo (citadas: sin comillas MySQL ni siquiera las parsea).
    ("A1", _MYSQL_ONLY, "SELECT `update`, `delete`, `insert` FROM t", ()),
    ("A1", (PG,), 'SELECT "update", "delete", "insert" FROM t', ()),
    # A2 — tablas y columnas con nombre de verbo.
    ("A2", _MYSQL_ONLY, "SELECT deleted_at, updates FROM `delete`", ()),
    ("A2", (PG,), 'SELECT deleted_at, updates FROM "update"', ()),
    # A3 — alias y tablas con nombre de verbo.
    ("A3-join", ALL_ENGINES, "SELECT * FROM updates u JOIN inserts i ON u.id = i.id", ()),
    ("A3-alias", _MYSQL_ONLY, "SELECT u.id AS `update` FROM updates AS u", ()),
    ("A3-alias", (PG,), 'SELECT u.id AS "update" FROM updates AS u', ()),
    # A4 — la palabra peligrosa es un literal.
    ("A4-select", ALL_ENGINES, "SELECT 'DELETE FROM users'", ()),
    ("A4-like", ALL_ENGINES, "SELECT * FROM logs WHERE c LIKE '%UPDATE%'", ()),
    # A5 — comparación contra un literal.
    ("A5", ALL_ENGINES, "SELECT * FROM logs WHERE action = 'INSERT'", ()),
    # A6 — funciones de solo lectura con nombre de verbo (INSERT() es ``exp.Stuff`` en MySQL).
    ("A6-insert", _MYSQL_ONLY, "SELECT INSERT('abc', 1, 1, 'x')", ()),
    ("A6-replace", ALL_ENGINES, "SELECT REPLACE(c, 'a', 'b') FROM t", ()),
    # A7 — marcas de comentario dentro de literales.
    ("A7", ALL_ENGINES, "SELECT 'a--b', 'x#y', '/* z */'", ()),
    # A8 — formas permitidas.
    ("A8-cte", ALL_ENGINES, "WITH c AS (SELECT 1 AS x) SELECT * FROM c", ()),
    ("A8-union", ALL_ENGINES, "SELECT a FROM t UNION SELECT a FROM u", ()),
    (
        "A8-join",
        ALL_ENGINES,
        "SELECT t.id, u.name FROM t LEFT JOIN u ON u.id = t.uid WHERE t.n > 1",
        (),
    ),
    (
        "A8-group",
        ALL_ENGINES,
        "SELECT a, COUNT(*) FROM t GROUP BY a HAVING COUNT(*) > 1 ORDER BY 2 DESC LIMIT 10",
        (),
    ),
    (
        "A8-window",
        ALL_ENGINES,
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY g ORDER BY id) AS rn FROM t",
        (),
    ),
    (
        "A8-subqueries",
        ALL_ENGINES,
        "SELECT * FROM t WHERE id IN (SELECT id FROM u WHERE EXISTS (SELECT 1 FROM v WHERE v.id = u.id))",
        (),
    ),
    ("A8-derived", ALL_ENGINES, "SELECT * FROM (SELECT a, b FROM t) AS d WHERE d.a = 1", ()),
    (
        "A8-aggregates",
        ALL_ENGINES,
        "SELECT COUNT(DISTINCT a), SUM(b), AVG(c), MIN(d), MAX(e) FROM t",
        (),
    ),
    (
        "A8-scalars",
        ALL_ENGINES,
        "SELECT COALESCE(a, b), NULLIF(a, b), LOWER(a), UPPER(a), TRIM(a), LENGTH(a), "
        "SUBSTRING(a, 1, 2), CONCAT(a, b) FROM t",
        (),
    ),
    (
        "A8-case-cast",
        ALL_ENGINES,
        "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END, CAST(b AS CHAR) FROM t",
        (),
    ),
    (
        "A8-between",
        ALL_ENGINES,
        "SELECT * FROM t WHERE a BETWEEN 1 AND 10 AND b IS NOT NULL AND c NOT LIKE 'x%'",
        (),
    ),
    ("A8-offset", ALL_ENGINES, "SELECT * FROM t ORDER BY a LIMIT 10 OFFSET 20", ()),
    ("A8-arith", ALL_ENGINES, "SELECT 1 + 2 * 3, -a, a % 2 FROM t", ()),
    # Variantes legítimas del mismo borde.
    ("A-trailing-semicolon", ALL_ENGINES, "SELECT 1;", ()),
    ("A-own-database", _MYSQL_ONLY, f"SELECT {DATABASE}.t.id FROM {DATABASE}.t", ()),
    ("A-pg-schema", (PG,), "SELECT * FROM public.t", ()),
    ("A-mysql-limit-offset-comma", _MYSQL_ONLY, "SELECT * FROM t LIMIT 5, 10", ()),
    (
        "A-mysql-date-format",
        _MYSQL_ONLY,
        "SELECT DATE_FORMAT(created_at, '%Y-%m'), IFNULL(a, 0) FROM t",
        (),
    ),
    ("A-mysql-comma-join", _MYSQL_ONLY, "SELECT a FROM t1, t2 WHERE t1.id = t2.id", ()),
    ("A-pg-cast", (PG,), "SELECT a::int, CAST(b AS text), c || d FROM t", ()),
    ("A-pg-ilike", (PG,), "SELECT * FROM public.t WHERE a ILIKE '%x%'", ()),
    ("A-pg-filter", (PG,), "SELECT a, COUNT(*) FILTER (WHERE b > 1) FROM t GROUP BY a", ()),
    ("A-pg-limit-all", (PG,), "SELECT * FROM t LIMIT ALL", ()),
    # La consola solo mapea como lectura ``Select``/``Union``; el validador pide la segunda opinión
    # sobre la consulta envuelta, así que estas raíces se aceptan igual.
    ("A-intersect", ALL_ENGINES, "SELECT a FROM t INTERSECT SELECT a FROM u", ()),
    ("A-except", ALL_ENGINES, "SELECT a FROM t EXCEPT SELECT a FROM u", ()),
    ("A-parenthesized-root", ALL_ENGINES, "(SELECT a FROM t)", ()),
    (
        "A-union-order-limit",
        ALL_ENGINES,
        "SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY 1 LIMIT 5",
        (),
    ),
]

MUST_ACCEPT_CASES: list[Case] = _expand(_MUST_ACCEPT_ROWS)

# --------------------------------------------------------------------------- #
# Falsos positivos CONOCIDOS y deliberados                                     #
# --------------------------------------------------------------------------- #
# Se fijan en un test para que el costo esté escrito y no se "arregle" sin querer. Cada uno se
# rechaza por una razón de seguridad o de fidelidad del render, no por descuido.
_KNOWN_FALSE_POSITIVE_ROWS = [
    # sqlglot renderiza ``a DIV 2`` de MySQL como ``CAST(a / 2 AS SIGNED)``: otro árbol, y el round
    # trip (D4) rechaza lo que no se renderiza a sí mismo. Alternativa: ``FLOOR(a / 2)``.
    ("FP-div", _MYSQL_ONLY, "SELECT a DIV 2 FROM t", {"UNSUPPORTED_NODE"}),
    # El generador de MySQL escribe un salto de línea dentro de un literal como ``\n``: el texto
    # renderizado llevaría una barra invertida y significaría otra cosa bajo NO_BACKSLASH_ESCAPES.
    ("FP-newline-in-literal", _MYSQL_ONLY, "SELECT 'a\nb'", {"PARSE_FAILED"}),
]

KNOWN_FALSE_POSITIVE_CASES: list[Case] = _expand(_KNOWN_FALSE_POSITIVE_ROWS)
