# API Reference v3 — Adopción, reconciliación y snapshot (Plan 09)

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md) y
> [`api-reference-v2.md`](api-reference-v2.md). Documenta los **5 endpoints nuevos** del Plan 09
> que cierran la brecha entre *lo que existe en el servidor real* y *lo que gestiona el gateway*.
>
> A diferencia de los documentos anteriores (centrados en el contrato técnico), esta guía está
> escrita para **construir la UI**: por cada capacidad encontrarás el **problema**, **qué debe
> pasar**, **escenarios**, **flujos**, los **endpoints**, **casos de uso**, **ejemplos** y una
> **sugerencia de interpretación visual** (cómo representarlo en pantalla).
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie de sesión,
> códigos de error, paginación) idénticas a las del documento original
> ([§3](api-reference.md#3-convenciones-de-la-api)).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## Índice

- [0. El problema que resuelve el Plan 09](#0-el-problema-que-resuelve-el-plan-09)
- [1. Conceptos transversales](#1-conceptos-transversales)
- [2. Reconciliación (drift): ver lo real vs. lo gestionado](#2-reconciliación-drift)
- [3. Adoptar una base de datos existente](#3-adoptar-una-base-de-datos-existente)
- [4. Adoptar un usuario existente](#4-adoptar-un-usuario-existente)
- [5. Snapshot estructural (preview)](#5-snapshot-estructural-preview)
- [6. Blueprint baseline desde snapshot](#6-blueprint-baseline-desde-snapshot)
- [7. Vincular una BD adoptada a migraciones (3 modos)](#7-vincular-una-bd-adoptada-a-migraciones-3-modos)
- [7-bis. Actualizar una BD a la última versión (una sola llamada)](#7-bis-actualizar-una-bd-a-la-última-versión-o-a-una-versión-x-en-una-sola-llamada)
- [7-ter. Explorar y editar las versiones de un blueprint (select + ver SQL + CRUD)](#7-ter-explorar-y-editar-las-versiones-de-un-blueprint-select--ver-sql--crud)
- [8. Tipos nuevos (referencia rápida)](#8-tipos-nuevos-referencia-rápida)
- [9. Matriz de errores](#9-matriz-de-errores)
- [10. Recomendaciones de UX / diseño](#10-recomendaciones-de-ux--diseño)

---

## 0. El problema que resuelve el Plan 09

### El problema

El gateway opera en **dos planos** que hasta ahora no se cruzaban:

- **Plano en vivo (motor real):** la verdad absoluta — todas las BDs y usuarios que existen
  físicamente en el servidor MySQL/MariaDB/PostgreSQL.
- **Plano de inventario (gateway):** solo lo que el gateway **creó o conoce**, con su metadata
  (dueño, blueprint, estado, auditoría).

Esto generaba una confusión real y reportada: al dar de alta un servidor, el "resumen" (en vivo)
mostraba **todas** las BDs/usuarios reales, pero el listado de gestión
(`GET /managed-databases`, `GET /server-users`) **solo** mostraba lo creado a través del gateway.
El usuario veía objetos que luego "desaparecían" al ir a la vista de gestión. **No era un bug**:
son dos preguntas distintas. Lo que faltaba era el **puente** entre ambos planos.

### Qué debe pasar (la solución)

Tres capacidades nuevas, todas **deliberadas y no destructivas**:

1. **Reconciliar** — ver, de un vistazo, qué está gestionado, qué existe pero no está gestionado
   (adoptable) y qué quedó huérfano (en el inventario pero borrado del motor).
2. **Adoptar** — tomar una BD o usuario que **ya existe** en el motor y registrarlo en el
   inventario **sin recrearlo** (sin `CREATE DATABASE`/`CREATE USER`).
3. **Fotografiar (snapshot)** — capturar la **estructura completa** de una BD existente y, si se
   desea, fijarla como **blueprint baseline** versionable.

> **Por qué NO se importa todo automáticamente al dar de alta el servidor:** rompería la regla de
> integridad "cada BD gestionada tiene exactamente un dueño", metería ruido (BDs de sistema/otras
> apps) y habilitaría operaciones destructivas (`DROP`) sobre objetos que nunca se quisieron
> gestionar. La adopción es **selectiva y explícita**.

### Posibilidades / escenarios típicos

| Escenario | Capacidad |
|---|---|
| "Acabo de conectar un servidor con 30 BDs legacy y quiero ver cuáles administra el gateway." | Reconcile |
| "Esta BD ya existe (la creó un DBA a mano); quiero gestionarla desde el gateway." | Adopt database |
| "El usuario `app_ro` ya existe en el motor; quiero administrarlo." | Adopt user |
| "Quiero convertir una BD legacy en una plantilla versionada para clonarla." | Snapshot → from-snapshot |
| "Borré una BD por fuera del gateway y el inventario quedó desfasado." | Reconcile (la marca `orphan`) |

---

## 1. Conceptos transversales

- **`state` de reconciliación:** `managed` · `unmanaged` · `orphan` (ver §2).
- **`origin` de una BD gestionada:** `provisioned` (la creó el gateway) | `adopted` (preexistía).
  Útil para mostrar un badge distinto en la UI.
- **`source_engine` / `has_non_portable` de un blueprint baseline:** un snapshot que incluye
  objetos procedurales (stored procedures, functions, triggers, events) queda **atado a su motor
  de origen** porque ese código no es traducible entre motores. La UI debe advertirlo.
- **Solo estructura, nunca datos:** ningún endpoint de snapshot devuelve filas de las tablas.

---

## 2. Reconciliación (drift)

### Problema
El admin no tiene forma de saber, de un vistazo, qué objetos del servidor están bajo gestión del
gateway y cuáles no.

### Qué debe pasar
Una sola llamada cruza el motor en vivo con el inventario y devuelve, por cada BD y usuario, su
**estado de reconciliación**. Es de **solo lectura**: no cambia nada.

### Flujo
```
[Pantalla del servidor] ──▶ GET /servers/{id}/reconcile
        │
        ├── databases: [{name, state, managed_id?, owner_id?, status?}]
        └── users:     [{username, host?, state, managed_id?}]
                  │
   state = managed   → ya gestionado (acciones normales)
   state = unmanaged → botón "Adoptar"
   state = orphan    → aviso "existe en el inventario pero no en el motor"
```

### Endpoint

#### `GET /api/v1/servers/{server_id}/reconcile` 🔒 🔌

Cruza el plano en vivo con el inventario. **No muta nada.**

**Respuesta** `200` — `ApiResponse[ReconcileResult]`:

| Campo | Tipo | Detalle |
|---|---|---|
| `server_id` | int | — |
| `databases[]` | `{name, state, managed_id?, owner_id?, status?}` | una entrada por BD |
| `users[]` | `{username, host?, state, managed_id?}` | una entrada por usuario |

`state` ∈ `managed` (en motor **y** inventario) · `unmanaged` (solo motor → **adoptable**) ·
`orphan` (solo inventario → se borró por fuera).

### Qué resuelve
Da la "foto de la verdad" combinada: responde a la vez *qué existe* y *qué administro*, que antes
exigía comparar dos listados a mano.

### Casos de uso
- Pantalla de "salud del servidor" tras conectarlo.
- Punto de entrada para el flujo de adopción (los `unmanaged` son los candidatos).
- Detección de drift operativo (`orphan` = alguien borró la BD por fuera).

### Ejemplos

**1) Servidor recién conectado, mayoría sin gestionar:**
```bash
curl https://<host>/api/v1/servers/42/reconcile -b cookies.txt
```
```json
{
  "data": {
    "server_id": 42,
    "databases": [
      { "name": "app_prod",   "state": "managed",   "managed_id": 7, "owner_id": 3, "status": "active" },
      { "name": "legacy_crm", "state": "unmanaged" },
      { "name": "reporting",  "state": "unmanaged" },
      { "name": "ventas_old", "state": "orphan",    "managed_id": 9 }
    ],
    "users": [
      { "username": "app_user", "host": "%", "state": "managed", "managed_id": 4 },
      { "username": "dba_root", "host": "10.0.%", "state": "unmanaged" }
    ]
  }
}
```

**2) PostgreSQL (los usuarios no tienen `host`):**
```json
{
  "data": {
    "server_id": 51,
    "databases": [ { "name": "billing", "state": "managed", "managed_id": 12, "owner_id": 5, "status": "active" } ],
    "users": [ { "username": "billing_app", "host": null, "state": "managed", "managed_id": 6 } ]
  }
}
```

### 🎨 Interpretación visual sugerida
Una **tabla de doble pestaña** ("Bases de datos" / "Usuarios") con una **columna de estado por
color**:

```
┌──────────────────────────────────────────────────────────────┐
│  Servidor: mysql-prod (#42)        [ Bases de datos | Usuarios ]│
├──────────────────────────────────────────────────────────────┤
│  Nombre        Estado          Dueño        Acción             │
│  app_prod      🟢 Gestionada   app_user     Ver ▸              │
│  legacy_crm    🟡 Sin gestionar  —          [ Adoptar ]        │
│  reporting     🟡 Sin gestionar  —          [ Adoptar ]        │
│  ventas_old    🔴 Huérfana      (#9)        [ Archivar/Limpiar]│
└──────────────────────────────────────────────────────────────┘
```
- 🟢 `managed` (verde): fila normal, click → detalle.
- 🟡 `unmanaged` (ámbar): CTA principal **"Adoptar"** (abre el formulario de §3/§4).
- 🔴 `orphan` (rojo): aviso de drift; ofrecer "archivar" o "eliminar del inventario".
- Añade un contador resumen arriba: `12 gestionadas · 18 adoptables · 1 huérfana`.

---

## 3. Adoptar una base de datos existente

### Problema
Una BD ya existe en el motor (la creó un DBA, un script, o una versión previa) y el equipo quiere
gestionarla desde el gateway **sin recrearla ni perder sus datos**.

### Qué debe pasar
El gateway **verifica que la BD exista** en el motor (solo lectura), exige un **dueño válido del
mismo servidor**, y registra la metadata con estado `active` y `origin=adopted`. **Nunca ejecuta
`CREATE DATABASE`** ni toca los datos.

### Flujo
```
reconcile → (state=unmanaged) → [Adoptar]
   │
   ├─ ¿Existe un ServerUser dueño en este servidor?
   │     NO → primero adoptar/crear el usuario (§4)
   │     SÍ → continuar
   └─ POST /managed-databases/adopt { name, server_id, owner_id }
         200/201 → aparece como 🟢 managed (origin=adopted)
```

### Endpoint

#### `POST /api/v1/managed-databases/adopt` 🔒 🔌

**Body** (`AdoptDatabaseIn`):

| Campo | Tipo | Req | Detalle |
|---|---|---|---|
| `name` | string | sí | Nombre EXACTO de la BD existente |
| `server_id` | int | sí | — |
| `owner_id` | int | sí | `ServerUser` del **mismo** servidor |
| `model_id` | int | no | Blueprint a vincular (opcional) |
| `model_version` | string | no | Versión del blueprint en la que **ya está** la BD → hace `stamp` al adoptar. Requiere `model_id`. Validada pre-insert (`422` si no existe). Omitir = BD "en ceros" |
| `charset`, `collation` | string | no | Opcionales (no se aplica DDL) |
| `notes` | string | no | — |

**Respuesta** `201` — `ApiResponse[ManagedDatabaseOut]` con `status: "active"`,
`origin: "adopted"`. Si se pasó `model_version`, `model_version` viene seteado y la versión
quedó marcada (`stamp`) en el motor.

**Errores:** `422` si `model_version` viene sin `model_id` o no existe en el blueprint (la BD
**no** queda registrada); `404` si la BD no existe en el motor; `409` si ya está adoptada.

### Qué resuelve
Convierte una BD "invisible" para el gateway en una BD gestionable (migraciones, reasignación de
dueño, auditoría) sin riesgo para los datos existentes.

### Casos de uso
- Onboarding de un servidor legacy.
- Recuperar gestión tras una creación manual de emergencia.
- Vincular de paso un blueprint (`model_id`) para luego migrarla.

### Ejemplos

**1) Adopción simple:**
```bash
curl -X POST https://<host>/api/v1/managed-databases/adopt -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "name": "legacy_crm", "server_id": 42, "owner_id": 3 }'
```
```json
{ "data": { "id": 21, "name": "legacy_crm", "server_id": 42, "owner_id": 3,
            "status": "active", "origin": "adopted", "model_id": null }, 
  "message": "Base de datos existente adoptada al inventario." }
```

**2) Adoptar y vincular un blueprint en el mismo paso:**
```bash
curl -X POST https://<host>/api/v1/managed-databases/adopt -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "name": "legacy_crm", "server_id": 42, "owner_id": 3, "model_id": 8 }'
```

**2b) Adoptar declarando que la BD ya está en la versión `0003` (stamp-on-adopt):**
```bash
curl -X POST https://<host>/api/v1/managed-databases/adopt -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "name": "legacy_crm", "server_id": 42, "owner_id": 3, "model_id": 8, "model_version": "0003" }'
```
> El `apply` posterior parte de `0003` — no reintenta crear lo que ya existe. Es la forma
> recomendada para una BD cuyo esquema ya coincide con un blueprint (vs. adoptar y luego `stamp`).

**3) Error — la BD no existe en el motor (`404`):**
```json
{ "detail": { "msg": "La base de datos no existe en el motor; no hay nada que adoptar." } }
```

**4) Error — ya estaba adoptada (`409`):**
```json
{ "detail": { "msg": "Ya existe una base de datos con ese nombre en el servidor (¿ya adoptada?)." } }
```

### 🎨 Interpretación visual sugerida
Un **modal "Adoptar base de datos"** lanzado desde la fila `unmanaged`:
```
┌─ Adoptar base de datos ─────────────────────────┐
│  Nombre:    legacy_crm   (precargado, solo lectura)│
│  Servidor:  mysql-prod #42                          │
│  Propietario *:  [ ▼ elegir ServerUser… ]           │
│      └ ⚠ Si no aparece, adopta primero el usuario   │
│  Blueprint (opcional):  [ ▼ ninguno ]               │
│                         [ Cancelar ]  [ Adoptar ]   │
└─────────────────────────────────────────────────┘
```
Tras éxito: la fila pasa de 🟡 a 🟢 y muestra un badge **`adoptada`** (distíngela de `provisioned`
con un ícono distinto, p. ej. 📥 vs 🛠). En `404`/`409` muestra el mensaje inline bajo el campo
correspondiente.

---

## 4. Adoptar un usuario existente

### Problema
Un usuario/rol ya existe en el motor y se quiere administrar desde el gateway, pero el gateway no
conoce (ni debe conocer) su contraseña.

### Qué debe pasar
El gateway **verifica que el usuario exista** (por `username`+`host` en MySQL, por `username` en
PostgreSQL) y lo registra **sin password** (`has_password=false`). Nunca ejecuta `CREATE USER`. La
contraseña se podrá fijar más adelante con el flujo normal de cambio de password.

### Flujo
```
reconcile (pestaña Usuarios) → (state=unmanaged) → [Adoptar usuario]
   └─ POST /server-users/adopt { server_id, username, host }
        201 → usuario gestionable (has_password=false)
        (opcional) PATCH .../{id} ?provision=true con password → fija credencial
```

### Endpoint

#### `POST /api/v1/server-users/adopt` 🔒 🔌

**Body** (`AdoptUserIn`): `{ server_id: int, username: string, host?: string (def "%"), notes?: string }`.
En PostgreSQL `host` se ignora.

**Respuesta** `201` — `ApiResponse[ServerUserOut]` con `has_password: false`.

### Qué resuelve
Permite gestionar usuarios preexistentes (asignarlos como dueños de BDs adoptadas, otorgarles
grants, auditarlos) sin tener que recrearlos ni conocer su clave.

### Casos de uso
- Adoptar el dueño de una BD que también se va a adoptar (orden: usuario → BD).
- Inventariar cuentas legacy para auditarlas.

### Ejemplos

**1) MySQL (con host):**
```bash
curl -X POST https://<host>/api/v1/server-users/adopt -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "server_id": 42, "username": "app_ro", "host": "10.0.%" }'
```
```json
{ "data": { "id": 15, "server_id": 42, "username": "app_ro", "host": "10.0.%",
            "has_password": false, "is_active": true },
  "message": "Usuario existente adoptado al inventario." }
```

**2) PostgreSQL (sin host):**
```bash
curl -X POST https://<host>/api/v1/server-users/adopt -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "server_id": 51, "username": "billing_app" }'
```

**3) Error — no existe (`404`)** / **ya adoptado (`409`)** — mismos patrones que §3.

### 🎨 Interpretación visual sugerida
Idéntico patrón de modal que §3 pero con `username`/`host` precargados. Tras adoptar, mostrar un
**badge ámbar `sin contraseña`** y un CTA secundario **"Fijar contraseña"** (abre el PATCH con
`provision=true`). Esto comunica que el gateway aún no puede autenticarse como ese usuario.

---

## 5. Snapshot estructural (preview)

### Problema
Antes de convertir una BD en plantilla, el admin necesita **ver exactamente qué estructura tiene**
(no solo tablas: también vistas, rutinas, triggers…), y poder revisarla.

### Qué debe pasar
El gateway introspecciona la BD en vivo y devuelve la lista de **sentencias DDL** que reconstruyen
su estructura, en **orden de dependencia**. **Solo estructura, jamás filas.** Es una *preview*: no
persiste nada.

### Flujo
```
[Detalle de BD] → [Ver snapshot] → GET /servers/{id}/databases/{db}/snapshot
   └─ StructureDump { source_engine, has_non_portable, statements[] }
        └─ (si gusta) → "Guardar como blueprint" → §6
```

### Endpoint

#### `GET /api/v1/servers/{server_id}/databases/{database}/snapshot` 🔒 🔌

**Respuesta** `200` — `ApiResponse[StructureDump]`:

| Campo | Tipo | Detalle |
|---|---|---|
| `database` | string | — |
| `source_engine` | string | `mysql` \| `mariadb` \| `postgresql` |
| `statements[]` | `{object_type, name, ddl}` | en orden de dependencia |
| `has_non_portable` | bool | `true` si hay objetos procedurales |

`object_type` ∈ `table` · `view` · `materialized_view` · `routine` · `trigger` · `sequence` ·
`type` · `extension` · `index` · `event`.

### Qué resuelve
Da una representación fiel y revisable de la estructura completa (incluida la lógica de negocio:
vistas, procedures, triggers), que es la base para crear un blueprint baseline.

### Casos de uso
- Inspección previa a clonar/plantillar una BD.
- Diff visual entre dos BDs (capturar ambas y comparar — fuera de alcance del backend, pero la UI
  puede hacerlo).
- Documentar la estructura de una BD legacy.

### Ejemplos

**1) BD con tabla + vista + procedure (MySQL):**
```bash
curl https://<host>/api/v1/servers/42/databases/legacy_crm/snapshot -b cookies.txt
```
```json
{
  "data": {
    "database": "legacy_crm",
    "source_engine": "mysql",
    "has_non_portable": true,
    "statements": [
      { "object_type": "table",   "name": "clientes", "ddl": "CREATE TABLE `clientes` (`id` int PRIMARY KEY, `nombre` varchar(120))" },
      { "object_type": "view",    "name": "v_top",    "ddl": "CREATE VIEW `v_top` AS SELECT * FROM `clientes` LIMIT 10" },
      { "object_type": "routine", "name": "sp_alta",  "ddl": "CREATE PROCEDURE `sp_alta`(IN n VARCHAR(120)) BEGIN INSERT INTO clientes(nombre) VALUES(n); END" }
    ]
  }
}
```

**2) PostgreSQL con extensión + tipo enum + tabla (portable, sin procedurales):**
```json
{
  "data": {
    "database": "billing", "source_engine": "postgresql", "has_non_portable": false,
    "statements": [
      { "object_type": "extension", "name": "pgcrypto", "ddl": "CREATE EXTENSION IF NOT EXISTS \"pgcrypto\"" },
      { "object_type": "type",      "name": "estado",   "ddl": "CREATE TYPE \"estado\" AS ENUM ('alta','baja')" },
      { "object_type": "table",     "name": "facturas", "ddl": "CREATE TABLE facturas (...)" }
    ]
  }
}
```

### 🎨 Interpretación visual sugerida
Un **panel de revisión agrupado por tipo de objeto**, con el DDL en bloques de código colapsables:
```
Snapshot de legacy_crm (mysql)        ⚠ contiene objetos no portables
┌─────────────────────────────────────────────┐
│ ▸ Tablas (1)        clientes                  │
│ ▸ Vistas (1)        v_top                     │
│ ▾ Rutinas (1)  ⚠    sp_alta                   │
│      CREATE PROCEDURE `sp_alta` ( … ) BEGIN…  │   ← bloque de código
└─────────────────────────────────────────────┘
        [ Guardar como blueprint baseline ▸ ]
```
- Muestra un **banner de advertencia** cuando `has_non_portable=true`: *"Incluye procedimientos/
  triggers: el blueprint quedará atado al motor MySQL."*
- Agrupa por `object_type` con contadores; resalta con ⚠ los tipos procedurales (`routine`,
  `trigger`, `event`).
- El orden de `statements` ya es el de dependencia: respétalo si muestras el DDL completo.

---

## 6. Blueprint baseline desde snapshot

### Problema
Una BD legacy/única no encaja con ningún blueprint existente, pero se quiere usar como **punto de
partida versionado** (para clonarla, migrarla en el futuro, o estandarizarla).

### Qué debe pasar
El gateway toma el snapshot estructural y crea un **blueprint nuevo** cuya migración baseline
(`0001`) contiene ese DDL. El baseline se etiqueta con `source_engine` y `has_non_portable`. Si
contiene objetos procedurales, **no podrá aplicarse a un motor distinto** (ver §7).

> **⚠ Aprobación requerida (R1):** como el baseline es **DDL capturado del motor** (potencialmente
> no confiable), nace **`reviewed: false`** y **no se puede aplicar** hasta que un admin lo revise
> y apruebe con `PATCH …/migrations/0001` `{"reviewed": true}`. Mientras tanto, `apply`/`apply-all`
> responden **`409`**. Ver §7-ter ("Aprobación de un baseline") y la guía dedicada
> [`api-reference-v4.md`](api-reference-v4.md) (flujo completo de revisión/aprobación).

### Flujo
```
snapshot (§5) → [Guardar como blueprint] → POST /database-models/from-snapshot
   └─ crea DatabaseModel + ModelMigration v0001 (is_baseline=true)
        └─ a partir de aquí: crear v0002, v0003… (migraciones normales del Plan 02)
```

### Endpoint

#### `POST /api/v1/database-models/from-snapshot` 🔒 🔌 · *rate limit 10/min*

**Body** (`FromSnapshotIn`):

| Campo | Tipo | Req | Detalle |
|---|---|---|---|
| `server_id` | int | sí | servidor de la BD a fotografiar |
| `database` | string | sí | BD existente |
| `name` | string | sí | nombre del blueprint a crear |
| `slug` | string | sí | identificador estable (`^[a-z0-9]+([-_][a-z0-9]+)*$`) |
| `description` | string | no | — |
| `baseline_name` | string | no | nombre de la migración baseline (def "Snapshot baseline") |

**Respuesta** `201` — `ApiResponse[FromSnapshotOut]`:

| Campo | Tipo | Detalle |
|---|---|---|
| `model` | `DatabaseModelOut` | el blueprint creado |
| `baseline_version` | string | `"0001"` |
| `source_engine` | string | motor de origen |
| `has_non_portable` | bool | — |
| `object_counts` | `{tipo: n}` | conteo por tipo de objeto |
| `statements_captured` | int | total de sentencias |

### Qué resuelve
Convierte una estructura legacy en una **plantilla versionable** reutilizando todo el módulo de
migraciones de blueprints (Plan 02) de ahí en adelante.

### Casos de uso
- Estandarizar una BD "modelo" para clonarla en N servidores.
- Congelar el estado actual como `v0001` y evolucionar con deltas.

### Ejemplos

**1) Crear blueprint desde una BD MySQL:**
```bash
curl -X POST https://<host>/api/v1/database-models/from-snapshot -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "server_id": 42, "database": "legacy_crm", "name": "CRM Legacy", "slug": "crm-legacy" }'
```
```json
{
  "data": {
    "model": { "id": 8, "name": "CRM Legacy", "slug": "crm-legacy", "current_version": "0001", "is_active": true },
    "baseline_version": "0001",
    "source_engine": "mysql",
    "has_non_portable": true,
    "object_counts": { "table": 6, "view": 2, "routine": 1 },
    "statements_captured": 9
  },
  "message": "Blueprint baseline creado desde snapshot."
}
```

**2) Error — la BD está vacía (`422`):**
```json
{ "detail": { "msg": "La base de datos no tiene objetos estructurales que fotografiar." } }
```

**3) Error — slug/nombre duplicado (`409`):**
```json
{ "detail": { "msg": "Ya existe un blueprint con ese nombre o slug." } }
```

