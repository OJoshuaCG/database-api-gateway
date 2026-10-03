# API v33 — MCP: la tool `search_schema` (buscar estructura sin conocer el nombre)

Addendum de [v23 §9](api-reference-v23.md) (transporte y tools del MCP). Agrega una tool de solo
lectura y publica `annotations` en `tools/list`. No cambia ninguna ruta REST ni el frontend.

## Resumen

| Cambio | Dónde | Scope |
|---|---|---|
| Tool nueva `search_schema` | `tools/list` / `tools/call` | `databases.read` |
| `annotations` en cada tool de `tools/list` | `tools/list` | — |
| Variable `MCP_SEARCH_MAX_TABLES` (default 200) | entorno del proceso | — |
| Warnings `mcp.warn.search_scan_truncated`, `mcp.warn.search_results_truncated` | respuesta | — |

Aditivo. Un token que ya tiene `databases.read` pasa a ver la tool nueva sin reemitirse.

## Entrada

```jsonc
{ "database_id": 12,          // el que devuelve list_databases
  "query": "fecha nacimiento", // 2..100 caracteres tras recortar espacios
  "kinds": ["table", "view", "column"], // opcional; default esos tres; también routine, trigger
  "limit": 20 }                // opcional; 1..50, default 20
```

Schema cerrado: cualquier otra clave se rechaza. `query` vacía, solo espacios, sin letras ni
dígitos, fuera de rango, o un `kinds`/`limit` inválido → error de tool `mcp.invalid_argument`,
antes de abrir ninguna conexión.

## Salida (`structuredContent`)

Mismo envelope que `get_schema` (`notice`, `untrusted_fields`, `clipped_fields`, `warnings`,
`database`). `data`:

| Campo | Significado |
|---|---|
| `query_tokens` | La consulta ya normalizada (sin acentos, sin palabras vacías) |
| `hits[]` | Resultados ordenados por `score` descendente, desempate estable |
| `count` / `total_matches` | Devueltos / coincidencias totales (puede haber más que `limit`) |
| `truncated` | `true` si CUALQUIER motivo de `truncated_reasons` recortó la búsqueda |
| `truncated_reasons` | `results_limit`, `scan_cap` (más tablas que `MCP_SEARCH_MAX_TABLES`), `time_budget` |
| `scanned_tables` / `total_tables` | Tablas cuyo detalle se leyó / tablas de la base |
| `searched_kinds` | Los tipos efectivamente buscados |
| `next_step` | Cómo seguir con `get_schema` |

Cada elemento de `hits[]`: `kind`, `name`, `table`, `column`, `data_type`, `key_flags[]`
(`primary_key` / `foreign_key` / `unique`), `references` (`tabla.columna` de una FK), `comment`
(texto de terceros, máx. 200 caracteres), `score`, `matched_on` (`name`, `name_and_table`,
`comment`, `name_and_comment`), `matched_tokens[]` y `get_schema_object` (`{kind, name}` para
`get_schema.objects`). Los nombres de tablas y vistas se buscan siempre; las columnas y los
comentarios de tabla solo en las tablas leídas (ver `scanned_tables`).

## Errores

Idénticos a `get_schema` (mismo gate de ocho ejes, misma credencial de solo lectura): una base que
el token no alcanza responde `mcp.not_found`, igual que una inexistente; sin credencial vigente,
`mcp.readonly_credential_missing`; sesión agotada, `mcp.session_timeout`.

## Privilegios

**No hacen falta privilegios nuevos.** La credencial de solo lectura ya tiene `SELECT`,
`SHOW VIEW`, `TRIGGER` y `EVENT` por base: alcanza para leer nombres, columnas y comentarios del
catálogo, que es todo lo que la tool consulta. Ver
[`docs/features/mcp-para-colaboradores.md`](features/mcp-para-colaboradores.md) (A.6).
