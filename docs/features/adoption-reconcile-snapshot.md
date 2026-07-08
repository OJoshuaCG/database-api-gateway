# Adopción, reconciliación y snapshot (Plan 09)

Cierra la brecha entre **lo que existe en el motor real** y **lo que gestiona el gateway**: permite
ver la divergencia (drift), **adoptar** BDs/usuarios preexistentes al inventario sin recrearlos, y
**fotografiar** la estructura de una BD para fijarla como **blueprint baseline** versionable. Se
apoya en la [gestión de servidores e introspección](server-management.md), la
[gestión de usuarios/BDs](database-management.md) y las
[migraciones de blueprints](model-migrations.md).

> Diseño e historia: [`docs/plans/09-adopcion-reconciliacion-y-snapshot.md`](../plans/09-adopcion-reconciliacion-y-snapshot.md).
> Guía detallada para frontend (escenarios, flujos, ejemplos y mockups): [`api-reference-v3.md`](../api-reference-v3.md).

## Concepto: los dos planos

- **Plano en vivo (motor real):** la verdad absoluta — todas las BDs/usuarios que existen.
  Consultable con `GET /servers/{id}/databases` y `/users` (introspección).
- **Plano de inventario (gateway):** solo lo que el gateway **administra**, con su metadata
  (dueño, blueprint, estado, auditoría).

El Plan 09 es el **puente** entre ambos. No cambia el comportamiento de los listados existentes;
añade descubrimiento y promoción **deliberada** (nunca import masivo automático, que rompería la
regla de propietario y habilitaría DROP sobre objetos no gestionados).

## Endpoints (API v1)