### 🎨 Interpretación visual sugerida
Botón **"Guardar como blueprint baseline"** al pie del panel de snapshot (§5). Al pulsarlo, un
modal pide `name`/`slug`/`description`. Tras éxito, muestra una **tarjeta resumen** del blueprint
con los `object_counts` como chips (`6 tablas · 2 vistas · 1 rutina`) y, si `has_non_portable`, un
**candado con el motor** (`🔒 mysql`) indicando que es específico de ese motor.

---

## 7. Vincular una BD adoptada a migraciones (3 modos)

Una BD adoptada puede engancharse al sistema de migraciones de **tres maneras**. Estos modos
**reutilizan endpoints ya existentes** del Plan 02 ([api-reference §8–9](api-reference.md#8-blueprints-de-bd-database-models));
aquí se explica **cuándo usar cada uno** desde la UI.

> **Regla de oro:** nunca apliques migraciones de construcción (`v0001…`) sobre una BD que ya
> tiene datos/estructura — fallaría o dañaría. Usa `stamp` para "marcar" el punto de partida.

| Modo | Cuándo | Endpoint | Efecto |
|---|---|---|---|
| **1 — Aplicar desde v1** | La BD adoptada está **vacía** | `POST /managed-databases/{id}/migrations/apply?dry_run=true` y luego sin `dry_run` | Crea la estructura del blueprint. **Haz primero el dry-run.** |
| **2 — Stamp en versión X** | La BD ya **coincide** con un blueprint en la versión X | `POST /managed-databases/{id}/migrations/stamp?version=000X` | Marca la versión **sin ejecutar SQL**; de X+1 en adelante solo corren deltas nuevos. |
| **3 — Snapshot → baseline** | La BD tiene estructura **propia/legacy** | `POST /database-models/from-snapshot` (§6) + `stamp` en el baseline | Crea blueprint baseline y marca la BD en él. |

Para vincular el blueprint a la BD adoptada antes de migrar:
`PATCH /api/v1/managed-databases/{id}` con `{ "model_id": <blueprint> }`.

### Cross-engine guard (importante para la UI)
Si un blueprint tiene un baseline de snapshot **no portable** (`has_non_portable=true`,
`source_engine=mysql`) y se intenta **aplicar** a un servidor de **otro motor** (p. ej.
PostgreSQL), el endpoint `apply` responde **`422`**:
```json
{ "detail": { "msg": "El blueprint tiene un baseline de snapshot del motor 'mysql' con objetos no portables (rutinas/triggers): no puede aplicarse a un servidor 'postgresql'. Genere un baseline específico para este motor." } }
```
La UI debería **deshabilitar** el botón "Aplicar" (o avisar antes) cuando el motor de la BD destino
≠ `source_engine` del blueprint baseline no portable.

### 🎨 Interpretación visual sugerida
Un **asistente de 1 paso ("Vincular a migraciones")** que, según el estado de la BD adoptada,
recomiende el modo:
```
¿Cómo está esta base de datos adoptada?
  ( ) Vacía            → Modo 1: aplicar desde v0001  [requiere dry-run]
  ( ) Igual a un blueprint conocido (versión: ____)  → Modo 2: stamp
  (•) Estructura propia → Modo 3: crear blueprint baseline desde snapshot
```

---

## 7-bis. Actualizar una BD a la última versión (o a una versión X) en UNA sola llamada

### Problema
El equipo asumía que para llevar una BD de su versión actual a la última había que llamar al
endpoint **una vez por cada versión** (pasar `0003`, luego `0004`, luego `0005`…). Eso es tedioso
y propenso a errores de orden.

### Qué pasa en realidad
**No hace falta.** `POST /managed-databases/{id}/migrations/apply` aplica **todas** las migraciones
pendientes **secuencialmente y en orden** en **una sola llamada**. Tú decides el destino con el
parámetro `version`:

- **Sin `version`** → aplica hasta la **última** disponible.
- **`?version=000X`** → aplica `actual+1 … X` (incluida X).
- **Forward-only** → si `version` ≤ la actual, no hace nada (no baja de versión; para revertir está
  `/rollback`).
- **`422`** si `version` no existe en el blueprint.
- **`?dry_run=true`** → te devuelve el plan (qué se aplicaría) sin tocar la BD.
- **`409`** si el blueprint tiene un **baseline de snapshot sin revisar** (R1): apruébalo primero
  (`PATCH …/migrations/{version}` `{"reviewed": true}`, §7-ter).

El gateway internamente calcula la lista ordenada (orden **numérico**, no `"0010" < "0009"`),
adquiere un lock por BD y aplica una a una, deteniéndose en el primer fallo.

### Escenarios / flujos
```
1) BD nueva (sin versión)         → apply            → aplica 0001..0005
2) BD en 0003, blueprint en 0005  → apply            → aplica 0004, 0005
3) BD en 0002, "quiero la 0005"   → apply?version=0005 → aplica 0003, 0004, 0005
4) BD en 0005, pido 0003          → apply?version=0003 → no-op (sugerir /rollback)
5) "¿qué pasaría?"                → apply?dry_run=true → plan sin ejecutar
```

### Endpoint (mismo de siempre, respuesta enriquecida)

#### `POST /api/v1/managed-databases/{db_id}/migrations/apply` 🔒 🔌 · *rate 10/min*

**Query:** `version?` (`\d{4,10}`, objetivo inclusive; omitir = última) · `force?` (override de
cuarentena) · `dry_run?`.

**Respuesta** `200` — `ApiResponse[MigrationApplyOut]`:

| Campo | Tipo | Detalle |
|---|---|---|
| `from_version` | string\|null | versión ANTES de aplicar |
| `to_version` | string\|null | versión DESPUÉS de aplicar |
| `target_version` | string\|null | lo solicitado (`null` = última) |
| `applied_count` | int | nº de migraciones aplicadas |
| `no_op` | bool | `true` si no había nada que aplicar |
| `failed` / `quarantined` | bool | hubo fallo / quedó en cuarentena |
| `dry_run` | bool | — |
| `pending_versions` | string[] | versiones consideradas/aplicadas |
| `results` | list | `{migration_id, version, status, error?, execution_ms}` |

### Casos de uso
- Botón **"Actualizar a la última"** (sin `version`).
- Selector **"Ir a la versión…"** (`?version=000X`).
- **Previsualizar** antes de aplicar (`?dry_run=true`).

### Ejemplos

**1) Actualizar a la última (BD en 0002 → 0005), una sola llamada:**
```bash
curl -X POST "https://<host>/api/v1/managed-databases/11/migrations/apply" -b cookies.txt
```
```json
{ "data": { "from_version": "0002", "to_version": "0005", "target_version": null,
            "applied_count": 3, "no_op": false, "failed": false,
            "pending_versions": ["0003","0004","0005"],
            "results": [ {"version":"0003","status":"applied","execution_ms":12},
                         {"version":"0004","status":"applied","execution_ms":8},
                         {"version":"0005","status":"applied","execution_ms":20} ] },
  "message": "Aplicadas 3 migración(es): 0002 → 0005." }
```

**2) Ir a una versión específica (0005) estando en 0002:**
```bash
curl -X POST "https://<host>/api/v1/managed-databases/11/migrations/apply?version=0005" -b cookies.txt
```

