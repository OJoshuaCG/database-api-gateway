# Gestión de Usuarios, Bases de Datos y Permisos (Iteración 2)

Capa de **aprovisionamiento**: el gateway no solo inventaría servidores e
inspecciona estructura (Iteración 1) — ahora **crea y administra** usuarios del
motor, bases de datos y permisos (DDL/DCL) en los servidores destino.

Se apoya en la [capa de conexión remota](remote-connections.md) y los adaptadores
multi-motor; toda credencial sigue [cifrada](encryption.md) y todo endpoint exige
[sesión de administrador](authentication.md).

## Entidades del inventario

Tres modelos ORM nuevos en la BD de metadatos del gateway:

| Modelo | Tabla | Rol |
|---|---|---|
| `ServerUser` | `server_users` | Usuario real del motor (el **propietario**): `'user'@'host'` en MySQL, ROLE con LOGIN en PostgreSQL. Password opcional, **cifrado Fernet**. |
| `DatabaseModel` | `database_models` | **Blueprint/categoría** lógica versionada (p. ej. "Whatsapp", "SMS"). Define migraciones SQL que el gateway aplica a las BDs que lo replican — ver [Migraciones de Blueprints](model-migrations.md) (Plan 02 implementado). |
| `ManagedDatabase` | `managed_databases` | **BD real** creada en un servidor. Pertenece a exactamente **un** `ServerUser` del mismo servidor; opcionalmente replica un blueprint. |

**Reglas de integridad:**
- 1 BD gestionada → **exactamente 1** propietario (`owner_id` FK `NOT NULL`, `ondelete=RESTRICT`).
- Nombre de BD **único por servidor**; usuario único por `(servidor, username, host)`.
- El propietario debe ser un `ServerUser` **del mismo servidor** (validado en el controller; 409 si no coincide).
- Borrar un `ServerUser` que posee BDs → **409** (reasigna o elimina las BDs primero).
- Borrar un `Server` → cascada que limpia sus usuarios y BDs del inventario (`ondelete=CASCADE`).

`ProvisionStatus` (en `managed_databases.status`): `pending` → `active` | `error` | `archived`.

## Modelo de consistencia inventario ↔ motor

Las operaciones que tocan el motor se controlan con **flags de query**, de modo que
se puede gestionar solo el inventario o también el motor real:

- `?provision=true` (POST) → ejecuta el DDL/DCL en el motor.
- `?drop_remote=true` (DELETE) → ejecuta `DROP` en el motor.

Patrón sin rollback silencioso:

```
ManagedDatabase.create(provision=true):
    INSERT status=pending
    → CREATE DATABASE  (SIN GRANT: el propietario NO recibe privilegios por defecto;
                        en PostgreSQL queda como OWNER nativo, en MySQL/MariaDB sin nada)
         éxito → status=active
         falla → status=error (detalle en notes), se conserva el registro, error HTTP al cliente

ServerUser.create(provision=true):
    INSERT (reclama unicidad)
    → CREATE USER
         falla → rollback LIMPIO del registro (no quedó usuario en el motor)

ServerUser.update password (provision=true):
    ALTER USER en el motor PRIMERO → luego persiste el password cifrado
    (el inventario nunca se adelanta al motor)
```

Toda operación mutante o que toca el motor (incluidos los fallos) queda registrada en
la tabla de **auditoría** (`audit_log`): acción, objeto, admin, Request ID, IP,
`touched_engine`, `status`, y un `detail` corto **sin credenciales**. La auditoría es
best-effort: nunca rompe la operación de negocio.

## Endpoints (API v1)

> Todos requieren sesión de administrador. **GW** = solo inventario · **GW+motor** = además ejecuta DDL/DCL.

### Usuarios del motor — `/server-users`
| Método | Ruta | Alcance |
|---|---|---|
| GET | `/server-users?server_id=` | GW (listado paginado, filtrable por servidor) |
| POST | `/server-users?provision=true` | GW+motor (`CREATE USER`) |
| GET | `/server-users/{id}` | GW |
| PATCH | `/server-users/{id}?provision=true` | GW+motor si cambia password (`ALTER USER`) |
| DELETE | `/server-users/{id}?drop_remote=true` | GW+motor (`DROP USER`; bloquea si posee BDs) |
| GET | `/server-users/{id}/databases` | GW (BDs que posee) |
| POST | `/server-users/{id}/grants` | GW+motor (`GRANT`/`REVOKE` granular por nivel y objeto) |
| POST | `/server-users/provision` | GW+motor (crear usuario + aprovisionar + grants iniciales en una sola llamada) |

