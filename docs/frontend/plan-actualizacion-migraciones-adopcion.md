# Plan de Frontend — Actualización de Migraciones de Blueprints + Adopción

> Plan de implementación de frontend **tecnológicamente neutro**. No contiene código,
> frameworks ni decisiones de arquitectura frontend. Describe qué construir, qué validar,
> qué estados manejar y cómo navegar. El contrato de API es completo y está reflejado tal
> cual lo entrega el backend.

---

## Resumen de la actualización

Esta actualización introduce **4 cambios** en el backend del gateway que impactan la UI de
administración de blueprints (`DatabaseModel`), sus migraciones versionadas y la adopción
de bases de datos preexistentes:

1. **Stamp-on-adopt** — al adoptar una BD existente, el admin puede declarar en qué versión
   del blueprint ya se encuentra, evitando que un `apply` posterior reintente crear objetos
   que ya existen.
2. **Editar el SQL de una migración** — corrección de `up_sql` (y overrides por motor) de una
   migración que aún **no** se ha aplicado con éxito.
3. **Eliminar una versión (solo la punta)** — borrar únicamente la última versión del
   blueprint y sin historial de aplicación.
4. **`stamp` como vía de recuperación de cuarentena** — un stamp exitoso saca a una BD de
   `status="error"` y la vuelve a `active`.

Todas las rutas viven bajo `/api/v1`, requieren **sesión de administrador** (cookie de
sesión httpOnly), y responden con la envoltura estándar `ApiResponse[T]`:
`{ "data": <T>, "message": <string opcional> }` (los campos `null` se omiten del JSON).
Los errores controlados devuelven HTTP 4xx con cuerpo
`{ "detail": { "msg": "<mensaje>", ... } }`.

---

## 0. Contexto general del plan

**De qué va este plan.** Extender la UI de administración de blueprints y de bases de datos
gestionadas para soportar cuatro nuevos comportamientos del motor de migraciones: adopción
con versión declarada, corrección de SQL de migraciones no aplicadas, borrado seguro de la
versión más reciente, y recuperación de BDs en cuarentena mediante stamp.

**Módulo(s) involucrado(s).**
- **Migraciones de Blueprints — Plan 02**: rutas `POST/GET/PATCH/DELETE
  /database-models/{model_id}/migrations/...` y `/managed-databases/{id}/migrations/*`
  (controllers `model_migration_controller.py` y `managed_migration_controller.py`;
  schemas `app/schemas/model_migration.py`).
- **Adopción / Reconciliación / Snapshot — Plan 09**: ruta
  `POST /managed-databases/adopt` (controller `managed_database_controller.py`; schema
  `app/schemas/managed_database.py`), y `GET /servers/{id}/reconcile` como punto de entrada.

**Qué cubre este plan.**
- Formulario de adopción con selección de blueprint y versión de partida (Cambio 1).
- Edición del SQL de una migración con re-confirmación de overlays por motor (Cambio 2).
- Borrado de la versión punta desde el listado de versiones (Cambio 3).
- Acción de recuperación por stamp para BDs en `status="error"` (Cambio 4).
- Estados, validaciones y manejo de errores por código HTTP de cada flujo.

**Qué NO cubre este plan (fuera de alcance, deliberado).**
- Creación de migraciones (`POST .../migrations`) más allá de referenciarla como salida.
- Flujo completo de `apply` / `rollback` / `apply-all` (existente; solo se referencia).
- Generación de snapshots estructurales y creación de blueprints desde snapshot.
- La pantalla completa de reconciliación (se usa solo como origen de la adopción).
- Gestión de servidores y de ServerUsers (solo se consumen sus identificadores).

**Problemática.** Hoy el admin no puede: (a) adoptar una BD que ya existe en un motor y que
ya tiene cierto esquema sin arriesgar que el `apply` posterior falle con "la tabla ya
existe"; (b) corregir un error tipográfico o de SQL en una migración recién creada sin
recrearla; (c) borrar una versión mal creada; ni (d) recuperar de forma limpia una BD que
quedó en cuarentena por un apply fallido cuyo esquema en realidad ya coincide. Todo esto se
resuelve manualmente contra el motor o no se puede resolver desde la UI.

**Solución propuesta.** Se enriquece el formulario de adopción con un selector de "versión
en la que ya está la BD"; se añade una acción de edición del SQL en el detalle de la
migración con las salvaguardas correctas (bloqueo si ya se aplicó, re-confirmación de
overrides); se habilita el borrado solo sobre la versión punta; y se ofrece "stamp para
recuperar" como acción de resolución en las BDs en error. La UI traslada al usuario las
reglas de negocio del backend (irreversibilidad, precondiciones) de forma explícita.