**3) Ya está al día (no-op):**
```json
{ "data": { "from_version": "0005", "to_version": "0005", "applied_count": 0, "no_op": true },
  "message": "La BD ya está en la versión más reciente (0005); nada que aplicar." }
```

**4) Pido una versión anterior a la actual (no-op, sin downgrade):**
```json
{ "data": { "from_version": "0005", "to_version": "0005", "target_version": "0003", "no_op": true },
  "message": "La versión solicitada (0003) ya está aplicada o es anterior a la actual (0005): no se aplica nada (usa /rollback para revertir)." }
```

**5) Previsualizar (dry-run):**
```json
{ "data": { "dry_run": true, "from_version": "0002", "to_version": "0005",
            "pending_versions": ["0003","0004","0005"], "no_op": false } }
```

### 🎨 Interpretación visual sugerida
Una **barra de versiones** con el estado actual y un CTA de actualización:
```
Blueprint: WhatsApp     BD en ● 0002        Última: 0005
  0001 ─●─ 0002 ─○─ 0003 ─○─ 0004 ─○─ 0005
         ▲ actual            (3 pendientes)
   [ Actualizar a la última (0005) ]   [ Ir a versión… ▼ ]   [ Previsualizar ]
```
- Tras aplicar, anima el avance `0002 → 0005` y muestra un toast con `applied_count` y el rango.
- Si `no_op`, no muestres error: un aviso neutro "ya estás al día" (o, si pidió una versión
  anterior, ofrece el botón **"Revertir (rollback)"**).
