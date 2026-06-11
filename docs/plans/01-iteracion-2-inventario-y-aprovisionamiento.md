# 01 — Iteración 2: Inventario completo y aprovisionamiento de usuarios/BDs

**Estado:** Pendiente · **Depende de:** 00 (parcial) · **Esfuerzo:** alto

## Objetivo

Completar el núcleo "del ahora": modelar **usuarios del motor (propietarios)**,
**modelos/blueprints** y **bases de datos gestionadas**, y exponer la API que
realmente **crea/gestiona usuarios, bases de datos y permisos** en los servidores
destino (DDL/DCL). Los métodos de escritura ya están implementados en los adapters
(`create_user`, `drop_user`, `change_password`, `create_database`, `drop_database`,
`grant_database`, `revoke_database`); esta iteración los **conecta** a entidades y
endpoints.

## Reglas de negocio (de los requisitos)

- Una **base de datos gestionada** pertenece a **exactamente un** usuario del motor.
- Un usuario del motor puede tener **muchas** bases de datos.
- Varias bases de datos pueden compartir el **mismo modelo** (misma estructura, BDs distintas).
- Nombre de BD único **por servidor**; usuario del motor único por servidor (+host en MySQL).
- El gateway no gestiona multiusuario propio (sigue habiendo un solo admin).

## Modelo de datos (ORM, BD del gateway)

Nuevos modelos en `app/models/`, registrados en `app/models/__init__.py`.

### `ServerUser` (`server_users`) — el propietario
| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `server_id` | FK→`servers.id` `ondelete=CASCADE` | `index` |
| `username` | `String(128)` | usuario del motor |
| `host` | `String(255)` default `"%"` | parte de `'user'@'host'` en MySQL; en PG se fuerza/ignora a `"%"` |
| `password_encrypted` | `Text` nullable | **CIFRADO Fernet**, opcional; nunca se expone |
| `is_active` | `Boolean` default `True` | |
| `notes` | `Text` nullable | |
| timestamps | (TimestampMixin) | |

Constraint: `UniqueConstraint("server_id","username","host", name="uq_server_users_server_username_host")`.

### `DatabaseModel` (`database_models`) — blueprint/categoría
| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `name` | `String(100)` unique | "Whatsapp", "SMS", "Llamadas" |
| `slug` | `String(120)` unique, index | identificador estable |
| `description` | `Text` nullable | |
| `current_version` | `String(50)` default `"0.0.0"` | versión del blueprint (string libre por ahora) |
| `is_active` | `Boolean` default `True` | |
| timestamps | | |

> Nota de naming: la clase `DatabaseModel` choca conceptualmente con la convención
> `*_model.py`. Sugerencia: clase `DatabaseModel` en `app/models/database_model.py`,
> o renombrar a `DatabaseBlueprint`. **Decisión a confirmar.**

### `ManagedDatabase` (`managed_databases`) — BD real en un servidor
| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `name` | `String(64)` | nombre de la BD en el motor |
| `server_id` | FK→`servers.id` `ondelete=CASCADE` | `index` |
| `owner_id` | FK→`server_users.id` `ondelete=RESTRICT` | **dueño único**, `NOT NULL`, `index` |
| `model_id` | FK→`database_models.id` `ondelete=SET NULL` nullable | blueprint que replica |
| `model_version` | `String(50)` nullable | versión del modelo implementada |
| `charset` / `collation` | `String` | `utf8mb4` por defecto en MySQL |
| `status` | enum `pending\|active\|error\|archived` | refleja si ya se creó en el motor |
| `notes` | `Text` nullable | |
| timestamps | | |

Constraints/reglas:
- `UniqueConstraint("server_id","name", name="uq_managed_databases_server_name")` → BD única por servidor.
- "Un solo dueño" se garantiza con `owner_id` como **una sola columna FK NOT NULL** (no tabla puente).
- Integridad cruzada `owner.server_id == managed_database.server_id`: **validar en el controller** (lanzar 409/422 si no coinciden). Endurecimiento futuro: FK compuesta.

