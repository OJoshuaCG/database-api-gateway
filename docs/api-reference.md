# API Reference — Database API Gateway

> Referencia completa para integrar el **Database API Gateway** en tu desarrollo.
> Documenta cada endpoint, sus parámetros, tipos y valores permitidos, ejemplos de
> uso (`curl` + JSON) y el orden en que deben consumirse para cumplir cada propósito.
>
> Este documento **consolida** el contenido de los addendums `api-reference-v2.md` a
> `api-reference-v5.md` y de las guías de feature específicas para frontend
> (`docs/features/*-frontend-api.md`). Esos archivos siguen existiendo como historial
> con más detalle narrativo (escenarios, mockups, diagramas de flujo), pero **este es
> el documento a leer primero**: todo lo que necesitás para integrar el gateway hoy
> está acá.

**Versión de la API:** `v1` · **Base URL:** `https://<host>/api/v1` · **Estado:** Iteraciones 1 y 2 + migraciones de blueprints (Plan 02) + adopción/reconciliación/snapshot (Plan 09) + gestión agrupada de usuarios del motor + comparación de esquemas entre BDs (con cierre de dependencias y reconciliación de aplicaciones parciales) implementadas (ver [§15](#15-estado-del-proyecto)).

---

## Índice

1. [¿Qué es y qué problema resuelve?](#1-qué-es-y-qué-problema-resuelve)
2. [Conceptos clave](#2-conceptos-clave)
3. [Convenciones de la API](#3-convenciones-de-la-api)
4. [Tipos de datos y enums](#4-tipos-de-datos-y-enums)
5. [Autenticación (`/auth`)](#5-autenticación-auth)
6. [Servidores (`/servers`)](#6-servidores-servers)
7. [Usuarios del motor (`/server-users` y `/servers/{id}/users/*`)](#7-usuarios-del-motor-server-users-y-serversidusers)
8. [Blueprints de BD y sus migraciones (`/database-models`)](#8-blueprints-de-bd-database-models)
9. [Bases de datos gestionadas y migraciones por BD (`/managed-databases`)](#9-bases-de-datos-gestionadas-managed-databases)
10. [Comparación de esquemas entre BDs (`/schema-comparisons`)](#10-comparación-de-esquemas-entre-bds-schema-comparisons)
11. [Catálogo de privilegios (`/privileges`)](#11-catálogo-de-privilegios-privileges)
12. [Perfiles de permisos (`/permission-profiles`)](#12-perfiles-de-permisos-permission-profiles)
13. [Administración: cifrado (`/admin/crypto`)](#13-administración-cifrado-admincrypto)
14. [Health checks](#14-health-checks)
15. [Estado del proyecto](#15-estado-del-proyecto)
16. [Flujos de integración (orden de llamadas)](#16-flujos-de-integración-orden-de-llamadas)
17. [Apéndice: tabla resumen de endpoints](#17-apéndice-tabla-resumen-de-endpoints)
18. [Apéndice: variables de entorno del integrador](#18-apéndice-variables-de-entorno-del-integrador)

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
| **DatabaseModel** | Un *blueprint*/categoría lógica (p. ej. "WhatsApp", "SMS"). Metadato del inventario; varias BDs pueden replicar el mismo modelo. Tiene una secuencia versionada de migraciones SQL ([§8](#8-blueprints-de-bd-database-models)). |
| **SchemaComparison** | Un diff estructural entre dos BDs (mismo motor, o MySQL↔MariaDB), calculado una vez y persistido con TTL. Se puede **adoptar** como nueva versión de un blueprint o **ejecutar** directamente sobre la BD destino ([§10](#10-comparación-de-esquemas-entre-bds-schema-comparisons)). |
| **Privilege** | Entrada del catálogo de privilegios soportados por cada motor (`SELECT`, `CREATE`, …). |
| **AuditLog** | Registro interno de toda operación sensible (no expuesto por la API). |

**Propiedad de una base de datos:**
- *MySQL/MariaDB*: propiedad **lógica** — el owner es el `ServerUser` con `GRANT ALL` sobre la BD.
- *PostgreSQL*: propiedad **nativa** — la BD tiene un `OWNER` (`ALTER DATABASE … OWNER TO`).

**Estados de aprovisionamiento** (`ProvisionStatus`): una BD pasa de `pending` →
`active` si el `CREATE` en el motor tiene éxito, o queda en `error` si falla. **No hay
rollback silencioso**: el registro se conserva con el detalle del error para auditoría y
reintento. `error` también se usa como **cuarentena** tras un fallo de migración a
mitad de camino (ver [§9](#9-bases-de-datos-gestionadas-managed-databases)).

**Aplicación parcial (concepto transversal, importante para la UI de migraciones):**
el DDL de MySQL/MariaDB hace **commit por sentencia** (no es transaccional). Si una
migración de N sentencias falla en la sentencia k, la BD queda **físicamente** con k
cambios aplicados, pero el campo `current_version` de esa BD no se actualiza hasta que
la migración *entera* termina — puede leerse como "sano" aunque no lo esté. El gateway
expone esto explícitamente y ofrece una vía de reconciliación automática y otra manual
(ver [§9](#9-bases-de-datos-gestionadas-managed-databases)). **En PostgreSQL esto casi
nunca ocurre**: el motor ejecuta DDL transaccional, así que una migración fallida se
deshace sola.

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

**Excepción al envelope**: `GET /schema-comparisons/{id}/export` ([§10](#10-comparación-de-esquemas-entre-bds-schema-comparisons))
es una descarga de archivo (`Content-Disposition: attachment`, `application/sql`); no
usa `ApiResponse`.

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

O, en los endpoints más nuevos, con más estructura (siempre compatible con leer solo
`detail.msg` como el mensaje para el usuario):

```json
{ "detail": { "msg": "Mensaje para el usuario.", "type": "AppHttpException", "context": { } } }
```

Algunos errores traen además **`detail.public_context`**: datos estructurados que la
UI necesita para guiar al usuario (por ejemplo, exactamente qué versiones les falta un
rollback confirmado, o qué dependencias faltan en una selección). A diferencia de
`context` (que es información de debug y **solo aparece con `APP_ENV=development`**),
**`public_context` viaja siempre, en cualquier entorno** — es el campo que hay que leer
para construir flujos guiados de recuperación de error, nunca `context`.

| Código | Significado en el gateway |
|---|---|
| `400` | Petición mal formada. |
| `401` | No autenticado / sesión inválida o usuario inactivo. |
| `404` | El recurso del inventario no existe (o, en `/schema-comparisons`, una BD cruda que no existe de verdad en el motor). |
| `409` | Conflicto: recurso duplicado, dependencias, validación cruzada (p. ej. owner de otro servidor), "ya existe en el motor destino", o un estado que exige resolución previa (cuarentena, aplicación parcial, blueprint sin revisar). |
| `410` | El recurso expiró (hoy: una `SchemaComparison` vencida — [§10](#10-comparación-de-esquemas-entre-bds-schema-comparisons)). Hay que recalcularlo. |
| `422` | Error de validación de Pydantic (tipos/patrones), falta una confirmación obligatoria (`confirm_name`, `confirm_username`, `password` al aprovisionar, `confirm_target_name`/`confirm_token`, `confirm_version`), o una selección/plan que no cierra sus dependencias. |
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
endpoints comparten el rate limit global configurado en el servidor, salvo los
específicos anotados en cada sección (`grants`, `apply-all`, `apply`/`rollback`/`stamp`,
`adopt`/`execute` de schema-comparisons).

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
| `GrantLevel` | `global`, `database`, `schema`, `table`, `column`, `sequence`, `routine` | Nivel de un grant (§7/§12). `schema` y `sequence` solo PostgreSQL. |

**Tipos de grants** (usados en los endpoints de permisos, §7 y §12):

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
| `host` (ServerUser) | `^[A-Za-z0-9_.%:\-]{1,255}$` | Solo MySQL/MariaDB; `%` = wildcard (**es un host real**, no significa "todos los hosts" — ver §7). |
| `name` (ManagedDatabase) | `^[A-Za-z_][A-Za-z0-9_]{0,62}$` | Nombre de BD. |
| `charset` / `collation` | `^[A-Za-z0-9_]{1,64}$` | Solo MySQL/MariaDB. |
| `slug` (DatabaseModel) | `^[a-z0-9]+(?:[-_][a-z0-9]+)*$` | kebab/snake en minúsculas. |

Las marcas de tiempo (`created_at`, `updated_at`) son `datetime` en formato ISO 8601.

> **Regla de orden transversal (importante, aplica a `/schema-comparisons` y —
> internamente— a la generación de migraciones): cuando un objeto expone tanto `seq`
> como `phase`, `seq` es la ÚNICA fuente de verdad del orden de ejecución.** `phase` es
> una etiqueta gruesa (1..9) útil para agrupar/filtrar visualmente, pero el orden real
> de ejecución puede **cruzar fases** (una FK puede depender de una `PRIMARY KEY`
> creada en una fase posterior). Ordenar/ejecutar por `phase` produce una secuencia que
> el motor puede rechazar. Ver el detalle completo en [§10](#10-comparación-de-esquemas-entre-bds-schema-comparisons).

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

> **Seguridad (anti-SSRF):** si `REMOTE_SSRF_GUARD_ENABLED=True`, el host se revalida **al
> conectar** (no solo al registrar el servidor): un destino que resuelva a loopback,
> link-local/metadata (169.254.169.254), multicast o reservado se rechaza con `422` en cada
> operación contra el motor (cierra el DNS-rebinding).

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

Lista los usuarios/roles del motor, **plano** (un `user@host` por cuenta). Respuesta:
`ApiResponse[list[EngineUserInfo]]` (`EngineUserInfo` = `{username, host?}`; `host` solo
en MySQL/MariaDB).

```json
{ "data": [ { "username": "app_user", "host": "%" }, { "username": "readonly" } ] }
```

> Este listado es redundante cuando un username tiene varios hosts (MySQL/MariaDB).
> Para la pantalla principal de gestión de usuarios del motor, usá la **vista agrupada**
> [`GET /servers/{server_id}/users/grouped`](#7-usuarios-del-motor-server-users-y-serversidusers)
> en su lugar — este endpoint sigue existiendo por compatibilidad.

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

#### `GET /api/v1/servers/{server_id}/reconcile` 🔌 *(Plan 09)*

Cruza el **plano en vivo** (lo que existe en el motor) con el **inventario** (lo que
gestiona el gateway) y clasifica cada BD y usuario. Read-only. Respuesta `ReconcileResult`:

| Campo | Tipo | Detalle |
|---|---|---|
| `server_id` | int | — |
| `databases` | list | `{name, state, managed_id?, owner_id?, status?}` |
| `users` | list | `{username, host?, state, managed_id?}` |

`state` ∈ `managed` (en motor **y** en inventario) · `unmanaged` (solo en el motor →
**adoptable**) · `orphan` (solo en el inventario → se borró por fuera).

```json
{
  "data": {
    "server_id": 42,
    "databases": [
      { "name": "app_prod",  "state": "managed",   "managed_id": 7, "owner_id": 3, "status": "active" },
      { "name": "legacy_crm","state": "unmanaged" },
      { "name": "ventas_old","state": "orphan",    "managed_id": 9 }
    ],
    "users": [ { "username": "app_user", "host": "%", "state": "managed", "managed_id": 4 } ]
  }
}
```

#### `GET /api/v1/servers/{server_id}/databases/{database}/snapshot` 🔌 *(Plan 09)*

Snapshot **estructural** de una BD (tablas, vistas, rutinas, triggers, y según motor
secuencias/tipos/extensiones/events). **Solo estructura, nunca filas.** Es la *preview*
(no persiste). Respuesta `StructureDump`:

| Campo | Tipo | Detalle |
|---|---|---|
| `database` | string | — |
| `source_engine` | string | `mysql` \| `mariadb` \| `postgresql` |
| `statements` | list | `{object_type, name, ddl}` en orden de dependencia |
| `has_non_portable` | bool | `true` si incluye objetos procedurales (rutinas/triggers/events) |

```json
{
  "data": {
    "database": "legacy_crm", "source_engine": "mysql", "has_non_portable": true,
    "statements": [
      { "object_type": "table", "name": "clientes", "ddl": "CREATE TABLE `clientes` (...)" },
      { "object_type": "view",  "name": "v_top",    "ddl": "CREATE VIEW `v_top` AS ..." }
    ]
  }
}
```

---

## 7. Usuarios del motor (`/server-users` y `/servers/{id}/users/*`)

Gestiona los usuarios/roles reales del motor. Hay **dos formas** de trabajar con ellos,
que conviven:

- **`/server-users`** — recurso de **inventario** (nivel superior, no anidado bajo
  `/servers`; se filtra con `?server_id=`). Opera por `id` de inventario y solo
  funciona con usuarios **adoptados**.
- **`/servers/{id}/users/*`** — opera por **identidad física** (`server_id` + `username`
  + `host`) directamente sobre el motor, **funcionen o no adoptados** en el inventario.
  Incluye la **vista agrupada** (recomendada como pantalla principal) y endpoints
  **batch** que operan sobre un username completo (todos sus hosts) de una sola
  llamada.

**Todos requieren sesión.** El password se cifra y nunca se devuelve.

### 7.1 Por qué existen dos formas (leer antes de diseñar la UI)

En **MySQL/MariaDB** un usuario **no es una entidad única**: `'alice'@'localhost'` y
`'alice'@'%'` son **cuentas separadas**, cada una con su propia contraseña y sus propios
grants. El listado plano (`GET /servers/{id}/users`, [§6](#6-servidores-servers))
devuelve un `user@host` por cuenta, así que un mismo nombre aparece repetido N veces.

En **PostgreSQL** un `ROLE` **no tiene host** (el acceso por host se controla en
`pg_hba.conf`, fuera del alcance SQL). Un usuario = un rol.

**Asimetría por motor (la UI DEBE respetarla):**

| Concepto | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Identidad de usuario | `'user'@'host'` (varias por nombre) | un `ROLE` (una por nombre) |
| `supports_hosts` (en la respuesta agrupada) | `true` | `false` |
| Columna "host" en la UI | mostrar | **ocultar** |
| Botón "Agregar host" | mostrar | **ocultar** (el endpoint da `422`) |

La respuesta de la vista agrupada trae `supports_hosts`: leela y adaptá la UI —
si es `false`, ocultá la columna host y el botón "Agregar host"; cada usuario tendrá
una sola identidad con `host: null`.

**Por qué los endpoints batch (por username completo):** como cada host es una cuenta
separada en MySQL/MariaDB, gestionar "el usuario alice" como concepto humano obligaba
antes a repetir la misma operación host por host desde el cliente. El admin razona en
términos de "el usuario alice", no de "cada una de sus cuentas": los endpoints batch
cierran esa brecha con **una intención = una llamada**, iterando por dentro sobre el
plano en vivo (no hay sintaxis nativa del motor para "todos los hosts a la vez").

> 🚫 **Trampa a evitar en la UI**: el alcance (un host vs. todos) **NUNCA** debe
> inferirse del valor del campo `host`. En MySQL, `"%"` **es un host real** (comodín de
> conexión al motor), **no** significa "todos los hosts" para el gateway. El alcance
> siempre es una elección **explícita**: un toggle "Este host / Todos los hosts", o el
> campo `scope` en `define-password`.

### 7.2 Endpoints de inventario (`/server-users`, por `id`)

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
| `POST` | `/api/v1/server-users/{user_id}/apply-profile/{profile_id}` | — | 🔌 Aplica un [perfil de permisos](#12-perfiles-de-permisos-permission-profiles). |
| `POST` | `/api/v1/server-users/provision` | — | 🔌 Crea + aprovisiona el usuario + aplica grants iniciales (`201`). |
| `POST` | `/api/v1/server-users/adopt` | — | 🔌 **(Plan 09)** Adopta un usuario que **ya existe** en el motor (sin `CREATE USER`, sin password). `404` si no existe; `409` si ya está. Para adoptar **todos los hosts** de un username de una vez, ver `POST /servers/{id}/users/adopt-all-hosts` (§7.4). |

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

**Grants granulares 🔌** — otorgan/revocan/consultan privilegios del usuario a
cualquier nivel (`database`, `table`, `column`, …). Diferencias por motor: MySQL/MariaDB
usan `global/database/table/column/routine` y el `host` del usuario; PostgreSQL añade
`schema`/`sequence`, ignora el `host`, y **requiere `?database=`** al consultar grants
de objeto.

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

Aplica un [perfil de permisos](#12-perfiles-de-permisos-permission-profiles) al usuario.
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

### 7.3 Vista agrupada y CRUD por identidad (`/servers/{id}/users/*`)

Esta es la pantalla **recomendada** para la gestión principal de usuarios del motor: una
fila por username, expandible a sus identidades (hosts), cruzando el plano en vivo con
el inventario — **funcionen o no adoptados**.

#### `GET /api/v1/servers/{server_id}/users/grouped`

Vista principal. Cruza el motor real con el inventario del gateway y agrupa por
username. Respuesta `GroupedEngineUsersOut`:

```jsonc
{
  "data": {
    "dialect": "mysql",
    "supports_hosts": true,
    "users": [
      {
        "username": "alice",
        "identity_count": 3,
        "identities": [
          { "host": "localhost", "status": "adopted",   "server_user_id": 12,
            "has_password": true,  "is_active": true, "notes": null },
          { "host": "%",         "status": "unmanaged", "server_user_id": null,
            "has_password": false, "is_active": null, "notes": null },
          { "host": "10.0.0.5",  "status": "orphan",    "server_user_id": 33,
            "has_password": true,  "is_active": true, "notes": "temporal" }
        ]
      }
    ]
  },
  "message": "..."
}
```

**Estados de cada identidad** (`status`): `adopted` (en motor **y** inventario) ·
`unmanaged` (solo en el motor → adoptable) · `orphan` (solo en el inventario → drift,
se borró por fuera del gateway).

**PostgreSQL**: `supports_hosts: false`; cada usuario tiene **una sola** identidad con
`host: null`.

Campos clave por identidad: `server_user_id` (llave para navegar a los grants
`/server-users/{id}/grants`, presente si `status != unmanaged`) y `has_password`
(habilita el botón "Revelar contraseña"). No paginado (trae todo el servidor).

#### `POST /api/v1/servers/{server_id}/users` — crear en el motor

Ejecuta `CREATE USER`. Con `adopt=true` además registra en el inventario guardando la
contraseña **cifrada** (habilita revelarla luego).

**Body** — `EngineUserCreateIn`: `{ username, host? (default "%"), password, adopt? (default false), notes? }`.

**Respuesta 201**: `{ username, host, adopted: bool, server_user_id: number | null }`.

**Errores**: `401` · `404` servidor inexistente · `409` credencial pseudo-root · `422` validación.

#### `PATCH /api/v1/servers/{server_id}/users/password` — cambiar contraseña (un host)

Ejecuta `ALTER USER/ROLE`. Si ya hay fila de inventario, la **sincroniza** (queda
revelable). El flag `adopt` solo aplica si **no** había fila previa.

**Body** — `EnginePasswordChangeIn`: `{ username, host? (default "%"), new_password, adopt? }`.

**Respuesta 200**: `{ username, host, adopted: bool, server_user_id: number | null }`.

**Errores**: `401` · `404` · `409` credencial pseudo-root · `422`.

#### `DELETE /api/v1/servers/{server_id}/users` — eliminar (DROP)

Ejecuta `DROP USER/ROLE` y elimina la fila de inventario si existe. **Destructivo e
irreversible.**

**Query**: `username` (requerido), `host` (opcional, default `%`), `confirm_username`
(requerido, debe **repetir exactamente** el username — doble intención).

```bash
curl -X DELETE "https://<host>/api/v1/servers/42/users?username=alice&host=%25&confirm_username=alice" \
  -b cookies.txt
```

**Errores**: `401` · `404` · `409` el usuario posee BDs gestionadas o es la credencial
pseudo-root · `422` `confirm_username` no coincide.

#### `POST /api/v1/servers/{server_id}/users/add-host` — clonar cuenta a un nuevo host

Clona `'user'@'source_host'` a `'user'@'new_host'` (`CREATE USER`). **Solo MySQL/MariaDB**
→ `422` en PostgreSQL.

**Body** — `AddHostIn`: `{ username, source_host? (default "%"), new_host, reuse_password? (default true), new_password? (requerido si reuse_password=false), copy_grants? (default false), adopt? , notes? }`.

- `reuse_password: true` — copia el **hash** (misma contraseña; el gateway no la descubre en claro).
- `copy_grants: true` — replica permisos del origen; **best-effort** (`grants_error` si falla parcialmente).

**Respuesta 201**: `{ username, new_host, password_mode: "reused"|"new", grants_copied: number, grants_error: string|null, adopted: bool, server_user_id: number|null }`.

> **Advertencia si `copy_grants=true`**: se replican fielmente privilegios **globales**
> y `WITH GRANT OPTION` del origen — avisá del riesgo de sobre-aprovisionamiento.

**Errores**: `401` · `404` · `409` credencial pseudo-root · `422` en PostgreSQL, o
`reuse_password=false` sin `new_password`.

#### `POST /api/v1/servers/{server_id}/users/reveal-password` — revelar contraseña

Devuelve la contraseña **en claro**, solo cuando el gateway la conoce. **Acción auditada.**

**Body**: `{ username, host? }`. **Respuesta 200**: `{ username, host, password }`.

**Límite criptográfico**: el motor solo guarda un **hash irreversible** — una
contraseña que el gateway **nunca conoció** es irrecuperable; el gateway solo puede
revelar una que **él mismo fijó** (create, rotación, o `define-password`, ver abajo).

| Situación | Código |
|---|---|
| Usuario no en el inventario | `404` |
| Adoptado, pero el gateway no conoce la contraseña | `409` |
| Contraseña fijada por el gateway | `200` |

> **UX**: habilitá "Revelar" solo si `has_password: true`. Tratá la contraseña como
> secreto efímero (no la persistas en el cliente).

### 7.4 Operaciones batch (username completo, todos los hosts)

Tres endpoints que operan sobre **todas las identidades en vivo** de un username de una
sola llamada, sin iterar host por host desde el cliente. Todos son **fail-tolerant por
host** (un host que falla no aborta los demás) y devuelven **200/201 con un array
`results[]`** — el desenlace real vive ahí, **no** en el código HTTP.

> ⚠️ **Distinción crítica: DEFINIR vs. ROTAR/CAMBIAR contraseña.** Son operaciones
> **conceptualmente distintas** y la UI **no debe mezclarlas en un mismo flujo**:
>
> | | **Definir** (`define-password`) | **Rotar/Cambiar** (`password`, `password-all-hosts`) |
> |---|---|---|
> | ¿Toca el motor? | **No** — nunca ejecuta `ALTER USER/ROLE` | **Sí** |
> | ¿Qué hace? | Cifra y guarda una contraseña que el admin **ya conoce** | Cambia la contraseña **real** vigente |
> | Riesgo | El gateway **no puede verificar** que sea la real | Ninguno — la real pasa a ser la enviada |
>
> `define-password` resuelve un hueco real: antes, adoptar sin contraseña dejaba
> `has_password: false` **para siempre**, sin forma de rellenarlo salvo rotando de
> verdad (lo que puede romper apps que usan la clave actual). Con `define-password`, el
> admin humano que **sabe** cuál es la contraseña vigente se la "dicta" al gateway sin
> tocar el motor.

#### `POST /api/v1/servers/{server_id}/users/adopt-all-hosts`

Adopta en una sola operación **todas** las identidades en vivo de un username. Nunca
ejecuta `CREATE USER` — solo registra lo que ya existe (`origin='adopted'`).

**Body** — `AdoptAllHostsIn`: `{ username, known_password? (si se envía, se cifra y guarda en TODAS las filas SIN ejecutar ALTER USER), notes? }`.

**Respuesta 201** — `BatchAdoptOut`:
```jsonc
{ "username": "alice", "dialect": "mysql", "total_hosts": 3, "adopted": 2,
  "results": [
    { "host": "localhost", "status": "adopted",        "server_user_id": 41 },
    { "host": "%",         "status": "already_adopted", "server_user_id": 12 },
    { "host": "10.0.0.5",  "status": "adopted",         "server_user_id": 42 }
  ] }
```
`results[].status`: `adopted` | `already_adopted` (no es error — un mix de ambos es un
éxito normal). En PostgreSQL, `results[].host` es `null`.

**Errores**: `401` · `404` el username no existe en vivo (nada que adoptar) · `409`
credencial pseudo-root · `422`.

#### `POST /api/v1/servers/{server_id}/users/define-password`

Cifra y guarda una contraseña ya conocida por el admin, **sin tocar el motor**.

**Body** — `DefineKnownPasswordIn`: `{ username, scope: "host"|"all_hosts", host? (solo si scope="host"; "%" es un host REAL), known_password, adopt_if_missing? (crea la fila para hosts en vivo sin ella), overwrite? (OBLIGATORIO true para sobrescribir una identidad que ya tenía contraseña guardada) }`.

**Respuesta 200** — `KnownPasswordSetOut`:
```jsonc
{ "username": "alice", "scope": "all_hosts", "total_hosts": 2, "updated": 1,
  "results": [
    { "host": "%",         "status": "updated",                  "server_user_id": 12 },
    { "host": "localhost", "status": "conflict_needs_overwrite", "server_user_id": 41 }
  ] }
```
`results[].status`: `updated` | `adopted` | `skipped_not_found` | `conflict_needs_overwrite`
(no es error — reenviar con `overwrite=true` si el admin confirma).

> ⚠️ **Aviso obligatorio en la UI**: el gateway **no verifica** que `known_password` sea
> la real. Si el admin se equivoca, `reveal-password` devolverá luego un valor
> incorrecto sin que nadie lo detecte.

**Errores**: `401` · `404` (`scope="all_hosts"` y el username no existe en vivo) · `409`
credencial pseudo-root · `422`.

#### `PATCH /api/v1/servers/{server_id}/users/password-all-hosts`

Ejecuta `ALTER USER/ROLE` para cambiar la contraseña **real** en **todos** los hosts en
vivo de un username. Es la versión batch de `PATCH /users/password`. **Sí toca el
motor**, fail-tolerant.

**Body** — `EnginePasswordChangeAllHostsIn`: `{ username, new_password, confirm_username (OBLIGATORIO, debe coincidir EXACTO — doble intención), adopt_if_missing? }`.

**Respuesta 200** — `PasswordChangeBatchOut`:
```jsonc
{ "username": "alice", "total_hosts": 2, "updated": 1,
  "results": [
    { "host": "%",         "status": "rotated", "server_user_id": 12,   "adopted": false, "error": null },
    { "host": "localhost", "status": "error",   "server_user_id": null, "adopted": false, "error": "motor caído" }
  ] }
```

> ⚠️ **Fail-tolerant → estado divergente posible**: un host con `status: "error"`
> **conserva la contraseña ANTERIOR** en el motor real. Renderizá `results` por host y
> **nunca** un "contraseña cambiada" genérico que oculte un fallo parcial.

**Errores**: `401` · `404` username no existe en vivo · `409` credencial pseudo-root ·
`422` `confirm_username` no coincide.

### 7.5 Semántica de errores y flujo (resumen de la sección)

| Código | Causa |
|---|---|
| `401` | Sin sesión admin. |
| `404` | Servidor/usuario inexistente; en los batch, el username no existe **en vivo** en el motor. |
| `409` | Credencial **pseudo-root** del gateway (guard anti-lockout); eliminar un usuario con BDs gestionadas; revelar sin que el gateway conozca la contraseña. |
| `422` | Whitelist de `username`/`host`; `add-host` en PostgreSQL; confirmación (`confirm_username`) incorrecta; `reuse_password=false` sin `new_password`. |

**Acciones según `status` de una identidad** (vista agrupada):

| `status` | Qué ofrecer |
|---|---|
| `adopted` | Revelar (si `has_password`) · Rotar · Eliminar · Ver grants (`server_user_id`) |
| `unmanaged` | Adoptar (rotar/crear con `adopt=true`, o el endpoint singular `/server-users/adopt`) · Rotar · Eliminar · Agregar host |
| `orphan` | Resolver **drift**: recrear en el motor (`create`) o limpiar la fila huérfana (`DELETE /server-users/{id}`) |

**Flujo recomendado**: `GET /users/grouped` → leer `supports_hosts` una vez → por
identidad expandida, ofrecer acciones según `status` → por username, ofrecer las
acciones **batch** (adoptar todos, definir/rotar contraseña de todos) → tras cualquier
escritura, recargar la vista agrupada o actualizar el estado local afectado.

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
| `POST` | `/api/v1/database-models/from-snapshot` 🔌 | **(Plan 09)** Crea un blueprint cuyo baseline (`0001`) es el snapshot estructural de una BD existente. Rate limit **10/min**. Nace `is_baseline=true` y `reviewed=false` (ver el gate R1 más abajo). |

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
| `version` | string \| null | **no** | patrón `^\d{4,10}$`. **Si se omite, el gateway autoasigna la siguiente secuencial** (`max+1`). Pásala solo para fijarla a mano. Se ordena NUMÉRICAMENTE. |
| `name` | string | sí | 1–200 caracteres |
| `up_sql` | string | sí | delta SQL base (estilo MySQL); 1–262144 chars (256 KB) |
| `up_sql_mysql` | string \| null | no | override manual MySQL/MariaDB; ≤256 KB |
| `up_sql_postgresql` | string \| null | no | override manual PostgreSQL; ≤256 KB |
| `down_sql` | string \| null | no | rollback **confirmado**; ≤256 KB. Sin él, el rollback responde `409` |

> **Versión autoasignada (recomendado).** Como la numeración es entera y secuencial, **`version`
> es opcional**: si se omite, el gateway calcula y asigna **la siguiente** (`max(versión)+1`,
> p. ej. `0007`) de forma autónoma. Pensado para equipos: con varios colaboradores nadie necesita
> consultar antes "cuál fue la última". La asignación es **segura ante concurrencia** (reintenta
> ante colisión con el `UNIQUE` por blueprint). Si pasas `version` manualmente y ya existe → `409`.

`ModelMigrationPatch` (body de `PATCH`): `name?`, `down_sql?`, `up_sql_mysql?`,
`up_sql_postgresql?`, `reviewed?` (aprueba un baseline de snapshot — gate R1, ver abajo).
**No** se puede modificar el SQL de una migración ya aplicada **exitosamente** en alguna
BD (`409`) — un intento que solo falló no congela el SQL.

`ModelMigrationOut` (detalle): `{ id, model_id, version, name, up_sql, up_sql_mysql?,
up_sql_postgresql?, down_sql?, down_sql_suggested?, translated: {mysql, postgresql}, checksum,
source_engine?, is_baseline, has_non_portable, reviewed, created_at, updated_at }`.
`down_sql_suggested` es un rollback **auto-generado** para operaciones aditivas (revísalo y
confírmalo con `PATCH`).

`ModelMigrationSummary` (item de listado): `{ id, model_id, version, name,
has_mysql_override, has_postgresql_override, has_rollback, checksum, is_baseline, reviewed,
created_at }`.

#### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/database-models/{model_id}/migrations` | Lista paginada (resúmenes). |
| `POST` | `/api/v1/database-models/{model_id}/migrations` | Crea una migración (`201`). **`version` es opcional**: si se omite, el gateway autoasigna la siguiente secuencial (max+1). Devuelve `translated` + `down_sql_suggested`. |
| `GET` | `/api/v1/database-models/{model_id}/migrations/{version}` | Detalle completo. |
| `PATCH` | `/api/v1/database-models/{model_id}/migrations/{version}` | Confirma `down_sql` / añade overrides / **aprueba un baseline de snapshot** (`reviewed: true`, gate R1). |
| `DELETE` | `/api/v1/database-models/{model_id}/migrations/{version}` | Elimina — **solo la última versión** (la punta) y **solo si no** tiene historial de aplicación; si no, `409`. |
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

**Crear SIN pasar versión (autoasignada — recomendado para equipos):** omite el campo `version`
y el gateway le pone la siguiente secuencial. Si el blueprint ya tiene `0001`, esta queda `0002`:

```bash
curl -X POST https://<host>/api/v1/database-models/3/migrations -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "name": "Add status", "up_sql": "ALTER TABLE orders ADD COLUMN status VARCHAR(20)" }'
```

```json
{ "data": { "version": "0002", "name": "Add status", "reviewed": true, "is_baseline": false,
            "down_sql_suggested": "ALTER TABLE orders DROP COLUMN status;", "checksum": "…" },
  "message": "Migración creada." }
```

> **Concurrencia:** si dos colaboradores crean a la vez sin `version`, el gateway resuelve el
> empate (reintenta con el siguiente número); ninguno tiene que consultar la última versión.
> Pasar `version` manualmente sigue siendo válido; una versión explícita duplicada da `409`.

> Cuándo escribir `up_sql_postgresql` manual: `ENUM(...)` inline, `ON UPDATE CURRENT_TIMESTAMP`,
> `UNSIGNED/ZEROFILL`, `ALTER … MODIFY … AUTO_INCREMENT`/`DROP PRIMARY KEY` (PostgreSQL usa
> `ALTER COLUMN … TYPE`/`DROP CONSTRAINT`, que no se derivan automáticamente) y rutinas
> `BEGIN…END` con `;` internos no se traducen de forma fiable. `AUTO_INCREMENT`, backticks y
> `DATETIME` sí. Si aplicás a PostgreSQL una migración con este tipo de DDL sin override, el
> `apply` responde **422** con el detalle de qué construcción no se pudo traducir — antes de
> tocar el motor, nunca ejecuta SQL roto.

**Confirmar el rollback sugerido:**

```bash
curl -X PATCH https://<host>/api/v1/database-models/3/migrations/0001 -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "down_sql": "DROP TABLE IF EXISTS orders" }'
```

#### El gate `reviewed` (R1) — por qué un `apply` puede bloquearse aunque tu versión esté bien

Cuando creás un blueprint **desde un snapshot** (`POST /database-models/from-snapshot`),
su migración baseline (`0001`) contiene **DDL capturado del motor real** — estructura
que el gateway no generó y trata como **potencialmente no confiable** (puede traer
vistas/rutinas/triggers con lógica arbitraria). Por eso ese baseline nace **sin
aprobar** (`reviewed: false`) y el gateway **bloquea cualquier `apply`** de ese
blueprint hasta que un admin lo revise y apruebe.

**El detalle que confunde:** el bloqueo es **a nivel de blueprint**, no de versión.
Aunque crees una **nueva versión** (`0002`, escrita a mano y ya `reviewed: true`) e
intentes actualizar la BD a ella, el `apply` **sigue bloqueado** porque el baseline
`0001` —base de todo el esquema— continúa sin revisar:

```
409 Conflict
{ "detail": { "msg": "El blueprint tiene un baseline de snapshot SIN revisar (0001).
   Contiene DDL capturado del motor: revísalo y apruébalo (PATCH reviewed=true en esa
   versión) antes de aplicar." } }
```

Resolución: `GET .../migrations/{version}` la versión que el mensaje señala → revisar
su `up_sql` → `PATCH .../migrations/{version}` `{ "reviewed": true }` → reintentar el
`apply`. `reviewed` **no** afecta a `stamp` (que no ejecuta SQL).

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
| `on_failure` | string | `"auto"` | `auto`\|`reconcile`\|`leave` — qué hacer si una migración falla a mitad en alguna BD (ver §9). |

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
  "status": "active", "notes": null, "origin": "provisioned",
  "created_at": "2026-06-23T10:00:00Z", "updated_at": "2026-06-23T10:00:00Z"
}
```

> `origin` *(Plan 09)*: `provisioned` (creada por el gateway) | `adopted` (preexistente,
> registrada con `POST /managed-databases/adopt`).

### Endpoints

| Método | Ruta | Query | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/managed-databases` | `page`, `size`, `server_id?`, `owner_id?`, `model_id?`, `status?` | Lista paginada con filtros (`status` ∈ `ProvisionStatus`). |
| `POST` | `/api/v1/managed-databases` | `provision=false` | Registra (`201`, status `pending`). Con `provision=true` 🔌 ejecuta `CREATE DATABASE` + `GRANT` al owner. |
| `GET` | `/api/v1/managed-databases/{db_id}` | — | Detalle. |
| `PATCH` | `/api/v1/managed-databases/{db_id}` | — | Actualiza metadata. |
| `DELETE` | `/api/v1/managed-databases/{db_id}` | `drop_remote=false`, `confirm_name?` | Elimina del inventario. Con `drop_remote=true` 🔌 ejecuta `DROP DATABASE`. |
| `POST` | `/api/v1/managed-databases/{db_id}/reassign-owner` | `provision=false` | Cambia el owner. Con `provision=true` 🔌 revoca/otorga (o `ALTER OWNER` en PG). |
| `POST` | `/api/v1/managed-databases/adopt` | — | 🔌 **(Plan 09)** Adopta una BD que **ya existe** en el motor (sin `CREATE DATABASE`; status `active`, `origin=adopted`). `404` si no existe; `409` si ya está. Ver **stamp-on-adopt** abajo. |

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

**Adoptar una BD preexistente, con "stamp-on-adopt"** 🔌 *(Plan 09)*:

```
POST /api/v1/managed-databases/adopt
Body: { server_id, name, owner_id, model_id?, model_version? }
```

Si pasás `model_id` + `model_version`, el gateway hace **stamp** de esa versión en el
motor destino como parte de la adopción — así un `apply` posterior no reintenta crear
objetos que ya existen físicamente en la BD adoptada. `model_version` requiere
`model_id` y se valida que exista **antes** de insertar (`422` si no). No inyecta
`IF NOT EXISTS`: resuelve el conflicto de "ya existe" sin enmascarar drift.

```bash
curl -X POST https://<host>/api/v1/managed-databases/adopt -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "server_id": 5, "name": "legacy_crm", "owner_id": 7, "model_id": 9, "model_version": "0003" }'
```

### Migraciones sobre la BD gestionada 🔌

Aplica/revierte/consulta las migraciones del [blueprint](#8-blueprints-de-bd-database-models)
asignado (`model_id`) **sobre esta BD real**. La versión actual de cada BD la mantiene el
gateway dentro de la propia BD destino (tabla `_gw_v_{slug}`, gestionada con Alembic
embebido); el historial queda en el gateway. Requieren que la BD tenga un blueprint
asignado (`422` si no). Rate limit **10/min** en `apply`/`rollback`/`stamp`/`reconcile-partial`.

| Método | Ruta | Query | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/managed-databases/{db_id}/migrations/status` | — | Versión actual vs. pendientes, y si hay una **aplicación parcial** pendiente de resolver. |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/apply` | `version?`, `force?`, `dry_run?`, `on_failure?` | **Una sola llamada** aplica secuencialmente, en orden, **todas** las pendientes hasta `version` (o hasta la **última** si se omite). Forward-only (`version` ≤ actual → no-op). `422` si `version` no existe, o si el SQL no se puede traducir con certeza al motor destino; `409` si hay un baseline de snapshot sin revisar (gate R1, §8). Respuesta `MigrationApplyOut` con `from_version`→`to_version` y (si aplica) `reconciliation`. |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/rollback` | `confirm_version` (**obligatorio**), `target_version?` | **Una sola llamada** revierte secuencialmente hasta `target_version` (anterior a la actual); sin él, revierte solo la última. `409` si alguna versión del camino no tiene `down_sql` confirmado, **o si la BD tiene una aplicación parcial sin resolver** (ver abajo). Respuesta `MigrationRollbackOut` con `from_version`→`to_version`. |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/stamp` | `version` (**obligatorio**), `force?` | Marca una versión **sin ejecutar SQL** (BDs pre-existentes). ⚠️ Ver la advertencia sobre el anti-patrón más abajo antes de usarlo para "arreglar" un fallo de `apply`. |
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/reconcile-partial` | `confirm_version` (**obligatorio**), `dry_run?`, `force?` | Deshace las sentencias que **sí** se aplicaron de una migración que falló a mitad. Ver el detalle completo más abajo. |
| `GET` | `/api/v1/managed-databases/{db_id}/migrations/history` | `page`, `size` | Historial paginado de aplicaciones. |

**Query params de `apply`:**

| Parámetro | Tipo | Default | Detalle |
|---|---|---|---|
| `version` | string \| null | — | Patrón `^\d{4,10}$`. Versión objetivo: en **una llamada** aplica todas las pendientes hasta ella (o hasta la última si se omite). Forward-only; `422` si no existe. |
| `force` | bool | `false` | Reintenta una BD en **cuarentena** (tras inspección). |
| `dry_run` | bool | `false` | No aplica: devuelve el plan (`from_version` + `pending_versions`). |
| `on_failure` | string | `"auto"` | `auto`\|`reconcile`\|`leave` — ver "Aplicación parcial" abajo. Solo relevante en MySQL/MariaDB. |

**Estado (`status`)** — `MigrationStatusOut`:

```bash
curl https://<host>/api/v1/managed-databases/5/migrations/status -b cookies.txt
```
```jsonc
{ "data": { "managed_database_id": 5, "model_id": 3, "slug": "whatsapp",
            "current_version": null, "latest_available": "0002",
            "pending_count": 2, "pending_versions": ["0001","0002"],
            "has_partial_application": false,   // true = hay algo a medias, ver abajo
            "partial_application": [] } }
```

**Previsualizar (dry-run):**

```bash
curl -X POST "https://<host>/api/v1/managed-databases/5/migrations/apply?dry_run=true" -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "database_name": "app_prod", "server_id": 42,
            "dry_run": true, "from_version": null, "to_version": "0002", "target_version": null,
            "no_op": false, "pending_versions": ["0001","0002"] } }
```

**Aplicar:** respuesta `MigrationApplyOut` con el salto `from_version`→`to_version`.

```bash
curl -X POST https://<host>/api/v1/managed-databases/5/migrations/apply -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "database_name": "app_prod", "server_id": 42,
            "from_version": null, "to_version": "0002", "target_version": null,
            "applied_count": 2, "failed": false, "quarantined": false, "no_op": false,
            "pending_versions": ["0001","0002"],
            "results": [ { "migration_id": 1, "version": "0001", "status": "applied", "error": null, "execution_ms": 42,
                           "resumed": false, "resumed_from_statement": null, "statement_total": 4, "failed_at_statement_index": null },
                         { "migration_id": 2, "version": "0002", "status": "applied", "error": null, "execution_ms": 31,
                           "resumed": false, "resumed_from_statement": null, "statement_total": 2, "failed_at_statement_index": null } ],
            "reconciliation": null },
  "message": "Aplicadas 2 migración(es): ∅ → 0002." }
```

- `results[].resumed: true` — este intento **retomó automáticamente** desde un fallo
  parcial previo (checkpoint de sentencia); `resumed_from_statement` indica desde cuál.
- `results[].failed_at_statement_index` — si `status: "failed"`, la sentencia (1-based)
  en la que murió, de `statement_total`. Puede ser `null` si la migración no es elegible
  para checkpoint (datos-semilla, objetos no portables, o sentencias con estado de
  sesión) — ahí el fallo sigue siendo todo-o-nada. **Nunca** se expone el SQL de la
  sentencia (puede tener secretos), solo el índice.
- Estos mismos campos aplican en `results[]` de `MigrationRollbackOut`
  (`POST .../migrations/rollback`), con la misma semántica.

> `422` si la `version` objetivo no existe en el blueprint · `409` si hay un **baseline
> de snapshot sin revisar** (gate R1, ver [§8](#8-blueprints-de-bd-database-models)) ·
> `422` si el SQL tiene construcciones de MySQL que no se traducen con certeza a
> PostgreSQL (ver la nota de `up_sql_postgresql` en §8) — se detecta **antes** de tocar
> el motor.

#### Aplicación parcial — qué pasa cuando un `apply` falla a mitad de camino

El DDL de MySQL/MariaDB hace **commit por sentencia** (no es transaccional). Si una
migración de N sentencias falla en la k, la BD queda **físicamente** con k cambios
aplicados, pero `current_version` no se actualiza hasta que la migración *entera*
termina — puede leerse como sano sin estarlo. **En PostgreSQL esto casi nunca ocurre**:
el motor ejecuta DDL transaccional y deshace la migración fallida por sí solo.

El query param **`on_failure`** de `apply` decide qué hacer (solo relevante en
MySQL/MariaDB):

| Valor | Comportamiento |
|---|---|
| `auto` (default) | Si falla a mitad, el backend deshace automáticamente lo aplicado — **pero solo si puede deshacerlo TODO**. Si lo logra, la BD queda limpia en su versión anterior y **NO entra en cuarentena**. |
| `reconcile` | Igual que `auto`, pero si hay sentencias sin reverso las saltea y las reporta (en vez de no intentar nada). |
| `leave` | No deshace nada — comportamiento histórico (cuarentena + checkpoint, requiere intervención manual). |

`MigrationApplyOut.reconciliation` (`null` si no hizo falta) informa qué hizo el
sistema:

```jsonc
// apply?on_failure=auto, la migración 0008 falló en la sentencia 6 de 12
{
  "managed_database_id": 1, "from_version": "0007", "to_version": "0007",
  "applied_count": 0, "failed": true, "quarantined": false,
  "reconciliation": {
    "version": "0008", "attempted": true,
    "undone_count": 5, "statements_to_undo": 5,
    "fully_reconciled": true,
    "unconfirmed_reverses": [], "unreversible_statements": [],
    "error": null
  }
}
```

**Mensaje sugerido para la UI** cuando `reconciliation.fully_reconciled: true`: *"La
migración 0008 falló, pero el sistema deshizo automáticamente los cambios que había
aplicado. La base de datos volvió a la versión anterior (0007) sin intervención
necesaria. Corregí la migración y reintentá."* — esto reemplaza cualquier mensaje de
"la BD quedó en cuarentena, contactá a un DBA" para el caso feliz (la mayoría).

⚠️ **`reconciliation: null` con `failed: true` NO significa "no pasó nada".** Puede ser:
(1) la migración falló en su **primera** sentencia — no había nada que reconciliar
(caso benigno); (2) se usó `on_failure=leave` explícito; (3) `on_failure=auto` pero
había sentencias sin reverso seguro — el sistema **deliberadamente no intentó** una
reconciliación parcial. La única forma confiable de distinguirlos es consultar
`GET .../migrations/status`: si `has_partial_application: true`, es el caso 2 o 3.

**`GET .../migrations/status`** informa el detalle si algo quedó a medias:

```jsonc
{ "has_partial_application": true,
  "partial_application": [
    { "version": "0008", "model_migration_id": 20,
      "applied_statements": 6, "total_statements": 12,
      "reconcilable": true, "reason": null, "statements_to_undo": 6 }
  ] }
```

Puede haber **más de una entrada** en `partial_application[]` (migraciones distintas
que quedaron a medias en momentos distintos). `reconcile-partial` solo resuelve **la de
versión más alta** por llamada — si hay varias, hace falta iterar de la más alta a la
más baja.

#### `POST .../migrations/reconcile-partial` — deshacer a mano una aplicación parcial

```
POST /api/v1/managed-databases/{db_id}/migrations/reconcile-partial?confirm_version=0008&dry_run=true
```

| Param | Requerido | Descripción |
|---|---|---|
| `confirm_version` | sí | Debe ser la versión que aparece en `partial_application[].version` (doble intención). |
| `dry_run` | no | `true` devuelve los reversos exactos SIN ejecutar nada. Recomendado llamarlo primero **siempre** — ⚠️ salvo que haya sentencias sin reverso: ver nota abajo. |
| `force` | no | Procede aunque alguna sentencia ya aplicada no tenga reverso — la saltea y la reporta (`409` sin esto). |

```jsonc
// dry_run=true
{ "data": {
    "managed_database_id": 1, "database_name": "db1", "server_id": 2,
    "version": "0008", "applied_statements": 6, "total_statements": 12,
    "statements_to_undo": 6, "unreversible_statements": [], "unconfirmed_reverses": [],
    "dry_run": true,
    "statements": [
      { "seq": 6, "sql": "DROP INDEX \"ix_z\"" },
      { "seq": 5, "sql": "ALTER TABLE \"t\" DROP CONSTRAINT \"fk_x\"" }
    ] } }
```

Con `dry_run=false` (default): mismo shape con `undone_count`, `failed`,
`fully_reconciled`, `remaining_applied_statements` y `results[]` por sentencia.
`unconfirmed_reverses` lista reversos que **sí se ejecutaron** pero no son
demostrablemente seguros (p. ej. recrear una tabla borrada devuelve la estructura, no
las filas) — mostrarlos como aviso, no como error.

⚠️ **`dry_run=true` NO garantiza previsualizar si hay sentencias sin reverso.** El
backend valida `force` **antes** de mirar `dry_run`: si el plan tiene sentencias sin
`down_sql` y no se pasó `force=true`, la llamada responde **409 incluso en modo
dry-run**. Si el primer intento con `dry_run=true` da 409 con
`public_context.unreversible_statements`, reintentá con `dry_run=true&force=true` para
ver el plan completo antes de decidir si se ejecuta de verdad.

**NO es un `downgrade` de Alembic**: la versión parcial nunca se registró, así que no
se toca la tabla de versión — es una **compensación** que devuelve el plano físico al
estado que el ledger ya afirma.

**Requiere que la versión tenga manifiesto de sentencias** (lo tienen las versiones
adoptadas desde un diff estructural — [§10](#10-comparación-de-esquemas-entre-bds-schema-comparisons)).
Sin él, `reconcilable: false` con un `reason` explicando por qué, y la salida es
reintentar `apply` (retoma del checkpoint) o `stamp?force=true` tras reconciliar a
mano.

#### ⚠️ El anti-patrón que la UI nunca debe sugerir

La reacción intuitiva ante un `apply` que falla a mitad es `stamp --force` a la versión
que falló y después `rollback`. **Esto EMPEORA el problema**: el `stamp` **afirma** que
la migración se aplicó completa, así que el `rollback` posterior ejecuta **todos** los
reversos contra una BD que solo tiene una fracción de los cambios físicos — los
reversos de lo que nunca corrió fallan ("no existe") y el rollback muere a mitad,
dejando un **tercer** estado inconsistente. Además, `stamp --force` descarta el
checkpoint, que era la única prueba de dónde había quedado.

Las vías correctas, en orden de preferencia: (1) `on_failure=auto`/`reconcile` en el
propio `apply` (automático); (2) `reconcile-partial` (explícito); (3) reintentar
`apply` sin más (retoma del checkpoint). `stamp --force` es solo para "ya reconcilié el
estado físico a mano" — su `409` lo explica.

**Rollback (DESTRUCTIVO — doble confirmación), target-based y secuencial:** `confirm_version` debe
igualar la versión actual; `target_version` (opcional, anterior a la actual) revierte
secuencialmente hasta ahí en **una llamada** (sin él, revierte solo la última).

```bash
# Estando en 0010, revertir hasta 0007 (deshace 0010, 0009, 0008):
curl -X POST "https://<host>/api/v1/managed-databases/5/migrations/rollback?confirm_version=0010&target_version=0007" -b cookies.txt
```
```json
{ "data": { "managed_database_id": 5, "from_version": "0010", "to_version": "0007",
            "target_version": "0007", "reverted_count": 3,
            "reverted_versions": ["0010","0009","0008"], "failed": false, "no_op": false },
  "message": "Revertidas 3 migración(es): 0010 → 0007." }
```

> `422` si `confirm_version` no coincide con la actual o `target_version` no es anterior/no existe ·
> `409` si **alguna** versión del camino no tiene `down_sql` confirmado, o si
> `has_partial_application: true` (resolvé primero con `reconcile-partial` o
> reintentando `apply`).

El cuerpo del `409` por `down_sql` faltante trae siempre (en cualquier entorno)
`detail.public_context.missing_down_sql` con la lista exacta de versiones — es el
campo a leer para armar un flujo guiado (listar esas versiones y enlazar a
`PATCH .../migrations/{version}` de cada una), **no** `detail.context` (dev-only).

**Historial:**

```json
{ "data": [ { "id": 10, "managed_database_id": 5, "model_migration_id": 2, "version": "0002",
              "applied_at": "2026-06-26T10:00:00Z", "status": "applied", "error": null, "execution_ms": 31 } ],
  "pagination": { "page": 1, "size": 20, "total": 2, "...": "..." } }
```

> **Cuarentena:** una migración multi-sentencia que falla a mitad (con `on_failure=leave`,
> o si `auto`/`reconcile` no pudieron reconciliar del todo) deja la BD en
> `status: "error"` (con detalle en `notes`); el siguiente `apply` responde `409` hasta
> que inspecciones y reintentes con `?force=true`. Con `on_failure=auto` y sin
> sentencias irreversibles, este estado no debería acumularse — es la recuperación de
> último recurso, no el flujo esperado. El gateway re-valida el `checksum` antes de
> aplicar: si la migración fue alterada en la BD del gateway, responde `409`.

---

## 10. Comparación de esquemas entre BDs (`/schema-comparisons`)

Compara la **estructura** de dos bases de datos gestionadas (mismo motor, o
MySQL↔MariaDB) y, a partir del diff, permite **adoptar** el resultado como una nueva
versión de un blueprint (Opción A, integrándose con [§8](#8-blueprints-de-bd-database-models))
o **ejecutarlo directamente** sobre la BD destino (Opción B, para BDs sin blueprint).
Ambos lados aceptan una BD ya en el inventario **o** una BD cruda de cualquier servidor
dado de alta (sin necesidad de registrarla primero).

### 10.1 Entidades

`SchemaComparisonSummaryOut` (`data` de crear y de `GET /{id}`):

```
- id:                    number
- source_server_id:      number   (SIEMPRE poblado, sea la BD adoptada o cruda)
- source_database_name:  string   (SIEMPRE poblado)
- target_server_id:      number   (SIEMPRE poblado)
- target_database_name:  string   (SIEMPRE poblado)
- source_database_id:    number | null   (managed_database_id SI está en el inventario; null = BD cruda)
- target_database_id:    number | null   (ídem — null = la Opción A NO está disponible para esta comparación)
- source_engine / target_engine: enum [mysql, mariadb, postgresql]
- cross_flavor_warning:  boolean  (true si es MySQL↔MariaDB — ruido esperable)
- scope_note:            string | null  (p. ej. en PostgreSQL: "solo schema public")
- item_count:            number   (nº de SENTENCIAS del diff — ver nota abajo)
- counts:                object   (mapa object_type → {new?, modified?, dropped?} — cuenta OBJETOS)
- has_destructive:       boolean
- expired:               boolean  (true si venció el TTL → recalcular)
- created_at / expires_at: date
```

> **`item_count` cuenta SENTENCIAS, `counts` cuenta OBJETOS.** Un mismo objeto lógico
> puede generar varias sentencias (p. ej. en PostgreSQL, modificar una columna puede
> rendir 2-3 `ALTER COLUMN`). La selección/ejecución es por `id` de sentencia
> individual, pero conviene **agrupar visualmente por `object_name`** — y, más
> importante, por `op_group` (ver abajo).

`SchemaComparisonItemOut` (cada elemento de `GET /{id}/items`):

```
- id:               number   (clave para selección/ejecución)
- comparison_id:     number
- seq:               number   (orden de EJECUCIÓN — ver §10.2, ÚNICA fuente de verdad)
- object_type:       enum [table, column, primary_key, foreign_key, unique_constraint,
                     check_constraint, index, view, materialized_view, routine,
                     trigger, event, sequence, enum_type, extension]
- object_name:       string   (p. ej. "productos.descripcion")
- change_type:       enum [new, modified, dropped]
- phase:             number   (etiqueta INFORMATIVA de agrupación, 1..9 — NO ordena, ver §10.2)
- sql:               string   (DDL EXACTO a correr en el TARGET)
- risk_flags:        object RiskFlags
- down_sql:          string | null   (rollback inferido; null si no reversible con certeza)
- down_confirmed:    boolean
- op_group:          string | null   (grupo ATÓMICO del cambio — ver §10.2. Puede ser null
                     en comparaciones creadas ANTES de esta funcionalidad: tratalo como
                     "ítem suelto, sin grupo ni dependencias conocidas")
- depends_on:        array<string>   (op_group que deben ejecutarse ANTES — ver §10.2)
- execution_status:  enum | null [applied, failed, skipped]
- execution_error:   string | null
- executed_at:       date | null
```

```
Entidad: RiskFlags (embebido en cada ítem)
- destructive:                boolean   (potencialmente destructivo)
- lock_heavy:                 boolean   (puede tomar locks pesados)
- data_conversion:            boolean   (implica conversión de datos)
- needs_review:               boolean
- requires_individual_review: boolean   (objeto procedural: NUNCA en modos automáticos;
                              obliga selección manual + ver el cuerpo completo)
- cross_flavor_warning:       boolean   (ítem afectado por comparación MySQL↔MariaDB)
- possible_rename_of:         string | null   (heurística advisory: si NO es null, este
                              DROP/CREATE probablemente sea un RENAME de ese objeto)
```

### 10.2 `seq` es el orden; `phase` es solo una etiqueta

**Ordená y ejecutá siempre por `seq`, nunca por `phase`.** `phase` (1..9) es una
etiqueta gruesa del pipeline conceptual (prerrequisitos → tablas → aditivos → cuerpos →
destructivos → …), útil para un filtro o badge de color en la UI, pero el orden
**real** de ejecución puede cruzar fases: una FK (fase 3) puede depender de una
`PRIMARY KEY` que se agrega en fase 4 sobre la tabla referida, y en ese caso la FK se
ejecuta **después**, aunque su `phase` sea menor. `seq` ya viene con eso resuelto — es
el resultado de un ordenador topológico, no de un simple orden por fase.

### 10.3 Grupos atómicos (`op_group`) y dependencias (`depends_on`)

**`op_group` — la unidad de selección es el CAMBIO, no la fila.** Un
índice/UNIQUE/CHECK/FK *redefinido* (mismo nombre, definición distinta) se renderiza
como **dos filas**: un `DROP` del viejo y un `CREATE`/`ADD` del nuevo. Ambas comparten
`op_group`. Si la UI permite tildar checkboxes por fila suelta, hay que agruparlas: **si
el usuario marca una fila de un `op_group`, se marcan todas las de ese grupo** (o se
deshabilita el checkbox individual y se muestra un solo control por grupo). Enviar solo
la mitad de un grupo en `selected_item_ids` se **rechaza con 422**.

**`depends_on` — lista de `op_group` que deben ir ANTES.** Solo lista dependencias que
hay que *crear en esta misma comparación*: algo que ya existe en el destino no aparece.
Úsalo para deshabilitar/advertir si el usuario desmarca un ítem del que otros dependen,
o para un tooltip explicativo. Alternativa más simple: usar `resolve-selection` (§10.6),
que resuelve esto por vos.

### 10.4 Selector de BDs por motor (reusado de otros módulos)

Para armar el selector de las dos BDs a comparar, usá `GET /managed-databases` (filtrado
por `?server_id=` si querés acotar a un servidor, y comparando el campo `engine` del
`ServerOut` correspondiente) para elegir BDs **ya adoptadas**. Para una BD **cruda** de
cualquier servidor (sin adoptar), usá `GET /servers/{server_id}/databases` (§6) para
listar los nombres reales y pasá `{lado}_server_id` + `{lado}_database_name` al crear la
comparación en vez de `{lado}_database_id`.

### 10.5 Endpoints

| Método | Ruta | Rate limit | Descripción |
|---|---|---|---|
| `POST` | `/api/v1/schema-comparisons` | 10/min | Crea la comparación: snapshotea ambas BDs, corre el diff, renderiza el DDL para el motor del target, persiste y devuelve el resumen. |
| `GET` | `/api/v1/schema-comparisons/{id}` | — | Resumen de una comparación ya calculada (sin recomputar). |
| `GET` | `/api/v1/schema-comparisons/{id}/items` | — | Detalle paginado de sentencias (`?page=&size=&object_type=&change_type=`). |
| `GET` | `/api/v1/schema-comparisons/{id}/export` | — | Descarga el diff como archivo `.sql` (ver §10.9). |
| `POST` | `/api/v1/schema-comparisons/{id}/resolve-selection` | — | Cierra una selección parcial (agrega lo que falta) SIN adoptar ni ejecutar. |
| `POST` | `/api/v1/schema-comparisons/{id}/adopt` | 3/min | **Opción A**: crea una nueva versión del blueprint del target con el DDL seleccionado. |
| `POST` | `/api/v1/schema-comparisons/{id}/execute-preview` | — | Resuelve un modo/selección de Opción B **sin ejecutar**: sentencias exactas + `confirm_token`. |
| `POST` | `/api/v1/schema-comparisons/{id}/execute` | 3/min | **Opción B**: ejecuta el DDL directamente sobre el target (sin blueprint). |

Cada BD, en cualquiera de los dos lados, se referencia de una de estas dos formas
(nunca ambas, nunca ninguna): `{lado}_database_id` (BD en el inventario) **o**
`{lado}_server_id` + `{lado}_database_name` (BD cruda de cualquier servidor dado de
alta). Si una referencia cruda coincide con una `ManagedDatabase` ya existente, el
backend la **auto-resuelve** transparentemente (mismo comportamiento que si se hubiera
pasado el id directamente).

#### `POST /api/v1/schema-comparisons` — crear

```bash
curl -X POST "https://<host>/api/v1/schema-comparisons" -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "source_database_id": 7, "target_database_id": 12 }'
```
```json
{
  "data": {
    "id": 42,
    "source_server_id": 3, "source_database_name": "productos_ref",
    "target_server_id": 3, "target_database_name": "productos_db",
    "source_database_id": 7, "target_database_id": 12,
    "source_engine": "mysql", "target_engine": "mysql",
    "cross_flavor_warning": false, "scope_note": null,
    "item_count": 18,
    "counts": { "table": { "new": 1, "modified": 1 }, "column": { "new": 2, "modified": 1, "dropped": 1 },
                "index": { "new": 1 }, "routine": { "new": 1 } },
    "has_destructive": true, "expired": false,
    "created_at": "2026-07-13T10:00:00", "expires_at": "2026-07-14T10:00:00"
  },
  "message": "Comparación creada."
}
```

Con un lado **crudo** (sin adoptar): `{ "source_database_id": 7, "target_server_id": 5, "target_database_name": "legacy_db_09" }`
→ si `legacy_db_09` no está en el inventario, la respuesta trae `target_database_id: null`
(la Opción A queda deshabilitada para esta comparación: ocultá ese botón en la UI en vez
de esperar el 422).

**Errores**: `401` · `404` BD inexistente (por id) o BD cruda inexistente en el motor
(por `server_id`+nombre) · `422` un lado manda ambas representaciones o ninguna, source
y target son la misma BD física, motores incompatibles, o el diff excede el máximo de
sentencias/bytes configurado · `429` (10/min) · `502`/`504` motor inalcanzable.

#### `GET /api/v1/schema-comparisons/{id}` — resumen

Mismo shape que la creación, sin recomputar. **Errores**: `404` · `410` expiró (recalculá) · `401`.

#### `GET /api/v1/schema-comparisons/{id}/items` — detalle paginado

```bash
curl "https://<host>/api/v1/schema-comparisons/42/items?page=1&size=20" -b cookies.txt
```
```json
{
  "data": [
    { "id": 501, "comparison_id": 42, "seq": 3, "object_type": "column",
      "object_name": "productos.descripcion", "change_type": "new", "phase": 3,
      "sql": "ALTER TABLE `productos` ADD COLUMN `descripcion` TEXT NULL",
      "risk_flags": { "destructive": false, "requires_individual_review": false },
      "down_sql": "ALTER TABLE `productos` DROP COLUMN `descripcion`", "down_confirmed": true,
      "op_group": "column|productos.descripcion|new", "depends_on": [],
      "execution_status": null, "execution_error": null, "executed_at": null },
    { "id": 507, "comparison_id": 42, "seq": 9, "object_type": "column",
      "object_name": "clientes.rfc", "change_type": "dropped", "phase": 7,
      "sql": "ALTER TABLE `clientes` DROP COLUMN `rfc`",
      "risk_flags": { "destructive": true, "needs_review": true, "possible_rename_of": "clientes.rfc_nuevo" },
      "down_sql": null, "down_confirmed": false,
      "op_group": "column|clientes.rfc|dropped", "depends_on": [],
      "execution_status": null, "execution_error": null, "executed_at": null }
  ],
  "pagination": { "page": 1, "size": 20, "total": 18, "pages": 1 }
}
```

> **Lecturas para la UI**: el ítem `501` es aditivo seguro; el `507` es un `DROP`
> marcado `possible_rename_of` → **advertencia fuerte**, probablemente sea un rename y
> no una eliminación real. Un ítem con `risk_flags.requires_individual_review: true`
> (rutinas/triggers/eventos) **nunca** entra en modos automáticos y su cuerpo debe
> mostrarse completo antes de confirmar.

**Errores**: `404` · `410` expiró · `401`.

### 10.6 Cerrar una selección: `POST .../resolve-selection`

Endpoint nuevo, **solo lectura** (no adopta ni ejecuta nada):

```bash
curl -X POST "https://<host>/api/v1/schema-comparisons/42/resolve-selection" -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "selected_item_ids": [507, 512] }'
```
```json
{
  "data": {
    "comparison_id": 42,
    "requested_item_ids": [507, 512],
    "resolved_item_ids": [501, 507, 512],
    "added_item_ids": [501],
    "added_reasons": { "view|v_reporte|new": ["table|clientes|new"] },
    "added": [ { "item_id": 501, "object_type": "table", "object_name": "clientes",
                 "change_type": "new", "sql": "CREATE TABLE `clientes` (...)" } ],
    "total": 3
  }
}
```

`resolved_item_ids` viene **en orden de ejecución** (no en el orden enviado). **Flujo
recomendado**: el usuario tilda ítems libremente (checkboxes por `op_group`) → al pasar
a confirmación, llamar a `resolve-selection` → si `added_item_ids` no está vacío,
mostrar un aviso ("se agregaron N sentencia(s) porque las que elegiste dependen de
ellas") con el detalle de `added` → usar `resolved_item_ids` como el `selected_item_ids`
real que se manda a `adopt`/`execute`. Reemplaza el ciclo "intento → 422 → agrego a mano
→ reintento".

⚠️ **No valida si la comparación expiró** (a diferencia de `adopt`/`execute`, que sí lo
hacen) — no lo uses como sustituto de refrescar la comparación si pasó mucho tiempo.

### 10.7 Opción A — Adoptar como versión del blueprint

**Cuándo**: el target está en el inventario **y** tiene `model_id` (es decir,
`SchemaComparisonSummaryOut.target_database_id` no es `null` — la UI debe ocultar esta
opción si vino `null`, sin necesidad de llamar al endpoint para saberlo).

```
POST /api/v1/schema-comparisons/{id}/adopt
Body: { "selected_item_ids": number[], "name": string, "description"?: string,
        "execute_immediately"?: boolean (default false),
        "auto_resolve_dependencies"?: boolean (default false) }
```

- **`auto_resolve_dependencies: false` (default, fail-closed):** si la selección no
  cierra, **422** con `public_context.missing_dependencies` y
  `public_context.suggested_item_ids` (el resultado que hubiera dado
  `resolve-selection`). Si tu pantalla ya llama a `resolve-selection` antes de
  confirmar, este 422 no debería aparecer en producción.
- **`true`:** el backend cierra la selección por su cuenta y la incluye; la respuesta
  trae `added_item_ids` (sin el detalle de `added_reasons` — para transparencia total,
  preferí el flujo de §10.6).

```json
{
  "data": {
    "comparison_id": 42, "model_id": 9, "version": "0007",
    "statements": 3, "executed": false,
    "migration": { "id": 88, "version": "0007", "is_baseline": true, "reviewed": false,
                   "has_non_portable": true, "up_sql": "...", "down_sql_suggested": "...", "...": "..." },
    "apply_result": null,
    "added_item_ids": [],
    "plan_warnings": []
  },
  "message": "Versión adoptada al blueprint (pendiente de revisión)."
}
```

`plan_warnings` (`[{code, message, op_group}]`) son avisos **no bloqueantes** del linter
de plan — no cambian lo que se ejecuta, pero vale mostrarlos:

| `code` | Significado |
|---|---|
| `create_and_drop_same_object` | El plan crea y borra el mismo objeto — casi siempre un rename no auto-detectado. |
| `destructive_without_rollback` | Hay cambios destructivos sin `down_sql`: la versión no podrá revertirse automáticamente. |

Con `execute_immediately: true`, la versión nace `reviewed: true` y `apply_result` trae
el resultado del `apply` normal (mismo shape que [§9](#9-bases-de-datos-gestionadas-managed-databases));
si es `false`, nace `reviewed: false` (gate R1, [§8](#8-blueprints-de-bd-database-models))
y hay que aprobarla + aplicarla luego.

**Errores**: `401` · `410` expiró · `422` el target no está en el inventario (usar
Opción B), el target está en el inventario pero sin blueprint (usar Opción B, mensaje
distinto, mismo código), o la selección no cierra sus dependencias · `409` anti-TOCTOU
(el esquema del target cambió desde que se calculó — recalculá) · `429` (3/min).

> ⚠️ **Limitación conocida**: adoptar por Opción A una **rutina/trigger de
> MySQL/MariaDB con cuerpo `BEGIN...END`** puede fallar al aplicarse si el `up_sql` se
> editó a mano rompiendo el cuerpo. Para objetos procedurales, la Opción B
> (`mode=custom`) no tiene este riesgo porque no re-parte el SQL ya renderizado.

### 10.8 Opción B — Ejecutar directamente sobre el target

**Cuándo**: el target **no** tiene `model_id`. Bloqueada (`409`) si sí lo tiene → usar
Opción A.

**Paso 1 — preview obligatorio**, `POST .../execute-preview` (sin rate limit, solo
lectura): resuelve el modo/selección y devuelve las sentencias exactas + el
`confirm_token` a reenviar. **El cliente NO puede calcular este token por su cuenta**
(requeriría replicar el filtro de riesgo sobre todos los ítems y el formato exacto de
serialización del servidor) — llamalo siempre antes de habilitar el botón de confirmar,
cada vez que cambie el modo o la selección.

```bash
curl -X POST "https://<host>/api/v1/schema-comparisons/42/execute-preview" -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "mode": "all_except_destructive", "selected_item_ids": null }'
```
```json
{
  "data": {
    "comparison_id": 42, "target_database_id": 12, "mode": "all_except_destructive",
    "statements": [ { "item_id": 501, "object_type": "column", "object_name": "productos.descripcion",
                       "sql": "ALTER TABLE `productos` ADD COLUMN `descripcion` TEXT NULL",
                       "risk_flags": { "destructive": false } } ],
    "excluded_by_dependency": [],
    "plan_warnings": [],
    "confirm_token": "3f9a1c...<sha256, reenviar tal cual en execute>"
  }
}
```

`excluded_by_dependency` (lista de `op_group`) — en los modos automáticos (`all`,
`all_except_destructive`) el filtro por riesgo puede dejar fuera una dependencia sin
sacar a sus dependientes (p. ej. una tabla marcada `possible_rename_of` queda excluida
por destructiva, pero sus índices no) — el backend **poda transitivamente** esos
huérfanos en vez de fallar, y los reporta acá. **Mostralo en el preview**, antes de
confirmar: cambia lo que realmente va a ejecutarse.

**Paso 2 — ejecutar**, `POST .../execute` (rate limit 3/min):

```
Body: { "mode": "all"|"all_except_destructive"|"custom",
        "selected_item_ids"?: number[] (requerido si mode=custom),
        "confirm_target_name": string, "confirm_token": string }
Query: ?force=true (override de cuarentena)
```

```bash
curl -X POST "https://<host>/api/v1/schema-comparisons/42/execute" -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{ "mode": "all_except_destructive", "selected_item_ids": null,
        "confirm_target_name": "productos_db", "confirm_token": "3f9a1c...<sha256>" }'
```
```json
{
  "data": {
    "comparison_id": 42, "target_database_id": 12, "mode": "all_except_destructive",
    "total": 8, "applied_count": 8, "failed": false,
    "statements": [ { "item_id": 501, "object_type": "column", "object_name": "productos.descripcion",
                       "status": "applied", "error": null, "execution_ms": 12 } ],
    "excluded_by_dependency": [], "plan_warnings": []
  },
  "message": "Ejecutadas 8 sentencia(s); 0 fallidas."
}
```

Con `mode=custom`: la selección se valida contra el cierre de dependencias igual que en
`adopt` (sin flag `auto_resolve_dependencies` en este endpoint — llamá a
`resolve-selection` primero si necesitás cerrarla).

**Errores**: `401` · `410` expiró · `409` el target tiene blueprint (usar Opción A),
cuarentena sin `force`, o anti-TOCTOU · `422` `confirm_target_name`/`confirm_token` no
coinciden, `mode=custom` sin `selected_item_ids`, o selección que no cierra · `429`
(3/min).

### 10.9 Export a `.sql`

```
GET /api/v1/schema-comparisons/{id}/export
```

**No usa el envelope `ApiResponse`** — es una descarga de archivo
(`Content-Disposition: attachment`, `application/sql`, nombre
`schema-diff-{id}-{target}.sql`).

| Query | Descripción |
|---|---|
| `item_ids` | Repetible (`?item_ids=1&item_ids=2`). Por defecto exporta **todas** las entidades. |
| `object_type`, `change_type` | Mismos filtros que `/items`, combinables con `item_ids`. |
| `include_rollback` | `true` anexa al final el `down_sql` sugerido (orden inverso), **comentado** (nunca ejecutable por accidente). |

Los objetos con cuerpo (rutinas/triggers/eventos) en MySQL/MariaDB se envuelven con
`DELIMITER $$ ... $$ DELIMITER ;`. Funciona igual para BDs adoptadas o crudas (solo lee
ítems ya calculados, no toca el motor ni valida fingerprint). El archivo incluye un
bloque de comentarios con los metadatos (source/target/motores) y marca `[EXPIRADA]` si
la comparación ya venció.

### 10.10 Matriz de errores (resumen de la sección)

| Situación | Código | CTA sugerido |
|---|---|---|
| source == target | `422` | "Elegí dos bases de datos distintas." |
| Motores incompatibles | `422` | Mostrar `detail.msg` tal cual |
| Comparación expiró (`GET`/`items`/`adopt`/`execute`) | `410` | Banner + botón **"Recalcular"** |
| `adopt` con target sin inventario o sin blueprint | `422` | `detail.msg` + CTA "Ejecutar directo (Opción B)" |
| `execute` con target con blueprint | `409` | `detail.msg` + CTA "Adoptar como versión (Opción A)" |
| `adopt`/`execute` — esquema del target cambió (anti-TOCTOU) | `409` | Banner + CTA **"Recalcular"** |
| `execute` — cuarentena sin `force` | `409` | Banner cuarentena + CTA "Reintentar con force" |
| `execute` — `confirm_target_name` no coincide | `422` | Error inline en el campo |
| `execute` — `confirm_token` no coincide | `422` | "El conjunto a ejecutar cambió; recalculá el preview." |
| Selección no cierra dependencias | `422` | Leer `public_context.missing_dependencies`/`suggested_item_ids` → CTA "Resolver automáticamente" |
| Rate limit | `429` | "Demasiadas solicitudes; esperá un momento." |

---

## 11. Catálogo de privilegios (`/privileges`)

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

## 12. Perfiles de permisos (`/permission-profiles`)

Plantillas de privilegios **por motor**, reutilizables para aplicar a usuarios con
[`apply-profile`](#7-usuarios-del-motor-server-users-y-serversidusers). CRUD puro de
inventario; **no toca ningún motor**. Requiere sesión.

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

## 13. Administración: cifrado (`/admin/crypto`)

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

## 14. Health checks

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

## 15. Estado del proyecto

| Iteración | Estado | Alcance |
|---|---|---|
| **1** | ✅ Completada | Infra FastAPI, modelo `Server` + CRUD, cifrado Fernet, conexión remota, introspección, anti-SSRF, anti-inyección, rate limiting, auditoría base. |
| **2** | ✅ Completada | `ServerUser`, `DatabaseModel`, `ManagedDatabase`, `Privilege`; aprovisionamiento `CREATE/DROP` USER y DATABASE, `GRANT/REVOKE`, reasignación de owner, doble confirmación, catálogo de privilegios. |
| **2+** | ✅ Completada | Gestión granular de permisos (grants por nivel, introspección de permisos efectivos, perfiles de permisos reutilizables, creación unificada usuario+grants) y rotación de cifrado (DEK). |
| **3 (Plan 02)** | ✅ Completada | **Migraciones de blueprints**: deltas SQL versionados por blueprint, aplicación/rollback/stamp/historial por BD, dry-run, apply-all, auto-traducción cross-engine, cuarentena y checkpoint/resume por sentencia. Verificado e2e en MySQL 8 / MariaDB 11 / PostgreSQL 16. |
| **Plan 09** | ✅ Completada | **Adopción, reconciliación y snapshot**: `reconcile` (plano en vivo vs. inventario), adoptar BDs/usuarios preexistentes (con stamp-on-adopt), snapshot estructural, blueprint baseline desde snapshot (gate R1 de revisión). |
| **Gestión de usuarios del motor** | ✅ Completada | Vista agrupada por username (`adopted`/`unmanaged`/`orphan`, `supports_hosts`), CRUD por identidad física (adoptados y no), agregar host (MySQL/MariaDB), revelar/definir contraseña, endpoints batch por username completo. |
| **Comparación de esquemas** | ✅ Completada | Diff estructural entre dos BDs (mismo motor o MySQL↔MariaDB), adopción como versión de blueprint u ejecución directa, orden topológico de ejecución, cierre de dependencias de una selección parcial, linter de invariantes del plan. |
| **Reconciliación de aplicaciones parciales** | ✅ Completada | DDL transaccional en PostgreSQL (elimina el estado parcial de raíz), auto-reconciliación configurable al fallar un `apply` en MySQL/MariaDB, endpoint dedicado de reconciliación manual. |
| **Clonado de bases de datos** | ✅ Completada (backend) | Clonado de estructura y datos entre servidores/motores. **Sin guía de API para frontend todavía** — ver `docs/features/database-clone.md`. |
| **Siguiente** | ⏳ Pendiente | Aprovisionamiento de servidores (Terraform/SSH), observabilidad/SSO, CI/CD, *production readiness*. Ver `docs/plans/`. |

> Los endpoints `/api/v1/test/*` que pueda exponer la app son **ejemplos de demostración
> del template** y no forman parte de la API funcional del gateway; no están documentados
> aquí.

---

## 16. Flujos de integración (orden de llamadas)

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
   (GET /api/v1/servers/{id}/users/grouped 🔌 lista los usuarios del motor, agrupados)
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

# 3) (opcional) Revertir, reconciliar un fallo parcial, o aplicar a TODAS las BDs
POST  /api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version=0002 🔌
POST  /api/v1/managed-databases/{db_id}/migrations/reconcile-partial?confirm_version=0008&dry_run=true 🔌
POST  /api/v1/database-models/{model_id}/migrations/apply-all 🔌     → fan-out a N BDs
```

Dependencias y reglas:
- La BD debe tener `model_id` asignado (al crearla en el paso D, o vía `PATCH`); si no, los
  endpoints de migración responden `422`.
- El orden de versiones es **numérico**: `apply` recorre las pendientes en orden ascendente
  y se detiene en la primera que falle.
- `rollback` es destructivo: exige `?confirm_version=` = versión actual (`422` si no) y que
  esa versión tenga `down_sql` confirmado (`409` si no) — y que la BD **no** tenga una
  aplicación parcial pendiente (`409`, ver [§9](#9-bases-de-datos-gestionadas-managed-databases)).
- **Recuperación de fallo (MySQL/MariaDB):** con el default `on_failure=auto`, un `apply`
  que falla a mitad se auto-resuelve en la misma llamada (campo `reconciliation`). Si
  usaste `on_failure=leave` o quedó algo sin reconciliar del todo, usá
  `reconcile-partial` (nunca `stamp --force` + `rollback` — ver la advertencia en §9).
  En **PostgreSQL** esto casi no ocurre: el motor deshace la migración fallida solo.
- `stamp` marca una BD pre-existente en una versión **sin ejecutar SQL** (cuando el esquema
  ya existe pero el gateway aún no lo registra) — o para descartar (`force=true`) un
  checkpoint parcial ya reconciliado a mano.

Escenario real (frontend): un wizard de "publicar nueva versión de esquema" llamaría
`POST …/migrations` (subir el delta) → mostrar `translated`/`down_sql_suggested` para
revisión → `PATCH` (confirmar rollback) → por cada BD afectada, `…/migrations/apply?dry_run=true`
(preview) → `…/migrations/apply` (confirmar) → `…/migrations/history` (resultado). Para
desplegar a toda una familia de BDs, `…/migrations/apply-all`.

### J. Comparar dos BDs y sincronizarlas (schema-comparisons)

Depende de: dos BDs alcanzables (mismo motor, o MySQL↔MariaDB) — adoptadas o crudas.

```
1. POST /api/v1/schema-comparisons 🔌
        body: { source_database_id | source_server_id+source_database_name,
                target_database_id | target_server_id+target_database_name }
2. GET  /api/v1/schema-comparisons/{id}                → resumen (¿hay destructivos?)
3. GET  /api/v1/schema-comparisons/{id}/items           → DDL exacto por ítem (ordenar por seq)
4. POST /api/v1/schema-comparisons/{id}/resolve-selection
        body: { selected_item_ids }                     → cierra la selección del usuario

# Según si el target tiene blueprint:
5a. POST /api/v1/schema-comparisons/{id}/adopt 🔌       → Opción A (crea versión en el blueprint)
5b. POST /api/v1/schema-comparisons/{id}/execute-preview → Opción B, paso 1 (obtiene confirm_token)
    POST /api/v1/schema-comparisons/{id}/execute 🔌     → Opción B, paso 2 (ejecuta)
```

Reglas: la dirección `source`/`target` siempre es explícita, nunca inferida. Ordená y
ejecutá siempre por `seq`, nunca por `phase`. Agrupá la selección por `op_group`. Una
comparación tiene TTL — `410` si expiró, hay que recalcularla (volver al paso 1).

### K. Reconciliar una aplicación parcial (recuperación fina, MySQL/MariaDB)

Depende de: una BD gestionada con blueprint cuyo `GET .../migrations/status` informa
`has_partial_application: true`.

```
1. GET  /api/v1/managed-databases/{db_id}/migrations/status
        → lee partial_application[] (la de versión más alta primero)
2. POST /api/v1/managed-databases/{db_id}/migrations/reconcile-partial
        ?confirm_version=<version>&dry_run=true            → preview de los reversos exactos
3. POST /api/v1/managed-databases/{db_id}/migrations/reconcile-partial 🔌
        ?confirm_version=<version>                          → ejecuta (agregar &force=true si
                                                                hay sentencias sin reverso)
4. GET  /api/v1/managed-databases/{db_id}/migrations/status → confirmar has_partial_application=false
```

Si hay **más de una** entrada en `partial_application[]`, repetir desde el paso 2 con la
siguiente versión (de mayor a menor) — cada llamada solo resuelve la de versión más
alta pendiente.

---

## 17. Apéndice: tabla resumen de endpoints

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
| 59 | GET | `/api/v1/servers/{server_id}/reconcile` | 🔒 | 🔌 |
| 60 | GET | `/api/v1/servers/{server_id}/databases/{database}/snapshot` | 🔒 | 🔌 |
| 61 | POST | `/api/v1/server-users/adopt` | 🔒 | 🔌 |
| 62 | POST | `/api/v1/managed-databases/adopt` | 🔒 | 🔌 |
| 63 | POST | `/api/v1/database-models/from-snapshot` | 🔒 | 🔌 |
| 64 | GET | `/api/v1/servers/{server_id}/users/grouped` | 🔒 | 🔌 |
| 65 | POST | `/api/v1/servers/{server_id}/users` | 🔒 | 🔌 |
| 66 | PATCH | `/api/v1/servers/{server_id}/users/password` | 🔒 | 🔌 |
| 67 | DELETE | `/api/v1/servers/{server_id}/users` | 🔒 | 🔌 |
| 68 | POST | `/api/v1/servers/{server_id}/users/add-host` | 🔒 | 🔌 |
| 69 | POST | `/api/v1/servers/{server_id}/users/reveal-password` | 🔒 | 🔌 |
| 70 | POST | `/api/v1/servers/{server_id}/users/adopt-all-hosts` | 🔒 | 🔌 |
| 71 | POST | `/api/v1/servers/{server_id}/users/define-password` | 🔒 | — (nunca toca el motor) |
| 72 | PATCH | `/api/v1/servers/{server_id}/users/password-all-hosts` | 🔒 | 🔌 |
| 73 | POST | `/api/v1/schema-comparisons` | 🔒 | 🔌 (solo lectura de ambos motores) |
| 74 | GET | `/api/v1/schema-comparisons/{id}` | 🔒 | — |
| 75 | GET | `/api/v1/schema-comparisons/{id}/items` | 🔒 | — |
| 76 | GET | `/api/v1/schema-comparisons/{id}/export` | 🔒 | — |
| 77 | POST | `/api/v1/schema-comparisons/{id}/resolve-selection` | 🔒 | — |
| 78 | POST | `/api/v1/schema-comparisons/{id}/adopt` | 🔒 | 🔌* |
| 79 | POST | `/api/v1/schema-comparisons/{id}/execute-preview` | 🔒 | — |
| 80 | POST | `/api/v1/schema-comparisons/{id}/execute` | 🔒 | 🔌 |
| 81 | POST | `/api/v1/managed-databases/{db_id}/migrations/reconcile-partial` | 🔒 | 🔌 |

\* Toca el motor solo cuando el flag (`provision` / `drop_remote` / `execute_immediately`)
es `true`. Los grants y `provision`/`apply-profile` tocan el motor siempre. Los
endpoints **48–58** son el módulo de migraciones de blueprints (Plan 02); los `48–52`
son CRUD de inventario, el resto tocan el motor. Los endpoints **59–63** son el módulo
de adopción/reconciliación/snapshot (Plan 09); todos leen el motor (solo lectura), salvo
que crean metadata en el gateway. Los endpoints **64–72** son la gestión agrupada de
usuarios del motor por identidad física ([§7](#7-usuarios-del-motor-server-users-y-serversidusers)) —
`71` es el único que nunca toca el motor (cifra y guarda, nada más). Los endpoints
**73–80** son el módulo de comparación de esquemas ([§10](#10-comparación-de-esquemas-entre-bds-schema-comparisons)):
`73` snapshotea ambos motores (solo lectura); `74/75/76/77/79` son puro inventario; `78`
toca el motor solo si `execute_immediately=true`; `80` siempre toca el motor. El `81` es
la reconciliación manual de una aplicación parcial ([§9](#9-bases-de-datos-gestionadas-managed-databases)).

---

## 18. Apéndice: variables de entorno del integrador

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

*Generado a partir del código fuente del backend (rutas, schemas y DTOs), consolidando
`api-reference-v2.md` a `api-reference-v5.md` y las guías de feature para frontend. Para
el detalle narrativo con escenarios, mockups y diagramas, esos documentos siguen
disponibles; para detalles de cada feature a nivel backend, consulta `docs/features/`;
para el roadmap, `docs/plans/`.*
