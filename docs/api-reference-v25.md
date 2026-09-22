# Slug de blueprint renombrable, contabilidad de versiones huérfana e historial con actor

Addendum posterior a `api-reference-v24.md`. Cubre lo que salió del incidente de producción en
el que se renombró el `slug` de un blueprint con 10 bases asignadas: la app mostró **todas las
versiones como pendientes desde la 0001**, se aplicó sobre 8 bases y falló con "ya existen
tablas" — ese fallo fue lo único que evitó el daño real.

**No repite v23 ni v24.** Para el envelope de autorización, el CSRF y el catálogo de
capacidades, la fuente sigue siendo v23.

---

## Δ Actualización: lo que cambió DESPUÉS de la primera publicación de este addendum

El frontend ya implementó la primera versión de v25 (`main`, `3e32ff1`). Esta sección es el
**delta** contra esa versión: lo que todavía no tiene. El resto del documento ya está
actualizado en su lugar.

### 🔴 Rompe el parseo: `action` gana un quinto valor, `already`

`renameSlugActionSchema` está declarado hoy como `z.enum(['rename', 'skip', 'conflict',
'unreachable'])`. El backend ahora puede devolver **`already`**, y un valor fuera de un
`z.enum` **no se strippea: descarta el plan entero**.

```ts
export const renameSlugActionSchema = z.enum(['rename', 'skip', 'already', 'conflict', 'unreachable'])
```

`already` = la base **ya tiene** la tabla destino y no tiene la de origen. No hay nada que
hacer y **no bloquea**. En el renombrado de slug es raro; en la migración de prefijo (§2.3)
es **el caso normal** de toda base ya migrada. Es el único cambio de esta actualización que
rompe algo, y va antes de desplegar el backend.

### Campos nuevos (todos `.nullish()`, se strippean sin romper)

| Dónde | Campo | Qué es |
|---|---|---|
| Cada ítem de `databases` del plan | `source_table` | La tabla a renombrar **en esa base**. Puede ser `_gw_v_…` o `_datum_version_…`: se resuelve por base |
| Cada ítem de `databases` del plan | `has_mirror` | Si la base ya tiene `_datum_migrations`. `null` = ilegible |
| Plan | `mirror_table`, `mirror_pending_count` | Cuántas bases quedan sin espejo |
| Plan | `prefix_only` | `true` en la migración de prefijo (§2.3) |
| Resultado de `rename-slug` y `migrate-version-table` | `mirror` | `{created, failed, skipped_disabled}`. Ver §2.2 |

### Dos endpoints nuevos

`POST /database-models/{id}/migrate-version-table/plan` y
`POST /database-models/{id}/migrate-version-table`. Ver §2.3. **Es el botón de "actualizar al
formato Datum"** y hoy no tiene ninguna referencia en el frontend.

### Cambió el nombre de las tablas: prefijo `_datum_`

Los ejemplos con `_gw_v_…` de la primera versión quedaron viejos. Ver §8. Lo que importa para
la UI: **no hay que asumir ningún prefijo**. Usá siempre el nombre que devuelve el backend
(`new_table`, `source_table`, `expected_table`), porque dentro de un mismo blueprint conviven
bases con los dos.

---

## 0. Lo que ordena todo: dos campos cambian de TIPO 🔴

> **Corrección sobre addendums anteriores.** v24 y sus predecesores dicen que la SPA hace
> `safeParse` del envelope completo y que "un campo nuevo descarta la respuesta entera". **Eso
> no es cierto en este repo**: no hay un solo `.strict()` en `src/lib/contracts/`, así que
> `z.object` hace *strip* y un campo nuevo se ignora en silencio. Ya estaba verificado por
> escrito en el JSDoc de `allows_agent_access` en `src/lib/contracts/environments.ts`, contra
> zod 4.4.3. Esta sección corrige la premisa, no la conclusión: **el orden de despliegue sigue
> siendo obligatorio**, por un motivo distinto y más concreto.

### Lo que SÍ rompe el `safeParse`

`migrationHistoryItemSchema` (`src/lib/contracts/db-migrations.ts:271`) declara hoy:

```ts
model_migration_id: z.number().int(),   // pasa a number | null
version: z.string(),                    // pasa a string | null
```