**Actores / usuario objetivo.** El **admin único** del gateway (proyecto single-admin, sin
roles ni multi-tenant). No hay diferenciación de permisos por usuario.

**Flujo principal de uso.** El admin entra a la reconciliación de un servidor, detecta una
BD no gestionada y la **adopta declarando su versión de blueprint**. Más adelante, si
detecta un error en una migración aún no aplicada, la **corrige**; si creó una versión de
más, **borra la punta**. Si un `apply` deja una BD en cuarentena y verifica que el esquema
ya coincide, la **recupera con un stamp**.

---

## 1. Resumen del módulo o feature

Administración de migraciones versionadas de blueprints y de las bases de datos gestionadas
que las replican. Sirve al admin único del gateway. Flujo principal: adoptar/gestionar una
BD, mantener el catálogo de versiones del blueprint (crear/corregir/borrar la punta), y
mantener cada BD sincronizada y fuera de cuarentena mediante apply/stamp.

---

## 2. Entidades involucradas

### Entidad: ManagedDatabase (salida `ManagedDatabaseOut`)
- id:            number,  requerido, solo lectura
- name:          string,  requerido, solo lectura (nombre en el motor)
- server_id:     number,  requerido
- owner_id:      number,  requerido (ServerUser del mismo servidor)
- model_id:      number,  opcional (null si no vinculada a blueprint)
- model_version: string,  opcional (null si "en ceros")
- charset:       string,  opcional
- collation:     string,  opcional
- status:        enum,    requerido, valores: [pending, active, error, archived]
- notes:         string,  opcional
- origin:        enum,    requerido, valores: [provisioned, adopted]
- created_at:    date,    solo lectura
- updated_at:    date,    solo lectura

### Entidad: AdoptDatabaseIn (entrada del formulario de adopción)
- name:          string,  requerido, pattern: `^[A-Za-z_][A-Za-z0-9_]{0,62}$`
- server_id:     number,  requerido
- owner_id:      number,  requerido (ServerUser del MISMO servidor que `server_id`)
- model_id:      number,  opcional (requerido si se declara `model_version`)
- model_version: string,  opcional, maxLength: 50 (requiere `model_id`) — versión ya presente
- charset:       string,  opcional
- collation:     string,  opcional
- notes:         string,  opcional

### Entidad: ModelMigration (detalle `ModelMigrationOut`)
- id:                 number,  solo lectura
- model_id:           number,  solo lectura
- version:            string,  solo lectura (identifica la migración en la ruta)
- name:               string,  editable
- up_sql:             string,  editable (min 1, max 256 KB) — dialecto de referencia MySQL
- up_sql_mysql:       string,  opcional (override MySQL/MariaDB)
- up_sql_postgresql:  string,  opcional (override PostgreSQL)
- down_sql:           string,  opcional (rollback confirmado)
- down_sql_suggested: string,  opcional, solo lectura (regenerado por el backend)
- translated:         object,  solo lectura ({ mysql?: string, postgresql?: string })
- checksum:           string,  solo lectura (recalculado al editar up_sql)
- source_engine:      string,  opcional, solo lectura
- is_baseline:        boolean, solo lectura
- has_non_portable:   boolean, solo lectura
- reviewed:           boolean, editable (aprueba baseline de snapshot)
- created_at:         date,    solo lectura
- updated_at:         date,    solo lectura

### Entidad: ModelMigrationSummary (listados)
- id:                    number,  solo lectura
- model_id:              number,  solo lectura
- version:               string,  solo lectura
- name:                  string,  solo lectura
- has_mysql_override:    boolean, solo lectura
- has_postgresql_override:boolean, solo lectura
- has_rollback:          boolean, solo lectura
- checksum:              string,  solo lectura
- is_baseline:           boolean, solo lectura
- reviewed:              boolean, solo lectura
- created_at:            date,    solo lectura

### Entidad: MigrationStatusOut (estado de sincronización de una BD)
- managed_database_id: number,  solo lectura
- model_id:            number,  opcional
- slug:                string,  opcional
- current_version:     string,  opcional (versión actualmente marcada en el motor)
- latest_available:    string,  opcional (última versión del blueprint)
- pending_count:       number,  solo lectura
- pending_versions:    array de string, solo lectura

---

## 3. Contrato de API

### 3.1 — CAMBIO 1 · `POST /api/v1/managed-databases/adopt`

