"""
Vocabulario CERRADO de los códigos del MCP.

Existe como catálogo y no como literales sueltos por el mismo motivo que los demás catálogos del
repo: los códigos los consume un agente automático, así que un typo no lo nota nadie hasta que
alguien depura por qué su cliente no reacciona.

EL ORDEN DE LOS EJES DEL GATE ES AUTORIZACIÓN PRIMERO, POLÍTICA DESPUÉS
-----------------------------------------------------------------------
Y **no** de más barato a más caro. Los ocho ejes son consultas locales sobre la BD de metadatos y
la diferencia de costo es ruido; en cambio un orden por costo convierte los códigos de los ejes de
POLÍTICA en un **oráculo de inventario**: un `database_id` de otro proyecto recibiría
``environment_unassigned`` o ``environment_denies_agents``, y con eso el agente aprende que la
base existe y en qué estado está.

Por eso "no pertenece a tu proyecto" emite ``not_found`` y se evalúa **antes** que cualquier eje
de política.
"""

#: --- Autorización (se evalúan primero) ------------------------------------ #
CODE_SCOPE_DENIED = "mcp.scope_denied"
#: El mismo código para "no existe" y "no es de tu proyecto". Es deliberado: distinguirlos le
#: dice al agente que la base existe, que es exactamente lo que el alcance por proyecto oculta.
CODE_NOT_FOUND = "mcp.not_found"
CODE_READONLY_MISSING = "mcp.readonly_credential_missing"

#: --- Política (se evalúan después) ---------------------------------------- #
CODE_ENV_UNASSIGNED = "mcp.environment_unassigned"
CODE_ENV_DENIES = "mcp.environment_denies_agents"
CODE_NOT_OPTED_IN = "mcp.database_not_opted_in"
CODE_BLOCKED = "mcp.database_blocked"

#: Tope de objetos superado. Se corta con un ERROR y nunca truncando: una lista cortada le hace
#: creer al agente que no hay más, que es peor que un fallo.
CODE_TOO_MANY_OBJECTS = "mcp.too_many_objects"

#: La sesión de lectura superó ``MCP_SESSION_MAX_SECONDS``. Se corta y se reporta: una sesión
#: larga retiene undo (MySQL) o frena el VACUUM (PG) en la base de un tercero.
CODE_SESSION_TIMEOUT = "mcp.session_timeout"

#: Un argumento con forma válida pero valor inaceptable (un ``kind`` desconocido, una lista
#: vacía, dos veces la misma base en un diff). Error de TOOL y no de protocolo: el agente puede
#: corregirlo, que es lo que el código le dice.
CODE_INVALID_ARGUMENT = "mcp.invalid_argument"

#: --- Warnings (viajan en la respuesta, nunca solo en el log) --------------- #
#: La base está en cuarentena (``status == error``): su esquema no corresponde a ninguna versión
#: declarada. No se deniega —esconderlo justo cuando un humano diagnostica es peor— pero se avisa.
WARN_DATABASE_QUARANTINED = "mcp.warn.database_quarantined"
#: PostgreSQL: el gateway introspecciona solo el schema ``public``.
WARN_PG_PUBLIC_SCHEMA_ONLY = "mcp.warn.pg_public_schema_only"
#: MySQL/MariaDB: el catálogo no participa del snapshot MVCC; un ``ALTER`` concurrente se ve.
WARN_MYSQL_STRUCTURE_NOT_ATOMIC = "mcp.warn.mysql_structure_not_atomic"
#: El motor rechazó una directiva de sesión (``session.degradations``).
WARN_SESSION_DIRECTIVE_REJECTED = "mcp.warn.session_directive_rejected"
#: ``kinds`` o ``name_prefix`` dejaron afuera objetos del índice.
WARN_OBJECTS_OMITTED_BY_FILTER = "mcp.warn.objects_omitted_by_filter"
#: Los cuerpos de vistas, rutinas y triggers no se entregan en la v1 (scope ``inspect:bodies``
#: apagado por diseño, plan 12 §4).
WARN_BODIES_UNAVAILABLE = "mcp.warn.bodies_unavailable"