### Blueprints — `/database-models`
CRUD estándar (`GET`/`POST`/`GET {id}`/`PATCH {id}`/`DELETE {id}`) — todo **GW** — más
`GET /database-models/{id}/databases`.

### Bases de datos gestionadas — `/managed-databases`
| Método | Ruta | Alcance |
|---|---|---|
| GET | `/managed-databases?server_id=&owner_id=&model_id=&status=` | GW |
| POST | `/managed-databases?provision=true` | GW+motor (`CREATE DATABASE`; **sin GRANT** automático) |
| GET | `/managed-databases/{id}` | GW |
| PATCH | `/managed-databases/{id}` | GW (metadatos del inventario) |
| DELETE | `/managed-databases/{id}?drop_remote=true` | GW+motor (`DROP DATABASE`) |
| POST | `/managed-databases/{id}/reassign-owner?provision=true` | GW+motor (re-grant / `ALTER OWNER`) |

Ningún `*Out` expone passwords: `ServerUserOut` informa `has_password: bool`.

## Diferencias por motor (encapsuladas en los adaptadores)

| Tema | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Crear BD | `CREATE DATABASE ... CHARACTER SET ...` (sin GRANT al owner) | `CREATE DATABASE ... OWNER <role> ENCODING 'UTF8' TEMPLATE template0` |
| Propiedad | lógica: asociación en metadatos del gateway (el owner **no** recibe privilegios automáticos) | nativa: `OWNER` (`ALTER DATABASE ... OWNER TO`) |
| Reasignar owner | revoca al anterior + otorga al nuevo (`grant_database`, aún default `ALL` — pendiente de cablear al catálogo, Plan 07) | `ALTER DATABASE ... OWNER TO` + re-grant + revoca al anterior |
| `charset`/`collation` | se usan | se ignoran (encoding fijo UTF8) |

