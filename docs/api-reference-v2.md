# API Reference v2 — Permisos, perfiles, administración y migraciones de blueprints

> **Addendum** de [`api-reference.md`](api-reference.md). Documenta los endpoints añadidos
> tras la referencia inicial (35 endpoints), en dos lotes:
> - **Lote A — permisos y administración (12 endpoints):** grants granulares, introspección
>   de permisos, perfiles reutilizables, creación unificada usuario+grants y rotación de cifrado.
> - **Lote B — migraciones de blueprints (11 endpoints, Plan 02):** deltas SQL versionados por
>   blueprint y su aplicación/rollback/stamp/historial sobre las BDs gestionadas.
>
> Con ambos lotes, la API funcional asciende a **58 endpoints** (excluyendo `/test`).
>
> Las convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, autenticación por
> cookie, códigos de error, paginación) son las mismas del documento original —
> consúltalas en sus secciones [§3](api-reference.md#3-convenciones-de-la-api) y
> [§4](api-reference.md#4-tipos-de-datos-y-enums). Estos endpoints ya están integrados en el
> documento principal ([§8](api-reference.md#8-blueprints-de-bd-database-models) y
> [§9](api-reference.md#9-bases-de-datos-gestionadas-managed-databases)).

**Versión de la API:** `v1` · 🔌 = toca el servidor de BD destino · 🔒 = requiere sesión

---

## Índice

- [API Reference v2 — Gestión de permisos, perfiles y administración](#api-reference-v2--gestión-de-permisos-perfiles-y-administración)
  - [Índice](#índice)
  - [1. Tipos y enums nuevos](#1-tipos-y-enums-nuevos)
    - [`GrantLevel` (enum)](#grantlevel-enum)
    - [`ObjectRef` (objeto destino del grant)](#objectref-objeto-destino-del-grant)
    - [`GrantInfo` (respuesta de introspección de permisos)](#grantinfo-respuesta-de-introspección-de-permisos)
    - [`privileges` (lista de strings)](#privileges-lista-de-strings)
  - [2. Comprobar delegación de privilegios](#2-comprobar-delegación-de-privilegios)
    - [`POST /api/v1/servers/{server_id}/grantable` 🔒 🔌](#post-apiv1serversserver_idgrantable--)
  - [3. Grants granulares de un usuario](#3-grants-granulares-de-un-usuario)
    - [`GET /api/v1/server-users/{user_id}/grants` 🔒 🔌](#get-apiv1server-usersuser_idgrants--)
    - [`POST /api/v1/server-users/{user_id}/grants` 🔒 🔌](#post-apiv1server-usersuser_idgrants--)
    - [`DELETE /api/v1/server-users/{user_id}/grants` 🔒 🔌](#delete-apiv1server-usersuser_idgrants--)
  - [4. Aplicar un perfil a un usuario](#4-aplicar-un-perfil-a-un-usuario)
    - [`POST /api/v1/server-users/{user_id}/apply-profile/{profile_id}` 🔒 🔌](#post-apiv1server-usersuser_idapply-profileprofile_id--)
  - [5. Crear usuario + grants en una llamada](#5-crear-usuario--grants-en-una-llamada)
    - [`POST /api/v1/server-users/provision` 🔒 🔌](#post-apiv1server-usersprovision--)
  - [6. Perfiles de permisos](#6-perfiles-de-permisos)
    - [Schemas](#schemas)
    - [Endpoints](#endpoints)
  - [7. Administración: rotación de cifrado](#7-administración-rotación-de-cifrado)
    - [`POST /api/v1/admin/crypto/rotate` 🔒](#post-apiv1admincryptorotate-)
  - [8. Diferencias por motor](#8-diferencias-por-motor)
  - [9. Flujo de integración: gestión de permisos](#9-flujo-de-integración-gestión-de-permisos)
  - [10. Tabla resumen del lote A (permisos y administración)](#10-tabla-resumen-del-lote-a-permisos-y-administración)
  - [11. Tipos nuevos: migraciones de blueprints](#11-tipos-nuevos-migraciones-de-blueprints)
  - [12. Migraciones del blueprint](#12-migraciones-del-blueprint)
  - [13. Migraciones sobre la BD gestionada](#13-migraciones-sobre-la-bd-gestionada)
  - [14. Flujo de integración: migraciones](#14-flujo-de-integración-migraciones)
  - [15. Tabla resumen del lote B (migraciones)](#15-tabla-resumen-del-lote-b-migraciones)

---

## 1. Tipos y enums nuevos

Estos tipos se comparten entre los endpoints de grants y perfiles.

### `GrantLevel` (enum)

Nivel de la entidad sobre la que se otorga/revoca un privilegio:

| Valor | Aplica a | Notas |
|---|---|---|
| `global` | MySQL/MariaDB · PostgreSQL | Privilegios a nivel servidor. |
| `database` | ambos | Sobre una base de datos completa. |
| `schema` | **solo PostgreSQL** | Sobre un esquema (default `public`). |
| `table` | ambos | Sobre una tabla. |
| `column` | ambos | Sobre columnas concretas de una tabla. |
| `sequence` | **solo PostgreSQL** | Sobre una secuencia. |
| `routine` | ambos | Sobre una función/procedimiento. |

### `ObjectRef` (objeto destino del grant)

Qué campos llevas depende del `level`. `schema` solo aplica a PostgreSQL.

| Campo | Tipo | Cuándo se usa |
|---|---|---|
| `database` | string \| null | `database`, `schema`, `table`, `column`, `sequence`, `routine` |
| `schema` | string \| null | solo PostgreSQL (default `public`) |
| `table` | string \| null | `table`, `column` |
| `columns` | list[string] | `column` (lista de columnas afectadas) |
| `sequence` | string \| null | `sequence` (PostgreSQL) |
| `routine` | `RoutineRef` \| null | `routine` → `{ "kind": "FUNCTION" \| "PROCEDURE", "name": "..." }` |

Ejemplos de `object_ref` por nivel:

```jsonc
// database
{ "database": "app_prod" }
// table (MySQL)
{ "database": "app_prod", "table": "items" }
// table (PostgreSQL, con schema)
{ "database": "app_prod", "schema": "public", "table": "items" }
// column
{ "database": "app_prod", "table": "items", "columns": ["price", "stock"] }
// routine
{ "database": "app_prod", "routine": { "kind": "FUNCTION", "name": "calc_total" } }
```

### `GrantInfo` (respuesta de introspección de permisos)

| Campo | Tipo | Detalle |
|---|---|---|
| `level` | `GrantLevel` | Nivel del privilegio. |
| `object` | string \| null | Objeto cualificado (p. ej. `app_prod.items`); `null` = global. |
| `privileges` | list[string] | Privilegios efectivos sobre ese objeto. |
| `with_grant_option` | bool | Si el usuario puede a su vez delegar esos privilegios. |

### `privileges` (lista de strings)

Tokens de privilegio (`SELECT`, `INSERT`, `CREATE`, `EXECUTE`, `ALL PRIVILEGES`, …). Se
validan contra el **catálogo** por motor y nivel antes de construir el SQL; un token no
soportado se rechaza con `422`. Consulta los válidos con
`GET /api/v1/privileges?engine=<motor>&active=true`.

---

## 2. Comprobar delegación de privilegios

Verifica **antes** de intentar un grant si la credencial pseudo-root del gateway puede
delegar ciertos privilegios (es decir, si los tiene `WITH GRANT OPTION`). Útil para dar
feedback inmediato sin tocar destructivamente el motor.

### `POST /api/v1/servers/{server_id}/grantable` 🔒 🔌

**Path params:** `server_id` (int).

**Body** (`GrantableRequest`):

| Campo | Tipo | Requerido | Detalle |
|---|---|---|---|
| `level` | `GrantLevel` | sí | Nivel a comprobar. |
| `object_ref` | `ObjectRef` | sí | Objeto destino. |
| `privileges` | list[string] | sí | Mínimo 1. |

**Respuesta** `200` — `ApiResponse[GrantableResult]` (`{can_grant, level, privileges}`).

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/servers/1/grantable \
  -H 'Content-Type: application/json' \
  -d '{ "level": "database", "object_ref": { "database": "app_prod" },
        "privileges": ["SELECT","INSERT"] }'
```

```json
{ "data": { "can_grant": true, "level": "database", "privileges": ["SELECT","INSERT"] } }
```

---

## 3. Grants granulares de un usuario

Otorga, revoca y consulta privilegios de un `ServerUser` registrado, a cualquier nivel
(`database`, `table`, `column`, …). Todas operan contra el motor destino.

> El `user_id` es el id del **ServerUser del inventario** del gateway. Para inspeccionar o
> modificar permisos, el usuario debe estar registrado (ver
> [§7 del documento principal](api-reference.md#7-usuarios-del-motor-server-users)).

### `GET /api/v1/server-users/{user_id}/grants` 🔒 🔌

Introspección de los **permisos efectivos** del usuario, leídos del motor real
(en MySQL/MariaDB desde `information_schema.*_PRIVILEGES`).

**Path params:** `user_id` (int).

**Query params:**

| Parámetro | Tipo | Detalle |
|---|---|---|
| `database` | string \| null | **Obligatorio en PostgreSQL** para grants de objeto (tablas/columnas/secuencias/rutinas). En MySQL/MariaDB se ignora. |

**Respuesta** `200` — `ApiResponse[list[GrantInfo]]`.

```bash
curl -b cookies.txt http://localhost/api/v1/server-users/7/grants
```

```json
{ "data": [
  { "level": "database", "object": "app_prod", "privileges": ["DELETE","INSERT","SELECT","UPDATE"], "with_grant_option": false },
  { "level": "table", "object": "app_prod.items", "privileges": ["SELECT"], "with_grant_option": false }
] }
```

### `POST /api/v1/server-users/{user_id}/grants` 🔒 🔌

Otorga uno o más privilegios. Pre-chequea `can_grant` y devuelve `403` si la credencial
del gateway no puede delegarlos (sin tocar el motor).

**Body** (`GrantRequest`):

| Campo | Tipo | Requerido | Detalle |
|---|---|---|---|
| `level` | `GrantLevel` | sí | Nivel del grant. |
| `object_ref` | `ObjectRef` | sí | Objeto destino. |
| `privileges` | list[string] | sí | Mínimo 1. |
| `with_grant_option` | bool | no | Default `false`. Permite que el usuario re-delegue. |

**Respuesta** `200` — `ApiResponse[dict]` (`{granted, level, privileges, with_grant_option}`).

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/server-users/7/grants \
  -H 'Content-Type: application/json' \
  -d '{ "level": "database", "object_ref": { "database": "app_prod" },
        "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "with_grant_option": false }'
```

```json
{ "data": { "granted": true, "level": "database",
            "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "with_grant_option": false },
  "message": "Privilegio(s) otorgado(s): SELECT, INSERT, UPDATE, DELETE a nivel database." }
```

> Errores: `403` la credencial del gateway no tiene `WITH GRANT OPTION` para esos
> privilegios · `422` privilegio no válido para el motor/nivel · `502`/`504` motor
> inalcanzable.

### `DELETE /api/v1/server-users/{user_id}/grants` 🔒 🔌

Revoca privilegios. El cuerpo viaja en el `DELETE` (`RevokeRequest`).

**Body** (`RevokeRequest`):

| Campo | Tipo | Requerido | Notas |
|---|---|---|---|
| `level` | `GrantLevel` | sí | |
| `object_ref` | `ObjectRef` | sí | |
| `privileges` | list[string] | sí (mínimo 1) | |
| `cascade` | bool | no (default `false`) | **Solo PostgreSQL**: revoca en cascada los privilegios re-delegados. En MySQL/MariaDB → `422`. |

**Query params:**

| Param | Tipo | Notas |
|---|---|---|
| `confirm_grantee` | string | **Obligatorio si `cascade=true`**: repetir el username del grantee (doble confirmación de operación GATE). |

**Errores específicos:**

- `409` — el `grantee` es la propia credencial del gateway (guard anti auto-lockout): no se permite revocarle privilegios a la cuenta pseudo-root de conexión.
- `422` — `cascade=true` en MySQL/MariaDB (no soportado), o falta `confirm_grantee` cuando `cascade=true`.

> **Auditoría:** todo REVOKE registra una fila de **intención** (`status="attempt"`,
> fail-closed) antes de ejecutar, con campos DCL granulares (`grantee`, `privilege`,
> `object_level`, `object_name`, `grantor`), y el resultado (`success`/`error`) después.

**Respuesta** `200` — `ApiResponse[None]`.

```bash
# REVOKE simple
curl -b cookies.txt -X DELETE http://localhost/api/v1/server-users/7/grants \
  -H 'Content-Type: application/json' \
  -d '{ "level": "table", "object_ref": { "database": "app_prod", "table": "items" },
        "privileges": ["DELETE"] }'

# REVOKE ... CASCADE (PostgreSQL) — exige confirmación
curl -b cookies.txt -X DELETE "http://localhost/api/v1/server-users/7/grants?confirm_grantee=analista" \
  -H 'Content-Type: application/json' \
  -d '{ "level": "table", "object_ref": { "database": "app_prod", "schema": "public", "table": "items" },
        "privileges": ["SELECT"], "cascade": true }'
```

```json
{ "message": "Privilegio(s) revocado(s): DELETE a nivel table." }
```

---

## 4. Aplicar un perfil a un usuario

Aplica un [perfil de permisos](#6-perfiles-de-permisos) guardado a un usuario. Para cada
nivel definido en el perfil debes mapear el objeto concreto; los niveles sin mapeo se
omiten (se reportan). Es **best-effort**: un grant que falle no aborta los demás.

### `POST /api/v1/server-users/{user_id}/apply-profile/{profile_id}` 🔒 🔌

**Path params:** `user_id` (int), `profile_id` (int).

**Body** (`ApplyProfileRequest`):

| Campo | Tipo | Detalle |
|---|---|---|
| `object_mappings` | list[`LevelObjectMapping`] | Lista de `{ "level": GrantLevel, "object_ref": ObjectRef }`. Un mapeo por cada nivel del perfil que quieras aplicar. |

**Respuesta** `200` — `ApiResponse[ApplyProfileResult]`:

| Campo | Tipo |
|---|---|
| `profile_id` | int |
| `profile_name` | string |
| `engine` | string |
| `grants_applied` | int |
| `skipped_levels` | list[string] |
| `errors` | list[string] |

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/server-users/7/apply-profile/3 \
  -H 'Content-Type: application/json' \
  -d '{ "object_mappings": [
        { "level": "database", "object_ref": { "database": "app_prod" } },
        { "level": "table",    "object_ref": { "database": "app_prod", "table": "items" } }
      ] }'
```

```json
{ "data": { "profile_id": 3, "profile_name": "app-readwrite", "engine": "mysql",
            "grants_applied": 2, "skipped_levels": [], "errors": [] },
  "message": "Perfil 'app-readwrite' aplicado: 2 grant(s)." }
```

> El motor del perfil debe coincidir con el del servidor del usuario, si no `422`.

---

## 5. Crear usuario + grants en una llamada

Endpoint unificado: crea el `ServerUser` en el inventario, lo **aprovisiona en el motor**
(`CREATE USER`) y aplica los `initial_grants` indicados. Los grants son best-effort: un
fallo en un grant no revierte la creación del usuario.

### `POST /api/v1/server-users/provision` 🔒 🔌

**Body** (`ServerUserFullCreate` = `ServerUserCreate` + `initial_grants`):

| Campo | Tipo | Requerido | Detalle |
|---|---|---|---|
| `server_id` | int | sí | `>= 1` |
| `username` | string | sí | patrón `^[A-Za-z_][A-Za-z0-9_]{0,62}$` |
| `host` | string | no | default `"%"`; solo MySQL/MariaDB |
| `password` | string \| null | sí (se aprovisiona) | necesario para el `CREATE USER` |
| `notes` | string \| null | no | — |
| `is_active` | bool | no | default `true` |
| `initial_grants` | list[`GrantOnCreate`] | no | `{ level, object_ref, privileges[], with_grant_option }` |

**Respuesta** `201` — `ApiResponse[ServerUserFullOut]`:

| Campo | Tipo | Detalle |
|---|---|---|
| `user` | `ServerUserOut` | El usuario creado. |
| `grants_applied` | int | Nº de grants aplicados con éxito. |
| `grant_results` | list[`GrantApplyResult`] | `{ level, object?, privileges[], success, error? }` por cada grant intentado. |

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/server-users/provision \
  -H 'Content-Type: application/json' \
  -d '{
        "server_id": 1, "username": "app_user", "host": "%", "password": "p@ss",
        "initial_grants": [
          { "level": "database", "object_ref": { "database": "app_prod" },
            "privileges": ["SELECT","INSERT","UPDATE","DELETE"] }
        ]
      }'
```

```json
{ "data": {
    "user": { "id": 7, "server_id": 1, "username": "app_user", "host": "%", "has_password": true },
    "grants_applied": 1,
    "grant_results": [ { "level": "database", "object": "app_prod",
      "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "success": true, "error": null } ]
  },
  "message": "Usuario 'app_user' aprovisionado. 1 grant(s) aplicado(s)." }
```

---

## 6. Perfiles de permisos

Plantillas de privilegios **por motor**, reutilizables para aplicar a usuarios con
[`apply-profile`](#4-aplicar-un-perfil-a-un-usuario). CRUD puro de inventario; **no toca
ningún motor**. Requiere sesión.

### Schemas

`PermissionProfileCreate`:

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `name` | string | sí | 1–100 caracteres |
| `engine` | `EngineType` | sí | `mysql` \| `mariadb` \| `postgresql` |
| `description` | string \| null | no | máx 255 |
| `items` | list[`PermissionProfileItemIn`] | sí | mínimo 1; cada item: `{ level: GrantLevel, privileges: list[string] }` |

`PermissionProfileUpdate`: `name?`, `description?`, `is_active?`, `items?`. El `engine` es
**inmutable**; si envías `items`, **reemplazan** por completo los anteriores.

`PermissionProfileOut`: `{ id, name, engine, description?, is_active, items[], created_at, updated_at }`
donde cada item de salida es `{ level, privileges[], requires_confirmation }`.

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/permission-profiles` | Lista (filtros `?engine=`, `?active=`). **No paginada.** |
| `POST` | `/api/v1/permission-profiles` | Crea un perfil (`201`). |
| `GET` | `/api/v1/permission-profiles/{profile_id}` | Detalle. |
| `PATCH` | `/api/v1/permission-profiles/{profile_id}` | Actualiza (items reemplazan). |
| `DELETE` | `/api/v1/permission-profiles/{profile_id}` | Elimina. |

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/permission-profiles \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "app-readwrite", "engine": "mysql",
        "description": "Lectura/escritura típica de aplicación",
        "items": [
          { "level": "database", "privileges": ["SELECT","INSERT","UPDATE","DELETE"] }
        ]
      }'
```

```json
{ "data": { "id": 3, "name": "app-readwrite", "engine": "mysql",
            "description": "Lectura/escritura típica de aplicación", "is_active": true,
            "items": [ { "level": "database",
              "privileges": ["SELECT","INSERT","UPDATE","DELETE"], "requires_confirmation": false } ] },
  "message": "Perfil de permisos creado." }
```

---

## 7. Administración: rotación de cifrado

Rota la **clave de datos (DEK)** y **re-cifra todas las credenciales** almacenadas
(servidores y usuarios), **sin cambiar `SECRET_KEY` ni reiniciar** la aplicación.
Requiere sesión. No toca los motores destino (opera sobre la BD de metadatos).

### `POST /api/v1/admin/crypto/rotate` 🔒

**Body:** ninguno.

**Respuesta** `200` — `ApiResponse[CryptoRotationOut]`:

| Campo | Tipo | Detalle |
|---|---|---|
| `servers_reencrypted` | int | Credenciales de servidor re-cifradas. |
| `server_users_reencrypted` | int | Credenciales de usuario re-cifradas. |

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/admin/crypto/rotate
```

```json
{ "data": { "servers_reencrypted": 12, "server_users_reencrypted": 30 },
  "message": "Clave de cifrado rotada; credenciales re-cifradas." }
```

---

## 8. Diferencias por motor

| Aspecto | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Niveles soportados | `global`, `database`, `table`, `column`, `routine` | `global`, `database`, `schema`, `table`, `column`, `sequence`, `routine` |
| `object_ref.schema` | se ignora | aplica (default `public`) |
| `host` del usuario | relevante (`'user'@'host'`) | se ignora |
| `GET .../grants` sin `database` | devuelve todos los niveles | falta el contexto de objeto: pasa `?database=` para tablas/columnas/secuencias/rutinas |
| Lectura de grants | `information_schema.{USER,SCHEMA,TABLE,COLUMN}_PRIVILEGES` | catálogos del sistema por base de datos |

---

## 9. Flujo de integración: gestión de permisos

Continúa el [flujo D del documento principal](api-reference.md#15-flujos-de-integración-orden-de-llamadas)
(crear usuario y BD). Requiere sesión activa.

```
# Opción rápida: usuario + grants en una sola llamada
POST /api/v1/server-users/provision 🔌
     body: { server_id, username, password, initial_grants:[{level, object_ref, privileges}] }

# Opción paso a paso:
1. (opcional) POST /api/v1/servers/{id}/grantable 🔌      → ¿puedo delegar estos privilegios?
2. POST /api/v1/server-users/{user_id}/grants 🔌          → otorga privilegios
3. GET  /api/v1/server-users/{user_id}/grants 🔌          → verifica los permisos efectivos
4. DELETE /api/v1/server-users/{user_id}/grants 🔌        → revoca si hace falta

# Con perfiles reutilizables:
A. POST /api/v1/permission-profiles                       → define la plantilla (por motor)
B. POST /api/v1/server-users/{user_id}/apply-profile/{profile_id} 🔌
        body: { object_mappings:[{level, object_ref}] }   → aplica la plantilla al usuario
```

Dependencias y reglas:
- Los endpoints de grants operan sobre un `ServerUser` ya registrado; crea el usuario
  primero (o usa `/provision`).
- En PostgreSQL, recuerda pasar `?database=` al consultar grants de objeto.
- El motor del perfil debe coincidir con el del servidor del usuario (`422` si no).
- `grantable`/`grants POST` devuelven `403` si la credencial pseudo-root del gateway no
  puede delegar (`WITH GRANT OPTION`).

---

## 10. Tabla resumen del lote A (permisos y administración)

> 🔌 = toca el servidor de BD destino · 🔒 = requiere sesión

| Método | Ruta | Auth | Motor |
|---|---|---|---|
| POST | `/api/v1/servers/{server_id}/grantable` | 🔒 | 🔌 |
| GET | `/api/v1/server-users/{user_id}/grants` | 🔒 | 🔌 |
| POST | `/api/v1/server-users/{user_id}/grants` | 🔒 | 🔌 |
| DELETE | `/api/v1/server-users/{user_id}/grants` | 🔒 | 🔌 |
| POST | `/api/v1/server-users/{user_id}/apply-profile/{profile_id}` | 🔒 | 🔌 |
| POST | `/api/v1/server-users/provision` | 🔒 | 🔌 |
| GET | `/api/v1/permission-profiles` | 🔒 | — |
| POST | `/api/v1/permission-profiles` | 🔒 | — |
| GET | `/api/v1/permission-profiles/{profile_id}` | 🔒 | — |
| PATCH | `/api/v1/permission-profiles/{profile_id}` | 🔒 | — |
| DELETE | `/api/v1/permission-profiles/{profile_id}` | 🔒 | — |
| POST | `/api/v1/admin/crypto/rotate` | 🔒 | — |

Estos 12 (lote A) elevaron la API a **47 endpoints**. El lote B (§11–§15) añade 11 más,
para un total de **58**. Ver el [resumen completo](api-reference.md#16-apéndice-tabla-resumen-de-endpoints)
en el documento principal (ya integrados).

---

## 11. Tipos nuevos: migraciones de blueprints

Tipos del módulo de **migraciones de blueprints** (Plan 02). Una migración es un delta SQL
versionado de un [`DatabaseModel`](api-reference.md#8-blueprints-de-bd-database-models); el
gateway lo aplica a las BDs que replican ese blueprint.

### `MigrationStatus` (enum)

Desenlace de una migración en el historial: `applied` | `failed`.

### Versión (`version`)

String de **solo dígitos**, patrón `^\d{4,10}$` (`0001`, `0002`…). **Se compara y ordena
NUMÉRICAMENTE**, no lexicográficamente: usa un ancho consistente para evitar ambigüedad
(el gateway ordena por valor entero, así que `0009` < `0010` < `0100`).

### `ModelMigrationCreate` / `ModelMigrationPatch` (bodies)

| Campo | Tipo | Requerido (Create) | Detalle |
|---|---|---|---|
| `version` | string | sí | patrón `^\d{4,10}$` |
| `name` | string | sí | 1–200 |
| `up_sql` | string | sí | delta SQL base, estilo MySQL; 1–262144 (256 KB) |
| `up_sql_mysql` | string \| null | no | override manual MySQL/MariaDB |
| `up_sql_postgresql` | string \| null | no | override manual PostgreSQL |
| `down_sql` | string \| null | no | rollback **confirmado**; sin él, el rollback da `409` |

`ModelMigrationPatch` acepta `name?`, `down_sql?`, `up_sql_mysql?`, `up_sql_postgresql?`.
El `up_sql`/variantes **no** se pueden cambiar si la migración ya se aplicó en alguna BD (`409`).

### `ModelMigrationOut` (detalle) / `ModelMigrationSummary` (lista)

`ModelMigrationOut`: `{ id, model_id, version, name, up_sql, up_sql_mysql?, up_sql_postgresql?,
down_sql?, down_sql_suggested?, translated: {mysql, postgresql}, checksum, created_at, updated_at }`.

- `translated` — el `up_sql` **auto-traducido** por motor con `sqlglot` (lo que realmente se
  ejecutaría). Los overrides manuales tienen prioridad sobre la traducción.
- `down_sql_suggested` — rollback **sugerido** automáticamente para operaciones aditivas
  (CREATE TABLE → DROP TABLE, ADD COLUMN → DROP COLUMN). Revísalo y confírmalo con `PATCH`.
- `checksum` — SHA256 de todo el SQL + versión; el gateway lo re-valida antes de aplicar.

`ModelMigrationSummary` (lista): `{ id, model_id, version, name, has_mysql_override,
has_postgresql_override, has_rollback, checksum, created_at }`.

---

## 12. Migraciones del blueprint

CRUD de inventario (**no toca motores**) bajo `/database-models/{model_id}/migrations`.

### `GET /api/v1/database-models/{model_id}/migrations` 🔒

Lista paginada (`page`, `size`) de `ModelMigrationSummary`, en orden de versión numérico.

### `POST /api/v1/database-models/{model_id}/migrations` 🔒

Crea una migración (`201`). **Body:** `ModelMigrationCreate`. La respuesta incluye
`translated` (por motor) y `down_sql_suggested`.

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/database-models/3/migrations \
  -H 'Content-Type: application/json' \
  -d '{ "version": "0001", "name": "Esquema inicial",
        "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)" }'
```
```json
{ "data": { "version": "0001", "name": "Esquema inicial",
    "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
    "down_sql": null, "down_sql_suggested": "DROP TABLE IF EXISTS orders;",
    "translated": { "mysql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
      "postgresql": "CREATE TABLE orders (id INT GENERATED BY DEFAULT AS IDENTITY NOT NULL PRIMARY KEY, total INT)" },
    "checksum": "…" },
  "message": "Migración creada." }
```

> Errores: `404` blueprint inexistente · `409` versión duplicada · `422` patrón de versión,
> SQL vacío o >256 KB.

### `GET /api/v1/database-models/{model_id}/migrations/{version}` 🔒

Detalle (`ModelMigrationOut`). `404` si no existe esa versión.

### `PATCH /api/v1/database-models/{model_id}/migrations/{version}` 🔒

Confirma `down_sql` o añade overrides. **Body:** `ModelMigrationPatch`.

```bash
curl -b cookies.txt -X PATCH http://localhost/api/v1/database-models/3/migrations/0001 \
  -H 'Content-Type: application/json' -d '{ "down_sql": "DROP TABLE IF EXISTS orders" }'
```

> `409` si intentas cambiar el SQL de una migración ya aplicada en alguna BD.

### `DELETE /api/v1/database-models/{model_id}/migrations/{version}` 🔒

Elimina la migración. `409` si ya tiene historial de aplicación (revierte primero).

### `POST /api/v1/database-models/{model_id}/migrations/apply-all` 🔒 🔌

Aplica las pendientes a **todas** las BDs del blueprint (síncrono, acotado). **Rate limit 3/min.**

| Query | Tipo | Default | Detalle |
|---|---|---|---|
| `max_databases` | int | `10` | `1..100`. Cota de BDs por llamada. |
| `force` | bool | `false` | Override de cuarentena en cada BD. |
| `dry_run` | bool | `false` | Devuelve el plan por BD sin aplicar. |

**Respuesta** `200` — `ApplyAllOut` `{ model_id, total_databases, processed, results[] }`,
cada `result`: `{ managed_database_id, database_name, server_id, ok, applied[], dry_run, pending_versions[], error? }`.

```json
{ "data": { "model_id": 3, "total_databases": 12, "processed": 10,
    "results": [ { "managed_database_id": 5, "database_name": "app_a", "server_id": 1,
                   "ok": true, "applied": [ { "version": "0001", "status": "applied", "execution_ms": 42 } ] } ] },
  "message": "Aplicación masiva ejecutada." }
```

---

## 13. Migraciones sobre la BD gestionada

Aplicación real sobre una BD bajo `/managed-databases/{db_id}/migrations`. **Tocan el motor.**
Requieren que la BD tenga `model_id` (blueprint) asignado (`422` si no). Rate limit **10/min**
en `apply`/`rollback`/`stamp`.

### `GET /api/v1/managed-databases/{db_id}/migrations/status` 🔒 🔌

`MigrationStatusOut` `{ managed_database_id, model_id, slug, current_version, latest_available,
pending_count, pending_versions[] }`.

### `POST /api/v1/managed-databases/{db_id}/migrations/apply` 🔒 🔌

Aplica las pendientes en orden numérico; se detiene en la primera que falle.

| Query | Tipo | Default | Detalle |
|---|---|---|---|
| `version` | string \| null | — | `^\d{4,10}$`. Aplica solo **hasta** esa versión (inclusive). |
| `force` | bool | `false` | Reintenta una BD en **cuarentena** tras inspección. |
| `dry_run` | bool | `false` | No aplica: devuelve `current_version` + `pending_versions`. |

**Respuesta** (apply real): `{ managed_database_id, database_name, server_id, applied_count,
failed, quarantined, results: [{ migration_id, version, status, error?, execution_ms }] }`.
Con `dry_run=true`: `{ …, dry_run: true, current_version, pending_versions[], pending_count }`.

```bash
curl -b cookies.txt -X POST http://localhost/api/v1/managed-databases/5/migrations/apply
```
```json
{ "data": { "managed_database_id": 5, "database_name": "app_prod", "server_id": 42,
            "applied_count": 2, "failed": false, "quarantined": false,
            "results": [ { "migration_id": 1, "version": "0001", "status": "applied", "execution_ms": 42 },
                         { "migration_id": 2, "version": "0002", "status": "applied", "execution_ms": 31 } ] },
  "message": "Migraciones aplicadas." }
```

> Errores: `422` BD sin blueprint / blueprint sin migraciones · `409` cuarentena (sin `force`),
> lock ocupado, o checksum alterado · `502`/`504` motor inalcanzable.

### `POST /api/v1/managed-databases/{db_id}/migrations/rollback` 🔒 🔌

Revierte la última aplicada. **DESTRUCTIVO** — `confirm_version` (query, obligatorio) debe
igualar la versión actual de la BD.

```bash
curl -b cookies.txt -X POST \
  "http://localhost/api/v1/managed-databases/5/migrations/rollback?confirm_version=0002"
```
```json
{ "data": { "managed_database_id": 5, "rolled_back_version": "0002", "current_version": "0001",
            "result": { "version": "0002", "status": "applied", "execution_ms": 28 } },
  "message": "Rollback ejecutado." }
```

> `422` si `confirm_version` falta o no coincide con la versión actual · `409` si esa versión
> no tiene `down_sql` confirmado o la BD no tiene nada aplicado.

### `POST /api/v1/managed-databases/{db_id}/migrations/stamp` 🔒 🔌

Marca la BD en `version` (query, obligatorio) **sin ejecutar SQL** — para BDs cuyo esquema ya
existe pero el gateway aún no registra. `422` si la versión no existe en el blueprint.

### `GET /api/v1/managed-databases/{db_id}/migrations/history` 🔒 🔌

Historial paginado (`page`, `size`) de `MigrationHistoryOut` `{ id, managed_database_id,
model_migration_id, version, applied_at, status, error?, execution_ms? }`, más reciente primero.

---

## 14. Flujo de integración: migraciones

Continúa los flujos D/G del [documento principal](api-reference.md#15-flujos-de-integración-orden-de-llamadas).
Requiere: un blueprint y BDs creadas con ese `model_id`.

```
# 1) Definir el delta (inventario, no toca motores)
POST  /api/v1/database-models/{model_id}/migrations          → translated + down_sql_suggested
PATCH /api/v1/database-models/{model_id}/migrations/{ver}     → (opcional) confirmar down_sql

# 2) Aplicar a UNA BD (toca el motor)
GET   /api/v1/managed-databases/{db_id}/migrations/status            → current vs pendientes
POST  /api/v1/managed-databases/{db_id}/migrations/apply?dry_run=true → plan
POST  /api/v1/managed-databases/{db_id}/migrations/apply 🔌          → aplica
GET   /api/v1/managed-databases/{db_id}/migrations/history           → resultado

# 3) (opcional) rollback / fan-out
POST  /api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version={ver} 🔌
POST  /api/v1/database-models/{model_id}/migrations/apply-all 🔌
```

Reglas y dependencias:
- La BD necesita `model_id` asignado; si no, los endpoints de migración dan `422`.
- Versiones en orden **numérico**; `apply` se detiene en el primer fallo.
- Tras un fallo la BD entra en **cuarentena** (`status: error`): reintenta con `?force=true`
  tras inspeccionar. Diseña migraciones idempotentes (`IF NOT EXISTS`).
- `rollback` exige `?confirm_version=` = versión actual y `down_sql` confirmado.

---

## 15. Tabla resumen del lote B (migraciones)

> 🔌 = toca el servidor de BD destino · 🔒 = requiere sesión

| Método | Ruta | Auth | Motor |
|---|---|---|---|
| GET | `/api/v1/database-models/{model_id}/migrations` | 🔒 | — |
| POST | `/api/v1/database-models/{model_id}/migrations` | 🔒 | — |
| GET | `/api/v1/database-models/{model_id}/migrations/{version}` | 🔒 | — |
| PATCH | `/api/v1/database-models/{model_id}/migrations/{version}` | 🔒 | — |
| DELETE | `/api/v1/database-models/{model_id}/migrations/{version}` | 🔒 | — |
| POST | `/api/v1/database-models/{model_id}/migrations/apply-all` | 🔒 | 🔌 |
| GET | `/api/v1/managed-databases/{db_id}/migrations/status` | 🔒 | 🔌 |
| POST | `/api/v1/managed-databases/{db_id}/migrations/apply` | 🔒 | 🔌 |
| POST | `/api/v1/managed-databases/{db_id}/migrations/rollback` | 🔒 | 🔌 |
| POST | `/api/v1/managed-databases/{db_id}/migrations/stamp` | 🔒 | 🔌 |
| GET | `/api/v1/managed-databases/{db_id}/migrations/history` | 🔒 | 🔌 |

Detalle conceptual del módulo: [`docs/features/model-migrations.md`](features/model-migrations.md).

---

*Generado a partir del código fuente (rutas, schemas y DTOs). Estilo alineado con
[`api-reference.md`](api-reference.md).*
