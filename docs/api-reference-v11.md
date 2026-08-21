# API Reference v11 — Validación de migraciones y collation de referencia del blueprint

Addendum al contrato consolidado (`api-reference.md` §8). Dos cambios que atacan el mismo
problema: **hasta ahora, la única forma de saber si una migración era correcta era
aplicarla**, y cuando fallaba ya había BDs en cuarentena.

## 1. `POST /api/v1/database-models/{model_id}/migrations/validate`

Rate limit **20/minute**. 🔌 **solo** si se pasa `managed_database_id`.

### Petición

```jsonc
{
  "up_sql": "ALTER TABLE clientes ADD COLUMN alias VARCHAR(50) COLLATE utf8mb4_bin;",
  "version": null,                 // alternativa a up_sql: valida una versión ya guardada
  "managed_database_id": 5         // opcional: activa la verificación contra el catálogo
}
```

`up_sql` **o** `version` (si llegan las dos, manda `up_sql`: es lo que el usuario tiene
delante en el formulario). Sin ninguna de las dos → `422`.

### Respuesta

```jsonc
{
  "data": {
    "statements": [
      { "seq": 0, "sql": "…", "kind": "alter", "danger": "ddl", "reasons": [],
        "seeds": false, "destructive": false, "collations": ["utf8mb4_bin"],
        "parse_error": null }
    ],
    "has_seed": false,
    "forced_collations": ["utf8mb4_bin"],
    "forced_charsets": [],
    "destructive_statements": [],
    "parse_errors": [],
    "gateway_internal_tables": [],
    "postgresql_blockers": [],
    "resumable": true,
    "referenced_tables": ["clientes"],
    "checked_database": "app_prod",
    "missing_tables": [],
    "catalog_error": null,
    "blueprint_collation": "utf8mb4_unicode_ci",
    "collation_conflicts": ["utf8mb4_bin"]
  }
}
```

La forma de `statements[]` es deliberadamente la de `QueryStatementPlanOut` de la consola
SQL: el frontend ya sabe pintarla.

### Qué detecta, y qué no

| Detecta | Cómo |
|---|---|
| Errores de sintaxis, con línea y columna | `sqlglot.parse_one`. La consola SQL se traga la excepción porque le basta con fallar cerrado; para un validador **el mensaje es el producto**. |
| DDL que no se traduce con certeza a PostgreSQL | `SqlTranslator.translation_blockers`. Es el mismo `422` que hoy solo aparece al aplicar contra un destino PostgreSQL. |
| Siembra de datos | Clasificador AST **más** un fallback por regex: `LOAD DATA` y `COPY` caen en la blocklist (`danger=blocked`) y `REPLACE INTO` parsea como `Command` genérico, así que ninguna de las tres se marca como `write`. |
| `COLLATE` / `CHARACTER SET` forzados | Regex sobre el SQL **enmascarado** (`mask_quoted_spans`): sin eso, un `COMMENT 'usa COLLATE x'` daría un falso positivo. Como la máscara conserva la longitud, el nombre real se recorta del texto original en las mismas posiciones — así funciona también con `COLLATE "es_ES"`. |
| `DROP` / `TRUNCATE` / `DELETE` sin `WHERE` | `kind` del clasificador, más la ausencia de cláusula para el `DELETE`. |
| Referencias a la contabilidad interna (`_gw_*`) | `identifiers.references_gateway_internal_table`. |
| Si un fallo a mitad podrá auto-reconciliarse | `migration_progress.is_resumable`. |
| **Tablas referenciadas que no existen** | Solo con `managed_database_id`. |

**Lo que ningún análisis estático puede detectar** es justamente el error que motivó esto:
un `ALTER TABLE` sobre una tabla inexistente es **sintácticamente impecable**. Por eso
existe `managed_database_id`.

> **Un dry-run real (ejecutar y revertir) no es una alternativa.** En MySQL/MariaDB cada
> sentencia DDL hace COMMIT implícito, así que es estructuralmente imposible. Solo
> funcionaría en PostgreSQL, y quedaría medio feature.

### Verificación contra el catálogo (`managed_database_id`)

- **La BD debe pertenecer al blueprint** o la respuesta es `422`. Es una frontera de
  autorización: sin ella se podría sondear el catálogo de cualquier BD del gateway pasando
  su id a un blueprint que no la contiene.
- Una sola lectura del catálogo (`list_tables`), no una consulta por tabla.
- **La comparación es insensible a mayúsculas a propósito.** MySQL sobre Linux distingue
  nombres de tabla y PostgreSQL pliega a minúsculas salvo comillas: ser estricto produciría
  avisos falsos constantes, y un validador que grita en falso deja de leerse. Se pierde
  algún positivo raro; se evita el ruido que lo haría inútil.
