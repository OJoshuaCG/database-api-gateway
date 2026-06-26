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
| `version` | sí | Solo dígitos, 4–10 (`0001`, `0002`…). **Orden numérico**: mantén un ancho consistente |
| `name` | sí | Descripción corta (≤200) |
| `up_sql` | sí | Delta SQL base, **estilo MySQL de referencia**. ≤256 KB |
| `up_sql_mysql` | no | Override manual para MySQL/MariaDB (si la auto-traducción no basta) |
| `up_sql_postgresql` | no | Override manual para PostgreSQL |
| `down_sql` | no | Rollback **confirmado**. Sin él, `rollback` responde 409 |
| `down_sql_suggested` | (auto) | Rollback sugerido por el gateway para ops aditivas; revisar y confirmar vía `PATCH` |
| `checksum` | (auto) | SHA256 de todo el SQL + versión; detecta alteración antes de aplicar |

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
PATCH  /api/v1/database-models/{id}/migrations/{version}  # confirma down_sql / añade overrides
DELETE /api/v1/database-models/{id}/migrations/{version}  # solo si NO tiene historial de aplicación
```

### Aplicación sobre una BD gestionada (tocan el motor)

```http
GET    /api/v1/managed-databases/{id}/migrations/status     # versión actual vs. pendientes
POST   /api/v1/managed-databases/{id}/migrations/apply      # ?version= ?force= ?dry_run=   (10/min)
POST   /api/v1/managed-databases/{id}/migrations/rollback   # ?confirm_version= (OBLIGATORIO) (10/min)
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
# Crear la primera migración (esquema inicial, estilo MySQL)
curl -X POST .../api/v1/database-models/1/migrations -b cookie.txt \
  -H 'Content-Type: application/json' -d '{
    "version": "0001",
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
# → { "dry_run": true, "current_version": null, "pending_versions": ["0001"], "pending_count": 1 }
```

### 4. Aplicar

```bash
curl -X POST .../api/v1/managed-databases/5/migrations/apply -b cookie.txt
# → { "applied_count": 1, "failed": false, "quarantined": false,
#     "results": [{ "version": "0001", "status": "applied", "execution_ms": 42 }] }
```

`?version=0003` aplica solo hasta esa versión (inclusive).

### 5. Historial y rollback

```bash
curl .../api/v1/managed-databases/5/migrations/history -b cookie.txt
# → [{ "version": "0001", "status": "applied", "applied_at": "…", "execution_ms": 42 }]

# Rollback DESTRUCTIVO: confirm_version debe igualar la versión actual de la BD
curl -X POST '.../api/v1/managed-databases/5/migrations/rollback?confirm_version=0001' -b cookie.txt
```

Sin `confirm_version` (o si no coincide con la versión actual) → **422**. Si la versión
actual no tiene `down_sql` confirmado → **409**.

### 6. Marcar una BD pre-existente (stamp)

Si una BD ya tiene el esquema de una versión pero el gateway no lo sabe, `stamp` registra
la versión **sin ejecutar SQL**:

```bash
curl -X POST '.../api/v1/managed-databases/5/migrations/stamp?version=0003' -b cookie.txt
```

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
  verificado). Una migración con historial de aplicación no puede modificar su SQL.
- **Cuarentena (fallo parcial)**: como el DDL no es transaccional en MySQL/MariaDB (y el
  runner corre en AUTOCOMMIT), una migración multi-sentencia que falla a mitad puede dejar
  estado parcial. El gateway marca la BD con `status=error` + nota; el siguiente `apply`
  responde **409** hasta que inspecciones y reintentes con **`?force=true`**. Un apply
  exitoso limpia la cuarentena.
- **Recomendación**: escribe migraciones **idempotentes** (`CREATE TABLE IF NOT EXISTS`,
  `ADD COLUMN IF NOT EXISTS`) para que un reintento sea seguro.

## Límites y consideraciones

| Límite | Valor |
|---|---|
| Tamaño de cada campo SQL | 256 KB (422 si se excede) |
| `apply-all` por request | `max_databases` ≤ 100 (síncrono) |
| Rate limit `apply`/`rollback`/`stamp` | 10/min |
| Rate limit `apply-all` | 3/min |
| Concurrencia | advisory lock por BD; `command.*` serializado en el proceso (multiprocessing = Plan 06) |

## Verificación

- Tests unitarios (SQLite): `tests/test_api_model_migrations.py`,
  `tests/test_migration_runner.py`, `tests/test_migration_integrity.py`,
  `tests/test_sql_dialect.py`.
- Verificación e2e contra motores reales (MySQL 8 / MariaDB 11 / PostgreSQL 16):
  script **manual** `scripts/verify_migrations_e2e.py` (requiere Docker).

---

**Siguiente:** [Clonado de bases de datos](../plans/05-clonado-de-bases-de-datos.md) ·
[Operación: seguridad, auditoría y observabilidad](../plans/06-operacion-seguridad-observabilidad.md)
