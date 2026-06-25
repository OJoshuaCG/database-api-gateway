# 02 — Migraciones de modelos (blueprints versionados)

**Estado:** ✅ Implementado · endurecido + auditoría técnica remediada ·
**Depende de:** 01 ✅ · **Esfuerzo:** alto · **Última revisión:** 2026-06-25

> **Verificación:** 310 tests unitarios en verde (SQLite). El flujo completo
> (apply/dry-run/history/rollback con confirm/stamp/checksum/cuarentena/traducción)
> está verificado e2e contra **MySQL 8, MariaDB 11 y PostgreSQL 16** reales mediante
> el script MANUAL `scripts/verify_migrations_e2e.py` (requiere Docker). Los tests de
> integración canónicos con **testcontainers para CI siguen pendientes** (los posee
> gateway-testing-qa; ver docs/plans/08 P1).
>
> **Gotcha clave:** el advisory lock abría una transacción que dejaba la migración sin
> commitear; la conexión del runner corre en **AUTOCOMMIT** (el lock de SESIÓN
> sobrevive). Ver `app/services/db_admin/migrations.py`.

## Auditoría técnica remediada (2026-06-25)

Tras una auditoría formal (0 críticos, 6 mayores, 7 menores, 3 sugerencias) se
corrigieron **todos** los hallazgos de código:

| Hallazgo | Severidad | Corrección |
|---|---|---|
| Orden de versión lexicográfico → salto silencioso de migraciones al cruzar de 4 a 5 dígitos | MAYOR | Comparación/orden **numérico** (`version_sort_key`) en runner y SQL (`length(), version`) |
| `version` fuera del checksum + usada en path → traversal si la BD del gateway es comprometida | MAYOR | `validate_version` en el runner antes de construir el path + `version` incluida en el checksum |
| Estado "sucio" tras fallo parcial sin cuarentena | MAYOR | Fallo → `status=error` + nota; re-`apply` exige `force=true`; éxito limpia la cuarentena |
| `apply-all` síncrono y trabajo N+1 (specs/integridad/credencial por BD) | MAYOR/DB | `specs` + integridad **una vez**; `ServerTarget` cacheado por servidor |
| `apply-all` abortaba el lote ante error inesperado | MENOR | `except Exception` por BD (continúa el lote) |
| Sin rate-limit en operaciones destructivas | MENOR | `apply`/`rollback`/`stamp` `10/min`; `apply-all` `3/min` |
| Lock PG bloqueante asimétrico vs MySQL | MENOR | `pg_try_advisory_lock` con sondeo/timeout → 409 homogéneo |
| Sin cota de tamaño de SQL | MENOR | `max_length=256 KB` en el schema |
| `compute_checksum` acoplado en un controller | SUGERENCIA | Movido a `app/services/db_admin/migration_integrity.py` |
| Boilerplate por operación en el runner | SUGERENCIA | Context manager `_prepared` (tempdir + AUTOCOMMIT + lock + Config) |
| Tabla de historial sin endpoint de lectura | (previo) | `GET /managed-databases/{id}/migrations/history` |
| Falta de preview antes de aplicar DDL masivo | SUGERENCIA | `?dry_run=true` en `apply` y `apply-all` |

**Pendientes arquitectónicos (NO de código) — diferidos al Plan 06:** fan-out
asíncrono real de `apply-all` con background jobs, y reducir el `_ALEMBIC_LOCK`
global a multiprocessing (hoy serializa `command.*` en todo el proceso). Mitigados por
ahora: `apply-all` acotado por `max_databases` (≤100) + rate-limit, y el lock global es
correcto aunque no óptimo. Tests de integración en CI con testcontainers: pendientes.

## Endurecimiento aplicado tras revisión (2026-06-25)

- **Seguridad — integridad del rollback:** el `checksum` ahora cubre `down_sql`
  además de `up_sql` y variantes; `_verify_integrity` se invoca también en
  `rollback` y `stamp` (antes solo en `apply`).