- Usa **`dry_run`** para poblar la lista de "pendientes" antes de confirmar; deshabilita el CTA si
  `pending_versions` está vacío.
- Para una migración con `failed=true`: marca la versión que falló en rojo y muestra el banner de
  **cuarentena** con la acción "Reintentar con force".

### Revertir a una versión anterior (rollback secuencial)

El rollback es el **espejo** del apply: también en **una sola llamada** revierte secuencialmente
todas las migraciones necesarias hasta una versión objetivo. **No** hay que revertir una por una.

#### `POST /api/v1/managed-databases/{db_id}/migrations/rollback` 🔒 🔌 · *rate 10/min*

**Query:** `confirm_version` (**obligatorio**, = versión ACTUAL, doble confirmación de operación
destructiva) · `target_version?` (destino, **anterior** a la actual; si se omite, revierte solo la
última).

- Estando en `0010`, `?confirm_version=0010&target_version=0007` → revierte `0010, 0009, 0008`
  en orden y deja la BD en `0007`, en **una** llamada.
- **`409`** si alguna migración del camino no tiene `down_sql` confirmado (se valida ANTES de
  ejecutar; indica qué versiones confirmar con `PATCH`).
- **`422`** si `target_version` no es anterior a la actual (para avanzar se usa `apply`) o no existe.
- Forward = `apply`; backward = `rollback`. Nunca se mezclan.