> **Política de privilegios:** crear una BD o un usuario **no otorga ningún privilegio**
> por defecto (jamás `ALL PRIVILEGES`; eso solo lo tiene la credencial pseudo-root de
> conexión). Los privilegios se asignan **explícitamente** mediante
> `POST /api/v1/server-users/{user_id}/grants`. El catálogo de privilegios controlados
> por motor está en `GET /api/v1/privileges` (ver `privileges`). Ver también la sección
> [Gestión de permisos sobre la base de datos](#gestión-de-permisos-sobre-la-base-de-datos).

## Gestión de permisos sobre la base de datos

### Sin privilegios automáticos al crear

Crear una `ManagedDatabase` (con o sin `?provision=true`) **no otorga ningún privilegio**
al owner sobre esa base de datos:

- **PostgreSQL:** el owner queda registrado como `OWNER` nativo de la BD (derechos de
  propiedad implícitos sobre la BD en sí), pero sin entradas en ACL y sin acceso a objetos
  dentro del schema `public` hasta que se haga un GRANT explícito.
- **MySQL / MariaDB:** el owner es solo una asociación lógica en el inventario del gateway.
  El usuario del motor no recibe ningún `GRANT`; no puede conectarse a esa BD ni operar
  ningún objeto hasta que se otorguen privilegios explícitamente.

### Otorgar permisos: `POST /api/v1/server-users/{user_id}/grants`

Para dar acceso a un usuario sobre una base de datos gestionada:

```http
POST /api/v1/server-users/{user_id}/grants
Content-Type: application/json

{
  "level": "database",
  "object_ref": { "database": "nombre_bd" },
  "privileges": ["SELECT", "INSERT", "UPDATE", "DELETE"]
}
```

El catálogo de privilegios válidos por motor y nivel está en `GET /api/v1/privileges`.

> Ver `docs/features/permissions.md` para la documentación completa del módulo de
> permisos (niveles, motor-específico, REVOKE, catálogo).

### Flujo de trabajo recomendado: crear BD y dar acceso

**Opción A — flujos separados (usuario ya existe):**

```
1. POST /api/v1/managed-databases?provision=true     → crea la BD en el motor
2. POST /api/v1/server-users/{id}/grants             → otorga privilegios sobre la BD
```

**Opción B — aprovisionamiento unificado (usuario nuevo):**

```
1. POST /api/v1/managed-databases?provision=true     → crea la BD
2. POST /api/v1/server-users/provision               → crea el usuario + aprovisiona
                                                        + aplica initial_grants en una
                                                        sola llamada
```

`initial_grants` en el body de `/provision` acepta la misma estructura que `/grants`,
lo que evita viajes adicionales cuando se aprovisiona un usuario desde cero.

## Verificación

Suite pytest (SQLite, **adaptador mockeado** en los flujos de aprovisionamiento):
CRUD de las 3 entidades, unicidad, integridad owner↔servidor, no-fuga de password,
`provision` éxito→`active` / fallo→`error`, rollback de `create_user`, `drop_remote`,
`reassign-owner`, bloqueo de borrado con BDs, y registro de auditoría en fallos.

```bash
uv run pytest -q tests/test_api_server_users.py tests/test_api_managed_databases.py tests/test_api_database_models.py
```

> ⚠️ **Importante (caveat de entorno):** el sandbox de desarrollo no tiene Docker ni
> MySQL/PostgreSQL, por lo que **el DDL/DCL real NO se ejecutó contra un motor vivo**;
> los tests validan la lógica del gateway con el adaptador mockeado. Antes de producción
> es **obligatorio** ejecutar el checklist de abajo contra motores reales.

### Checklist de verificación contra motores reales (gate de despliegue)

1. **MySQL 8 — aprovisionar el usuario antes que la BD:** `CREATE DATABASE` no requiere
   que el owner exista en el motor (la BD se crea sin GRANT automático), pero si
   posteriormente se otorgan privilegios via `POST /server-users/{id}/grants`, el usuario
   debe existir en MySQL (`GRANT` **no autocrea** usuarios en MySQL 8). **Siempre
   aprovisiona el `ServerUser` (`?provision=true`) antes de llamar a `/grants`.**
2. **MySQL — escape de password:** probar create/change-password con passwords que
   contengan `'` y `\`, con `sql_mode=NO_BACKSLASH_ESCAPES` y sin él (el escape ahora
   dobla la comilla `''`, seguro en ambos modos).
3. **MySQL — privilegios del admin:** el `GRANT/REVOKE` incluye `FLUSH PRIVILEGES`
   (requiere `RELOAD`). Verificar el set de privilegios del usuario pseudo-root o quitar
   el `FLUSH` (no es necesario para GRANT/REVOKE).
4. **MySQL 8 — `caching_sha2_password`:** confirmar que pymysql (con `cryptography`)
   autentica al admin sobre el socket configurado.
5. **PostgreSQL — reasignación de propietario:** `ALTER DATABASE ... OWNER TO` requiere
   que el admin sea miembro del nuevo rol (o superusuario). Verificar el modelo de
   privilegios del pseudo-root. Nota: cambia la propiedad de la BD, **no** la de objetos
   ya creados por el dueño anterior.
6. **PostgreSQL — grants de dos niveles:** confirmar que el owner accede a tablas
   existentes y futuras del schema `public`.
7. **Migración Alembic en MySQL/MariaDB reales:** la migración se autogeneró sobre SQLite
   (usa `batch_alter_table`). Verificar especialmente los defaults `(CURRENT_TIMESTAMP)`
   (requieren MySQL 8.0.13+) y regenerar/ajustar contra el motor real si hace falta.
8. **Mapeo de errores del driver:** forzar duplicado/inexistente/sin-permiso/timeout y
   confirmar los códigos HTTP (409/404/403/504) contra los `errno`/`SQLSTATE` reales.

## Próximos pasos

El versionado y la migración de blueprints (`DatabaseModel`) ya está implementado: ver
[Migraciones de Blueprints](model-migrations.md). El clonado de BDs entre servidores y el
aprovisionamiento de infraestructura siguen en `docs/plans/` (Iteraciones 3+).
