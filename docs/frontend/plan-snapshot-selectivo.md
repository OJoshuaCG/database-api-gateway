# Plan de Frontend — Snapshot selectivo (blueprint desde una BD existente)

> Plan de implementación de frontend **tecnológicamente neutro**. No contiene código,
> frameworks, librerías ni decisiones de arquitectura frontend. Describe **qué** construir,
> **qué** validar, **qué** estados manejar y **cómo** navegar. El contrato de API es
> completo y se refleja tal cual lo entrega el backend (sobre `ApiResponse[T]` en éxito;
> `{ "detail": { "msg", "type", "context"? } }` en error).

---

## Resumen de la actualización

`POST /database-models/from-snapshot` deja de ser **todo o nada**. Antes fotografiaba toda
la estructura de una BD en una sola migración baseline (`0001`), solo estructura. Ahora es
**selectivo**: el admin elige **qué** objetos migrar, **cómo** versionarlos
(`single` / `by_class` / `manual`) y, opcionalmente, incluir **datos-semilla** de tablas de
catálogo. Es retrocompatible: los valores por defecto (`layout="single"`, sin datos)
reproducen exactamente el comportamiento histórico.

El endpoint de preview `GET /servers/{id}/databases/{db}/snapshot` se amplía con
`?include_data_stats=true`, que agrega por tabla una **estimación** de filas y si tiene
clave primaria — información necesaria para decidir qué catálogos sembrar.

Todas las rutas viven bajo `/api/v1`, requieren **sesión de administrador** (cookie de
sesión) y responden con la envoltura estándar `ApiResponse[T]`
(`{ "data": <T>, "message"?: string, "pagination"?: {...} }`; los campos `null` se omiten
del JSON). Los errores devuelven `{ "detail": { "msg": string, "type": string, "context"?: any } }`,
donde **`context` solo está presente en entorno de desarrollo** (ver la nota crítica en la
sección 3 y en Consideraciones).

---

## 0. Contexto general del plan

**De qué va este plan.** Construir un **asistente (wizard) de "Crear blueprint desde
snapshot"**: una guía paso a paso que toma una base de datos ya existente en un servidor
destino, muestra su estructura real, deja al admin escoger qué objetos capturar y cómo
repartirlos en versiones, incluir opcionalmente datos de catálogo, y crear un blueprint
(`DatabaseModel`) versionado listo para revisar y aplicar sobre otras BDs.

**Módulo(s) involucrado(s).**
- **Adopción / Reconciliación / Snapshot — Plan 09**: rutas
  `GET /api/v1/servers/{server_id}/databases/{database}/snapshot` y
  `POST /api/v1/database-models/from-snapshot` (adapters `dump_structure()` en
  `app/services/db_admin/`; lógica de layout en `app/services/db_admin/` — `build_versions`,
  `validate_manual_layout`; DTOs `StructureDump`/`DumpStatement` en `dtos.py`). Guía backend:
  `docs/features/adoption-reconcile-snapshot.md` y `docs/api-reference-v3.md`.
- **Migraciones de Blueprints — Plan 02** (dependencia aguas abajo, no se implementa aquí):
  las versiones creadas nacen `reviewed=false` y se aprueban/aplican con
  `PATCH /database-models/{id}/migrations/{version}` y los endpoints `apply`/`apply-all`
  (`managed_migration_controller.py`, `model_migration_controller.py`). `ModelMigrationOut`
  y `ModelMigrationSummary` ahora exponen `kind` ("schema" | "data").

**Qué cubre este plan.**
- Selección del origen (servidor + base de datos) y disparo del preview.
- Explorador de la estructura capturada (objetos por tipo, dependencias, portabilidad).
- Selección de objetos (filtros include/exclude por tipo y por objeto).
- Elección de estrategia de versionado (`single`, `by_class`, `manual`) con previsualización
  de las versiones que se crearán.
- Constructor visual de **layout manual** (buckets ordenados de esquema y de datos).
- Selección de **datos-semilla** de tablas de catálogo, guiada por `table_stats`.
- Identidad del blueprint (nombre, slug, descripción, nombre del baseline) y confirmación
  del rollback de datos.
- Pantalla de resultado (versiones creadas, tablas omitidas) y **puente al gate de revisión**
  (`reviewed`) obligatorio antes de aplicar.
- Estados de UI, validación en cliente y manejo de errores por código HTTP.

**Qué NO cubre este plan (fuera de alcance, deliberado).**
- El flujo completo de **revisar el SQL, aprobar (`reviewed`) y aplicar/revertir** versiones:
  es de Plan 02; aquí solo se enlaza como paso siguiente y se referencian sus endpoints.
- La pantalla de **reconciliación** (`GET /servers/{id}/reconcile`) y la **adopción**
  (`/managed-databases/adopt`, `/server-users/adopt`): se usan solo como posible punto de
  entrada.
- Gestión de servidores, usuarios de servidor y BDs gestionadas (solo se consumen sus
  identificadores y listados).
- Edición posterior del blueprint o de sus migraciones (`PATCH`/`DELETE` de versiones).

**Problemática.** Hoy, para llevar una BD legacy/única a un blueprint gestionable, el admin
solo podía capturarla entera en una sola versión, sin control sobre qué entra ni cómo se
versiona, y sin forma de traer datos de catálogo (tipos, estados, monedas…) que las tablas
de esquema necesitan para funcionar. Eso obliga a limpiar el blueprint a mano, mezcla
objetos portables con código atado al motor, y deja al admin sin visibilidad de qué se
capturó ni de qué se omitió y por qué.