Los dos pasan a **nullable** en esta entrega, porque la FK de `model_migration_id` cambió de
`ON DELETE CASCADE` a `SET NULL`. Un `null` contra `z.number().int()` **falla el parseo y
descarta la página entera de historial**.

**Y el fallo es DIFERIDO**, que es lo que lo vuelve peligroso: solo aparece cuando alguien
borra una versión del blueprint, que puede ser semanas después del despliegue. Para entonces
nadie lo asocia a este cambio.

### Lo que NO rompe, pero es peor

Los demás campos nuevos se strippean sin ruido. No rompen nada — **reproducen el incidente.**

Sin declarar `has_orphan_accounting` en el schema de `status`, zod lo descarta, la UI nunca se
entera de que la contabilidad está huérfana y **vuelve a ofrecer el botón de aplicar con la
cadena entera figurando pendiente**, exactamente como durante el incidente que originó esta
entrega.

Un campo faltante que no rompe nada es más difícil de detectar que uno que sí.

### El resumen operativo

| Endpoint | Cambio | Si no se declara |
|---|---|---|
| `GET .../migrations/history` | `model_migration_id` y `version` pasan a **nullable** | 🔴 Rompe el parseo, de forma diferida |
| `GET .../migrations/history` | `direction`, `applied_checksum`, `actor_type`, `actor_id`, `actor_username`, `request_id` | Se strippean; el historial pierde las cuatro dimensiones nuevas |
| `GET .../migrations/status` | `cached_version`, `orphan_version_tables`, `has_orphan_accounting` | 🔴 Se strippea; la UI reproduce el incidente |

**Campos nuevos: `.nullish()`, nunca `.optional()`.** Y los dos cambios de tipo van **antes**
de que el backend se despliegue.

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
  "current_table": "_datum_version_production_db",
  "new_table": "_datum_version_facturacion",
  "no_op": false,
  "databases": [
    { "managed_database_id": 7, "database_name": "tienda_42", "server_id": 3,
      "server_name": "mysql-prod-1", "action": "rename",
      "source_table": "_gw_v_production_db", "has_mirror": false, "detail": null },
    { "managed_database_id": 8, "database_name": "tienda_43", "server_id": 3,
      "server_name": "mysql-prod-1", "action": "skip",
      "source_table": null, "has_mirror": true, "detail": null }
  ],
  "rename_count": 1,
  "mirror_table": "_datum_migrations",
  "mirror_pending_count": 1,
  "prefix_only": false,
  "blockers": [],
  "requires_confirmation": true,
  "confirm_token": "1790000000.9f2a…",
  "expires_at": "2026-09-21T10:15:00Z",
  "fingerprint": "sha256…"
}
```

`action` es un enum cerrado de **cinco** valores:

| Valor | Significa | ¿Bloquea? |
|---|---|---|
| `rename` | Tiene la tabla de origen y el destino está libre | No |
| `skip` | Nunca fue posicionada: no hay tabla que renombrar | No |
| `already` | **Ya tiene el destino** y no el origen. Nada que hacer | No |
| `conflict` | **Conviven las dos** tablas: no se puede decidir cuál es el puntero bueno | **Sí** |
| `unreachable` | No se pudo leer la base | **Sí** |

`source_table` viene **por base**, y no es un detalle: dentro de un mismo blueprint puede
haber bases con el prefijo histórico `_gw_v_` y otras con el vigente `_datum_version_`. Si la
UI muestra "qué se va a renombrar", tiene que mostrar el de cada fila.

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
  "no_op": false,
  "mirror": { "created": [ /* ítems */ ], "failed": [], "skipped_disabled": false }
}
```

**`mirror`**: además de renombrar, la operación crea la tabla espejo `_datum_migrations` en
las bases donde falte (§8). Un fallo acá **no aborta** la operación, pero **sí se reporta** en
`mirror.failed`: la UI tiene que mostrarlo, porque sin eso el operador cree que el parque
quedó uniforme. `skipped_disabled: true` significa que el espejo está apagado por
`MIGRATION_MIRROR_ENABLED` en el backend.

Rate limit 3/min. El plan se **recalcula desde cero** en la ejecución: el token no transporta el
plan, solo prueba que el estado del parque no cambió desde el preview.

