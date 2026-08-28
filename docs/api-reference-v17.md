# API Reference v17 — Conversión de collation en lote, versión de contabilidad y deriva

Addendum al contrato consolidado. **Supersede una afirmación de
[`api-reference-v8.md`](api-reference-v8.md) §3.0**, que hoy dice como regla dura que *"en todo
el resto del módulo **no hay `public_context`**"* y *"**nunca leas `detail.context`**"*. Eso deja
de ser cierto: todos los rechazos nuevos de este módulo traen `public_context.code` con
vocabulario cerrado. El resto de v8 sigue vigente.

> **No hay que tocar el parser**: `errors.ts` ya extrae `public_context.code` a `ApiError.code`
> de forma genérica. Lo que hay que cambiar es *leerlo* — hoy
> `collation-conversions/wizard/messages.ts` clasifica **ocho** errores del módulo matcheando
> prosa en español con expresiones regulares, y su propio docstring explica que era porque el
> backend no exponía códigos. Ver §7.

---

## 0. Lo que resuelve, en una línea

Convertir la collation de **todas** las BDs de un blueprint en un gesto, y dejar constancia de
eso como versión del blueprint sin que esa versión sea peligrosa.

### Por qué NO es una migración de blueprint

Es la pregunta que va a volver, así que queda escrita. El SQL de una conversión promete un
resultado que **depende del estado de cada destino**. Una versión estática no puede recrear los
objetos con la collation congelada de la hermana —necesita su cuerpo, sus grants y su DEFINER,
no los del origen—, así que aplicarla le convertiría las tablas y le dejaría las vistas y
rutinas en la collation vieja: exactamente el `Illegal mix of collations` que este módulo
existe para evitar, ahora sobre una BD cuyo operador nunca vio el asistente.

Por eso el lote son **N conversiones reales**, cada una leyendo su propio inventario. La
versión (§4) es **contabilidad de algo ya ocurrido**: se crea y se **stampea**, nunca se aplica.

---

## 1. Nulabilidad: leer esto antes de escribir los schemas zod

`ApiResponse` filtra los `None` **solo del envelope**. Los `None` **anidados dentro de `data`
salen como `null` explícito**, y Zod `.optional()` **rechaza `null`**. Es la causa raíz de
`T-260822-lz-contratos-nullish`, con dos endpoints roros hoy por eso.

**Regla para todo lo de este documento: cada campo marcado `| null` va `.nullable()`, no
`.optional()`.** Y el `safeParse` corre sobre el envelope completo, así que una divergencia de
un campo cuesta la respuesta **entera**.

Los schemas de la SPA no usan `.strict()` (verificado: 0 de 27 archivos de
`src/lib/contracts/`), así que **todo lo aditivo de este documento no rompe nada** aunque el
frontend se despliegue después.

---

## 2. Campos nuevos en el summary de una conversión

`GET /collation-conversions/{id}` gana cuatro campos. Aditivos, y los cuatro pueden ser `null`.

| Campo | Tipo | Para qué |
|---|---|---|
| `batch_id` | `int \| null` | Lote al que pertenece, o `null` si es una conversión suelta. Permite volver al lote desde un deep-link a un job. |
| `batch_seq` | `int \| null` | Posición **1-based** en el lote. Los jobs corren **en serie**, así que es lo único que permite decir "la 4 de 12" y ordenar la tabla de forma estable: el estado por sí solo no distingue "en cola" de "ya terminada" en un orden reproducible. |
| `tables_total` | `int \| null` | Tablas a convertir según el plan confirmado. |
| `objects_total` | `int \| null` | Objetos a recrear según el plan confirmado. |

**Los dos `*_total` son el denominador que faltaba.** `progress` solo cuenta lo *hecho* y nunca
el total — está declarado en v8 §3.2 — así que hoy la SPA lo parchea guardando los totales del
preview en estado de React (`use-collation-conversion-wizard.ts`), que **se pierde al recargar**
justo en una operación que dura horas. Ahora vienen del servidor y sobreviven la recarga: se
puede borrar ese `savedTotals`.

Son `null` mientras el job no se haya previsualizado.

---

## 3. Lote por blueprint

Cuatro endpoints nuevos bajo `/database-models/{model_id}`.

### 3.1 `POST /database-models/{model_id}/collation-conversions` → 201

