# API Reference v19 — Clonar N bases de un servidor a otro en un gesto

Addendum al contrato consolidado. **Todo lo de acá es ADITIVO**: rutas nuevas bajo un prefijo
nuevo, un schema nuevo y códigos nuevos dentro del vocabulario `clone.*` que ya existía. Ningún
endpoint anterior cambia de forma, así que un cliente que ignore este documento sigue
funcionando igual.

> Lo único que cambia en el módulo viejo es que `ClonePreviewIn` ahora se usa de verdad con su
> idioma declarativo (`structure`/`data`, que existían desde el trabajo de solo-datos y la SPA
> nunca adoptó). Ver §7.

---

## 0. Lo que resuelve, en una línea

Mover varias bases de datos de un servidor a otro sin repetir el asistente N veces, con **una
sola confirmación** y un estado agregado que se puede seguir.

### Qué NO es

**No es un motor de clonación nuevo.** Cada fila del lote termina siendo un `CloneJob` real, con
su snapshot, su fingerprint anti-TOCTOU, su advisory lock, sus ítems y su pantalla de detalle de
siempre. El lote es la capa de orquestación que faltaba, y nada más.

---

## 1. Las tres restricciones que hay que entender antes de leer el contrato

Son decisiones de diseño, no limitaciones temporales, y explican la forma de todo lo demás.

### 1.1 El lote NO borra el destino

`clean_mode` **no existe** en ningún schema de este contrato. Un modo destructivo multiplicado
por N bases y autorizado con un solo gesto es exactamente la operación que tiene que seguir
siendo de a una, con el re-tipeo del nombre de esa base concreta.

**Consecuencia visible, y hay que decirla en la UI:** con destino EXISTENTE la única intención
admitida es `copy_intent: 'data_only'` — con `structure_and_data` los `CREATE TABLE` chocarían
contra las tablas que ya están. Y como `data.on_existing='truncate'` sigue sin existir, **no hay
"vaciar y recargar"**: para un refresco total hay que borrar las bases destino desde la pantalla
de ciclo de vida del servidor (que tiene su propia confirmación) y correr el lote con
`target_mode: 'new'`.

### 1.2 No hay `preview`

El plan de cada base se resuelve **cuando le toca el turno**, no al planear el lote. Al planear
solo se valida lo barato: identificadores, alcance, existencia del destino y coherencia del
modo, sin fotografiar una sola base.

Tres motivos: evita N snapshots antes de que el operador confirme; evita que las últimas filas
de un lote de horas venzan por `CLONE_TTL_HOURS`; y evita ejecutar un DDL calculado seis horas
antes. **Lo que se confirma es la INTENCIÓN** —el conjunto exacto de pares origen→destino—, que
a esa escala de tiempo es lo único honestamente confirmable.

### 1.3 Las filas van EN SERIE

Una por vez. `CLONE_BATCH_MAX_WORKERS` (default `1`) controla cuántos **lotes** corren a la vez,
no cuántas filas dentro de un lote: eso no es configurable. La UI tiene que decirlo, porque si
no "4 de 12" se lee como lentitud en vez de como el diseño.

---

## 2. Endpoints

Todos bajo `AdminDep`, envueltos en `ApiResponse[T]`.

| Método | Ruta | Límite | Qué hace |
|---|---|---|---|
| POST | `/api/v1/database-clone-batches` | 10/min | Arma el plan del lote. **201.** |
| GET | `/api/v1/database-clone-batches` | — | Historial paginado, del más nuevo al más viejo. |
| GET | `/api/v1/database-clone-batches/{id}` | 30/min | Cabecera + `counts`. Latido del polling. |
| GET | `/api/v1/database-clone-batches/{id}/items` | — | Una fila por base, ordenadas por `seq`. |
| POST | `/api/v1/database-clone-batches/{id}/execute` | 3/min | Confirma y encola el recorrido. |
| POST | `/api/v1/database-clone-batches/{id}/cancel` | — | Cancelación cooperativa. |
| GET | `/api/v1/database-clone-batches/{id}/retry-candidates` | — | Qué se puede relanzar y qué no. |
| POST | `/api/v1/database-clone-batches/{id}/retry-failed` | 3/min | Crea un lote NUEVO. **201.** |

---

## 3. `POST /database-clone-batches` — armar el plan

```jsonc
{
  "source_server_id": 3,
  "target_server_id": 7,
  "copy_intent": "structure_and_data",   // structure_only | structure_and_data | data_only
  "data_on_existing": null,              // OBLIGATORIO solo con data_only: append | upsert
  "structure": null,                     // CloneStructureSpec, igual que el clon individual
  "data": null,
  "target_charset": null,
  "rows": [
    { "source_database_name": "ventas",  "target_database_name": "stg_ventas",  "target_mode": "new" },
    { "source_database_name": "compras", "target_database_name": null,          "target_mode": "new" }
  ]
}
```

- `target_database_name: null` → **se usa el mismo nombre del origen**. Es el caso mayoritario.
- `source_database_id` (opcional, por fila): id del inventario si la base está adoptada.
- `overrides` (opcional, por fila): objeto con lo que esta fila le pisa al perfil global
  (`copy_intent`, `structure`, `data`, `data_on_existing`, `target_charset`, `target_collation`).
  **`clean_mode` dentro de `overrides` da 422**, igual que en el perfil.