- **Alcance: tablas.** Columnas y tipos quedan fuera.
- Si el motor no es alcanzable, `catalog_error` lo dice y **el análisis estático se
  devuelve igual**: perder también lo que sí se pudo comprobar sin conexión sería peor.
- Es 🔌: rate limit y `audit.record("migration.validate", …)`.

## 2. `charset` / `collation` en `DatabaseModel`

Dos columnas nulables nuevas en `database_models`, expuestas en `DatabaseModelCreate`,
`DatabaseModelUpdate` y `DatabaseModelOut`. Revisión Alembic `c7d8e9f0a1b2`.

Un blueprint **es** el esquema base que sus BDs replican, y el juego de caracteres forma
parte del esquema tanto como las columnas. Declararlo da un valor de referencia estable:
el validador puede avisar cuando una migración fuerza un `COLLATE` distinto
(`collation_conflicts`), y se pueden detectar BDs desviadas.

- **Nulables y sin `server_default`.** Los blueprints existentes no tienen un valor correcto
  que inventar, y rellenarlos con uno arbitrario generaría avisos falsos en masa. Mientras
  estén vacías, `collation_conflicts` viene vacío y la comparación queda a cargo del cliente
  contra el collation real de las BDs.
- **Semántica de la familia MySQL.** PostgreSQL usa `encoding` + `lc_collate`/`lc_ctype`,
  que no son equivalentes: contra destinos PostgreSQL **no se compara**. Modelarlo por motor
  sería lo completo, pero hoy nadie lo necesita.
- La comparación ignora mayúsculas (los nombres de collation no distinguen caja).

## 3. Cambios relacionados en §8 (ya en `api-reference.md`)

- `ModelMigrationSummary` y `ModelMigrationOut` ganan `sql_frozen`, `deletable` y
  `block_reason`.
- El `DELETE` de una versión ya no lo bloquea un intento **fallido**.
- `apply-all` acepta `on_failure` (la ruta no lo exponía, así que el controlador recibía
  siempre `"auto"` pese a estar documentado).


## 4. `GET /api/v1/database-models/{model_id}/databases` — estado de despliegue

**No es un endpoint nuevo**: el que ya existía gana tres campos. Un hermano
`/migrations/status` habría duplicado el listado y obligado al cliente a cruzar dos
respuestas para pintar una sola tabla.

| Campo | Significado |
|---|---|
| `pending_count` | Versiones del blueprint que a esta BD le faltan |
| `pending_versions` | Cuáles |
| `has_partial_application` | Quedaron sentencias a medias. Ojo: `model_version` **no** lo refleja — Alembic solo registra al TERMINAR el upgrade |

Antes, saber esto exigía una llamada por BD a `/managed-databases/{id}/migrations/status`, y
**cada una abre una conexión al motor**. Ahora la tabla entera cuesta **3 queries locales y
cero conexiones**: `managed_databases.model_version` es una copia que el gateway ya mantiene
tras cada apply, `compute_pending` es una función pura y el estado parcial vive en la BD del
gateway.

`?refresh=true` 🔌 relee la versión REAL de cada BD y resincroniza la copia. Es la vía para
corregir el dato si alguien migró una BD por fuera del gateway. Rate limit **10/min** y
auditoría. **Una BD inalcanzable no rompe la tabla**: se conserva su valor cacheado y se
sigue — fallar todo porque un servidor de doce esté caído haría inútil la pantalla justo
cuando más se necesita.

## 5. `database_ids` en `apply-all`

`POST .../migrations/apply-all?database_ids=5&database_ids=9` aplica **solo a esos destinos**.
Sin el parámetro, el comportamiento es el de siempre (todas, hasta `max_databases`).

Motivación: en desarrollo lo normal es probar una versión nueva contra una BD y solo después
ir a por el resto. Antes solo se podía acotar *cuántas* BDs, nunca *cuáles*.

**Un id que no pertenezca al blueprint devuelve `422` con la lista.** No es cosmética: `IN` lo
ignoraría en silencio, y esta es la frontera que impide aplicar las migraciones de un
blueprint a una BD ajena pasando su id.

Va por query y no por body porque el endpoint ya es 100 % query params; añadirle un cuerpo
habría roto la homogeneidad para consumidores existentes.

## 6. `captured_select_count` / `select_results_available` en `ApplyAllItemOut`

Paridad con `MigrationApplyOut`, que ya los tenía. Sin ellos, tras un apply masivo no había
forma de saber en qué BDs quedaron capturas ni de ofrecer un enlace para verlas.
