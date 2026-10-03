# API v34 — MCP: la tool `draft_query` (clasificar SQL de un agente sin ejecutarlo)

Addendum de [v23 §9](api-reference-v23.md) (transporte y tools del MCP). Agrega una tool que
**recibe un texto SQL y devuelve solo texto**. No cambia ninguna ruta REST ni el frontend, no
agrega variables de entorno ni migraciones, y no abre ninguna conexión a un motor.

## Resumen

| Cambio | Dónde | Scope |
|---|---|---|
| Tool nueva `draft_query` | `tools/list` / `tools/call` | `databases.read` |
| Códigos públicos de razón y de advertencia del sobre | respuesta | — |
| Invariante de v23 §9.5: "no acepta SQL" pasa a "no **ejecuta** SQL" | documentación | — |

Aditivo. Un token que ya tiene `databases.read` pasa a ver la tool nueva sin reemitirse.

## Entrada

```jsonc
{ "database_id": 12,   // el que devuelve list_databases
  "sql": "UPDATE t SET a = 1" } // cualquier texto; el schema no fija un máximo a propósito
```

Schema cerrado: cualquier otra clave se rechaza (error de protocolo). Un `database_id` que no sea
entero o un `sql` que no sea cadena → error de tool `MALFORMED_REQUEST`. **Todo texto** —vacío,
ilegible, enorme— devuelve un sobre; un error de protocolo por un SQL mal escrito invitaría a
reintentar en loop.

## Salida (`structuredContent`)

Exactamente estas cinco claves, siempre:

```jsonc
{ "classification": "write",
  "reasons": ["NOT_SELECT"],
  "warnings": ["WRITE_NOT_EXECUTED"],
  "query_text": "UPDATE t SET a = 1",
  "touches_engine": false }
```

| Campo | Significado |
|---|---|
| `classification` | `read`, `write`, `ddl`, `blocked` o `invalid` (conjunto cerrado) |
| `reasons[]` | Códigos públicos cerrados que explican por qué NO es una lectura aceptable. Vacío si `read` |
| `warnings[]` | `WRITE_NOT_EXECUTED` (escritura), `DDL_NOT_EXECUTED` (DDL). Una escritura o un DDL **siempre** los traen |
| `query_text` | El texto canónico si es una lectura aceptada; si no, el texto recibido sin caracteres de control y recortado a 16 KiB |
| `touches_engine` | **Siempre `false`**: redactar nunca ejecuta, ni siquiera una lectura |

`classification`: `read` es una lectura que el validador acepta; `write` y `ddl` salen de la
política de la consola; `blocked` es lo que la consola prohíbe incluso confirmando **o** una
lectura que el perfil de agente rechaza (con sus `reasons`); `invalid` es un texto vacío, ilegible,
enorme o con varias sentencias.

### Códigos de `reasons`

`PARSE_FAILED`, `MULTIPLE_STATEMENTS`, `NOT_SELECT`, `DML_IN_CTE`, `DML_IN_SUBQUERY`,
`SELECT_INTO`, `LOCKING_READ`, `FUNCTION_NOT_ALLOWED`, `VARIABLE_ASSIGNMENT`,
`EXECUTABLE_COMMENT`, `COMMENT_NOT_ALLOWED`, `SYSTEM_SCHEMA`, `CROSS_DATABASE`, `UNSUPPORTED_NODE`,
`LIMIT_NOT_BOUNDABLE`, `OFFSET_TOO_HIGH`, `SQL_TOO_LARGE`.

El vocabulario público completo (que incluye códigos de capas posteriores, como `DATA_DISABLED` o
`QUERY_TIMEOUT`) vive en `app/services/mcp_catalog.py`; los códigos internos del validador nunca
salen.

Lo que sqlglot 30.11 no parsea se rechaza como `PARSE_FAILED`, con el código más específico
sumado cuando se lee de los tokens: `SELECT … INTO OUTFILE` (`SELECT_INTO`) y un DML dentro de un
subselect (`DML_IN_SUBQUERY`).

## Qué decide el validador

La decisión sale del **AST** (nunca de palabras sueltas): una lista blanca de tipos de nodo, de
argumentos de cada nodo y de funciones, sobre el árbol completo. Un texto se acepta solo si:

- es exactamente **una** sentencia (`SELECT`, `UNION`/`INTERSECT`/`EXCEPT`, con `WITH` opcional);
- no tiene **ningún comentario** (los `/*!…*/` y `/*M!…*/`, que el motor ejecuta, se rechazan sobre
  el texto crudo, incluso dentro de un literal);
- todas sus funciones están en la lista blanca (cerrada; las desconocidas y las calificadas se
  rechazan);
- no asigna ni lee variables, no toma locks (`FOR UPDATE`/`FOR SHARE`/`LOCK IN SHARE MODE`) ni
  materializa el resultado (`INTO`);
- solo nombra la base fijada por `database_id` (otra base, un nombre de tres partes en PostgreSQL y
  los esquemas del sistema se rechazan);
- su `LIMIT`/`OFFSET` son literales enteros (un `OFFSET` mayor a 10 000 se rechaza).

El render canónico del árbol vuelve a pasar por el pipeline completo y tiene que dar el mismo
conjunto de nodos; si no, se rechaza (`UNSUPPORTED_NODE`).

**Falsos positivos deliberados:** en MySQL/MariaDB un literal con barra invertida o con salto de
línea se rechaza (`PARSE_FAILED`) porque el mismo texto significaría otra cosa bajo
`NO_BACKSLASH_ESCAPES`; `a DIV 2` se rechaza (`UNSUPPORTED_NODE`) porque sqlglot lo renderiza como
otro árbol. Un `LIMIT ALL` de PostgreSQL se acepta (el parser no deja rastro y la consulta se acota
igual con un `LIMIT` empujado por el gateway).

## Lo que el validador NO detecta

El parseo no ve: vistas con `DEFINER`, tablas `FEDERATED`/`CONNECT`/`SPIDER` (y extensiones tipo
`dblink`) que leen otras bases, ni las diferencias entre cómo sqlglot y el motor leen el mismo
texto. **La barrera real es la cuenta del motor**: `SELECT` sobre una sola base, transacción
`READ ONLY` y timeout del lado del servidor. El validador es defensa en profundidad, y esta tool
no ejecuta nada, así que hoy esos residuales no tienen un camino al motor.

## Errores

El gate de la base es el de las demás tools: una base que el token no alcanza responde
`mcp.not_found`; sin scope, `mcp.scope_denied`; sin credencial de solo lectura vigente en el
servidor, `mcp.readonly_credential_missing` (aunque `draft_query` no la use para conectar).