- **Seguridad — doble intención:** `rollback` exige `?confirm_version=<versión actual>`
  (operación destructiva), igual que `confirm_name` en DROP DATABASE.
- **Correctitud:** tras `apply`, `model_version` se sincroniza releyendo la fuente de
  verdad (`_gw_v_{slug}` en la BD destino), no la contabilidad local; `create`/`delete`
  de migración usan un único commit atómico (inserción/borrado + `current_version`).
- **Robustez:** en MySQL/MariaDB se verifica el retorno de `GET_LOCK` (409 si no se
  adquirió); el nombre de la tabla de versión se trunca a 63 (límite de PostgreSQL).
- **DBA:** índice compuesto `(managed_database_id, applied_at)` para el historial;
  índice redundante de `model_id` eliminado (lo cubre el UNIQUE compuesto); la
  migración usa `op.create_index` plano (no `batch`, que es solo para SQLite).
- **Observabilidad:** nuevo `GET /managed-databases/{id}/migrations/history` (paginado).

## Tradeoff conocido: AUTOCOMMIT vs atomicidad DDL

El runner corre en AUTOCOMMIT, necesario para que el advisory lock de sesión no
envuelva la migración en una transacción no commiteada. Consecuencia: se renuncia a
la atomicidad DDL transaccional que **PostgreSQL** sí soportaría. Una migración
multi-sentencia que falle a mitad deja estado parcial (igual que MySQL, cuyo DDL
autocommitea siempre). Mitigación: las migraciones deben ser **idempotentes**
(`CREATE TABLE IF NOT EXISTS`, etc.) y la cadena se detiene en el primer fallo.
Mejora futura (no bloqueante): estrategia por motor con `pg_advisory_xact_lock` +
DDL transaccional real en PostgreSQL.

## Cuándo escribir `up_sql_postgresql` manual (matriz de equivalencia)

sqlglot auto-traduce el `up_sql` (estilo MySQL) a PostgreSQL para DDL común
(`AUTO_INCREMENT`→`IDENTITY`, backticks→comillas, `DATETIME`→`TIMESTAMP`). Requieren
**override manual** `up_sql_postgresql` (sqlglot no los resuelve de forma fiable):
`ENUM(...)` inline, `ON UPDATE CURRENT_TIMESTAMP` (en PG es un trigger), `UNSIGNED`/
`ZEROFILL`, `ALTER TABLE ... MODIFY ... AUTO_INCREMENT`, y rutinas `BEGIN…END` con
`;` internos (el splitter no las parte; subir como un solo delta o usar override).

## Objetivo

Permitir que un **modelo/blueprint** (Whatsapp, SMS, Llamadas…) defina una estructura versionada
(tablas, vistas, stored procedures, triggers) y poder **aplicar/migrar** esa estructura sobre las
N bases de datos que replican el modelo, sabiendo en todo momento qué versión tiene cada BD.

---

## Principio Arquitectónico

**Alembic es la primitiva de migración embebida. El gateway construye la orquestación sobre ella.**

| Responsabilidad | Quién la resuelve |
|----------------|-------------------|
| Tabla de versión en cada BD administrada (`_gw_v_{slug}`) | Alembic (`MigrationContext`) |
| Grafo de revisiones, orden, `stamp`, modo offline `--sql` | Alembic |
| Ejecución del SQL de migración en la BD destino | `MigrationRunner` vía adapters |
| Fan-out sobre N BDs (iterar, conectar, llamar Alembic) | `MigrationRunner` |
| Locking por BD (advisory locks antes de migrar) | `MigrationRunner` |
| Espejo de versiones en gateway DB (dashboard sin abrir N conexiones) | `database_migration_history` |
| Checksum de integridad (Alembic no lo trae) | `MigrationRunner` |
| Auto-traducción SQL entre motores | `sqlglot` |
| Auto-generación de `down_sql` sugerido | `RollbackGenerator` |
| Convención para SP/vistas/triggers | Migración nueva por cada cambio (ver abajo) |

---

