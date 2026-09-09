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
