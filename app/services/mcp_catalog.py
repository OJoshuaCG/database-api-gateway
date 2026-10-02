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