## Estrategia: scripts SQL versionados con Alembic como librería

Cada modelo tiene una secuencia ordenada de revisiones. Al subir una migración via API,
el gateway genera el archivo Python de Alembic en `migrations/{model_slug}/versions/`
(fuente ejecutable) pero el SQL fuente de verdad vive en `model_migrations` (reconstruible).
Alembic aplica en orden, registra la versión en `_gw_v_{slug}` dentro de cada BD administrada.

> No se usa autogenerate en runtime ni archivos Python como fuente de verdad persistente.
> Los .py son generados desde la BD del gateway al aplicar, reconstituibles en contenedores.

---

## Decisiones de diseño confirmadas

| Tema | Decisión |
|------|----------|
| Naming entidad | `DatabaseModel` no se renombra ("etiqueta" = término coloquial) |
| Alembic uso | `command.upgrade` + `MigrationContext` vía `env.py` con conexión inyectada |
| SQL cross-engine | `sqlglot` auto-traduce; overrides `up_sql_mysql`/`up_sql_postgresql` opcionales |
| `down_sql` | `sqlglot` genera `down_sql_suggested`; admin confirma via PATCH antes de ser ejecutable |
| Formato SQL | Deltas incrementales; la primera migración puede ser el esquema completo inicial |
| Almacenamiento | Tabla `model_migrations` en BD del gateway (fuente de verdad) |
| Versión en BD administrada | Alembic → tabla `_gw_v_{model_slug}` dentro de cada BD administrada |
| Log histórico | `database_migration_history` en gateway (auditoría + espejo para dashboard) |
| Formato de versión | Entero con padding `0001`, `0002`… (lexicográfico = cronológico) |
| Thread-safety | Fan-out masivo usa `multiprocessing`, no threads (Plan 06) |
| Locking | Advisory locks por BD: `pg_advisory_lock` / `GET_LOCK` MySQL |
| `apply-all` | Stub síncrono con `?max_databases=N` en Plan 02; async multiprocessing en Plan 06 |

---

## Modelo de datos (extiende el de 01)

### `ModelMigration` (`model_migrations`)

| Campo | Tipo | Notas |
|-------|------|-------|
| `id` | PK | |
| `model_id` | FK→`database_models.id` CASCADE | |
| `version` | `String(10)` | `"0001"`, `"0002"`… padding de 4 dígitos mínimo |
| `name` | `String(200)` | Descripción corta |
| `up_sql` | `Text` | SQL que el admin subió (fuente de verdad) |
| `up_sql_mysql` | `Text` nullable | Override manual MySQL/MariaDB |
| `up_sql_postgresql` | `Text` nullable | Override manual PostgreSQL |
| `down_sql_suggested` | `Text` nullable | Auto-generado por `RollbackGenerator` (para revisión) |
| `down_sql` | `Text` nullable | Confirmado por admin (null hasta que lo apruebe via PATCH) |
| `checksum` | `String(64)` | `SHA256(up_sql + up_sql_mysql + up_sql_postgresql)` |
| timestamps | | |

`UniqueConstraint("model_id", "version")`.

### `DatabaseMigrationHistory` (`database_migration_history`)

| Campo | Tipo | Notas |
|-------|------|-------|
| `id` | PK | |
| `managed_database_id` | FK→`managed_databases.id` CASCADE | |
| `model_migration_id` | FK→`model_migrations.id` | |
| `applied_at` | datetime | |
| `status` | enum `applied\|failed` | |
| `error` | `Text` nullable | |
| `execution_ms` | `Integer` nullable | |

`ManagedDatabase.model_version` refleja la última migración `applied` exitosamente
(actualizado por el runner, espejo de `_gw_v_{slug}` en la BD administrada).

---

## Estructura de scripts Alembic por modelo

```
migrations/
├── _shared/
│   └── env.py                        # env.py compartido, inyección de conexión
├── whatsapp/
│   └── versions/
│       ├── 0001_esquema_inicial.py   # generado desde model_migrations al aplicar
│       └── 0002_agregar_telefono.py
└── sms/
    └── versions/
        └── 0001_esquema_sms.py
```