**Respuesta** `200` — `ApiResponse[MigrationRollbackOut]`: `from_version`→`to_version`,
`target_version`, `reverted_count`, `reverted_versions` (de la más reciente a la más antigua),
`failed`, `quarantined`, `results`.

```bash
curl -X POST "https://<host>/api/v1/managed-databases/11/migrations/rollback?confirm_version=0010&target_version=0007" -b cookies.txt
```
```json
{ "data": { "from_version": "0010", "to_version": "0007", "target_version": "0007",
            "reverted_count": 3, "reverted_versions": ["0010","0009","0008"],
            "failed": false, "quarantined": false },
  "message": "Revertidas 3 migración(es): 0010 → 0007." }
```

**Error típico (`409`) — falta confirmar un `down_sql` en el camino:**
```json
{ "detail": { "msg": "No se puede revertir: las versiones 0009 no tienen rollback (down_sql) confirmado. Confírmalo con PATCH en cada migración." } }
```

> 🎨 **Visual:** reusa la barra de versiones del apply pero con la flecha hacia atrás
> (`0010 → 0007`). Antes de ejecutar, muestra qué versiones se desharán (`reverted_versions`) y un
> aviso destructivo (pide repetir la versión actual = `confirm_version`). Si algún paso no tiene
> `down_sql`, deshabilita el botón y enlaza a confirmar el rollback de esas versiones.

