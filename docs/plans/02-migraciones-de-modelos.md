# 02 — Migraciones de modelos (blueprints versionados)

**Estado:** Pendiente · **Depende de:** 01 · **Esfuerzo:** alto

## Objetivo

Permitir que un **modelo** (Whatsapp, SMS, Llamadas…) defina una estructura
versionada (tablas, vistas, stored procedures, triggers) y poder **aplicar/migrar**
esa estructura sobre las N bases de datos que replican el modelo, sabiendo en todo
momento qué versión tiene cada BD.

## Estrategia recomendada: scripts SQL versionados (estilo Flyway)

Cada modelo tiene una secuencia ordenada de migraciones DDL versionadas. El gateway
aplica a cada BD las que le falten y registra el historial. Determinista, auditable
y soporta SP/vistas/triggers (a diferencia de un diff de estructura).

> Alternativa descartada para v1: BD canónica de referencia + diff/sync (más frágil
> con SP/triggers). Se puede ofrecer como utilidad complementaria más adelante.

## Modelo de datos (extiende el de 01)

### `ModelMigration` (`model_migrations`)
| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `model_id` | FK→`database_models.id` CASCADE | |
| `version` | `String(50)` | p.ej. `0001`, `1.2.0`; orden estable |
| `name` | `String(200)` | descripción corta |
| `up_sql` | `Text` | DDL idempotente para aplicar |
| `down_sql` | `Text` nullable | reversa (opcional) |
| `checksum` | `String(64)` | hash del `up_sql` para detectar alteraciones |
| timestamps | | |

Constraint: `UniqueConstraint("model_id","version")`.

### `DatabaseMigrationHistory` (`database_migration_history`)
| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `managed_database_id` | FK→`managed_databases.id` CASCADE | |
| `model_migration_id` | FK→`model_migrations.id` | |
| `applied_at` | datetime | |
| `status` | enum `applied\|failed` | |
| `error` | `Text` nullable | |
| `execution_ms` | `Integer` nullable | |

`ManagedDatabase.model_version` pasa a reflejar la última migración aplicada.

## Componentes

- **`MigrationRunner`** (`app/services/db_admin/migrations.py`): calcula migraciones
  pendientes para una BD (las del modelo con `version` > la aplicada y no presentes
  en el historial), las aplica en orden dentro de la BD destino y registra historial.
- Reutiliza `database_connection(target, db)` del adapter. Para MySQL muchas sentencias
  DDL hacen commit implícito; para PostgreSQL, envolver en transacción cuando sea posible.
- Validar `checksum` antes de aplicar (rechazar si una migración ya aplicada cambió).

## API (`/api/v1`)

| Método | Path | Descripción |
|---|---|---|
| GET/POST | `/database-models/{id}/migrations` | listar/crear migraciones del modelo |
| GET | `/managed-databases/{id}/migrations/status` | versión actual vs. pendientes |
| POST | `/managed-databases/{id}/migrations/apply` | aplica pendientes (o hasta `?version=`) |
| POST | `/database-models/{id}/migrations/apply-all` | aplica a TODAS las BDs del modelo (job en background — ver plan 06) |
| POST | `/managed-databases/{id}/migrations/rollback` | revierte la última (si hay `down_sql`) |

## Decisiones a confirmar

- **Origen de las migraciones:** ¿se cargan vía API (texto SQL), desde archivos en repo,
  o ambas? Recomendado: API + posibilidad de import desde archivos.
- **Aislamiento de SP/triggers/`DELIMITER`:** los SP MySQL usan `DELIMITER`, que es del
  cliente, no del servidor. Hay que ejecutarlos sentencia a sentencia respetando el
  cuerpo (usar el driver con `multi`/separación adecuada, no `DELIMITER`).
- **Aplicación masiva:** debe ser un job en background con reporte por BD (plan 06).

## Riesgos

- Una migración fallida a mitad puede dejar la BD inconsistente → registrar `failed`,
  detener esa BD, continuar con las demás, y exponer el detalle.
- DDL no transaccional en MySQL: documentar que una migración debe ser lo más atómica
  e idempotente posible.

## Verificación

- Crear modelo con 2 migraciones, crear 2 BDs del modelo, aplicar, verificar estructura
  e historial en ambas; cambiar una migración aplicada → detección por checksum;
  aplicar masivo y revisar el reporte por BD.