- Tope de filas: `CLONE_BATCH_MAX_ROWS` (default 25).

### 3.1 Lo que rebota el lote entero (422) vs. lo que bloquea UNA fila

Es la parte del contrato que más cambia la UI, y no es arbitraria:

- **422 del lote entero** para lo que el operador corrige en el formulario, antes de que exista
  nada: lote vacío, tope excedido, nombres destino repetidos, `clean_mode` destructivo,
  `data_only` sin `on_existing`.
- **Fila con `status: "blocked"`** para lo que depende del estado del servidor y varía por base:
  el destino ya existe (con `new`), no existe (con `existing`), la base es de sistema o es la BD
  de metadatos del gateway, o el modo no es representable sobre un destino existente. **El lote
  se crea igual**, con esas filas marcadas y su `error_code`.

El motivo: rebotar la petición por el primer problema obliga a corregir un lote de 12 bases de a
una. La UI las muestra todas juntas.

**Excepción:** si NINGUNA fila queda ejecutable, sí es 422 `clone.batch_empty`, con
`public_context.blocked` listando el motivo de cada una.

### 3.2 Respuesta — `CloneBatchOut`

```jsonc
{
  "id": 12,
  "source_server_id": 3,
  "target_server_id": 7,
  "copy_intent": "structure_and_data",
  "data_on_existing": null,
  "target_charset": null,
  "target_collation": null,
  "total": 12,
  "confirm_token": "9f2c…",              // se reenvía tal cual en execute
  "status": "pending",                   // pending|running|done|partial|failed|interrupted|canceled
  "cancel_requested": false,
  "error": null,
  "counts": { "pending": 11, "blocked": 1, "total": 12 },
  "created_by_username": "admin",
  "created_at": "…", "expires_at": "…", "started_at": null, "finished_at": null
}
```

`counts` es la respuesta a **«¿4 de 12?»**. Viene **derivado en vivo** del estado de las filas —
se renderiza tal cual, no se re-suma en el cliente. Las claves son estados de fila (ver §5) más
`total`.

---

## 4. `POST .../execute` — la confirmación agregada

```jsonc
{ "confirm_server_name": "prod-mysql-01", "confirm_token": "9f2c…" }
```

**Un solo gesto para todo el lote: re-tipear el nombre del SERVIDOR destino.** No se re-tipea
cada base, y es deliberado: con doce bases, doce re-tipeos se vuelven copiar y pegar sin leer, y
además protegen el eje equivocado — en un lote el error catastrófico no es escribir mal un
nombre, es que la lista entera apunte al servidor que no era.

El otro eje lo cierra `confirm_token`, que es un **sha256 del conjunto ORDENADO** de
`(origen, destino, target_mode)` más el servidor destino. Se recomputa server-side sobre las
filas persistidas: agregar, quitar o editar una sola fila lo invalida
(409 `clone.batch_set_mismatch`).

| Código | Status | Cuándo |
|---|---|---|
| `clone.batch_not_pending` | 409 | El lote ya se ejecutó o ya terminó. |
| `clone.batch_expired` | 410 | Venció el TTL del plan. Hay que rearmarlo. |
| `clone.batch_set_mismatch` | 409 | El conjunto de filas cambió desde que se planeó. |
| `clone.batch_confirm_server_mismatch` | 422 | El nombre escrito no es el del servidor destino. |
| `clone.batch_token_mismatch` | 422 | El token no corresponde a este lote. |

---

## 5. `GET .../items` — `CloneBatchItemOut`

```jsonc
{
  "id": 101, "batch_id": 12, "seq": 3,
  "source_database_name": "ventas", "source_database_id": 44,
  "target_database_name": "stg_ventas", "target_mode": "new",
  "clone_job_id": 887,          // null = la fila todavía no se materializó en un job
  "status": "succeeded",
  "phase": "done", "progress": { "phase": "data", "tables": { "facturas": 12000 } },
  "error": null, "error_code": null,
  "started_at": "…", "finished_at": "…"
}
```

### 5.1 De dónde sale `status` — importa para no duplicar estado

El backend resuelve `COALESCE(job.status, item.outcome)`:

- Mientras `clone_job_id` es `null`, el estado es del ítem: `pending` | `blocked` | `skipped` |
  `canceled`.
- En cuanto hay job, el estado **es el del job**: `pending` | `running` | `succeeded` | `failed`
  | `interrupted` | `canceled`.

Por eso el enum que el cliente tiene que aceptar es la **unión de los dos**. El ítem nunca copia
el estado del job, así que no existen dos versiones del mismo dato que puedan divergir.

`clone_job_id` no nulo → hay una pantalla de detalle completa en
`/database-clones?jobId={clone_job_id}`. Conviene enlazarla: es donde están las sentencias, los
pasos y el progreso por tabla.

---

## 6. El reintento — y por qué son DOS grupos