---

## 7-ter. Explorar y editar las versiones de un blueprint (select + ver SQL + CRUD)

### Problema
El frontend necesita: (a) poblar un **select** con las versiones de un blueprint **sin** cargar el
SQL (rápido y ligero), (b) al elegir una versión, **ver su SQL** (y la traducción por motor) para
analizar qué cambios implica, y (c) **gestionar** las versiones (crear, editar, eliminar) para
preparar la evolución del blueprint.

### Qué pasa / posibilidades
Hay **dos vistas** deliberadamente separadas:
- **Listado ligero** (sin SQL) → para el select.
- **Detalle** (con SQL + traducciones) → al seleccionar una versión.

La **versión actual de una BD** se obtiene con un endpoint aparte (`…/migrations/status`); no se
mezcla con el listado del blueprint (ver nota de no-redundancia abajo). El CRUD tiene **guards de
integridad** para no corromper historia ya aplicada.

### Flujos
```
[Pantalla del blueprint]
  GET …/migrations                 → opciones del <select> (version + name, sin SQL)
        │  (usuario elige 0005)
        ▼
  GET …/migrations/0005            → panel de detalle: up_sql, overrides, down_sql, translated
        │
        ├─ POST …/migrations             → crear una versión nueva
        ├─ PATCH …/migrations/0005        → confirmar down_sql / añadir override por motor
        └─ DELETE …/migrations/0005       → eliminar (solo si NUNCA se aplicó; si no, 409)

[Para una BD concreta]
  GET /managed-databases/{db_id}/migrations/status  → current_version + pending_versions
        └─ el front marca en el <select> la actual y distingue aplicadas vs pendientes
```

### Endpoints (todos 🔒; **GW** = solo inventario, no tocan el motor)

| Método | Ruta | Devuelve | Uso |
|---|---|---|---|
| `GET` | `/api/v1/database-models/{model_id}/migrations` | `list[ModelMigrationSummary]` (paginado, **sin SQL**) | **Opciones del select** |
| `GET` | `/api/v1/database-models/{model_id}/migrations/{version}` | `ModelMigrationOut` (**con SQL** + `translated`) | Detalle al seleccionar |
| `POST` | `/api/v1/database-models/{model_id}/migrations` | `ModelMigrationOut` (`201`) | Crear versión (**`version` opcional → autoasignada**) |
| `PATCH` | `/api/v1/database-models/{model_id}/migrations/{version}` | `ModelMigrationOut` | Confirmar `down_sql` / overrides |
| `DELETE` | `/api/v1/database-models/{model_id}/migrations/{version}` | — | Eliminar versión |
| `GET` | `/api/v1/managed-databases/{db_id}/migrations/status` | `MigrationStatusOut` | Versión actual de **una BD** |

> **No redundancia (decisión de diseño):** el listado de versiones es del **blueprint**; la versión
> actual es de **una BD**. Son dos preguntas distintas → dos endpoints. El frontend combina en
> cliente: `list` da las opciones, `status` da `current_version` + `pending_versions` para
> resaltar/clasificar. No se pasa `db_id` al listado de versiones a propósito.