**Las 5 preguntas:**
1. **¿Qué hace?** Registra en el inventario del gateway una BD que ya existe en el motor
   (sin ejecutar DDL). Si se pasa `model_version`, además hace `stamp` de esa versión en el
   motor para dejar la BD marcada como "ya está en esa versión".
2. **¿Qué soluciona?** Permite gobernar una BD preexistente y evita que un `apply` posterior
   reintente crear objetos que ya existen (el clásico "la tabla ya existe"), declarando la
   versión de partida en lugar de inyectar `IF NOT EXISTS` (que enmascararía drift).
3. **¿Cuándo y cómo utilizarlo?** Cuando el admin decide adoptar una BD no gestionada
   detectada en la reconciliación. Se invoca al enviar el formulario de adopción con
   `name`, `server_id`, `owner_id` y, opcionalmente, `model_id` + `model_version`.
4. **¿En qué parte del flujo?** Acción "Adoptar" desde la pantalla de reconciliación de un
   servidor; consume el formulario de adopción y, al éxito, lleva al detalle de la BD
   gestionada (ver sección 5, Flujo A).
5. **¿Relación con otros módulos?** Consume `server_id` de la reconciliación
   (`GET /servers/{id}/reconcile`), `owner_id` del catálogo de ServerUsers del servidor,
   `model_id` de los blueprints (`DatabaseModel`) y `model_version` del listado de versiones
   del blueprint (`GET /database-models/{id}/migrations`). Su resultado (una BD con
   `model_version` seteada) alimenta el flujo posterior de `apply`/`status` de migraciones.

**Contrato técnico:**
```
POST /api/v1/managed-databases/adopt
Autenticación: requerida — sesión de administrador (cookie httpOnly)

Headers:
  - Cookie de sesión
  - Content-Type: application/json

Body (AdoptDatabaseIn):
  {
    "name":          string, requerido, pattern ^[A-Za-z_][A-Za-z0-9_]{0,62}$,
    "server_id":     number, requerido,
    "owner_id":      number, requerido,
    "model_id":      number, opcional,
    "model_version": string, opcional, maxLength 50 (requiere model_id),
    "charset":       string, opcional,
    "collation":     string, opcional,
    "notes":         string, opcional
  }

Respuesta exitosa (sobre ApiResponse):
  HTTP 201
  {
    "data": <ManagedDatabaseOut>,   // si se pasó model_version, viene seteado
    "message": string | omitido
  }

Respuestas de error (forma real: { "detail": { "msg": "...", ... } }):
  HTTP 404 → la BD "name" no existe en el motor del servidor
  HTTP 409 → la BD ya está adoptada / registrada
  HTTP 422 → model_version sin model_id, O la versión no existe en el blueprint
             (en este caso la BD NO queda registrada — validación pre-insert)
```

---

### 3.2 — CAMBIO 2 · `PATCH /api/v1/database-models/{model_id}/migrations/{version}`

**Las 5 preguntas:**
1. **¿Qué hace?** Actualiza campos de una migración de blueprint: `name`, `up_sql`,
   `down_sql`, overrides por motor (`up_sql_mysql`, `up_sql_postgresql`) y/o `reviewed`.
2. **¿Qué soluciona?** Permite corregir el SQL de una migración recién creada (errores
   tipográficos, ajustes) sin tener que crear una migración nueva, siempre que aún no se
   haya aplicado con éxito en ninguna BD. También sirve para aprobar (`reviewed`) un baseline
   de snapshot.
3. **¿Cuándo y cómo utilizarlo?** Desde el detalle de una migración, acción "Editar SQL".
   Se envían solo los campos a cambiar. Si se toca `up_sql` y existen overrides, hay que
   reenviarlos corregidos o limpiarlos con `null` en el mismo PATCH.
4. **¿En qué parte del flujo?** Detalle de migración → "Editar" → formulario de edición →
   guardar (ver sección 5, Flujo B).
5. **¿Relación con otros módulos?** Opera sobre la salida de `POST .../migrations` (creación).
   El bloqueo por aplicación exitosa depende del historial que genera `apply`
   (`POST /managed-databases/{id}/migrations/apply`). Al editar `up_sql`, el backend regenera
   `down_sql_suggested` y recalcula `checksum` (reflejados en `ModelMigrationOut`).