### `ON DELETE`
| FK | Acción | Razón |
|---|---|---|
| `server_users.server_id` | CASCADE | borrar Server limpia su inventario |
| `managed_databases.server_id` | CASCADE | idem |
| `managed_databases.owner_id` | RESTRICT | proteger "un dueño"; obliga a reasignar antes |
| `managed_databases.model_id` | SET NULL | blueprint opcional, no destruir BDs |

## API (`/api/v1`)

Leyenda: **GW** = solo BD del gateway · **GW+REMOTE** = además ejecuta DDL/DCL.

### ServerUsers
| Método | Path | Alcance |
|---|---|---|
| GET | `/servers/{server_id}/users` | GW (inventario) |
| GET | `/server-users/{id}` | GW |
| POST | `/servers/{server_id}/users` (`?provision=true`) | GW+REMOTE (`CREATE USER`) |
| PATCH | `/server-users/{id}` | GW+REMOTE (si cambia password → `ALTER USER`) |
| DELETE | `/server-users/{id}` | GW+REMOTE (bloquea si posee BDs; si no, `DROP USER`) |
| GET | `/server-users/{id}/databases` | GW |

### DatabaseModels (todo GW)
CRUD estándar en `/database-models` + `GET /database-models/{id}/databases`.

### ManagedDatabases
| Método | Path | Alcance |
|---|---|---|
| GET | `/managed-databases` (filtros `?server_id=&owner_id=&model_id=&status=`) | GW |
| GET | `/managed-databases/{id}` | GW |
| POST | `/managed-databases` | GW+REMOTE (`CREATE DATABASE` + `GRANT` al owner) |
| PATCH | `/managed-databases/{id}` | GW (+REMOTE si cambia charset/collation) |
| DELETE | `/managed-databases/{id}` (`?drop_remote=true`) | GW+REMOTE (`DROP DATABASE`) |
| POST | `/managed-databases/{id}/reassign-owner` | GW (+REMOTE re-grants) |

## Decisiones de diseño

- **Consistencia GW↔REMOTE:** insertar en GW con `status=pending` → ejecutar DDL/DCL
  remoto → si OK `status=active`; si falla, dejar `status=error` con el detalle en
  `notes` (auditable, permite reintento) y devolver el error HTTP. **No** hacer rollback
  silencioso del registro.
- **`?provision=false` / `?drop_remote=false`:** permitir registrar/desregistrar en el
  inventario sin tocar el motor (útil para adoptar objetos preexistentes).
- **Ownership en PostgreSQL** = `OWNER` nativo de la BD (`ALTER DATABASE ... OWNER TO`).
  En MySQL = concepto lógico en metadatos + `GRANT ALL ON db.*` (ya implementado en los adapters).

## Schemas Pydantic
`Create`/`Update`/`Out` por recurso. **Ningún `*Out` expone passwords** (cifrados o no);
exponer `has_password: bool`. Validar `name`/`username`/`slug` con `Field(pattern=...)`
acorde a la whitelist de identificadores.

## Pasos de implementación
1. Modelos ORM + registro en `__init__.py`.
2. `alembic revision --autogenerate` + revisar `ondelete` (autogenerate a veces no los detecta) + `upgrade`.
3. Schemas, controllers (reutilizan `get_adapter` + métodos de escritura ya existentes), routes, wiring.
4. Validación cruzada owner↔server en el controller de `ManagedDatabase`.
5. Tests: CRUD de cada entidad, regla "1 BD→1 dueño", unicidad, y flujo GW+REMOTE con motor real (crear user→crear BD→grant→introspeccionar→drop).

## Verificación (end-to-end con motor real)
- Crear `ServerUser` → aparece en `mysql.user`/`pg_roles`.
- Crear `ManagedDatabase` con owner → la BD existe y el owner tiene permisos.
- Reasignar owner → grants actualizados.
- Borrar con `?drop_remote=true` → la BD desaparece del motor.
- Intentar borrar un `ServerUser` con BDs → 409 (RESTRICT).

## Riesgos
- Divergencia inventario↔motor (mitigado con `status` + endpoint de reconciliación futuro).
- MySQL `'user'@'host'`: fijar `host="%"` como invariante salvo necesidad explícita.
- PG grants en dos niveles (DATABASE + SCHEMA): ya resuelto en `PostgresAdapter.grant_database`.