> **Versión autoasignada (al crear):** `version` en el `POST` es **opcional**. Si se omite, el
> gateway asigna la **siguiente secuencial** (max+1) de forma autónoma y con reintento ante
> colisión — pensado para varios colaboradores creando migraciones a la vez (nadie tiene que
> consultar antes "cuál fue la última"). Pásala solo si quieres fijarla a mano; una versión
> explícita duplicada da `409`. En la UI: deja el campo "versión" vacío por defecto (placeholder
> "siguiente: 000N") y muéstralo opcional/avanzado.

### Qué resuelve
Separar "catálogo de versiones del blueprint" (ligero, para el select) de "el SQL de una versión"
(detalle on-demand) y de "en qué versión está esta BD" (status). Cada pantalla pide solo lo que
necesita.

### Casos de uso
- **Select de versiones** en un formulario de "aplicar/actualizar" o "revertir".
- **Visor de SQL** para revisar qué hará una migración antes de aplicarla (panel de detalle).
- **Editor de blueprint**: crear deltas, confirmar el `down_sql` de rollback, añadir overrides por
  motor.
- **Comparar**: traer dos detalles y diferenciar su `up_sql` en cliente.

### Ejemplos

**1) Listado para el select (sin SQL) — paginado:**
```bash
curl "https://<host>/api/v1/database-models/8/migrations?page=1&size=50" -b cookies.txt
```
> El listado es **paginado**: `size` está acotado por `PAGINATION_MAX_SIZE` (por defecto **50**,
> tope duro 200). Si un blueprint tiene más versiones que `size`, recorre las páginas con `page`
> (usa `pagination.total`/`pages` de la respuesta). No asumas que una sola llamada trae todas.
```json
{ "data": [
    { "id": 1, "model_id": 8, "version": "0001", "name": "Esquema inicial",
      "has_mysql_override": false, "has_postgresql_override": false, "has_rollback": true,
      "checksum": "…", "created_at": "2026-06-20T10:00:00Z" },
    { "id": 2, "model_id": 8, "version": "0002", "name": "Add phone", "has_rollback": false, "…": "…" }
  ],
  "pagination": { "page": 1, "size": 200, "total": 10, "pages": 1 } }
```

**2) Detalle de una versión (CON el SQL y la traducción por motor):**
```bash
curl "https://<host>/api/v1/database-models/8/migrations/0002" -b cookies.txt
```
```json
{ "data": {
    "id": 2, "model_id": 8, "version": "0002", "name": "Add phone",
    "up_sql": "ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    "up_sql_mysql": null, "up_sql_postgresql": null,
    "down_sql": null, "down_sql_suggested": "ALTER TABLE users DROP COLUMN phone",
    "translated": { "mysql": "ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
                    "postgresql": "ALTER TABLE users ADD COLUMN phone VARCHAR(20)" },
    "checksum": "…", "source_engine": null, "is_baseline": false, "has_non_portable": false,
    "reviewed": true, "created_at": "…", "updated_at": "…" } }
```

**3) Crear una versión SIN pasar el número (autoasignada — recomendado):** omite `version` y
el gateway le pone la siguiente secuencial:
```bash
curl -X POST "https://<host>/api/v1/database-models/8/migrations" -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "name": "Add status", "up_sql": "ALTER TABLE orders ADD COLUMN status VARCHAR(20)" }'
```
```json
{ "data": { "version": "0003", "name": "Add status", "is_baseline": false, "reviewed": true,
            "down_sql_suggested": "ALTER TABLE orders DROP COLUMN status;", "checksum": "…" },
  "message": "Migración creada." }
```
> Si necesitas fijar la versión a mano, incluye `"version": "0003"`; una duplicada da `409`.

**4) Confirmar el `down_sql` (rollback) de una versión vía PATCH:**
```bash
curl -X PATCH "https://<host>/api/v1/database-models/8/migrations/0002" -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "down_sql": "ALTER TABLE users DROP COLUMN phone" }'
```

**5) Versión actual de una BD (para marcar el select):**
```bash
curl "https://<host>/api/v1/managed-databases/11/migrations/status" -b cookies.txt
```
```json
{ "data": { "managed_database_id": 11, "model_id": 8, "slug": "whatsapp",
            "current_version": "0003", "latest_available": "0010",
            "pending_count": 7, "pending_versions": ["0004","0005","0006","0007","0008","0009","0010"] } }
```

**6) Eliminar una versión nunca aplicada → `204/200`; aplicada en alguna BD → `409`:**
```json
{ "detail": { "msg": "No se puede eliminar una migración con historial de aplicación. Revierta y/o cree una migración compensatoria." } }
```

### Aprobación de un baseline (R1 — seguridad)
Las versiones tienen un campo **`reviewed`**: las escritas a mano nacen `true`; un **baseline de
snapshot** (`is_baseline: true`, DDL capturado del motor) nace **`false`** y **no se puede
aplicar** (`apply`/`apply-all` → `409`) hasta que un admin lo revise y apruebe:
```bash
curl -X PATCH "https://<host>/api/v1/database-models/8/migrations/0001" -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "reviewed": true }'
```
> 🎨 Visual: muestra un badge **"⚠ pendiente de revisión"** en las versiones con `reviewed: false`,
> con un botón **"Revisar y aprobar"** que abre el detalle (SQL) y, al confirmar, hace el PATCH.
> Deshabilita el botón "Aplicar" de cualquier BD de ese blueprint mientras el baseline no esté
> aprobado (el backend igualmente devuelve `409`).

### Guards de integridad (importante para la UI)
- **Editar el SQL efectivo** (`up_sql_*`) de una versión **ya aplicada** en alguna BD → **`409`**:
  crea una versión nueva para corregir. (El `name`/`down_sql`/`reviewed` sí se pueden ajustar.)
- **Eliminar una versión con historial de aplicación** → **`409`**: protege la trazabilidad.
- **"Reducir" la versión de una BD** NO es borrar la versión del blueprint: es un **rollback**
  (§7-bis). Borrar la *definición* solo aplica a versiones nunca aplicadas.
- **Baseline de snapshot sin revisar** (`reviewed: false`) → `apply`/`apply-all` dan **`409`**
  hasta aprobarlo (ver arriba).

