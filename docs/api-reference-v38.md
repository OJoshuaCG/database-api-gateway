# API v38 — Tool `run_select` del MCP: SQL de solo lectura del agente

Addendum de [v37](api-reference-v37.md). Entrega 6 (última) de `mcp-readonly-query-execution`. **No hay
rutas HTTP nuevas**: es una tool del endpoint `/mcp/`. Existe solo con `MCP_DATA_QUERY_ENABLED=true`
(apagado por default; independiente de `MCP_DATA_READ_ENABLED`); scope `data.query`. Contrato para el
frontend: ninguno (scopes y opt-in ya están en v36).

## Invariante (reemplaza al de v23 §9.5)

**El MCP ejecuta únicamente `SELECT` únicos validados, dentro de una transacción `READ ONLY` y bajo una
credencial por base con `SELECT` solamente.** Antes: "no acepta SQL del agente" (hasta v33) y "no EJECUTA
SQL del agente" (v34). Lo que no es un `SELECT` aceptable no llega al motor.

## Tool

| Tool | Argumentos | Qué hace |
|---|---|---|
| `run_select` | `database_id`, `sql`, `limit?` | Valida el `sql` con el validador compartido; si es una lectura aceptada la ejecuta con la credencial de datos de la base; si no, devuelve el sobre del borrador |

Mismo gate que v37 (kill switch → scope → base y entorno → opt-in → credencial con sonda fresca), mismo
servicio y mismos topes (filas 100/200/500, timeout 20 s/30 s, respuesta 128 KiB, celda 512). `limit`
solo puede igualar o bajar el máximo; por encima se recorta y `warnings` trae `LIMIT_TOO_HIGH`.

## Respuestas

- **Lectura aceptada**: el sobre de v37 (`data.rows` como arreglos, `untrusted_fields`, `truncated`,
  `human_query`, `executed_sql`, …). `executed_sql` es el render de `sqlglot` del árbol verificado, no el
  texto del agente: un `LIMIT` propio literal `<=` tope se ejecuta tal cual; uno mayor o ausente se
  reemplaza por tope + 1.
- **Todo lo demás** (escritura, DDL, bloqueado, ilegible): el sobre del borrador, **sin filas, sin
  conexión y sin intención de auditoría de ejecución**:
  `{classification: read|write|ddl|blocked|invalid, reasons[], warnings[], query_text, touches_engine: false}`.
  Un rechazo no es un error de protocolo. Códigos de `reasons`: `PARSE_FAILED`, `MULTIPLE_STATEMENTS`,
  `NOT_SELECT`, `DML_IN_CTE`, `DML_IN_SUBQUERY`, `SELECT_INTO`, `LOCKING_READ`, `FUNCTION_NOT_ALLOWED`,
  `VARIABLE_ASSIGNMENT`, `EXECUTABLE_COMMENT`, `COMMENT_NOT_ALLOWED`, `SYSTEM_SCHEMA`, `CROSS_DATABASE`,
  `UNSUPPORTED_NODE`, `OFFSET_TOO_HIGH` (literal `> MCP_QUERY_MAX_OFFSET`), `LIMIT_NOT_BOUNDABLE`,
  `SQL_TOO_LARGE`. Advertencias: `WRITE_NOT_EXECUTED`, `DDL_NOT_EXECUTED`.
- **Errores de tool** (códigos cerrados, mensaje fijo, nunca texto del motor): `DATA_DISABLED`,
  `PROBE_NOT_GREEN`, `QUERY_TIMEOUT`, `QUERY_FAILED`, `AUDIT_UNAVAILABLE`, `DATA_ACCOUNT_BUSY`
  (cuenta de datos en su tope de conexiones, 429), `MALFORMED_REQUEST` (solo
  argumentos ausentes o mal tipados) y los de autorización (`mcp.scope_denied`, `mcp.not_found`,
  `mcp.environment_denies_agents`…). El gate corre ANTES del validador: con el gate cerrado ni siquiera
  se clasifica el texto.

La fila de auditoría del despacho (`mcp.run_select`) marca `touched_engine=false` cuando la respuesta fue
un borrador (`touches_engine: false`), y `true` cuando hubo ejecución; la ejecución se audita aparte
(`mcp.agent_query`, intención antes de conectar).

## Riesgo residual aceptado

La barrera real es la cuenta del motor (`SELECT` sobre una sola base, transacción `READ ONLY`, timeout
del lado del servidor); el validador es defensa en profundidad. Quedan: (1) **inyección de prompt por los
datos de las filas** (mitigada por el sobre, contenida porque ninguna tool escribe); (2) lo que el parseo
no ve — vistas con `DEFINER`, tablas `FEDERATED`/`CONNECT`/`SPIDER`/FDW y diferenciales entre `sqlglot` y
el motor; (3) **los datos personales no se filtran**: la lista de denegación por PII quedó diferida
(enmienda S14; `PII_BLOCKED` reservado), así que lo legible lo fija el `GRANT`. Costo deliberado: en
MySQL/MariaDB un literal con barra invertida se rechaza (`PARSE_FAILED`).

Variable nueva: `MCP_DATA_QUERY_ENABLED` (ya listada en v37 junto a los topes).
