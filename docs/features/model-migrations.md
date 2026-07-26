# Migraciones de Blueprints (versionado de esquema)

Permite que un **blueprint** (`DatabaseModel`, p. ej. "Whatsapp", "SMS") tenga un
esquema **versionado** —una secuencia de deltas SQL— y aplicarlo, revertirlo o marcarlo
sobre las **N bases de datos gestionadas** que lo replican, sabiendo en todo momento qué
versión tiene cada una. Se apoya en la [gestión de servidores e introspección](server-management.md),
la [capa de conexión remota](remote-connections.md) y la [autenticación](authentication.md).

> Para el diseño interno (Alembic embebido, advisory locks, AUTOCOMMIT, decisiones de
> arquitectura y la auditoría remediada) ver [`docs/plans/02-migraciones-de-modelos.md`](../plans/02-migraciones-de-modelos.md).

## Concepto

- **Blueprint** (`DatabaseModel`, tabla `database_models`): plantilla lógica versionada.
  Su `current_version` refleja la última migración subida.
- **Migración** (`ModelMigration`, tabla `model_migrations`): un **delta SQL** con una
  `version` (solo dígitos; se ordena **numéricamente**, no lexicográfico). La primera
  puede ser el esquema completo; las siguientes son cambios incrementales.
- **Versión real de cada BD**: la mantiene **Alembic dentro de la propia BD gestionada**
  en una tabla `_gw_v_{slug}`. Es la fuente de verdad.
- **Historial** (`database_migration_history` en el gateway): espejo de auditoría
  (cuándo, resultado, duración, error) para consultar sin abrir N conexiones.

## Anatomía de una migración

| Campo | Obligatorio | Notas |
|---|---|---|
| `version` | **no** | Solo dígitos, 4–10. **Si se omite, el gateway autoasigna la siguiente secuencial** (`max+1`); pásala solo para fijarla. Orden numérico |
| `name` | sí | Descripción corta (≤200) |
| `up_sql` | sí | Delta SQL base, **estilo MySQL de referencia**. ≤256 KB |
| `up_sql_mysql` | no | Override manual para MySQL/MariaDB (si la auto-traducción no basta) |
| `up_sql_postgresql` | no | Override manual para PostgreSQL |
| `down_sql` | no | Rollback **confirmado**. Sin él, `rollback` responde 409 |
| `down_sql_suggested` | (auto) | Rollback sugerido por el gateway para ops aditivas; revisar y confirmar vía `PATCH` |
| `checksum` | (auto) | SHA256 de todo el SQL + versión; detecta alteración antes de aplicar. **No** incluye `kind` (para no invalidar checksums existentes) |
| `kind` | (auto) | `schema` (DDL, default) o `data` (datos-semilla upsert de un snapshot). Una migración `data` está **atada a `source_engine`** (la sintaxis upsert difiere por motor) y **no se traduce** cross-engine |
| `reviewed` | (auto) | `true` para migraciones escritas a mano; toda migración generada por **snapshot** (Plan 09) nace `false` y **no se aplica** hasta aprobarla (`PATCH reviewed=true`) |
| `source_engine` / `is_baseline` / `has_non_portable` | (auto) | Metadatos de una migración generada por snapshot (motor de origen; si trae objetos procedurales no portables). El snapshot puede dividirse en varias versiones — ver [adopción/snapshot](adoption-reconcile-snapshot.md) |

