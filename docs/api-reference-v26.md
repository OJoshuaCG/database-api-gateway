# Búsqueda de texto en el SQL de las versiones de un blueprint

Addendum posterior a `api-reference-v25.md`. Cubre un endpoint nuevo, de solo lectura, para
encontrar una versión concreta de un blueprint buscando texto dentro de su `up_sql`, sin abrir
las versiones una por una.

**No repite v23 ni v25.** Para el envelope de autorización, el CSRF y el catálogo de
capacidades, la fuente sigue siendo v23. **No cambia ningún contrato existente**: todo lo de
este documento es aditivo.

---

## 1. Endpoint

```http
GET /api/v1/database-models/{model_id}/migrations/search
```

- **Capacidad**: `blueprints.read` (la misma que el listado de versiones).
- **Solo lee la BD del gateway**: no abre conexión a ningún motor, así que no tiene rate limit
  propio ni depende de que los servidores destino estén arriba.
- **Paginado** con el envelope estándar (`?page=&size=`, `pagination` al lado de `data`).

### Query params

| Param | Tipo | Default | Regla |
|---|---|---|---|
| `q` | string | — (obligatorio) | Máx. 200. Se busca **sin los espacios de los extremos** y tiene que quedar con **≥ 4 caracteres**. Literal: `%` y `_` no son comodines |
| `case_sensitive` | bool | `false` | `true` distingue mayúsculas |
| `last` | int \| omitido | todas | `1..1000`. Solo las últimas N versiones del blueprint, por **número** de versión |
| `date_from` | `YYYY-MM-DD` \| omitido | sin límite | Fecha de creación de la versión, **inclusiva** |
| `date_to` | `YYYY-MM-DD` \| omitido | sin límite | **Inclusiva**: entra todo el día |
| `order` | `asc` \| `desc` | `desc` | Orden por número de versión. `desc` = más nuevas primero |
| `page`, `size` | int | 1, 20 | Paginación estándar |

**`last` y las fechas se combinan como intersección**: primero la ventana de las últimas N
versiones, después el filtro de fechas dentro de esa ventana. "Últimas 20, de septiembre" puede
devolver menos de 20 aunque haya más versiones de septiembre más viejas. Cualquiera de las dos
fechas sola es válida ("desde el 1 de septiembre hasta hoy").

Las fechas se comparan contra `created_at` tal como lo guarda el gateway (reloj del servidor
de la BD del gateway, sin zona horaria). Para un rango de días alcanza; no lo uses para
precisión de horas.

`pagination.total` es la cantidad de **versiones que coinciden**, no la de versiones del
blueprint.

---

## 2. Respuesta

```json
{
  "data": [
    {
      "id": 812,
      "model_id": 14,
      "version": "0042",
      "name": "Índice de facturas",
      "created_at": "2026-09-18T10:21:07",
      "is_latest": true,
      "match_count": 3,
      "lines_matched": 2,
      "snippets": [
        {
          "line": 3,
          "text": "ALTER TABLE invoices ADD INDEX idx_invoice_ref (invoice_ref);",
          "match_start": 35,
          "match_end": 46
        },
        {
          "line": 7,
          "text": "…INSERT INTO settings (k, v) VALUES ('invoice_ref_prefix', 'FAC-'), ('n…",
          "match_start": 38,
          "match_end": 49
        }
      ]
    }
  ],
  "pagination": { "page": 1, "size": 20, "total": 1, "pages": 1, "has_next": false, "has_prev": false }
}
```

| Campo | Qué es |
|---|---|
| `is_latest` | La versión es la **punta del blueprint** (la de mayor número de todo el catálogo, no de la búsqueda ni de la ventana `last`). Mismo significado que en el listado |
| `match_count` | Coincidencias totales en el `up_sql` de esa versión |
| `lines_matched` | Líneas distintas con al menos una coincidencia |
| `snippets` | **Hasta 3** líneas con coincidencia, en orden de aparición. Si `lines_matched > snippets.length`, hay más que no se muestran: decí "y N más" |
| `snippets[].line` | Número de línea (1-based) dentro del `up_sql` |
| `snippets[].text` | La línea, recortada a ~160 caracteres alrededor de la coincidencia. Si se recortó, empieza y/o termina en `…` |
| `snippets[].match_start` / `match_end` | Offsets **relativos a `text`**, ya contando el `…`. `match_end` es exclusivo. **Cuentan puntos de código (índices de Python), no unidades UTF-16**: `text.slice(match_start, match_end)` se corre en líneas con emoji u otros caracteres fuera del BMP. Cortá sobre `Array.from(text)` |

**La respuesta nunca trae el `up_sql` completo** (puede ser LONGTEXT). Para ver la versión
entera, abrila con el endpoint existente `GET /database-models/{model_id}/migrations/{version}`.

```ts
export const migrationSearchSnippetSchema = z.object({
  line: z.number().int().min(1),
  text: z.string(),
  match_start: z.number().int().min(0),
  match_end: z.number().int().min(0),
})

export const migrationSearchHitSchema = z.object({
  id: z.number().int(),
  model_id: z.number().int(),
  version: z.string(),
  name: z.string(),
  created_at: z.string(),
  is_latest: z.boolean(),
  match_count: z.number().int().min(1),
  lines_matched: z.number().int().min(1),
  snippets: z.array(migrationSearchSnippetSchema),
})
```

---

## 3. Errores

| Status | `public_context.code` | Cuándo | Campos extra |
|---|---|---|---|
| 422 | `model_migration.search_query_too_short` | `q` sin espacios de los extremos tiene menos de 4 caracteres | `min_length` (4) |
| 422 | `model_migration.search_invalid_date_range` | `date_from` posterior a `date_to` | — |
| 422 | — (error de validación) | `q` ausente o de más de 200 caracteres, `last` fuera de `1..1000`, fecha mal formada, `order` inválido | — |
| 404 | — | El blueprint no existe | — |

Un término de 1–3 caracteres **siempre** devuelve el código `search_query_too_short`, no el
error de validación genérico: el mínimo lo aplica el backend con código a propósito, para que la
UI tenga una sola forma de error que mapear. Los dos códigos nuevos son del recurso
`model_migration.`, así que caen en la misma pantalla que el resto de los errores de versiones.

---

## 4. Sugerencias de UI

- **Caja de búsqueda con debounce** (~300 ms). No dispares la request hasta tener 4 caracteres
  sin espacios en los extremos; mostrá el mínimo como ayuda, no como error.
- **Preset de ventana `last`**: 5 / 10 / 20 / 50 / Todas (Todas = omitir el parámetro).
  Cualquier otro valor entre 1 y 1000 es válido si más adelante se quiere un campo libre.
- **Selector de rango de fechas** con ambos extremos opcionales. Validá `desde ≤ hasta` en el
  cliente; el 422 del backend queda como red de seguridad.
- **Toggle "Distinguir mayúsculas"** → `case_sensitive`.
- **Resultado**: versión + nombre + fecha, insignia "Última" si `is_latest`, contador
  `match_count`, y los fragmentos con el número de línea y la coincidencia resaltada con
  `match_start`/`match_end`. Usá fuente monoespaciada y no hagas `trim()` del `text`: los
  offsets son sobre el texto tal cual llega. Insertá los tres tramos (antes, coincidencia,
  después) **como texto, nunca como HTML**: el `up_sql` lo escribe el usuario.
- **Click en un resultado** → abrir la versión (`GET .../migrations/{version}`), idealmente
  posicionando el visor en `snippets[0].line`.
- **Cambiar `q`, `case_sensitive`, `last` o las fechas vuelve a `page=1`.** `total` depende de
  todos esos filtros.