#: ``search_schema`` no pudo leer todo lo que debía: el tope de tablas escaneadas por llamada
#: (``MCP_SEARCH_MAX_TABLES``) o el presupuesto de tiempo lo cortaron. Los resultados son válidos
#: pero PARCIALES para columnas y comentarios de las tablas no escaneadas.
WARN_SEARCH_SCAN_TRUNCATED = "mcp.warn.search_scan_truncated"
#: ``search_schema`` encontró más coincidencias que ``limit``: se devuelven las mejor rankeadas.
WARN_SEARCH_RESULTS_TRUNCATED = "mcp.warn.search_results_truncated"

#: --- Referencias ---------------------------------------------------------- #
#: v1 NO acepta referencia cruda (`server_id` + nombre) y se rechaza ANTES de cualquier lookup.
#: La referencia cruda existe para flujos de adopción y legado de la SPA; para un agente es puro
#: downside, porque es la forma que se escapa del inventario. Negarla de plano es más simple que
#: confiar en que el fail-closed del entorno la neutralice.
CODE_REFERENCE_NOT_SUPPORTED = "mcp.reference_not_supported"

#: Mensajes de los ejes de política, para que el operador que lea el error del agente sepa qué
#: palanca le falta. Los de autorización NO tienen mensaje propio: comparten uno genérico.
POLICY_HINTS = {
    CODE_ENV_UNASSIGNED: (
        "La base no tiene entorno asignado. Un agente nunca alcanza una base sin clasificar."
    ),
    CODE_ENV_DENIES: (
        "El entorno de esta base no permite agentes (allows_agent_access está en false)."
    ),
    CODE_NOT_OPTED_IN: (
        "La base no tiene el opt-in de agentes (agent_access_allowed está en false). "
        "Cada base se habilita explícitamente."
    ),
    CODE_BLOCKED: (
        "La base tiene el veto de agentes activo (agent_access_blocked). No hay override."
    ),
}

# --------------------------------------------------------------------------- #
# SQL de agente: códigos PÚBLICOS de razón y de advertencia                     #
# --------------------------------------------------------------------------- #
#
# Vocabulario CERRADO de ``reasons[]`` y ``warnings[]`` de ``draft_query`` (y de las tools que
# aceptan SQL de un agente). Son strings en MAYÚSCULAS y no ``mcp.*`` porque son el contrato del
# SOBRE de respuesta (``{classification, reasons, warnings, …}``) y no códigos de error de tool.
#
# NUNCA salen nombres internos: el validador razona con códigos propios (``agent_sql.*``) y los
# traduce con ``public_reason`` justo antes de responder. Así un refactor del validador no cambia
# el contrato que consume el agente, y un código interno nuevo sin traducción falla en un test
# (``INTERNAL_TO_PUBLIC`` es total) en vez de filtrarse al agente.

