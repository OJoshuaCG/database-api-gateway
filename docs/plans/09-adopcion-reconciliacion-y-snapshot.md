# 09 — Adopción de BDs/usuarios existentes, reconciliación (drift) y snapshot estructural

**Estado:** ✅ Implementado (F1–F4) + revisión de seguridad sin bloqueantes · **Depende de:** 01 ✅, 02 ✅ ·
**Esfuerzo:** alto · **Última revisión:** 2026-06-29

> **Verificación:** 329 tests en verde (SQLite + adapter mockeado, mismo enfoque que Plan 02).
> Migración Alembic `a7b8c9d0e1f2` aplica/revierte limpia. Revisión de seguridad (gateway-security):
> **0 bloqueantes**; doble defensa validate+quote en todo el dump, sin fuga de credenciales,
> `adopt` no ejecuta DDL y verifica existencia, guard cross-engine correcto. Endpoints nuevos:
> `GET /servers/{id}/reconcile`, `GET /servers/{id}/databases/{db}/snapshot`,
> `POST /managed-databases/adopt`, `POST /server-users/adopt`,
> `POST /database-models/from-snapshot`. Docs: `api-reference.md` (§6–9, tabla §16 endpoints 59–63)
> y guía frontend `api-reference-v3.md`.
>
> **Pendiente (deuda, NO bloqueante):** F2 modos 1/2 reutilizan `apply`/`stamp` existentes (sin
> endpoint nuevo); R1 (tratar el baseline de snapshot como DDL no confiable del motor, revisión
> obligatoria antes de `apply` masivo) y R2 (revalidación de IP anti-SSRF pre-conexión, deuda
> compartida con Plan 08 #4) quedan como mejoras futuras.

> Este plan extiende el inventario (Plan 01) y el módulo de migraciones de blueprints
> (Plan 02). No introduce un motor nuevo: **reutiliza** la introspección existente
> (`ServerAdapter.list_databases/list_users/list_tables/get_table_schema`), el
> aprovisionamiento (`ManagedDatabase`/`ServerUser`) y, sobre todo, la primitiva
> `MigrationRunner.stamp(...)` ya implementada (`app/services/db_admin/migrations.py:440`).

---

## 1. Contexto y problemática

### 1.1 Los dos planos (recordatorio)

El gateway opera sobre dos fuentes de datos distintas y **deliberadamente separadas**
(ver `docs/features/server-management.md`):

- **Plano de inventario** — la BD de metadatos del gateway. Contiene **solo lo que el
  gateway administra**: `ManagedDatabase`, `ServerUser`, con su dueño, blueprint, estado,
  password cifrado y auditoría.
- **Plano del motor destino** — el servidor real (MySQL/MariaDB/PostgreSQL). La verdad
  absoluta. El gateway no persiste nada de aquí; se conecta bajo demanda.

Hoy ya existen endpoints para cada plano:

| Pregunta que responde | Endpoint | Fuente |
|---|---|---|
| ¿Qué existe **realmente** en el servidor? | `GET /servers/{id}/databases` · `GET /servers/{id}/users` | Motor en vivo (adapter) |
| ¿Qué **administra** el gateway? | `GET /managed-databases` · `GET /server-users` | Inventario interno (ORM) |

### 1.2 La fricción detectada

Al dar de alta un servidor, el "resumen" (vista en vivo) muestra **todas** las BDs y
usuarios reales. Pero el listado de gestión solo muestra lo que se creó **a través del
gateway**. Esto se percibe como una contradicción ("las veo y luego no las veo"), cuando
en realidad son dos preguntas distintas. El problema real no es de comportamiento sino de
**falta de un puente** entre ambos planos:

1. No hay forma de **descubrir la divergencia** (drift) entre lo real y lo gestionado.
2. No hay forma de **adoptar** una BD/usuario que ya existe en el motor (creado por fuera
   del gateway, p. ej. una BD vacía aprovisionada manualmente, o legacy con datos) para
   que pase a ser gestionable sin recrearlo.
3. Una BD adoptada que ya tiene estructura **no puede engancharse al sistema de
   migraciones** de forma segura: aplicar un blueprint desde `v1` sobre una BD que ya
   tiene esas tablas **falla** (`CREATE TABLE` duplicado) o, si el delta trae `DROP`,
   **daña** datos. Las migraciones del Plan 02 son **deltas hacia adelante**, no un merge.

### 1.3 Por qué NO importar todo automáticamente al dar de alta el servidor

Se evaluó importar todas las BDs/usuarios al registrar un servidor y se **descarta**:

- **Rompe la integridad `owner_id`.** `ManagedDatabase.owner_id` es FK **NOT NULL** con
  `ondelete=RESTRICT`: cada BD gestionada debe tener exactamente un `ServerUser` dueño del
  mismo servidor. En un import masivo no hay dueño que asignar (y en MySQL/MariaDB el dueño
  es **lógico**, el motor no lo conoce).
- **Mete ruido.** BDs de sistema, de otras aplicaciones, legacy y basura contaminarían el
  inventario, que dejaría de significar "lo que administro deliberadamente".
- **Riesgo destructivo.** Una vez en inventario, una BD se puede `DROP` desde el gateway
  (`?drop_remote=true`). Adoptar todo a ciegas otorga ese poder sobre BDs que nunca se
  quiso gestionar.
- **No se pierde visibilidad.** La vista en vivo ya muestra todo; la adopción es
  **promoción deliberada**, no la única forma de ver.

**Decisión:** adopción **selectiva** (una por una, o multi-selección desde la vista de
drift), con dueño explícito.

---

## 2. Objetivo

1. **Reconciliar**: cruzar el plano en vivo con el inventario y clasificar cada objeto.
2. **Adoptar**: registrar en el inventario una BD/usuario que **ya existe** en el motor,
   sin ejecutar DDL de creación y preservando la integridad del modelo.
3. **Enganchar a migraciones** una BD adoptada mediante tres modos seguros, apoyados en
   `stamp` (nunca reproducir historia sobre estado no reconciliado).
4. **Snapshot**: capturar la estructura **completa** de una BD existente (no solo tablas)
   como un nuevo **blueprint baseline** versionable.

---

## 3. Principios (heredados del roadmap)

- Formato `ApiResponse[T]` + helpers; errores con `AppHttpException`.
- Toda operación contra un motor pasa por un `ServerAdapter` (nunca SQL crudo en el
  controller). El snapshot es una **nueva capacidad del adapter**, no lógica del controller.
- Identificadores SQL validados/quoteados (`identifiers.py`); valores parametrizados.
- Endpoints con I/O remoto se declaran `def` (no `async def`).
- Toda operación destructiva o de cara al motor queda **auditada** (Plan 06) y, si toca
  estructura, ofrece **`?dry_run=true`** antes de ejecutar.
- **Regla de oro de adopción:** el gateway nunca reproduce migraciones de construcción
  sobre una BD cuyo estado real no reconcilió.

---

## 4. Parte A — Reconciliación (drift view)

### 4.1 Endpoint

```http
GET /api/v1/servers/{server_id}/reconcile
```

Cruza, **en una sola llamada**, el plano en vivo y el inventario, y devuelve la
clasificación. No muta nada (read-only).

### 4.2 Clasificación

Por cada BD (y análogamente por cada usuario):

| Estado | Significado |
|---|---|
| `managed` | Existe en el motor **y** en el inventario (gestionada y coherente). |
| `unmanaged` | Existe en el motor pero **no** en el inventario → candidata a **adoptar**. |
| `orphan` | Existe en el inventario pero **no** en el motor (se borró por fuera, o `DROP` externo) → candidata a **archivar/limpiar**. |

> El cruce se hace por nombre de BD (único por servidor) y por `(username, host)` para
> usuarios. Las BDs/usuarios de sistema se excluyen igual que en `list_databases/list_users`.

### 4.3 Respuesta (forma)

```json
{
  "data": {
    "server_id": 1,
    "databases": [
      { "name": "whatsapp", "state": "managed",   "managed_id": 12, "owner_id": 3 },
      { "name": "legacy_crm", "state": "unmanaged" },
      { "name": "ventas_old", "state": "orphan",  "managed_id": 9 }
    ],
    "users": [
      { "username": "app_wa", "host": "%", "state": "managed", "managed_id": 7 },
      { "username": "root_legacy", "host": "localhost", "state": "unmanaged" }
    ]
  }
}
```

> **Cuidado con el drift estructural oculto:** `reconcile` cruza **existencia**, no
> estructura. Una BD `managed` puede haber divergido en su esquema respecto al blueprint
> (alguien creó tablas a mano). Detectar ese drift fino queda **fuera de alcance** de este
> plan (se apoyaría en el snapshot + diff; ver §9 Deuda).

---

## 5. Parte B — Adopción selectiva

### 5.1 Endpoints

```http
POST /api/v1/managed-databases/adopt
POST /api/v1/server-users/adopt
```

Diferencia esencial con `POST /managed-databases?provision=true`: **`adopt` NO ejecuta
`CREATE DATABASE`/`CREATE USER`** — solo registra metadata de algo que ya existe.

### 5.2 Body y reglas — `managed-databases/adopt`

```json
{
  "server_id": 1,
  "name": "legacy_crm",
  "owner_id": 3,
  "model_id": null
}
```

Reglas (validadas en el controller, fail-closed):

1. **Verificar existencia real**: consulta en vivo (`adapter.list_databases()`); si la BD
   no existe en el motor → **404**. (No se adopta lo inexistente.)
2. **NO ejecuta DDL de creación.** Estado inicial = **`active`** (no `pending`: ya existe).
3. **`owner_id` obligatorio**, debe ser un `ServerUser` **del mismo servidor** (mismo 409
   que el flujo de creación). Mantiene intacta la regla `owner_id` NOT NULL.
4. **Lee charset/collation reales** del motor para no inventarlos (MySQL: de
   `information_schema.SCHEMATA`; PG: encoding fijo, se ignora como en `create_database`).
5. **Idempotente**: si ya está en el inventario (nombre único por servidor) → **409**.
6. **Auditoría** con `touched_engine=false` (solo se leyó), acción `adopt`.

### 5.3 Body y reglas — `server-users/adopt`

```json
{ "server_id": 1, "username": "root_legacy", "host": "localhost" }
```

- Verifica existencia vía `adapter.list_users()`; 404 si no existe.
- **Sin password**: `has_password=false` hasta que se rote vía el flujo de cambio de
  password (`ALTER USER`). No se ejecuta `CREATE USER`.
- Unicidad `(server, username, host)` → 409 si ya está.

### 5.4 Multi-selección (comodidad sin import masivo)

Para "elegirlas según necesidad" sin hacerlo a mano una por una, la UI permite
multi-selección desde la vista de drift y dispara N llamadas a `/adopt`. Opcionalmente un
`POST /managed-databases/adopt-batch` que itere server-side (cada ítem exige su `owner_id`;
falla por-ítem sin abortar el lote, igual que `apply-all` del Plan 02). Sigue siendo
adopción **deliberada**.

---

## 6. Parte C — Enganchar una BD adoptada al sistema de migraciones (3 modos)

Una BD adoptada puede vincularse a un blueprint (`model_id`) de tres formas. **El admin
elige el modo explícitamente; el gateway nunca autodetecta la versión.** El default seguro
es el que **no ejecuta SQL** (stamp).

### Modo 1 — BD vacía → `apply` desde v1

- **Precondición:** la BD está **verificablemente vacía** (introspección: sin tablas,
  vistas, rutinas…). Si no está vacía → se rechaza este modo (**409**).
- **Acción:** adjuntar `model_id` y ejecutar el `apply` normal del Plan 02 desde la primera
  versión, **con `?dry_run=true` primero** + confirmación.
- **Caso de uso:** BDs creadas vacías por fuera del gateway que se quieren poblar con el
  blueprint.

### Modo 2 — BD que coincide con un blueprint conocido en versión X → `stamp`

- **Acción:** adjuntar `model_id` y llamar a `MigrationRunner.stamp(... version=X)`
  (`migrations.py:440`), que marca la BD en la versión X **sin ejecutar SQL**. Desde `X+1`
  en adelante solo corren deltas nuevos.
- **Quién aporta X:** el admin (no se infiere). El sistema valida que X exista en el
  blueprint (el propio `stamp` ya lo valida → 422 si no existe).
- **Caso de uso:** una BD que se construyó "igual" a un blueprint existente y se quiere
  unir a esa línea de versiones.

### Modo 3 — BD legacy/única → snapshot como nuevo baseline → `stamp`

- **Acción:** generar un **snapshot estructural** (Parte D) → guardarlo como **nuevo
  blueprint** cuya `v1` es el baseline → `stamp` la BD en ese baseline → versionar `v2,
  v3…` de ahí en adelante.
- **Caso de uso:** BD con estructura propia (vistas, triggers, procedures…) que se quiere
  tomar como punto de partida versionado.

> **Por qué `stamp` y no "ignorar lo que existe y agregar lo nuevo":** ese comportamiento
> solo funcionaría si **todas** las migraciones fueran idempotentes
> (`CREATE TABLE IF NOT EXISTS`…), lo cual es frágil de garantizar. `stamp` es la vía
> robusta y es el patrón estándar de Alembic (`alembic stamp`).

---

## 7. Parte D — Snapshot estructural COMPLETO por motor

### 7.1 Alcance: no solo tablas

El snapshot debe capturar **toda la estructura**, no únicamente tablas, o el baseline
quedaría incompleto (vistas rotas, lógica de negocio ausente al re-volcar). Por motor:

| Objeto | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Tablas (+ índices, FKs, charset/collation, columnas generadas, particiones) | ✅ | ✅ (+ herencia/partición) |
| Vistas | ✅ | ✅ |
| Vistas materializadas | — | ✅ |
| Funciones | ✅ | ✅ (varios lenguajes) |
| Stored procedures | ✅ | ✅ (`PROCEDURE`) |
| Triggers (+ su función en PG) | ✅ | ✅ |
| Events (scheduler) | ✅ | — |
| Sequences | MariaDB 10.3+ | ✅ |
| Tipos custom / ENUM / domains | (ENUM inline en columnas) | ✅ |
| Extensions | — | ✅ (`CREATE EXTENSION`) |
| Schemas (namespaces) | — | ✅ (más allá de `public`) |

> El conjunto de tipos de objeto debe ser un **registro extensible por adapter**, no una
> lista cerrada hardcodeada, para incorporar events/sequences/extensions sin reescribir el
> núcleo.

### 7.2 Fuente autoritativa del DDL (por motor)

No se reconstruye DDL a mano desde `information_schema`: cada motor emite su propio DDL
autoritativo.

- **MySQL/MariaDB:** `SHOW CREATE TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER|EVENT`.
- **PostgreSQL** (no tiene `SHOW CREATE`): se arma desde catálogos `pg_*` con
  `pg_get_viewdef()`, `pg_get_functiondef()`, `pg_get_triggerdef()`, `pg_get_indexdef()`,
  `pg_get_constraintdef()`, más consultas a `pg_class`/`pg_proc`/`pg_type`/`pg_extension`.

### 7.3 Contrato del adapter

Nueva capacidad abstracta en `ServerAdapter` (`base_adapter.py`), implementada por
`MySQLAdapter`, `MariaDBAdapter` y `PostgresAdapter`:

```python
@abstractmethod
def dump_structure(self, database: str) -> StructureDump: ...
```

`StructureDump` (nuevo DTO en `dtos.py`) devuelve una **lista ordenada de sentencias DDL**
agrupadas/etiquetadas por tipo de objeto:

```python
@dataclass
class DumpStatement:
    object_type: str   # "table" | "view" | "trigger" | "routine" | "sequence" | ...
    name: str
    ddl: str

@dataclass
class StructureDump:
    source_engine: EngineType        # motor de origen (clave para portabilidad)
    statements: list[DumpStatement]  # YA en orden de dependencia
    has_non_portable: bool           # True si incluye objetos procedurales
```

### 7.4 Los tres puntos duros (decisiones explícitas)

1. **Orden topológico de dependencias.** El DDL debe emitirse en orden o el `apply`
   fallará: `extensions/types → tablas → sequences → funciones → vistas (ordenadas entre
   sí) → triggers → events`. Casos borde: vistas materializadas y dependencias circulares
   entre vistas (resolver con orden de creación diferido o documentar la limitación).

2. **Los objetos procedurales atan el baseline a su motor.** `sqlglot` traduce DDL/DML
   estructural de forma razonable (es lo que ya hace el Plan 02 para `up_sql`), pero **NO**
   traduce código procedural (PL/pgSQL ↔ el dialecto `BEGIN…END` de MySQL). Por tanto:
   - La versión del blueprint generada por snapshot se **etiqueta con `source_engine`** y
     `has_non_portable`.
   - Si se intenta aplicar a un motor distinto: **rechazar** (422) o **degradar** al
     subconjunto estructural traducible, avisando explícitamente qué objetos se omiten.
   - Esto contrasta con un blueprint **escrito a mano**, que sí es portable vía el
     traductor. Es una diferencia de diseño que debe quedar documentada en
     `docs/features/model-migrations.md`.

3. **`DEFINER` / contexto de seguridad / `search_path`.** Vistas, triggers y rutinas en
   MySQL llevan `DEFINER=` y `SQL SECURITY`; las funciones PG llevan
   `SECURITY DEFINER/INVOKER`, owner y `search_path`. Capturarlos literal hace que el
   `apply` **falle en otro servidor** (el usuario `DEFINER` no existe ahí). Decisión:
   **normalizar/quitar `DEFINER`/owner** al capturar (o reescribir al pseudo-root del
   destino), como paso de sanitización **revisable**.

### 7.5 Snapshot = borrador revisable, solo estructura

- **Solo estructura, jamás datos** (coherente con la introspección actual, que nunca
  devuelve filas). Sanitización adicional: revisar literales en defaults/cuerpos de
  funciones (no suelen contener credenciales, pero se audita el riesgo).
- El DDL autogenerado es un **borrador**: se ofrece **preview/`dry_run`** y el admin puede
  **editar** antes de fijarlo como blueprint, justamente por el orden, el `DEFINER` y las
  cláusulas específicas de motor.
- **Riesgo de esconder drift:** si se hace snapshot de una BD y luego se `stamp` de varias
  BDs "casi iguales" al mismo baseline, se oculta divergencia real. Mitigación: snapshot
  **por-BD revisado**, o aceptar un baseline canónico y que `reconcile` marque las
  divergentes (diff estructural — ver §9 Deuda).

### 7.6 Endpoints del snapshot

```http
GET  /api/v1/servers/{server_id}/databases/{database}/snapshot     # preview (DDL ordenado, no persiste)
POST /api/v1/database-models/from-snapshot                         # crea blueprint baseline desde el dump
```

`from-snapshot` body (forma):

```json
{
  "server_id": 1,
  "database": "legacy_crm",
  "model_name": "CRM Legacy",
  "strip_definer": true,
  "edited_statements": null
}
```

- Si `edited_statements` viene, se usa en lugar del autogenerado (el admin revisó/editó).
- Crea el `DatabaseModel` + su primera `ModelMigration` (la `v1` baseline) con el DDL
  ordenado, `source_engine` y `has_non_portable` marcados.

---

## 8. Modelo de datos y cambios de código

### 8.1 Cambios en modelos / migración Alembic

- `ModelMigration` (o la entidad de versión del blueprint): añadir
  `source_engine: EngineType | None` y `is_baseline: bool` y `has_non_portable: bool`.
  Nueva migración Alembic de la BD del gateway.
- `ManagedDatabase`: reutiliza `status=active` para adoptadas; no requiere columna nueva
  (opcional `origin: "provisioned" | "adopted"` para trazabilidad — recomendado).

### 8.2 Adapters (`app/services/db_admin/`)

- `base_adapter.py`: `dump_structure(database) -> StructureDump` (abstracto).
- `mysql_adapter.py` / `mariadb`: implementación vía `SHOW CREATE *` + orden topológico.
- `postgres_adapter.py`: implementación vía `pg_get_*def()` + catálogos.
- `dtos.py`: `DumpStatement`, `StructureDump`.
- Helper de **sanitización de `DEFINER`/owner** (compartido o por adapter).

### 8.3 Controllers

- `server_controller.py`: `reconcile(server_id)` (cruza in-vivo vs inventario);
  `snapshot(server_id, database)` (preview).
- `managed_database_controller.py`: `adopt(...)`, opcional `adopt_batch(...)`.
- `server_user_controller.py`: `adopt(...)`.
- `model_migration_controller.py`: `create_from_snapshot(...)`.
- `managed_migration_controller.py`: orquestación de los 3 modos (verificar-vacía / stamp /
  snapshot+stamp) reutilizando `MigrationRunner.stamp`/`apply` ya existentes.

### 8.4 Rutas

- `servers.py`: `GET /{id}/reconcile`, `GET /{id}/databases/{db}/snapshot`.
- `managed_databases.py`: `POST /adopt`, `POST /adopt-batch`.
- `server_users.py`: `POST /adopt`.
- `model_migrations.py` (o `database_models.py`): `POST /from-snapshot`.

### 8.5 Schemas Pydantic

`AdoptDatabaseRequest`, `AdoptUserRequest`, `ReconcileResult`, `SnapshotResult`,
`FromSnapshotRequest`, más los `*Out` correspondientes (sin exponer credenciales).

---

## 9. Consideraciones críticas (leer antes de implementar)

1. **Integridad `owner_id` es sagrada.** `adopt` exige `owner_id` válido del mismo
   servidor. No relajar la FK ni inventar dueños.
2. **`adopt` ≠ `create`.** Nunca ejecutar `CREATE DATABASE/USER` en adopción; verificar
   existencia primero (404 si no existe). Estado inicial `active`.
3. **Nunca reproducir historia sobre estado no reconciliado.** Aplicar `v1..vN` sobre una
   BD con datos falla o daña: usar `stamp`. El modo `apply-desde-v1` exige BD
   **verificablemente vacía**.
4. **El snapshot ata el blueprint a su motor si incluye procedurales.** Etiquetar
   `source_engine`/`has_non_portable`; rechazar o degradar al aplicar cross-engine. No
   prometer portabilidad que `sqlglot` no puede cumplir.
5. **Orden topológico obligatorio** en el dump, o el re-volcado falla.
6. **Sanitizar `DEFINER`/owner/search_path**, o el `apply` falla en otro servidor.
7. **Snapshot solo estructura, jamás datos.** Y es un **borrador revisable** (dry-run +
   edición), no una verdad autogenerada.
8. **Drift fino (estructural) está fuera de alcance.** `reconcile` cruza existencia, no
   esquema. El diff estructural (snapshot vs estado real de una BD `managed`) es deuda
   futura.
9. **Auditoría y rate-limit** coherentes con Plan 02/06: `adopt`/`from-snapshot` auditados;
   operaciones que tocan estructura ofrecen `dry_run` y rate-limit (alineado con el
   `10/min` de operaciones destructivas).
10. **Idempotencia / concurrencia.** Unicidad nombre-por-servidor y `(server,user,host)`
    evita duplicados; el `stamp`/`apply` reusa el advisory lock por BD del Plan 02.

---

## 10. Seguridad (AppSec)

- El snapshot ejecuta **solo lectura** sobre el motor con la credencial pseudo-root; nunca
  expone esa credencial. Los nombres de BD/objeto pasan por la validación de
  identificadores (anti-inyección) antes de cualquier `SHOW CREATE`/consulta de catálogo.
- El DDL capturado puede contener **lógica de negocio sensible** (cuerpos de funciones):
  tratarlo como dato del cliente (no loguear cuerpos completos; auditar solo metadatos).
- `adopt` no incrementa privilegios en el motor (solo registra metadata); pero **habilita**
  operaciones destructivas futuras (`DROP`) sobre el objeto → tratar la adopción como una
  acción privilegiada, auditada, de admin.
- Revisar el snapshot generado con el subagente `gateway-security` antes de exponer
  `from-snapshot` (riesgo de `DEFINER`/`SECURITY DEFINER` que escale privilegios al
  re-aplicar).

---

## 11. Plan de implementación por fases

| Fase | Alcance | Entregable |
|---|---|---|
| **F1 — Reconcile + Adopt** | `GET /reconcile`, `POST /adopt` (DB y user), schemas, auditoría, tests | Puente in-vivo↔inventario funcional, sin tocar migraciones |
| **F2 — Modos 1 y 2** | Vincular BD adoptada: `apply`-si-vacía (con verificación de vacío) y `stamp`-en-versión-X | Reutiliza `MigrationRunner.apply/stamp` existentes |
| **F3 — Snapshot estructural** | `dump_structure` por adapter (tablas+vistas+triggers+rutinas+…), orden topológico, sanitización DEFINER, DTOs | `GET /snapshot` (preview) |
| **F4 — Modo 3 (snapshot→baseline)** | `POST /database-models/from-snapshot`, columnas `source_engine`/`is_baseline`/`has_non_portable`, `stamp` en baseline | Blueprint baseline versionable desde BD legacy |
| **F5 — Comodidad y endurecimiento** | `adopt-batch`, multi-selección, gates cross-engine, docs en `docs/features/`, e2e contra motores reales | Feature completo y verificado |

---

## 12. Verificación

### 12.1 Tests unitarios (SQLite + adapter mockeado)

- `reconcile`: clasifica managed/unmanaged/orphan correctamente con sets en vivo simulados.
- `adopt`: 404 si no existe en motor, 409 si ya en inventario, 409 si owner de otro
  servidor, `active` como estado inicial, sin DDL de creación, auditoría `touched_engine=false`.
- Modo 1: rechaza (409) si la BD no está vacía; aplica desde v1 si vacía.
- Modo 2: `stamp` valida versión (422 si no existe en blueprint).
- Snapshot: orden topológico estable; `has_non_portable=true` cuando hay rutinas;
  `DEFINER` removido tras sanitización.

### 12.2 Checklist contra motores reales (gate de despliegue — requiere Docker)

> Igual que el Plan 02, el sandbox no tiene Docker/MySQL/PG: el DDL real debe verificarse
> manualmente antes de producción. Ampliar `scripts/verify_migrations_e2e.py`.

1. **MySQL 8 — `SHOW CREATE` completo:** tabla con FK + columna generada + partición;
   vista; trigger; procedure y function con `;` internos; event. Confirmar que el dump
   re-aplicado en una BD vacía reproduce la estructura.
2. **MariaDB — sequences** (10.3+) capturadas y recreadas.
3. **PostgreSQL — `pg_get_*def`:** vista materializada, función PL/pgSQL, trigger + su
   función, ENUM/domain, extension, sequence con ownership. Verificar orden topológico
   (types/extensions antes que tablas; triggers al final).
4. **Orden de dependencias:** una vista que referencia otra vista; FK entre tablas;
   trigger que llama a una función. El re-volcado no debe fallar por orden.
5. **`DEFINER`/owner:** snapshot de objetos con `DEFINER` de un usuario inexistente en el
   destino → tras sanitización, el `apply` no falla.
6. **Cross-engine guard:** intentar aplicar un baseline `source_engine=mysql` con
   procedurales sobre PostgreSQL → 422 (o degradado con aviso explícito).
7. **Adopción + `stamp` + forward:** adoptar BD legacy, snapshot→baseline, `stamp`, luego
   crear `v2` y `apply` → solo corre `v2`.
8. **Reconcile tras `DROP` externo:** borrar una BD por fuera del gateway → aparece como
   `orphan`.

---

## 13. Fuera de alcance (deuda futura)

- **Diff estructural fino** entre el snapshot de una BD `managed` y su blueprint (drift de
  esquema, no solo de existencia). Base para una alerta de "esta BD divergió del modelo".
- **Snapshot de datos** (seeds) — este plan es estructura pura.
- **Reconciliación de grants/permisos** (qué privilegios reales tiene cada usuario vs lo
  que el gateway cree) — se cruza con el Plan 07.
- **Portabilidad real cross-engine de procedurales** (transpilación PL/pgSQL ↔ MySQL) — no
  resoluble con `sqlglot`; requeriría un transpilador dedicado, probablemente nunca.
- **Fan-out asíncrono** de `adopt-batch` con background jobs (alineado con la deuda de
  `apply-all` del Plan 02 diferida al Plan 06).

---

**Relacionado:** Plan 01 (inventario), Plan 02 (migraciones de blueprints), Plan 07
(permisos granulares — para futura reconciliación de grants), `docs/features/server-management.md`
y `docs/features/database-management.md` (los dos planos).
