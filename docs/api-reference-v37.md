# API v37 — Tools de datos del MCP: `sample_rows`, `distinct_values`, `count_rows`

Addendum de [v36](api-reference-v36.md). Entrega 5 de `mcp-readonly-query-execution`. **No hay rutas
HTTP nuevas**: son tools del endpoint `/mcp/`. Existen solo con `MCP_DATA_READ_ENABLED=true`; scope
`data.read`. Contrato para el frontend: ninguno (la SPA ya gestiona scopes y opt-in en v36).

## Tools

| Tool | Argumentos | Sentencia que arma el gateway |
|---|---|---|
| `sample_rows` | `database_id`, `table`, `columns?`, `limit?` | `SELECT <cols|*> FROM <t> LIMIT n+1` |
| `distinct_values` | `database_id`, `table`, `column`, `limit?` | `SELECT DISTINCT <c> FROM <t> ORDER BY <c> LIMIT n+1` |
| `count_rows` | `database_id`, `table` | `SELECT COUNT(*) FROM <t> LIMIT 2` |

Identificadores cuoteados, validados contra el catálogo y pasados por `validate_agent_select`.

## Respuesta

`{notice, data:{columns, rows[arreglos]}, row_count, truncated, truncation_reason (row_cap|byte_budget|null),
clipped_cells, executed_sql, human_query, duration_ms, warnings, untrusted_fields, untrusted_content,
source, database}`. `data.columns` y `data.rows` son contenido no confiable de terceros.

## Topes y códigos

Filas 100/200/500; timeout 20 s/30 s; respuesta 128 KiB (recorta filas); celda 512. Códigos de error
cerrados: `DATA_DISABLED`, `PROBE_NOT_GREEN`, `UNKNOWN_IDENTIFIER`, `MALFORMED_REQUEST`,
`QUERY_TIMEOUT`, `QUERY_FAILED`, `AUDIT_UNAVAILABLE`; más los de autorización (`mcp.scope_denied`,
`mcp.not_found`, `mcp.environment_denies_agents`…). Advertencia: `LIMIT_TOO_HIGH`.

Variables nuevas: `MCP_QUERY_DEFAULT_ROWS`, `MCP_QUERY_MAX_ROWS`, `MCP_QUERY_TIMEOUT_MS`,
`MCP_QUERY_MAX_OFFSET`, `MCP_QUERY_MAX_SQL_BYTES`, `MCP_DATA_MAX_RESULT_BYTES`.