### `migrations/_shared/env.py`

```python
from alembic import context

config = context.config
target_metadata = None  # NO autogenerate — SQL directo

def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("'connection' no inyectada en config.attributes")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=config.attributes["version_table"],  # "_gw_v_{slug}"
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()

run_migrations_online()
```

### Script Python generado (ejemplo)

```python
# migrations/whatsapp/versions/0002_agregar_telefono.py
revision = "0002"
down_revision = "0001"

def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN phone VARCHAR(20)")

def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN phone")
    # Si no hay down_sql confirmado → lanza NotImplementedError
```

---

## Componentes

### `MigrationRunner` (`app/services/db_admin/migrations.py`)

```python
class MigrationRunner:
    def _make_config(self, model_slug, connection) -> Config:
        cfg = Config()
        cfg.set_main_option("script_location", f"migrations/{model_slug}")
        cfg.attributes["connection"] = connection
        cfg.attributes["version_table"] = f"_gw_v_{model_slug}"
        return cfg

    def ensure_script_files(self, model_slug, migrations, engine_type): ...
    def get_current_version(self, connection, model_slug) -> str | None: ...
    def acquire_lock(self, connection, managed_db_id, engine_type): ...
    def release_lock(self, connection, managed_db_id, engine_type): ...
    def apply_pending(self, managed_db, migrations, up_to_version=None) -> list[MigrationResult]: ...
    def rollback_last(self, managed_db) -> MigrationResult: ...
    def stamp(self, managed_db, version: str): ...  # marcar sin ejecutar
```

### `SqlTranslator` + `RollbackGenerator` (`app/services/db_admin/sql_dialect.py`)

- `SqlTranslator.translate(sql, to_engine)` — usa `sqlglot.transpile()`, retorna `str | None`
- `RollbackGenerator.generate(sql)` — infiere inverso para CREATE→DROP, ADD COLUMN→DROP COLUMN; `None` para operaciones destructivas

---

## API (`/api/v1`)

| Método | Path | Descripción |
|--------|------|-------------|
| GET/POST | `/database-models/{id}/migrations` | Listar/crear migraciones del modelo |
| GET | `/database-models/{id}/migrations/{version}` | Detalle de versión |
| PATCH | `/database-models/{id}/migrations/{version}` | Confirmar `down_sql` o añadir override de variante |
| DELETE | `/database-models/{id}/migrations/{version}` | Solo si no hay historial `applied` |
| GET | `/managed-databases/{id}/migrations/status` | Versión actual + pendientes |
| POST | `/managed-databases/{id}/migrations/apply` | Aplica pendientes (`?version=0003` para target) |
| POST | `/managed-databases/{id}/migrations/rollback` | `command.downgrade -1` (409 si no hay `down_sql`) |
| POST | `/managed-databases/{id}/migrations/stamp` | Marcar versión sin ejecutar (BDs pre-existentes) |
| POST | `/database-models/{id}/migrations/apply-all` | Aplica a todas las BDs del modelo (`?max_databases=10`) |

### Respuesta de `POST /migrations` (crear versión)

```json
{
  "version": "0002", "name": "Agregar teléfono",
  "up_sql": "ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
  "down_sql": null,
  "down_sql_suggested": "ALTER TABLE users DROP COLUMN phone",
  "translated": {
    "mysql": "ALTER TABLE users ADD COLUMN phone VARCHAR(20)",
    "postgresql": "ALTER TABLE users ADD COLUMN phone VARCHAR(20)"
  },
  "checksum": "abc123..."
}
```

El admin revisa `down_sql_suggested` y llama `PATCH` si desea confirmarlo.

---

## Gotchas y traps operacionales

### `alembic.context` no es thread-safe
`alembic.context` es un proxy global de módulo. `command.upgrade` con múltiples tenants en
threads del mismo proceso produce corrupción silenciosa. **Fan-out masivo → `multiprocessing`,
no `threading`.** En Plan 02 (stub síncrono) no es problema; crítico en Plan 06.

