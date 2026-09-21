# Slug de blueprint renombrable, contabilidad de versiones huérfana e historial con actor

Addendum posterior a `api-reference-v24.md`. Cubre lo que salió del incidente de producción en
el que se renombró el `slug` de un blueprint con 10 bases asignadas: la app mostró **todas las
versiones como pendientes desde la 0001**, se aplicó sobre 8 bases y falló con "ya existen
tablas" — ese fallo fue lo único que evitó el daño real.

**No repite v23 ni v24.** Para el envelope de autorización, el CSRF y el catálogo de
capacidades, la fuente sigue siendo v23.

---

## 0. Lo que ordena todo: tres schemas EXISTENTES ganan campos 🔴

La SPA hace `safeParse` del envelope **completo**: un campo nuevo descarta la respuesta entera.
Tres schemas que ya consumís ganan campos en esta entrega:

| Endpoint | Campos nuevos |
|---|---|
| `GET /managed-databases/{id}/migrations/status` | `cached_version`, `orphan_version_tables`, `has_orphan_accounting` |
| `GET /managed-databases/{id}/migrations/history` | `direction`, `applied_checksum`, `actor_type`, `actor_id`, `actor_username`, `request_id` |
| `GET .../history` (cambio de tipo) | `model_migration_id` pasa a **nullable** |

**Todos se declaran `.nullish()` en zod, nunca `.optional()`.** Y el orden de despliegue no es
negociable: **primero zod, después el backend.** Al revés, la pantalla de migraciones deja de
parsear el minuto en que sube la API.

El cambio de tipo de `model_migration_id` es el más fácil de pasar por alto: dejó de ser
`number` y ahora es `number | null`, porque su FK pasó de `ON DELETE CASCADE` a `SET NULL`.

---

## 1. El `slug` ya no se cambia por `PATCH` 🔴

`PATCH /api/v1/database-models/{model_id}` ahora responde **409** si el payload trae un `slug`
distinto del actual **y el blueprint tiene bases gestionadas**.

No es una restricción de forma: el `slug` nombra la tabla de versión de Alembic
(`_gw_v_{slug}`) **dentro de cada base gestionada**. Cambiarlo no renombra nada en los motores,
así que el gateway pasa a leer una tabla que no existe, reporta la cadena entera como pendiente
y un `apply` la reaplica desde la 0001. Es exactamente el incidente.

```jsonc
{ "detail": { "msg": "El slug nombra la tabla de versión dentro de 10 base(s) gestionada(s), así que cambiarlo acá las dejaría huérfanas. Usá POST /database-models/19/rename-slug, que renombra también en los motores con preview y confirmación.",
              "public_context": {
                "code": "database_model.slug_in_use",
                "current_slug": "test_db",
                "requested_slug": "production_db",
                "managed_database_count": 10
              } } }
```

**El `name` sigue siendo libre.** Renombrar el blueprint para una persona es seguro; cambiar su
identificador para el motor no lo es. La UI puede seguir editando `name` sin fricción y debería
separar visualmente los dos campos.

Un blueprint **sin** bases asignadas sigue pudiendo cambiar su slug por `PATCH` normalmente.

---

## 2. Renombrar el slug: preview + ejecución

### 2.1 `POST /api/v1/database-models/{model_id}/rename-slug/plan`

Preflight. **No escribe nada**, ni en el gateway ni en un motor. Abre una conexión por base, así
que tiene rate limit propio (10/min) aunque sea una lectura.

```jsonc
// request
{ "new_slug": "facturacion" }
```

```jsonc
// 200
{
  "model_id": 19,
  "current_slug": "production_db",
  "new_slug": "facturacion",
  "current_table": "_gw_v_production_db",
  "new_table": "_gw_v_facturacion",
  "no_op": false,
  "databases": [
    { "managed_database_id": 7, "database_name": "tienda_42", "server_id": 3,
      "server_name": "mysql-prod-1", "action": "rename", "detail": null },
    { "managed_database_id": 8, "database_name": "tienda_43", "server_id": 3,
      "server_name": "mysql-prod-1", "action": "skip", "detail": null }
  ],
  "rename_count": 1,
  "blockers": [],
  "requires_confirmation": true,
  "confirm_token": "1790000000.9f2a…",
  "expires_at": "2026-09-21T10:15:00Z",
  "fingerprint": "sha256…"
}
```

`action` es un enum cerrado de cuatro valores:

| Valor | Significa | ¿Bloquea? |
|---|---|---|
| `rename` | Tiene la tabla vieja y el nombre nuevo está libre | No |
| `skip` | Nunca fue posicionada: no hay tabla que renombrar | No |
| `conflict` | El nombre **destino** ya existe en esa base | **Sí** |
| `unreachable` | No se pudo leer la base | **Sí** |