> Todos requieren sesión de administrador. 🔌 = leen/tocan el motor destino.

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/v1/servers/{id}/reconcile` 🔌 | Cruza en vivo vs inventario; clasifica cada BD/usuario: `managed` · `unmanaged` (adoptable) · `orphan` (borrado por fuera). Read-only. |
| `POST` | `/api/v1/managed-databases/adopt` 🔌 | Adopta una BD que **ya existe** (sin `CREATE DATABASE`; estado `active`, `origin=adopted`). Exige `owner_id` del mismo servidor. `model_version` opcional (requiere `model_id`) → hace `stamp` de esa versión al adoptar. `404` si no existe; `409` si ya está; `422` si `model_version` no existe en el blueprint. |
| `POST` | `/api/v1/server-users/adopt` 🔌 | Adopta un usuario existente (sin `CREATE USER`, sin password → `has_password=false`). |
| `GET` | `/api/v1/servers/{id}/databases/{db}/snapshot` 🔌 | **Snapshot estructural** (preview): tablas, vistas, rutinas, triggers, y según motor secuencias/tipos/extensiones/events. **Solo estructura, nunca filas**; `DEFINER` saneado. Con `?include_data_stats=true` agrega `table_stats` (estimación de filas y si la tabla tiene PK) para informar la selección de datos-semilla. |
| `POST` | `/api/v1/database-models/from-snapshot` 🔌 | Crea un blueprint desde el snapshot de una BD. **Snapshot selectivo**: elige qué objetos migrar, cómo versionarlos (`single`/`by_class`/`manual`) y qué datos-semilla incluir. Rate limit 10/min. Ver sección siguiente. |

## Snapshot → blueprint baseline (y el gate `reviewed`)

`dump_structure` captura el DDL autoritativo por motor (`SHOW CREATE *` en MySQL/MariaDB;
`pg_get_*def()` + reflexión en PostgreSQL) en **orden de dependencia**. Si el snapshot incluye
objetos **procedurales** (rutinas/triggers/events), el baseline queda **atado a su motor de origen**
(`source_engine`, `has_non_portable=true`) porque ese código no es traducible cross-engine — aplicarlo
a otro motor da `422` (cross-engine guard).

**Seguridad (R1):** el baseline es DDL capturado de un motor potencialmente no confiable, así que
nace **`reviewed=false`** y **no se puede aplicar** (`apply`/`apply-all` → `409`) hasta que un admin
lo revise y apruebe con `PATCH /database-models/{id}/migrations/{version}` `{"reviewed": true}`.

## Snapshot selectivo: qué migrar, cómo versionar y datos-semilla

`from-snapshot` no es "todo o nada". Por defecto (`layout="single"`, sin datos) reproduce el
baseline estructural histórico en una sola versión `0001`. Además permite:

### 1. Elegir qué objetos migrar

- `include_object_types` / `exclude_object_types`: filtra por clase
  (`table`, `view`, `routine`, `trigger`, `sequence`, `type`, `extension`, `index`, `event`).
  Ej.: `exclude_object_types=["routine","trigger"]` → baseline **portable** (sin código atado a motor).
- `include_objects` / `exclude_objects`: filtra objetos concretos por `{object_type, name}`.

### 2. Cómo versionar (`layout`)

- **`single`**: todo el esquema seleccionado en una versión (default, histórico).
- **`by_class`**: una versión por clase de objeto, en orden de dependencia:
  `tablas(+índices) → vistas → vistas materializadas → rutinas → triggers → events`. Aísla lo
  no-portable de lo portable: un consumidor puede aplicar solo hasta las vistas y detenerse.
- **`manual`**: el usuario agrupa objetos en `manual_layout` (buckets **ordenados**; el gateway
  asigna los números de versión). Cada bucket es de **esquema XOR de datos**. Se valida
  **topológicamente**: si una tabla queda en una versión anterior a otra de la que depende por FK,
  o una vista/rutina antes de todas las tablas, la respuesta es `422` con `context.violations`
  (lista completa de problemas, cada uno con `reason` accionable).

Las **tablas** se emiten siempre en **orden topológico por FK** (corrige el orden alfabético del
dump, que podía romper el re-apply con FKs cruzadas). Los **datos van siempre en la(s) última(s)
versión(es)**, una por tabla.

### 3. Datos-semilla (catálogos/tipos) — opt-in, NO es ETL

`data_tables=[{table, mode}]` incluye los **datos** de tablas de catálogo como `INSERT`
**idempotente** (`mode`: `upsert` → `ON DUPLICATE KEY UPDATE`/`ON CONFLICT DO UPDATE`;
`insert_ignore` → `INSERT IGNORE`/`DO NOTHING`). Cada tabla se sirve en su propia versión
`kind="data"`, con **rollback por PK** (`DELETE ... WHERE pk IN (...)`) autogenerado.

- **Requiere PK** (para el upsert y el rollback); sin PK la tabla se omite.
- **Guardrails** (env `SNAPSHOT_DATA_*`, con techos duros en código): máximo de filas y bytes por
  tabla, máximo de tablas, tamaño de lote. `on_oversize`: `skip` (omitir y reportar en
  `skipped_tables`) o `error` (`422`). La estructura de la tabla sembrada debe estar incluida.
- **`confirm_data_rollback`**: `true` confirma el `DELETE` (rollback aplicable); `false` (default)
  lo deja solo como `down_sql_suggested` (fail-closed, coherente con el rollback del resto).
- Una versión `kind="data"` queda **atada a `source_engine`** (la sintaxis upsert difiere por
  motor): aplicarla a otro motor da `422` (cross-engine guard). Su SQL **no se traduce** con sqlglot.
- **Tipos de valor**: se soportan NULL, enteros/decimales/floats finitos, booleanos, cadenas,
  fechas/horas, binarios (hex) y JSON. Un valor de tipo no soportado (p. ej. UUID/INET/INTERVAL de
  PostgreSQL, o un byte nulo) **omite la tabla** (fail-closed, `reason=unsupported_type`); los arrays
  de PG se serializan como JSON y probablemente fallen en el `apply` (→ cuarentena, nunca datos
  corruptos). La lectura es en **streaming** con tope de filas/bytes para acotar la memoria.

> **Alcance de la validación del layout manual:** es **sólida a nivel de tabla** (FK, trigger→tabla,
> índice→tabla y aislamiento de datos). Las dependencias entre vistas/rutinas (que no se parsean)
> se cubren de forma **conservadora** ("después de todas las tablas"); un caso raro vista→vista podría
> pasar la validación y fallar en el `apply` → cuarentena. El gate `reviewed` (revisión humana del
> SQL antes de aplicar) es el backstop.

> Toda migración generada por snapshot (estructura o datos) nace `reviewed=false` (R1) y debe
> aprobarse antes de aplicar.

La respuesta (`FromSnapshotOut`) resume `versions[]` (por versión: `kind`, `object_counts`,
`has_non_portable`), `skipped_tables[]`, `data_tables_captured` y `total_versions`. **Nunca**
devuelve el SQL ni los valores de las filas.

## Los 3 modos para vincular una BD adoptada a migraciones

1. **BD vacía** → `apply` desde `0001` (con `dry_run` primero).
2. **BD que ya coincide con un blueprint en la versión X** → adopta indicando
   `model_version=X` (ver abajo) **o** `stamp` en X tras adoptar; sigue con `apply` de ahí.
3. **BD legacy/única** → `from-snapshot` (baseline) → aprobar (`reviewed`) → `stamp`/`apply`.

### `model_version` al adoptar (stamp-on-adopt)

`POST /managed-databases/adopt` acepta un `model_version` opcional (requiere `model_id`):
declara en qué versión del blueprint **ya se encuentra** la BD. Si se indica, el gateway
hace el `stamp` de esa versión en el motor (sin ejecutar DDL) en la misma operación, de modo
que un `apply` posterior no reintente crear objetos que ya existen. La versión se valida
contra el blueprint **antes** de registrar la BD (`422` si no existe → la BD no queda
adoptada a medias). Omitir `model_version` = la BD llega "en ceros" (modo 1).

> Por esto **no** se inyecta `IF NOT EXISTS` en el DDL adoptado: el mismo baseline debe poder
> aplicarse tal cual a BDs nuevas vacías. El conflicto "la tabla ya existe" se resuelve
> declarando la versión de partida (stamp), no volviendo el DDL idempotente (que enmascararía
> drift si la estructura viva difiere del baseline).

## Seguridad

- Todos los endpoints exigen `AdminDep`; los identificadores pasan por validación anti-inyección
  antes de cualquier `SHOW CREATE`/consulta de catálogo; el `DEFINER` se sanea en el dump.
- La adopción no ejecuta DDL de creación y audita con `touched_engine=false`.
- Anti-SSRF: el host se revalida **al conectar** (no solo al registrar) — ver [conexión remota](remote-connections.md).

## Verificación

- Unitarios (SQLite + adapter mockeado): `tests/test_api_plan09_adopt_snapshot.py`;
  snapshot selectivo: `tests/test_snapshot_layout.py` (orden topológico, `build_versions`,
  `validate_manual_layout`, render de literales/upsert/rollback por PK, y regresión de
  "no traducir datos") y `tests/test_api_snapshot_selective.py` (filtros, `by_class`, manual con
  violaciones, datos-semilla, guard cross-engine de datos).
- e2e contra motores reales (MySQL 8 / MariaDB 11 / PostgreSQL 16): `scripts/verify_migrations_e2e.py`
  — `run_plan09` + `run_snapshot_selective` (split `by_class`, orden FK topológico, upsert
  idempotente y rollback por PK contra el motor real).

---

**Siguiente:** [Migraciones de blueprints](model-migrations.md) ·
[Gestión de usuarios/BDs](database-management.md)