**Contrato técnico:**
```
PATCH /api/v1/database-models/{model_id}/migrations/{version}
Autenticación: requerida — sesión de administrador

Headers:
  - Cookie de sesión
  - Content-Type: application/json

Body (ModelMigrationPatch — todos opcionales):
  {
    "name":              string,
    "up_sql":            string, min 1, max 256 KB (dialecto de referencia MySQL),
    "down_sql":          string,
    "up_sql_mysql":      string | null (override MySQL/MariaDB),
    "up_sql_postgresql": string | null (override PostgreSQL),
    "reviewed":          boolean
  }

Respuesta exitosa (sobre ApiResponse):
  HTTP 200
  {
    "data": <ModelMigrationOut>,   // down_sql_suggested y checksum recalculados si cambió up_sql
    "message": string | omitido
  }

Respuestas de error (forma real: { "detail": { "msg": "...", ... } }):
  HTTP 409 (caso A) → up_sql ya fue aplicado EXITOSAMENTE en alguna BD
                      (mensaje sugiere fix-forward: crear nueva migración)
  HTTP 409 (caso B) → se cambió up_sql pero hay overrides obsoletos no reenviados
                      (mensaje: "reenvía corregido o limpia con null los overrides")
  HTTP 404 → model_id o version inexistentes
  HTTP 422 → validación de campos (p. ej. up_sql vacío o excede 256 KB)
```

> Nota: un intento de aplicación que solo **falló** NO bloquea la edición. Solo una
> aplicación **exitosa** produce el 409 del caso A.

---

### 3.3 — CAMBIO 3 · `DELETE /api/v1/database-models/{model_id}/migrations/{version}`

**Las 5 preguntas:**
1. **¿Qué hace?** Elimina una versión de migración del blueprint, únicamente si es la última
   (número más alto) y no tiene historial de aplicación.
2. **¿Qué soluciona?** Permite deshacer la creación de una versión errónea/de más sin
   ensuciar el catálogo, manteniendo la secuencia forward-only coherente.
3. **¿Cuándo y cómo utilizarlo?** Desde el listado de versiones, acción "Eliminar" habilitada
   solo en la versión punta. Se invoca tras confirmación del usuario.
4. **¿En qué parte del flujo?** Listado de versiones → "Eliminar" (solo en la punta) → modal
   de confirmación → refrescar listado (ver sección 5, Flujo C).
5. **¿Relación con otros módulos?** Al borrar la punta, el backend recalcula
   `current_version` del blueprint (retrocede; `"0.0.0"` si no quedan versiones), lo que
   afecta la información mostrada por `GET /managed-databases/{id}/migrations/status` y por
   el detalle del blueprint.

**Contrato técnico:**
```
DELETE /api/v1/database-models/{model_id}/migrations/{version}
Autenticación: requerida — sesión de administrador

Respuesta exitosa (sobre ApiResponse):
  HTTP 200
  { "data": null (omitido), "message": string | omitido }   // ApiResponse[None]

Respuestas de error (forma real: { "detail": { "msg": "...", ... } }):
  HTTP 409 → la versión NO es la última, O tiene historial de aplicación
             (el mensaje indica cuál es la versión "latest" actual)
  HTTP 404 → model_id o version inexistentes
```

---

### 3.4 — CAMBIO 4 · `POST /api/v1/managed-databases/{db_id}/migrations/stamp?version=XXXX`

**Las 5 preguntas:**
1. **¿Qué hace?** Marca en el motor que una BD está en una versión dada SIN ejecutar SQL. Si
   la BD estaba en `status="error"` (cuarentena), un stamp exitoso la vuelve a `active` y
   limpia `notes`.
2. **¿Qué soluciona?** Es la vía de recuperación para una BD que quedó en cuarentena por un
   apply fallido pero cuyo esquema ya coincide con el baseline: evita reintentar un
   `CREATE TABLE` de algo que ya existe.
3. **¿Cuándo y cómo utilizarlo?** Desde el detalle/estado de una BD gestionada, acción
   "Marcar versión (stamp)". Se pasa `version` como query param (patrón `^\d{4,10}$`).
   Especialmente ofrecido cuando la BD está en `status="error"`.
4. **¿En qué parte del flujo?** BD en `status="error"` → "Marcar versión (stamp) para
   recuperar" → seleccionar versión → confirmar (ver sección 5, Flujo D).
5. **¿Relación con otros módulos?** Complementa `apply?force=true` como resolución de
   cuarentena. Su resultado (`MigrationStatusOut`) coincide con el que devuelve
   `GET /managed-databases/{id}/migrations/status`; la versión a marcar proviene del listado
   de versiones del blueprint.