Planifica: crea y previsualiza **un job por BD activa** del blueprint. Rate limit `10/minute`
(toca el motor una vez por BD).

**Body**

| Campo | Tipo | Default | Notas |
|---|---|---|---|
| `target_charset` | `string \| null` | — | Obligatorio en MySQL/MariaDB. Las BDs PostgreSQL del blueprint salen como ítem con `error_code`, sin abortar el lote. |
| `target_collation` | `string` | — | Obligatorio. |
| `scope` | `"all_tables" \| "explicit"` | `"all_tables"` | `all_tables` se resuelve contra el inventario **propio de cada BD**. |
| `tables` | `string[]` | `[]` | Solo con `scope="explicit"`. |
| `objects` | `"all" \| "none"` | `"all"` | `none` deja los objetos congelados: es el caso que la herramienta existe para evitar, y el preview lo avisa. |
| `include_database_default` | `bool` | `true` | |
| `environment_id` | `int \| null` | `null` | Acota el lote a ese entorno. |
| `max_databases` | `int` (1..100) | `10` | |

**Respuesta** (`data`)

| Campo | Tipo | Notas |
|---|---|---|
| `batch_id` | `int` | |
| `model_id` / `model_slug` | `int` / `string` | El slug hay que reenviarlo en `/execute`. |
| `target_charset` | `string \| null` | |
| `target_collation` | `string` | |
| `total_eligible` | `int` | BDs activas del blueprint **antes** del tope. |
| `max_databases` | `int` | |
| `capped` | `bool` | `true` si el tope dejó BDs elegibles afuera. **Mostrarlo**: silenciarlo haría creer que se convirtió todo el blueprint. |
| `batch_token` | `string` | Va en `/execute`. |
| `expires_at` | `datetime` | TTL del plan (`COLLATION_CONVERSION_TTL_HOURS`, 24 h). Vencido → 410. |
| `runs_serially` | `bool` (siempre `true`) | Ver §6. |
| `databases[]` | ver abajo | |

**`databases[]`** — misma forma que `ApplyAllItemOut`, a propósito: el frontend no aprende una
segunda forma de ítem-por-BD.

| Campo | Tipo |
|---|---|
| `managed_database_id` | `int` |
| `server_id` | `int` |
| `database_name` | `string` |
| `batch_seq` | `int` |
| `job_id` | `int \| null` |
| `ok` | `bool` |
| `error` | `string \| null` |
| `error_code` | `string \| null` |
| `tables_to_convert` | `int` |
| `objects_to_recreate` | `int` |
| `include_database_default` | `bool` |
| `missing_tables` | `string[]` |
| `warnings` | `string[]` |
| `confirm_token` | `string \| null` |

**Solo entran las BDs `status=active`**: una `pending` no existe en el motor, una `error` está en
cuarentena y una `archived` fue retirada de uso.

**422** con `code = "collation.batch_no_eligible_databases"` si el blueprint no tiene ninguna.

### 3.2 `POST /database-models/{model_id}/collation-conversions/{batch_id}/execute`

Confirma y encola. Rate limit **`3/minute`**.

**Un lote se lleva N re-tipeos y hay que reponerlos.** El `batch_token` lo genera el servidor,
así que aporta **frescura**, no **intención** — es el mismo argumento con el que v13 eliminó el
consentimiento por corrida. Por eso el body exige las cuatro cosas juntas:

| Campo | Tipo | Notas |
|---|---|---|
| `confirm_model_slug` | `string` | Slug exacto del blueprint. |
| `confirm_token` | `string` | El `batch_token` del plan. |
| `database_ids` | `int[]` | **El conjunto previsualizado, echado de vuelta.** Cualquier diferencia es 422 fail-closed: no se recorta ni se amplía. |
| `confirmations` | `{ [managed_database_id: string]: string }` | `id → nombre exacto re-tipeado`. Obligatorio para toda BD cuyo entorno tenga `blocks_destructive_migrations=true`. |
| `force` | `bool` | Override de cuarentena y de drift de inventario, **por BD**. **NO** amplía el conjunto ni saltea el re-tipeo. |

**Respuesta 200** (aunque alguna BD se rechace):

```
{ batch_id, model_id, enqueued: int, runs_serially: true,
  results: [{ managed_database_id, database_name, job_id, batch_seq, ok, error, error_code }] }
```