**Los dos bloqueantes abortan la operación entera, no solo esa base.** El gateway apunta a UN
nombre, así que dejar medio parque renombrado deja a la otra mitad con su contabilidad huérfana.
La UI tiene que presentarlo así: no es una lista de "las que sí y las que no", es un semáforo.

`no_op: true` significa que los dos slugs truncan al **mismo** nombre de tabla (el límite es 63
caracteres). Ahí no hay nada que renombrar en ningún motor y el cambio es puramente local: no se
emite token y la ejecución no lo pide.

`confirm_token` se emite **solo** si hay bases que renombrar y nada bloquea. Uno que no hace
falta entrena al cliente a mandarlo siempre.

### 2.2 `POST /api/v1/database-models/{model_id}/rename-slug`

```jsonc
// request
{ "new_slug": "facturacion", "confirm_token": "1790000000.9f2a…" }
```

```jsonc
// 200
{
  "model": { /* DatabaseModelOut con el slug nuevo */ },
  "renamed_databases": [ /* los items con action="rename" que sí se renombraron */ ],
  "no_op": false
}
```

Rate limit 3/min. El plan se **recalcula desde cero** en la ejecución: el token no transporta el
plan, solo prueba que el estado del parque no cambió desde el preview.

**El orden interno importa y conviene que la UI lo explique**: primero los N renames remotos, y
el slug del gateway se actualiza **último**. Si algo falla a mitad, se compensa renombrando de
vuelta y **el slug no se modifica**.

---

## 3. Diagnóstico: `GET /api/v1/database-models/{model_id}/version-tables`

Solo lectura. Compara, base por base, lo que el gateway **espera** contra lo que hay en el motor.

```jsonc
// 200
{
  "model_id": 19,
  "slug": "production_db",
  "expected_table": "_gw_v_production_db",
  "databases": [
    {
      "managed_database_id": 7, "database_name": "tienda_42",
      "server_id": 3, "server_name": "mysql-prod-1",
      "expected_table": "_gw_v_production_db",
      "present_tables": ["_gw_v_test_db"],
      "orphan_tables": [
        { "table": "_gw_v_test_db", "version": "0012" }
      ],
      "current_version": null,
      "cached_version": "0012",
      "status": "orphaned",
      "detail": "La versión real vive en una tabla que el gateway ya no lee. Todas las versiones figuran pendientes aunque no lo estén."
    }
  ],
  "summary": { "ok": 2, "orphaned": 8, "mixed": 0, "none": 0, "unreachable": 0 },
  "needs_attention": true
}
```

`status` por base, enum cerrado de cinco valores:

| Valor | Significa |
|---|---|
| `ok` | Solo la tabla esperada. Nada que hacer |
| `orphaned` | **No** está la esperada pero sí otras. La versión real vive donde el gateway no mira |
| `mixed` | Está la esperada y además sobra basura de un rename o una recuperación a medias |
| `none` | Ninguna. Es lo **normal** en una base que nunca fue posicionada |
| `unreachable` | No se pudo leer. No se asume nada |

`needs_attention` es el semáforo: `true` si hay al menos una `orphaned` o `mixed`.

Un motor caído **no rompe el informe**: esa base sale `unreachable` y el resto se reporta igual.

**`orphan_tables[].version` es el dato con el que se decide la recuperación**: es la versión
que esa base tiene realmente, guardada en la tabla que el gateway dejó de leer. `null` significa
que la tabla existe pero está vacía.

Con eso, el `stamp` de recuperación sale de acá: `POST /managed-databases/{id}/migrations/stamp`
con esa versión. Comparalo contra `cached_version` antes de ejecutar — si difieren, mirá esa
base en particular antes de tocarla.

---

## 4. `status` avisa cuando sus propios datos no son de fiar

`GET /api/v1/managed-databases/{db_id}/migrations/status` gana tres campos:

```jsonc
{
  "current_version": null,
  "cached_version": "0012",
  "orphan_version_tables": ["_gw_v_test_db"],
  "has_orphan_accounting": true,
  "pending_versions": ["0001", "0002", "…", "0012"]
}
```

**`has_orphan_accounting: true` significa que `pending_versions` NO es de fiar.** La versión real
vive en una tabla que el gateway no está leyendo, así que la cadena figura entera pendiente sin
estarlo, y aplicar ahí reejecutaría migraciones ya aplicadas.