REASON_PARSE_FAILED = "PARSE_FAILED"
REASON_MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
REASON_NOT_SELECT = "NOT_SELECT"
REASON_DML_IN_CTE = "DML_IN_CTE"
REASON_DML_IN_SUBQUERY = "DML_IN_SUBQUERY"
REASON_SELECT_INTO = "SELECT_INTO"
REASON_LOCKING_READ = "LOCKING_READ"
REASON_FUNCTION_NOT_ALLOWED = "FUNCTION_NOT_ALLOWED"
REASON_VARIABLE_ASSIGNMENT = "VARIABLE_ASSIGNMENT"
REASON_EXECUTABLE_COMMENT = "EXECUTABLE_COMMENT"
REASON_COMMENT_NOT_ALLOWED = "COMMENT_NOT_ALLOWED"
REASON_SYSTEM_SCHEMA = "SYSTEM_SCHEMA"
REASON_CROSS_DATABASE = "CROSS_DATABASE"
REASON_UNSUPPORTED_NODE = "UNSUPPORTED_NODE"
REASON_LIMIT_TOO_HIGH = "LIMIT_TOO_HIGH"
REASON_OFFSET_TOO_HIGH = "OFFSET_TOO_HIGH"
REASON_UNKNOWN_IDENTIFIER = "UNKNOWN_IDENTIFIER"
REASON_DATA_DISABLED = "DATA_DISABLED"
REASON_PROBE_NOT_GREEN = "PROBE_NOT_GREEN"
#: Reservado: no hay fuente de verdad de PII en el modelo; el límite real es el GRANT del motor.
REASON_PII_BLOCKED = "PII_BLOCKED"
REASON_QUERY_TIMEOUT = "QUERY_TIMEOUT"
REASON_AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
REASON_CREDENTIAL_TOO_BROAD = "CREDENTIAL_TOO_BROAD"
REASON_WRITE_PRIVILEGE_PRESENT = "WRITE_PRIVILEGE_PRESENT"
REASON_FEDERATED_TABLE_PRESENT = "FEDERATED_TABLE_PRESENT"
#: Adiciones del diseño (aditivas sobre el vocabulario de la spec).
REASON_SQL_TOO_LARGE = "SQL_TOO_LARGE"
REASON_LIMIT_NOT_BOUNDABLE = "LIMIT_NOT_BOUNDABLE"
REASON_QUERY_FAILED = "QUERY_FAILED"
REASON_MALFORMED_REQUEST = "MALFORMED_REQUEST"

REASON_CODES = frozenset(
    {
        REASON_PARSE_FAILED,
        REASON_MULTIPLE_STATEMENTS,
        REASON_NOT_SELECT,
        REASON_DML_IN_CTE,
        REASON_DML_IN_SUBQUERY,
        REASON_SELECT_INTO,
        REASON_LOCKING_READ,
        REASON_FUNCTION_NOT_ALLOWED,
        REASON_VARIABLE_ASSIGNMENT,
        REASON_EXECUTABLE_COMMENT,
        REASON_COMMENT_NOT_ALLOWED,
        REASON_SYSTEM_SCHEMA,
        REASON_CROSS_DATABASE,
        REASON_UNSUPPORTED_NODE,
        REASON_LIMIT_TOO_HIGH,
        REASON_OFFSET_TOO_HIGH,
        REASON_UNKNOWN_IDENTIFIER,
        REASON_DATA_DISABLED,
        REASON_PROBE_NOT_GREEN,
        REASON_PII_BLOCKED,
        REASON_QUERY_TIMEOUT,
        REASON_AUDIT_UNAVAILABLE,
        REASON_CREDENTIAL_TOO_BROAD,
        REASON_WRITE_PRIVILEGE_PRESENT,
        REASON_FEDERATED_TABLE_PRESENT,
        REASON_SQL_TOO_LARGE,
        REASON_LIMIT_NOT_BOUNDABLE,
        REASON_QUERY_FAILED,
        REASON_MALFORMED_REQUEST,
    }
)

#: Una sentencia de escritura o DDL nunca se ejecuta por el MCP: estas advertencias viajan SIEMPRE
#: con ella para que el agente no crea que "borrar" ocurrió. ``LIMIT_TOO_HIGH`` es también
#: advertencia (un ``limit`` pedido por encima del tope se recorta, no se rechaza).
WARN_WRITE_NOT_EXECUTED = "WRITE_NOT_EXECUTED"
WARN_DDL_NOT_EXECUTED = "DDL_NOT_EXECUTED"
WARN_LIMIT_TOO_HIGH = REASON_LIMIT_TOO_HIGH

