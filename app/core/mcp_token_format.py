"""
Prefijos del bearer de agente, en un módulo sin dependencias.

Viven acá y no en ``mcp_auth`` porque ``mcp_auth`` importa ``limiter`` (``hit_or_429``) y el
``key_func`` del limitador también necesita saber qué prefijos son válidos: definirlos en
``mcp_auth`` obligaría a ``limiter`` a importarlo de vuelta y cerraría un ciclo. Un módulo hoja
rompe el ciclo y deja una única fuente de verdad para los dos.
"""

#: Prefijo con el que se EMITEN los tokens nuevos: ``datum.<token_id>.<secreto>``.
TOKEN_PREFIX = "datum"

#: Prefijo de los tokens emitidos antes del cambio de nombre del producto. Se sigue aceptando:
#: los agentes ya configurados con ``dbgw.<id>.<secreto>`` no deben romperse.
LEGACY_TOKEN_PREFIX = "dbgw"

#: Prefijos que el PARSEO acepta. Todo prefijo que no esté acá es un bearer malformado.
ACCEPTED_TOKEN_PREFIXES = (TOKEN_PREFIX, LEGACY_TOKEN_PREFIX)
