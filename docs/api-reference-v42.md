# API v42 — MCP: `database.engine_version` (versión limpia del motor)

Addendum de [v41](api-reference-v41.md). **No hay rutas REST nuevas**: cambia el contrato de las
tools MCP que leen el catálogo de una base. Por qué se expone solo la versión numérica, en
`docs/development/decisiones-e-incidentes.md`.

## El campo

El bloque `database` del sobre de las tools suma un campo:

```json
"database": { "database_id": 7, "engine": "mariadb", "engine_version": "11.8.3" }
```

| Campo | Tipo | Valores |
|---|---|---|
| `engine_version` | `string \| null` | `mayor.menor.parche` (`11.8.3`), `mayor.menor` si el motor no trae parche (`16.3`), o `null` |

El patrón es `^[0-9]+(\.[0-9]+){1,2}$`: el modelo de salida rechaza cualquier otro valor, así que una
cadena de build no puede salir ni por descuido de un mapeador.

## Qué tools lo llevan

Las que **ya abren la sesión de lectura** del motor y reusan ese façade (un `SELECT VERSION()` más
sobre la sesión existente, nunca una conexión extra):

`list_objects`, `get_schema`, `search_schema`, `check_freshness`, `get_table_stats`,
`get_definition` y `diff_schemas`.

- `diff_schemas` informa la versión del lado **origen** (`source_database_id`), que es el que
  describe el bloque `database` de su sobre.
- `get_definition` informa `null` cuando ningún objeto pedido está en el índice: esa llamada hace cero
  lecturas al motor y la versión no justifica una.
- `list_objects` lee `VERSION()` una sola vez y la comparte con la disponibilidad de cuerpos. Antes,
  sin el scope `data.definitions`, no la leía; ahora sí, solo para este campo.
- Las tools sin sesión de lectura (`list_databases`, `draft_query`, `list_environments`,
  `list_exports`, `list_clones`, `list_catalogs`) no lo llevan. `sample_rows`, `distinct_values`,
  `count_rows` y `run_select` usan la credencial de datos y su propio sobre: tampoco.

## Regla de parseo

De la cadena cruda de `VERSION()` se toman los primeros `mayor.menor[.parche]` y se descarta todo lo
demás. Función única: `readonly_probe.public_engine_version`.

| Cadena cruda | `engine_version` |
|---|---|
| `11.8.3-MariaDB-0+deb13u1 from Debian` | `11.8.3` |
| `5.5.5-10.11.6-MariaDB` | `10.11.6` (el prefijo `5.5.5-` es de compatibilidad de replicación de MariaDB: la versión real va después) |
| `10.6.12-MariaDB-1:10.6.12+maria~ubu2004` | `10.6.12` |
| `8.0.36-0ubuntu0.22.04.1` | `8.0.36` |
| `5.7.44-log` | `5.7.44` |
| `16.3 (Debian 16.3-1.pgdg120+1)` | `16.3` |
| vacío, `null` o ilegible | `null` |

## Por qué solo dígitos

La cadena de build (`-0+deb13u1 from Debian`, `~ubu2004`, `pgdg120+1`) revela la distribución y el
nivel de parche del **paquete**: justo lo que un escáner de vulnerabilidades necesita para elegir qué
probar. Un agente necesita saber si el motor es 11.8 o 10.6 para razonar sobre sintaxis y
funciones, no cómo se compiló. Por eso la limpieza es por construcción (se toman dígitos, no se
quitan sufijos conocidos) y el patrón del modelo es la segunda barrera.

Sin cambios de autorización ni de capacidades.