**Contrato técnico:**
```
POST /api/v1/managed-databases/{db_id}/migrations/stamp?version=XXXX
Autenticación: requerida — sesión de administrador
Rate limit: 10/min

Parámetros de query:
  - version: string, requerido, pattern ^\d{4,10}$

Respuesta exitosa (sobre ApiResponse):
  HTTP 200
  {
    "data": <MigrationStatusOut>,   // BD pasa de error → active si estaba en cuarentena
    "message": string | omitido
  }

Respuestas de error (forma real: { "detail": { "msg": "...", ... } }):
  HTTP 404 → db_id inexistente o versión no aplicable
  HTTP 422 → version no cumple el patrón ^\d{4,10}$
  HTTP 429 → rate limit excedido (10/min)
```

---

## 4. Vistas propuestas

### Vista A — Formulario de adopción de BD (Cambio 1)
Adoptar una BD preexistente declarando opcionalmente su versión de blueprint.

**Layout (wireframe textual):**
```
[Encabezado]
  Título: "Adoptar base de datos" | Contexto: servidor #<server_id> (nombre)

[Formulario]
  Campo: Nombre de la BD (name)         → precargado desde reconciliación, editable
  Campo: Servidor (server_id)           → fijo/precargado (contexto de origen)
  Selector: Propietario (owner_id)      → ServerUsers del mismo servidor
  Selector: Blueprint (model_id)        → opcional; lista de DatabaseModel
  Selector: Versión de partida          → visible SOLO si hay blueprint elegido
     opciones: [ "Vacía / en ceros" ] + versiones del blueprint (model_version)
  Campo: Charset (charset)              → opcional
  Campo: Collation (collation)          → opcional
  Área: Notas (notes)                   → opcional
  [Aviso contextual]
     Si versión != "vacía": "Se marcará (stamp) esta versión en el motor sin ejecutar SQL."
  [Botón: Adoptar] [Botón: Cancelar]
```

**Componentes por vista:**
- Formulario de adopción con validación en cliente.
- Selector con búsqueda para `owner_id` (relación con ServerUsers del servidor).
- Selector de blueprint (`model_id`) con búsqueda.
- Selector dependiente de versión de partida, poblado al elegir blueprint (incluye la
  opción "Vacía / en ceros"), oculto/deshabilitado si no hay blueprint.
- Aviso contextual sobre el stamp cuando se elige una versión concreta.

**Estados de UI requeridos:**
- `cargando`: al poblar el selector de versiones tras elegir blueprint; al enviar.
- `vacío`: si el blueprint elegido no tiene versiones → mostrar solo "Vacía / en ceros" con
  nota "Este blueprint aún no tiene versiones".
- `error`: mapear por código (ver sección 6). Mostrar `detail.msg`.
- `éxito`: confirmación (toast "BD adoptada") + redirección al detalle de la BD gestionada.

---

### Vista B — Detalle de migración + edición de SQL (Cambio 2)
Ver y corregir el SQL de una migración de blueprint no aplicada.

**Layout (wireframe textual):**
```
[Encabezado]
  Blueprint <slug> · Versión <version> · nombre
  Badges: [baseline?] [reviewed?] [tiene rollback?] [override MySQL] [override PG]
  Indicador: checksum

[Cuerpo — modo lectura]
  Bloque: up_sql (referencia MySQL)
  Bloque: up_sql_mysql (override) — si existe
  Bloque: up_sql_postgresql (override) — si existe
  Bloque: down_sql (confirmado) / down_sql_suggested (sugerido, solo lectura)
  Bloque: translated { mysql, postgresql } (solo lectura)

[Acciones]
  [Editar]  (deshabilitado con tooltip si ya fue aplicado con éxito)
  [Aprobar baseline] (reviewed=true) — visible si is_baseline y no reviewed

[Cuerpo — modo edición]
  Campo: name
  Editor: up_sql (min 1, max 256 KB)
  Aviso destacado: "Editar el SQL base regenera el rollback sugerido y el checksum,
     y requiere re-confirmar los overrides por motor."
  Si hay overrides existentes y se tocó up_sql:
     Editor: up_sql_mysql   → [reenviar corregido] o [limpiar (null)]
     Editor: up_sql_postgresql → [reenviar corregido] o [limpiar (null)]
     (no se puede guardar hasta resolver cada override)
  Editor: down_sql (confirmar rollback) — opcional
  [Guardar] [Cancelar]
```

