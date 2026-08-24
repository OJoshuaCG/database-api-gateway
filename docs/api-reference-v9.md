# API Reference v9 — Captura de resultados de `SELECT` dentro de una migración

> ⚠️ **Parcialmente superado por [`api-reference-v13.md`](api-reference-v13.md).** El
> **consentimiento por corrida** (`allow_result_capture`) se eliminó: las "tres llaves" de §2 son
> ahora **dos** (opt-in + revisión), y el `409` con `public_context.capture_versions` de §3.2/§3.3
> ya no existe. El 409 que queda es el de captura **sin revisar**, y ahora lleva
> `public_context.code`. Este documento se conserva sin reescribir porque es el registro de por
> qué la feature se diseñó así; para lo que rige hoy, v13.


> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md),
> [`api-reference-v2.md`](api-reference-v2.md), [`api-reference-v3.md`](api-reference-v3.md),
> [`api-reference-v4.md`](api-reference-v4.md), [`api-reference-v5.md`](api-reference-v5.md),
> [`api-reference-v6.md`](api-reference-v6.md), [`api-reference-v7.md`](api-reference-v7.md) y
> [`api-reference-v8.md`](api-reference-v8.md).
>
> Como **v6** y **v8**: describe un módulo **NUEVO que nunca fue expuesto al frontend**. No hay
> pantalla existente que ajustar — hay que diseñar una desde cero, y hay que **modificar** las
> pantallas de `apply`/`rollback`/PATCH de migraciones que ya existen (§1.1). El backend ya está
> implementado y commiteado.
>
> ⚠️ **Feature P0 de seguridad, no un capricho de UX.** Este módulo es la **primera excepción
> deliberada** a la regla "el gateway nunca almacena datos de negocio de las bases que gestiona".
> Por eso lleva tres capas de confirmación superpuestas (§2) y la UI tiene que transmitirlas
> **todas**, no solo la que sea más cómoda de mostrar.
>
> Mismo formato que v3–v8: **problema → qué debe pasar → escenarios → flujos → endpoints →
> ejemplos → interpretación visual**.
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie, errores,
> paginación) idénticas al documento original ([§3](api-reference.md#3-convenciones-de-la-api)) y
> a las precisiones de [`api-reference-v8.md` §3.0](api-reference-v8.md#30-envelope-y-errores-tres-precisiones-que-valen-para-todo-el-módulo)
> (**sin `success`**, errores en producción solo con `detail.msg`, `X-Request-ID` en el header).
> Este módulo agrega **una precisión propia** en [§3.0](#30-una-precisión-propia-public_context-en-los-409-de-este-módulo).
>
> Documentación de ingeniería del mismo módulo (más detalle interno del que el frontend
> necesita): [`docs/features/model-migrations.md`](features/model-migrations.md).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## Índice

- [0. El problema: por qué existe este módulo](#0-el-problema-por-qué-existe-este-módulo)
- [1. Alcance: qué cubre y qué NO cubre](#1-alcance-qué-cubre-y-qué-no-cubre)
  - [1.1 Pantallas EXISTENTES que este módulo modifica](#11-pantallas-existentes-que-este-módulo-modifica)
- [2. Las tres llaves (y el kill switch que las apaga a todas)](#2-las-tres-llaves-y-el-kill-switch-que-las-apaga-a-todas)
  - [2.1 Diagrama de las tres llaves](#21-diagrama-de-las-tres-llaves)
  - [2.2 El gate de revisión es de ALCANCE ACOTADO, no de todo el blueprint](#22-el-gate-de-revisión-es-de-alcance-acotado-no-de-todo-el-blueprint)
  - [2.3 Editar el SQL revoca la revisión](#23-editar-el-sql-revoca-la-revisión)
- [3. Los endpoints](#3-los-endpoints)
  - [3.0 Una precisión propia: `public_context` en los 409 de este módulo](#30-una-precisión-propia-public_context-en-los-409-de-este-módulo)
  - [3.1 `PATCH /database-models/{model_id}/migrations/{version}`](#31-patch-database-modelsmodel_idmigrationsversion-)
  - [3.2 `POST /managed-databases/{db_id}/migrations/apply`](#32-post-managed-databasesdb_idmigrationsapply-)
  - [3.3 `POST /managed-databases/{db_id}/migrations/rollback`](#33-post-managed-databasesdb_idmigrationsrollback-)
  - [3.4 `POST /managed-databases/{db_id}/migrations/stamp`](#34-post-managed-databasesdb_idmigrationsstamp-)
  - [3.5 `GET /managed-databases/{db_id}/migrations/{version}/select-results`](#35-get-managed-databasesdb_idmigrationsversionselect-results-)
  - [3.6 `DELETE /managed-databases/{db_id}/migrations/{version}/select-results`](#36-delete-managed-databasesdb_idmigrationsversionselect-results-)
  - [3.7 `POST /database-models/{model_id}/migrations/apply-all`](#37-post-database-modelsmodel_idmigrationsapply-all-)
- [4. Semántica de validación](#4-semántica-de-validación)
  - [4.1 `capture_selects` es opt-in, `reviewed` no se elige directo](#41-capture_selects-es-opt-in-reviewed-no-se-elige-directo)
  - [4.2 Los punteros de `apply`/`rollback` NUNCA traen filas](#42-los-punteros-de-applyrollback-nunca-traen-filas)
  - [4.3 `durability`: por qué una fila puede describir un dato que ya no existe](#43-durability-por-qué-una-fila-puede-describir-un-dato-que-ya-no-existe)
  - [4.4 Un `SELECT` con comentario delante SÍ se captura](#44-un-select-con-comentario-delante-sí-se-captura)
  - [4.5 `stale` y `missing_indexes` en la lectura](#45-stale-y-missing_indexes-en-la-lectura)
  - [4.6 TTL y purga: las filas no viven para siempre](#46-ttl-y-purga-las-filas-no-viven-para-siempre)
- [5. Flujos completos, paso a paso](#5-flujos-completos-paso-a-paso)
  - [5.1 Camino feliz: crear, revisar, aplicar, leer](#51-camino-feliz-crear-revisar-aplicar-leer)
  - [5.2 Bloqueo por falta de revisión](#52-bloqueo-por-falta-de-revisión)
  - [5.3 Bloqueo por falta de consentimiento en la corrida](#53-bloqueo-por-falta-de-consentimiento-en-la-corrida)
  - [5.4 Editar el SQL después de aprobar](#54-editar-el-sql-después-de-aprobar)
  - [5.5 Rollback de recuperación con una versión futura sin revisar](#55-rollback-de-recuperación-con-una-versión-futura-sin-revisar)
  - [5.6 Kill switch apagado](#56-kill-switch-apagado)
- [6. Interpretación visual: pantallas y estados](#6-interpretación-visual-pantallas-y-estados)
- [7. Tipos (referencia rápida)](#7-tipos-referencia-rápida)
- [8. Matriz de errores](#8-matriz-de-errores)
- [9. Checklist de implementación](#9-checklist-de-implementación)

---

## 0. El problema: por qué existe este módulo

Una migración que **verifica** algo antes de decidir qué hacer —¿quedaron filas sin backfill?,
¿hay duplicados que van a romper un `UNIQUE`?— casi siempre lleva un `SELECT` de diagnóstico.
Hasta ahora, ese `SELECT` se ejecutaba contra el motor destino y **el resultado se tiraba**:
Alembic no devuelve nada de un `SELECT` suelto, y el gateway solo informaba "aplicada" o
"falló" — justo en el momento en que lo que se quería mirar **ya cambió** (la migración ya
corrió, los datos que motivaron el `SELECT` pueden haber sido tocados por la propia migración).

Este módulo agrega la opción de **capturar** ese resultado — columnas y filas — y guardarlo
cifrado en el gateway, disponible por un endpoint auditado, con TTL. Es la única forma de que un
operador vea después *"che, la migración detectó 12 filas sin `updated_at`"* en vez de tener que
reconstruir la consulta a mano y confiar en que nada cambió mientras tanto.

**El costo de esto es que el gateway pasa a guardar datos de la base del cliente**, algo que
hasta ahora nunca hacía. Por eso la feature es **opt-in por versión**, exige **revisión humana**
del SQL antes de poder ejecutarse, y exige **consentimiento explícito en cada corrida** — porque
un mismo blueprint de migraciones se aplica sobre N bases de dueños distintos, y aprobar la
migración una vez no es lo mismo que autorizar la extracción de filas de *esta* base en
*particular*.

**Actor:** el admin único del gateway (sesión admin por cookie). No hay roles ni multi-tenant.

---

## 1. Alcance: qué cubre y qué NO cubre

### Cubre

- **Marcar una versión de migración** para que capture el resultado de sus sentencias `SELECT`
  (`capture_selects`), tanto en `up` como en `down`.
- **Aprobar/rechazar** esa captura (`reviewed`) antes de que pueda ejecutarse.
- **Dar o negar consentimiento por corrida** (`allow_result_capture`) al aplicar o revertir.
- **Leer** las filas capturadas de una versión ya aplicada/revertida en una BD puntual.
- **Purgar** esas filas a demanda, además del TTL automático.
- Punteros de cuántas filas se capturaron en la última corrida, sin exponer el contenido en la
  respuesta de `apply`/`rollback`.

### NO cubre

- **No hay edición de las filas capturadas.** Es una foto de solo lectura; la única escritura
  posible es el `DELETE` que las purga todas.
- **No hay búsqueda ni filtro sobre las filas capturadas.** `GET .../select-results` devuelve
  **todo** lo capturado en la corrida más reciente de esa versión, sin paginar.
- **No hay export (CSV, etc.).** Si la UI lo necesita, es trabajo de frontend sobre el JSON que
  ya llega — el backend no lo ofrece.
- **No captura nada que no sea `SELECT`/`WITH`/`TABLE`/`VALUES`.** Un `SHOW`, un `EXPLAIN` o
  cualquier DML no se captura nunca, tenga o no `capture_selects=true` (§4.4).
- **No es un mecanismo de auditoría de todo lo que corrió.** Solo guarda el **resultado**, no el
  plan de ejecución ni métricas de performance.
- **No hay histórico multi-corrida.** El endpoint de lectura muestra la corrida MÁS RECIENTE; si
  la versión se aplica, se revierte y se vuelve a aplicar, lo anterior se sobrescribe (ver
  [§4.6](#46-ttl-y-purga-las-filas-no-viven-para-siempre)).

### 1.1 Pantallas EXISTENTES que este módulo modifica

Esto **no es un módulo aislado**: toca tres flujos que el frontend ya tiene construidos. Si la
UI de creación/edición de migraciones y de `apply`/`rollback` no se actualiza, la feature queda
invisible y el operador se va a topar con `409` que no sabe interpretar.

| Pantalla existente | Qué cambia | Documentado en |
|---|---|---|
| Crear/editar una migración | Nuevo campo `capture_selects` en el formulario; nuevo campo de solo lectura `reviewed` con acción de aprobar | [§3.1](#31-patch-database-modelsmodel_idmigrationsversion-) |
| Diálogo de `apply`/`apply-all` | Nuevo parámetro `allow_result_capture`; nuevos campos de respuesta `captured_select_count` / `select_results_available` | [§3.2](#32-post-managed-databasesdb_idmigrationsapply-), [§3.7](#37-post-database-modelsmodel_idmigrationsapply-all-) |
| Diálogo de `rollback` | Idéntico a `apply`: `allow_result_capture` + los mismos dos campos de respuesta | [§3.3](#33-post-managed-databasesdb_idmigrationsrollback-) |
| Diálogo de `stamp` | Nuevo `409` posible si la versión tiene captura sin revisar (con `force` como escape) | [§3.4](#34-post-managed-databasesdb_idmigrationsstamp-) |

---

## 2. Las tres llaves (y el kill switch que las apaga a todas)

### 2.1 Diagrama de las tres llaves

```
capture_selects=true (opt-in, por versión)
        │
        ▼
  nace reviewed=false ──────► 409 en apply / apply-all / rollback / stamp
        │                     hasta que un admin hace
        │                     PATCH reviewed=true
        ▼
  admin aprueba (reviewed=true)
        │
        ▼
  cada llamada a apply/rollback exige
  allow_result_capture=true ─────────► 409 si falta (antes de tocar el motor)
        │
        ▼
  se ejecuta, se capturan las filas, se cifran, quedan disponibles
  por GET .../select-results hasta que expiran (TTL) o se purgan
```

Un kill switch de servidor, `MIGRATION_CAPTURE_ENABLED`, **desactiva las tres llaves a la vez**
cuando está en `false`: el codegen deja de emitir la instrucción de captura (capturar pasa a ser
**físicamente imposible**) y por lo tanto los gates de revisión y de consentimiento también se
saltean — no tendría sentido bloquear algo que no puede pasar. Es una variable de entorno; la UI
no la controla, pero **debe saber que existe** porque cambia el comportamiento observable
([§5.6](#56-kill-switch-apagado)).

### 2.2 El gate de revisión es de ALCANCE ACOTADO, no de todo el blueprint

> 🚨 **El 409 de "versiones sin revisar" nombra solo las versiones relevantes a ESA llamada, no
> todo el blueprint.**

- En `apply`/`apply-all`: solo las versiones **pendientes de aplicar en esa BD** (con
  `version=X`, un prefijo estricto hasta X — no todo lo que exista en el blueprint).
- En `rollback`: solo las versiones **en el camino a revertir** hacia `target_version`.

Es deliberado: el `rollback` es la vía de **recuperación** cuando una migración salió mal. Si el
gate mirara el blueprint completo, una versión futura sin revisar —que ni siquiera se va a
ejecutar en esa corrida— le quitaría al operador su única salida. Mismo criterio para
`dry_run=true`: **nunca dispara ninguno de los dos gates**, porque no ejecuta nada.

### 2.3 Editar el SQL revoca la revisión

Aprobar `reviewed=true` sobre un SQL y después cambiarlo (`PATCH up_sql`, `down_sql`, o los
overrides por motor) **resetea `reviewed` a `false`** automáticamente, siempre que
`capture_selects` esté activo (el que ya tenía la versión, o el que se active en el mismo
`PATCH`). Nadie debería poder aprobar `SELECT 1` y terminar ejecutando `SELECT * FROM clientes`
sin que alguien vuelva a mirar. Ver [§5.4](#54-editar-el-sql-después-de-aprobar) para el flujo
completo, incluida la excepción de `down_sql` sobre una migración ya aplicada.

---

## 3. Los endpoints

| # | Endpoint | Rate limit | Toca el motor |
|---|---|---|---|
| 1 | `PATCH /database-models/{model_id}/migrations/{version}` | *(global)* | — |
| 2 | `POST /managed-databases/{db_id}/migrations/apply` | `10/minute` | 🔌 |
| 3 | `POST /managed-databases/{db_id}/migrations/rollback` | `10/minute` | 🔌 |
| 4 | `POST /managed-databases/{db_id}/migrations/stamp` | `10/minute` | — |
| 5 | `GET /managed-databases/{db_id}/migrations/{version}/select-results` | `20/minute` | — |
| 6 | `DELETE /managed-databases/{db_id}/migrations/{version}/select-results` | *(global)* | — |
| 7 | `POST /database-models/{model_id}/migrations/apply-all` | `3/minute` | 🔌 |

**Todos requieren sesión admin** (🔒). Sin sesión: `401`. El `{version}` siempre valida contra el
patrón `^\d{4,10}$` — un valor con otro formato es `422` de validación, no `404`.

> Los endpoints 2–4 y 7 **ya existen** (documentados en versiones anteriores de esta serie);
> acá solo se documentan los campos **nuevos** que trae esta feature. Los 5 y 6 son
> **completamente nuevos**.

### 3.0 Una precisión propia: `public_context` en los 409 de este módulo

Sumado a las tres precisiones de [`api-reference-v8.md` §3.0`](api-reference-v8.md#30-envelope-y-errores-tres-precisiones-que-valen-para-todo-el-módulo)
(sin `success`, `detail.context` es dev-only, `X-Request-ID` en el header):

**A diferencia de la mayoría de los módulos de este gateway, los `409` de captura SÍ traen
`public_context` — y viaja en todos los ambientes, no solo en `development`.** Es información
que la UI puede usar sin una segunda llamada:

| Error | `public_context` | Para qué sirve |
|---|---|---|
| Versiones sin revisar bloqueando `apply`/`apply-all`/`rollback` | `{"unreviewed_capture": ["0007", "0009"]}` | Resaltar EXACTAMENTE esas versiones en la lista, con un botón directo a "revisar y aprobar" |
| Falta consentimiento de corrida | `{"capture_versions": ["0007"]}` | Mostrar cuáles de las versiones que se van a tocar son las que activan el aviso (puede ser un subconjunto de las pendientes) |
| `stamp` sobre versión con captura sin revisar | `{"unreviewed_capture": ["0009"]}` | Mismo patrón, un solo elemento |

> El resto de los `409`/`404` de este módulo (versión inexistente, BD en cuarentena, edición de
> SQL ya aplicado) **no traen `public_context` propio de esta feature** — son los mismos errores
> genéricos de migraciones que ya existían.

---

### 3.1 `PATCH /database-models/{model_id}/migrations/{version}` 🔒

Edita una migración del blueprint. **Rate limit global.** Esta feature agrega dos campos al
body y una regla de reseteo automático.

**Body (campos nuevos de esta feature)**

| Campo | Tipo | Req. | Nota |
|---|---|---|---|
| `capture_selects` | `boolean \| null` | ❌ | Activa/desactiva la captura para esta versión. `null` = no tocar el valor actual |
| `reviewed` | `boolean \| null` | ❌ | Aprobar (`true`) o desaprobar (`false`) manualmente. `null` = no tocar |

> ⚠️ **El reset automático de `reviewed` a `false` PISA lo que se envíe en el mismo `PATCH`.**
> Si en la misma llamada se manda `up_sql` (o `down_sql`, o un override) **y** `reviewed: true`,
> gana el reset: la versión queda `reviewed: false` en la respuesta. No es un bug — es la regla
> de [§2.3](#23-editar-el-sql-revoca-la-revisión) aplicándose antes de que el valor enviado
> tenga efecto. Si la UI ofrece "editar y aprobar" en un solo paso, tiene que avisar que el
> segundo aprobado no sirve y hace falta un `PATCH` separado después de revisar el diff real.

#### Ejemplo A — activar la captura en una versión nueva

```http
PATCH /api/v1/database-models/12/migrations/0007
Content-Type: application/json

{ "capture_selects": true }
```

```json
{
  "data": {
    "id": 812,
    "model_id": 12,
    "version": "0007",
    "up_sql": "-- verificar filas sin backfill\nSELECT id, created_at FROM clientes WHERE migrated_at IS NULL",
    "down_sql": "SELECT 1",
    "capture_selects": true,
    "reviewed": false,
    "created_at": "2026-08-14T08:00:00Z"
  }
}
```

- **`reviewed` pasó a `false` aunque no se haya enviado ese campo.** Es el efecto del opt-in: la
  primera vez que `capture_selects` se activa, la versión nace sin revisar.

#### Ejemplo B — aprobar después de revisar el SQL

```http
PATCH /api/v1/database-models/12/migrations/0007
Content-Type: application/json

{ "reviewed": true }
```

```json
{
  "data": {
    "id": 812,
    "model_id": 12,
    "version": "0007",
    "up_sql": "-- verificar filas sin backfill\nSELECT id, created_at FROM clientes WHERE migrated_at IS NULL",
    "down_sql": "SELECT 1",
    "capture_selects": true,
    "reviewed": true,
    "created_at": "2026-08-14T08:00:00Z"
  }
}
```

#### Errores

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `409` | `"La migración ya fue aplicada exitosamente en alguna BD: no se puede modificar su SQL. Cree una nueva migración para corregir (fix-forward)."` | Se intenta cambiar `up_sql`/`down_sql`/overrides de una versión con aplicación exitosa registrada. `capture_selects`/`reviewed` **solos** sí se pueden cambiar en cualquier momento |
| `404` | *(mensaje genérico de migración no encontrada)* | `model_id`/`version` inválidos |

---

### 3.2 `POST /managed-databases/{db_id}/migrations/apply` 🔌🔒

Aplica las migraciones pendientes sobre una base puntual. **Rate limit `10/minute`.** Esta
feature agrega un query param y dos campos de respuesta.

**Query (nuevo)**

| Param | Tipo | Default | Nota |
|---|---|---|---|
| `allow_result_capture` | `boolean` | `false` | Consentimiento explícito para esta corrida. Ver [§2](#2-las-tres-llaves-y-el-kill-switch-que-las-apaga-a-todas) |

**Response (campos nuevos)**

| Campo | Tipo | Nota |
|---|---|---|
| `captured_select_count` | `number` | Filas escritas por ESTA corrida (no un acumulado histórico — ver [§4.2](#42-los-punteros-de-applyrollback-nunca-traen-filas)). `0` si no se capturó nada |
| `select_results_available` | `boolean` | `true` si hay algo para leer con [§3.5](#35-get-managed-databasesdb_idmigrationsversionselect-results-) |

```http
POST /api/v1/managed-databases/87/migrations/apply?version=0007&allow_result_capture=true
```

```json
{
  "message": "Migración aplicada.",
  "data": {
    "database_id": 87,
    "applied_versions": ["0006", "0007"],
    "current_version": "0007",
    "status": "active",
    "captured_select_count": 12,
    "select_results_available": true
  }
}
```

> **`select_results_available: true` NO garantiza filas > 0.** Un `SELECT` que devolvió cero
> filas también queda "disponible para leer" — la lectura va a traer `items` con `row_count: 0`,
> no un 404. La UI debe distinguir "no se capturó nada porque `capture_selects=false`" (este
> campo en `false`) de "se capturó y no había filas" (este campo en `true`, `captured_select_count`
> puede ser `0` igual si el `SELECT` no devolvió filas pero sí generó un registro de captura).

#### Errores (nuevos de esta feature — los genéricos de `apply` ya estaban documentados)

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `409` | `"El blueprint tiene versiones con captura de resultados SIN revisar ({versions}). Esas migraciones guardan el resultado de sus SELECT (datos de la BD destino) en el gateway: revisá qué consultan y aprobalas (PATCH reviewed=true) antes de aplicar o revertir."` | Alguna versión pendiente en el camino de esta corrida tiene `capture_selects=true` y `reviewed=false`. `public_context: {unreviewed_capture: [...]}` |
| `409` | `"Confirmación requerida: las versiones pendientes {versions} tienen la captura de resultados activada, así que esta aplicación va a guardar filas de esta base de datos (cifradas) en el gateway. Repetí la llamada con allow_result_capture=true si es lo que querés, o desactivá capture_selects en esas versiones."` | Hay versiones con `capture_selects=true` en el camino y no se envió `allow_result_capture=true`. `public_context: {capture_versions: [...]}` |

Ambos se saltean por completo si `MIGRATION_CAPTURE_ENABLED=False` en el servidor
([§5.6](#56-kill-switch-apagado)). Ninguno de los dos aplica con `dry_run=true`.

---

### 3.3 `POST /managed-databases/{db_id}/migrations/rollback` 🔌🔒

Revierte migraciones sobre una base puntual. **Rate limit `10/minute`.** Mismo patrón que
`apply`: nuevo query param, dos campos de respuesta nuevos.

**Query (nuevo)**

| Param | Tipo | Default | Nota |
|---|---|---|---|
| `allow_result_capture` | `boolean` | `false` | Igual que en `apply`, pero evaluado sobre el camino de REVERSIÓN, no de aplicación |

**Response (campos nuevos):** idénticos a `apply` — `captured_select_count`,
`select_results_available`.

```http
POST /api/v1/managed-databases/87/migrations/rollback?confirm_version=0007&allow_result_capture=true
```

```json
{
  "message": "Rollback aplicado.",
  "data": {
    "database_id": 87,
    "rolled_back_versions": ["0007"],
    "current_version": "0006",
    "status": "active",
    "captured_select_count": 1,
    "select_results_available": true
  }
}
```

#### Errores (nuevos de esta feature)

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `409` | `"El blueprint tiene versiones con captura de resultados SIN revisar ({versions})…"` (mismo texto que en `apply`, `public_context: {unreviewed_capture: [...]}`) | Alguna versión en el camino **a revertir** (no todo el blueprint — [§2.2](#22-el-gate-de-revisión-es-de-alcance-acotado-no-de-todo-el-blueprint)) tiene captura sin revisar |
| `409` | `"Confirmación requerida: las versiones a revertir {versions} tienen la captura de resultados activada…"` (texto adaptado a "revertir", `public_context: {capture_versions: [...]}`) | Falta `allow_result_capture=true` para el rollback |

> ⚠️ **El texto del segundo 409 dice "revertir", no "aplicar".** Es a propósito
> ([§2](#2-las-tres-llaves-y-el-kill-switch-que-las-apaga-a-todas)): si la UI arma un mensaje
> genérico en vez de mostrar `detail.msg` tal cual, va a mandar al operador al botón equivocado
> (uno cree que tiene que ir a la pantalla de `apply`).

---

### 3.4 `POST /managed-databases/{db_id}/migrations/stamp` 🔌🔒

Marca una BD en una versión sin ejecutar SQL. **Rate limit `10/minute`.** Esta feature agrega
una defensa en profundidad: el `stamp` no ejecuta nada, pero es lo que **habilita** un `rollback`
posterior sobre esa versión.

```http
POST /api/v1/managed-databases/87/migrations/stamp?version=0007
```

#### Error nuevo de esta feature

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `409` | `"...tiene captura de resultados sin revisar. Revisá el SQL y aprobala (PATCH reviewed=true) antes de marcarla."` | La versión a stampear tiene `capture_selects=true` y `reviewed=false`. `public_context: {unreviewed_capture: [version]}` |

> **Tiene `force=true` como escape**, a diferencia de `apply`/`rollback`. Es necesario: una
> versión aplicada hace meses, a la que después se le activó `capture_selects`, queda
> `reviewed=false` retroactivamente; y una BD que perdió su puntero de versión (por fuera del
> control normal del gateway) tiene que poder re-stampearse igual. `force=true` en este endpoint
> **no habilita la captura** — solo permite marcar el puntero de versión; la captura real sigue
> dependiendo de `reviewed=true` y `allow_result_capture=true` en el `apply`/`rollback` que
> venga después.

---

### 3.5 `GET /managed-databases/{db_id}/migrations/{version}/select-results` 🔒

**Nuevo.** Lee lo capturado en la corrida más reciente de esa versión sobre esa BD. **Rate limit
`20/minute`.** No pagina — trae todo de una vez.

```http
GET /api/v1/managed-databases/87/migrations/0007/select-results
```

```json
{
  "data": {
    "managed_database_id": 87,
    "database_name": "clientes_prod",
    "server_id": 3,
    "model_migration_id": 812,
    "version": "0007",
    "capture_selects": true,
    "stale": false,
    "expected_indexes": [0],
    "missing_indexes": [],
    "durability_warning": null,
    "items": [
      {
        "statement_index": 0,
        "direction": "up",
        "sql": "-- verificar filas sin backfill\nSELECT id, created_at FROM clientes WHERE migrated_at IS NULL",
        "sql_hash": "9f2c4b1e8a7d6c5b",
        "status": "ok",
        "durability": "committed",
        "columns": ["id", "created_at"],
        "rows": [
          [101, "2026-08-10T14:22:00Z"],
          [102, "2026-08-11T09:03:12Z"]
        ],
        "row_count": 2,
        "truncated": false,
        "payload_bytes": 118,
        "error": null,
        "captured_at": "2026-08-14T10:15:03Z",
        "migration_checksum": "a1b2c3d4e5f6..."
      }
    ]
  }
}
```

#### Los campos que necesitan explicación

| Campo | Qué significa realmente |
|---|---|
| `rows` | **Listas posicionales, no objetos.** El orden corresponde 1:1 con `columns`. `rows[i][j]` es la columna `columns[j]` de la fila `i`. No llega como `[{"id": 101, ...}]` |
| `status` | `"ok"` \| `"error"`. Un `"error"` significa que la captura de ESE statement falló (best-effort) — **la migración siguió corriendo igual**. El `error` acota el motivo, nunca es el texto crudo del motor |
| `durability` | `"committed"` \| `"rolled_back"` \| `"unknown"`. Ver [§4.3](#43-durability-por-qué-una-fila-puede-describir-un-dato-que-ya-no-existe) — **es el campo más importante de leer antes de confiar en una fila** |
| `truncated` | `true` si se alcanzó `MIGRATION_CAPTURE_MAX_ROWS`, `MIGRATION_CAPTURE_MAX_BYTES` o `MIGRATION_CAPTURE_MAX_CELL_CHARS` en algún punto. Las filas que SÍ llegaron son reales — no se descartan, solo se avisa que hay más |
| `expected_indexes` / `missing_indexes` | Ver [§4.5](#45-stale-y-missing_indexes-en-la-lectura) |
| `stale` | `true` si el SQL de la versión cambió DESPUÉS de esta captura (el `migration_checksum` guardado no coincide con el actual). La captura sigue siendo la que se hizo, pero ya no describe el SQL vigente |

> 🚨 **No hay `direction: "up"` y `direction: "down"` mezclados con significado de "aplicado Y
> revertido".** `items` refleja **la corrida más reciente únicamente**: si la versión está
> aplicada, son las capturas del `up`; si se revirtió después, son las del `down` (y las del
> `up` de esa corrida específica ya no están, se sobrescribieron). No asumir histórico.

**Errores:** `404` `"La versión {version} no existe en el blueprint de esta BD."` · `401` sin
sesión · `429` rate limit `20/minute`. **No hay `410` ni `409`** — a diferencia de otros módulos
de este gateway, leer una captura vieja o expirada simplemente devuelve `items: []` (ver
[§4.6](#46-ttl-y-purga-las-filas-no-viven-para-siempre)), nunca un error por vencimiento.

---

### 3.6 `DELETE /managed-databases/{db_id}/migrations/{version}/select-results` 🔒

**Nuevo.** Purga a demanda las filas capturadas de esa versión en esa BD, sin esperar el TTL.
*(Sin rate limit propio.)*

```http
DELETE /api/v1/managed-databases/87/migrations/0007/select-results
```

```json
{ "message": "Resultados capturados eliminados.", "data": null }
```

- **Idempotente.** Si no había nada capturado, igual responde `200` — no es un `404`.
- **No afecta el estado de la migración.** `applied_versions`, `current_version`, `reviewed`,
  `capture_selects` quedan intactos. Solo borra las filas.
- **Irreversible.** No hay papelera ni confirmación de dos pasos a nivel API — si la UI quiere
  un diálogo de confirmación, es responsabilidad del frontend, el backend lo ejecuta directo.

**Errores:** `404` `"La versión {version} no existe en el blueprint de esta BD."` · `401` sin
sesión.

---

### 3.7 `POST /database-models/{model_id}/migrations/apply-all` 🔌🔒

Aplica el blueprint completo sobre TODAS las BDs asociadas al modelo. **Rate limit `3/minute` —
el más restrictivo del módulo.** Mismo campo nuevo que `apply`.

**Query (nuevo)**

| Param | Tipo | Default | Nota |
|---|---|---|---|
| `allow_result_capture` | `boolean` | `false` | Se evalúa **por BD**, no una vez para todo el lote |

> 🚨 **El 409 de consentimiento/revisión sale POR BASE DE DATOS, no frena el lote entero.** Si
> el blueprint tiene una versión con captura sin revisar y eso bloquea la BD #3 de 10, las BDs
> #1, #2 y #4–#10 **igual se aplican** (o fallan por sus propios motivos) — la respuesta trae un
> resultado por BD, y la de la #3 es la que lleva el `409` de este módulo. La UI del lote **no
> debe interpretar "hay un 409 de captura" como "no se aplicó nada"**.

Estructura de respuesta y demás errores genéricos de `apply-all`: sin cambios respecto a lo ya
documentado en versiones anteriores de esta serie — acá solo aplica el matiz de arriba.

---

## 4. Semántica de validación

### 4.1 `capture_selects` es opt-in, `reviewed` no se elige directo

Una migración "normal" (sin tocar `capture_selects`) nace `reviewed: true` — no hay nada que
revisar porque no se está guardando nada. `reviewed` solo se fuerza a `false` **cuando
`capture_selects` pasa a `true`**, ya sea en la creación o en un `PATCH` posterior. No hay forma
de crear una migración con `capture_selects=true` y `reviewed=true` en la misma llamada: el
controller ignora ese `reviewed` de entrada y fuerza `false` (ver el reset de
[§2.3](#23-editar-el-sql-revoca-la-revisión)).

### 4.2 Los punteros de `apply`/`rollback` NUNCA traen filas

`captured_select_count` es un **contador de escritura de ESA corrida**, no un acumulado
histórico. Si una versión se aplicó con captura, se revirtió (`down_sql` sin lecturas) y
`captured_select_count` del rollback da `0`, es correcto — no un bug. Esto es deliberado, no un
descuido: mostrar las filas en la respuesta de `apply`/`rollback` las mandaría directo al log si
`LOGGER_MIDDLEWARE_SHOW_BODY=true` está activo en el servidor. La única vía de lectura es
[§3.5](#35-get-managed-databasesdb_idmigrationsversionselect-results-), que pasa por auditoría.

### 4.3 `durability`: por qué una fila puede describir un dato que ya no existe

La persistencia difiere por motor, y **cambia lo que una fila capturada significa**:

- **MySQL/MariaDB (autocommit):** cada captura se escribe con una sesión corta propia,
  **inmediatamente**. `durability: "committed"` siempre que `status: "ok"`.
- **PostgreSQL (transaccional):** las capturas se acumulan en un buffer en memoria durante toda
  la migración y se vuelcan recién cuando `upgrade`/`downgrade` termina (éxito o error).
  - Si la migración completa con éxito → `durability: "committed"`.
  - **Si la migración FALLA y hace rollback** → las capturas de ESA corrida se marcan
    `durability: "rolled_back"` **pero igual se guardan y quedan legibles**.

> 🚨 **Una fila con `durability: "rolled_back"` describe datos de un `SELECT` que corrió dentro
> de una transacción que el motor deshizo.** No es un error del gateway ni un dato corrupto: es
> información real y útil para diagnosticar por qué falló la migración — pero **la UI tiene que
> mostrar esto de forma explícita** ("estos datos son de un intento que se revirtió"), porque un
> operador que lo lea sin ese contexto puede asumir que esas filas siguen existiendo en la BD
> destino, y no necesariamente es así.

`durability_warning` (a nivel de toda la respuesta de [§3.5](#35-get-managed-databasesdb_idmigrationsversionselect-results-))
trae un texto server-side listo para mostrar cuando corresponde este caso — mostrarlo como
banner en la parte superior de la pantalla de lectura.

### 4.4 Un `SELECT` con comentario delante SÍ se captura

Antes de la corrección del 2026-08-14, un `SELECT` con un comentario explicativo delante (el
caso más común: *"-- verificar filas sin backfill\nSELECT ..."*) **no se capturaba nunca, en
silencio** — sin error visible, `items: []` sin ningún aviso. Ya está corregido: el pre-filtro
ahora salta comentarios además de blancos antes de decidir si una sentencia es capturable. La UI
**no necesita hacer nada especial por esto** — se documenta acá para que, si algún día aparece
un `SELECT` que "debería" capturarse y no lo hace, el primer sospechoso sea el clasificador
(`query_policy.classify_statement`), no una regresión de comentarios.

### 4.5 `stale` y `missing_indexes` en la lectura

`expected_indexes` es la lista de posiciones de sentencia que el SQL **actual** de la versión
debería capturar (derivado con el mismo clasificador que usa el motor real). `missing_indexes`
son las posiciones que se esperaban pero no aparecen en `items` — por ejemplo, si se agregó un
`SELECT` nuevo al `up_sql` **después** de la última corrida capturada. Cuando `missing_indexes`
no está vacío, la UI debe indicar *"hay sentencias en el SQL actual que todavía no se
ejecutaron/capturaron — aplicá de nuevo para verlas"*, no tratarlo como error.

### 4.6 TTL y purga: las filas no viven para siempre

Las capturas expiran a las `MIGRATION_CAPTURE_TTL_HOURS` horas (default 168 = 7 días; `0`
desactiva el TTL) y se purgan automáticamente cada
`MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES` minutos (default 60) además de al arrancar el
proceso. Una vez purgadas (por TTL o por el `DELETE` de [§3.6](#36-delete-managed-databasesdb_idmigrationsversionselect-results-)),
[§3.5](#35-get-managed-databasesdb_idmigrationsversionselect-results-) devuelve `items: []` — no
un `404` ni un `410`. La UI debe tratar `items: []` con `capture_selects: true` como *"había
algo y expiró/se purgó"*, distinto de `capture_selects: false` (*"nunca se capturó nada"*). El
campo que los distingue es `capture_selects`, no un tercer estado explícito.

---

## 5. Flujos completos, paso a paso

### 5.1 Camino feliz: crear, revisar, aplicar, leer

1. Admin crea/edita la versión `0007` con `capture_selects: true` → nace `reviewed: false`
   ([§3.1](#31-patch-database-modelsmodel_idmigrationsversion-)).
2. Admin revisa el `up_sql`/`down_sql` en la UI y aprueba: `PATCH { "reviewed": true }`.
3. Admin aplica sobre la BD 87: `POST .../apply?version=0007&allow_result_capture=true`. Sin el
   segundo query param, `409` ([§5.3](#53-bloqueo-por-falta-de-consentimiento-en-la-corrida)).
4. La respuesta trae `captured_select_count: 12`, `select_results_available: true`.
5. Admin abre la pantalla de resultados: `GET .../0007/select-results` → ve `columns`/`rows`.

### 5.2 Bloqueo por falta de revisión

1. Admin activa `capture_selects: true` en `0007` y **no** la aprueba.
2. Intenta `POST .../apply?version=0007` → `409`, `detail.msg` menciona `0007`,
   `public_context.unreviewed_capture: ["0007"]`.
3. La UI resalta `0007` en la lista de pendientes con un botón "revisar y aprobar" que navega
   directo a [§3.1](#31-patch-database-modelsmodel_idmigrationsversion-).
4. Tras aprobar, reintenta el mismo `apply` (todavía sin `allow_result_capture`) →
   [§5.3](#53-bloqueo-por-falta-de-consentimiento-en-la-corrida).

### 5.3 Bloqueo por falta de consentimiento en la corrida

1. `0007` ya está `reviewed: true`.
2. `POST .../apply?version=0007` (sin `allow_result_capture`) → `409`,
   `public_context.capture_versions: ["0007"]`.
3. La UI muestra un diálogo específico: *"esta aplicación va a guardar filas de esta base de
   datos en el gateway — ¿confirmás?"*, con checkbox u opción explícita, no un simple "reintentar".
4. Reintenta con `allow_result_capture=true` → `200`.

### 5.4 Editar el SQL después de aprobar

1. `0007` está `reviewed: true`, `capture_selects: true`.
2. Admin hace `PATCH { "up_sql": "SELECT * FROM clientes" }` (sin tocar `reviewed`).
3. La respuesta trae `reviewed: false` — reseteado automáticamente
   ([§2.3](#23-editar-el-sql-revoca-la-revisión)).
4. Cualquier `apply` posterior vuelve a bloquear hasta una nueva aprobación explícita.

> **Caso particular: cambiar solo `down_sql` de una versión YA aplicada.** Es el flujo normal de
> "confirmar el rollback después de aplicar" y **no** dispara el `409` de edición de SQL
> aplicado (ese 409 es solo para `up_sql`/overrides de `up`). Pero **sí** resetea `reviewed` si
> `capture_selects` está activo, y además **purga las capturas existentes de esa versión en la
> misma transacción** — el `down_sql` nuevo ya no corresponde a las filas viejas.

### 5.5 Rollback de recuperación con una versión futura sin revisar

1. Blueprint con versiones `0001..0010`. Solo `0010` tiene `capture_selects=true` y
   `reviewed=false`. La BD está en `0005`.
2. `0007` salió mal en producción y hace falta un `rollback` de emergencia hacia `0004`.
3. `POST .../rollback?confirm_version=0005&target_version=0004` → **no** se bloquea por `0010`:
   esa versión no está en el camino de reversión ([§2.2](#22-el-gate-de-revisión-es-de-alcance-acotado-no-de-todo-el-blueprint)).
4. Si alguna versión ENTRE `0004` y `0005` sí tuviera captura sin revisar, ahí sí bloquearía —
   con `public_context.unreviewed_capture` listando solo esas.

### 5.6 Kill switch apagado

Con `MIGRATION_CAPTURE_ENABLED=False` en el servidor:

1. Una versión con `capture_selects=true` y `reviewed=false` **no bloquea** `apply`/`rollback`.
2. `allow_result_capture` se puede omitir sin `409`.
3. `apply`/`rollback` funcionan normal, pero `captured_select_count` siempre es `0` y
   `select_results_available` siempre `false` — la captura es físicamente imposible.
4. `GET .../select-results` sigue respondiendo `200` con `items: []` para captarás previas ya
   purgadas, o con datos viejos si el switch se apagó DESPUÉS de una captura anterior (el TTL
   sigue corriendo independiente del switch).

La UI no tiene forma de leer este switch por API — si un operador reporta que "no me deja
capturar nada y tampoco me pide confirmación", el primer paso de diagnóstico es preguntar por la
config del servidor, no asumir un bug de frontend.

---

## 6. Interpretación visual: pantallas y estados

**Formulario de migración (crear/editar):**

- Checkbox "Capturar resultados de SELECT" → `capture_selects`.
- Al activarlo por primera vez (o reactivarlo tras editar SQL), mostrar de inmediato un badge
  "Sin revisar" — no esperar a que el usuario navegue a otra pantalla para descubrirlo.
- Botón "Marcar como revisada" solo visible cuando `capture_selects: true` y `reviewed: false`;
  debe abrir/mostrar el SQL completo antes de habilitar la confirmación (no un botón de un solo
  clic sin mostrar qué se está aprobando).

**Lista de versiones del blueprint:**

- Badge por versión: `reviewed: false` + `capture_selects: true` → "⚠️ Captura sin revisar" en
  color de alerta. `reviewed: true` + `capture_selects: true` → "🔒 Captura aprobada" en color
  neutro/informativo. Sin `capture_selects` → sin badge.

**Diálogo de `apply`/`apply-all`/`rollback`:**

- Si el blueprint (o el camino relevante) tiene alguna versión con `capture_selects: true` y
  `reviewed: true`, mostrar el checkbox de consentimiento (`allow_result_capture`) **antes** de
  intentar la llamada — no esperar al `409` para mostrarlo, ya que la UI puede conocer este
  estado leyendo las versiones de antemano.
- Tras un `200` con `select_results_available: true`, ofrecer un link directo a la pantalla de
  lectura de esa versión.

**Pantalla de lectura (`select-results`):**

- Banner de `durability_warning` cuando no es `null`, en color de alerta (no informativo).
- Tabla por `statement_index`: columnas = `columns`, filas = `rows` (recordar: son arrays
  posicionales, no objetos con clave).
- Filas con `status: "error"` se muestran como una fila de error dentro de ESE statement, no
  como fallo de toda la pantalla — el resto de los `items` puede tener `status: "ok"`.
- `truncated: true` → aviso "hay más filas/datos de los que se muestran" al pie de la tabla de
  ESE statement.
- Botón "Purgar ahora" → `DELETE`, con confirmación (irreversible, [§3.6](#36-delete-managed-databasesdb_idmigrationsversionselect-results-)).

---

## 7. Tipos (referencia rápida)

```
MigrationSelectResultItemOut:
  statement_index: number
  direction: "up" | "down"
  sql: string
  sql_hash: string
  status: "ok" | "error"
  durability: "committed" | "rolled_back" | "unknown"
  columns: string[]
  rows: any[][]                    # posicional — rows[i][j] ↔ columns[j]
  row_count: number = 0
  truncated: boolean = false
  payload_bytes: number = 0
  error: string | null
  captured_at: string (ISO datetime)
  migration_checksum: string

MigrationSelectResultsOut:
  managed_database_id: number
  database_name: string
  server_id: number
  model_migration_id: number
  version: string
  capture_selects: boolean = false
  stale: boolean = false
  expected_indexes: number[] = []
  missing_indexes: number[] = []
  durability_warning: string | null
  items: MigrationSelectResultItemOut[] = []

# Campos nuevos en tipos ya existentes:
ModelMigrationCreate/Patch/Out/Summary:
  capture_selects: boolean = false
  reviewed: boolean = true          # default true; false solo cuando capture_selects se activa

MigrationApplyOut / MigrationRollbackOut (campos agregados):
  captured_select_count: number = 0
  select_results_available: boolean = false
```

---

## 8. Matriz de errores

```
--- PATCH /database-models/{model_id}/migrations/{version} ---
409 — edición de up_sql/down_sql/overrides sobre migración ya aplicada exitosamente
      msg: "La migración ya fue aplicada exitosamente en alguna BD: no se puede modificar
            su SQL. Cree una nueva migración para corregir (fix-forward)."
      → capture_selects/reviewed SOLOS sí se pueden cambiar siempre
404 — migración no encontrada

--- Apply / Apply-all (POST .../migrations/apply, apply-all) ---
409 — versiones con captura SIN REVISAR en el camino de esta corrida
      msg: "El blueprint tiene versiones con captura de resultados SIN revisar
            ({versions})…revísalas y aprobalas (PATCH reviewed=true) antes de aplicar
            o revertir."
      public_context: { unreviewed_capture: [...] }
409 — falta allow_result_capture=true habiendo versiones con capture_selects en el camino
      msg: "Confirmación requerida: las versiones pendientes {versions} tienen la captura
            de resultados activada…Repetí la llamada con allow_result_capture=true…"
      public_context: { capture_versions: [...] }
      → en apply-all, este 409 sale POR BD, no frena el lote entero
      → ninguno de los dos aplica con dry_run=true
      → ambos se saltean si MIGRATION_CAPTURE_ENABLED=False
409 — BD en cuarentena (error genérico, ya documentado en versiones anteriores)
429 — rate limit 10/minute (apply) · 3/minute (apply-all)

--- Rollback (POST .../migrations/rollback) ---
409 — versiones con captura SIN REVISAR en el camino DE REVERSIÓN (no todo el blueprint)
      msg: mismo texto que en apply, adaptado a "revertir"
      public_context: { unreviewed_capture: [...] }
409 — falta allow_result_capture=true
      msg: "Confirmación requerida: las versiones a revertir {versions}…"
      public_context: { capture_versions: [...] }
429 — rate limit 10/minute

--- Stamp (POST .../migrations/stamp) ---
409 — versión con captura sin revisar
      msg: "…tiene captura de resultados sin revisar. Revisá el SQL y aprobala
            (PATCH reviewed=true) antes de marcarla."
      public_context: { unreviewed_capture: [version] }
      → tiene force=true como escape (solo mueve el puntero de versión, NO habilita captura)
429 — rate limit 10/minute

--- Lectura / purga (GET, DELETE .../select-results) ---
404 — "La versión {version} no existe en el blueprint de esta BD."
      → único error de estos dos endpoints además de 401
      → NO hay 409 ni 410: expirado/purgado = items: [] con 200, no error
429 — rate limit 20/minute (solo GET; DELETE no tiene rate limit propio)

--- Transversales ---
401 — sin sesión admin (todos los endpoints)
422 — {version} no matchea ^\d{4,10}$
429 — "Demasiadas solicitudes. Límite: {N}/minute"
5xx — { "detail": { "msg": "Error interno del servidor", "type": "InternalServerError" } }
      SIN request_id en el cuerpo → leerlo del header X-Request-ID
```

---

## 9. Checklist de implementación

**Formulario y lista de migraciones**

- [ ] Agregar el checkbox `capture_selects` al formulario de creación/edición.
- [ ] Mostrar el badge de `reviewed`/`capture_selects` en la lista de versiones
      ([§6](#6-interpretación-visual-pantallas-y-estados)) — no esperar al `409` para que el
      operador se entere de que algo está sin revisar.
- [ ] El botón "Marcar como revisada" debe **mostrar el SQL** antes de habilitar la confirmación.
- [ ] Avisar explícitamente que "editar y aprobar en un mismo PATCH" no funciona: el reset de
      `reviewed` gana siempre ([§3.1](#31-patch-database-modelsmodel_idmigrationsversion-)).

**Diálogos de `apply`/`apply-all`/`rollback`**

- [ ] Mostrar el checkbox de `allow_result_capture` de forma **proactiva** cuando corresponda,
      leyendo las versiones de antemano — no reactivamente tras un `409`.
- [ ] Distinguir en el copy los dos `409` posibles (falta revisión vs. falta consentimiento):
      son causas y acciones distintas, aunque ambos sean `409` del mismo endpoint.
- [ ] Leer `public_context.unreviewed_capture` / `capture_versions` para resaltar EXACTAMENTE
      las versiones involucradas, no un mensaje genérico.
- [ ] En `apply-all`, no interpretar un `409` de una BD del lote como "no se aplicó nada" —
      revisar el resultado por BD.
- [ ] Tras un `200`, si `select_results_available: true`, ofrecer navegar a la pantalla de
      lectura.

**Pantalla de lectura de resultados**

- [ ] Renderizar `rows` como arrays posicionales (`rows[i][j]` ↔ `columns[j]`), no como objetos.
- [ ] Mostrar el banner de `durability_warning` con peso visual de alerta cuando no sea `null`.
- [ ] Explicar `durability: "rolled_back"` en la propia fila/statement: esos datos vienen de una
      transacción deshecha por el motor.
- [ ] Tratar `items: []` distinto según `capture_selects`: `true` = "expiró o se purgó";
      `false` = "nunca se activó la captura".
- [ ] Mostrar `missing_indexes` no vacío como aviso de "hay sentencias nuevas sin capturar
      todavía", no como error.
- [ ] `status: "error"` en un ítem es un fallo acotado de ESE statement — el resto de `items`
      puede seguir siendo válido.
- [ ] `truncated: true` → avisar que hay más filas/bytes de los mostrados, sin implicar
      corrupción de datos.
- [ ] El botón de purgar (`DELETE`) debe confirmar antes de ejecutar: es irreversible y no hay
      papelera.

**Transversales**

- [ ] Mostrar `detail.msg` tal cual — trae la acción correctiva en español.
- [ ] Usar `public_context` (viaja en TODOS los ambientes en este módulo, a diferencia del resto
      del gateway) para evitar una segunda llamada.
- [ ] No asumir que un `409` de captura implica que nada se ejecutó en `apply-all`: es por BD.
- [ ] Recordar que ninguno de los dos gates aplica con `dry_run=true`.
- [ ] Ante reportes de "no bloquea nada y no pide consentimiento", primero preguntar por
      `MIGRATION_CAPTURE_ENABLED` en el servidor antes de asumir un bug de frontend
      ([§5.6](#56-kill-switch-apagado)).