### MySQL DDL no es transaccional
`CREATE TABLE`, `ALTER TABLE` hacen commit implícito. Si una migración de 5 sentencias falla
en la 3, la BD queda inconsistente y Alembic no avanza la versión. Diseñar migraciones
idempotentes (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).
Registrar `failed`, dejar para inspección manual, continuar con las demás BDs.

### SP / vistas / triggers: no hay "repeatable"
Alembic no re-aplica automáticamente un objeto cuando su definición cambia (Liquibase sí tiene
`runOnChange`). Convención: crear una migración nueva por cada cambio de SP/vista:
```sql
DROP PROCEDURE IF EXISTS nombre;
CREATE PROCEDURE nombre() BEGIN ... END;
```

### DELIMITER en stored procedures MySQL
`DELIMITER $$` es una directiva del cliente MySQL, no del servidor. PyMySQL no la entiende.
Los SP deben subirse sin `DELIMITER`, con el cuerpo completo como una sentencia:
```sql
CREATE PROCEDURE sp_nombre() BEGIN SELECT 1; END
```

### Drift / tampering
Alembic no verifica que las migraciones aplicadas no hayan cambiado (Flyway/Liquibase sí).
El gateway valida `checksum` de `model_migrations` antes de aplicar: si el SQL fue alterado
después de aplicarse en alguna BD, rechaza con error bloqueante.

### Locking obligatorio
Dos workers migrando la misma BD = doble aplicación o corrupción. Adquirir advisory lock
antes de `command.upgrade`, liberar siempre en finally:
- PostgreSQL: `SELECT pg_advisory_lock({managed_db_id})`
- MySQL: `SELECT GET_LOCK('gw_migrate_{managed_db_id}', 30)`

---

## Qué hay en cada BD

```
BD administrada "ventas" (servidor MySQL):
  _gw_v_whatsapp   ← Alembic (version_num = '0003')
  users, orders    ← tablas creadas por las migraciones

Gateway BD:
  model_migrations             ← SQL + checksum (fuente de verdad)
  database_migration_history   ← log auditoría / espejo para dashboard

Filesystem gateway:
  migrations/whatsapp/versions/  ← archivos .py generados desde model_migrations
```

---

## Orden de implementación

1. `uv add sqlglot`
2. Modelos ORM: `model_migration.py`, `database_migration_history.py` + `enums.py` + `__init__.py`
3. Migración Alembic del gateway: `uv run alembic revision --autogenerate -m "plan02 model migrations"`
4. `migrations/_shared/env.py`
5. `SqlTranslator` + `RollbackGenerator` + tests unitarios
6. `MigrationRunner` (generación scripts, apply, rollback, stamp, locking) + tests unitarios
7. Schemas Pydantic (`app/schemas/model_migration.py`)
8. Controller de migraciones del blueprint
9. Extender controllers `ManagedDatabase` y `DatabaseModel`
10. Routes + registrar en `v1/__init__.py`
11. Tests de integración (testcontainers MySQL + PG)

---

## Verificación

1. POST migración v0001 → respuesta incluye `translated.postgresql` + `down_sql_suggested`
2. PATCH confirma `down_sql`; POST v0002 (ALTER ADD COLUMN)
3. Crear BD MySQL, asignar blueprint → `/status` muestra `current_version: null`, 2 pendientes
4. `apply` → Alembic crea `_gw_v_{slug}` en BD MySQL; `model_version=0002`
5. BD PostgreSQL → `apply` usa variantes PG auto-traducidas
6. Modificar `up_sql` post-aplicación → checksum mismatch → error bloqueante
7. `rollback` → `command.downgrade -1`; `model_version=0001`
8. `stamp` en BD pre-existente → marca sin ejecutar
9. `apply-all` → todas las BDs del blueprint actualizadas
10. Dos workers simultáneos → advisory lock → serialización correcta