**Componentes por vista:**
- Visor de SQL de solo lectura por dialecto/override.
- Editor de texto para SQL con contador de tamaño (límite 256 KB) y validación min 1.
- Bloque de re-confirmación de overrides con toggle "reenviar corregido" / "limpiar".
- Aviso destacado de efectos secundarios (regeneración de rollback y checksum).
- Badges de estado de la migración (baseline, reviewed, overrides, rollback).

**Estados de UI requeridos:**
- `cargando`: al abrir el detalle y al guardar.
- `vacío`: no aplica (siempre hay una migración).
- `error`: distinguir 409-caso A (ya aplicada → botón Editar deshabilitado y explicación
  fix-forward), 409-caso B (overrides obsoletos → forzar resolución en el mismo formulario),
  422 (validación de tamaño/vacío). Mostrar `detail.msg`.
- `éxito`: toast "Migración actualizada" + refresco del detalle (nuevo checksum y
  down_sql_suggested).

---

### Vista C — Listado de versiones del blueprint + borrado de la punta (Cambio 3)
Administrar el catálogo de versiones; borrar solo la última.

**Layout (wireframe textual):**
```
[Encabezado]
  Blueprint <slug> · current_version: <valor> | [Botón: Nueva versión]

[Tabla de versiones]
  Columnas: Versión | Nombre | Baseline | Reviewed | Overrides | Rollback | Creada | Acciones
  Acciones por fila: [Ver] [Eliminar]
    - [Eliminar] habilitado SOLO en la versión punta (número más alto)
    - En el resto: deshabilitado + tooltip: "Solo se puede eliminar la última versión (<latest>)"
  Footer: paginación (page / size)

[Estado vacío]
  Mensaje: "Este blueprint no tiene versiones" + [Botón: Crear primera versión]
```

**Componentes por vista:**
- Tabla de datos con paginación (`page`/`size`).
- Acción de borrado condicionada a la versión punta, con tooltip explicativo en las demás.
- Modal de confirmación para borrado (acción destructiva/irreversible).

**Estados de UI requeridos:**
- `cargando`: al listar y al borrar.
- `vacío`: mensaje + CTA de creación.
- `error`: 409 (no es la punta o tiene historial → mostrar `detail.msg` con la "latest"
  actual y refrescar la tabla, porque la punta pudo haber cambiado). 404 → versión ya no
  existe, refrescar.
- `éxito`: toast "Versión eliminada" + refresco de la tabla y de `current_version`.

---

### Vista D — Estado de migración de una BD + recuperación por stamp (Cambio 4)
Ver el estado de sincronización y recuperar una BD en cuarentena.

**Layout (wireframe textual):**
```
[Encabezado]
  BD <name> · servidor #<server_id> · Badge de status: [pending|active|error|archived]

[Panel de estado (MigrationStatusOut)]
  current_version | latest_available | pending_count
  Lista: pending_versions

[Banner de cuarentena — visible si status="error"]
  Texto: "Esta base está en cuarentena por un apply fallido."
  Acciones de resolución:
    [Reintentar apply (force)]   → apply?force=true (flujo existente)
    [Marcar versión (stamp) para recuperar]  → abre selector de versión

[Diálogo: stamp]
  Selector: versión a marcar (patrón numérico 4-10 dígitos)
  Aviso: "El stamp NO ejecuta SQL; solo marca la versión. Úsalo si el esquema ya coincide."
  [Confirmar] [Cancelar]
```

**Componentes por vista:**
- Panel de estado de sincronización (versión actual, disponible, pendientes).
- Banner de cuarentena con acciones de resolución (apply force + stamp).
- Diálogo de stamp con selector de versión y validación de patrón.
- Indicador de "operación en curso" (bloquear controles mientras el stamp está en vuelo).

**Estados de UI requeridos:**
- `cargando`: al leer el estado y al ejecutar el stamp (bloquear el diálogo).
- `vacío`: si la BD no está vinculada a blueprint (`model_id=null`) → ocultar stamp y
  mostrar nota "BD sin blueprint asociado".