Es el aviso que no existía durante el incidente. **La UI tiene que bloquear o advertir el botón
de aplicar con este flag en `true`**, y mandar al informe de la §3.

La sonda se dispara solo ante esa firma exacta (el motor no reporta versión **pero** el
inventario sí tenía una), así que una base nueva devuelve `orphan_version_tables: []` sin costo.

---

## 5. `history` responde quién y con qué texto

`GET /api/v1/managed-databases/{db_id}/migrations/history` gana seis campos.

```jsonc
{
  "id": 412, "managed_database_id": 7,
  "model_migration_id": 88,
  "version": "0012",
  "applied_at": "2026-09-14T11:02:31",
  "status": "applied",
  "error": null,
  "execution_ms": 1840,
  "direction": "up",
  "applied_checksum": "9f2a…",
  "actor_type": "admin",
  "actor_id": 1,
  "actor_username": "ocarrasco",
  "request_id": "b71c…"
}
```

Tres cosas que conviene entender para pintarlo bien:

**`direction`** distingue un apply de un rollback. Antes eran indistinguibles: las dos escribían
`status: "applied"`. Es `null` en todo el historial previo a esta entrega, y ahí un `applied`
**no prueba** que la versión siga vigente.

**`applied_checksum`** es el checksum del SQL que **realmente corrió**, no el vigente de la
definición. Si difiere del `checksum` de la migración, esa versión se editó después de aplicarse
en esa base. Es un dato accionable: la UI puede marcar esas filas como divergentes.

**`version`** ahora sale de la copia congelada al momento del intento. Antes se resolvía por el
join, o sea que mostraba la versión **actual** — y un renumerado hacía que un evento viejo
exhibiera un número que nunca tuvo. Para filas previas a esta entrega sigue cayendo al join, con
esa salvedad.

**`model_migration_id` puede ser `null`** desde esta entrega: su FK pasó a `ON DELETE SET NULL`,
así que borrar una versión del blueprint ya **no borra** su historial de aplicación en las N
bases. Con la FK en null, `version` y `applied_checksum` son lo único que queda del evento.

`request_id` correlaciona con `audit_log` y con los logs HTTP.

---

## 6. `stamp` gana `purge`

`POST /api/v1/managed-databases/{db_id}/migrations/stamp?version=0012&force=true&purge=true`

Vacía la tabla de versión **antes** de escribir, en vez de pedirle a Alembic que resuelva el
puntero actual para moverlo.

Existe para un caso: una base cuyo puntero nombra una revisión que ya no está en la cadena.
Sin `purge`, Alembic muere con `Can't locate revision identified by …` y esa base queda **sin
apply, sin rollback y sin stamp**.

**Requiere `force`** (422 si no): descarta el puntero actual sin leerlo, así que quien lo pide
está afirmando que ya sabe en qué versión está esa base. No lo ofrezcas como un checkbox más —
va detrás de la misma fricción que `force`.

El destino sigue teniendo que existir en el blueprint.

---

## 7. Vocabulario de errores

Siete códigos nuevos, todos en `public_context.code`:

| Código | Cuándo | Salida |
|---|---|---|
| `database_model.slug_in_use` | `PATCH` que cambia el slug de un blueprint con bases | Usar `/rename-slug` |
| `database_model.name_or_slug_taken` | Nombre o slug duplicado (alta y `PATCH`) | Elegir otro valor |
| `database_model.slug_rename_conflict` | Alguna base ya tiene la tabla destino. Trae `conflicting_databases` | Resolver esas bases primero |
| `database_model.slug_rename_unreachable` | Alguna base ilegible. Trae `unreachable_databases` | Recuperar el acceso |
| `database_model.slug_rename_confirmation_required` | Falta `confirm_token`. Trae `rename_plan` | Pedir el preview |
| `database_model.slug_rename_plan_stale` | El parque cambió desde el preview | Volver a pedir el plan |
| `database_model.slug_rename_failed` | Falló a mitad. Trae `renamed`, `failed`, `not_compensated` | Reparar a mano las de `not_compensated` |

**Corrección sobre el vocabulario de migraciones.** `migration_freeze_catalog.ERROR_CODES` estaba
definido dos veces y la segunda pisaba a la primera: el catálogo efectivo tenía **5 de 12**
códigos. Los siete que faltaban son los del borrado con renumerado —`version_in_use`,
`unreadable_databases`, `renumber_confirmation_required`, `renumber_plan_stale`,
`renumber_stamp_failed`, `renumber_target_missing`, `affected_partial_application`—. **Siempre se
emitieron correctamente**; lo que estaba mal era el catálogo. Si el cliente los tenía como
desconocidos, ya se pueden mapear.