### 🎨 Interpretación visual sugerida
Layout **maestro-detalle** de dos columnas:
```
┌─ Versiones (blueprint: WhatsApp) ─┐  ┌─ Detalle 0005 ───────────────────────────┐
│  ○ 0001  Esquema inicial   ✅↩     │  │ up_sql (MySQL ref):                       │
│  ○ 0002  Add phone                 │  │   ALTER TABLE …                           │
│  ● 0003  Add status   ←ACTUAL (BD) │  │ ──────────────────────────────────────── │
│  ○ 0004  …            ⏳pendiente  │  │ Traducido · MySQL | PostgreSQL  [tabs]    │
│  ○ 0005  …            ⏳pendiente  │  │ down_sql (rollback):  ✅ confirmado / ➕   │
│   …                                │  │ [ Editar ]  [ Confirmar rollback ]  [🗑]   │
└────────────────────────────────────┘  └───────────────────────────────────────────┘
        ▲ select / lista                         ▲ panel de detalle (lazy: GET {version})
```
- El **select** se llena con `list` (solo `version` + `name`); el SQL se carga **al seleccionar**
  (GET detalle) — no lo traigas todo de golpe.
- Cruza con `status` de la BD para marcar: `●` actual, `⏳` pendiente, `✅` ya aplicada.
- Iconos por versión desde el summary: `↩`/`✅` = `has_rollback`, badge "override" si
  `has_mysql_override`/`has_postgresql_override`, `🔒 motor` si el detalle trae
  `has_non_portable`/`is_baseline` (baseline de snapshot, §6).
- En el detalle, los tabs **MySQL | PostgreSQL** usan el campo `translated` para mostrar qué SQL
  real correrá en cada motor.
- Botones **Editar/Eliminar** deshabilitados (con tooltip explicando el `409`) cuando la versión
  ya fue aplicada en alguna BD.

---

## 8. Tipos nuevos (referencia rápida)

```jsonc
// ReconcileResult
{ "server_id": 42, "databases": [ReconcileDatabaseItem], "users": [ReconcileUserItem] }

// ReconcileDatabaseItem
{ "name": "legacy_crm", "state": "managed|unmanaged|orphan",
  "managed_id": 7|null, "owner_id": 3|null, "status": "active|pending|error|archived|null" }

// ReconcileUserItem
{ "username": "app_ro", "host": "%"|null, "state": "managed|unmanaged|orphan", "managed_id": 4|null }

// StructureDump
{ "database": "legacy_crm", "source_engine": "mysql|mariadb|postgresql",
  "has_non_portable": true, "statements": [DumpStatement] }

// DumpStatement
{ "object_type": "table|view|materialized_view|routine|trigger|sequence|type|extension|index|event",
  "name": "clientes", "ddl": "CREATE TABLE …" }

// ManagedDatabaseOut gana el campo:
{ "origin": "provisioned|adopted", "...": "resto igual que antes" }

// ModelMigrationOut (detalle) gana:
{ "source_engine": "mysql|null", "is_baseline": true, "has_non_portable": true, "...": "…" }

// FromSnapshotOut
{ "model": DatabaseModelOut, "baseline_version": "0001", "source_engine": "mysql",
  "has_non_portable": true, "object_counts": {"table": 6, "view": 2}, "statements_captured": 9 }

// MigrationApplyOut (respuesta de .../migrations/apply, real o dry-run)
{ "managed_database_id": 11, "from_version": "0002", "to_version": "0005",
  "target_version": null, "applied_count": 3, "no_op": false, "failed": false,
  "quarantined": false, "dry_run": false, "pending_versions": ["0003","0004","0005"],
  "results": [MigrationResultOut] }

// MigrationRollbackOut (respuesta de .../migrations/rollback)
{ "managed_database_id": 11, "from_version": "0010", "to_version": "0007",
  "target_version": "0007", "reverted_count": 3, "failed": false, "quarantined": false,
  "no_op": false, "reverted_versions": ["0010","0009","0008"], "results": [MigrationResultOut] }
```

---

## 9. Matriz de errores

| Endpoint | Código | Cuándo | Qué mostrar |
|---|---|---|---|
| `reconcile`, `snapshot` | `502`/`504` | servidor inalcanzable/timeout | "No se pudo contactar el servidor." |
| `adopt` (db/user) | `404` | el objeto no existe en el motor | "Ese objeto ya no existe en el servidor." |
| `adopt` (db/user) | `409` | ya está en el inventario | "Ya estaba adoptado." |
| `adopt database` | `409` | owner de otro servidor | "El propietario pertenece a otro servidor." |
| `adopt database` | `422` | `model_id` inexistente | "El blueprint indicado no existe." |
| `from-snapshot` | `422` | BD vacía | "La base de datos no tiene objetos que fotografiar." |
| `from-snapshot` | `409` | slug/nombre duplicado | "Ya existe un blueprint con ese nombre o slug." |
| `migrations/apply` | `422` | cross-engine (baseline no portable) | "Ese blueprint no aplica a este motor." |
| todos | `401` | sin sesión | redirigir a login |

Formato de error: `{ "detail": { "msg": "...", "context": {...} } }` (el `context` solo en
`APP_ENV=development`; nunca contiene credenciales).

---

## 10. Recomendaciones de UX / diseño

1. **Dos planos, dos vistas, un puente.** Mantén separadas "En el servidor (en vivo)" y
   "Gestionadas por el gateway", y usa la pantalla de **reconcile** como el puente que une ambas
   con acciones (Adoptar / Archivar).
2. **Color = estado.** 🟢 managed · 🟡 unmanaged (adoptable) · 🔴 orphan. Reutiliza la misma
   semántica de color en toda la app.
3. **`origin` con badge.** Distingue 🛠 `provisioned` de 📥 `adopted` en los listados de BDs; ayuda
   a entender de un vistazo el origen de cada registro.
4. **Advierte lo no portable.** Cuando `has_non_portable=true`, muestra un candado con el motor
   (`🔒 mysql`) en el blueprint y deshabilita "Aplicar" hacia motores distintos (evita el `422`).
5. **Snapshot = borrador revisable.** Presenta el DDL agrupado y colapsable; deja claro que es una
   foto de **estructura**, **nunca de datos**, y que puede requerir revisión (p. ej. `DEFINER` ya
   viene saneado; rutinas con `;` internos pueden requerir ajuste antes de aplicar).
6. **Orden de adopción:** usuario → base de datos. Si el dueño no existe en el inventario, guía al
   admin a adoptarlo primero (el selector de propietario debe ofrecer "Adoptar usuario…").
7. **Confirma lo destructivo aguas abajo.** La adopción no es destructiva, pero habilita
   operaciones que sí lo son (`DROP` con `drop_remote=true`): mantén esas confirmaciones (repetir
   nombre) que ya exige el backend.

---

> **Resumen:** el Plan 09 no cambia el comportamiento de los listados existentes; añade el puente
> que faltaba entre el motor real y el inventario. Con `reconcile` ves la verdad combinada, con
> `adopt` traes objetos existentes bajo gestión sin recrearlos, y con `snapshot`/`from-snapshot`
> conviertes estructura legacy en plantillas versionables. Todo deliberado, auditado y no
> destructivo.