- `error`: 404 (versión no aplicable), 422 (patrón inválido), 429 (rate limit → mensaje "Has
  alcanzado el límite de 10/min, intenta en un momento"). Mostrar `detail.msg`.
- `éxito`: toast "Versión marcada"; si venía de `error`, actualizar el badge a `active` y
  limpiar el banner de cuarentena; refrescar el panel de estado.

---

## 5. Flujo de navegación

**Flujo A — Adopción con versión (Cambio 1):**
```
Reconciliación del servidor (GET /servers/{id}/reconcile)
  → [clic en "Adoptar" en una BD unmanaged] → Formulario de adopción (Vista A)
      → [elegir blueprint (model_id)] → se puebla el selector de versión de partida
          → GET /database-models/{model_id}/migrations (listado de versiones)
      → [elegir versión concreta] → se muestra aviso de stamp
      → [Adoptar] → POST /managed-databases/adopt
          → [201] → Detalle de la BD gestionada (toast de éxito)
          → [422 versión inexistente] → permanece en el form, BD NO registrada, mostrar error
          → [404 / 409] → permanece en el form, mostrar error
      → [Cancelar] → vuelve a Reconciliación (sin cambios)
```

**Flujo B — Edición de SQL (Cambio 2):**
```
Listado de versiones (Vista C) → [Ver] → Detalle de migración (Vista B)
  → [Editar]  (si NO fue aplicada con éxito)
      → Modo edición
      → [cambiar up_sql con overrides presentes] → exige resolver cada override
      → [Guardar] → PATCH .../migrations/{version}
          → [200] → Detalle refrescado (nuevo checksum + down_sql_suggested), toast
          → [409 caso A: ya aplicada] → deshabilita Editar, muestra fix-forward
          → [409 caso B: overrides obsoletos] → mantiene edición, resalta overrides
          → [422] → mantiene edición, muestra validación
      → [Cancelar] → modo lectura (sin cambios)
```

**Flujo C — Borrado de la punta (Cambio 3):**
```
Listado de versiones (Vista C)
  → [Eliminar] (habilitado SOLO en la versión punta)
      → Modal de confirmación (advertencia de irreversibilidad)
          → [Confirmar] → DELETE .../migrations/{version}
              → [200] → refresca tabla + current_version, toast
              → [409] → muestra "latest" actual del mensaje, refresca tabla
          → [Cancelar] → cierra modal, sin cambios
```

**Flujo D — Recuperación por stamp (Cambio 4):**
```
Detalle/Estado de BD gestionada (Vista D)  [status="error"]
  → [Marcar versión (stamp) para recuperar]
      → Diálogo de stamp → [elegir versión] → [Confirmar]
          → POST /managed-databases/{db_id}/migrations/stamp?version=XXXX
              → [200] → status pasa a "active", limpia banner, refresca estado, toast
              → [404 / 422] → mantiene diálogo, muestra error
              → [429] → mantiene diálogo, mensaje de rate limit
      → [Cancelar] → cierra diálogo, sin cambios
  → [Reintentar apply (force)] → flujo de apply existente (referencia)
```

---

## 6. Consideraciones adicionales

### Manejo transversal de errores
Forma real del error: `{ "detail": { "msg": "<mensaje>", ... } }`. La UI debe **mostrar
`detail.msg`** al usuario (no inventar textos genéricos) y, cuando exista, conservar
cualquier identificador de request presente en el payload para soporte.

Mapa de códigos por acción:
- **Adopción (Cambio 1):** 404 = "la BD no existe en el motor" (revisar nombre exacto);
  409 = "ya adoptada" (ir al detalle existente); 422 = versión sin blueprint o versión
  inexistente → **la BD NO quedó registrada**, el usuario puede corregir y reintentar.
- **Edición (Cambio 2):** 409-A = ya aplicada con éxito → bloquear edición y sugerir crear
  nueva migración (fix-forward); 409-B = overrides obsoletos → forzar reenvío/limpieza;
  422 = SQL vacío o > 256 KB.
- **Borrado (Cambio 3):** 409 = no es la punta o tiene historial → mostrar la "latest"
  indicada y refrescar; 404 = ya no existe.
- **Stamp (Cambio 4):** 404 = versión no aplicable; 422 = patrón `^\d{4,10}$` inválido;
  429 = rate limit 10/min.

Distinción 4xx vs 5xx: los 4xx anteriores son errores de negocio/validación (mostrar
`detail.msg` y permitir corrección). Un 5xx debe tratarse como fallo inesperado (mensaje
genérico + conservar identificador de request si viene).

### Estados de la BD gestionada (`status`)
- `pending`: recién registrada, aún sin aplicar migraciones.
- `active`: operativa y sincronizada según el flujo.
- `error`: **cuarentena** por apply fallido → mostrar banner con las dos vías de
  recuperación (apply force / stamp).
- `archived`: fuera de operación (mostrar de solo lectura; ocultar acciones destructivas).
La UI debe reflejar estas transiciones tras cada acción (p. ej. `error → active` tras stamp
exitoso).

### Paginación
Offset con `page`/`size` (query params). Aplica al listado de versiones
(`GET /database-models/{id}/migrations`). Renderizar controles anterior/siguiente y total si
el `message`/metadatos lo proveen.

### Permisos
Proyecto **single-admin**: no hay roles ni multi-tenant. No se ocultan vistas por rol; toda
la UI asume un único administrador autenticado por cookie de sesión. `[SUPUESTO]` No aplica
diferenciación de permisos por usuario (declarado explícitamente).

### Operaciones destructivas / irreversibles
- **Borrado de versión (Cambio 3):** irreversible. Requiere **modal de confirmación** con
  advertencia explícita de irreversibilidad. Solo habilitado en la versión punta.
  `[SUPUESTO]` Confirmación simple (un clic en el modal) es suficiente; no se exige escribir
  el nombre de la versión, dado que el borrado ya está acotado por el backend a la punta sin
  historial. Justificación: el riesgo real está limitado por las precondiciones del backend.
- **Stamp (Cambio 4):** marca estado sin ejecutar SQL, pero cambia la versión declarada de la
  BD; usar diálogo de confirmación con el aviso "no ejecuta SQL; úsalo solo si el esquema ya
  coincide". Mostrar estado "operación en curso" (deshabilitar controles + spinner) durante
  la llamada.
- **Edición de SQL (Cambio 2):** no es destructiva sobre el motor, pero regenera rollback y
  checksum; avisar de esos efectos secundarios antes de guardar.
- **Estado "operación en curso":** para stamp, borrado y guardado de edición, deshabilitar el
  botón de acción y mostrar indicador de progreso hasta recibir respuesta, evitando
  doble-envío (especialmente relevante por el rate limit del stamp).

### Acciones en lote
No aplican en estos flujos. `[SUPUESTO]` No hay selección múltiple ni operaciones masivas en
los cambios cubiertos por este plan.

### Exportación y gráficas
No aplican en estos flujos. No hay descarga de archivos ni visualizaciones de datos en el
alcance de esta actualización.

### Supuestos (recopilación)
- `[SUPUESTO]` Single-admin: sin diferenciación de permisos por usuario en la UI.
- `[SUPUESTO]` Borrado de versión: confirmación simple en modal (sin exigir teclear el
  nombre), justificado por las precondiciones del backend (solo punta, sin historial).
- `[SUPUESTO]` Sin acciones en lote en los flujos de este plan.

---

## Checklist final de tareas de frontend (priorizadas)

**P0 — Bloqueantes para la nueva funcionalidad**
1. Formulario de adopción (Vista A) con selector dependiente de "versión de partida" que se
   puebla al elegir blueprint e incluye la opción "Vacía / en ceros".
2. Regla de UI: `model_version` solo enviable si hay `model_id`; deshabilitar/ocultar el
   selector de versión si no hay blueprint.
3. Manejo de errores de adopción por código (404 / 409 / 422), mostrando `detail.msg` y
   dejando claro que en 422 la BD NO quedó registrada.
4. Detalle de migración con acción "Editar SQL" (Vista B) y bloqueo del botón cuando ya fue
   aplicada con éxito (tooltip fix-forward).
5. Bloque de re-confirmación de overrides al editar `up_sql` (reenviar corregido o limpiar
   con null); impedir guardar hasta resolverlos; manejar 409-caso B.

**P1 — Salvaguardas y recuperación**
6. Listado de versiones (Vista C) con "Eliminar" habilitado solo en la punta + tooltip en el
   resto; modal de confirmación con advertencia de irreversibilidad; manejo de 409 con la
   "latest" actual y refresco.
7. Banner de cuarentena en el detalle de BD (Vista D) para `status="error"` con las dos vías
   de recuperación (apply force / stamp).
8. Diálogo de stamp con validación de patrón `^\d{4,10}$`, aviso de "no ejecuta SQL", estado
   "operación en curso" y transición visual `error → active` tras éxito.
9. Manejo de 429 (rate limit 10/min) en el stamp con mensaje claro y bloqueo temporal del
   botón.

**P2 — Refinamiento de estados y consistencia**
10. Aviso de efectos secundarios (regeneración de `down_sql_suggested` y `checksum`) al
    editar `up_sql`, con refresco del detalle tras guardar.
11. Reflejo consistente de `status` de la BD (pending/active/error/archived) con sus badges y
    ocultamiento de acciones destructivas en `archived`.
12. Paginación offset (`page`/`size`) en el listado de versiones.
13. Estados `cargando`/`vacío`/`error`/`éxito` en las cuatro vistas, con `detail.msg` como
    fuente de los textos de error.