Un rechazo **del lote** (slug, conjunto, re-tipeo, token, estado) es 422/409 y **no encola nada**.

### 3.3 `GET /database-models/{model_id}/collation-conversions/{batch_id}`

Polling del lote. Rate limit `30/minute`. **No paginado.**

```
{ batch: { batch_id, model_id, target_charset|null, target_collation, status, error|null,
           total, max_databases, capped, blueprint_version_id|null,
           created_by_username|null, expires_at, created_at,
           started_at|null, finished_at|null, runs_serially,
           counts: { total, queued, running, done, failed, canceled } },
  jobs: [ <summary de conversión, con batch_seq y los *_total de §2> ] }
```

`batch.status` ∈ `pending | running | done | failed | canceled`.

**`counts` existe para no recorrer N filas en cada tick.** Y `done`/`failed` se **derivan** al
leer: ningún worker los escribe (que cada uno consultara a sus hermanos para saber si es el
último sería una carrera).

### 3.4 `POST /database-models/{model_id}/collation-conversions/{batch_id}/cancel`

Devuelve la misma forma que §3.3. Rate limit `10/minute`.

Las BDs **en cola no llegan a tocar el motor**. La que está convirtiendo termina su paso y corta
en el próximo punto seguro: matar un `ALTER TABLE` a mitad dejaría la tabla a medio reescribir.

---

## 4. Versión de contabilidad

`POST /database-models/{model_id}/collation-conversions/{batch_id}/blueprint-version` → 201.
Rate limit `3/minute`. Body: `{ name?: string | null }` (≤200 chars).

```
{ batch_id, model_id, version, migration_id, statement_count,
  stamped: [{ managed_database_id, ok, error|null }],
  pending_stamp: int[], note: string }
```

**Lo que la UI tiene que dejar claro, porque es la parte que se malinterpreta:** esta versión
**se stampea, no se aplica**. Mostrar `note` tal cual — dice que una BD agregada al blueprint
*después* la tendrá pendiente, y que aplicarla le convertiría las tablas **sin** recrearle los
objetos con la collation congelada. Para esa base el camino correcto es su propio job de
conversión y después `stamp`.

`pending_stamp` son las BDs cuyo `stamp` falló. **La versión no se borra**: existe y es
correcta, lo que falta es la marca de esas bases, que se pone a mano con `/migrations/stamp`.

Los ocho rechazos son **409** con su `code` (§5).

---

## 5. Vocabulario CERRADO de códigos, con su texto

Todos viajan en `detail.public_context.code`. Este es el mapa completo: armar el objeto de
mensajes con esto y **no inventar copy**, que después diverge.

| `code` | HTTP | Texto sugerido |
|---|---|---|
| `collation.scope_not_allowed` | 409 | «Esa base es la propia base de metadatos del gateway: no se puede convertir.» |
| `collation.batch_no_eligible_databases` | 422 | «El blueprint no tiene ninguna base activa que se pueda convertir.» |
| `collation.batch_database_set_mismatch` | 422 | «El conjunto de bases cambió desde que se planificó. Volvé a planificar el lote.» Trae `planned_database_ids` y `received_database_ids`. |
| `collation.batch_confirmation_required` | 422 | «Escribí el nombre exacto de las bases de entorno protegido para confirmar.» Trae `requires_confirmation: int[]`. |
| `collation.batch_not_pending` | 409 | «Este lote ya se ejecutó o se canceló.» |
| `collation.engine_not_applicable` | ítem | «El objetivo pedido no aplica a este motor.» (por BD, dentro de una respuesta 200) |
| `collation.version_batch_not_complete` | 409 | «El lote no terminó bien en todas sus bases.» Trae `unfinished: string[]`. |
| `collation.version_blueprint_has_other_engines` | 409 | «El blueprint tiene bases de otro motor: este SQL no aplica.» Trae `engines: string[]`. |
| `collation.version_databases_missing_from_batch` | 409 | «Hay bases activas que no participaron del lote.» Trae `missing_database_ids: int[]`. |
| `collation.version_not_at_head` | 409 | «Alguna base no está en la última versión del blueprint. Aplicá las pendientes primero.» Trae `head_version` y `databases_behind: int[]`. |
| `collation.version_table_sets_differ` | 409 | «Las bases no tienen el mismo conjunto de tablas: hay deriva estructural que resolver antes.» |
| `collation.version_partial_selection` | 409 | «Alguna base convirtió solo parte de sus tablas: no se puede versionar una conversión parcial.» Trae `database_name`. |
| `collation.version_too_large` | 409 | «El SQL de la versión supera el tope de tamaño.» Trae `bytes` y `max_bytes`. |
| `collation.version_quarantined_before_batch` | 409 | «Alguna base está en cuarentena: revisala antes de versionar.» Trae `quarantined_database_ids: int[]`. |