El gateway **auto-traduce** `up_sql` de MySQL a PostgreSQL con `sqlglot`; el campo
calculado `translated` muestra el SQL efectivo por motor. Los overrides solo se necesitan
cuando la traducción no es fiable (ver [matriz de equivalencia](#matriz-de-equivalencia-ddl)).

## Flujo de la feature (MVC)

```
routes/v1/model_migrations.py     →  controllers/model_migration_controller.py   (CRUD, BD gateway)
routes/v1/managed_databases.py    →  controllers/managed_migration_controller.py →  services/db_admin/migrations.py
   (/migrations/*)                                                                   (MigrationRunner → motor destino)
```

- El CRUD del blueprint **no toca ningún motor** (solo la BD de metadatos del gateway).
- `apply`/`rollback`/`stamp` sí tocan el motor destino vía `MigrationRunner` (Alembic
  embebido) bajo un advisory lock por BD.

## Endpoints

> Todos requieren sesión de administrador (`AdminDep`).

### Migraciones del blueprint (solo BD del gateway)

```http
GET    /api/v1/database-models/{id}/migrations            # lista paginada (?page=&size=)
POST   /api/v1/database-models/{id}/migrations            # crea una versión
GET    /api/v1/database-models/{id}/migrations/{version}  # detalle (con translated + sugerencia)
PATCH  /api/v1/database-models/{id}/migrations/{version}  # confirma down_sql / añade overrides / corrige up_sql (si no aplicada)
DELETE /api/v1/database-models/{id}/migrations/{version}  # solo la ÚLTIMA versión y sin historial de aplicación
```

### Aplicación sobre una BD gestionada (tocan el motor)

```http
GET    /api/v1/managed-databases/{id}/migrations/status     # versión actual vs. pendientes
POST   /api/v1/managed-databases/{id}/migrations/apply      # ?version= ?force= ?dry_run= — UNA llamada, secuencial (10/min)
POST   /api/v1/managed-databases/{id}/migrations/rollback   # ?confirm_version= (OBLIG.) ?target_version= — secuencial (10/min)
POST   /api/v1/managed-databases/{id}/migrations/stamp      # ?version=  (marca sin ejecutar) (10/min)
GET    /api/v1/managed-databases/{id}/migrations/history    # historial paginado
```

### Aplicación masiva

```http
POST   /api/v1/database-models/{id}/migrations/apply-all    # ?max_databases=(1..100) ?force= ?dry_run= (3/min)
```

`apply-all` es **síncrono y acotado** (`max_databases` ≤100); continúa con las demás BDs
aunque una falle. El fan-out asíncrono real es del Plan 06.

## Flujo de trabajo (ejemplos)

### 1. Crear el blueprint y subir migraciones

```bash
# Crear la primera migración (esquema inicial, estilo MySQL).
# "version" es OPCIONAL: si se omite, el gateway autoasigna la siguiente (0001, 0002…).
curl -X POST .../api/v1/database-models/1/migrations -b cookie.txt \
  -H 'Content-Type: application/json' -d '{
    "name": "Esquema inicial",
    "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)"
  }'
```

La respuesta incluye la traducción por motor y un rollback **sugerido** (no confirmado):

```json
{
  "success": true,
  "message": "Migración creada.",
  "data": {
    "version": "0001", "name": "Esquema inicial",
    "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
    "down_sql": null,
    "down_sql_suggested": "DROP TABLE IF EXISTS orders;",
    "translated": {
      "mysql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
      "postgresql": "CREATE TABLE orders (id INT GENERATED BY DEFAULT AS IDENTITY NOT NULL PRIMARY KEY, total INT)"
    },
    "checksum": "…"
  }
}
```

### 2. Confirmar el rollback sugerido

```bash
curl -X PATCH .../api/v1/database-models/1/migrations/0001 -b cookie.txt \
  -H 'Content-Type: application/json' -d '{"down_sql": "DROP TABLE IF EXISTS orders"}'
```

### 3. Ver estado y previsualizar (dry-run)

```bash
curl .../api/v1/managed-databases/5/migrations/status -b cookie.txt
# → { "current_version": null, "pending_count": 1, "pending_versions": ["0001"], ... }

curl -X POST '.../api/v1/managed-databases/5/migrations/apply?dry_run=true' -b cookie.txt
# → { "dry_run": true, "from_version": null, "to_version": "0001",
#     "pending_versions": ["0001"], "no_op": false }
```

### 4. Aplicar (una sola llamada, secuencial)

```bash
curl -X POST .../api/v1/managed-databases/5/migrations/apply -b cookie.txt
# → { "from_version": null, "to_version": "0002", "target_version": null,
#     "applied_count": 2, "failed": false, "no_op": false,
#     "pending_versions": ["0001","0002"], "results": [ … ] }
```

`?version=0003` aplica en **una sola llamada** todas las pendientes hasta `0003` (inclusive), en
orden. Forward-only; `422` si la versión no existe; `409` si hay un baseline de snapshot sin
revisar (R1).

### 5. Historial y rollback (target-based, secuencial)

```bash
curl .../api/v1/managed-databases/5/migrations/history -b cookie.txt
# → [{ "version": "0001", "status": "applied", "applied_at": "…", "execution_ms": 42 }]

# Rollback DESTRUCTIVO: confirm_version = versión actual; target_version = destino (anterior).
# En UNA llamada revierte secuencialmente todas las necesarias (p. ej. 0010 → 0007).
curl -X POST '.../api/v1/managed-databases/5/migrations/rollback?confirm_version=0010&target_version=0007' -b cookie.txt
# → { "from_version": "0010", "to_version": "0007", "reverted_count": 3,
#     "reverted_versions": ["0010","0009","0008"], "failed": false }
```

Sin `target_version` revierte solo la última. Sin `confirm_version` (o si no coincide con la
versión actual) → **422**. Si **alguna** versión del camino no tiene `down_sql` confirmado → **409**.

### 6. Marcar una BD pre-existente (stamp)

Si una BD ya tiene el esquema de una versión pero el gateway no lo sabe, `stamp` registra
la versión **sin ejecutar SQL**:

```bash
curl -X POST '.../api/v1/managed-databases/5/migrations/stamp?version=0003' -b cookie.txt
```

`stamp` es una **afirmación explícita** del admin ("esta BD está en la versión X"): además
de marcar la versión, **saca la BD de cuarentena** (`status=error → active`) si un `apply`
previo la dejó ahí. Es la vía correcta para reconciliar una BD adoptada cuyo esquema ya
coincide con el baseline (evita reintentar un `CREATE TABLE` de algo que ya existe).

**`stamp` bloquea (409) si detecta un checkpoint de aplicación PARCIAL** para esa BD (ver
"Checkpoint de sentencia" más abajo): stampear a ciegas por encima de un fallo a mitad de
camino dejaría un `rollback` posterior ejecutando el `down_sql` completo contra una BD que
solo tiene una fracción de los cambios físicos. `?force=true` es la vía explícita para
"ya reconcilié el estado físico a mano" — descarta el checkpoint y procede.

## Matriz de equivalencia DDL

El gateway auto-traduce `up_sql` (MySQL → PostgreSQL) para DDL común. **Escribe un
`up_sql_postgresql` manual** cuando uses construcciones que `sqlglot` no traduce de forma
fiable:

| Construcción MySQL | Auto-traducción | Acción |
|---|---|---|
| `INT AUTO_INCREMENT` | → `IDENTITY` / `SERIAL` | Automática ✅ |
| backticks, `DATETIME` | → comillas, `TIMESTAMP` | Automática ✅ |
| `ENUM('a','b')` inline | no fiable | **Override PG** (crear `TYPE … AS ENUM`) |
| `ON UPDATE CURRENT_TIMESTAMP` | se pierde (en PG es trigger) | **Override PG** |
| `UNSIGNED` / `ZEROFILL` | se descartan | **Override PG** si importan |
| `ALTER … MODIFY … AUTO_INCREMENT` | sin equivalente | **Override PG** |
| Rutinas `BEGIN…END` con `;` internos | el splitter las parte mal | Subir como un solo delta / **override** |

## Integridad, cuarentena y recuperación

- **Checksum**: antes de aplicar, el gateway re-valida el `checksum` (cubre SQL + versión).
  Si la fila fue alterada directamente en la BD del gateway → **409** (no aplica SQL no
  verificado).
- **Editar `up_sql` (corrección)**: vía `PATCH` puedes corregir `up_sql` (y overrides)
  **mientras la migración no se haya aplicado EXITOSAMENTE en ninguna BD**. Un intento que
  solo *falló* no congela el SQL (ninguna BD depende de él) → sí se puede corregir. Si ya
  hubo una aplicación exitosa → **409**: usa **fix-forward** (nueva migración correctiva).
  Al cambiar `up_sql` se regenera el `down_sql_suggested`; si existen overrides por-motor
  debes **reenviarlos corregidos o limpiarlos (null)** en el mismo `PATCH` (409 si no), para
  que no quede SQL viejo aplicándose en silencio.
- **Eliminar una versión**: `DELETE` solo permite borrar la **última** versión del blueprint
  (la punta) y **sin historial** de aplicación (409 en otro caso). Borrar una intermedia
  dejaría un hueco del que podría depender una versión posterior.
- **Cuarentena (fallo parcial)**: como el DDL no es transaccional en MySQL/MariaDB (y el
  runner corre en AUTOCOMMIT), una migración multi-sentencia que falla a mitad puede dejar
  estado parcial. El gateway marca la BD con `status=error` + nota; el siguiente `apply`
  responde **409** hasta que inspecciones y reintentes con **`?force=true`**. Un `apply`
  exitoso —o un `stamp`— limpia la cuarentena.
- **Checkpoint de sentencia (resume automático)**: cuando una migración de N sentencias
  falla a mitad (p. ej. 3 de 50 ya commitearon), el gateway graba un checkpoint por
  sentencia en su propia BD de metadatos (`migration_statement_progress`, tabla
  **efímera**: una fila existe solo mientras hay progreso incompleto). El próximo
  `apply` sobre esa BD **retoma automáticamente desde la sentencia 4**, sin re-ejecutar
  las 3 que ya corrieron ni requerir inspección manual — esto es lo que reemplaza el
  flujo manual anterior de "inspeccionar y stampear a mano".
  - **Fail-closed por diseño** (`app/services/db_admin/migration_progress.py`): el
    checkpoint solo se activa (`is_resumable`) para SQL de esquema "plano". Se
    **deshabilita** (todo-o-nada, como antes) si la migración es `kind='data'`, incluye
    objetos no portables (`has_non_portable`), o contiene sentencias que dependen de
    **estado de sesión** (`SET`, `PREPARE`, `LOCK TABLES`, `USE`, transacciones
    explícitas) — un reintento abre una conexión NUEVA y ese estado (p. ej.
    `SET FOREIGN_KEY_CHECKS=0` de la sentencia 1) no sobrevive.
  - El checkpoint queda **ligado al `checksum`** de la migración: si el admin edita el
    `up_sql` mientras hay un checkpoint incompleto, el `PATCH` responde **409** (no se
    permite editar con un resume en curso — el índice del checkpoint dejaría de
    corresponder al SQL nuevo).
  - **Límite irreducible, no resuelto por el checkpoint**: si la sentencia que falló
    está genuinamente rota (error de sintaxis, tipo, o un `ALTER` multi-cláusula que
    MySQL/MariaDB aplica parcialmente — p. ej. `ADD COLUMN a, ADD COLUMN b` donde `a`
    commitea y `b` falla), el resume solo evita re-tropezar con las sentencias previas
    ya aplicadas; la sentencia rota vuelve a fallar igual y requiere corrección manual
    (nueva migración fix-forward o reconciliación directa). El checkpoint reduce
    drásticamente la intervención manual, no la elimina.
  - **Ventana de doble escritura**: el commit del DDL (BD destino) y el commit del
    checkpoint (BD del gateway) son dos datastores independientes sin transacción
    compartida. Si el proceso muere justo entre ambos, el checkpoint puede quedar un
    statement atrasado — el resume re-ejecuta a lo sumo UNA sentencia ya aplicada
    (falla ruidosa tipo "ya existe"), nunca salta una sentencia que no corrió (el
    checkpoint se graba SIEMPRE después del `op.execute`, nunca antes).
- **Recomendación**: escribe migraciones **idempotentes** (`CREATE TABLE IF NOT EXISTS`,
  `ADD COLUMN IF NOT EXISTS`) para que un reintento sea seguro.

## Aplicación parcial: el rollback ya no opera a ciegas

### El problema

Cuando el `apply` de la versión N muere en la sentencia k de N, quedan **dos verdades en
desacuerdo**:

- Alembic escribe la versión en `_gw_v_{slug}` recién al **terminar** el `upgrade()`, así
  que el ledger sigue diciendo *"estoy en N-1"*;
- la BD tiene, físicamente, las primeras k sentencias de N **ya commiteadas** (AUTOCOMMIT:
  el DDL de MySQL/MariaDB no es transaccional).

`rollback` no veía nada raro: leía `current = N-1` y se ponía a ejecutar el `down_sql` de
**N-1** contra una BD contaminada con parte de N. O falla a mitad —dejando un tercer estado
inconsistente— o "funciona" por casualidad y deja objetos huérfanos de N que el gateway ya
no sabe que existen. `current_version` tampoco delataba el problema: la versión parcial
nunca se registró.

Y con solo los blobs `up_sql`/`down_sql` era **imposible** arreglarlo: el `down_sql` es una
secuencia independiente, con otra cantidad de sentencias (los cambios sin reverso
simplemente no aparecen), así que "deshacé las k primeras del up" era ininferible.

### La solución: manifiesto de sentencias + reconciliación

**`model_migration_statements`** (tabla nueva, opcional) guarda **una fila por sentencia**
de la versión, con su reverso **emparejado** y su `seq` coincidiendo exactamente con el
índice del checkpoint (`migration_statement_progress.last_statement_index`). Ese acople es
lo que hace posible saber qué se aplicó y qué hay que deshacer.

Lo escribe el flujo que conoce el emparejamiento sentencia↔reverso: la **adopción de un
diff estructural** (`POST /schema-comparisons/{id}/adopt`). Una migración escrita a mano no
lo tiene y sigue funcionando como antes (todo-o-nada).

Tres barreras fail-closed antes de confiar en un manifiesto:

1. tiene que ser del **mismo motor** que la BD destino (el SQL traducido cross-engine puede
   no partirse en la misma cantidad de sentencias);
2. concatenar sus sentencias tiene que **reproducir exactamente** el `up_sql` vigente — es
   la misma operación con la que se construyó, así que es una igualdad exacta y no depende
   del splitter. Si no coincide, el SQL fue editado y el manifiesto NO se usa;
3. el `PATCH` que cambia el SQL **borra** el manifiesto (barrera primaria).

### Qué cambió en la práctica

- **`rollback` responde 409** mientras haya una aplicación parcial sin resolver (guard
  ROB2). El mensaje ofrece las dos salidas reales: completar con `apply` (que retoma desde
  el checkpoint) o reconciliar.
- **`GET /migrations/status`** informa `has_partial_application` y, por versión,
  `applied_statements`/`total_statements`, si es `reconcilable` y `statements_to_undo`. El
  frontend puede ofrecer el botón solo cuando sirve.
- **`POST /managed-databases/{id}/migrations/reconcile-partial`** deshace las sentencias que
  **sí** se aplicaron: ejecuta el reverso exacto de las k primeras, **en orden inverso**,
  hasta que el plano físico vuelve a coincidir con el ledger.
  - **No es un `downgrade` de Alembic**: la versión N nunca se aplicó, no hay nada que bajar
    en el ledger — y por eso **no se toca** la tabla de versión. Es una compensación.
  - `confirm_version` obligatorio (la versión parcialmente aplicada: el admin tiene que
    haber mirado el estado antes).
  - `dry_run=true` devuelve los reversos exactos sin tocar el motor. **Recomendado siempre.**
  - El checkpoint se **decrementa** después de cada reverso exitoso, así que si la
    reconciliación misma falla a mitad, el checkpoint sigue describiendo con exactitud qué
    queda aplicado y un reintento retoma donde quedó. Se limpia al llegar a 0, y ahí la BD
    sale de cuarentena.
  - `force=true` procede aunque alguna sentencia aplicada **no tenga reverso**: la saltea y
    la reporta en `unreversible_statements` (esos cambios quedan en la BD). Sin ese
    override, una sola sentencia irreversible dejaría al admin sin salida automática.
  - Sin manifiesto responde **409 con el motivo**, nunca reconcilia a ciegas. La salida ahí
    sigue siendo reconciliar a mano + `stamp?force=true`.

### Efecto secundario bueno: cuerpos procedurales resumibles

El checkpoint excluía las migraciones con rutinas/triggers (`has_non_portable`, o cualquier
`CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT`) por prudencia: el riesgo era indexar mal por una
duda del splitter. Con un manifiesto **no hay splitter** — el índice es dato persistido —
así que esas migraciones ahora **sí** se pueden resumir (`is_resumable(..., manifest_pinned=True)`).
Las exclusiones por **estado de sesión** (`SET`, `LOCK TABLES`, transacciones explícitas) y
por `kind='data'` se mantienen siempre: no dependen de cómo se obtuvieron las sentencias
sino de que un resume abre una conexión **nueva** que pierde ese estado.

### Pendiente

`create_from_snapshot` (baseline de Plan 09) **no** escribe manifiesto todavía: un baseline
que falla a mitad sigue siendo todo-o-nada. Es viable (el `StructureDump` tiene
`object_type` + `name`, así que el reverso `DROP <tipo> <nombre>` es derivable) y quedó como
follow-up.

## Límites y consideraciones

| Límite | Valor |
|---|---|
| Tamaño de cada campo SQL | 256 KB (422 si se excede) |
| `apply-all` por request | `max_databases` ≤ 100 (síncrono) |
| Rate limit `apply`/`rollback`/`stamp`/`reconcile-partial` | 10/min |
| Rate limit `apply-all` | 3/min |
| Concurrencia | advisory lock por BD; `command.*` serializado en el proceso (multiprocessing = Plan 06) |

## Verificación

- Tests unitarios (SQLite): `tests/test_api_model_migrations.py`,
  `tests/test_migration_runner.py`, `tests/test_migration_integrity.py`,
  `tests/test_sql_dialect.py`, `tests/test_api_migrations_apply_flow.py`,
  `tests/test_api_migrations_rollback_flow.py`.
- Verificación e2e contra motores reales (MySQL 8 / MariaDB 11 / PostgreSQL 16): script
  `scripts/verify_migrations_e2e.py` (requiere Docker) — **ejecutado: 153 checks / 0 fallos**
  (2026-06-29), cubre apply/rollback secuencial, gate `reviewed`, autoasignación de versión y los
  flujos de [adopción/snapshot](adoption-reconcile-snapshot.md) (Plan 09). Pendiente: integrarlo en
  CI con testcontainers (Plan 08).
- **Checkpoint de sentencia (resume automático) — pendiente de verificación e2e**: revisado
  en diseño con los agentes `gateway-senior-python`/`gateway-db-dialects` (ventana de doble
  escritura, binding por checksum, exclusión de `BEGIN...END`/estado de sesión), verificado
  con `ast.parse`/compilación del codegen y ejecución directa de las funciones puras
  (`is_resumable`, generación del archivo de revisión) — **no** verificado contra un motor
  real (sin Docker/MySQL disponibles en el entorno donde se implementó) ni con la suite
  `pytest`. Antes de confiar en producción: correr el ciclo apply parcial → reintento
  contra MySQL/MariaDB/PostgreSQL reales, y la migración Alembic
  (`d1e2f3a4b5c6_add_migration_statement_progress`) con upgrade/downgrade/upgrade contra la
  BD del gateway real.

---

**Siguiente:** [Clonado de bases de datos](../plans/05-clonado-de-bases-de-datos.md) ·
[Operación: seguridad, auditoría y observabilidad](../plans/06-operacion-seguridad-observabilidad.md)
