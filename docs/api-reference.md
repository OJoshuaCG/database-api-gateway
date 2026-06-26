# API Reference — Database API Gateway

> Referencia completa para integrar el **Database API Gateway** en tu desarrollo.
> Documenta cada endpoint, sus parámetros, tipos y valores permitidos, ejemplos de
> uso (`curl` + JSON) y el orden en que deben consumirse para cumplir cada propósito.

**Versión de la API:** `v1` · **Base URL:** `https://<host>/api/v1` · **Estado:** Iteraciones 1 y 2 + migraciones de blueprints (Plan 02) implementadas (ver [§14](#14-estado-del-proyecto)).

---

## Índice

1. [¿Qué es y qué problema resuelve?](#1-qué-es-y-qué-problema-resuelve)
2. [Conceptos clave](#2-conceptos-clave)
3. [Convenciones de la API](#3-convenciones-de-la-api)
4. [Tipos de datos y enums](#4-tipos-de-datos-y-enums)
5. [Autenticación (`/auth`)](#5-autenticación-auth)
6. [Servidores (`/servers`)](#6-servidores-servers)
7. [Usuarios del motor (`/server-users`)](#7-usuarios-del-motor-server-users)
8. [Blueprints de BD y sus migraciones (`/database-models`)](#8-blueprints-de-bd-database-models)
9. [Bases de datos gestionadas y migraciones por BD (`/managed-databases`)](#9-bases-de-datos-gestionadas-managed-databases)
10. [Catálogo de privilegios (`/privileges`)](#10-catálogo-de-privilegios-privileges)
11. [Perfiles de permisos (`/permission-profiles`)](#11-perfiles-de-permisos-permission-profiles)
12. [Administración: cifrado (`/admin/crypto`)](#12-administración-cifrado-admincrypto)
13. [Health checks](#13-health-checks)
14. [Estado del proyecto](#14-estado-del-proyecto)
15. [Flujos de integración (orden de llamadas)](#15-flujos-de-integración-orden-de-llamadas)
16. [Apéndice: tabla resumen de endpoints](#16-apéndice-tabla-resumen-de-endpoints)
17. [Apéndice: variables de entorno del integrador](#17-apéndice-variables-de-entorno-del-integrador)

---

## 1. ¿Qué es y qué problema resuelve?

El **Database API Gateway** es un controlador central que permite a un administrador
gestionar **múltiples servidores remotos de bases de datos** (MySQL, MariaDB,
PostgreSQL) a través de una única API HTTP, **sin exponer nunca las credenciales
pseudo-root** de esos servidores.

Resuelve tres problemas:

1. **Segregación de credenciales.** Las contraseñas pseudo-root se almacenan cifradas
   (Fernet) en la base de datos de metadatos del propio gateway. Nunca se serializan en
   respuestas ni se escriben en logs: las respuestas solo informan un booleano
   `has_root_password` / `has_password`.
2. **Inventario + aprovisionamiento.** Mantiene un catálogo de servidores, usuarios,
   bases de datos y privilegios, y puede orquestar su creación/eliminación en el motor
   destino vía DDL/DCL (`CREATE USER`, `CREATE DATABASE`, `GRANT`, `DROP`, …).
3. **Introspección segura de estructura.** Permite inspeccionar bases de datos, tablas y
   esquemas de columnas **sin leer datos de las filas**, validando todos los
   identificadores para evitar inyección SQL.

```
                ┌────────────────────────────────────┐
  Admin  ─────▶ │   Database API Gateway              │
 (cookie)       │   FastAPI + BD de metadatos (cifr.) │
                └──────────────────┬─────────────────┘
                                   │  pseudo-root cifrada (Fernet)
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
      MySQL / MariaDB         PostgreSQL              MySQL ...
       (servidor 1)          (servidor 2)           (servidor N)
```

### Modelo de operación: inventario vs. motor destino

Toda la API distingue dos tipos de efecto:

- **Operaciones de inventario** — solo leen/escriben en la BD de metadatos del gateway.
  Son rápidas y no dependen de que el servidor destino esté accesible.
- **Operaciones que tocan el motor destino** (marcadas con **🔌** en este documento) —
  abren una conexión al servidor remoto y ejecutan SQL real. Pueden fallar si el
  servidor no es alcanzable (`502`), agota el tiempo de espera (`504`) o el recurso
  ya existe (`409`).

Muchos endpoints de escritura aceptan un flag (`?provision=true` / `?drop_remote=true`)
que decide si la operación es solo de inventario o también toca el motor.

---

## 2. Conceptos clave

| Entidad | Descripción |
|---|---|
| **Server** | Un servidor de BD remoto registrado en el inventario (host, puerto, motor, credencial pseudo-root cifrada). |
| **ServerUser** | Un usuario/rol real del motor que el gateway gestiona. En MySQL es `'usuario'@'host'`; en PostgreSQL es un `ROLE … LOGIN`. Es el **propietario** de bases de datos. |
| **ManagedDatabase** | Una base de datos real creada/gestionada por el gateway en un servidor. Pertenece a **exactamente un** `ServerUser` (owner) del mismo servidor. |
| **DatabaseModel** | Un *blueprint*/categoría lógica (p. ej. "WhatsApp", "SMS"). Metadato del inventario; varias BDs pueden replicar el mismo modelo. |
| **Privilege** | Entrada del catálogo de privilegios soportados por cada motor (`SELECT`, `CREATE`, …). |
| **AuditLog** | Registro interno de toda operación sensible (no expuesto por la API). |

**Propiedad de una base de datos:**
- *MySQL/MariaDB*: propiedad **lógica** — el owner es el `ServerUser` con `GRANT ALL` sobre la BD.
- *PostgreSQL*: propiedad **nativa** — la BD tiene un `OWNER` (`ALTER DATABASE … OWNER TO`).

**Estados de aprovisionamiento** (`ProvisionStatus`): una BD pasa de `pending` →
`active` si el `CREATE` en el motor tiene éxito, o queda en `error` si falla. **No hay
rollback silencioso**: el registro se conserva con el detalle del error para auditoría y
reintento.

**Seguridad transversal:**
- Credenciales cifradas con **Fernet** (derivado de `SECRET_KEY`).
- **Anti-SSRF**: al registrar un servidor se rechazan hosts privados/loopback.
- **Anti-inyección**: identificadores validados contra whitelist y quoteados por dialecto.
- **Doble confirmación** en operaciones destructivas que tocan el motor (hay que repetir
  el nombre exacto del recurso).
- **Auditoría** best-effort de toda mutación; nunca almacena credenciales ni datos.

---

## 3. Convenciones de la API

### Base URL y versionado

La API está versionada como una sub-app montada en `/api/v1`. Los *health checks* viven
en la raíz, fuera del versionado.

```
https://<host>/api/v1/...      ← toda la API funcional
https://<host>/health          ← liveness (sin versión, sin auth)
https://<host>/health/ready    ← readiness (sin versión, sin auth)
```

Documentación interactiva (si `DOCS_ENABLED=true`): `GET /api/v1/docs` (Swagger) y
`GET /api/v1/redoc`.

### Envelope de respuesta

**Todas** las respuestas exitosas usan el envelope `ApiResponse[T]`:

```json
{
  "data": { },
  "message": "Texto opcional para el usuario",
  "pagination": { }
}
```

- `data` — el payload (objeto, lista, string…). Ausente en respuestas sin contenido.
- `message` — texto opcional. Ausente si no se proporciona.
- `pagination` — solo presente en listados paginados.

> Los campos con valor `null` **se omiten** del JSON. Si un endpoint no devuelve
> `message` ni `pagination`, esas claves simplemente no aparecen.

### Paginación

Los listados aceptan dos query params:

| Parámetro | Tipo | Default | Restricción |
|---|---|---|---|
| `page` | int | `1` | `>= 1` |
| `size` | int | `20` | `>= 1`, máximo **200** |

La respuesta incluye un bloque `pagination`:

```json
{
  "data": [ ],
  "pagination": {
    "page": 1,
    "size": 20,
    "total": 150,
    "pages": 8,
    "has_next": true,
    "has_prev": false
  }
}
```

### Errores

Los errores **no** usan el envelope; devuelven el status HTTP correspondiente y un cuerpo
con `detail`:

```json
{ "detail": "Servidor no encontrado." }
```

| Código | Significado en el gateway |
|---|---|
| `400` | Petición mal formada. |
| `401` | No autenticado / sesión inválida o usuario inactivo. |
| `404` | El recurso del inventario no existe. |
| `409` | Conflicto: recurso duplicado, dependencias, o validación cruzada (p. ej. owner de otro servidor); también "ya existe en el motor destino". |
| `422` | Error de validación de Pydantic (tipos/patrones) o falta una confirmación obligatoria (`confirm_name`, `confirm_username`, `password` al aprovisionar). |
| `429` | Rate limit excedido. |
| `502` | No se pudo conectar al servidor de base de datos destino. 🔌 |
| `504` | La operación en el servidor destino excedió el tiempo de espera. 🔌 |

### Autenticación

El gateway usa **sesión por cookie firmada** (httpOnly). El modelo es de **administrador
único**.

1. `POST /api/v1/auth/login` con usuario y contraseña → el servidor responde con
   `Set-Cookie` (sesión firmada).
2. En cada petición posterior, **envía la cookie**. Con `curl`, usa un *cookie jar*:
   `-c cookies.txt` para guardarla y `-b cookies.txt` para enviarla.
3. Todos los endpoints bajo `/api/v1` (excepto `login`) requieren la cookie; sin ella
   devuelven `401`.

`POST /api/v1/auth/login` está limitado a **5 peticiones por minuto** por IP. El resto de
endpoints comparten el rate limit global configurado en el servidor.

---

## 4. Tipos de datos y enums

Valores reutilizados a lo largo de la API:

| Enum | Valores permitidos | Uso |
|---|---|---|
| `EngineType` | `mysql`, `mariadb`, `postgresql` | Motor de un servidor. |
| `ServerStatus` | `active`, `inactive`, `unreachable` | Estado operativo de un servidor en el inventario. |
| `ProvisionStatus` | `pending`, `active`, `error`, `archived` | Consistencia inventario ↔ motor de una BD gestionada. `error` también marca **cuarentena** tras un fallo de migración (ver [§9](#9-bases-de-datos-gestionadas-managed-databases)). |
| `MigrationStatus` | `applied`, `failed` | Desenlace de una migración de blueprint en el historial. |
| `ssl_mode` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` | Modo TLS hacia el servidor destino. `null` o `""` ⇒ sin TLS. Se normaliza a minúsculas. |
| `GrantLevel` | `global`, `database`, `schema`, `table`, `column`, `sequence`, `routine` | Nivel de un grant (§11/§7). `schema` y `sequence` solo PostgreSQL. |

**Tipos de grants** (usados en los endpoints de permisos, §7 y §11):

- `ObjectRef` — objeto destino de un grant; los campos dependen del nivel:
  `{ database?, schema? (solo PG, default "public"), table?, columns?: list[str], sequence?, routine?: {kind: "FUNCTION"|"PROCEDURE", name} }`.
- `GrantInfo` — privilegio efectivo (respuesta de introspección):
  `{ level: GrantLevel, object?: str, privileges: list[str], with_grant_option: bool }`.
- `privileges` — lista de tokens (`SELECT`, `INSERT`, `EXECUTE`, `ALL PRIVILEGES`, …)
  validados contra el catálogo por motor y nivel; uno no soportado da `422`.

**Patrones de identificadores** (validación *fail-fast* en la API, alineada con la
whitelist anti-inyección del motor):

| Campo | Patrón | Notas |
|---|---|---|
| `username` (ServerUser) | `^[A-Za-z_][A-Za-z0-9_]{0,62}$` | Empieza por letra o `_`; hasta 63 chars. |
| `host` (ServerUser) | `^[A-Za-z0-9_.%:\-]{1,255}$` | Solo MySQL/MariaDB; `%` = wildcard. |
| `name` (ManagedDatabase) | `^[A-Za-z_][A-Za-z0-9_]{0,62}$` | Nombre de BD. |
| `charset` / `collation` | `^[A-Za-z0-9_]{1,64}$` | Solo MySQL/MariaDB. |
| `slug` (DatabaseModel) | `^[a-z0-9]+(?:[-_][a-z0-9]+)*$` | kebab/snake en minúsculas. |

Las marcas de tiempo (`created_at`, `updated_at`) son `datetime` en formato ISO 8601.

---

## 5. Autenticación (`/auth`)

Gestiona el ciclo de sesión del administrador. Es el punto de entrada obligatorio: sin
una sesión válida, el resto de la API responde `401`.

### `POST /api/v1/auth/login`

Inicia sesión y emite la cookie de sesión. **Rate limit: 5/minuto.**

**Body** (`LoginIn`):

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `username` | string | sí | 1–128 caracteres |
| `password` | string | sí | mínimo 1 carácter |

**Respuesta** `200` — `ApiResponse[AdminOut]` (`AdminOut` = `{id, username}`).

```bash
curl -X POST https://<host>/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -c cookies.txt \
  -d '{"username": "admin", "password": "s3cr3t"}'
```

```json
{ "data": { "id": 1, "username": "admin" }, "message": "Sesión iniciada." }
```

> Errores: `401` credenciales inválidas · `429` demasiados intentos.

### `POST /api/v1/auth/logout`

Cierra la sesión actual. **Requiere sesión.**

**Respuesta** `200` — `ApiResponse[None]`.

```bash
curl -X POST https://<host>/api/v1/auth/logout -b cookies.txt
```

```json
{ "message": "Sesión cerrada." }
```

### `GET /api/v1/auth/me`

Devuelve el administrador autenticado. Útil para validar la sesión. **Requiere sesión.**

**Respuesta** `200` — `ApiResponse[AdminOut]`.

```bash
curl https://<host>/api/v1/auth/me -b cookies.txt
```

```json
{ "data": { "id": 1, "username": "admin" } }
```

---

## 6. Servidores (`/servers`)

Gestiona el inventario de servidores destino y ofrece operaciones de introspección en
vivo. **Todos los endpoints requieren sesión.** La credencial pseudo-root entra en texto
plano al crear/actualizar, se cifra antes de persistir y **nunca** se devuelve.

### Schema `ServerCreate` (body de creación)

| Campo | Tipo | Requerido | Validación / valores |
|---|---|---|---|
| `name` | string | sí | 1–100 caracteres |
| `host` | string | sí | 1–255 caracteres (rechazado si es privado/loopback — anti-SSRF) |
| `port` | int | sí | 1–65535 |
| `engine` | `EngineType` | sí | `mysql` \| `mariadb` \| `postgresql` |
| `root_username` | string | sí | 1–128 caracteres |
| `root_password` | string | sí | mínimo 1 (se cifra; nunca se devuelve) |
| `ssl_mode` | string \| null | no | uno de los `ssl_mode` válidos; `null`/`""` ⇒ sin TLS |
| `notes` | string \| null | no | — |
| `is_active` | bool | no | default `true` |

`ServerUpdate` (para `PATCH`) tiene **los mismos campos, todos opcionales**. Solo se
actualizan los enviados; `root_password` omitido ⇒ no cambia.

### Schema `ServerOut` (respuesta)

```json
{
  "id": 42,
  "name": "mysql-prod",
  "host": "db.example.com",
  "port": 3306,
  "engine": "mysql",
  "root_username": "gateway_root",
  "ssl_mode": "require",
  "status": "active",
  "is_active": true,
  "notes": "Producción",
  "has_root_password": true,
  "created_at": "2026-06-23T10:00:00Z",
  "updated_at": "2026-06-23T10:00:00Z"
}
```

### Endpoints CRUD (inventario)

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/servers` | Lista paginada de servidores. |
| `POST` | `/api/v1/servers` | Registra un servidor (`201`). |
| `GET` | `/api/v1/servers/{server_id}` | Detalle de un servidor. |
| `PATCH` | `/api/v1/servers/{server_id}` | Actualiza parcialmente. |
| `DELETE` | `/api/v1/servers/{server_id}` | Elimina del inventario. |

**Crear un servidor:**

```bash
curl -X POST https://<host>/api/v1/servers -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{
        "name": "mysql-prod",
        "host": "db.example.com",
        "port": 3306,
        "engine": "mysql",
        "root_username": "gateway_root",
        "root_password": "super-secreta",
        "ssl_mode": "require"
      }'
```

```json
{ "data": { "id": 42, "name": "mysql-prod", "...": "...", "has_root_password": true },
  "message": "Servidor registrado exitosamente." }
```

> `GET /api/v1/servers/{id}` devuelve `404` si no existe. `POST` puede devolver `409`
> (host:puerto duplicado) o `422` (anti-SSRF / validación).

### Operaciones contra el destino 🔌

Estas requieren que el servidor sea alcanzable; pueden devolver `502`/`504`.

#### `POST /api/v1/servers/{server_id}/test-connection` 🔌

Verifica conectividad y actualiza el `status` del servidor. Respuesta `ConnectionInfo`:

| Campo | Tipo |
|---|---|
| `ok` | bool |
| `dialect` | string |
| `server_version` | string \| null |

```bash
curl -X POST https://<host>/api/v1/servers/42/test-connection -b cookies.txt
```

```json
{ "data": { "ok": true, "dialect": "mysql", "server_version": "8.0.36" } }
```

#### `GET /api/v1/servers/{server_id}/databases` 🔌

Lista los nombres de las bases de datos del servidor (excluye las del sistema).
Respuesta: `ApiResponse[list[str]]`.

```json
{ "data": ["app_prod", "analytics", "billing"] }
```

#### `GET /api/v1/servers/{server_id}/users` 🔌

Lista los usuarios/roles del motor. Respuesta: `ApiResponse[list[EngineUserInfo]]`
(`EngineUserInfo` = `{username, host?}`; `host` solo en MySQL/MariaDB).

```json
{ "data": [ { "username": "app_user", "host": "%" }, { "username": "readonly" } ] }
```

#### `GET /api/v1/servers/{server_id}/databases/{database}/tables` 🔌

Lista las tablas de una base de datos. Respuesta: `ApiResponse[list[str]]`.

#### `GET /api/v1/servers/{server_id}/databases/{database}/tables/{table}/schema` 🔌

Devuelve la estructura de una tabla (**nunca filas**). Respuesta `TableSchema`:

| Campo | Tipo | Detalle |
|---|---|---|
| `database` | string | — |
| `table` | string | — |
| `columns` | list[`ColumnInfo`] | `{name, type, nullable, default?, primary_key, autoincrement, comment?}` |
| `primary_key` | list[string] | columnas que forman la PK |
| `foreign_keys` | list[`ForeignKeyInfo`] | `{name?, columns[], referred_table, referred_columns[]}` |
| `indexes` | list[`IndexInfo`] | `{name, columns[], unique}` |

```bash
curl https://<host>/api/v1/servers/42/databases/app_prod/tables/users/schema -b cookies.txt
```

```json
{
  "data": {
    "database": "app_prod",
    "table": "users",
    "columns": [
      { "name": "id", "type": "INTEGER", "nullable": false, "primary_key": true, "autoincrement": true },
      { "name": "email", "type": "VARCHAR(255)", "nullable": false, "primary_key": false, "autoincrement": false }
    ],
    "primary_key": ["id"],
    "foreign_keys": [],
    "indexes": [ { "name": "ix_users_email", "columns": ["email"], "unique": true } ]
  }
}
```

#### `POST /api/v1/servers/{server_id}/grantable` 🔌

Comprueba **antes** de intentar un grant si la credencial pseudo-root del gateway puede
delegar ciertos privilegios (`WITH GRANT OPTION`). No modifica nada.

**Body** (`GrantableRequest`): `{ level: GrantLevel, object_ref: ObjectRef, privileges: list[str] }`.

**Respuesta** `200` — `ApiResponse[GrantableResult]` (`{can_grant, level, privileges}`).

```bash
curl -X POST https://<host>/api/v1/servers/42/grantable -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "level": "database", "object_ref": { "database": "app_prod" }, "privileges": ["SELECT","INSERT"] }'
```

```json
{ "data": { "can_grant": true, "level": "database", "privileges": ["SELECT","INSERT"] } }
```

---

## 7. Usuarios del motor (`/server-users`)

Gestiona los usuarios/roles reales del motor que actúan como **propietarios** de bases de
datos. Recurso de nivel superior (no anidado bajo `/servers`); se filtra con
`?server_id=`. **Todos requieren sesión.** El password se cifra y nunca se devuelve.

### Schemas

`ServerUserCreate` (body de creación):

| Campo | Tipo | Requerido | Validación / valores |
|---|---|---|---|
| `server_id` | int | sí | `>= 1` |
| `username` | string | sí | patrón `^[A-Za-z_][A-Za-z0-9_]{0,62}$` |
| `host` | string | no | default `"%"`; patrón de host; solo MySQL/MariaDB |
| `password` | string \| null | condicional | mínimo 1; **obligatorio si `?provision=true`** |
| `notes` | string \| null | no | — |
| `is_active` | bool | no | default `true` |

`ServerUserUpdate` (body de `PATCH`): `password?`, `is_active?`, `notes?`. El
`username`/`host`/`server_id` son **inmutables**.

`ServerUserOut` (respuesta):

```json
{
  "id": 7, "server_id": 42, "username": "app_user", "host": "%",
  "is_active": true, "notes": null, "has_password": true,
  "created_at": "2026-06-23T10:00:00Z", "updated_at": "2026-06-23T10:00:00Z"
}
```

### Endpoints

| Método | Ruta | Query | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/server-users` | `page`, `size`, `server_id?` | Lista paginada; filtra por servidor. |
| `POST` | `/api/v1/server-users` | `provision=false` | Crea el usuario (`201`). Con `provision=true` 🔌 ejecuta `CREATE USER`. |
| `GET` | `/api/v1/server-users/{user_id}` | — | Detalle. |
| `PATCH` | `/api/v1/server-users/{user_id}` | `provision=false` | Actualiza. Con `provision=true` 🔌 ejecuta `ALTER USER` solo si se envía nuevo `password`. |
| `DELETE` | `/api/v1/server-users/{user_id}` | `drop_remote=false`, `confirm_username?` | Elimina del inventario. Con `drop_remote=true` 🔌 ejecuta `DROP USER`. |
| `GET` | `/api/v1/server-users/{user_id}/databases` | — | Lista las BDs cuyo owner es este usuario. |
| `GET` | `/api/v1/server-users/{user_id}/grants` | `database?` (oblig. en PG) | 🔌 Permisos efectivos del usuario (introspección del motor). |
| `POST` | `/api/v1/server-users/{user_id}/grants` | — | 🔌 Otorga privilegios a un nivel/objeto. |
| `DELETE` | `/api/v1/server-users/{user_id}/grants` | `confirm_grantee?` | 🔌 Revoca privilegios (cuerpo en el `DELETE`; `cascade?` solo PG). |
| `POST` | `/api/v1/server-users/{user_id}/apply-profile/{profile_id}` | — | 🔌 Aplica un [perfil de permisos](#11-perfiles-de-permisos-permission-profiles). |
| `POST` | `/api/v1/server-users/provision` | — | 🔌 Crea + aprovisiona el usuario + aplica grants iniciales (`201`). |

**Crear y aprovisionar un usuario en el motor** 🔌:

```bash
curl -X POST "https://<host>/api/v1/server-users?provision=true" -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "server_id": 42, "username": "app_user", "host": "%", "password": "p@ss" }'
```

```json
{ "data": { "id": 7, "server_id": 42, "username": "app_user", "host": "%", "has_password": true },
  "message": "Usuario creado y aprovisionado en el motor." }
```

> Si `provision=true` y no se envía `password` ⇒ `422`. Sin `provision`, el mensaje es
> `"Usuario creado en el inventario."`.

**Eliminar usuario y borrarlo del motor** (doble confirmación) 🔌:

```bash
curl -X DELETE "https://<host>/api/v1/server-users/7?drop_remote=true&confirm_username=app_user" \
  -b cookies.txt
```

```json
{ "message": "Usuario eliminado." }
```

> `drop_remote=true` exige que `confirm_username` coincida **exactamente** con el
> username (si no, `422`). Un usuario que posee BDs no puede eliminarse hasta reasignar o
> borrar esas BDs (regla `RESTRICT` ⇒ `409`).

### Grants granulares 🔌

Otorgan/revocan/consultan privilegios del usuario a cualquier nivel (`database`, `table`,
`column`, …). Operan contra el motor destino. Diferencias por motor: MySQL/MariaDB usan
`global/database/table/column/routine` y el `host` del usuario; PostgreSQL añade
`schema`/`sequence`, ignora el `host`, y **requiere `?database=`** al consultar grants de
objeto.

#### `GET /api/v1/server-users/{user_id}/grants`

Lee los permisos efectivos del usuario del motor real. **Query:** `database?` (obligatorio
en PostgreSQL para tablas/columnas/secuencias/rutinas). Respuesta `ApiResponse[list[GrantInfo]]`.

```bash
curl -b cookies.txt https://<host>/api/v1/server-users/7/grants
```
```json
{ "data": [
  { "level": "database", "object": "app_prod", "privileges": ["DELETE","INSERT","SELECT","UPDATE"], "with_grant_option": false },
  { "level": "table", "object": "app_prod.items", "privileges": ["SELECT"], "with_grant_option": false }
] }
```

#### `POST /api/v1/server-users/{user_id}/grants`

Otorga privilegios. Pre-chequea `can_grant` (→ `403` si la credencial del gateway no puede
delegar). **Body** (`GrantRequest`): `{ level, object_ref, privileges: list[str], with_grant_option?: bool }`.

```bash
curl -b cookies.txt -X POST https://<host>/api/v1/server-users/7/grants \
  -H "Content-Type: application/json" \
  -d '{ "level": "database", "object_ref": { "database": "app_prod" },
        "privileges": ["SELECT","INSERT","UPDATE","DELETE"] }'
```
```json
{ "data": { "granted": true, "level": "database",
            "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "with_grant_option": false },
  "message": "Privilegio(s) otorgado(s): SELECT, INSERT, UPDATE, DELETE a nivel database." }
```

#### `DELETE /api/v1/server-users/{user_id}/grants`

Revoca privilegios. **Body** (`RevokeRequest`): `{ level, object_ref, privileges: list[str], cascade?: bool }`.
**Query**: `confirm_grantee` (str) — obligatorio si `cascade=true`: repetir el username del grantee.
Respuesta `ApiResponse[None]`.

- `409` si el `grantee` es la propia credencial del gateway (anti auto-lockout).
- `cascade=true` solo en PostgreSQL (revoca privilegios re-delegados); en MySQL/MariaDB → `422`.
- Sin `confirm_grantee` cuando `cascade=true` → `422`.

```bash
# REVOKE simple
curl -b cookies.txt -X DELETE https://<host>/api/v1/server-users/7/grants \
  -H "Content-Type: application/json" \
  -d '{ "level": "table", "object_ref": { "database": "app_prod", "table": "items" }, "privileges": ["DELETE"] }'

# REVOKE ... CASCADE (PostgreSQL) — exige confirmación
curl -b cookies.txt -X DELETE "https://<host>/api/v1/server-users/7/grants?confirm_grantee=analista" \
  -H "Content-Type: application/json" \
  -d '{ "level": "table", "object_ref": { "database": "app_prod", "schema": "public", "table": "items" }, "privileges": ["SELECT"], "cascade": true }'
```

#### `POST /api/v1/server-users/{user_id}/apply-profile/{profile_id}`

Aplica un [perfil de permisos](#11-perfiles-de-permisos-permission-profiles) al usuario.
**Body** (`ApplyProfileRequest`): `{ object_mappings: [{ level, object_ref }] }` (un mapeo
por cada nivel del perfil que quieras aplicar; los sin mapeo se omiten). Best-effort: un
grant que falle no aborta los demás. Respuesta `ApiResponse[ApplyProfileResult]`
(`{profile_id, profile_name, engine, grants_applied, skipped_levels[], errors[]}`).

#### `POST /api/v1/server-users/provision`

Endpoint unificado: crea el usuario, lo aprovisiona (`CREATE USER`) y aplica
`initial_grants`, todo en una llamada (`201`). **Body** (`ServerUserFullCreate`): campos de
`ServerUserCreate` + `initial_grants: [{ level, object_ref, privileges[], with_grant_option? }]`.
Respuesta `ApiResponse[ServerUserFullOut]` (`{user, grants_applied, grant_results[]}`).

```bash
curl -b cookies.txt -X POST https://<host>/api/v1/server-users/provision \
  -H "Content-Type: application/json" \
  -d '{ "server_id": 42, "username": "app_user", "host": "%", "password": "p@ss",
        "initial_grants": [ { "level": "database", "object_ref": { "database": "app_prod" },
          "privileges": ["SELECT","INSERT","UPDATE","DELETE"] } ] }'
```
```json
{ "data": {
    "user": { "id": 7, "server_id": 42, "username": "app_user", "host": "%", "has_password": true },
    "grants_applied": 1,
    "grant_results": [ { "level": "database", "object": "app_prod",
      "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "success": true, "error": null } ] },
  "message": "Usuario 'app_user' aprovisionado. 1 grant(s) aplicado(s)." }
```

---

## 8. Blueprints de BD (`/database-models`)

Gestiona *blueprints*/categorías lógicas de bases de datos. **CRUD puro de inventario; no
toca ningún motor.** Requiere sesión.

### Schemas

`DatabaseModelCreate`:

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `name` | string | sí | 1–100 caracteres |
| `slug` | string | sí | 1–120, patrón `^[a-z0-9]+(?:[-_][a-z0-9]+)*$` |
| `description` | string \| null | no | — |
| `current_version` | string | no | default `"0.0.0"`, máx 50 |
| `is_active` | bool | no | default `true` |

`DatabaseModelUpdate`: mismos campos, **todos opcionales**.

`DatabaseModelOut`: `{id, name, slug, description?, current_version, is_active, created_at, updated_at}`.

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/database-models` | Lista paginada. |
| `POST` | `/api/v1/database-models` | Crea (`201`). |
| `GET` | `/api/v1/database-models/{model_id}` | Detalle. |
| `PATCH` | `/api/v1/database-models/{model_id}` | Actualiza. |
| `DELETE` | `/api/v1/database-models/{model_id}` | Elimina. |
| `GET` | `/api/v1/database-models/{model_id}/databases` | BDs que replican este blueprint. |

```bash
curl -X POST https://<host>/api/v1/database-models -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "name": "WhatsApp", "slug": "whatsapp", "current_version": "1.2.0" }'
```

```json
{ "data": { "id": 3, "name": "WhatsApp", "slug": "whatsapp", "current_version": "1.2.0", "is_active": true },
  "message": "Blueprint creado." }
```

### Migraciones del blueprint (versionado de esquema) 🔧

Un blueprint puede tener una **secuencia versionada de migraciones SQL** (deltas) que el
gateway aplica a las BDs que lo replican. Estos endpoints son **CRUD de inventario** (no
tocan ningún motor); la aplicación real sobre cada BD vive en [§9](#9-bases-de-datos-gestionadas-managed-databases).
El SQL se escribe en estilo **MySQL de referencia** y el gateway lo **auto-traduce** a
PostgreSQL con `sqlglot` (campo calculado `translated`); puedes sobrescribir la traducción
con overrides manuales. Detalle conceptual: [feature doc](features/model-migrations.md).

#### Schemas

`ModelMigrationCreate` (body de creación):

| Campo | Tipo | Requerido | Validación / valores |
|---|---|---|---|
| `version` | string | sí | patrón `^\d{4,10}$` (solo dígitos). **Se ordena NUMÉRICAMENTE** — mantén ancho consistente (`0001`, `0002`…) |
| `name` | string | sí | 1–200 caracteres |
| `up_sql` | string | sí | delta SQL base (estilo MySQL); 1–262144 chars (256 KB) |
| `up_sql_mysql` | string \| null | no | override manual MySQL/MariaDB; ≤256 KB |
| `up_sql_postgresql` | string \| null | no | override manual PostgreSQL; ≤256 KB |
| `down_sql` | string \| null | no | rollback **confirmado**; ≤256 KB. Sin él, el rollback responde `409` |

`ModelMigrationPatch` (body de `PATCH`): `name?`, `down_sql?`, `up_sql_mysql?`,
`up_sql_postgresql?`. **No** se puede modificar el SQL de una migración ya aplicada en
alguna BD (`409`).

`ModelMigrationOut` (detalle): `{ id, model_id, version, name, up_sql, up_sql_mysql?,
up_sql_postgresql?, down_sql?, down_sql_suggested?, translated: {mysql, postgresql}, checksum,
created_at, updated_at }`. `down_sql_suggested` es un rollback **auto-generado** para
operaciones aditivas (revísalo y confírmalo con `PATCH`).

`ModelMigrationSummary` (item de listado): `{ id, model_id, version, name,
has_mysql_override, has_postgresql_override, has_rollback, checksum, created_at }`.

#### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/database-models/{model_id}/migrations` | Lista paginada (resúmenes). |
| `POST` | `/api/v1/database-models/{model_id}/migrations` | Crea una migración (`201`). Devuelve `translated` + `down_sql_suggested`. |
| `GET` | `/api/v1/database-models/{model_id}/migrations/{version}` | Detalle completo. |
| `PATCH` | `/api/v1/database-models/{model_id}/migrations/{version}` | Confirma `down_sql` / añade overrides. |
| `DELETE` | `/api/v1/database-models/{model_id}/migrations/{version}` | Elimina (solo si **no** tiene historial de aplicación; si no, `409`). |
| `POST` | `/api/v1/database-models/{model_id}/migrations/apply-all` 🔌 | Aplica a **todas** las BDs del blueprint. Rate limit **3/min**. |

**Crear una migración:**

```bash
curl -X POST https://<host>/api/v1/database-models/3/migrations -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "version": "0001", "name": "Esquema inicial",
        "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)" }'
```

```json
{ "data": {
    "version": "0001", "name": "Esquema inicial",
    "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
    "down_sql": null,
    "down_sql_suggested": "DROP TABLE IF EXISTS orders;",
    "translated": {
      "mysql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
      "postgresql": "CREATE TABLE orders (id INT GENERATED BY DEFAULT AS IDENTITY NOT NULL PRIMARY KEY, total INT)"
    },
    "checksum": "…" },
  "message": "Migración creada." }
```

> Cuándo escribir `up_sql_postgresql` manual: `ENUM(...)` inline, `ON UPDATE CURRENT_TIMESTAMP`,
> `UNSIGNED/ZEROFILL`, `ALTER … MODIFY … AUTO_INCREMENT` y rutinas `BEGIN…END` con `;`
> internos no se traducen de forma fiable. `AUTO_INCREMENT`, backticks y `DATETIME` sí.

**Confirmar el rollback sugerido:**

```bash
curl -X PATCH https://<host>/api/v1/database-models/3/migrations/0001 -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "down_sql": "DROP TABLE IF EXISTS orders" }'
```

**Aplicación masiva (a todas las BDs del blueprint)** 🔌:

```bash
curl -X POST "https://<host>/api/v1/database-models/3/migrations/apply-all?max_databases=10" \
  -b cookies.txt
```

| Query (apply-all) | Tipo | Default | Detalle |
|---|---|---|---|
| `max_databases` | int | `10` | `1..100`. Cota de BDs a procesar por llamada (síncrono). |
| `force` | bool | `false` | Override de cuarentena en cada BD (ver §9). |
| `dry_run` | bool | `false` | No aplica: devuelve el plan (pendientes) por BD. |

```json
{ "data": { "model_id": 3, "total_databases": 12, "processed": 10,
    "results": [ { "managed_database_id": 5, "database_name": "app_a", "server_id": 1,
                   "ok": true, "applied": [ { "version": "0001", "status": "applied", "execution_ms": 42 } ] } ] },
  "message": "Aplicación masiva ejecutada." }
```

> Continúa con las demás BDs aunque una falle (cada `result` trae `ok`/`error`). El
> fan-out asíncrono real (jobs) es del Plan 06; hoy es síncrono y acotado por `max_databases`.

---

## 9. Bases de datos gestionadas (`/managed-databases`)

Gestiona bases de datos reales en los servidores destino. Cada BD pertenece a un
`ServerUser` (owner) del **mismo servidor**. Requiere sesión.

### Schemas

`ManagedDatabaseCreate` (body de creación):

| Campo | Tipo | Requerido | Validación / valores |
|---|---|---|---|
| `name` | string | sí | patrón `^[A-Za-z_][A-Za-z0-9_]{0,62}$` |
| `server_id` | int | sí | `>= 1` |
| `owner_id` | int | sí | `>= 1`; debe ser un `ServerUser` del mismo `server_id` |
| `model_id` | int \| null | no | `>= 1` (blueprint) |
| `model_version` | string \| null | no | máx 50 |
| `charset` | string \| null | no | patrón charset; MySQL/MariaDB |
| `collation` | string \| null | no | patrón charset; MySQL/MariaDB |
| `notes` | string \| null | no | — |

`ManagedDatabaseUpdate` (body de `PATCH`): `model_id?`, `model_version?`, `charset?`,
`collation?`, `notes?`. El `name`/`server_id`/`owner_id` **no** se editan aquí (para owner
usa `reassign-owner`).

`ReassignOwnerIn`: `{ "owner_id": int }` (nuevo propietario, mismo servidor).

`ManagedDatabaseOut` (respuesta):

```json
{
  "id": 11, "name": "app_prod", "server_id": 42, "owner_id": 7,
  "model_id": 3, "model_version": "1.2.0",
  "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci",
  "status": "active", "notes": null,
  "created_at": "2026-06-23T10:00:00Z", "updated_at": "2026-06-23T10:00:00Z"
}
```

### Endpoints

| Método | Ruta | Query | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/managed-databases` | `page`, `size`, `server_id?`, `owner_id?`, `model_id?`, `status?` | Lista paginada con filtros (`status` ∈ `ProvisionStatus`). |
| `POST` | `/api/v1/managed-databases` | `provision=false` | Registra (`201`, status `pending`). Con `provision=true` 🔌 ejecuta `CREATE DATABASE` + `GRANT` al owner. |
| `GET` | `/api/v1/managed-databases/{db_id}` | — | Detalle. |
| `PATCH` | `/api/v1/managed-databases/{db_id}` | — | Actualiza metadata. |
| `DELETE` | `/api/v1/managed-databases/{db_id}` | `drop_remote=false`, `confirm_name?` | Elimina del inventario. Con `drop_remote=true` 🔌 ejecuta `DROP DATABASE`. |
| `POST` | `/api/v1/managed-databases/{db_id}/reassign-owner` | `provision=false` | Cambia el owner. Con `provision=true` 🔌 revoca/otorga (o `ALTER OWNER` en PG). |

**Crear y aprovisionar una BD** 🔌:

```bash
curl -X POST "https://<host>/api/v1/managed-databases?provision=true" -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{
        "name": "app_prod",
        "server_id": 42,
        "owner_id": 7,
        "model_id": 3,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci"
      }'
```

```json
{ "data": { "id": 11, "name": "app_prod", "status": "active", "owner_id": 7 },
  "message": "Base de datos creada y aprovisionada en el motor." }
```

> Si `owner_id` no pertenece al `server_id` indicado ⇒ `409`. Si el `CREATE` en el motor
> falla, la BD queda con `status: "error"` y el detalle en `notes` (sin rollback). Sin
> `provision`, la BD queda en `status: "pending"` y el mensaje es
> `"Base de datos registrada en el inventario."`.

**Eliminar BD del motor** (doble confirmación) 🔌:

```bash
curl -X DELETE "https://<host>/api/v1/managed-databases/11?drop_remote=true&confirm_name=app_prod" \
  -b cookies.txt
```

```json
{ "message": "Base de datos eliminada." }
```

> `drop_remote=true` exige que `confirm_name` coincida **exactamente** con el nombre de la
> BD (si no, `422`).

**Reasignar propietario** 🔌:

```bash
curl -X POST "https://<host>/api/v1/managed-databases/11/reassign-owner?provision=true" \
  -b cookies.txt -H "Content-Type: application/json" \
  -d '{ "owner_id": 9 }'
```

```json
{ "data": { "id": 11, "owner_id": 9, "...": "..." }, "message": "Propietario reasignado." }
```

### Migraciones sobre la BD gestionada 🔌

Aplica/revierte/consulta las migraciones del [blueprint](#8-blueprints-de-bd-database-models)
asignado (`model_id`) **sobre esta BD real**. La versión actual de cada BD la mantiene el
gateway dentro de la propia BD destino (tabla `_gw_v_{slug}`, gestionada con Alembic
embebido); el historial queda en el gateway. Requieren que la BD tenga un blueprint
asignado (`422` si no). Rate limit **10/min** en `apply`/`rollback`/`stamp`.

| Método | Ruta | Query | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/managed-databases/{db_id}/migrations/status` | — | Versión actual vs. pendientes. |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/apply` | `version?`, `force?`, `dry_run?` | Aplica las pendientes (o hasta `version`). |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/rollback` | `confirm_version` (**obligatorio**) | Revierte la última aplicada. |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/stamp` | `version` (**obligatorio**) | Marca una versión **sin ejecutar SQL** (BDs pre-existentes). |
| `GET` | `/api/v1/managed-databases/{db_id}/migrations/history` | `page`, `size` | Historial paginado de aplicaciones. |

**Query params de `apply`:**

| Parámetro | Tipo | Default | Detalle |
|---|---|---|---|
| `version` | string \| null | — | Patrón `^\d{4,10}$`. Aplica solo **hasta** esa versión (inclusive). |
| `force` | bool | `false` | Reintenta una BD en **cuarentena** (tras inspección). |
| `dry_run` | bool | `false` | No aplica: devuelve `current_version` + `pending_versions`. |

**Estado (`status`)** — `MigrationStatusOut`:

```bash
curl https://<host>/api/v1/managed-databases/5/migrations/status -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "model_id": 3, "slug": "whatsapp",
            "current_version": null, "latest_available": "0002",
            "pending_count": 2, "pending_versions": ["0001","0002"] } }
```

**Previsualizar (dry-run):**

```bash
curl -X POST "https://<host>/api/v1/managed-databases/5/migrations/apply?dry_run=true" -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "database_name": "app_prod", "server_id": 42,
            "dry_run": true, "current_version": null,
            "pending_versions": ["0001","0002"], "pending_count": 2 } }
```

**Aplicar:**

```bash
curl -X POST https://<host>/api/v1/managed-databases/5/migrations/apply -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "database_name": "app_prod", "server_id": 42,
            "applied_count": 2, "failed": false, "quarantined": false,
            "results": [ { "migration_id": 1, "version": "0001", "status": "applied", "error": null, "execution_ms": 42 },
                         { "migration_id": 2, "version": "0002", "status": "applied", "error": null, "execution_ms": 31 } ] },
  "message": "Migraciones aplicadas." }
```

**Rollback (DESTRUCTIVO — doble confirmación):** `confirm_version` debe igualar la versión
actual de la BD; si no, `422`. Si esa versión no tiene `down_sql` confirmado, `409`.

```bash
curl -X POST "https://<host>/api/v1/managed-databases/5/migrations/rollback?confirm_version=0002" -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "rolled_back_version": "0002", "current_version": "0001",
            "result": { "migration_id": 2, "version": "0002", "status": "applied", "execution_ms": 28 } },
  "message": "Rollback ejecutado." }
```

**Historial:**

```json
{ "data": [ { "id": 10, "managed_database_id": 5, "model_migration_id": 2, "version": "0002",
              "applied_at": "2026-06-26T10:00:00Z", "status": "applied", "error": null, "execution_ms": 31 } ],
  "pagination": { "page": 1, "size": 20, "total": 2, "...": "..." } }
```

> **Cuarentena:** como el DDL no es transaccional en MySQL/MariaDB, una migración
> multi-sentencia que falla a mitad puede dejar estado parcial. La BD queda en
> `status: "error"` (con detalle en `notes`) y el siguiente `apply` responde `409` hasta
> que inspecciones y reintentes con `?force=true`; un `apply` exitoso limpia la cuarentena.
> Recomendación: escribe migraciones **idempotentes** (`CREATE TABLE IF NOT EXISTS`, …).
> El gateway re-valida el `checksum` antes de aplicar: si la migración fue alterada en la
> BD del gateway, responde `409`.

---

## 10. Catálogo de privilegios (`/privileges`)

Consulta y activa/desactiva los privilegios que la plataforma controla por motor.
Requiere sesión. **No toca ningún motor** (es un catálogo del gateway).

### Endpoints

#### `GET /api/v1/privileges`

| Query | Tipo | Descripción |
|---|---|---|
| `engine` | string \| null | `mysql` \| `mariadb` \| `postgresql` |
| `active` | bool \| null | `true` = solo los privilegios que la plataforma controla |

Respuesta `ApiResponse[list[PrivilegeOut]]` (**no paginada**). `PrivilegeOut`:
`{id, engine, name, category, context?, description, is_sensitive, is_active, created_at, updated_at}`.

```bash
curl "https://<host>/api/v1/privileges?engine=mysql&active=true" -b cookies.txt
```

```json
{ "data": [
  { "id": 1, "engine": "mysql", "name": "SELECT", "category": "object",
    "description": "Leer filas", "is_sensitive": false, "is_active": true,
    "created_at": "…", "updated_at": "…" }
] }
```

#### `PATCH /api/v1/privileges/{privilege_id}`

**Body** (`PrivilegeUpdate`): `{ "is_active": bool }`.

```bash
curl -X PATCH https://<host>/api/v1/privileges/1 -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "is_active": false }'
```

```json
{ "data": { "id": 1, "is_active": false, "...": "..." }, "message": "Privilegio desactivado." }
```

---

## 11. Perfiles de permisos (`/permission-profiles`)

Plantillas de privilegios **por motor**, reutilizables para aplicar a usuarios con
[`apply-profile`](#7-usuarios-del-motor-server-users). CRUD puro de inventario; **no toca
ningún motor**. Requiere sesión.

### Schemas

`PermissionProfileCreate`:

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `name` | string | sí | 1–100 caracteres |
| `engine` | `EngineType` | sí | `mysql` \| `mariadb` \| `postgresql` |
| `description` | string \| null | no | máx 255 |
| `items` | list | sí | mínimo 1; cada item: `{ level: GrantLevel, privileges: list[str] }` |

`PermissionProfileUpdate`: `name?`, `description?`, `is_active?`, `items?`. El `engine` es
**inmutable**; si envías `items`, **reemplazan** por completo los anteriores.

`PermissionProfileOut`: `{ id, name, engine, description?, is_active, items[], created_at, updated_at }`
donde cada item de salida es `{ level, privileges[], requires_confirmation }`.

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/permission-profiles` | Lista (filtros `?engine=`, `?active=`). **No paginada.** |
| `POST` | `/api/v1/permission-profiles` | Crea (`201`). |
| `GET` | `/api/v1/permission-profiles/{profile_id}` | Detalle. |
| `PATCH` | `/api/v1/permission-profiles/{profile_id}` | Actualiza (items reemplazan). |
| `DELETE` | `/api/v1/permission-profiles/{profile_id}` | Elimina. |

```bash
curl -b cookies.txt -X POST https://<host>/api/v1/permission-profiles \
  -H "Content-Type: application/json" \
  -d '{ "name": "app-readwrite", "engine": "mysql",
        "items": [ { "level": "database", "privileges": ["SELECT","INSERT","UPDATE","DELETE"] } ] }'
```

```json
{ "data": { "id": 3, "name": "app-readwrite", "engine": "mysql", "is_active": true,
            "items": [ { "level": "database",
              "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "requires_confirmation": false } ] },
  "message": "Perfil de permisos creado." }
```

---

## 12. Administración: cifrado (`/admin/crypto`)

Operaciones de administración del cifrado de credenciales. Requiere sesión. No toca los
motores destino (opera sobre la BD de metadatos).

### `POST /api/v1/admin/crypto/rotate`

Rota la **clave de datos (DEK)** y **re-cifra todas las credenciales** almacenadas
(servidores y usuarios), **sin cambiar `SECRET_KEY` ni reiniciar** la aplicación.

**Body:** ninguno. **Respuesta** `200` — `ApiResponse[CryptoRotationOut]`
(`{servers_reencrypted, server_users_reencrypted}`).

```bash
curl -b cookies.txt -X POST https://<host>/api/v1/admin/crypto/rotate
```

```json
{ "data": { "servers_reencrypted": 12, "server_users_reencrypted": 30 },
  "message": "Clave de cifrado rotada; credenciales re-cifradas." }
```

---

## 13. Health checks

No versionados, **sin autenticación**, sin envelope `ApiResponse`. Pensados para
*probes* de Docker/Kubernetes.

### `GET /health` — liveness

Confirma que el proceso responde. No comprueba dependencias. Siempre `200`.

```json
{ "status": "ok", "service": "<APP_NAME>", "environment": "production" }
```

### `GET /health/ready` — readiness

Comprueba que la BD de metadatos es alcanzable (`SELECT 1`). Devuelve `200` si está lista
o `503` si no.

```json
{ "status": "ready", "service": "<APP_NAME>", "environment": "production" }
```

```json
// 503 Service Unavailable
{ "status": "unavailable", "service": "<APP_NAME>", "environment": "production",
  "detail": "metadata database unreachable" }
```

---

## 14. Estado del proyecto

| Iteración | Estado | Alcance |
|---|---|---|
| **1** | ✅ Completada | Infra FastAPI, modelo `Server` + CRUD, cifrado Fernet, conexión remota, introspección, anti-SSRF, anti-inyección, rate limiting, auditoría base. |
| **2** | ✅ Completada | `ServerUser`, `DatabaseModel`, `ManagedDatabase`, `Privilege`; aprovisionamiento `CREATE/DROP` USER y DATABASE, `GRANT/REVOKE`, reasignación de owner, doble confirmación, catálogo de privilegios. |
| **2+** | ✅ Completada | Gestión granular de permisos (grants por nivel, introspección de permisos efectivos, perfiles de permisos reutilizables, creación unificada usuario+grants) y rotación de cifrado (DEK). |
| **3 (Plan 02)** | ✅ Completada | **Migraciones de blueprints**: deltas SQL versionados por blueprint, aplicación/rollback/stamp/historial por BD, dry-run, apply-all, auto-traducción cross-engine y cuarentena. Verificado e2e en MySQL 8 / MariaDB 11 / PostgreSQL 16. |
| **3+** | ⏳ Pendiente | Aprovisionamiento de servidores (Terraform/SSH), clonado de BDs, observabilidad/SSO, *production readiness*. Ver `docs/plans/`. |

> Los endpoints `/api/v1/test/*` que pueda exponer la app son **ejemplos de demostración
> del template** y no forman parte de la API funcional del gateway; no están documentados
> aquí.

---

## 15. Flujos de integración (orden de llamadas)

Cada flujo lista la secuencia de endpoints y sus dependencias. Recuerda enviar la cookie
de sesión en cada llamada (paso A).

### A. Autenticarse (siempre primero)

```
POST /api/v1/auth/login        → guarda la cookie (Set-Cookie)
GET  /api/v1/auth/me           → (opcional) valida la sesión
…                              → usa la cookie en todas las llamadas
POST /api/v1/auth/logout       → al terminar
```

### B. Registrar y validar un servidor

```
1. POST /api/v1/servers                         → crea el servidor (cifra la pseudo-root)
2. POST /api/v1/servers/{id}/test-connection 🔌 → verifica conectividad; fija status
```

Depende de: sesión activa. Salida: un `server_id` utilizable por los demás recursos.

### C. Inspeccionar la estructura de un servidor (solo lectura)

```
1. GET /api/v1/servers/{id}/databases 🔌                              → elige una BD
2. GET /api/v1/servers/{id}/databases/{db}/tables 🔌                  → elige una tabla
3. GET /api/v1/servers/{id}/databases/{db}/tables/{t}/schema 🔌       → columnas/PK/FK/índices
   (GET /api/v1/servers/{id}/users 🔌 lista los usuarios del motor)
```

Depende de: un servidor alcanzable (paso B exitoso).

### D. Aprovisionar un usuario y una base de datos (flujo principal)

```
1. POST /api/v1/server-users?provision=true 🔌
      body: { server_id, username, host?, password }      → crea el usuario (owner)
2. POST /api/v1/managed-databases?provision=true 🔌
      body: { name, server_id, owner_id, charset?, collation?, model_id? }
                                                           → CREATE DATABASE + GRANT al owner
```

Dependencias y reglas:
- El `owner_id` del paso 2 es el `id` devuelto en el paso 1, y **debe** pertenecer al
  mismo `server_id` (si no, `409`).
- Con `provision=true`, `password` es obligatorio en el paso 1 (`422` si falta).
- Si el `CREATE` del paso 2 falla, la BD queda en `status: "error"` (revisa `notes`).
- Para registrar sin tocar el motor todavía, usa `provision=false`: la BD queda en
  `pending` y puedes aprovisionarla más adelante.

### E. Reasignar el propietario de una BD

```
POST /api/v1/managed-databases/{db_id}/reassign-owner?provision=true 🔌
     body: { owner_id: <nuevo_owner> }   → revoca al anterior y otorga al nuevo (o ALTER OWNER en PG)
```

El nuevo owner debe ser un `ServerUser` del mismo servidor (`409` si no).

### F. Borrado seguro

```
# Para borrar un usuario que posee BDs, primero libéralas:
1. (reasignar con E)  o  (borrar las BDs con paso 2)
2. DELETE /api/v1/managed-databases/{db_id}?drop_remote=true&confirm_name=<name> 🔌
3. DELETE /api/v1/server-users/{user_id}?drop_remote=true&confirm_username=<username> 🔌
```

Reglas:
- Un `ServerUser` con BDs no se puede borrar (`RESTRICT` ⇒ `409`): reasigna o borra sus
  BDs primero.
- `drop_remote=true` exige repetir el nombre exacto (`confirm_name` / `confirm_username`),
  de lo contrario `422`.
- Sin `drop_remote`, solo se borra del inventario y el objeto sigue existiendo en el motor.

### G. Blueprints y catálogo de privilegios (auxiliares, solo inventario)

```
POST /api/v1/database-models                 → define un blueprint reutilizable
GET  /api/v1/database-models/{id}/databases  → BDs que lo replican
GET  /api/v1/privileges?engine=…&active=true → privilegios que la plataforma controla
PATCH /api/v1/privileges/{id}                → activa/desactiva un privilegio
```

Puedes referenciar `model_id` al crear una BD (paso D) para asociarla a un blueprint.

### H. Gestión de permisos de un usuario

```
# Rápido: usuario + grants en una sola llamada
POST /api/v1/server-users/provision 🔌
     body: { server_id, username, password, initial_grants:[{level, object_ref, privileges}] }

# Paso a paso:
1. (opcional) POST /api/v1/servers/{id}/grantable 🔌      → ¿puedo delegar estos privilegios?
2. POST   /api/v1/server-users/{user_id}/grants 🔌        → otorga
3. GET    /api/v1/server-users/{user_id}/grants 🔌        → verifica permisos efectivos (PG: ?database=)
4. DELETE /api/v1/server-users/{user_id}/grants 🔌        → revoca

# Con perfiles reutilizables:
A. POST /api/v1/permission-profiles                       → plantilla por motor
B. POST /api/v1/server-users/{user_id}/apply-profile/{profile_id} 🔌
        body: { object_mappings:[{level, object_ref}] }   → aplica la plantilla
```

Reglas: los grants operan sobre un `ServerUser` ya registrado (créalo primero o usa
`/provision`); en PostgreSQL pasa `?database=` para grants de objeto; el motor del perfil
debe coincidir con el del servidor (`422`); `grantable`/`grants POST` devuelven `403` si la
credencial pseudo-root no puede delegar (`WITH GRANT OPTION`).

### I. Versionar un blueprint y aplicarlo a sus BDs (migraciones)

Permite definir el esquema de un blueprint como una secuencia de deltas SQL y aplicarlo a
las BDs que lo replican. Depende de: un blueprint (paso G) y BDs creadas con ese `model_id`
(paso D con `model_id`).

```
# 1) Definir las migraciones del blueprint (inventario; no toca motores)
POST  /api/v1/database-models/{model_id}/migrations
      body: { version:"0001", name, up_sql }          → devuelve translated + down_sql_suggested
PATCH /api/v1/database-models/{model_id}/migrations/0001
      body: { down_sql: "<rollback confirmado>" }      → (opcional) habilita el rollback

# 2) Previsualizar y aplicar sobre UNA BD que use el blueprint
GET   /api/v1/managed-databases/{db_id}/migrations/status            → current vs pendientes
POST  /api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true → plan sin ejecutar
POST  /api/v1/managed-databases/{db_id}/migrations/apply 🔌          → aplica las pendientes
GET   /api/v1/managed-databases/{db_id}/migrations/history           → auditoría del resultado

# 3) (opcional) Revertir o aplicar a TODAS las BDs del blueprint
POST  /api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version=0002 🔌
POST  /api/v1/database-models/{model_id}/migrations/apply-all 🔌     → fan-out a N BDs
```

Dependencias y reglas:
- La BD debe tener `model_id` asignado (al crearla en el paso D, o vía `PATCH`); si no, los
  endpoints de migración responden `422`.
- El orden de versiones es **numérico**: `apply` recorre las pendientes en orden ascendente
  y se detiene en la primera que falle.
- `rollback` es destructivo: exige `?confirm_version=` = versión actual (`422` si no) y que
  esa versión tenga `down_sql` confirmado (`409` si no).
- **Recuperación de fallo:** si una migración falla, la BD entra en **cuarentena**
  (`status: "error"`); inspecciona el estado real y reintenta con `?force=true`. Diseña las
  migraciones idempotentes para que un reintento sea seguro.
- `stamp` marca una BD pre-existente en una versión **sin ejecutar SQL** (cuando el esquema
  ya existe pero el gateway aún no lo registra).

Escenario real (frontend): un wizard de "publicar nueva versión de esquema" llamaría
`POST …/migrations` (subir el delta) → mostrar `translated`/`down_sql_suggested` para
revisión → `PATCH` (confirmar rollback) → por cada BD afectada, `…/migrations/apply?dry_run=true`
(preview) → `…/migrations/apply` (confirmar) → `…/migrations/history` (resultado). Para
desplegar a toda una familia de BDs, `…/migrations/apply-all`.

---

## 16. Apéndice: tabla resumen de endpoints

> 🔌 = toca el servidor de BD destino · 🔒 = requiere sesión

| # | Método | Ruta | Auth | Motor |
|---|---|---|---|---|
| 1 | GET | `/health` | — | — |
| 2 | GET | `/health/ready` | — | — |
| 3 | POST | `/api/v1/auth/login` | — | — |
| 4 | POST | `/api/v1/auth/logout` | 🔒 | — |
| 5 | GET | `/api/v1/auth/me` | 🔒 | — |
| 6 | GET | `/api/v1/servers` | 🔒 | — |
| 7 | POST | `/api/v1/servers` | 🔒 | — |
| 8 | GET | `/api/v1/servers/{server_id}` | 🔒 | — |
| 9 | PATCH | `/api/v1/servers/{server_id}` | 🔒 | — |
| 10 | DELETE | `/api/v1/servers/{server_id}` | 🔒 | — |
| 11 | POST | `/api/v1/servers/{server_id}/test-connection` | 🔒 | 🔌 |
| 12 | GET | `/api/v1/servers/{server_id}/databases` | 🔒 | 🔌 |
| 13 | GET | `/api/v1/servers/{server_id}/users` | 🔒 | 🔌 |
| 14 | GET | `/api/v1/servers/{server_id}/databases/{database}/tables` | 🔒 | 🔌 |
| 15 | GET | `/api/v1/servers/{server_id}/databases/{database}/tables/{table}/schema` | 🔒 | 🔌 |
| 16 | POST | `/api/v1/servers/{server_id}/grantable` | 🔒 | 🔌 |
| 17 | GET | `/api/v1/server-users` | 🔒 | — |
| 18 | POST | `/api/v1/server-users` (`?provision`) | 🔒 | 🔌* |
| 19 | GET | `/api/v1/server-users/{user_id}` | 🔒 | — |
| 20 | PATCH | `/api/v1/server-users/{user_id}` (`?provision`) | 🔒 | 🔌* |
| 21 | DELETE | `/api/v1/server-users/{user_id}` (`?drop_remote`) | 🔒 | 🔌* |
| 22 | GET | `/api/v1/server-users/{user_id}/databases` | 🔒 | — |
| 23 | GET | `/api/v1/server-users/{user_id}/grants` | 🔒 | 🔌 |
| 24 | POST | `/api/v1/server-users/{user_id}/grants` | 🔒 | 🔌 |
| 25 | DELETE | `/api/v1/server-users/{user_id}/grants` | 🔒 | 🔌 |
| 26 | POST | `/api/v1/server-users/{user_id}/apply-profile/{profile_id}` | 🔒 | 🔌 |
| 27 | POST | `/api/v1/server-users/provision` | 🔒 | 🔌 |
| 28 | GET | `/api/v1/database-models` | 🔒 | — |
| 29 | POST | `/api/v1/database-models` | 🔒 | — |
| 30 | GET | `/api/v1/database-models/{model_id}` | 🔒 | — |
| 31 | PATCH | `/api/v1/database-models/{model_id}` | 🔒 | — |
| 32 | DELETE | `/api/v1/database-models/{model_id}` | 🔒 | — |
| 33 | GET | `/api/v1/database-models/{model_id}/databases` | 🔒 | — |
| 34 | GET | `/api/v1/managed-databases` | 🔒 | — |
| 35 | POST | `/api/v1/managed-databases` (`?provision`) | 🔒 | 🔌* |
| 36 | GET | `/api/v1/managed-databases/{db_id}` | 🔒 | — |
| 37 | PATCH | `/api/v1/managed-databases/{db_id}` | 🔒 | — |
| 38 | DELETE | `/api/v1/managed-databases/{db_id}` (`?drop_remote`) | 🔒 | 🔌* |
| 39 | POST | `/api/v1/managed-databases/{db_id}/reassign-owner` (`?provision`) | 🔒 | 🔌* |
| 40 | GET | `/api/v1/privileges` | 🔒 | — |
| 41 | PATCH | `/api/v1/privileges/{privilege_id}` | 🔒 | — |
| 42 | GET | `/api/v1/permission-profiles` | 🔒 | — |
| 43 | POST | `/api/v1/permission-profiles` | 🔒 | — |
| 44 | GET | `/api/v1/permission-profiles/{profile_id}` | 🔒 | — |
| 45 | PATCH | `/api/v1/permission-profiles/{profile_id}` | 🔒 | — |
| 46 | DELETE | `/api/v1/permission-profiles/{profile_id}` | 🔒 | — |
| 47 | POST | `/api/v1/admin/crypto/rotate` | 🔒 | — |
| 48 | GET | `/api/v1/database-models/{model_id}/migrations` | 🔒 | — |
| 49 | POST | `/api/v1/database-models/{model_id}/migrations` | 🔒 | — |
| 50 | GET | `/api/v1/database-models/{model_id}/migrations/{version}` | 🔒 | — |
| 51 | PATCH | `/api/v1/database-models/{model_id}/migrations/{version}` | 🔒 | — |
| 52 | DELETE | `/api/v1/database-models/{model_id}/migrations/{version}` | 🔒 | — |
| 53 | POST | `/api/v1/database-models/{model_id}/migrations/apply-all` | 🔒 | 🔌 |
| 54 | GET | `/api/v1/managed-databases/{db_id}/migrations/status` | 🔒 | 🔌 |
| 55 | POST | `/api/v1/managed-databases/{db_id}/migrations/apply` | 🔒 | 🔌 |
| 56 | POST | `/api/v1/managed-databases/{db_id}/migrations/rollback` | 🔒 | 🔌 |
| 57 | POST | `/api/v1/managed-databases/{db_id}/migrations/stamp` | 🔒 | 🔌 |
| 58 | GET | `/api/v1/managed-databases/{db_id}/migrations/history` | 🔒 | 🔌 |

\* Toca el motor solo cuando el flag (`provision` / `drop_remote`) es `true`. Los grants y
`provision`/`apply-profile` tocan el motor siempre. Los endpoints **48–58** son el módulo de
migraciones de blueprints (Plan 02); los `48–52` son CRUD de inventario, el resto tocan el motor.

---

## 17. Apéndice: variables de entorno del integrador

Relevantes para quien despliega o consume el gateway (la lista completa está en
`.env.example` y `app/core/environments.py`):

| Variable | Propósito |
|---|---|
| `CORS_ORIGINS` | Orígenes permitidos para el frontend que consume la API (coma-separados). |
| `DOCS_ENABLED` | Habilita `/api/v1/docs` y `/api/v1/redoc`. |
| `RATE_LIMIT_DEFAULT` | Límite global por IP (p. ej. `100/minute`). El login es fijo `5/minute`. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Credenciales del administrador único (sembrado al arrancar). |
| `SECRET_KEY` | Deriva la clave Fernet y firma la sesión. Obligatorio en producción. |
| `REMOTE_CONNECT_TIMEOUT` | Segundos para abrir la conexión a un servidor destino. |
| `REMOTE_STATEMENT_TIMEOUT_MS` | Milisegundos máximos por sentencia en el destino. |
| `REMOTE_SSL_MODE` | `ssl_mode` por defecto si un servidor no define el suyo. |

---

*Generado a partir del código fuente del backend (rutas, schemas y DTOs). Para detalles
de cada feature, consulta `docs/features/`; para el roadmap, `docs/plans/`.*