**Forma de error por ítem, una sola**: `ok: bool` + `error_code: string | null`. Molde de
`classifyItem` de `environments/messages.ts`, cuyo fallback documentado es que un ítem con
`ok:false` y **sin** `error_code` cae en `failed`, nunca en `blocked`.

---

## 6. Deriva de collation

`GET /database-models/{model_id}/collation-drift`. **Sin rate limit: no abre ninguna conexión al
motor.**

```
{ model_id, model_slug, declared: {charset, collation} | null,
  source: "cached", source_note: string,
  databases: [{ managed_database_id, database_name, server_id, server_name, engine,
                environment_slug|null, charset|null, collation|null,
                status, source_of_truth }] }
```

`status` ∈ `ok | drifted | unknown | undeclared | not_applicable`.

**`unknown` NO es `ok`.** Pintarlos con el mismo color le diría al operador que todo está bien
sobre bases de las que no se sabe nada. `not_applicable` es PostgreSQL: allá el concepto es
`encoding` + `lc_collate`, que no son equivalentes.

**Mostrar `source_note` textualmente** («Lectura del inventario del gateway, no del motor. Puede
estar desactualizada.»). Es una caché, y esta pantalla se usa para decidir conversiones.

`source_of_truth` ∈ `adopted | provisioned | unknown` — de dónde sale el dato. Importa porque
`charset`/`collation` siguen siendo escribibles a mano por `PATCH /managed-databases/{id}`: una
fila puede decir `ok` porque alguien lo tipeó, sin que nadie haya leído el motor (deuda
`T-260824-lz-charset-managed-patch`).

La declaración se escribe con el `PATCH /database-models/{id}` que ya existe.

---

## 7. Los ocho errores viejos siguen sin código

`collation-conversions/wizard/messages.ts` los clasifica con expresiones regulares sobre la
prosa. **No se les dio código en esta entrega.** Consecuencia concreta: quedan **dos mecanismos
de clasificación en el mismo archivo**, y reescribir un mensaje en español —algo que nadie
considera un cambio de contrato— degrada la UI en silencio.

Mientras siga así, `classifyConversionError` debe chequear **`error.code` primero** y la prosa
solo como fallback. Y conviene abrir el ítem para darles código y borrar `MESSAGE_PATTERNS`.

---

## 8. UX que el contrato no puede imponer pero de la que depende

1. **El lote corre EN SERIE.** `COLLATION_CONVERSION_MAX_WORKERS` es 1 por default, así que un
   lote de 12 monopoliza el módulo por horas. Sin decirlo, la UI parece colgada. El campo que lo
   alimenta es `runs_serially`, y `counts.queued` da la cola.
2. **`capped` se muestra o se miente.** Si el tope dejó bases afuera y no se dice, el operador
   cree que convirtió el blueprint entero.
3. **La versión no se aplica.** Ver §4: `note` va visible.
4. **`objects: "none"`** deja los objetos congelados, que es el bug que la herramienta ataca.
   Los `warnings` del plan lo dicen; hay que mostrarlos.
5. Las filas de `docs/api-coverage.md` las agrega **el frontend** en su propio PR (ese archivo
   vive en el repo de la SPA). Los endpoints nuevos son los cinco de §3 y §4 más el de §6.

---

## 9. Lo que NO cambió

- `POST /servers/{sid}/databases/{db}/collation-conversions` y todo el flujo unitario
  (`/objects`, `/preview`, `/execute`, `/items`, `/cancel`): mismas rutas, mismos bodies. Solo
  el summary gana los cuatro campos de §2.
- La semántica de `progress`, `phase` y `status` de un job.
- El doble factor del `execute` unitario (`confirm_target_name` + `confirm_token`).