`GET .../retry-candidates` devuelve `{ retryable: [...], needs_manual: [...] }`.

La regla es el **destino**, no el estado del job: el lote no puede limpiar nada, así que solo
sabe escribir sobre algo virgen.

- **`retryable`** — filas que nunca llegaron a tener job (bloqueadas, salteadas, canceladas
  antes de arrancar), que es el caso que importa después de un reinicio o una cancelación; y las
  que fallaron tan temprano que no dejaron rastro.
- **`needs_manual`** — con un `reason` en prosa, por dos motivos distintos:
  - **datos parciales**: la fila alcanzó a copiar filas. La copia hace commit por lote en
    AUTOCOMMIT y no es reanudable, así que reintentar agregaría encima y **duplicaría datos en
    silencio**.
  - **base creada a medias**: la fila creaba el destino y el intento anterior alcanzó a crearlo
    antes de fallar. Un reintento con el mismo modo se bloquearía por "el destino ya existe".

`POST .../retry-failed` arma un **lote nuevo** en `pending` con las `retryable`. Vuelve a exigir
la confirmación agregada a propósito: el estado de los servidores cambió desde el plan original.
Si no hay ninguna reintentable → 422 `clone.batch_retry_not_eligible`.

---

## 7. Lo que esto cambia en el asistente INDIVIDUAL

Nada del contrato. Lo que cambia es el uso: `ClonePreviewIn` acepta desde el trabajo de
solo-datos un **idioma declarativo** que la SPA nunca adoptó, y que es lo que convierte «copiar
todas las tablas» en un click en vez de doscientos:

```jsonc
{ "structure": { "mode": "all", "types": ["table"], "names": [],
                 "include_patterns": ["fact_*"], "exclude_patterns": ["*_tmp"] } }
```

**Dos trampas que el cliente tiene que respetar:**

1. `selection` y `structure` son **mutuamente excluyentes**, y el backend valida sobre las
   claves REALMENTE enviadas — mandar `selection: null` acompañando a `structure` ya cuenta
   como enviar las dos y responde 422.
2. El orden de resolución es tipos → `mode`/`names` → patrones de inclusión → patrones de
   exclusión. Con `mode: "include"` y `names: []` el conjunto base es **vacío**, así que unos
   `include_patterns` sin `names` no seleccionan nada. Para «los objetos que matcheen X» el modo
   correcto es **`all`** con `include_patterns`.

---

## 8. Vocabulario de códigos nuevos

Todos viajan en `detail.public_context.code` (visible SIEMPRE, también en producción) y
comparten el namespace `clone.*` porque el lote es la orquestación del mismo módulo, no otro.

| Código | Texto sugerido |
|---|---|
| `clone.batch_destructive_not_allowed` | Un lote no puede borrar el destino. Para reemplazar una base existente, usá el asistente de a una. |
| `clone.batch_existing_requires_data_only` | Sobre una base que ya existe el lote solo puede copiar datos: no borra, así que la estructura tiene que estar creada. |
| `clone.batch_duplicate_target` | Dos bases del lote apuntan al mismo destino. Cambiá uno de los dos nombres. |
| `clone.batch_target_exists` | Esa base ya existe en el destino. Cambiá el nombre, o usá la existente y copiá solo datos. |
| `clone.batch_target_missing` | Esa base no existe en el destino. |
| `clone.batch_empty` | Ninguna de las bases seleccionadas se puede clonar con esta configuración. |
| `clone.batch_too_large` | El lote supera el tope de bases. Dividilo. (`max_rows`, `requested`) |
| `clone.batch_set_mismatch` | El conjunto de bases cambió. Volvé a armar el lote. |
| `clone.batch_not_pending` | El lote ya no está pendiente. |
| `clone.batch_expired` | El plan del lote expiró. |
| `clone.batch_token_mismatch` | El token de confirmación no corresponde a este lote. |
| `clone.batch_confirm_server_mismatch` | El nombre del servidor destino no coincide. |
| `clone.batch_row_blocked` | Esta base no se pudo preparar. |
| `clone.batch_retry_not_eligible` | No hay filas reintentables: las fallidas tocaron el destino. |

Una fila bloqueada también puede traer códigos que ya existían: `clone.scope_not_allowed`,
`clone.same_database`, `clone.source_not_found`, `clone.target_schema_incompatible`.

---

## 9. Durabilidad — el límite que hay que mostrar, no esconder

El worker es in-process (`P-24`): **si el proceso se reinicia, el lote queda `interrupted`** y no
se reanuda solo. Al arrancar, el barrido lo cierra y marca `skipped` las filas que no llegaron a
correr; de ahí sale «reintentar las que faltaron», que cubre justamente ese caso.

No se re-encola automáticamente a propósito: el fingerprint del origen pudo cambiar y el token
autorizaba otro conjunto. Reanudar una parte de un lote sobre bases de terceros sin que un
humano lo confirme es el default equivocado.

**Requisito operativo:** mientras exista el lote, `WORKERS=1`. El barrido marca `interrupted`
todo lo que esté `running` sin distinguir de qué proceso es, y un lote de horas agranda
muchísimo la ventana en la que dos procesos se pisan.
