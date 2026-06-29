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
| `POST` | `/api/v1/managed-databases/adopt` 🔌 | Adopta una BD que **ya existe** (sin `CREATE DATABASE`; estado `active`, `origin=adopted`). Exige `owner_id` del mismo servidor. `404` si no existe; `409` si ya está. |
| `POST` | `/api/v1/server-users/adopt` 🔌 | Adopta un usuario existente (sin `CREATE USER`, sin password → `has_password=false`). |
| `GET` | `/api/v1/servers/{id}/databases/{db}/snapshot` 🔌 | **Snapshot estructural** (preview): tablas, vistas, rutinas, triggers, y según motor secuencias/tipos/extensiones/events. **Solo estructura, nunca filas**; `DEFINER` saneado. |
| `POST` | `/api/v1/database-models/from-snapshot` 🔌 | Crea un blueprint cuyo baseline (`0001`) es el snapshot estructural de una BD. Rate limit 10/min. |

## Snapshot → blueprint baseline (y el gate `reviewed`)

`dump_structure` captura el DDL autoritativo por motor (`SHOW CREATE *` en MySQL/MariaDB;
`pg_get_*def()` + reflexión en PostgreSQL) en **orden de dependencia**. Si el snapshot incluye
objetos **procedurales** (rutinas/triggers/events), el baseline queda **atado a su motor de origen**
(`source_engine`, `has_non_portable=true`) porque ese código no es traducible cross-engine — aplicarlo
a otro motor da `422` (cross-engine guard).

**Seguridad (R1):** el baseline es DDL capturado de un motor potencialmente no confiable, así que
nace **`reviewed=false`** y **no se puede aplicar** (`apply`/`apply-all` → `409`) hasta que un admin
lo revise y apruebe con `PATCH /database-models/{id}/migrations/{version}` `{"reviewed": true}`.

## Los 3 modos para vincular una BD adoptada a migraciones

1. **BD vacía** → `apply` desde `0001` (con `dry_run` primero).
2. **BD que ya coincide con un blueprint en la versión X** → `stamp` en X (marca sin ejecutar) y
   sigue con `apply` de ahí en adelante.
3. **BD legacy/única** → `from-snapshot` (baseline) → aprobar (`reviewed`) → `stamp`/`apply`.

## Seguridad

- Todos los endpoints exigen `AdminDep`; los identificadores pasan por validación anti-inyección
  antes de cualquier `SHOW CREATE`/consulta de catálogo; el `DEFINER` se sanea en el dump.
- La adopción no ejecuta DDL de creación y audita con `touched_engine=false`.
- Anti-SSRF: el host se revalida **al conectar** (no solo al registrar) — ver [conexión remota](remote-connections.md).

## Verificación

- Unitarios (SQLite + adapter mockeado): `tests/test_api_plan09_adopt_snapshot.py`.
- e2e contra motores reales (MySQL 8 / MariaDB 11 / PostgreSQL 16): `scripts/verify_migrations_e2e.py`
  (`run_plan09`) — **ejecutado: 153 checks / 0 fallos** (2026-06-29).

---

**Siguiente:** [Migraciones de blueprints](model-migrations.md) ·
[Gestión de usuarios/BDs](database-management.md)
