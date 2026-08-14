# API Reference v8 — Conversión de collation de una base de datos completa (y los objetos que la congelan)

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md),
> [`api-reference-v2.md`](api-reference-v2.md), [`api-reference-v3.md`](api-reference-v3.md),
> [`api-reference-v4.md`](api-reference-v4.md), [`api-reference-v5.md`](api-reference-v5.md),
> [`api-reference-v6.md`](api-reference-v6.md) y [`api-reference-v7.md`](api-reference-v7.md).
>
> Como **v6**: describe un módulo **NUEVO que nunca fue expuesto al frontend**. No hay pantalla
> existente que ajustar ni supuesto que corregir — hay una pantalla que diseñar desde cero. El
> backend ya está implementado y commiteado.
>
> ⚠️ **No confundir con v7.** v7 documenta el **catálogo** de charsets/collations: qué pares se
> pueden **elegir al CREAR** una base de datos. Este documento describe cómo **CAMBIAR** el
> charset/collation de una base **que ya existe**, con sus datos y sus objetos adentro. Son dos
> módulos distintos que se tocan en un solo punto (§4.3): en MySQL/MariaDB, el objetivo de la
> conversión se valida contra el catálogo de v7.
>
> Mismo formato que v3/v4/v5/v6/v7: **problema → qué debe pasar → escenarios → flujos →
> endpoints → ejemplos → interpretación visual**.
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie, errores,
> paginación) idénticas al documento original ([§3](api-reference.md#3-convenciones-de-la-api)),
> con **tres precisiones importantes** en [§3.0](#30-envelope-y-errores-tres-precisiones-que-valen-para-todo-el-módulo).
>
> Documentación de ingeniería del mismo módulo (más detalle interno del que el frontend
> necesita): [`docs/features/collation-conversion.md`](features/collation-conversion.md).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## Índice

- [0. El problema: por qué existe este módulo](#0-el-problema-por-qué-existe-este-módulo)
  - [0.1 El bug que tienen las herramientas gráficas](#01-el-bug-que-tienen-las-herramientas-gráficas)
  - [0.2 Por qué PostgreSQL es otra operación](#02-por-qué-postgresql-es-otra-operación)
- [1. Alcance: qué cubre y qué NO cubre](#1-alcance-qué-cubre-y-qué-no-cubre)
  - [1.1 Endpoints de OTROS módulos que esta pantalla necesita](#11-endpoints-de-otros-módulos-que-esta-pantalla-necesita)
  - [1.2 Limitaciones que hay que conocer ANTES de diseñar](#12-limitaciones-que-hay-que-conocer-antes-de-diseñar)
- [2. Los dos modos: `universal` y `columns`](#2-los-dos-modos-universal-y-columns)
  - [2.1 Tabla comparativa](#21-tabla-comparativa)
  - [2.2 El modo NO se elige: lo determina el motor](#22-el-modo-no-se-elige-lo-determina-el-motor)
  - [2.3 Los 5 tipos de objeto congelados](#23-los-5-tipos-de-objeto-congelados)
- [3. Los 7 endpoints](#3-los-7-endpoints)
  - [3.0 Envelope y errores: tres precisiones que valen para todo el módulo](#30-envelope-y-errores-tres-precisiones-que-valen-para-todo-el-módulo)
  - [3.1 `POST /servers/{server_id}/databases/{database}/collation-conversions`](#31-post-serversserver_iddatabasesdatabasecollation-conversions-)
  - [3.2 `GET /collation-conversions/{job_id}`](#32-get-collation-conversionsjob_id-)
  - [3.3 `GET /collation-conversions/{job_id}/objects`](#33-get-collation-conversionsjob_idobjects-)
  - [3.4 `POST /collation-conversions/{job_id}/preview`](#34-post-collation-conversionsjob_idpreview-)
  - [3.5 `POST /collation-conversions/{job_id}/execute`](#35-post-collation-conversionsjob_idexecute-)
  - [3.6 `GET /collation-conversions/{job_id}/items`](#36-get-collation-conversionsjob_iditems-)
  - [3.7 `POST /collation-conversions/{job_id}/cancel`](#37-post-collation-conversionsjob_idcancel-)
- [4. Semántica de validación (lo que más confusión genera)](#4-semántica-de-validación-lo-que-más-confusión-genera)
  - [4.1 `target_charset` es condicional, y el error va en los dos sentidos](#41-target_charset-es-condicional-y-el-error-va-en-los-dos-sentidos)
  - [4.2 `target_collation` es SIEMPRE obligatoria](#42-target_collation-es-siempre-obligatoria)
  - [4.3 Dos catálogos distintos que no se mezclan](#43-dos-catálogos-distintos-que-no-se-mezclan)
  - [4.4 La selección de objetos es exclusiva de `universal`](#44-la-selección-de-objetos-es-exclusiva-de-universal)
  - [4.5 Granularidad: siempre por TABLA, nunca por columna](#45-granularidad-siempre-por-tabla-nunca-por-columna)
  - [4.6 Best-effort por ítem, con UNA excepción](#46-best-effort-por-ítem-con-una-excepción)
  - [4.7 Recuperación: el objeto que se dropeó y no se recreó](#47-recuperación-el-objeto-que-se-dropeó-y-no-se-recreó)
  - [4.8 `grants_*`: solo PROCEDURE y FUNCTION](#48-grants_-solo-procedure-y-function)
  - [4.9 Advertencias con semántica DISTINTA por motor](#49-advertencias-con-semántica-distinta-por-motor)
  - [4.10 Fingerprint anti-TOCTOU y expiración del plan](#410-fingerprint-anti-toctou-y-expiración-del-plan)
  - [4.11 Confirmación de dos factores](#411-confirmación-de-dos-factores)
  - [4.12 Polling: `execute` solo ENCOLA](#412-polling-execute-solo-encola)
- [5. Flujos completos, paso a paso](#5-flujos-completos-paso-a-paso)
  - [5.1 Conversión completa en MySQL/MariaDB (happy path)](#51-conversión-completa-en-mysqlmariadb-happy-path)
  - [5.2 Conversión en PostgreSQL (happy path)](#52-conversión-en-postgresql-happy-path)
  - [5.3 Un objeto falla y el resto continúa](#53-un-objeto-falla-y-el-resto-continúa)
  - [5.4 El `ALTER DATABASE` falla y corta todo](#54-el-alter-database-falla-y-corta-todo)
  - [5.5 Fingerprint stale durante el preview](#55-fingerprint-stale-durante-el-preview)
  - [5.6 Plan expirado](#56-plan-expirado)
  - [5.7 Rutina sin grants legibles: `skipped` fail-closed](#57-rutina-sin-grants-legibles-skipped-fail-closed)
- [6. Interpretación visual: pantallas, estados y transiciones](#6-interpretación-visual-pantallas-estados-y-transiciones)
- [7. Tipos (referencia rápida)](#7-tipos-referencia-rápida)
- [8. Matriz de errores](#8-matriz-de-errores)
- [9. Checklist de implementación](#9-checklist-de-implementación)

---

## 0. El problema: por qué existe este módulo

Una base de datos acumula collations distintas con el tiempo: tablas creadas en épocas
distintas, un `utf8mb3` heredado de una migración vieja, una tabla nueva en `utf8mb4`. Mientras
nadie compare texto entre esas tablas, "funciona". El día que una consulta hace un `JOIN` o un
`WHERE a.email = b.email` entre dos columnas de collations distintas, el motor corta:

```
ERROR 1267 (HY000): Illegal mix of collations (utf8mb4_general_ci,IMPLICIT)
and (utf8mb4_unicode_ci,IMPLICIT) for operation '='
```

Unificar la collation de toda la base es la solución. El problema es que **hacerlo bien no es
lo que hacen las herramientas gráficas**.

### 0.1 El bug que tienen las herramientas gráficas

Herramientas como **HeidiSQL** ofrecen "cambiar el collation de toda la BD" y hacen dos cosas:
`ALTER DATABASE` y un `ALTER TABLE ... CONVERT TO CHARACTER SET` por tabla. Eso **no alcanza**.

En MySQL/MariaDB, cuando se crea una **PROCEDURE, FUNCTION, TRIGGER, EVENT o VIEW**, el motor
**congela dentro del objeto** la `collation_connection` de la sesión que lo creó. Esa collation
congelada es la que heredan:

- los parámetros `VARCHAR`/`CHAR` de una **PROCEDURE** o **FUNCTION**,
- las variables `DECLARE` de un **TRIGGER** o un **EVENT**,
- los **literales de texto** embebidos en el cuerpo de una **VIEW**.

Si se cambia la collation de la base y de las tablas pero **no se recrean esos objetos**,
quedan comparando texto en dos collations distintas y el `Illegal mix of collations` aparece en
producción — típicamente semanas después, en el peor momento, y sin relación aparente con el
cambio que lo causó.

La documentación de MySQL lo dice sin rodeos en la página de `ALTER DATABASE`:

> *"If you change the default character set or collation for a database, any stored routines
> that are to use the new defaults must be dropped and recreated."*

**No existe** un `ALTER PROCEDURE` ni un `ALTER TRIGGER` que cambie el cuerpo ni la collation de
sus parámetros. La única forma de arreglar un objeto ya creado es **`DROP` + `CREATE` con el
cuerpo EXACTO, en una sesión que ya tenga la collation objetivo** (`SET NAMES`). Ese es
precisamente el hueco que este módulo cierra: hace lo que HeidiSQL hace, **más** la parte que
HeidiSQL se olvida.

### 0.2 Por qué PostgreSQL es otra operación

PostgreSQL **no tiene este problema**: resuelve la collation dinámicamente, en cada ejecución y
contra el tipo real de la columna en ese momento. Una función o una vista **no congelan** nada,
así que no hay nada que refrescar con un DROP+CREATE.

Pero PostgreSQL tiene una restricción propia: el **`ENCODING`/`LC_COLLATE` de una base de datos
es INMUTABLE** tras el `CREATE DATABASE`. No existe un `ALTER DATABASE ... SET ENCODING`.
Cambiar el encoding de una base PostgreSQL implica volcar y recargar en una base nueva (para
eso está el [módulo de clonado](features/database-clone.md)).

Lo que **sí** se puede hacer en PostgreSQL es cambiar la collation **por columna**:

```sql
ALTER TABLE "public"."users"
  ALTER COLUMN "email" SET DATA TYPE character varying(255) COLLATE "es-ES-x-icu";
```

Y eso es exactamente lo que hace este módulo en PostgreSQL. **Misma pantalla, mismos endpoints,
operación distinta.**

**Actor:** el admin único del gateway (sesión admin por cookie). No hay roles ni multi-tenant.

---

## 1. Alcance: qué cubre y qué NO cubre

### Cubre

- **Planificar** una conversión de collation sobre una base de datos identificada por
  **identidad física** (`server_id` + nombre) — funcione o no adoptada en el inventario del
  gateway.
- **Inventariar en vivo** qué collations hay dando vueltas: por tabla (MySQL/MariaDB) o por
  columna (PostgreSQL), con un resumen agrupado.
- **Listar los 5 tipos de objeto congelados** con su `collation_connection`, para que el
  operador vea qué quedó desactualizado (solo MySQL/MariaDB).
- **Previsualizar** el plan exacto (las sentencias, qué se saltea, qué ya no existe) sin
  ejecutar nada.
- **Ejecutar** de forma asíncrona con doble confirmación, y **seguir el progreso** por polling.
- **Reportar el resultado paso por paso**, incluida la captura y reaplicación de privilegios de
  rutina.

### NO cubre

- **No hay rollback.** Convertir es una operación de una sola dirección. Volver atrás es **otra
  conversión** (a la collation anterior), con el mismo costo y el mismo riesgo. La UI **no debe
  ofrecer "deshacer"** ni sugerir que exista.
- **No cambia el `ENCODING`/`LC_COLLATE` de una base PostgreSQL.** Es imposible
  ([§0.2](#02-por-qué-postgresql-es-otra-operación)). En ese motor `include_database_default` se
  fuerza a `false` sea lo que sea que se envíe.
- **No convierte otras bases del mismo servidor.** Un job = una base. Si hay FKs cruzadas entre
  bases, hay que planificar una conversión por cada una (y el backend lo advierte).
- **PostgreSQL: solo el schema `public`.** Misma limitación que el diff de esquema y el clonado.
- **No hay `resolve-selection` ni grafo de dependencias** (a diferencia del clon y de
  schema-comparisons). Es deliberado: cada objeto se procesa completo e independiente, así que
  el orden es irrelevante. La selección es exactamente lo que el usuario eligió.
- **No hay endpoint de listado de jobs.** Solo se accede a un job por su `id`. Si la UI quiere
  un historial de conversiones, **hoy no hay endpoint** — hay que pedirlo al backend.
- **No expone el DDL capturado** de un objeto recreado, aunque el backend lo guarde
  ([§4.7](#47-recuperación-el-objeto-que-se-dropeó-y-no-se-recreó)).

### 1.1 Endpoints de OTROS módulos que esta pantalla necesita

Los 7 endpoints de §3 son todo lo que este módulo expone. La pantalla necesita además esto, que
ya está documentado en otro lado y **cuyo contrato no se repite acá**:

| Para | Endpoint | Por qué | Documentado en |
|---|---|---|---|
| Elegir el **servidor** y conocer su motor | `GET /servers` / `GET /servers/{id}` 🔒 | El `engine` determina el **modo** y por lo tanto la pantalla entera ([§2.2](#22-el-modo-no-se-elige-lo-determina-el-motor)) | [`api-reference.md` §6](api-reference.md#6-servidores-servers) |
| Elegir la **base de datos** | `GET /servers/{id}/databases` 🔌🔒 | La BD se identifica por nombre, no por id de inventario | [`api-reference.md` §6](api-reference.md#6-servidores-servers) |
| Poblar el selector de **collation objetivo** en MySQL/MariaDB | `GET /charset-collation-options?engine_family=mysql&only_enabled=true` 🔒 | Es el catálogo global del gateway. **Solo para MySQL/MariaDB** ([§4.3](#43-dos-catálogos-distintos-que-no-se-mezclan)) | [`api-reference-v7.md` §5.1](api-reference-v7.md#51-get-charset-collation-options-) |
| Poblar el selector de **collation objetivo** en PostgreSQL | *(ninguno)* — sale de `GET /collation-conversions/{id}/objects` → `available_collations` | Ese catálogo es **por servidor** y se lee en vivo | este documento, [§3.3](#33-get-collation-conversionsjob_idobjects-) |

> 🐔🥚 **Ojo con el orden en PostgreSQL.** `available_collations` viene del **inventario**, que
> solo existe **después** de crear el plan — y crear el plan **ya exige** un `target_collation`
> válido. Ver [§4.3](#43-dos-catálogos-distintos-que-no-se-mezclan) para la salida.

### 1.2 Limitaciones que hay que conocer ANTES de diseñar

| Limitación | Consecuencia para la UI |
|---|---|
| **El worker no es durable** (in-process, `ThreadPoolExecutor`). Si el gateway se reinicia, los jobs `running` quedan en **`interrupted`** | `interrupted` es un estado real y visible. La UI debe explicarlo: *"el gateway se reinició durante la conversión; revisá los pasos ya aplicados antes de crear un plan nuevo"* — **no** es lo mismo que `failed` |
| **Cancelar es cooperativo** | No interrumpe un `ALTER TABLE` en curso: solo detiene los pasos que todavía no arrancaron. El copy del botón no puede prometer "detener ahora" |
| **Sin rollback automático** | No ofrecer "deshacer" |
| **`ALTER TABLE ... CONVERT TO CHARACTER SET` reescribe la tabla completa** | En tablas grandes tarda minutos u horas y **bloquea escrituras**. La UI debe tratar esto como una ventana de mantenimiento, no como un botón más |
| **PostgreSQL: `ALTER COLUMN ... TYPE` toma `ACCESS EXCLUSIVE`** | Bloquea **hasta los `SELECT`** y reconstruye los índices afectados |
| **Un solo worker por defecto** (`COLLATION_CONVERSION_MAX_WORKERS=1`) | Dos jobs simultáneos se encolan, no corren en paralelo. Un job puede quedarse en `pending` un rato sin que sea un error |
| **Estado del backend: falta la corrida contra motores reales** | El módulo está verificado con tests de API y adapters mockeados, pero **no e2e contra MySQL/MariaDB/PostgreSQL reales**. El contrato podría tener ajustes menores: conviene aislar el mapeo de estas respuestas en un solo lugar del código de UI |

---

## 2. Los dos modos: `universal` y `columns`

### 2.1 Tabla comparativa

| | `universal` | `columns` |
|---|---|---|
| **Motor** | MySQL / MariaDB | PostgreSQL |
| **`target_charset`** | **Obligatorio** | **Prohibido** (422 si se envía) |
| **Se valida contra** | Catálogo global del gateway (`charset_collation_options`) | `pg_collation` **del servidor**, leído en vivo |
| **`ALTER DATABASE`** | Sí, opcional (`include_database_default`) | **Nunca** — se fuerza a `false` |
| **Conversión de tablas** | `ALTER TABLE ... CONVERT TO CHARACTER SET` (tabla entera) | `ALTER TABLE ... ALTER COLUMN ... TYPE ... COLLATE` (una sentencia por tabla, con todas sus columnas) |
| **Recreación de objetos** | Sí: PROCEDURE, FUNCTION, TRIGGER, EVENT, VIEW | **No existe** — 422 si se envía `objects` |
| **`objects` del inventario** | Poblado | **Siempre `[]`** |
| **`available_collations`** | **Siempre `[]`** | Poblado (para armar el selector) |
| **`columns` por tabla (inventario)** | **`null`** | Poblado |
| **`summary` agrupa por** | par `(charset, collation)` de **tabla** | `collation` de **columna** (`column_count` poblado) |
| **`phase`s posibles** | `database` → `tables` → `objects` → `done` | `tables` → `done` |
| **`object_type` de los ítems** | `database`, `table`, `procedure`, `function`, `trigger`, `event`, `view` | **siempre `table`** |
| **Atomicidad por tabla** | ❌ No hay DDL transaccional: "media tabla convertida" es un riesgo real | ✅ Sí — DDL transaccional, todas las columnas en una sentencia |
| **`columns_affected` por ítem** | `null` | Poblado |
| **`grants_*` por ítem** | Poblado en PROCEDURE/FUNCTION | Siempre `null` |

### 2.2 El modo NO se elige: lo determina el motor

> 🚨 **No hay ningún campo de entrada para el modo.** El backend lo infiere del `engine` del
> servidor y lo devuelve en `mode` de **todas** las salidas (summary, inventario, preview).

Consecuencias de diseño, que son el corazón de esta pantalla:

1. **El formulario de creación del plan es casi idéntico** para los dos motores: elegir servidor,
   elegir base, elegir collation objetivo. Lo único que cambia es que MySQL/MariaDB suma el
   charset y PostgreSQL no lo debe mostrar.
2. **A partir del inventario, las pantallas divergen de raíz.** No es un detalle cosmético: en
   `universal` hay un selector de objetos, un paso de `ALTER DATABASE` y una fase de recreación
   que en `columns` **no existen**.
3. **La UI ya sabe el motor antes de llamar** (lo trae `GET /servers/{id}`). Puede — y debe —
   adaptar el formulario **antes** del primer request, en vez de esperar un `422`.

### 2.3 Los 5 tipos de objeto congelados

Solo en `universal`. Son los `object_type` válidos en la selección y en el inventario:

| `object_type` | Qué congela | ¿Tiene privilegios propios? |
|---|---|---|
| `procedure` | La collation de sus parámetros `VARCHAR`/`CHAR` | ✅ **Sí** — ver [§4.8](#48-grants_-solo-procedure-y-function) |
| `function` | Ídem | ✅ **Sí** |
| `trigger` | La collation de sus variables `DECLARE` | ❌ No (viaja en el privilegio `TRIGGER` de su tabla) |
| `event` | Ídem | ❌ No (viaja en el privilegio `EVENT` de la base) |
| `view` | La collation de los literales de texto de su cuerpo | ❌ No |

> **`table` NO está en esta lista.** Una tabla no se "recrea": se convierte con
> `ALTER TABLE ... CONVERT TO CHARACTER SET` y se selecciona por el campo `tables`, no por
> `objects`. Mandar `{"object_type": "table"}` es un `422` de validación Pydantic.

---

## 3. Los 7 endpoints

| # | Endpoint | Rate limit | Toca el motor |
|---|---|---|---|
| 1 | `POST /servers/{server_id}/databases/{database}/collation-conversions` | `10/minute` | 🔌 |
| 2 | `GET /collation-conversions/{job_id}` | *(global)* | — |
| 3 | `GET /collation-conversions/{job_id}/objects` | `10/minute` | 🔌 |
| 4 | `POST /collation-conversions/{job_id}/preview` | `10/minute` | 🔌 |
| 5 | `POST /collation-conversions/{job_id}/execute` | **`3/minute`** | 🔌 |
| 6 | `GET /collation-conversions/{job_id}/items` | *(global)* | — |
| 7 | `POST /collation-conversions/{job_id}/cancel` | *(global)* | — |

**Todos requieren sesión admin** (🔒). Sin sesión: `401`.

> Notá que **la creación cuelga de `/servers/...`** (la base se identifica por identidad física,
> como en el resto del módulo de servidor-BDs) y **el resto cuelga del job**. No hay
> `/servers/{id}/collation-conversions` para listar.

### 3.0 Envelope y errores: tres precisiones que valen para todo el módulo

Estas dos correcciones aplican a **todo el gateway**, no solo a este módulo, y contradicen
detalles que aparecen en documentos anteriores de esta serie. Están verificadas contra el
código.

**1. El envelope de éxito NO tiene un campo `success`.** Es exactamente:

```json
{
  "data":       "…el payload…",
  "message":    "texto opcional",
  "pagination": { "page": 1, "size": 20, "total": 42, "pages": 3, "has_next": true, "has_prev": false }
}
```

- `message` aparece **solo** si el endpoint lo provee.
- `pagination` aparece **solo** en listados paginados (acá: `/items`), y se llama `pagination`
  — **no** `meta`.
- Los campos `null` **de ese nivel** se omiten. Los `null` **dentro de `data`** SÍ viajan: un
  `"phase": null` o un `"columns": null` llegan explícitamente y hay que leerlos.

> ⚠️ Los ejemplos de [`api-reference-v7.md`](api-reference-v7.md) muestran un `"success": true`
> que **el backend no emite**. Si el código de UI ya lo usa como discriminador, está roto (o
> funciona por accidente). **La respuesta HTTP 2xx es el indicador de éxito.**

**2. Casi todos los errores de este módulo usan `context`, que es DEV-ONLY.** Los
`AppHttpException` de este módulo adjuntan su detalle estructurado en `detail.context`, que
**solo se serializa cuando `APP_ENV=development`**. En producción el cuerpo de error es
literalmente:

```json
{ "detail": { "msg": "…mensaje en español, listo para mostrar…", "type": "AppHttpException" } }
```

Consecuencias directas y no negociables:

- **`detail.msg` es lo único con lo que la UI cuenta en la enorme mayoría de los casos.** Está
  redactado en español, es específico y en varios casos **dice cómo salir del problema** (ej.
  *"reintentá con force=true"*). Se muestra tal cual.
- **Nunca leas `detail.context` ni `detail.loc`.** No existen fuera de desarrollo. Si en una
  prueba local aparecen, es porque el gateway corría en `development`.

**Las DOS únicas excepciones**, ambas en el endpoint de **creación del plan**, sí traen
`public_context` (que **sí viaja en todos los ambientes**):

| Error | `public_context` | Para qué sirve |
|---|---|---|
| `422` `"La combinación charset/collation no está habilitada en el catálogo del gateway."` | `{engine_family, requested: {charset, collation}, allowed: [{charset, collation, is_default}] (máx. 50), truncated}` | **Repoblar el selector en el acto**, sin una segunda llamada — igual que en v7 |
| `422` `"La collation pedida no existe en este servidor PostgreSQL…"` | `{available_count: number}` | Solo el conteo. **No trae la lista**: no sirve para poblar el selector |

> En todo el resto del módulo (preview, execute, cancel, items, y los demás errores de creación)
> **no hay `public_context`**. La recuperación de esos `422`/`409` es re-pedir el inventario o el
> catálogo correspondiente, no leerlo del error.

**3. El identificador de soporte viaja en un header, no en el cuerpo.** El `500` en producción es
`{"detail": {"msg": "Error interno del servidor", "type": "InternalServerError"}}` — **sin
`request_id`**. Para mostrar un identificador de soporte, la UI debe leer el header de respuesta
**`X-Request-ID`**, que el gateway pone en **todas** las respuestas.

---

### 3.1 `POST /servers/{server_id}/databases/{database}/collation-conversions` 🔌🔒

Crea el **plan**: valida el objetivo, lee el inventario en vivo, calcula el fingerprint
anti-TOCTOU y persiste el job en estado `pending`. **No ejecuta nada en el motor.**
**Rate limit `10/minute`.** → `201`.

**Path**

| Param | Tipo | Nota |
|---|---|---|
| `server_id` | `number` | Servidor del inventario del gateway |
| `database` | `string` | Nombre **físico** de la base. No hace falta que esté adoptada |

**Body**

| Campo | Tipo | Req. | Restricciones |
|---|---|---|---|
| `target_charset` | `string \| null` | **Condicional** | 1–64. **Obligatorio** en MySQL/MariaDB, **prohibido** en PostgreSQL ([§4.1](#41-target_charset-es-condicional-y-el-error-va-en-los-dos-sentidos)) |
| `target_collation` | `string` | ✅ **Siempre** | 1–64. Validación según motor ([§4.3](#43-dos-catálogos-distintos-que-no-se-mezclan)) |

#### Ejemplo A — servidor MariaDB (modo `universal`)

```http
POST /api/v1/servers/3/databases/app_db/collation-conversions
Content-Type: application/json

{
  "target_charset": "utf8mb4",
  "target_collation": "utf8mb4_unicode_ci"
}
```

```json
{
  "message": "Plan de conversión de collation creado.",
  "data": {
    "id": 41,
    "server_id": 3,
    "database_name": "app_db",
    "database_id": 87,
    "engine": "mariadb",
    "mode": "universal",
    "target_charset": "utf8mb4",
    "target_collation": "utf8mb4_unicode_ci",
    "previous_db_charset": "utf8mb3",
    "previous_db_collation": "utf8mb3_general_ci",
    "status": "pending",
    "phase": null,
    "progress": null,
    "error": null,
    "expired": false,
    "created_at": "2026-08-14T09:12:44Z",
    "expires_at": "2026-08-15T09:12:44Z",
    "started_at": null,
    "finished_at": null
  }
}
```

- `database_id` es `null` si la base **no está adoptada** en el inventario. Es normal y no
  impide nada; solo significa que no hay cuarentena que verificar
  ([§4.11](#411-confirmación-de-dos-factores)).
- `previous_db_charset` / `previous_db_collation` son el estado **antes** de convertir. Sirven
  para el encabezado *"utf8mb3_general_ci → utf8mb4_unicode_ci"*.

#### Ejemplo B — servidor PostgreSQL (modo `columns`)

**Sin `target_charset`.** Enviarlo es un `422`.

```http
POST /api/v1/servers/5/databases/app_db/collation-conversions
Content-Type: application/json

{ "target_collation": "es-ES-x-icu" }
```

```json
{
  "message": "Plan de conversión de collation creado.",
  "data": {
    "id": 42,
    "server_id": 5,
    "database_name": "app_db",
    "database_id": null,
    "engine": "postgresql",
    "mode": "columns",
    "target_charset": null,
    "target_collation": "es-ES-x-icu",
    "previous_db_charset": "UTF8",
    "previous_db_collation": "en_US.UTF-8",
    "status": "pending",
    "phase": null,
    "progress": null,
    "error": null,
    "expired": false,
    "created_at": "2026-08-14T09:20:01Z",
    "expires_at": "2026-08-15T09:20:01Z",
    "started_at": null,
    "finished_at": null
  }
}
```

> `previous_db_charset: "UTF8"` y `previous_db_collation: "en_US.UTF-8"` se informan **como
> contexto**: son el `ENCODING` y el `LC_COLLATE` de la base, **inmutables**. Esta operación
> **no los toca**. El copy debe dejarlo claro para que nadie lea "de en_US.UTF-8 a es-ES-x-icu"
> como si la base entera cambiara de locale.

#### Errores

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `422` | `"PostgreSQL no tiene charset por columna ni por tabla, y el ENCODING de la base es inmutable tras el CREATE DATABASE: no envíes target_charset, solo target_collation."` | Se envió `target_charset` a un servidor PostgreSQL |
| `422` | `"La combinación charset/collation no está habilitada en el catálogo del gateway."` **+ `public_context` con `allowed[]`** | MySQL/MariaDB: **el par no existe o está deshabilitado** en el catálogo de v7 |
| `422` | `"target_charset es obligatorio para MySQL/MariaDB: esta operación siempre fija charset y collation juntos (ALTER DATABASE/ALTER TABLE ... CHARACTER SET ... COLLATE ... exige ambos)."` | MySQL/MariaDB: **se omitió `target_charset`**. ⚠️ **No es un error de validación Pydantic**: el campo es opcional en el schema y el motor es el que lo vuelve obligatorio |
| `422` | `"La collation pedida no existe en este servidor PostgreSQL (o no es usable con el encoding de esta base). El catálogo de collations depende de los locales instalados en el SO de cada servidor: consultá las disponibles en el inventario del plan (available_collations)."` **+ `public_context: {available_count}`** | PostgreSQL: la collation no está en `pg_collation` de **ese** servidor (ojo con el case) |
| `422` | `"La conversión de charset/collation no aplica a este motor."` | El motor no es mysql/mariadb/postgresql |
| `422` | `"El {kind} es vacío o inválido."` / `"…excede la longitud máxima."` / `"…contiene caracteres no permitidos."` | Nombre de base inválido (`kind = "base de datos"`) |
| `404` | `"La base de datos no existe en el servidor."` | La base no existe en vivo |
| `404` | `"Servidor no encontrado."` | `server_id` inválido |
| `409` | `"Operación no permitida sobre una base de datos del sistema."` | `mysql`, `information_schema`, `performance_schema`, `sys`, `postgres`, `template0`, `template1` |
| `500` | `"No se pudo descifrar la credencial del servidor."` | Problema de crypto del gateway |
| `502` / `504` / `403` / `500` | `"No se pudo conectar al servidor de base de datos destino."` / `"La operación en el servidor destino excedió el tiempo de espera."` / `"La credencial del gateway no tiene permisos para esta operación."` / `"Ocurrió un error inesperado en el servidor destino."` | Fallo de infraestructura al leer el motor |
| `429` | `"Demasiadas solicitudes. Límite: 10/minute"` | Rate limit |

> **Los dos `422` de charset de MySQL son inequívocos y no se solapan**, así que la UI los
> distingue sin heurísticas:
>
> - **Falta `target_charset`** → mensaje dedicado que empieza con `"target_charset es
>   obligatorio…"`. Se valida **antes** de tocar el catálogo, así que no depende de qué
>   `target_collation` se haya mandado. La acción es "completá el charset", no "elegí otra
>   combinación".
> - **El par no está habilitado** → `"…no está habilitada en el catálogo del gateway."` **con
>   `public_context.allowed`**. La acción es elegir una de las opciones que vienen en `allowed`.

---

### 3.2 `GET /collation-conversions/{job_id}` 🔒

Cabecera + estado del job. **Es el endpoint de polling.** No toca el motor y no tiene rate limit
propio.

```http
GET /api/v1/collation-conversions/41
```

**Mientras corre:**

```json
{
  "data": {
    "id": 41,
    "server_id": 3,
    "database_name": "app_db",
    "database_id": 87,
    "engine": "mariadb",
    "mode": "universal",
    "target_charset": "utf8mb4",
    "target_collation": "utf8mb4_unicode_ci",
    "previous_db_charset": "utf8mb3",
    "previous_db_collation": "utf8mb3_general_ci",
    "status": "running",
    "phase": "tables",
    "progress": { "phase": "tables", "tables_done": 7, "objects_done": 0 },
    "error": null,
    "expired": false,
    "created_at": "2026-08-14T09:12:44Z",
    "expires_at": "2026-08-15T09:12:44Z",
    "started_at": "2026-08-14T09:31:10Z",
    "finished_at": null
  }
}
```

#### Los campos que necesitan explicación

| Campo | Qué significa realmente |
|---|---|
| `status` | `pending` \| `running` \| `succeeded` \| `failed` \| `interrupted` \| `canceled`. **`failed` significa "al menos un paso falló"**, no "no se hizo nada" ([§4.6](#46-best-effort-por-ítem-con-una-excepción)) |
| `phase` | `database` \| `tables` \| `objects` \| `done` \| `null`. En modo `columns` **solo** aparecen `tables` y `done` |
| `progress` | `null` hasta el primer avance. Luego **exactamente** tres claves: `{phase, tables_done, objects_done}`. Su `phase` **nunca llega a `"done"`** (se queda en el último valor real); el que sí llega a `"done"` es el `phase` de arriba. `objects_done` cuenta **intentos**, exitosos o fallidos |
| `error` | Mensaje de fallo **global** del job (ej. el corte por `ALTER DATABASE`). El detalle por paso está en `/items` |
| `expired` | **Calculado en cada lectura** (`expires_at < ahora`), no almacenado. Un job puede volverse `expired: true` entre dos polls |

> 🚨 **`progress` NO trae totales.** Solo cuenta lo hecho: `tables_done` y `objects_done`. Para
> mostrar "7 de 23" la UI **debe guardar los totales del preview** (`tables_to_convert`,
> `objects_to_recreate`). Sin eso, lo único honesto que puede mostrar es un contador
> incremental, no una barra de progreso.

> ⚠️ **`progress` se persiste con throttling (~3 s).** No es un contador en tiempo real: entre
> dos pasos rápidos puede no moverse, y un paso lento (una tabla enorme) lo deja quieto varios
> minutos. **Un `progress` que no cambia no significa que el job esté colgado.** No implementes
> un "timeout de UI" basado en eso.

**Errores:** `404` `"Job de conversión de collation no encontrado."`

---

### 3.3 `GET /collation-conversions/{job_id}/objects` 🔌🔒

**Inventario EN VIVO** de la base, para armar la pantalla de selección. Vuelve a leer el motor
en cada llamada. **Rate limit `10/minute`.**

> Este endpoint **no valida el fingerprint**: siempre devuelve la realidad actual. El que corta
> con `409` si la realidad cambió es el `preview`.

#### Ejemplo A — modo `universal` (MySQL/MariaDB)

```http
GET /api/v1/collation-conversions/41/objects
```

```json
{
  "data": {
    "job_id": 41,
    "database": "app_db",
    "engine": "mariadb",
    "mode": "universal",
    "db_charset": "utf8mb3",
    "db_collation": "utf8mb3_general_ci",
    "target_charset": "utf8mb4",
    "target_collation": "utf8mb4_unicode_ci",
    "tables": [
      { "name": "users",      "charset": "utf8mb3", "collation": "utf8mb3_general_ci", "mismatched_columns": 3, "needs_conversion": true,  "columns": null },
      { "name": "orders",     "charset": "utf8mb3", "collation": "utf8mb3_general_ci", "mismatched_columns": 1, "needs_conversion": true,  "columns": null },
      { "name": "already_ok", "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci", "mismatched_columns": 0, "needs_conversion": false, "columns": null }
    ],
    "summary": [
      { "charset": "utf8mb3", "collation": "utf8mb3_general_ci", "table_count": 2, "column_count": null },
      { "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci", "table_count": 1, "column_count": null }
    ],
    "objects": [
      { "object_type": "procedure", "name": "sp_recalcular", "character_set_client": "utf8mb3", "collation_connection": "utf8mb3_general_ci", "database_collation": "utf8mb3_general_ci", "is_outdated": true },
      { "object_type": "function",  "name": "fn_normalizar", "character_set_client": "utf8mb3", "collation_connection": "utf8mb3_general_ci", "database_collation": "utf8mb3_general_ci", "is_outdated": true },
      { "object_type": "trigger",   "name": "tg_users_audit","character_set_client": "utf8mb3", "collation_connection": "utf8mb3_general_ci", "database_collation": "utf8mb3_general_ci", "is_outdated": true },
      { "object_type": "event",     "name": "ev_limpieza",   "character_set_client": "utf8mb3", "collation_connection": "utf8mb3_general_ci", "database_collation": "utf8mb3_general_ci", "is_outdated": true },
      { "object_type": "view",      "name": "v_usuarios",    "character_set_client": "utf8mb3", "collation_connection": "utf8mb3_general_ci", "database_collation": null, "is_outdated": true }
    ],
    "available_collations": [],
    "notes": [],
    "warnings": [
      "Hay 2 columna(s) en OTRA(S) base(s) de datos del servidor con una FK hacia `app_db`: `otra_db`.`pedidos`.`user_id` → `users`. MySQL/MariaDB exigen el MISMO charset/collation en ambos lados de una FK, así que convertir esta BD puede hacer fallar la conversión de esas tablas (3780/1832) o dejar las referencias externas incompatibles. Esas otras bases NO se convierten con este job: planificá una conversión para cada una."
    ]
  }
}
```

Puntos a leer con atención:

- **`columns` es `null` en TODAS las tablas.** No es "no hay columnas": es *"en este modo la
  unidad es la tabla"*. La UI **no debe** renderizar un detalle por columna acá.
- **`database_collation` de la VIEW es `null` SIEMPRE.** `information_schema.VIEWS` no expone esa
  columna, ni en MySQL ni en MariaDB. No es un dato faltante ni un bug: mostrarlo como "—" y no
  como un error.
- **`available_collations` es `[]`.** El selector de collation en este motor sale del catálogo
  global (v7).
- **`mismatched_columns` importa incluso si el default de la tabla ya está al día.** Una tabla
  con `TABLE_COLLATION` correcta puede tener columnas con `COLLATE` explícito distinto; el
  backend solo marca `needs_conversion: false` si **el default y todas las columnas de texto**
  están en el objetivo.

#### Ejemplo B — modo `columns` (PostgreSQL)

```http
GET /api/v1/collation-conversions/42/objects
```

```json
{
  "data": {
    "job_id": 42,
    "database": "app_db",
    "engine": "postgresql",
    "mode": "columns",
    "db_charset": "UTF8",
    "db_collation": "en_US.UTF-8",
    "target_charset": null,
    "target_collation": "es-ES-x-icu",
    "tables": [
      {
        "name": "users",
        "charset": null,
        "collation": null,
        "mismatched_columns": 2,
        "needs_conversion": true,
        "columns": [
          { "name": "email",  "data_type": "character varying(255)", "current_collation": null,  "is_default_collation": true },
          { "name": "nombre", "data_type": "text",                   "current_collation": "C",   "is_default_collation": false },
          { "name": "ya_ok",  "data_type": "text",                   "current_collation": "es-ES-x-icu", "is_default_collation": false }
        ]
      },
      {
        "name": "orders",
        "charset": null,
        "collation": null,
        "mismatched_columns": 1,
        "needs_conversion": true,
        "columns": [
          { "name": "codigo", "data_type": "character varying(32)", "current_collation": "C", "is_default_collation": false }
        ]
      },
      {
        "name": "already_ok",
        "charset": null,
        "collation": null,
        "mismatched_columns": 0,
        "needs_conversion": false,
        "columns": [
          { "name": "descripcion", "data_type": "text", "current_collation": "es-ES-x-icu", "is_default_collation": false }
        ]
      }
    ],
    "summary": [
      { "charset": null, "collation": null,          "table_count": 1, "column_count": 1 },
      { "charset": null, "collation": "C",           "table_count": 2, "column_count": 2 },
      { "charset": null, "collation": "es-ES-x-icu", "table_count": 2, "column_count": 2 }
    ],
    "objects": [],
    "available_collations": [
      { "name": "C",            "provider": "c", "deterministic": true },
      { "name": "en_US",        "provider": "c", "deterministic": true },
      { "name": "es-ES-x-icu",  "provider": "i", "deterministic": true },
      { "name": "und-x-icu",    "provider": "i", "deterministic": true }
    ],
    "notes": [
      "PostgreSQL resuelve la collation dinámicamente: las vistas, funciones y triggers NO congelan la collation de la sesión que los creó, así que no hay objetos que recrear (a diferencia de MySQL/MariaDB).",
      "El ENCODING y el LC_COLLATE de la base son INMUTABLES tras el CREATE DATABASE: esta operación NO los cambia, solo la collation de las columnas indicadas.",
      "Alcance: solo el schema 'public' (misma limitación que el diff de esquema y el clonado)."
    ],
    "warnings": []
  }
}
```

> **`notes` vs. `warnings`.** `notes` son **explicaciones del alcance** (informativas, no
> accionables) y **en PostgreSQL las tres de arriba vienen SIEMPRE**. `warnings` son **riesgos
> concretos** que el operador debe evaluar. La UI debe darles peso visual distinto: `notes` como
> texto de ayuda plegable, `warnings` como avisos destacados.
>
> ⚠️ **`notes` solo existe en el inventario.** La respuesta del `preview` **no tiene** ese campo
> — ahí todo llega como `warnings`.

Puntos a leer con atención:

- **`charset` y `collation` de la tabla son `null`.** En PostgreSQL una tabla **no tiene**
  collation: la tienen sus columnas. La columna "Collation actual" de la grilla de tablas no
  aplica en este modo — hay que mostrar el detalle por columna.
- **`current_collation: null` + `is_default_collation: true` NO significa "ya está bien".** Es
  una columna **sin `COLLATE` explícito**, que hereda la default de la base. Para el motor,
  `pg_catalog.default` y una collation concreta son **objetos distintos**, y mezclarlas es justo
  lo que dispara el conflicto. **Esa columna cuenta como pendiente.** El copy correcto es
  *"heredada de la base"*, nunca *"sin collation"*.
- **En `summary`, el grupo con `collation: null`** es el de las columnas heredadas. `table_count`
  acá significa **"en cuántas tablas aparece"**, no "cuántas tablas la tienen como default".
- **`available_collations` es el selector.** `provider`: `c` = libc, `i` = ICU, `b` = builtin
  (PG 17+). `deterministic: false` (solo ICU) merece una advertencia fuerte
  ([§4.9](#49-advertencias-con-semántica-distinta-por-motor)).
- **Los nombres son case-sensitive**: `c` no es `C`. **No normalices** el texto.

**Errores:** `404` `"Job de conversión de collation no encontrado."` · `404` `"Servidor no
encontrado."` · `500` decrypt · `502`/`504`/`403`/`500` de infraestructura · `429` rate limit.

> **Este endpoint NO valida la expiración ni el estado del job.** Se puede leer el inventario de
> un plan expirado o ya ejecutado — devuelve la realidad actual del motor. Los `410`/`409` son
> exclusivos de `preview` y `execute`. Útil: la UI puede refrescar la grilla sin miedo a un
> `410` inesperado.

---

### 3.4 `POST /collation-conversions/{job_id}/preview` 🔌🔒

Resuelve el **plan final sin ejecutar**: las sentencias exactas, qué se saltea, qué de la
selección ya no existe, las advertencias, y el **`confirm_token`**. Relee el motor y **valida el
fingerprint**. **Rate limit `10/minute`.**

**Body**

| Campo | Tipo | Default | Nota |
|---|---|---|---|
| `tables` | `string[]` | `[]` | Nombres de tabla a convertir |
| `objects` | `{object_type, name}[]` | `[]` | **Solo `universal`.** En `columns` → `422` |
| `include_database_default` | `boolean` | `true` | **Solo `universal`.** En `columns` se **fuerza a `false`** en silencio |
| `force` | `boolean` | `false` | Ignora el fingerprint stale y adopta el inventario nuevo |

> Las dos listas son **explícitas a propósito**: convertir tablas grandes y recrear rutinas son
> operaciones caras e irreversibles, así que el gateway **nunca las asume**. Listas vacías son
> válidas (ej. recrear solo los objetos sin tocar ninguna tabla). Un plan que queda **totalmente
> vacío** se rechaza recién en `execute` ([§3.5](#35-post-collation-conversionsjob_idexecute-)).

#### Ejemplo A — modo `universal`

```http
POST /api/v1/collation-conversions/41/preview
Content-Type: application/json

{
  "tables": ["users", "orders", "already_ok", "ghost_table"],
  "objects": [
    { "object_type": "procedure", "name": "sp_recalcular" },
    { "object_type": "view",      "name": "v_usuarios" },
    { "object_type": "procedure", "name": "sp_fantasma" }
  ],
  "include_database_default": true
}
```

```json
{
  "data": {
    "job_id": 41,
    "database": "app_db",
    "mode": "universal",
    "target_charset": "utf8mb4",
    "target_collation": "utf8mb4_unicode_ci",
    "include_database_default": true,
    "steps": [
      {
        "object_type": "database",
        "object_name": "app_db",
        "action": "alter_database",
        "sql": "ALTER DATABASE `app_db` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        "reason": null,
        "columns": null
      },
      {
        "object_type": "table",
        "object_name": "users",
        "action": "convert_table",
        "sql": "ALTER TABLE `app_db`.`users` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        "reason": null,
        "columns": null
      },
      {
        "object_type": "table",
        "object_name": "orders",
        "action": "convert_table",
        "sql": "ALTER TABLE `app_db`.`orders` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        "reason": null,
        "columns": null
      },
      {
        "object_type": "table",
        "object_name": "already_ok",
        "action": "skip",
        "sql": null,
        "reason": "La tabla y todas sus columnas de texto ya están en la collation objetivo.",
        "columns": null
      },
      {
        "object_type": "procedure",
        "object_name": "sp_recalcular",
        "action": "recreate",
        "sql": "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci; DROP PROCEDURE IF EXISTS `app_db`.`sp_recalcular`; <SHOW CREATE PROCEDURE capturado en la ejecución>",
        "reason": null,
        "columns": null
      },
      {
        "object_type": "view",
        "object_name": "v_usuarios",
        "action": "recreate",
        "sql": "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci; DROP VIEW IF EXISTS `app_db`.`v_usuarios`; <SHOW CREATE VIEW capturado en la ejecución>",
        "reason": null,
        "columns": null
      }
    ],
    "tables_to_convert": 2,
    "tables_skipped": 1,
    "columns_to_convert": 0,
    "objects_to_recreate": 2,
    "missing": [ { "object_type": "procedure", "name": "sp_fantasma" } ],
    "missing_tables": ["ghost_table"],
    "warnings": [
      "ALTER TABLE ... CONVERT TO CHARACTER SET REESCRIBE cada tabla completa (puede tardar y bloquear escrituras en tablas grandes) y convierte también los datos de sus columnas de texto. Si el charset nuevo usa más bytes por carácter (p. ej. utf8mb3 → utf8mb4), un índice existente puede superar el límite de longitud de clave de InnoDB y fallar con (1071, 'Specified key was too long').",
      "Quedan 3 objeto(s) con la collation vieja congelada y sin recrear: function `fn_normalizar`, trigger `tg_users_audit`, event `ev_limpieza`. Es EXACTAMENTE el caso que esta herramienta existe para evitar: sus parámetros VARCHAR/CHAR, variables DECLARE y literales seguirán en la collation anterior y producirán 'Illegal mix of collations' en producción.",
      "Al dropear una PROCEDURE/FUNCTION, MySQL/MariaDB BORRAN sus privilegios…"
    ],
    "confirm_token": "9f2c4b1e8a7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928170615e4d3"
  }
}
```

> 🚨 **El `sql` de un paso `recreate` NO es SQL ejecutable.** Trae literalmente
> `<SHOW CREATE ... capturado en la ejecución>` como marcador de posición: el cuerpo real del
> objeto se captura **durante la ejecución**, no ahora. Es deliberado (el cuerpo puede ser
> enorme y podría cambiar entre el preview y el execute). **Mostralo como la FORMA del paso, no
> como una sentencia para copiar y pegar.** Si la UI ofrece "copiar SQL", ese texto no sirve.

- **`missing` / `missing_tables` NO son errores.** Son elementos de la selección que ya no
  existen en la base (alguien los borró entre el inventario y el preview). Se reportan, se
  excluyen del plan y el preview devuelve `200`. La UI debe mostrarlos como aviso informativo y
  **desmarcarlos de la selección**.
- `tables_skipped` cuenta las tablas seleccionadas que ya estaban al día (`action: "skip"`), no
  las que el usuario no eligió.

#### Ejemplo B — modo `columns`

```http
POST /api/v1/collation-conversions/42/preview
Content-Type: application/json

{ "tables": ["users", "orders", "already_ok"] }
```

```json
{
  "data": {
    "job_id": 42,
    "database": "app_db",
    "mode": "columns",
    "target_charset": null,
    "target_collation": "es-ES-x-icu",
    "include_database_default": false,
    "steps": [
      {
        "object_type": "table",
        "object_name": "users",
        "action": "convert_columns",
        "sql": "ALTER TABLE \"public\".\"users\" ALTER COLUMN \"email\" SET DATA TYPE character varying(255) COLLATE \"es-ES-x-icu\", ALTER COLUMN \"nombre\" SET DATA TYPE text COLLATE \"es-ES-x-icu\"",
        "reason": null,
        "columns": ["email", "nombre"]
      },
      {
        "object_type": "table",
        "object_name": "orders",
        "action": "convert_columns",
        "sql": "ALTER TABLE \"public\".\"orders\" ALTER COLUMN \"codigo\" SET DATA TYPE character varying(32) COLLATE \"es-ES-x-icu\"",
        "reason": null,
        "columns": ["codigo"]
      },
      {
        "object_type": "table",
        "object_name": "already_ok",
        "action": "skip",
        "sql": null,
        "reason": "Ninguna columna de texto de la tabla necesita cambio: ya están todas en la collation objetivo.",
        "columns": null
      }
    ],
    "tables_to_convert": 2,
    "tables_skipped": 1,
    "columns_to_convert": 3,
    "objects_to_recreate": 0,
    "missing": [],
    "missing_tables": [],
    "warnings": [
      "PostgreSQL no permite cambiar el ENCODING ni el LC_COLLATE de una base ya creada: este plan NO incluye ningún ALTER DATABASE (para eso hay que volcar y recargar en una base nueva, es decir el módulo de clonado). Tampoco recrea vistas/funciones/triggers: no hace falta.",
      "Cada ALTER TABLE ... ALTER COLUMN ... TYPE toma un lock ACCESS EXCLUSIVE sobre la tabla durante toda la operación (bloquea hasta los SELECT) y RECONSTRUYE todos los índices que incluyan esas columnas: cambiar la collation cambia el orden, así que el índice viejo no sirve. Como el tipo es el mismo, PostgreSQL normalmente NO reescribe la tabla en sí, pero la reconstrucción de índices en tablas grandes ya es una operación larga. Verificalo contra tu versión antes de una ventana ajustada."
    ],
    "confirm_token": "3a1f7e9d2c8b6a5049382716f5e4d3c2b1a0998877665544332211aabbccddee"
  }
}
```

- **`include_database_default: false` aunque no se haya enviado.** El default del body es `true`
  y el backend lo **fuerza** a `false`. La UI **no debe mostrar ese interruptor** en PostgreSQL.
- **`columns` del paso lista solo las columnas PENDIENTES** — `ya_ok` no aparece. Y **todas van
  en una sola sentencia**: un solo lock, una sola reconstrucción de índices, y (porque PostgreSQL
  sí tiene DDL transaccional) **atomicidad real por tabla**.
- `columns_to_convert: 3` = 2 de `users` + 1 de `orders`.

#### Errores

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `422` | `"PostgreSQL no recrea vistas, funciones, triggers ni eventos en una conversión de collation: los resuelve dinámicamente y no congelan nada. Enviá solo la selección de tablas."` | Se envió `objects` no vacío en modo `columns` |
| `409` | `"El inventario de la base de datos cambió desde que se creó el plan (se agregaron/borraron objetos o cambió alguna collation). Volvé a crear el plan, o reintentá con force=true."` | Fingerprint stale |
| `409` | `"El job ya está en estado '{status}'; crea un plan nuevo para previsualizar otra conversión."` | El job ya se ejecutó / canceló |
| `410` | `"El plan de conversión expiró; vuelve a crearlo."` | `expires_at` pasó |
| `404` | `"Job de conversión de collation no encontrado."` | `job_id` inexistente |
| `422` | *(validación Pydantic)* | `object_type` fuera de los 5 valores, `name` vacío o >512 |
| `429` | rate limit | `10/minute` |

---

### 3.5 `POST /collation-conversions/{job_id}/execute` 🔌🔒

Valida las dos confirmaciones, re-chequea fingerprint y cuarentena, audita **fail-closed** y
**ENCOLA** el job asíncrono. Devuelve el summary (con `status` ya en `pending`/`running`).
**Rate limit `3/minute` — el más restrictivo del módulo.**

**Body**

| Campo | Tipo | Req. | Nota |
|---|---|---|---|
| `confirm_target_name` | `string` | ✅ | Debe ser **exactamente** el nombre de la base |
| `confirm_token` | `string` | ✅ | El que devolvió el **último** `/preview` |
| `force` | `boolean` | ❌ | Default `false`. Cubre **los dos** casos: cuarentena de la BD **y** fingerprint stale (adopta el inventario actual antes de encolar). **No** saltea las confirmaciones |

```http
POST /api/v1/collation-conversions/41/execute
Content-Type: application/json

{
  "confirm_target_name": "app_db",
  "confirm_token": "9f2c4b1e8a7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928170615e4d3",
  "force": false
}
```

```json
{
  "message": "Conversión de collation encolada.",
  "data": {
    "id": 41,
    "server_id": 3,
    "database_name": "app_db",
    "database_id": 87,
    "engine": "mariadb",
    "mode": "universal",
    "target_charset": "utf8mb4",
    "target_collation": "utf8mb4_unicode_ci",
    "previous_db_charset": "utf8mb3",
    "previous_db_collation": "utf8mb3_general_ci",
    "status": "pending",
    "phase": null,
    "progress": null,
    "error": null,
    "expired": false,
    "created_at": "2026-08-14T09:12:44Z",
    "expires_at": "2026-08-15T09:12:44Z",
    "started_at": null,
    "finished_at": null
  }
}
```

> 🚨 **Un `200` acá significa "encolado", NO "convertido".** Es el punto donde más fácil se
> comete el error de mostrar un "listo, conversión completada". La conversión recién empieza:
> hay que pasar a la pantalla de progreso ([§4.12](#412-polling-execute-solo-encola)).

#### Errores

| Código | `detail.msg` (exacto) | Cuándo |
|---|---|---|
| `422` | `"confirm_target_name no coincide con el nombre de la base de datos."` | Nombre mal escrito |
| `422` | `"confirm_token no coincide con el plan actual; volvé a previsualizar."` | El token no corresponde al plan vigente |
| `422` | `"El plan no tiene ningún paso que ejecutar (ni ALTER DATABASE, ni tablas que convertir, ni objetos que recrear)."` | Selección vacía. **Muy fácil de provocar en modo `columns`**, donde no existe el `ALTER DATABASE` que en `universal` alcanzaba para tener un paso |
| `409` | `"Falta previsualizar el plan antes de ejecutarlo."` | Se llamó a `execute` sin un `preview` previo |
| `409` | `"El job ya está en estado '{status}'; no se puede re-ejecutar."` | Reintento sobre un job terminado |
| `409` | `"La base de datos está en cuarentena (status=error). Reintentá con force=true."` | Solo si la BD **está adoptada** y quedó en cuarentena |
| `409` | `"El inventario de la base de datos cambió desde el preview; volvé a previsualizar (o reintentá con force=true)."` | Fingerprint stale entre el preview y el execute. Con `force: true` se adopta el inventario actual — y si eso cambia el plan, el siguiente error es el `422` de `confirm_token` |
| `410` | `"El plan de conversión expiró; vuelve a crearlo."` | TTL vencido |
| `404` | `"Job de conversión de collation no encontrado."` | — |
| `429` | `"Demasiadas solicitudes. Límite: 3/minute"` | Rate limit |

---

### 3.6 `GET /collation-conversions/{job_id}/items` 🔒

Un ítem **por paso ejecutado**, con su resultado. **Paginado** con los query params estándar del
proyecto (`?page=`, `?size=`). Ordenado por `seq` ascendente. Sin rate limit propio.

> **Los ítems se van creando a medida que el worker avanza.** Durante la ejecución, este endpoint
> devuelve una lista **parcial y creciente**: es el detalle en vivo, no solo el informe final.

```http
GET /api/v1/collation-conversions/41/items?page=1&size=20
```

```json
{
  "data": [
    {
      "id": 501, "job_id": 41, "seq": 0,
      "object_type": "database", "object_name": "app_db",
      "previous_charset": "utf8mb3", "previous_collation": "utf8mb3_general_ci",
      "status": "ok", "error": null,
      "grants_captured": null, "grants_reapplied": null, "grants_error": null,
      "columns_affected": null,
      "execution_ms": 34, "executed_at": "2026-08-14T09:31:11Z"
    },
    {
      "id": 502, "job_id": 41, "seq": 1,
      "object_type": "table", "object_name": "users",
      "previous_charset": "utf8mb3", "previous_collation": "utf8mb3_general_ci",
      "status": "ok", "error": null,
      "grants_captured": null, "grants_reapplied": null, "grants_error": null,
      "columns_affected": null,
      "execution_ms": 184220, "executed_at": "2026-08-14T09:34:15Z"
    },
    {
      "id": 503, "job_id": 41, "seq": 2,
      "object_type": "table", "object_name": "orders",
      "previous_charset": "utf8mb3", "previous_collation": "utf8mb3_general_ci",
      "status": "error",
      "error": "Falló en convert_table: (1071, 'Specified key was too long; max key length is 3072 bytes')",
      "grants_captured": null, "grants_reapplied": null, "grants_error": null,
      "columns_affected": null,
      "execution_ms": 2210, "executed_at": "2026-08-14T09:34:18Z"
    },
    {
      "id": 504, "job_id": 41, "seq": 3,
      "object_type": "procedure", "object_name": "sp_recalcular",
      "previous_charset": null, "previous_collation": "utf8mb3_general_ci",
      "status": "ok", "error": null,
      "grants_captured": 2, "grants_reapplied": 2, "grants_error": null,
      "columns_affected": null,
      "execution_ms": 96, "executed_at": "2026-08-14T09:34:19Z"
    }
  ],
  "pagination": { "page": 1, "size": 20, "total": 4, "pages": 1, "has_next": false, "has_prev": false }
}
```

**Valores de `status` del ítem:**

| `status` | Qué significa | Cómo mostrarlo |
|---|---|---|
| `pending` | El ítem se persistió pero todavía no terminó de ejecutarse | Spinner / "en curso" |
| `ok` | El paso se aplicó | ✅ |
| `error` | El paso falló. `error` trae el mensaje del motor | ❌ + el texto de `error` completo |
| `skipped` | El paso **no se ejecutó a propósito** | ⚠️ **No es un error ni un éxito.** Tiene **tres** causas muy distintas — ver el recuadro de abajo |
| `null` | Estado todavía no fijado. Ocurre de verdad: al recrear un objeto, el ítem se persiste **antes** de tocar el motor | Tratar como `pending` |

> 🚨 **`error` está SOBRECARGADO: en las filas `skipped` no contiene un fallo, contiene el
> MOTIVO del salteo.** Si la UI renderiza `error` como "algo salió mal" sin mirar `status`, va a
> mostrar tres falsos errores por conversión exitosa. **Siempre leé `status` primero.**

**Las tres causas de `skipped`, con acciones opuestas:**

| Causa | `error` | `grants_error` | ¿Requiere acción? |
|---|---|---|---|
| La tabla ya estaba al día | `"La tabla y todas sus columnas de texto ya están en la collation objetivo."` (universal) / `"Ninguna columna de texto de la tabla necesita cambio: ya están todas en la collation objetivo."` (columns) | `null` | ❌ No |
| El elemento seleccionado **ya no existe** en la base | `"La tabla ya no existe en la base de datos; se omitió."` / `"El objeto ya no existe en la base de datos; se omitió."` | `null` | ❌ No (informativo) |
| **Rutina con grants ilegibles** (fail-closed) | `"Objeto no recreado: privilegios de rutina ilegibles (fail-closed)."` | **texto largo, poblado** | ✅ **SÍ** — la rutina sigue con la collation vieja ([§4.8](#48-grants_-solo-procedure-y-function)) |

**La presencia de `grants_error` es lo que distingue el caso accionable.** Además, ese tercer
caso **cuenta como fallo del job**: el `status` global termina en `failed`.

> Los elementos de `missing` / `missing_tables` del preview **también se registran como ítems**
> al final de la ejecución, con `status: "skipped"`. No inflan el conteo de errores.

**Ejemplo modo `columns`** (un ítem por tabla, `object_type` siempre `table`):

```json
{
  "data": [
    {
      "id": 610, "job_id": 42, "seq": 0,
      "object_type": "table", "object_name": "users",
      "previous_charset": null, "previous_collation": null,
      "status": "ok", "error": null,
      "grants_captured": null, "grants_reapplied": null, "grants_error": null,
      "columns_affected": 2,
      "execution_ms": 9840, "executed_at": "2026-08-14T09:40:02Z"
    },
    {
      "id": 611, "job_id": 42, "seq": 1,
      "object_type": "table", "object_name": "already_ok",
      "previous_charset": null, "previous_collation": null,
      "status": "skipped", "error": null,
      "grants_captured": null, "grants_reapplied": null, "grants_error": null,
      "columns_affected": null,
      "execution_ms": null, "executed_at": "2026-08-14T09:40:02Z"
    }
  ],
  "pagination": { "page": 1, "size": 20, "total": 2, "pages": 1, "has_next": false, "has_prev": false }
}
```

**Errores:** `404` job inexistente.

---

### 3.7 `POST /collation-conversions/{job_id}/cancel` 🔒

Marca el job como "cancelación solicitada". **Cooperativa**: el worker corta en el próximo punto
seguro, **entre pasos**. Sin rate limit propio.

```http
POST /api/v1/collation-conversions/41/cancel
```

```json
{
  "message": "Cancelación solicitada.",
  "data": { "id": 41, "status": "pending", "phase": null, "…": "…resto del summary…" }
}
```

> 🚨 **La respuesta NO trae `status: "canceled"`.** Devuelve el summary tal como está **en ese
> instante** — típicamente todavía `running`. El estado pasa a `canceled` cuando el worker llega
> al próximo punto seguro. La UI debe mostrar *"cancelación solicitada, esperando a que termine
> el paso en curso"* y **seguir haciendo polling** hasta ver `canceled`.

**Qué NO hace:**

- **No interrumpe un `ALTER TABLE ... CONVERT TO CHARACTER SET` en curso.** Esa sentencia la
  ejecuta el motor; matar la conexión dejaría la tabla a medio reescribir. Si el paso en curso es
  una tabla de 40 GB, la cancelación tarda lo que tarde esa tabla.
- **No revierte lo ya aplicado.** Los pasos completados quedan aplicados. Cancelar deja la base
  **parcialmente convertida** — con todos los riesgos de una conversión parcial
  ([§4.9](#49-advertencias-con-semántica-distinta-por-motor)). Esto **debe** decirse en el
  diálogo de confirmación de la cancelación.
- **No pone la base en cuarentena** ni escribe un `error` en el job. Un job `canceled` tiene
  `error: null`.
- **No se chequea durante la fase `database`.** El `ALTER DATABASE` se ejecuta completo aunque ya
  se haya pedido la cancelación: el primer punto de corte es antes de la primera tabla.

> **Caso borde: cancelar un job `pending` que todavía no arrancó.** El flag queda puesto, pero el
> job **sigue en `pending`** hasta que un worker lo tome; recién ahí se honra — y, por lo
> anterior, después de haber corrido la fase `database` entera. Si el worker nunca lo toma, el
> job queda en `pending` indefinidamente. La UI no debe quedarse esperando un `canceled` que tal
> vez no llegue: tras un tiempo razonable, mostrar *"cancelación pendiente"* y permitir salir.

**Errores:**

| Código | `detail.msg` | Cuándo |
|---|---|---|
| `409` | `"El job no se puede cancelar en estado '{status}'."` | Solo se puede cancelar en **`pending`** o **`running`** |
| `404` | `"Job de conversión de collation no encontrado."` | — |

---

## 4. Semántica de validación (lo que más confusión genera)

### 4.1 `target_charset` es condicional, y el error va en los dos sentidos

| Motor del servidor | `target_charset` | Si se hace mal |
|---|---|---|
| MySQL / MariaDB | **Obligatorio** | **Omitirlo** → `422` *"target_charset es obligatorio para MySQL/MariaDB: esta operación siempre fija charset y collation juntos…"* · **Mandar un par no habilitado** → `422` *"La combinación charset/collation no está habilitada en el catálogo del gateway."* **con `public_context.allowed`**. Son dos errores con causas y acciones distintas, y cada uno lo dice en su propio texto |
| PostgreSQL | **Prohibido** | Enviar **cualquier** valor → **`422`** *"PostgreSQL no tiene charset por columna ni por tabla, y el ENCODING de la base es inmutable…"* |

> **"Prohibido" significa el campo ausente, no el campo vacío.** Omitilo del payload o mandalo
> como `null`. Cualquier string —incluido `""`, que además viola `min_length=1`— dispara el error.

Como la UI **ya sabe el motor**, esto se resuelve mostrando u ocultando el campo. El `422` es la
red de seguridad, no el mecanismo esperado.

### 4.2 `target_collation` es SIEMPRE obligatoria

En los dos modos. **No hay "dejar que el motor decida"**, y es deliberado: unificar la collation
es el objetivo entero de la operación; dejar que el motor elija haría el resultado ambiguo y no
verificable.

No existe una opción "usar el valor por defecto del motor" como sí existe en el formulario de
creación de bases de datos de v7. **No la ofrezcas.**

### 4.3 Dos catálogos distintos que no se mezclan

Este es el punto conceptual que más se confunde, y confundirlo produce un selector que ofrece
valores que el backend rechaza siempre.

| | Catálogo global del gateway | `pg_collation` del servidor |
|---|---|---|
| **Cuándo se usa** | Modo `universal` (MySQL/MariaDB) | Modo `columns` (PostgreSQL) |
| **Cómo se obtiene** | `GET /charset-collation-options?engine_family=mysql&only_enabled=true` (v7) | `available_collations` de `GET /collation-conversions/{id}/objects` |
| **Alcance** | **Global**, administrado por el operador | **Por servidor**, leído EN VIVO |
| **Forma del valor** | `utf8mb4` + `utf8mb4_unicode_ci` | Nombre de objeto: `C`, `en_US`, `es-ES-x-icu` |
| **Case** | El charset se compara sin distinguir mayúsculas | **Case-sensitive**: `c` ≠ `C` |

> 🚨 **La lista de uno NO sirve para poblar el selector del otro.** El catálogo global describe
> los locales con los que se **crea** una base (`en_US.UTF-8`); `pg_collation` describe el
> `COLLATE` de una **columna** (`en_US`). Son espacios de valores distintos aunque los nombres se
> parezcan. Mandar `utf8mb4_unicode_ci` a un PostgreSQL da un `422` explícito.

**El problema del huevo y la gallina en PostgreSQL, y su salida.** `available_collations` solo
llega con el inventario, que exige un plan ya creado, que exige un `target_collation` válido.
Opciones para la UI, en orden de preferencia:

1. **Pedir un endpoint de catálogo por servidor al backend** (no existe hoy). Es la solución
   limpia.
2. **Crear un plan "sonda"** con una collation que exista con altísima probabilidad (`"C"` está
   en todo PostgreSQL), leer `available_collations`, dejar que el usuario elija, y **crear el
   plan definitivo** con la elegida. Los planes son baratos (no tocan el motor más que para
   leer) y expiran solos. Cuidado con el rate limit de `10/minute`.
3. **Campo de texto libre + manejo del `422`**, mostrando el mensaje del backend tal cual. Es lo
   peor de las tres: el mensaje no enumera las opciones válidas.

**`[SUPUESTO F1]`** — se asume que el equipo de frontend elegirá (2) como solución transitoria.
**Confirmar con backend si conviene agregar el endpoint de catálogo**, que es una mejora chica
del lado del servidor.

### 4.4 La selección de objetos es exclusiva de `universal`

En modo `columns`, un `objects` **no vacío** en `/preview` es un **`422` explícito**, no un
campo ignorado en silencio. El backend lo trata como un error de concepto del cliente.

**La UI no debería mostrar ese selector cuando el motor es PostgreSQL.** No es que no haya
objetos que mostrar: es que **la fase entera no existe** en ese modo (y el inventario devuelve
`objects: []` de forma consistente).

### 4.5 Granularidad: siempre por TABLA, nunca por columna

En los **dos** modos, la unidad de selección es la **tabla completa**. No hay forma de convertir
solo algunas columnas de una tabla.

- En `universal` es directo: la tabla es la unidad física de la conversión.
- En `columns` la traducción es interna: cada tabla seleccionada se convierte en **un**
  `ALTER TABLE` con **una cláusula `ALTER COLUMN` por cada columna de texto pendiente**. Las
  columnas ya al día no se tocan; una tabla con todas sus columnas al día queda como `skip`.

> **Implicación de UI:** el inventario del modo `columns` muestra el detalle por columna
> **para transparencia** (qué se va a cambiar y de qué a qué), pero **el checkbox es por tabla**.
> Poner checkboxes por columna sería prometer una granularidad que la API no tiene.

### 4.6 Best-effort por ítem, con UNA excepción

**Regla general:** cada paso es independiente. Una tabla que falla **no frena** a las demás. Un
objeto que falla **no frena** a los demás. Abortar al primer fallo dejaría la base a mitad de
camino, que es el estado más peligroso para esta operación.

**La única excepción:** en modo `universal`, si el **`ALTER DATABASE` inicial falla**, el
pipeline **se corta ahí**. No se toca ninguna tabla ni ningún objeto. El motivo: seguir dejaría
los objetos recreados apuntando a un default que no cambió — o sea, exactamente el problema que
la operación viene a resolver. El job termina con:

```
status: "failed"   phase: "done"
error:  "Falló el ALTER DATABASE; no se continuó con las tablas ni los objetos para no
         dejar la conversión a mitad. Ver los ítems."
```

**El `error` global del job en cualquier otro fallo** es el genérico
`"Al menos un paso falló; ver los ítems."` — que es literalmente una instrucción para la UI.

**Valores exactos de `error` en los estados terminales no-`succeeded`:**

| `status` | `error` |
|---|---|
| `failed` (corte por ALTER DATABASE) | `"Falló el ALTER DATABASE; no se continuó con las tablas ni los objetos para no dejar la conversión a mitad. Ver los ítems."` |
| `failed` (cualquier otro caso) | `"Al menos un paso falló; ver los ítems."` |
| `failed` (drift detectado por el worker) | `"El inventario cambió antes de ejecutar; volvé a planear la conversión."` — **el motor no se tocó**. Es la red de seguridad final del worker y hoy solo se alcanza en una carrera real: la base cambió entre el `execute` y el arranque del worker. **No es consecuencia de usar `force`** ([§4.10](#410-fingerprint-anti-toctou-y-expiración-del-plan)) |
| `interrupted` | `"El proceso se reinició mientras el job estaba en ejecución. Revisá los ítems ya aplicados antes de crear un plan nuevo."` |
| `canceled` | **`null`** — cancelar no escribe ningún `error` |

> 🚨 **Consecuencia obligatoria para la UI: `status: "succeeded"` NO es suficiente.** El job
> termina en `failed` si **al menos un** paso falló, pero **la UI debe mostrar el resultado por
> ítem en todos los casos**, no solo cuando el job falla. Nunca asumas "si terminó sin
> excepción, salió todo bien": un job `failed` puede tener 40 pasos `ok` y uno `error`, y el
> operador necesita saber cuál.

### 4.7 Recuperación: el objeto que se dropeó y no se recreó

MySQL/MariaDB **no tienen DDL transaccional**. Si el `CREATE` falla después de un `DROP`
exitoso, **el objeto desapareció del motor**.

El backend anticipa esto: persiste el DDL capturado **antes** de tocar el motor. Cuando pasa, el
`error` del ítem lo dice explícitamente:

```
Falló en create: (1064, "You have an error in your SQL syntax…") ATENCIÓN: el DROP se
aplicó y el CREATE no, así que el objeto NO existe en la base de datos. El DDL original
está guardado en 'captured_ddl' de este ítem: usalo para recrearlo a mano tras corregir
la causa.
```

y el job pasa a `failed`.

> ⚠️ **Limitación del contrato: `captured_ddl` NO se expone en `CollationConversionItemOut`.**
> El backend lo guarda en la columna `captured_ddl` de la tabla de ítems, pero el serializador de
> la API **no lo incluye**. El operador ve el mensaje que le dice "está en `captured_ddl`" y **no
> tiene forma de obtenerlo desde la UI**.
>
> **`[SUPUESTO F2]`** — se asume que el frontend querrá mostrar ese DDL para que el operador
> pueda recrear el objeto. **Hay que pedirle al backend que lo exponga** (agregar el campo a la
> salida del ítem, probablemente solo para ítems en `error`). Hasta entonces, la UI debe mostrar
> el `error` **completo, sin truncar**, y advertir que la recuperación requiere acceso directo a
> la base de metadatos del gateway.

Este es el peor escenario del módulo y merece el tratamiento visual más fuerte: no es "un paso
falló", es "un objeto de tu base ya no existe".

### 4.8 `grants_*`: solo PROCEDURE y FUNCTION

MySQL/MariaDB **borran los privilegios de una rutina al dropearla** (asimetría real con las
tablas, que sí los conservan). Por eso, para `procedure` y `function`, el gateway lee los grants
**antes** del `DROP` y los **reaplica** después del `CREATE`.

| Campo | Cuándo está poblado | Qué significa |
|---|---|---|
| `grants_captured` | Solo `procedure`/`function`, modo `universal` | Cuántos grants se leyeron antes del DROP |
| `grants_reapplied` | Ídem | Cuántos se reaplicaron después del CREATE |
| `grants_error` | Ídem, solo si hubo problema | El texto explica qué pasó y qué hacer |

**`trigger`, `event` y `view` tienen los tres campos en `null` siempre.** No tienen privilegios
propios a nivel de objeto.

**Los dos casos que la UI debe distinguir:**

1. **No se pudieron LEER los grants → `status: "skipped"`, no `error`.**
   Es **fail-closed**: la rutina **no se dropea**. Dropear a ciegas destruiría privilegios sin
   forma de restaurarlos, que es peor que no convertir el objeto. `grants_error` trae:

   > *"No se pudieron leer los privilegios de la rutina (mysql.procs_priv ilegible y el fallback
   > por SHOW GRANTS también falló). NO se dropeó: el motor borra esos privilegios con el DROP y
   > recrearla sin poder restaurarlos dejaría la rutina sin permisos. Otorgá SELECT…"*

   **La rutina quedó intacta y sin convertir.** La acción del operador es otorgar `SELECT` sobre
   `mysql.procs_priv` a la credencial del gateway y volver a planificar. Mostrar esto como un
   éxito silencioso sería un bug de UI: el objeto sigue con la collation vieja.

2. **Se recreó pero falló la REAPLICACIÓN → `status: "error"`.**
   El objeto existe pero **perdió permisos**. No se puede reportar como éxito: alguna aplicación
   va a empezar a fallar por permisos. Los dos campos:

   - `error`: `"Objeto recreado, privilegios de rutina NO reaplicados."`
   - `grants_error`: *"La rutina SE RECREÓ correctamente, pero no se pudieron reaplicar sus N
     privilegio(s): quedó sin permisos para quien la usaba. Reaplicalos a mano (ver logs del
     gateway para el detalle del motor)."*

> **`grants_captured` > `grants_reapplied` sin error es posible y NO es un bug.** La reaplicación
> omite los grants cuya lista de privilegios queda vacía y no tienen `GRANT OPTION`. No lo
> presentes como una discrepancia alarmante.

### 4.9 Advertencias con semántica DISTINTA por motor

Los `warnings[]` del inventario y del preview vienen **redactados en español y listos para
mostrar tal cual**. **No los reinterpretes, no los resumas y no los recategorices.** Son
específicos, incluyen nombres de objetos reales y códigos de error del motor.

La distinción crítica, que la UI **no debe aplanar** en un "cuidado, conversión parcial":

| | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| **Cuándo aparece el problema** | **Al aplicar el DDL** | **Al CONSULTAR**, después |
| **Códigos** | `3780` *"Referencing column … are incompatible"*, `1832` *"Cannot change column …: used in a foreign key constraint"* | `42P22` *"could not determine which collation to use"*, `42P21` *"collation mismatch between implicit collations"* |
| **Qué se rompe** | La conversión **falla** | La conversión **funciona**, y las consultas empiezan a fallar |
| **Riesgo de FK** | El motor exige la misma collation en ambos lados | El motor (≤17) **no valida nada**; la FK puede quedar lógicamente inconsistente. **PostgreSQL 18 sí lo exige**, y un `pg_dump`/`pg_upgrade` a 18 fallaría |

> Son **dos categorías de riesgo distintas**. En MySQL el riesgo es "esto va a fallar ahora"; en
> PostgreSQL es "esto va a andar y romperse después, en silencio". Un mensaje genérico pierde
> justamente lo que el operador necesita para decidir.

**Advertencias que aparecen y merecen tratamiento propio:**

| Advertencia | Modo | Por qué importa |
|---|---|---|
| Objetos congelados sin recrear | `universal` | **Es el caso que la herramienta existe para evitar.** Debe ser la más visible de todas |
| Longitud de clave InnoDB (`1071`) | `universal` | `utf8mb3` → `utf8mb4` puede hacer que un índice existente supere el límite |
| FKs desde OTRA base del servidor | `universal` | Esas bases **no** se convierten con este job |
| `ACCESS EXCLUSIVE` + reconstrucción de índices | `columns` | Bloquea hasta los `SELECT` |
| **Collation NO DETERMINISTA** | `columns` | Solo ICU. En PG 12–17 **impide `LIKE` y expresiones regulares** sobre las columnas convertidas. Si la app filtra con `LIKE`, **se rompe**. Aparece ya en el **inventario**, no solo en el preview: hay que mostrarla en el momento de elegir la collation |

### 4.10 Fingerprint anti-TOCTOU y expiración del plan

**Fingerprint.** Al crear el plan, el backend calcula un hash del inventario (tablas, charsets,
collations, **columnas** en modo `columns`, y objetos con su `collation_connection`). Ese hash se
vuelve a comparar en **tres** puntos: al previsualizar, al ejecutar y **al arrancar el worker**.

Si cambió → **`409`**, con dos mensajes distintos según el punto:

- En `preview`: *"El inventario de la base de datos cambió desde que se creó el plan… Volvé a
  crear el plan, o reintentá con force=true."*
- En `execute`: *"El inventario de la base de datos cambió desde el preview; volvé a
  previsualizar (o reintentá con force=true)."*

**`force: true` hace lo mismo en los dos endpoints: ADOPTA el inventario actual como base.** No
es "ignorar el error" ni "saltear el chequeo": es *"sí, ya vi que cambió; convertí lo que hay
ahora"*. Tanto `preview` como `execute` re-basan el fingerprint del plan antes de seguir, así que
el worker —que revalida el fingerprint de forma incondicional al arrancar, como red de seguridad
final— encuentra el plan consistente y ejecuta normalmente.

| | `preview(force=true)` | `execute(force=true)` |
|---|---|---|
| Salta la comparación | ✅ | ✅ |
| **Re-basa el fingerprint del plan** | ✅ | ✅ |
| Resultado | Plan nuevo, listo para confirmar | El job se encola y corre |

> ⚠️ **`force` NO desactiva la confirmación.** En `execute`, el `confirm_token` se recalcula
> contra el plan resuelto **sobre el inventario nuevo**. Si el drift **cambia el plan** (por
> ejemplo: una tabla seleccionada ganó una columna de texto, así que ahora hay una columna más
> que convertir), el token del preview viejo **ya no coincide** y la respuesta es un `422`
> *"confirm_token no coincide con el plan actual; volvé a previsualizar."*
>
> Es el comportamiento correcto y deseable: `force` acepta que el inventario cambió, pero **nadie
> confirma un plan que no vio**. Si el drift **no** altera el plan (lo más común: apareció una
> tabla que no está en la selección), el token sigue siendo válido y `execute(force=true)`
> funciona de una.
>
> **Regla práctica para la UI:** ofrecé `force` en el diálogo de ejecución, y si la respuesta es
> el `422` de token, mandá al usuario a rehacer el preview (que es lo que el mensaje ya le dice).

**Expiración.** El plan tiene TTL (`COLLATION_CONVERSION_TTL_HOURS`, **default 24 h**), visible
en `expires_at`. Vencido → **`410`** *"El plan de conversión expiró; vuelve a crearlo."*

- `expired` se **calcula en cada lectura**, no se almacena: un job puede pasar a
  `expired: true` entre dos polls.
- **`force` NO salva un plan expirado.** Hay que replanear desde cero.
- Un plan **ya en ejecución** no se ve afectado: la expiración bloquea `preview`/`execute`, no un
  worker que ya arrancó.

### 4.11 Confirmación de dos factores

`execute` exige **las dos** cosas, y cada una falla con su propio mensaje:

1. **`confirm_target_name`** — el nombre **exacto** de la base. Obliga a identificar
   conscientemente **cuál** base se convierte. Mismo patrón que el `DROP` de una base de datos.
2. **`confirm_token`** — el token que devolvió `/preview`. Está atado al **plan resuelto**:
   cualquier cambio de selección, de objetivo o del inventario lo invalida.

> **Corolario operativo: cada cambio de selección exige un `preview` nuevo.** El token del
> preview anterior deja de servir. La UI debe **invalidar el token guardado** cada vez que el
> usuario toca un checkbox, y deshabilitar el botón de ejecutar hasta que haya un preview
> vigente. Si no, el usuario cambia la selección, ejecuta, y recibe un `422` de token que no va a
> entender.

**`force` en `execute`** cubre **de forma segura los dos** casos: la **cuarentena** (la BD
adoptada quedó en `status=error` por un fallo anterior) y el **fingerprint stale** (adopta el
inventario actual antes de encolar, [§4.10](#410-fingerprint-anti-toctou-y-expiración-del-plan)).
Los dos mensajes de `409` lo dicen explícitamente. Lo que `force` **no** hace es saltear el
`confirm_target_name` ni el `confirm_token`.

### 4.12 Polling: `execute` solo ENCOLA

No hay server-sent events ni websockets: **polling simple**.

```
POST /execute → 200 (status: "pending")
   ↓
cada N segundos:
   GET /collation-conversions/{id}          → status, phase, progress
   GET /collation-conversions/{id}/items    → detalle por paso (lista creciente)
   ↓
hasta que status ∈ {succeeded, failed, interrupted, canceled}
```

**Cadencia sugerida:** 2–3 s los primeros segundos, luego 5–10 s. Una conversión de tablas
grandes tarda **minutos u horas**, así que el polling agresivo no aporta nada: `progress` se
persiste con throttling de ~3 s y un paso lento no lo mueve en absoluto.

**El polling debe sobrevivir a que el usuario se vaya de la pantalla.** Un job de 40 minutos no
se puede atar al ciclo de vida de una vista: al volver, la UI reconstruye todo desde
`GET /collation-conversions/{id}` + `/items`. **Toda la información necesaria está en el
servidor**; no hace falta guardar nada en el cliente salvo los totales del preview
([§3.2](#32-get-collation-conversionsjob_id-)).

---

## 5. Flujos completos, paso a paso

### 5.1 Conversión completa en MySQL/MariaDB (happy path)

```
1. El usuario elige servidor (engine = "mariadb") y base ("app_db").
   → La UI ya sabe: modo universal. Muestra el campo de charset.

2. GET /charset-collation-options?engine_family=mysql&only_enabled=true   (v7)
   → pobla el selector de charset/collation objetivo.
      OJO: engine_family = "mysql" para un servidor MariaDB.

3. POST /servers/3/databases/app_db/collation-conversions
   { "target_charset": "utf8mb4", "target_collation": "utf8mb4_unicode_ci" }
   → 201, job id=41, status "pending", expires_at = +24 h.
   → La UI muestra el encabezado: "utf8mb3_general_ci → utf8mb4_unicode_ci".

4. GET /collation-conversions/41/objects
   → 3 tablas (2 desactualizadas, 1 al día), 5 objetos congelados,
     summary con 2 grupos, 1 warning de FK externa.
   → Pantalla de selección: tablas preseleccionadas donde needs_conversion=true,
     objetos preseleccionados donde is_outdated=true.

5. POST /collation-conversions/41/preview
   { "tables": ["users","orders","already_ok"],
     "objects": [{procedure sp_recalcular}, {view v_usuarios}],
     "include_database_default": true }
   → 200. steps con el ALTER DATABASE primero, 2 convert_table, 1 skip,
     2 recreate. warnings incluye "Quedan 3 objeto(s) con la collation vieja
     congelada y sin recrear: …".
   → La UI GUARDA: confirm_token, tables_to_convert=2, objects_to_recreate=2.

6. El operador ve el warning de objetos sin recrear y VUELVE al paso 4 para
   marcar los 3 restantes → nuevo preview → token NUEVO (el anterior ya no sirve).

7. Diálogo de confirmación:
   - Advertencia de irreversibilidad y de reescritura de tablas.
   - Campo "escribí el nombre de la base": app_db
   POST /collation-conversions/41/execute
   { "confirm_target_name": "app_db", "confirm_token": "…", "force": false }
   → 200 "Conversión de collation encolada.", status "pending".

8. Pantalla de progreso (polling):
   GET /collation-conversions/41       → running / phase "database"
                                       → running / phase "tables", progress {tables_done: 1}
                                       → running / phase "objects", progress {objects_done: 3}
                                       → succeeded / phase "done"
   GET /collation-conversions/41/items → la lista crece paso a paso.

9. Resultado: status "succeeded", todos los ítems en "ok".
   → La UI muestra igualmente el detalle por ítem (§4.6).
```

### 5.2 Conversión en PostgreSQL (happy path)

```
1. El usuario elige servidor (engine = "postgresql") y base ("app_db").
   → Modo columns. La UI OCULTA el campo de charset, el selector de objetos
     y el interruptor de "cambiar el default de la base".

2. Obtener el catálogo de collations del servidor (§4.3):
   POST /servers/5/databases/app_db/collation-conversions
   { "target_collation": "C" }              ← plan sonda
   → 201, job id=42
   GET /collation-conversions/42/objects
   → available_collations: [C, en_US, es-ES-x-icu, und-x-icu]
   → El usuario elige "es-ES-x-icu".

3. POST /servers/5/databases/app_db/collation-conversions
   { "target_collation": "es-ES-x-icu" }    ← plan definitivo
   → 201, job id=43.
   (El plan sonda 42 se abandona: expira solo en 24 h.)

4. GET /collation-conversions/43/objects
   → tables con su detalle POR COLUMNA; objects: [].
   → La grilla muestra, por tabla, qué columnas están fuera del objetivo,
     incluida `email` con "heredada de la base" (current_collation: null).
   → Si la collation elegida fuera no determinista, acá ya aparece el warning
     de LIKE/regex.

5. POST /collation-conversions/43/preview
   { "tables": ["users","orders","already_ok"] }
   → 200. include_database_default: false (forzado).
     2 pasos convert_columns (uno por tabla, con todas sus columnas),
     1 skip. columns_to_convert: 3.
     warnings: ACCESS EXCLUSIVE + reconstrucción de índices.

6. POST /collation-conversions/43/execute
   { "confirm_target_name": "app_db", "confirm_token": "…" }
   → 200, encolado.

7. Polling: phase pasa directo de null → "tables" → "done".
   NUNCA aparecen las fases "database" ni "objects".

8. items: object_type siempre "table", columns_affected poblado (2 y 1),
   already_ok en "skipped".
```

### 5.3 Un objeto falla y el resto continúa

```
1..7. Igual que 5.1, con 3 objetos seleccionados.

8. Durante la ejecución, la captura del DDL de `fn_normalizar` falla.
   → Ese ítem queda en "error"; el worker SIGUE con los demás.

9. GET /collation-conversions/41 → status "failed", phase "done".
   GET /collation-conversions/41/items:
      database  app_db          → ok
      table     users           → ok
      table     orders          → ok
      procedure sp_recalcular   → ok   (grants_captured 2, grants_reapplied 2)
      function  fn_normalizar   → error  "Falló en capture: …"
      view      v_usuarios      → ok

10. La UI NO muestra "la conversión falló" a secas. Muestra:
    "Conversión completada con 1 error de 6 pasos."
    + la lista, con el ítem fallido destacado y su `error` completo.
    + acción sugerida: "Crear un plan nuevo solo con `fn_normalizar`."
```

**El punto de diseño:** `failed` significa *"al menos un paso falló"*. Presentarlo como "todo
falló" es incorrecto y hace que el operador repita trabajo ya hecho — repetir un
`CONVERT TO CHARACTER SET` sobre una tabla enorme que ya se convirtió cuesta horas.

### 5.4 El `ALTER DATABASE` falla y corta todo

```
1..7. Igual que 5.1.

8. El primer paso, ALTER DATABASE, falla (p. ej. la credencial pseudo-root
   no tiene ALTER sobre la base).

9. El pipeline SE CORTA. No se toca ninguna tabla ni ningún objeto.

10. GET /collation-conversions/41 → status "failed", phase "database",
    error con el mensaje del motor.
    GET /collation-conversions/41/items → UN SOLO ítem:
      database app_db → error "Falló en alter_database: (1044, 'Access denied…')"

11. La UI reconoce el caso por su forma (un único ítem, de object_type
    "database", en error) y muestra un mensaje distinto del de 5.3:

    "La conversión no se ejecutó. No se pudo cambiar el charset por defecto de
     la base, y continuar habría dejado los objetos recreados apuntando al
     default viejo. La base NO fue modificada."
    + "Corregí la causa y volvé a planificar."
```

**Es el único fallo del que se puede afirmar "la base no cambió".** Decirlo explícitamente es
valioso: en cualquier otro fallo la respuesta honesta es "revisá el detalle por ítem".

> Alternativa que la UI puede ofrecer: **volver a planificar con
> `include_database_default: false`**, convirtiendo tablas y objetos sin tocar el default de la
> base. Es un plan válido, aunque deja el default viejo para los objetos **nuevos** que se creen
> después.

### 5.5 Fingerprint stale durante el preview

```
1..4. Igual que 5.1. El operador se queda mirando la pantalla de selección.
      Mientras tanto, un deploy crea la tabla `sessions` en app_db.

5. POST /collation-conversions/41/preview { "tables": ["users","orders"] }
   → 409 "El inventario de la base de datos cambió desde que se creó el plan
          (se agregaron/borraron objetos o cambió alguna collation). Volvé a
          crear el plan, o reintentá con force=true."

6. La UI NO muestra un error rojo sin salida. Muestra el mensaje del backend
   y DOS acciones concretas:

   a) "Recargar el inventario"  ← la opción recomendada
      → GET /collation-conversions/41/objects   (siempre devuelve la realidad)
      → la grilla se repuebla; `sessions` aparece como no seleccionada
      → el operador decide si la incluye
      → POST /preview con force=true  → 200

   b) "Continuar con el plan anterior"
      → POST /preview con force=true directamente
      → 200, pero el plan resultante puede diferir del que el operador vio

7. En AMBOS casos la UI vuelve a mostrar el preview COMPLETO antes de
   habilitar el botón de ejecutar. Con force, el plan pudo cambiar.

8. POST /execute  { …, "force": false }
   → 200, encolado. El preview ya adoptó el inventario nuevo, así que el
     worker lo encuentra consistente.
```

**Variante: el drift aparece recién ENTRE el preview y el execute**

```
5'. El preview salió 200 y el operador tardó en confirmar. Mientras tanto se
    creó la tabla `sessions`.

6'. POST /execute { …, "force": false }
    → 409 "El inventario de la base de datos cambió desde el preview; volvé a
           previsualizar (o reintentá con force=true)."

7'. La UI ofrece las dos salidas, y las DOS son válidas:

    a) [Revisar los cambios]   ← recomendada
       → POST /preview con force=true → mostrar el plan nuevo → POST /execute

    b) [Ejecutar de todos modos]
       → POST /execute con force=true
       → adopta el inventario actual y encola.
       → SI el drift no cambió el plan (caso típico: la tabla nueva no está
         en la selección) → 200 y la conversión corre.
       → SI el drift SÍ cambió el plan → 422 "confirm_token no coincide con el
         plan actual; volvé a previsualizar." → la UI cae al camino (a).
```

**Regla:** ante estos `409` **la selección del usuario no se pierde**. Se recarga el inventario y
se reaplica la selección sobre los elementos que siguen existiendo.

### 5.6 Plan expirado

```
1..5. El operador crea el plan y el preview un viernes, y no ejecuta.

6. El lunes: POST /collation-conversions/41/execute
   → 410 "El plan de conversión expiró; vuelve a crearlo."

7. La UI NO ofrece "reintentar" (force NO sirve acá). Ofrece:

   "Este plan expiró el 15/08 a las 09:12. Los planes tienen una vigencia de
    24 horas porque describen el estado de la base en el momento en que se
    crearon."
   [Crear un plan nuevo con la misma configuración]   ← reusa target_charset
                                                        y target_collation
```

**Anticipación en el cliente:** `expires_at` viene en el summary. La UI puede mostrar un contador
("este plan vence en 3 h") y **advertir antes** de que el `410` ocurra, en vez de esperarlo. El
`410` sigue siendo la red de seguridad.

### 5.7 Rutina sin grants legibles: `skipped` fail-closed

```
1..8. Igual que 5.1, con `sp_recalcular` seleccionada.

9. El gateway no puede leer mysql.procs_priv y el fallback por SHOW GRANTS
   tampoco funciona.
   → NO dropea la rutina. Ítem con status "skipped" + grants_error.

10. GET /collation-conversions/41/items:
      procedure sp_recalcular → skipped
        grants_error: "No se pudieron leer los privilegios de la rutina
                       (mysql.procs_priv ilegible …). NO se dropeó: … Otorgá
                       SELECT sobre mysql.procs_priv …"
      view v_usuarios → ok      ← las vistas no tienen grants propios

11. La UI NO muestra "skipped" con el mismo ícono que "ya estaba al día".
    Muestra:
    ⚠️ "sp_recalcular NO se convirtió: sigue con la collation vieja."
       + el texto de grants_error completo
       + "Otorgá SELECT sobre mysql.procs_priv a la credencial del gateway y
          volvé a planificar."
```

**El punto de diseño:** `skipped` tiene **dos causas con acciones opuestas**. Una tabla `skipped`
porque ya estaba al día no requiere nada. Una rutina `skipped` por grants ilegibles **sigue
rota** y requiere intervención. **La presencia de `grants_error` es lo que las distingue.**

---

## 6. Interpretación visual: pantallas, estados y transiciones

Conceptual — sin tecnología ni implementación. Cuatro pantallas encadenadas.

### 6.1 Pantalla 1: crear el plan

```
[Barra superior]
  Conversión de collation                          [Cancelar]

[Paso 1 — Destino]
  Servidor:      ( selector )      → determina el MODO
  Base de datos: ( selector )      → se habilita al elegir servidor

[Paso 2 — Objetivo]

  ── si el motor es MySQL / MariaDB ─────────────────────────────────
  Juego de caracteres y ordenamiento:  ( selector del catálogo global )
    ⭐ utf8mb4 · utf8mb4_unicode_ci
       utf8mb4 · utf8mb4_general_ci
    Ayuda: "Solo se listan las combinaciones habilitadas por el
            administrador del gateway."
    ⚠️ NO incluir la opción "usar el valor por defecto del motor" (§4.2)

  ── si el motor es PostgreSQL ──────────────────────────────────────
  Collation objetivo:  ( selector de available_collations )
       C            (libc)
       en_US        (libc)
       es-ES-x-icu  (ICU)
    Ayuda: "Estas son las collations instaladas en ESTE servidor."
    ⚠️ NO mostrar campo de charset/encoding (§4.1)
    ⚠️ Aviso permanente: "El ENCODING y el LC_COLLATE de la base son
       inmutables: esta operación cambia la collation de las COLUMNAS
       de texto, no la de la base."

[Aviso de alcance — SIEMPRE visible]
  ⚠️ "Esta operación es IRREVERSIBLE y no tiene deshacer. Volver atrás
     requiere otra conversión, con el mismo costo."

                                        [Continuar → inventario]
```

**Estados:**

| Estado | Qué se muestra |
|---|---|
| `sin servidor` | Todo lo demás deshabilitado. No se conoce el modo |
| `cargando catálogo` | Selector de objetivo deshabilitado; el resto usable |
| `sin collations` (PG) | *"No se pudieron leer las collations de este servidor."* + reintento. **Bloquea**: sin objetivo no hay plan |
| `422 de objetivo` | Mensaje del backend tal cual; el resto del formulario **intacto** |

### 6.2 Pantalla 2: inventario y selección

```
[Encabezado]
  app_db  ·  MariaDB  ·  utf8mb3_general_ci  →  utf8mb4_unicode_ci
  Plan #41 · vence en 23 h 51 min                    [Recargar inventario]

[Resumen agrupado — de `summary`]
  ┌──────────────────────────────────────────────────────────┐
  │ utf8mb3 · utf8mb3_general_ci        2 tablas   ← a convertir │
  │ utf8mb4 · utf8mb4_unicode_ci        1 tabla    ✓ al día      │
  └──────────────────────────────────────────────────────────┘
  "Tenés 2 collations distintas en esta base."

[Advertencias — de `warnings`, TAL CUAL]
  ⚠️ Hay 2 columna(s) en OTRA(S) base(s) de datos del servidor con una FK…

[Tablas]                                       [☑ Seleccionar las pendientes]
  ☑  users        utf8mb3_general_ci   3 columnas fuera del objetivo
  ☑  orders       utf8mb3_general_ci   1 columna fuera del objetivo
  ☐  already_ok   utf8mb4_unicode_ci   ✓ al día

[Objetos congelados]        ← SOLO si mode = "universal"
  Ayuda: "Estos objetos guardan la collation con la que fueron creados.
          Si no se recrean, seguirán comparando texto en la collation
          vieja y producirán errores en producción."
  ☑  procedure  sp_recalcular    utf8mb3_general_ci   desactualizado
  ☑  function   fn_normalizar    utf8mb3_general_ci   desactualizado
  ☑  trigger    tg_users_audit   utf8mb3_general_ci   desactualizado
  ☑  event      ev_limpieza      utf8mb3_general_ci   desactualizado
  ☑  view       v_usuarios       utf8mb3_general_ci   desactualizado

[Opciones]                  ← SOLO si mode = "universal"
  ☑ Cambiar también el juego de caracteres por defecto de la base
    Ayuda: "Afecta a los objetos que se creen DESPUÉS. No modifica las
            tablas existentes."

                                            [Previsualizar el plan →]
```

**Variante del modo `columns` (PostgreSQL)** — la sección de objetos y la de opciones
**no existen**, y cada tabla se expande a su detalle por columna:

```
[Tablas]
  ☑  users            2 de 3 columnas fuera del objetivo        [▾]
       email     character varying(255)   heredada de la base  → es-ES-x-icu
       nombre    text                     C                    → es-ES-x-icu
       ya_ok     text                     es-ES-x-icu          ✓ al día
  ☑  orders           1 de 1 columna fuera del objetivo         [▾]
  ☐  already_ok       ✓ al día

  ⓘ La selección es por TABLA. Todas las columnas pendientes de una tabla
    se convierten juntas, en una sola operación.
```

**Reglas de la pantalla:**

- **Preseleccionar** las tablas con `needs_conversion: true` y los objetos con
  `is_outdated: true`. Es lo correcto por defecto: una conversión parcial es el riesgo principal.
- **Desmarcar un objeto congelado debe advertir**, no ser un clic silencioso.
- `already_ok` se muestra pero **no se preselecciona**: convertirla igual reescribe la tabla
  entera sin cambiar nada.
- El **contador de vencimiento** del plan es visible ([§5.6](#56-plan-expirado)).
- **`current_collation: null` se muestra como "heredada de la base"**, nunca como "sin
  collation" ni como celda vacía.

### 6.3 Pantalla 3: preview y confirmación

```
[Resumen del plan]
  2 tablas a convertir · 1 salteada · 5 objetos a recrear
  + cambiar el default de la base

[Elementos que ya no existen]        ← si missing / missing_tables
  ⓘ 1 tabla y 1 objeto de tu selección ya no existen en la base y se
    excluyeron: `ghost_table`, procedure `sp_fantasma`.

[Advertencias]                       ← de `warnings`, TAL CUAL, sin resumir
  ⚠️ ALTER TABLE ... CONVERT TO CHARACTER SET REESCRIBE cada tabla completa…
  ⚠️ Quedan 3 objeto(s) con la collation vieja congelada y sin recrear…

[Pasos]                                              [Ver SQL ▾]
  1. base de datos  app_db          cambiar default
  2. tabla          users           convertir
  3. tabla          orders          convertir
  4. tabla          already_ok      saltear — ya está en la collation objetivo
  5. procedure      sp_recalcular   recrear
  6. vista          v_usuarios      recrear
  ⓘ El SQL de los pasos "recrear" se muestra como forma: el cuerpo real
    del objeto se captura durante la ejecución.

[Confirmación]
  🚨 Esta operación es IRREVERSIBLE, puede tardar horas y bloquea
     escrituras en las tablas grandes.
  Escribí el nombre de la base para confirmar:  [ ____________ ]

                          [Volver a la selección]   [Ejecutar conversión]
```

- **`[Ejecutar]` deshabilitado** hasta que `confirm_target_name` coincida exactamente.
- **Volver a la selección INVALIDA el token** ([§4.11](#411-confirmación-de-dos-factores)): al
  volver hay que rehacer el preview.
- Los pasos `skip` se muestran **con su `reason`**, no se ocultan: son la prueba de que el
  backend entendió el estado actual.

### 6.4 Pantalla 4: progreso y resultado

```
[Encabezado]
  Convirtiendo app_db → utf8mb4_unicode_ci
  Estado: en curso · Fase: tablas
  ⏱ Iniciado hace 4 min                       [Cancelar conversión]

[Progreso]
  Tablas   ▓▓▓▓▓▓▓░░░░░░░  7 / 23      ← el total sale del PREVIEW (§3.2)
  Objetos  ░░░░░░░░░░░░░░  0 / 5
  ⓘ El progreso se actualiza cada pocos segundos. Una tabla grande puede
    tardar minutos sin que el contador se mueva.

[Detalle por paso]                                       (actualiza en vivo)
  ✅ base de datos  app_db          34 ms
  ✅ tabla          users           3 min 4 s
  ❌ tabla          orders          2,2 s
       (1071, 'Specified key was too long; max key length is 3072 bytes')
  ⏳ tabla          products        en curso
```

**Estados terminales:**

| `status` | Encabezado | Detalle |
|---|---|---|
| `succeeded` | ✅ *"Conversión completada."* | **Igual se muestra la lista de ítems** (§4.6) |
| `failed` | ⚠️ *"Conversión completada con N errores de M pasos."* — **nunca** "la conversión falló" | Ítems fallidos destacados arriba, con su `error` **completo** |
| `failed` + un solo ítem `database` en error | ❌ *"La conversión no se ejecutó. La base NO fue modificada."* (§5.4) | Caso especial, mensaje propio |
| `canceled` | ⏹ *"Conversión cancelada."* + ⚠️ *"Los pasos ya aplicados NO se revirtieron: la base quedó parcialmente convertida."* | Lista completa: qué alcanzó a aplicarse |
| `interrupted` | ⚠️ *"El gateway se reinició durante la conversión."* | *"Revisá los pasos ya aplicados antes de crear un plan nuevo."* |

**Tratamiento especial obligatorio** — un ítem cuyo `error` menciona que **el DROP se aplicó y el
CREATE no** ([§4.7](#47-recuperación-el-objeto-que-se-dropeó-y-no-se-recreó)) es el peor caso del
módulo: **un objeto de la base ya no existe**. Va arriba de todo, en rojo, con el texto de
`error` íntegro y sin truncar.

**Diálogo de cancelación:**

```
[Modal] Cancelar la conversión

  La cancelación es cooperativa: el paso EN CURSO va a terminar (una tabla
  grande puede tardar varios minutos más). Solo se detienen los pasos que
  todavía no empezaron.

  ⚠️ Lo ya convertido NO se revierte. La base va a quedar PARCIALMENTE
     convertida, con los riesgos de una conversión parcial.

              [Seguir convirtiendo]   [Solicitar cancelación]
```

### 6.5 Transiciones

```
Crear plan
  → [elegir servidor] → se fija el MODO → el formulario se adapta
  → [enviar]
       → 201 → Inventario y selección
       → 422 objetivo → mismo formulario, mensaje del backend, resto intacto

Inventario y selección
  → [recargar]      → GET /objects (siempre la realidad actual)
  → [previsualizar] → POST /preview
       → 200 → Preview y confirmación   (se guardan token + totales)
       → 409 fingerprint → diálogo de §5.5 → recargar o force → reintentar
       → 410 expirado → "crear un plan nuevo" (force NO sirve)
       → 422 objetos en modo columns → bug de la UI: no debió mostrar ese selector

Preview y confirmación
  → [volver]   → Inventario   ⚠️ INVALIDA el confirm_token
  → [ejecutar] → POST /execute
       → 200 → Progreso (polling)
       → 422 nombre / token / plan vacío → mismo diálogo, mensaje del backend
       → 409 sin preview → bug de la UI: no debió habilitar el botón
       → 409 cuarentena o fingerprint → ofrecer [Ejecutar de todos modos] (force=true)
              → 200 → Progreso
              → 422 token (el drift cambió el plan) → volver al Preview
       → 410 expirado → replanear

Progreso (polling)
  → [cancelar] → POST /cancel → seguir haciendo polling hasta ver "canceled"
  → status terminal → Resultado
  → [salir de la pantalla] → el job SIGUE. Al volver se reconstruye desde
                             GET /{id} + /items
```

---

## 7. Tipos (referencia rápida)

```ts
type ServerEngine = "mysql" | "mariadb" | "postgresql";
type ConversionMode = "universal" | "columns";   // lo determina el motor, NO el usuario

type JobStatus  = "pending" | "running" | "succeeded" | "failed" | "interrupted" | "canceled";
type JobPhase   = "database" | "tables" | "objects" | "done";   // columns: solo tables|done
type ItemStatus = "pending" | "ok" | "error" | "skipped";

type FrozenObjectType = "procedure" | "function" | "trigger" | "event" | "view";
// 'table' NO está: una tabla se selecciona por `tables`, no por `objects`.

type StepAction =
  | "alter_database" | "convert_table" | "recreate"   // universal
  | "convert_columns"                                  // columns
  | "skip";                                            // ambos

// --------------------------------------------------------------- Entrada

interface CollationConversionCreate {
  target_charset?: string | null;   // OBLIGATORIO en universal · PROHIBIDO en columns (422)
  target_collation: string;         // SIEMPRE obligatorio, 1..64
}

interface CollationObjectRef {
  object_type: FrozenObjectType;
  name: string;                     // 1..512
}

interface CollationConversionPreviewIn {
  tables?: string[];                        // default []
  objects?: CollationObjectRef[];           // default [] · no vacío en columns => 422
  include_database_default?: boolean;       // default true · FORZADO a false en columns
  force?: boolean;                          // default false · adopta el inventario actual
}

interface CollationConversionExecuteIn {
  confirm_target_name: string;      // nombre EXACTO de la base
  confirm_token: string;            // el del ÚLTIMO preview
  force?: boolean;                  // default false · cubre cuarentena Y fingerprint:
                                    // adopta el inventario actual antes de encolar.
                                    // NO saltea confirm_target_name ni confirm_token.
}

// --------------------------------------------------------------- Salida

interface CollationColumnOut {
  name: string;
  data_type: string;                    // tipo exacto: "character varying(255)"
  current_collation: string | null;     // null + is_default_collation => heredada de la base
  is_default_collation: boolean;        // ¡igual cuenta como PENDIENTE!
}

interface CollationTableOut {
  name: string;
  charset: string | null;               // null en PostgreSQL (las tablas no tienen collation)
  collation: string | null;
  mismatched_columns: number;
  needs_conversion: boolean;
  columns: CollationColumnOut[] | null; // null en universal · poblado en columns
}

interface CollationGroupOut {
  charset: string | null;
  collation: string | null;
  table_count: number;                  // columns: "en cuántas tablas aparece"
  column_count: number | null;          // null en universal
}

interface CollationOptionOut {          // fila de pg_collation
  name: string;                         // CASE-SENSITIVE: "c" !== "C"
  provider: "c" | "i" | "b" | null;     // libc | ICU | builtin (PG 17+)
  deterministic: boolean;               // false => rompe LIKE/regex en PG 12–17
}

interface CollationObjectOut {
  object_type: string;                  // uno de los 5 congelados
  name: string;
  character_set_client: string | null;
  collation_connection: string | null;  // la collation CONGELADA
  database_collation: string | null;    // SIEMPRE null en las VIEW
  is_outdated: boolean;
}

interface CollationInventoryOut {
  job_id: number;
  database: string;
  engine: ServerEngine;
  mode: ConversionMode;
  db_charset: string | null;            // PG: ENCODING
  db_collation: string | null;          // PG: LC_COLLATE (INMUTABLE, solo contexto)
  target_charset: string | null;
  target_collation: string;
  tables: CollationTableOut[];
  summary: CollationGroupOut[];
  objects: CollationObjectOut[];        // SIEMPRE [] en columns
  available_collations: CollationOptionOut[];  // SIEMPRE [] en universal
  notes: string[];
  warnings: string[];                   // listos para mostrar TAL CUAL
}

interface CollationConversionStepOut {
  object_type: string;                  // "database" | "table" | uno de los 5 congelados
  object_name: string;
  action: StepAction;
  sql: string | null;                   // en 'recreate' es una FORMA, no SQL ejecutable
  reason: string | null;                // motivo del 'skip'
  columns: string[] | null;             // solo columns: qué columnas altera ese paso
}

interface CollationConversionPreviewOut {
  job_id: number;
  database: string;
  mode: ConversionMode;
  target_charset: string | null;
  target_collation: string;
  include_database_default: boolean;    // siempre false en columns
  steps: CollationConversionStepOut[];
  tables_to_convert: number;            // ← GUARDAR: el polling no trae totales
  tables_skipped: number;
  columns_to_convert: number;           // 0 en universal
  objects_to_recreate: number;          // ← GUARDAR
  missing: CollationObjectRef[];        // seleccionados que ya no existen (NO abortan)
  missing_tables: string[];
  warnings: string[];
  confirm_token: string;                // ← GUARDAR · lo invalida cualquier cambio de selección
}

interface CollationConversionSummaryOut {
  id: number;
  server_id: number;
  database_name: string;
  database_id: number | null;           // null = la base NO está adoptada
  engine: ServerEngine;
  mode: ConversionMode;
  target_charset: string | null;
  target_collation: string;
  previous_db_charset: string | null;
  previous_db_collation: string | null;
  status: JobStatus;
  phase: JobPhase | null;
  progress: { phase: JobPhase | null; tables_done: number; objects_done: number } | null;
  error: string | null;                 // fallo GLOBAL del job
  expired: boolean;                     // CALCULADO en cada lectura
  created_at: string;                   // ISO 8601
  expires_at: string;
  started_at: string | null;
  finished_at: string | null;
}

interface CollationConversionItemOut {
  id: number;
  job_id: number;
  seq: number;                          // orden de ejecución
  object_type: string;
  object_name: string;
  previous_charset: string | null;
  previous_collation: string | null;
  status: ItemStatus | null;
  error: string | null;                 // mostrar COMPLETO, sin truncar
  grants_captured: number | null;       // solo procedure/function, universal
  grants_reapplied: number | null;
  grants_error: string | null;          // su presencia distingue los dos tipos de 'skipped'
  columns_affected: number | null;      // solo columns
  execution_ms: number | null;
  executed_at: string | null;
  // NO existe `captured_ddl` en la salida, aunque el backend lo guarde (§4.7)
}

// --------------------------------------------------------- Envelopes

interface ApiResponse<T> {
  data?: T;
  message?: string;
  pagination?: {                        // solo en /items · se llama `pagination`, NO `meta`
    page: number; size: number; total: number;
    pages: number; has_next: boolean; has_prev: boolean;
  };
  // NO existe un campo `success`. El éxito es el status HTTP 2xx.
}

interface ApiErrorBody {
  detail: {
    msg: string;                        // español, listo para mostrar — LO ÚNICO CONFIABLE
    type: string;                       // "AppHttpException" | "RequestValidationError" | …
    public_context?: unknown;           // viaja en TODOS los ambientes, pero solo en 2 errores
    context?: unknown;                  // SOLO en development — NUNCA dependas de esto
    loc?: unknown;                      // SOLO en development
  };
}
// El identificador de soporte viaja en el header de respuesta X-Request-ID.

// Los DOS únicos public_context del módulo, ambos al CREAR el plan:

interface CharsetRejectedContext {      // 422 "…no está habilitada en el catálogo del gateway."
  engine_family: "mysql" | "postgresql";
  requested: { charset: string | null; collation: string | null };
  allowed: { charset: string; collation: string | null; is_default: boolean }[];  // <= 50
  truncated: boolean;
}

interface PgCollationMissingContext {   // 422 "La collation pedida no existe en este servidor…"
  available_count: number;              // SOLO el conteo — la lista está en /objects
}
```

---

## 8. Matriz de errores

```
--- Crear el plan (POST /servers/{id}/databases/{db}/collation-conversions) ---
422 — target_charset enviado a un servidor PostgreSQL
      msg: "PostgreSQL no tiene charset por columna ni por tabla, y el ENCODING de la
            base es inmutable tras el CREATE DATABASE: no envíes target_charset, solo
            target_collation."
422 — MySQL/MariaDB: el PAR no existe o está DESHABILITADO en el catálogo
      msg: "La combinación charset/collation no está habilitada en el catálogo del gateway."
      public_context: { engine_family, requested, allowed[<=50], truncated }
      → ÚNICO error del módulo que permite repoblar el selector en el acto
422 — MySQL/MariaDB: se OMITIÓ target_charset
      msg: "target_charset es obligatorio para MySQL/MariaDB: esta operación siempre fija
            charset y collation juntos (ALTER DATABASE/ALTER TABLE ... CHARACTER SET ...
            COLLATE ... exige ambos)."
      → sin public_context. NO es un error de Pydantic: el campo es opcional en el schema
      → se valida ANTES del catálogo: no se confunde nunca con el error del par
422 — PostgreSQL: la collation no existe en ESE servidor (o no sirve con el encoding)
      msg: "La collation pedida no existe en este servidor PostgreSQL (o no es usable con
            el encoding de esta base). El catálogo de collations depende de los locales
            instalados en el SO de cada servidor: consultá las disponibles en el inventario
            del plan (available_collations)."
      public_context: { available_count }   ← solo el conteo, NO la lista
      → ojo con el CASE: "c" no es "C"
422 — el motor no es mysql/mariadb/postgresql
      msg: "La conversión de charset/collation no aplica a este motor."
422 — nombre de base inválido
      msg: "El base de datos es vacío o inválido." / "…excede la longitud máxima." /
           "…contiene caracteres no permitidos."
422 — validación Pydantic (target_collation ausente, longitudes)
      type: "RequestValidationError"
404 — "La base de datos no existe en el servidor."
404 — "Servidor no encontrado."
409 — "Operación no permitida sobre una base de datos del sistema."
500 — "No se pudo descifrar la credencial del servidor."
502/504/403/500 — fallos de infraestructura contra el motor destino
429 — rate limit 10/minute

--- Inventario (GET /collation-conversions/{id}/objects) ---
404 — "Job de conversión de collation no encontrado."
404 — "Servidor no encontrado."
500 — "No se pudo descifrar la credencial del servidor."
502/504/403/500 — fallos de infraestructura
429 — rate limit 10/minute
      ⚠️ NO valida expiración ni estado del job: nunca devuelve 410 ni 409

--- Preview (POST /collation-conversions/{id}/preview) ---
422 — objects no vacío en modo columns
      msg: "PostgreSQL no recrea vistas, funciones, triggers ni eventos en una conversión
            de collation: los resuelve dinámicamente y no congelan nada. Enviá solo la
            selección de tablas."
      → bug de la UI: ese selector no debió mostrarse
422 — validación Pydantic (object_type fuera de los 5 valores, name vacío o >512)
409 — fingerprint stale
      msg: "El inventario de la base de datos cambió desde que se creó el plan… Volvé a
            crear el plan, o reintentá con force=true."
409 — el job ya se ejecutó o se canceló
      msg: "El job ya está en estado '{status}'; crea un plan nuevo para previsualizar
            otra conversión."
410 — plan expirado (force NO sirve)
404 — job inexistente
429 — rate limit 10/minute

--- Execute (POST /collation-conversions/{id}/execute) ---
422 — "confirm_target_name no coincide con el nombre de la base de datos."
422 — "confirm_token no coincide con el plan actual; volvé a previsualizar."
      → causa nº 1: se cambió la selección sin rehacer el preview
422 — "El plan no tiene ningún paso que ejecutar (ni ALTER DATABASE, ni tablas que
       convertir, ni objetos que recrear)."
      → muy fácil de provocar en modo columns con la selección vacía
409 — "Falta previsualizar el plan antes de ejecutarlo."
409 — "El job ya está en estado '{status}'; no se puede re-ejecutar."
409 — "La base de datos está en cuarentena (status=error). Reintentá con force=true."
      → solo si la base está ADOPTADA en el inventario
409 — "El inventario de la base de datos cambió desde el preview; volvé a previsualizar
       (o reintentá con force=true)."
       → force=true acá es SEGURO: adopta el inventario actual antes de encolar (§4.10).
         Si el drift cambia el plan resuelto, el siguiente error es el 422 de
         confirm_token (nadie confirma un plan que no vio).
410 — plan expirado
404 — job inexistente
429 — rate limit 3/minute   ← el más restrictivo

--- Items / Summary / Cancel ---
404 — "Job de conversión de collation no encontrado."
409 — "El job no se puede cancelar en estado '{status}'."
      → solo se cancela en 'pending' o 'running'

--- Transversales ---
401 — sin sesión admin (todos los endpoints)
429 — "Demasiadas solicitudes. Límite: {N}/minute"
5xx — { "detail": { "msg": "Error interno del servidor", "type": "InternalServerError" } }
      SIN request_id en el cuerpo → leerlo del header X-Request-ID
```

---

## 9. Checklist de implementación

**Modo y formulario**

- [ ] **Derivar el modo del `engine` del servidor**, nunca pedirlo al usuario. `mysql`/`mariadb`
      → `universal`; `postgresql` → `columns`.
- [ ] **Ocultar el campo de charset en PostgreSQL.** Enviarlo es un `422` garantizado.
- [ ] **No ofrecer "usar el valor por defecto del motor"** para la collation: es obligatoria en
      los dos modos.
- [ ] **Poblar el selector desde el catálogo correcto**: catálogo global de v7
      (`engine_family=mysql`, ojo con MariaDB) en `universal`; `available_collations` del
      inventario en `columns`. **No mezclarlos.**
- [ ] **No normalizar el texto de la collation en PostgreSQL**: `c` no es `C`.
- [ ] **Resolver el huevo/gallina de PostgreSQL** ([§4.3](#43-dos-catálogos-distintos-que-no-se-mezclan))
      y **pedirle al backend un endpoint de catálogo por servidor**.

**Inventario y selección**

- [ ] **Preseleccionar** las tablas con `needs_conversion: true` y los objetos con
      `is_outdated: true`.
- [ ] **No mostrar el selector de objetos en modo `columns`.** No es que esté vacío: no existe.
- [ ] **Checkbox por TABLA, nunca por columna**, aunque el detalle por columna se muestre.
- [ ] **Mostrar `current_collation: null` como "heredada de la base"** y contarla como
      pendiente. Nunca "sin collation".
- [ ] **`database_collation: null` en las VIEW es normal**: mostrar "—", no un error.
- [ ] **Advertir al desmarcar un objeto congelado**: es exactamente el bug que el módulo evita.
- [ ] **Ocultar el interruptor de "cambiar el default de la base" en `columns`**: se fuerza a
      `false`.

**Preview y confirmación**

- [ ] **Guardar del preview: `confirm_token`, `tables_to_convert` y `objects_to_recreate`.** Los
      dos últimos son los únicos totales que existen para la barra de progreso.
- [ ] **Invalidar el token ante cualquier cambio de selección** y deshabilitar "Ejecutar" hasta
      que haya un preview vigente.
- [ ] **Mostrar los `warnings` TAL CUAL**, sin resumir ni recategorizar. La semántica MySQL
      ("falla al aplicar") vs. PostgreSQL ("falla al consultar") es información, no ruido.
- [ ] **Mostrar `missing` / `missing_tables` como aviso**, no como error, y desmarcarlos.
- [ ] **No presentar el `sql` de un paso `recreate` como SQL copiable**: trae un marcador de
      posición.
- [ ] **Exigir el nombre exacto de la base** antes de habilitar el botón de ejecutar.
- [ ] **Advertir la irreversibilidad y la duración** en el diálogo de confirmación.

**Ejecución y progreso**

- [ ] **Tratar el `200` de `execute` como "encolado", no como "hecho".**
- [ ] **Hacer polling de `GET /{id}` y `GET /{id}/items`** con cadencia moderada (2–3 s al
      principio, 5–10 s después).
- [ ] **No interpretar un `progress` estancado como job colgado**: se persiste cada ~3 s y un
      paso lento no lo mueve.
- [ ] **Reconstruir la pantalla desde el servidor al volver**: el job sobrevive a la navegación.
- [ ] **Mostrar SIEMPRE el resultado por ítem**, también cuando `status` es `succeeded`.
- [ ] **`failed` = "N pasos fallaron de M"**, nunca "la conversión falló".
- [ ] **Detectar el caso del `ALTER DATABASE`** (un solo ítem, `object_type: "database"`, en
      error) y decir explícitamente que **la base no fue modificada**.
- [ ] **Destacar en rojo el ítem cuyo `error` dice que el DROP se aplicó y el CREATE no**, con el
      texto íntegro. Y **pedir al backend que exponga `captured_ddl`**
      ([§4.7](#47-recuperación-el-objeto-que-se-dropeó-y-no-se-recreó)).
- [ ] **Distinguir los dos `skipped`** por la presencia de `grants_error`: "ya estaba al día" vs.
      "la rutina sigue rota y hace falta un `GRANT`".
- [ ] **Mostrar `interrupted` con su propio copy**, distinto de `failed`.
- [ ] **Explicar que cancelar es cooperativo y no revierte nada** en el diálogo de cancelación, y
      **seguir haciendo polling** hasta ver `canceled` (el `200` del cancel no lo trae).

**Errores y transversales**

- [ ] **Mostrar `detail.msg` tal cual.** Está redactado en español y varios mensajes dicen cómo
      salir del problema.
- [ ] **Nunca leer `detail.context` ni `detail.loc`**: no existen fuera de desarrollo.
- [ ] **Aprovechar `public_context.allowed`** en el único error que lo trae (par de charset no
      habilitado, al crear el plan) para **repoblar el selector sin una segunda llamada**, y
      manejar `truncated: true` como *"hay más opciones"*.
- [ ] **Distinguir los dos `422` de charset de MySQL**: el que trae `allowed` (par inválido) del
      que no lo trae (falta `target_charset`). Son mensajes y causas distintas.
- [ ] **No usar un campo `success` del envelope: no existe.** El éxito es el status HTTP.
- [ ] **En `/items`, leer `pagination`, no `meta`.**
- [ ] **Leer el identificador de soporte del header `X-Request-ID`**, no del cuerpo del error.
- [ ] **Ofrecer `force` ante un `409`, tanto de fingerprint como de cuarentena.** En los dos
      endpoints adopta el estado actual de forma segura
      ([§4.10](#410-fingerprint-anti-toctou-y-expiración-del-plan)).
- [ ] **Manejar el `422` de `confirm_token` como consecuencia posible de un
      `execute(force=true)`**: si el drift cambió el plan resuelto, hay que rehacer el preview.
      No es un fallo inesperado, es la confirmación haciendo su trabajo.
- [ ] **Nunca ofrecer `force` ante un `410`**: un plan expirado se reemplaza, no se fuerza.
- [ ] **Tras un `preview` con `force`, volver a mostrar el plan completo** antes de ejecutar: el
      plan pudo cambiar.
- [ ] **Leer `status` antes que `error` en cada ítem.** En las filas `skipped`, `error` contiene
      el **motivo del salteo**, no un fallo — renderizarlo como error produce falsos negativos en
      cada conversión exitosa.
- [ ] **Mostrar el vencimiento del plan** (`expires_at`) y advertir antes del `410`.
- [ ] **Respetar el rate limit de `3/minute` en `execute`**: nada de reintentos automáticos.
- [ ] **Aislar el mapeo de estas respuestas en un solo lugar**: falta la verificación e2e contra
      motores reales y el contrato podría tener ajustes menores.