WARNING_CODES = frozenset({WARN_WRITE_NOT_EXECUTED, WARN_DDL_NOT_EXECUTED, WARN_LIMIT_TOO_HIGH})

#: Traducción interno -> público (tabla del diseño). Total sobre los códigos internos del
#: validador (``agent_sql_policy.INTERNAL_CODES``) y de las capas de datos que se suman después.
INTERNAL_TO_PUBLIC: dict[str, str] = {
    # Léxico / parseo
    "agent_sql.unparseable": REASON_PARSE_FAILED,
    "agent_sql.tokenizer_error": REASON_PARSE_FAILED,
    "agent_sql.ambiguous_literal": REASON_PARSE_FAILED,
    "agent_sql.backslash_in_literal": REASON_PARSE_FAILED,
    "agent_sql.too_large": REASON_SQL_TOO_LARGE,
    "agent_sql.multiple_statements": REASON_MULTIPLE_STATEMENTS,
    # Forma de la sentencia
    "agent_sql.not_select": REASON_NOT_SELECT,
    "agent_sql.classify_not_read": REASON_NOT_SELECT,
    "agent_sql.dml_in_cte": REASON_DML_IN_CTE,
    "agent_sql.dml_in_subquery": REASON_DML_IN_SUBQUERY,
    "agent_sql.select_into": REASON_SELECT_INTO,
    "agent_sql.locking_read": REASON_LOCKING_READ,
    "agent_sql.function_not_allowed": REASON_FUNCTION_NOT_ALLOWED,
    "agent_sql.variable_assignment": REASON_VARIABLE_ASSIGNMENT,
    "agent_sql.executable_comment": REASON_EXECUTABLE_COMMENT,
    "agent_sql.comment": REASON_COMMENT_NOT_ALLOWED,
    # Identificadores. La contabilidad interna del gateway (``_gw_v_*``) se reporta como esquema
    # de sistema: es la misma clase de objeto (no es esquema del usuario) y evita un código nuevo.
    "agent_sql.system_schema": REASON_SYSTEM_SCHEMA,
    "agent_sql.gateway_internal_table": REASON_SYSTEM_SCHEMA,
    "agent_sql.cross_database": REASON_CROSS_DATABASE,
    "agent_sql.unsupported_construct": REASON_UNSUPPORTED_NODE,
    "agent_sql.render_mismatch": REASON_UNSUPPORTED_NODE,
    "agent_sql.too_complex": REASON_UNSUPPORTED_NODE,
    # Cota de filas
    "agent_sql.limit_not_boundable": REASON_LIMIT_NOT_BOUNDABLE,
    "agent_sql.offset_too_high": REASON_OFFSET_TOO_HIGH,
    # Capas de datos (slices 2-6): se declaran acá para que la tabla sea una sola.
    "mcp.data_disabled": REASON_DATA_DISABLED,
    "mcp.data_not_opted_in": REASON_DATA_DISABLED,
    "mcp.data_credential_missing": REASON_DATA_DISABLED,
    "mcp.data_probe_stale": REASON_PROBE_NOT_GREEN,
    "mcp.data_probe_failed": REASON_PROBE_NOT_GREEN,
    "mcp.query_timeout": REASON_QUERY_TIMEOUT,
    "mcp.audit_unavailable": REASON_AUDIT_UNAVAILABLE,
    "mcp.query_failed": REASON_QUERY_FAILED,
    "mcp.policy_miss": REASON_QUERY_FAILED,
    "mcp.query_rejected": REASON_MALFORMED_REQUEST,
}


def public_reason(internal: str) -> str:
    """
    El código público de un código interno. **Fail-closed**: un código sin traducción sale como
    ``UNSUPPORTED_NODE`` (rechazo) y nunca como el nombre interno. El test de totalidad existe
    para que esa rama no se ejecute jamás.
    """
    return INTERNAL_TO_PUBLIC.get(internal, REASON_UNSUPPORTED_NODE)