**El orden interno importa y conviene que la UI lo explique**: primero los N renames remotos, y
el slug del gateway se actualiza **último**. Si algo falla a mitad, se compensa renombrando de
vuelta y **el slug no se modifica**.

---

### 2.3 Migrar al formato Datum: `migrate-version-table` (el botón de "actualizar")

```
POST /api/v1/database-models/{model_id}/migrate-version-table/plan     (sin cuerpo)
POST /api/v1/database-models/{model_id}/migrate-version-table          { "confirm_token": "…" }
```

Moderniza las bases del blueprint al formato vigente **sin cambiar el slug**: renombra
`_gw_v_{slug}` → `_datum_version_{slug}` y crea `_datum_migrations` donde falte.

Es **la misma operación** que el renombrado de slug con el slug igual a sí mismo, así que
devuelve **exactamente los mismos schemas** (`RenameSlugPlanOut` y `RenameSlugOut`), con
`prefix_only: true` en el plan. La UI puede reusar el diálogo de dos pasos del renombrado.

Tres cosas que la UI tiene que saber:

- **Es opcional.** Una base con el prefijo histórico sigue funcionando indefinidamente: el
  backend resuelve el nombre por base. No hay que presentarlo como una migración obligatoria
  ni como un error pendiente.
- **`already` es el caso normal.** Toda base ya migrada sale así. Una segunda corrida sobre un
  blueprint ya migrado es un plan con todo en `already`, `rename_count: 0` y sin token — y no
  hay que mostrarlo como fallo.
- **El parque se moderniza solo con el uso** (§8): cada apply, rollback o stamp moderniza la
  base que toca. El botón sirve para no esperar.

Rate limit: 10/min el plan, 3/min la ejecución.

---

## 3. Diagnóstico: `GET /api/v1/database-models/{model_id}/version-tables`

Solo lectura. Compara, base por base, lo que el gateway **espera** contra lo que hay en el motor.

```jsonc
// 200
{
  "model_id": 19,
  "slug": "production_db",
  "expected_table": "_datum_version_production_db",
  "databases": [
    {
      "managed_database_id": 7, "database_name": "tienda_42",
      "server_id": 3, "server_name": "mysql-prod-1",
      "expected_table": "_datum_version_production_db",
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
| `database_model.slug_rename_conflict` | En alguna base **conviven** la tabla de origen y la de destino (ambiguo cuál es el puntero bueno). Trae `conflicting_databases`. **No** es "ya tiene el destino": eso es `already` y no bloquea | Revisar esas bases con `/version-tables` |
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

---

## 8. El formato de tablas `_datum_` dentro de cada base

Dos tablas de contabilidad del gateway viven **dentro de cada BD gestionada**. Ninguna es
esquema del usuario: el diff, el snapshot y el clon las excluyen.

| Tabla | Qué es | Nombre |
|---|---|---|
| `_datum_version_{slug}` | El puntero de versión de Alembic. Una fila | Antes `_gw_v_{slug}` |
| `_datum_migrations` | **Espejo** del historial de migraciones, con el blueprint como columna | Fijo, sin slug |

**El prefijo histórico `_gw_v_` sigue soportado para siempre.** El backend no asume un
nombre: lo resuelve contra cada base. Una base vieja funciona con su `_gw_v_`, una base nueva
nace con `_datum_version_`.

**Cuándo una base pasa al formato nuevo**, sin que nadie haga nada:

- una base **nueva** nace con el formato nuevo en su primera migración;
- **cada apply, rollback o stamp** moderniza la base que toca (renombra `_gw_v_` → `_datum_`
  y crea el espejo si falta). El `dry_run` **no**: un ensayo no escribe;
- `rename-slug` y `migrate-version-table` (§2) dejan la base en el formato completo.

**El espejo `_datum_migrations` es un espejo, no la fuente de verdad.** Se escribe fail-open:
si falla, la migración sigue, así que **puede tener huecos**. La autoridad es el historial del
gateway (`GET …/migrations/history`, §5). La UI no debe leerlo ni presentarlo como historial
completo. Existe para que la base se explique a sí misma cuando el gateway no está — un backup
restaurado en otro lado, por ejemplo — y por eso **sí viaja en el export**, a diferencia del
puntero de versión.

`MIGRATION_MIRROR_ENABLED` (backend, default `true`) apaga el espejo sin desplegar código.