**Solución propuesta.** Un asistente que hace **trivial el caso simple** ("dame el esquema y
ya" = un clic con valores por defecto) y **posible pero guiado el caso avanzado**: filtrar
objetos, separar por clase o armar versiones a mano, e incluir datos-semilla con salvaguardas
(solo tablas con PK, guardrails de tamaño, confirmación explícita del rollback por DELETE).
La UI traslada al usuario reglas del backend que no puede inferir solo: qué vuelve al
blueprint "no portable", por qué una tabla se omite, y que toda versión debe **revisarse y
aprobarse** antes de aplicar.

**Actores / usuario objetivo.** El **admin único** del gateway (proyecto single-admin, sin
roles ni multi-tenant). No hay diferenciación de permisos por usuario; toda la UI es para
ese perfil.

**Flujo principal de uso (happy path).** El admin abre "Crear blueprint desde snapshot",
elige un servidor y una BD; el gateway lee el motor en vivo y muestra su estructura. En el
caso simple pulsa "Crear con valores por defecto" y obtiene un blueprint con una sola versión
`0001`. En el caso avanzado ajusta qué objetos entran, cómo se versionan y qué catálogos se
siembran, revisa un resumen, y crea el blueprint. Termina en una pantalla que lista las
versiones creadas (marcando cuáles son de datos y cuáles no portables) y lo lleva a
revisarlas y aprobarlas antes de aplicarlas.

---

## 1. Resumen del módulo o feature

Asistente para convertir una BD existente en un **blueprint versionado**. Sirve al admin/DBA.
Dos endpoints: uno de **preview** (lee el motor en vivo, read-only) y uno de **creación**
(crea el blueprint y sus versiones, sin ejecutar DDL sobre la BD de origen). El flujo va de
"elegir origen → ver estructura → seleccionar y versionar → (opcional) datos → confirmar →
resultado", y desemboca en el gate de revisión de Plan 02.

---

## 2. Entidades involucradas

### Entidad: StructureDump (respuesta del preview — `data` del Endpoint 1)

```
Entidad: StructureDump
- database:         string,  solo lectura
- source_engine:    enum,    solo lectura, valores: [mysql, mariadb, postgresql]
- statements:       array<DumpStatement>, solo lectura
- has_non_portable: boolean, solo lectura  (true si hay rutinas/triggers/events)
- object_counts:    object,  solo lectura  (mapa { object_type: number }, derivado)
- table_stats:      array<TableStat> | null, solo lectura
                    (null salvo con include_data_stats=true)
```

```
Entidad: DumpStatement
- object_type: enum,   solo lectura, valores:
               [table, view, materialized_view, routine, trigger,
                sequence, type, extension, index, event]
- name:        string, solo lectura
- ddl:         string, solo lectura  (texto DDL; puede ser extenso — ver Consideraciones)
- depends_on:  array<string>, solo lectura  (nombres de tablas de las que depende:
               FK / trigger→tabla / índice→tabla)
```

```
Entidad: TableStat
- table:           string,  solo lectura
- estimated_rows:  number,  solo lectura  (ESTIMACIÓN del catálogo, NO exacta)
- has_primary_key: boolean, solo lectura  (si es false, la tabla NO puede sembrar datos)
```

### Entidad: FromSnapshotIn (cuerpo del Endpoint 2 — lo que el usuario compone)

```
Entidad: FromSnapshotIn
- server_id:             number,  requerido
- database:              string,  requerido
- name:                  string,  requerido   (nombre visible del blueprint)
- slug:                  string,  requerido    patrón: ^[a-z0-9]+(?:[-_][a-z0-9]+)*$
- description:           string,  opcional
- baseline_name:         string,  opcional, default: "Snapshot baseline"
- layout:                enum,    opcional, default: "single", valores: [single, by_class, manual]
- include_object_types:  array<string>, opcional  (si se da, SOLO esos tipos)
- exclude_object_types:  array<string>, opcional
- include_objects:       array<ObjectRef>, opcional
- exclude_objects:       array<ObjectRef>, opcional
- data_tables:           array<DataTableSel>, opcional, default: []
- on_oversize:           enum,    opcional, default: "skip", valores: [skip, error]
- confirm_data_rollback: boolean, opcional, default: false
- manual_layout:         array<ManualBucket>, condicional
                         (REQUERIDO si layout="manual"; PROHIBIDO en otro caso)
```

```
Entidad: ObjectRef            Entidad: DataTableSel
- object_type: enum, requerido - table: string, requerido
- name:        string, requerido - mode:  enum, requerido, valores: [upsert, insert_ignore]
```

```
Entidad: ManualBucket
- name:        string, opcional     (si se omite, el backend nombra la versión)
- objects:     array<ObjectRef>, condicional  } EXACTAMENTE UNO de los dos por bucket
- data_tables: array<string>,    condicional   } (esquema XOR datos; nunca ambos ni vacío)
```
Regla del bucket: cada `ManualBucket` es **de esquema** (`objects`) **o de datos**
(`data_tables`), nunca ambos ni vacío. El **orden de la lista** define el número de versión
(el usuario NO elige el número).

### Entidad: FromSnapshotOut (respuesta 201 — `data` del Endpoint 2)

```
Entidad: FromSnapshotOut
- model:                DatabaseModelOut, solo lectura
- baseline_version:     string,  solo lectura  (siempre "0001")
- source_engine:        enum,    solo lectura, valores: [mysql, mariadb, postgresql]
- has_non_portable:     boolean, solo lectura  (true si CUALQUIER versión trae objetos no portables)
- object_counts:        object,  solo lectura  (mapa { object_type: number })
- statements_captured:  number,  solo lectura
- total_versions:       number,  solo lectura
- data_tables_captured: number,  solo lectura
- skipped_tables:       array<SkippedTable>, solo lectura
- versions:             array<VersionSummary>, solo lectura
(IMPORTANTE: NUNCA incluye el SQL generado ni valores de filas)
```

```
Entidad: SkippedTable
- table:  string, solo lectura
- reason: enum,   solo lectura, valores:
          [no_primary_key, no_rows, oversize_rows, oversize_bytes,
           "unsupported_type:<tipo>", invalid_identifier]
```

```
Entidad: VersionSummary
- version:          string,  solo lectura  ("0001", "0002", …)
- kind:             enum,    solo lectura, valores: [schema, data]
- name:             string,  solo lectura
- object_counts:    object,  solo lectura
- has_non_portable: boolean, solo lectura
```

### Entidad: DatabaseModelOut (blueprint creado, anidado en la respuesta)

```
Entidad: DatabaseModelOut
- id:              number,  solo lectura
- name:            string,  solo lectura
- slug:            string,  solo lectura
- description:     string | null, solo lectura
- current_version: string,  solo lectura
- is_active:       boolean, solo lectura
- created_at:      date,    solo lectura
- updated_at:      date,    solo lectura
```

### Entidad relacionada (contexto, Plan 02): ModelMigration (con `kind`)
Las migraciones del blueprint ahora incluyen `kind` ("schema" | "data"). Una migración
`kind="data"` está **atada a `source_engine`** (la sintaxis upsert difiere por motor) y **no
se traduce cross-engine**. Toda versión creada por snapshot nace `reviewed=false`. La UI de
esta feature no edita migraciones; solo enlaza a la vista de revisión de Plan 02.

---

## 3. Contrato de API

> **Nota crítica y transversal sobre errores.** El backend responde errores como
> `{ "detail": { "msg": string, "type": string } }`. El campo **`detail.context`
> (incluida la lista `violations` del layout manual y el desglose de campos de validación)
> SOLO se incluye cuando el backend corre en `APP_ENV=development`**. En producción el
> frontend recibe únicamente `detail.msg` + `detail.type`. Esto condiciona el diseño del
> manejo de errores (ver `[SUPUESTO A]` y la sección 6). El **Request ID** para soporte NO
> viaja en el cuerpo: viene en el header de respuesta **`X-Request-ID`** (presente en toda
> respuesta, éxito o error) — la UI debe capturarlo y mostrarlo en los estados de error.

---

### Endpoint 1 (MODIFICADO) — Preview del snapshot

**Las 5 preguntas:**
1. **¿Qué hace?** Lee en vivo la estructura de una BD del motor destino y devuelve el DDL
   autoritativo de sus objetos (solo estructura, nunca filas), con conteos por tipo y, opt-in,
   estadísticas por tabla.
2. **¿Qué soluciona?** Da al admin **visibilidad completa** de qué contiene la BD antes de
   decidir qué capturar; sin esto, seleccionar objetos o datos sería a ciegas.
3. **¿Cuándo y cómo utilizarlo?** Al elegir servidor + base de datos en el asistente. Primero
   sin `include_data_stats` (más rápido); se re-llama con `include_data_stats=true` solo cuando
   el usuario decide explorar datos-semilla (hace una consulta extra de catálogo por tabla).
4. **¿En qué parte del flujo?** Es la **carga inicial** del paso "Preview / explorador de
   objetos" (Vista 2). Su salida alimenta los pasos de selección, versionado y datos.
5. **¿Relación con otros módulos/endpoints?** Sí: su resultado (los objetos y `table_stats`)
   es el insumo de `POST /database-models/from-snapshot` (Endpoint 2). El servidor y la BD
   suelen provenir del listado de servidores/BDs o de `GET /servers/{id}/reconcile` (Plan 09).

```
[GET] /api/v1/servers/{server_id}/databases/{database}/snapshot
Descripción: preview estructural en vivo de la BD (read-only, no ejecuta DDL).
Autenticación: requerida — sesión de administrador (cookie de sesión).

Parámetros de path:
  - server_id: number, requerido
  - database:  string, requerido  (nombre de la BD en el motor)

Parámetros de query:
  - include_data_stats: boolean, opcional, default: false
        false → table_stats = null (rápido)
        true  → agrega table_stats (consulta extra de catálogo por tabla; más lento)

Respuesta exitosa (sobre ApiResponse):
  HTTP 200
  {
    "data": {
      "database": "ventas",
      "source_engine": "postgresql",
      "statements": [
        { "object_type": "table", "name": "clientes", "ddl": "CREATE TABLE ...", "depends_on": [] },
        { "object_type": "view",  "name": "v_activos", "ddl": "CREATE VIEW ...",  "depends_on": ["clientes"] }
      ],
      "has_non_portable": false,
      "object_counts": { "table": 12, "view": 3, "routine": 0 },
      "table_stats": null   // o lista de { table, estimated_rows, has_primary_key } si include_data_stats=true
    }
  }
  Nota: la respuesta NO es paginada (no trae 'pagination'); devuelve la lista completa.

Respuestas de error (forma real del backend):
  HTTP 401 → no autenticado: { "detail": { "msg": "...", "type": "..." } }
  HTTP 404 → servidor o BD inexistente: { "detail": { "msg": "...", "type": "..." } }
  HTTP 422 → identificador inválido / no procesable
  HTTP 500 → error de conexión o del motor: { "detail": { "msg": "Error interno del servidor", "type": "InternalServerError" } }
  (Header X-Request-ID presente en todos los casos.)
```

---

### Endpoint 2 (MODIFICADO) — Crear blueprint desde snapshot

**Las 5 preguntas:**
1. **¿Qué hace?** Crea un blueprint (`DatabaseModel`) y sus migraciones versionadas a partir
   del snapshot de una BD, según la selección de objetos, la estrategia de versionado y los
   datos-semilla elegidos. **No ejecuta DDL sobre la BD de origen**; solo lee y persiste
   metadata + SQL de las versiones.
2. **¿Qué soluciona?** Convierte una BD real en una plantilla replicable y versionada, con
   control fino de qué entra y cómo se organiza — reemplaza la captura "todo o nada".
3. **¿Cuándo y cómo utilizarlo?** Al confirmar el asistente. El cuerpo se compone con lo
   elegido en los pasos previos; el caso simple envía solo `server_id`, `database`, `name`,
   `slug` (todo lo demás por defecto). Rate limit: **10/min**.
4. **¿En qué parte del flujo?** Es el **submit** del asistente (desde la Vista 6 "Resumen y
   confirmación"). Su respuesta abre la Vista 7 "Resultado".
5. **¿Relación con otros módulos/endpoints?** Fuerte con **Plan 02**: las versiones creadas
   (`versions[]`) son `ModelMigration` que nacen `reviewed=false` y deben aprobarse con
   `PATCH /database-models/{id}/migrations/{version}` `{"reviewed": true}` y luego aplicarse
   (`apply`/`apply-all`). Consume el `server_id`/`database` del Endpoint 1. El `model.id`
   resultante es la entrada de toda la administración posterior del blueprint.

```
[POST] /api/v1/database-models/from-snapshot
Descripción: crea el blueprint y sus versiones desde el snapshot.
Autenticación: requerida — sesión de administrador (cookie de sesión).
Rate limit: 10/min.
Headers requeridos: Content-Type: application/json (+ cookie de sesión).

Body (FromSnapshotIn) — ver entidad en sección 2. Ejemplos:

  // Caso simple (histórico): esquema completo, una versión, sin datos
  { "server_id": 3, "database": "ventas", "name": "Ventas", "slug": "ventas" }

  // Con datos de catálogo, versionado por clase
  {
    "server_id": 3, "database": "ventas", "name": "Ventas", "slug": "ventas",
    "layout": "by_class",
    "data_tables": [{ "table": "monedas", "mode": "upsert" }],
    "confirm_data_rollback": true
  }

  // Manual (buckets ordenados)
  {
    "server_id": 3, "database": "ventas", "name": "Ventas", "slug": "ventas",
    "layout": "manual",
    "manual_layout": [
      { "name": "Tablas base", "objects": [{ "object_type": "table", "name": "clientes" }] },
      { "name": "Vistas",      "objects": [{ "object_type": "view",  "name": "v_activos" }] },
      { "name": "Catálogos",   "data_tables": ["monedas"] }
    ],
    "confirm_data_rollback": true
  }

Respuesta exitosa (sobre ApiResponse):
  HTTP 201
  {
    "data": {
      "model": { "id": 42, "name": "Ventas", "slug": "ventas", "description": null,
                 "current_version": "0002", "is_active": true,
                 "created_at": "...", "updated_at": "..." },
      "baseline_version": "0001",
      "source_engine": "postgresql",
      "has_non_portable": false,
      "object_counts": { "table": 12, "view": 3 },
      "statements_captured": 15,
      "total_versions": 2,
      "data_tables_captured": 1,
      "skipped_tables": [ { "table": "logs", "reason": "no_primary_key" } ],
      "versions": [
        { "version": "0001", "kind": "schema", "name": "Snapshot baseline",
          "object_counts": { "table": 12, "view": 3 }, "has_non_portable": false },
        { "version": "0002", "kind": "data", "name": "Datos: monedas",
          "object_counts": {}, "has_non_portable": false }
      ]
    },
    "message": "Blueprint creado desde snapshot"
  }

Respuestas de error (forma real del backend):
  HTTP 401 → no autenticado
  HTTP 409 → nombre o slug de blueprint ya existente
  HTTP 422 → error de validación / negocio. detail.msg describe la causa; detail.context
             (SOLO en desarrollo) puede traer context.violations (layout manual) o el
             desglose de campos (validación Pydantic). Causas de 422:
               · BD vacía (sin objetos)
               · los filtros excluyeron todo
               · se pidieron datos de una tabla cuya estructura no está incluida
               · demasiadas tablas de datos (> límite, default 25)
               · una tabla superó el guardrail con on_oversize="error"
               · una versión supera el tope de SQL
               · layout manual inválido → detail.context.violations = [ { object, object_type,
                 version (1-based), reason, ...campos extra } ]
  HTTP 429 → rate limit (10/min) excedido: { "detail": { "msg": "Demasiadas solicitudes...", "type": "RateLimitExceeded" } }
  HTTP 500 → error interno
  (Header X-Request-ID presente en todos los casos.)
```

**Catálogo de `reason` en `skipped_tables` (mapear a texto accionable):**

| reason | Significado para el usuario |
|---|---|
| `no_primary_key` | La tabla no tiene PK; los datos-semilla requieren PK (upsert + rollback). Se omitió. |
| `no_rows` | La tabla no tenía filas que sembrar. Se omitió (informativo). |
| `oversize_rows` | Superó el máximo de filas permitido para datos-semilla. |
| `oversize_bytes` | Superó el máximo de bytes permitido para datos-semilla. |
| `unsupported_type:<tipo>` | Contiene un valor de tipo no soportado (ej. UUID/INET/INTERVAL). Se omitió. |
| `invalid_identifier` | El nombre no pasó la validación anti-inyección. Se omitió. |

**Catálogo de `reason` en `context.violations` (layout manual — DEV-only; mapear a objeto/versión):**

| reason | Campos extra | Mensaje accionable sugerido |
|---|---|---|
| `mixed_schema_and_data` | — | El bucket mezcla esquema y datos. Sepáralos en buckets distintos. |
| `empty_bucket` | — | La versión está vacía. Añade objetos o elimínala. |
| `duplicate_assignment` | `also_in_version` | El objeto está en dos versiones (también en la vX). Déjalo en una sola. |
| `unassigned_object` | — | Objeto seleccionado sin asignar a ninguna versión. Asígnalo. |
| `unknown_object` | — | El objeto no existe en el snapshot. Quítalo. |
| `unassigned_data_table` | — | Tabla de datos sin asignar a un bucket de datos. Asígnala. |
| `unknown_data_table` | — | La tabla de datos no existe en el snapshot. Quítala. |
| `dependency_in_later_version` | `depends_on`, `dependency_version` | Depende de un objeto que está en una versión posterior (vX). Muévelo después. |
| `prerequisite_after_a_table` | `must_be_at_most` | Un prerrequisito quedó después de una tabla. Muévelo a la versión ≤ X. |
| `must_be_after_all_tables` | `must_be_at_least` | Debe ir después de todas las tablas (versión ≥ X). |
| `schema_after_data` | `first_data_version` | Hay esquema después de datos. Los datos van al final (después de la vX). |
| `data_table_structure_not_included` | — | La estructura de esta tabla de datos no está incluida en la selección. Inclúyela. |
| `data_before_table_structure` | `table_structure_version` | Los datos van antes que la estructura de su tabla (vX). Muévelos después. |

---

## 4. Vistas propuestas

El feature se implementa como un **asistente por pasos** con un camino express. Se describen
7 vistas/pasos. La numeración de pasos es lógica; la UI puede permitir saltar directo al
submit desde el paso 1 (camino "1 clic").

### Vista 1 — Origen del snapshot (selección de servidor y BD)

Propósito: elegir de qué servidor y BD se toma el snapshot y disparar el preview.

```
[Barra superior]
  Título: "Crear blueprint desde snapshot"  | [Botón: Cancelar]
  Indicador de pasos: (1) Origen · (2) Preview · (3) Objetos · (4) Versionado · (5) Datos · (6) Resumen

[Formulario]
  Selector con búsqueda: Servidor destino  (obligatorio)
  Selector con búsqueda: Base de datos     (obligatorio; se puebla al elegir servidor)
  [Aviso] "El gateway leerá el motor en vivo; puede tardar en BDs grandes."

[Pie]
  [Botón: Ver estructura →]  (deshabilitado hasta elegir servidor + BD)
```

Componentes: dos selectores con búsqueda (relación); aviso informativo. `[SUPUESTO B]` la
lista de servidores y de BDs proviene de endpoints existentes (`GET /servers`,
`GET /servers/{id}/databases` o `reconcile`); no forman parte de este contrato.

Estados de UI:
- `cargando`: al poblar el selector de BDs tras elegir servidor (spinner en el selector).
- `vacío`: servidor sin BDs → mensaje "Este servidor no tiene bases de datos accesibles".
- `error`: fallo al listar BDs → banner con `detail.msg` + `X-Request-ID` + [Reintentar].
- `éxito`: al pulsar "Ver estructura" navega a Vista 2 disparando el Endpoint 1.

---

### Vista 2 — Preview / explorador de objetos

Propósito: mostrar la estructura capturada y permitir el camino express o continuar al avanzado.

```
[Encabezado del preview]
  BD: {database} · Motor: {source_engine}
  [Badge: "No portable"] visible si has_non_portable=true
    tooltip: "Incluye rutinas/triggers/events → el blueprint quedará atado a {source_engine}."
  Resumen de conteos (object_counts): Tablas 12 · Vistas 3 · Rutinas 0 · ...
  Toggle: "Incluir estadísticas de datos"  → re-llama Endpoint 1 con include_data_stats=true

[Cuerpo — lista agrupada por object_type]
  Grupo "Tablas (12)"
    Fila: nombre | dependencias (depends_on) | [Ver DDL]
    ...
  Grupo "Vistas (3)" ...
  Grupo "Rutinas / Triggers / Events" (marcados como NO portables) ...
  (Panel lateral / modal "Ver DDL": muestra statements[].ddl en solo lectura, con scroll.)

[Acciones]
  [Botón primario: Crear con valores por defecto]  (camino 1 clic → salta a Vista 6/submit
        con layout=single, sin datos, sin filtros)
  [Botón secundario: Personalizar →]  (continúa a Vista 3)
```

Componentes: cabecera con badges y conteos; toggle de estadísticas; lista agrupada/colapsable
con búsqueda por nombre (filtrado en cliente); visor de DDL en solo lectura; dos CTAs
(express vs avanzado).

Estados de UI:
- `cargando`: "Leyendo estructura del motor en vivo…" (puede tardar; spinner + texto). El
  toggle de estadísticas muestra su propio estado de carga al re-llamar.
- `vacío`: `statements` vacío → estado "Esta base de datos no tiene objetos que capturar"
  (no permite continuar; solo [Cambiar origen]). Corresponde también al 422 "BD vacía" del
  Endpoint 2 anticipado en cliente.
- `error`: 404 (BD/servidor no existe) o 500 (fallo de conexión) → banner con `detail.msg`,
  `X-Request-ID` y [Reintentar] / [Cambiar origen].
- `éxito`: datos renderizados; ambos CTAs habilitados.

---

### Vista 3 — Selección de objetos (filtros include/exclude)

Propósito: acotar qué objetos entran al blueprint por tipo y por objeto concreto.

```
[Modo de selección]
  ( ) Incluir todo (default)
  ( ) Incluir solo tipos seleccionados     → alimenta include_object_types
  ( ) Excluir tipos seleccionados          → alimenta exclude_object_types
  Chips por tipo presentes en el snapshot: [table] [view] [routine] [trigger] ...
  [Atajo: "Excluir rutinas y triggers → baseline portable"]
     al activarlo: exclude_object_types=["routine","trigger","event"]; muestra que
     has_non_portable pasaría a false.

[Ajuste fino por objeto (opcional, colapsable)]
  Lista de objetos con checkbox → alimenta include_objects / exclude_objects ({object_type,name})

[Vista previa de la selección]
  "Quedan seleccionados: 12 tablas, 3 vistas (0 rutinas)."
  [Badge dinámico: "Selección portable"] o [Badge: "No portable"]

[Pie] [← Atrás] [Continuar →]
```

Componentes: grupo de opciones mutuamente excluyentes para el modo de tipos; chips por tipo;
lista de objetos con selección múltiple; atajo "baseline portable"; indicador dinámico de
portabilidad y de conteo resultante (calculado en cliente sobre `statements`).

Estados de UI:
- `vacío` (derivado): si la selección deja 0 objetos → aviso inline "La selección excluye
  todo; ajusta los filtros" y bloqueo de [Continuar]. (Anticipa el 422 "los filtros
  excluyeron todo".)
- `éxito`: selección válida (≥1 objeto) → [Continuar] habilitado.

---

### Vista 4 — Estrategia de versionado (layout)

Propósito: elegir cómo se reparten los objetos en versiones y previsualizar el resultado.

```
[Opciones de layout — tarjetas]
  ( ) single   "Todo en una versión (0001)"  — el más simple; comportamiento histórico.
  ( ) by_class "Una versión por clase de objeto"
        orden: tablas(+índices) → vistas → vistas materializadas → rutinas → triggers → events
               (los datos, si los hay, van al final)
  ( ) manual   "Armar versiones a mano"  → abre Vista 5

[Previsualización de versiones (para single y by_class; calculada en cliente)]
  v0001 [schema] "Snapshot baseline" — 12 tablas, 3 vistas
  v0002 [schema] "Rutinas"           — 0            (se ocultan las vacías)
  ...
  [Nota] "Los datos-semilla que elijas se añadirán como versiones kind=data al final."

[Pie] [← Atrás] [Continuar →]
```

Componentes: selector tipo tarjetas para `layout`; previsualización de versiones (para
`manual` la previsualización real vive en la Vista 5). La previsualización de `by_class` es
**orientativa en cliente**; la numeración/estructura final la decide el backend.

Estados de UI:
- `éxito`: layout elegido; si `manual`, [Continuar] lleva a Vista 5; si no, a Vista 5b (datos)
  o directamente a Vista 6 según si el usuario quiere datos.

`[SUPUESTO C]` La previsualización de versiones para `by_class`/`single` se calcula en cliente
a partir de `object_counts`/`statements`; puede diferir levemente de la numeración final del
backend (que oculta clases vacías). Se muestra como "estimada".

---

### Vista 5 — Constructor de layout manual (solo si layout="manual")

Propósito: componer buckets ordenados (esquema XOR datos) y validar en cliente lo básico.

```
[Panel izquierdo: objetos disponibles del snapshot]
  Agrupados por tipo, con búsqueda. Cada objeto muestra sus depends_on.
  (Marcados los ya asignados; los no asignados resaltados como pendientes.)

[Panel derecho: versiones (buckets ordenados)]
  [+ Añadir versión de esquema]  [+ Añadir versión de datos]
  v1 (esquema) "Tablas base"   [arrastrar objetos aquí] [renombrar] [subir/bajar] [eliminar]
  v2 (esquema) "Vistas"        ...
  v3 (datos)   "Catálogos"     [asignar tablas de datos] ...
  (El número de versión = posición en la lista; el usuario reordena, no numera.)

[Validaciones en cliente (bloqueantes, antes de permitir avanzar):]
  - Un bucket no puede mezclar esquema y datos.               (mixed_schema_and_data)
  - Ningún bucket vacío.                                      (empty_bucket)
  - Todos los objetos seleccionados deben estar asignados.    (unassigned_object)
  - Los buckets de datos van DESPUÉS de todos los de esquema. (schema_after_data)
  - La tabla de un bucket de datos debe tener su estructura incluida en algún bucket de esquema
    anterior.                                                 (data_table_structure_not_included / data_before_table_structure)
  [Panel de problemas]: lista los incumplimientos detectados en cliente, cada uno enlazado
  al objeto/versión concreto.

[Pie] [← Atrás] [Continuar →]  (habilitado solo si la validación en cliente pasa)
```

Componentes: dos paneles (origen / destino) con asignación (arrastrar-soltar o mover);
reordenamiento de buckets; renombrado; panel de problemas de validación **en cliente**;
resaltado de objetos sin asignar.

Estados de UI:
- `error de validación (cliente)`: panel de problemas visible, [Continuar] bloqueado.
- `error de validación (servidor, al hacer submit en Vista 6)`: ver sección 6 — se re-mapean
  las `violations` (DEV) o se muestra `detail.msg` (producción) y se resaltan los buckets/objetos
  implicados devolviendo al usuario a esta vista.
- `éxito`: layout válido en cliente → [Continuar].

> **Importante:** la validación en cliente es **de conveniencia** (cubre lo evidente). La
> validación topológica completa (FK entre tablas, dependencias vista→vista/rutina) la hace el
> backend y **SIEMPRE** debe manejarse el 422 con `violations` — ver sección 6 y `[SUPUESTO A]`.

---

### Vista 5b — Datos-semilla (catálogos) — opcional

Propósito: elegir tablas de catálogo cuyos datos se incluirán como versiones `kind="data"`.

```
[Encabezado]
  "Datos-semilla — SOLO catálogos pequeños. No es para datos masivos."
  [Aviso] "Requieren clave primaria. Rigen guardrails de tamaño (filas/bytes/nº de tablas)."
  (Requiere estadísticas: si table_stats es null, botón [Cargar estadísticas] que re-llama
   Endpoint 1 con include_data_stats=true.)

[Tabla de candidatas (de table_stats)]
  Columnas: [✓] | Tabla | Filas estimadas | PK | Modo (upsert / insert_ignore)
    - Fila con has_primary_key=false → checkbox DESHABILITADO + tooltip
      "Sin PK: no puede sembrar datos".
    - Fila con estimated_rows alto → badge de advertencia "Muchas filas (estimado): puede
      superar el guardrail y omitirse".
    - Solo se listan/seleccionan tablas cuya estructura está incluida en la selección de la
      Vista 3 / los buckets de la Vista 5.
  Contador: "Seleccionadas: 3 / máx 25"  (aviso al acercarse/superar el límite)

[Comportamiento ante exceso de tamaño]
  ( ) Omitir tablas que excedan el guardrail y reportarlas (on_oversize="skip", default)
  ( ) Fallar la creación si alguna excede (on_oversize="error")

[Confirmación de rollback de datos]
  [ ] Confirmo el rollback por PK (DELETE) de las versiones de datos  → confirm_data_rollback
      Sin marcar: el rollback queda solo como sugerencia (no ejecutable).
      [Advertencia] "Al revertir una versión de datos se ejecutará DELETE por PK sobre las
       filas sembradas."

[Pie] [← Atrás] [Continuar →]
```

Componentes: tabla de selección múltiple guiada por `table_stats`; selector de `mode` por
tabla; opciones de `on_oversize`; checkbox de `confirm_data_rollback` con advertencia;
contador contra el límite de tablas.

Estados de UI:
- `cargando`: al pedir `include_data_stats=true` (spinner en la tabla).
- `vacío`: sin catálogos elegibles (todas sin PK o ninguna estructura incluida) → aviso
  "No hay tablas elegibles para datos-semilla" y opción de continuar sin datos.
- `éxito`: selección válida → [Continuar]. (Si el usuario no quiere datos, `data_tables=[]`.)

---

### Vista 6 — Resumen y confirmación (submit)

Propósito: identidad del blueprint y revisión final antes de crear.

```
[Identidad del blueprint]
  Campo: Nombre (name)                       — requerido
  Campo: Slug (slug)                          — requerido, patrón ^[a-z0-9]+(?:[-_][a-z0-9]+)*$
        (validar en cliente; sugerir slug a partir del nombre; avisar si tiene mayúsculas/espacios)
  Campo: Descripción (description)            — opcional
  Campo: Nombre del baseline (baseline_name)  — default "Snapshot baseline"

[Resumen de lo que se creará (recap de pasos 3–5b)]
  Origen: servidor {id} · BD {database} · motor {source_engine}
  Layout: {single | by_class | manual}
  Objetos: 12 tablas, 3 vistas (0 rutinas)  [Badge: portable / no portable]
  Datos-semilla: monedas (upsert), estados (insert_ignore)  · on_oversize: skip
  Rollback de datos: confirmado / solo sugerencia
  [Aviso si no portable o con datos] "Este blueprint quedará atado a {source_engine} y no podrá
   aplicarse a otros motores."

[Pie] [← Atrás] [Crear blueprint]  (dispara POST from-snapshot)
```

Componentes: formulario de identidad con validación de `slug` en cliente; panel de recap de
solo lectura; aviso de "atado al motor"; botón de submit con estado de envío.

Estados de UI:
- `cargando/enviando`: botón en estado "Creando…", controles deshabilitados (evita doble
  submit; respeta rate limit 10/min).
- `error`:
  - 409 → resaltar Nombre/Slug: "Ya existe un blueprint con ese nombre o slug".
  - 422 → banner con `detail.msg`; si es layout manual, además re-mapear `violations` (DEV) a
    los buckets y devolver a la Vista 5; si es "datos de tabla sin estructura", "demasiadas
    tablas de datos", "guardrail con error" o "versión supera el tope de SQL", enlazar al paso
    correspondiente (5b / 3).
  - 429 → "Demasiadas solicitudes, intenta en un momento" (mostrar espera).
  - 500 → banner genérico + `X-Request-ID`.
- `éxito`: 201 → navega a Vista 7 con la respuesta.

---

### Vista 7 — Resultado de creación

Propósito: confirmar qué se creó, qué se omitió, y llevar al gate de revisión.

```
[Encabezado de éxito]
  "Blueprint '{model.name}' creado (v{model.current_version})"
  Resumen: total_versions · statements_captured · data_tables_captured
  [Badge: "No portable — atado a {source_engine}"] si has_non_portable=true

[Lista de versiones creadas (versions[])]
  v0001 [schema] "Snapshot baseline"  — 12 tablas, 3 vistas   [PENDIENTE DE REVISIÓN]
  v0002 [data]   "Datos: monedas"     — (kind=data, resaltado distinto)  [PENDIENTE DE REVISIÓN]
  (Diferenciar visualmente kind=data de kind=schema; badge "no portable" por versión.)
  [Aviso destacado] "Todas las versiones nacen SIN aprobar. Debes revisar el SQL y aprobar
   cada una antes de poder aplicarlas."

[Tablas omitidas (skipped_tables[]) — si las hay]
  Tabla | Motivo (texto accionable según reason)
  logs  | Sin clave primaria — los datos-semilla requieren PK.

[Acciones]
  [Botón primario: Revisar y aprobar versiones →]  (lleva a la UI de Plan 02:
        detalle del blueprint model.id / migraciones — fuera de alcance de este plan)
  [Botón secundario: Crear otro] / [Ir al blueprint]
```

Componentes: panel de éxito; lista de versiones con distinción visual `data`/`schema` y estado
de revisión; tabla de omitidas; CTA al gate de revisión. **No** se muestra SQL aquí (la
respuesta nunca lo trae — ver Consideraciones).

Estados de UI:
- `éxito`: siempre (esta vista solo se abre tras 201).
- `vacío` (parcial): sin `skipped_tables` → se oculta esa sección.

---

## 5. Flujo de navegación

```
Vista 1: Origen (servidor + BD)
  → [Ver estructura] → GET .../snapshot → Vista 2: Preview
      → [Crear con valores por defecto]  → (submit express: layout=single, sin datos)
             → POST from-snapshot → Vista 7: Resultado
      → [Personalizar]                   → Vista 3: Selección de objetos
          → [Continuar] → Vista 4: Versionado
              → (layout=single | by_class)
                    → [¿Incluir datos?] Sí → Vista 5b: Datos-semilla → Vista 6: Resumen
                                        No → Vista 6: Resumen
              → (layout=manual)
                    → Vista 5: Constructor manual (buckets esquema)
                          → (los datos se definen como buckets de datos aquí, o en Vista 5b)
                          → [Continuar] → Vista 6: Resumen
      → [Cambiar origen]                 → Vista 1

Vista 6: Resumen
  → [Crear blueprint] → POST from-snapshot
      → [201]  → Vista 7: Resultado (con notificación de éxito)
      → [409]  → permanece en Vista 6, resalta Nombre/Slug
      → [422 layout manual] → vuelve a Vista 5, mapea violations a buckets/objetos
      → [422 datos/filtros/tamaño] → vuelve al paso implicado (5b / 3)
      → [429/500] → permanece en Vista 6, banner + X-Request-ID
  → [← Atrás] → paso anterior (sin perder la selección)

Vista 7: Resultado
  → [Revisar y aprobar versiones] → UI de Plan 02 (detalle blueprint / migraciones)
        → (por cada versión) revisar SQL → PATCH .../migrations/{version} {"reviewed": true}
        → apply / apply-all  (bloqueado con 409 mientras falten aprobaciones)
  → [Crear otro] → Vista 1   |   [Ir al blueprint] → detalle del blueprint
```

Puntos de entrada al asistente (`[SUPUESTO D]`): desde el listado de blueprints
("Crear desde snapshot") y/o desde la pantalla de reconciliación de un servidor (Plan 09),
que ya conoce servidor + BD y puede prellenar la Vista 1.

---

## 6. Consideraciones adicionales

### Manejo de errores (transversal, prioritario)

- **`detail.context` es DEV-only.** La UI **no puede depender** de `context.violations` ni del
  desglose de campos en producción. Estrategia obligatoria:
  1. Validación en cliente robusta antes de cada submit (especialmente el layout manual:
     mezcla esquema/datos, buckets vacíos, orden datos-al-final, objetos sin asignar,
     estructura de tabla de datos incluida). Esto reduce los 422 evitables.
  2. Siempre mostrar `detail.msg` (siempre presente) como mensaje principal.
  3. Cuando `detail.context.violations` **esté** presente (dev/staging), enriquecer: mapear
     cada violación a su `object`/`version` (1-based) y renderizarla junto al bucket/objeto
     concreto, con el texto accionable de la tabla de la sección 3.
  4. Capturar y mostrar el header **`X-Request-ID`** en todo estado de error (para soporte).
  `[SUPUESTO A]` Se asume que backend expone `violations` solo en desarrollo; si se necesita
  en producción, es un cambio de backend fuera de este plan.
- Distinguir 4xx (accionable por el usuario: corregir nombre/slug, selección, layout) de 5xx
  (transitorio: [Reintentar] + Request ID).

### Paginación
- **Ninguno de los dos endpoints es paginado.** El preview devuelve la lista **completa** de
  `statements` (y `table_stats`); no hay `page`/`size` aquí. En BDs con muchos objetos la lista
  puede ser larga: agrupar por tipo, colapsar grupos, virtualizar/paginar **en cliente** y
  ofrecer búsqueda por nombre. (La paginación offset `page`/`size` del proyecto aplica a otros
  listados, no a este contrato.)

### Permisos
- Proyecto **single-admin**: no hay roles ni multi-tenant. No se ocultan botones por rol; toda
  la feature es para el admin. Solo se exige sesión válida (401 → redirigir a login).

### Acciones en lote
- La feature es intrínsecamente de selección múltiple: objetos (include/exclude), tablas de
  datos, y asignación a buckets. No hay operaciones masivas sobre recursos existentes (crea uno
  nuevo). El único límite de lote es el **máximo de tablas de datos** (default 25) → mostrar
  contador y bloquear/avisar al superarlo.

### Operaciones destructivas / irreversibles / estado "en curso"
- **`from-snapshot` NO ejecuta DDL sobre la BD de origen ni sobre ningún motor**: solo lee y
  crea metadata + SQL de versiones. No es destructivo en sí. **No** requiere confirmación de
  irreversibilidad para el motor.
- **`confirm_data_rollback` sí es una confirmación de primera clase**: al marcarlo, el admin
  acepta que el rollback de una versión de datos ejecutará **DELETE por PK** sobre las filas
  sembradas (cuando esa versión se revierta, en Plan 02). Debe presentarse con advertencia
  explícita, desmarcado por defecto.
- **Estado "en curso"** en dos puntos: (a) el **preview** lee el motor en vivo y puede tardar
  → spinner con texto y deshabilitar navegación; (b) el **submit** de creación → botón en
  "Creando…", controles deshabilitados, evitar doble envío (coherente con rate limit 10/min).
- **La verdadera operación destructiva (apply/rollback sobre motores reales) es de Plan 02** y
  está fuera de alcance; aquí solo se refuerza el **gate `reviewed`**: la Vista 7 debe dejar
  clarísimo que ninguna versión puede aplicarse hasta revisarla y aprobarla, y encaminar a esa
  UI.

### Portabilidad / "atado al motor" (comunicación obligatoria)
- Mostrar el badge **"No portable"** siempre que `has_non_portable=true` (preview y resultado),
  con explicación: rutinas/triggers/events y toda versión `kind="data"` atan el blueprint a
  `source_engine`; aplicarlo a otro motor dará **422 (cross-engine guard)** en el `apply`.
- Resaltar el atajo "Excluir rutinas/triggers → baseline portable" y mostrar en vivo cómo la
  selección cambia el estado de portabilidad.

### Exportación / visualización del SQL generado
- **La respuesta de `from-snapshot` NUNCA incluye el SQL generado ni valores de filas.** Por
  tanto, en las vistas de esta feature **no se puede** mostrar/exportar el SQL de las versiones.
  El único SQL visible es el `statements[].ddl` del **preview** (para inspección, solo lectura).
  Para revisar el SQL real de cada versión (necesario para el gate `reviewed`), la UI debe
  enlazar al endpoint de detalle de migración de **Plan 02**. `[SUPUESTO E]` existe una vista/
  endpoint de Plan 02 que devuelve el SQL de una versión para su revisión.

### Diferenciación visual `kind="data"` vs `kind="schema"`
- En la previsualización de versiones (Vista 4), en el constructor manual (Vista 5) y en el
  resultado (Vista 7): las versiones de **datos** deben verse distintas (icono/color/etiqueta),
  con el recordatorio de que son **solo catálogos**, están **atadas al motor** y **no se
  traducen** cross-engine.

### Gráficas (opcional, recomendado)
- **Composición de objetos del snapshot** — representa `object_counts` (cuántos objetos hay por
  tipo). Tipo recomendado: **barras horizontales**. Justificación: se comparan magnitudes entre
  categorías nominales (tipos de objeto) cuyo número y longitud de etiqueta varían; las barras
  horizontales leen etiquetas largas sin rotar y ordenan por magnitud mejor que un pastel (que
  se vuelve ilegible con >4-5 categorías y no sirve para comparar valores similares).
  Endpoint: `object_counts` del Endpoint 1 (preview). Actualización: **estática** (una sola
  lectura por snapshot; no requiere polling).
- **Reparto por versión** (solo si layout=by_class/manual) — cuántos objetos caen en cada
  versión y de qué `kind`. Tipo recomendado: **barras apiladas por versión** (segmento
  schema/data). Justificación: muestra a la vez el tamaño de cada versión y su naturaleza
  (esquema vs datos) en una sola lectura, útil para validar que los datos quedaron al final.
  Datos: derivados en cliente de la selección / de `versions[]` en el resultado. Actualización:
  estática.
- No se recomiendan gráficas para `estimated_rows`: es una **estimación** y su valor guía una
  decisión puntual (¿siembro esta tabla?), no una tendencia; un número con badge de advertencia
  en la tabla de la Vista 5b comunica mejor que un gráfico.

### Supuestos (recopilación)
- `[SUPUESTO A]` `detail.context` (incl. `violations`) solo llega en desarrollo; en producción
  la UI se apoya en validación en cliente + `detail.msg` + `X-Request-ID`. Justificación:
  confirmado en los handlers del backend (`APP_ENV=="development"`).
- `[SUPUESTO B]` La lista de servidores y de BDs para la Vista 1 proviene de endpoints
  existentes (`GET /servers`, `/servers/{id}/databases` o `reconcile`), no de este contrato.
- `[SUPUESTO C]` La previsualización de versiones para `single`/`by_class` se calcula en cliente
  y se marca como "estimada"; la numeración final (que oculta clases vacías) la fija el backend.
- `[SUPUESTO D]` Puntos de entrada al asistente: listado de blueprints y/o pantalla de
  reconciliación (Plan 09), que puede prellenar servidor + BD.
- `[SUPUESTO E]` Existe una vista/endpoint de Plan 02 que expone el SQL de cada versión para el
  gate `reviewed`; esta feature solo enlaza a ella.
- `[SUPUESTO F]` `name` y `baseline_name` no tienen límites de longitud declarados en el
  contrato; se asume validación mínima (no vacío) en cliente hasta confirmar límites del backend.
```