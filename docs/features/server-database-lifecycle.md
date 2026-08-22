# Ciclo de vida de bases de datos a nivel servidor (crear / borrar / usuarios)

Este módulo permite **crear** y **borrar** bases de datos directamente en un servidor y
**listar qué usuarios/roles tienen permisos** sobre una BD, todo **por identidad física**
`(server_id, database)` — funcione o no adoptada la BD en el inventario del gateway. Es el
análogo, para bases de datos, del [CRUD de usuarios del motor por identidad](engine-users-management.md).

Complementa —no reemplaza— a [`/managed-databases`](database-management.md): aquel opera
sobre el inventario (y opcionalmente el motor con `?provision`/`?drop_remote`); este opera
sobre el **motor** directamente y toca el inventario solo como efecto secundario opcional.

Todos los endpoints exigen [sesión de administrador](authentication.md), auditan la
intención antes de tocar el motor y son compatibles con **MySQL/MariaDB y PostgreSQL**
(las diferencias de motor viven en los adapters).

## Endpoints (API v1, prefijo `/servers`)

| Método | Ruta | Rate limit | Descripción |
|---|---|---|---|
| POST | `/servers/{id}/databases` | 10/min | Crea la BD en el motor. Registro en inventario **opcional**. |
| POST | `/servers/{id}/databases/{db}/drop-preview` | 10/min | Paso 1 del borrado: valida, corre guards y emite el `confirm_token`. |
| DELETE | `/servers/{id}/databases/{db}` | 3/min | Paso 2 (irreversible): exige nombre exacto + `confirm_token`. |
| GET | `/servers/{id}/databases/{db}/users` | 30/min | Usuarios/roles con permisos sobre la BD, cruzados con inventario. |

## 1. Crear una base de datos

```http
POST /api/v1/servers/{server_id}/databases
Content-Type: application/json

{
  "name": "ventas",
  "charset": "utf8mb4",          // MySQL/MariaDB: CHARACTER SET · PostgreSQL: ENCODING
  "collation": "utf8mb4_general_ci", // MySQL: COLLATE · PostgreSQL: LOCALE (LC_COLLATE/LC_CTYPE)
  "owner": "app_pg",             // PostgreSQL OWNER (opcional); ignorado en MySQL/MariaDB
  "register": false              // ver abajo
}
```

- **`register: false` (default)** — "crea y listo": ejecuta `CREATE DATABASE` en el motor
  y **no toca el inventario**.
- **`register: true`** — además registra la BD como `ManagedDatabase(origin='provisioned')`.
  Requiere **`owner_id`** (un `ServerUser` del mismo servidor); reutiliza el flujo consistente
  de `/managed-databases` (`pending → active/error`). Sin `owner_id` → **422**.

El nombre a crear pasa por la whitelist **estricta** de identificadores y por el guard de
BDs de sistema (ver abajo).

> **Caveat PostgreSQL:** `collation` es el LOCALE de la BD (p. ej. `en_US.UTF-8`) y **debe
> existir en el SO** del servidor PostgreSQL, o el `CREATE DATABASE` falla con
> `invalid locale name`. Siempre se emite `TEMPLATE template0`.

### Catálogo de charsets/collations (qué se puede elegir)

`charset`/`collation` **no son texto libre**: se validan contra el catálogo **global**
`charset_collation_options` (`GET/POST/PATCH /api/v1/charset-collation-options`) **antes de
tocar el motor**. Una combinación que no esté `enabled` responde **422** y el `CREATE DATABASE`
nunca se emite; el `public_context` del error lista las combinaciones habilitadas (viaja también
en producción, para que el operador sepa qué sí puede elegir).

| Concepto | Detalle |
|---|---|
| Alcance | **Global**, no por servidor. `engine_family` agrupa **MySQL + MariaDB** como `mysql`; PostgreSQL aparte |
| Semántica | ambos ausentes → **no se valida** (el adapter usa su default). Solo `charset` → basta que alguna combinación habilitada lo use. Solo `collation` → debe estar habilitada. Ambos → el **par exacto** |
| Qué llega al DDL | los valores **canónicos del catálogo**, no el texto del request (el match de charset es case-insensitive; en PostgreSQL el locale se compara tal cual porque es del SO) |
| Caminos cubiertos | `POST /servers/{id}/databases` (con y sin `register`) y `POST /managed-databases`. **No** aplica a `adopt` ni al clonado: ahí el charset se **lee** del motor (se replica la realidad, no se elige) |
| Administración | `PATCH {"enabled": …, "is_default": …}`; a lo sumo un `is_default` por familia y un default debe estar habilitado. No hay `DELETE`: se deshabilita |
| Seed | MySQL: `utf8mb4_unicode_ci` (default) y `utf8mb4_general_ci` habilitadas; `utf8mb4_0900_ai_ci` (solo MySQL 8), `utf8mb3`, `latin1` **deshabilitadas** de referencia. PostgreSQL: `UTF8`/`en_US.UTF-8` (default) habilitada; `C` y `C.UTF-8` deshabilitadas |

> **PostgreSQL:** el catálogo es un **menú curado**, no una garantía: el locale depende del SO
> de **cada** servidor. Si no existe ahí, el motor responde `invalid locale name` y ese error
> nativo se propaga traducido — el catálogo no lo reemplaza.

## 2. Borrar una base de datos (confirmación de doble factor de backend)

El frontend puede sumar su propia triple confirmación visual (botón → SweetAlert → modal),
pero el **backend** exige dos factores independientes, en dos pasos:

### Paso 1 — preview

```http
POST /api/v1/servers/{server_id}/databases/{database}/drop-preview
```

```jsonc
{
  "database": "ventas",
  "engine": "postgresql",
  "active_connections": 3,          // conexiones abiertas contra la BD
  "is_managed": true,               // está en el inventario
  "managed_database_id": 42,
  "confirm_token": "1799999999.a3f9c1…",  // firmado, TTL 2 min
  "expires_at": "2026-07-29T21:32:00Z",
  "warnings": [
    "La base de datos tiene 3 conexión(es) activa(s); usa force_disconnect=true…",
    "La base de datos está registrada en el inventario; al eliminarla también…"
  ]
}
```

### Paso 2 — delete

```http
DELETE /api/v1/servers/{server_id}/databases/{database}
Content-Type: application/json

{
  "confirm_target_name": "ventas",       // debe coincidir EXACTO con el nombre
  "confirm_token": "1799999999.a3f9c1…", // el del preview, vigente
  "force_disconnect": false              // ver "Diferencias por motor"
}
```

Validaciones (en orden), todas **antes** de tocar el motor:

1. **Guard de BD de sistema** → 409 (ver abajo).
2. **`confirm_target_name` == nombre real** → 422 si no coincide. Obliga a identificar
   conscientemente *cuál* BD se borra.
3. **`confirm_token` válido** → 422 si no corresponde a esta `(server_id, database)` o está
   manipulado; **410** si expiró (reobtener el preview). Da frescura/anti-replay.
4. **Auditoría fail-closed** (`audit.record_intent`): si no se persiste, se aborta (500).

Solo entonces se ejecuta `DROP DATABASE`. Si la BD estaba en el inventario, **también se
borra su fila `ManagedDatabase`** (`inventory_removed: true` en la respuesta), para no dejar
referencias colgadas.

> El `confirm_token` es un **HMAC-SHA256** firmado con `SECRET_KEY`, con la expiración
> embebida (`"{epoch}.{hmac}"`). Es **stateless** (no requiere tabla) y está ligado a la
> identidad física, así que el token de una BD no sirve para otra ni para otro servidor.
> Ver `app/services/confirm_token.py`.

### "Solo inventario" vs "inventario + motor"

Este `DELETE` a nivel servidor **siempre** borra en el motor (y limpia el inventario si
aplica). Para borrar **solo** el registro de inventario sin tocar el motor, seguir usando
`DELETE /managed-databases/{id}?drop_remote=false` (ver [database-management](database-management.md)).

## 3. Usuarios con permisos sobre una base de datos

```http
GET /api/v1/servers/{server_id}/databases/{database}/users
```

Consulta **inversa** (por BD, agrupada por grantee) y cruzada con el inventario
(`adopted`/`unmanaged`, respetando `supports_hosts`):

```jsonc
{
  "dialect": "mysql",
  "supports_hosts": true,
  "database": "ventas",
  "grantees": [
    { "username": "app", "host": "%", "is_global": false,
      "privileges": ["SELECT", "INSERT"], "levels": ["database", "table"],
      "status": "adopted", "server_user_id": 12 },
    { "username": "reportes", "host": "%", "is_global": true,
      "privileges": ["SELECT"], "levels": ["global"],
      "status": "unmanaged", "server_user_id": null }
  ]
}
```

- **`levels`** — niveles por los que el grantee tiene relación con la BD
  (`global`/`database`/`table`/`column` en MySQL; `database`/`table` en PostgreSQL).
- **`is_global`** (solo MySQL/MariaDB) — el usuario tiene privilegios globales `*.*` que
  aplican a **todas** las BDs, no solo a esta. Se incluyen a propósito: si no, se
  subreportaría a quién tiene acceso efectivo.

## Guard de bases de datos de sistema

Los `create_database`/`drop_database` de los adapters por sí solos solo validan+quotean el
nombre: **nada** impedía un `DROP DATABASE mysql`. Este módulo agrega el guard explícito
`identifiers.ensure_not_reserved_database` (409, case-insensitive):

| Motor | Nombres reservados |
|---|---|
| MySQL/MariaDB | `information_schema`, `mysql`, `performance_schema`, `sys` |
| PostgreSQL | `postgres`, `template0`, `template1` |

Se aplica tanto al crear como al borrar (preview y delete).

## Diferencias por motor (encapsuladas en los adapters)

| Tema | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Crear BD | `CREATE DATABASE … CHARACTER SET … [COLLATE …]` | `CREATE DATABASE … [OWNER …] ENCODING '…' [LC_COLLATE/LC_CTYPE …] TEMPLATE template0` |
| Conexiones activas | no bloquean el `DROP` | **bloquean** el `DROP` ("database is being accessed…") |
| `force_disconnect=true` | **no-op** (se acepta por paridad) | `pg_terminate_backend(...)` sobre `pg_stat_activity` (excluye el propio backend) antes del `DROP`; funciona en todas las versiones |
| `active_connections` | `information_schema.PROCESSLIST WHERE DB=:db` | `pg_stat_activity WHERE datname=:db AND pid<>pg_backend_pid()` |
| Grantees por BD | `SCHEMA/TABLE/COLUMN_PRIVILEGES WHERE TABLE_SCHEMA=:db` + `USER_PRIVILEGES` globales | `pg_database.datacl` + `aclexplode` + owner (nivel servidor) y `table_privileges` del schema `public` (nivel BD) |
| `host` en la salida | `'user'@'host'` | `null` (un ROLE no tiene host) |

## Seguridad

- **Auth** obligatoria (`AdminDep`) y **rate limiting** en todos (create/preview 10/min,
  delete 3/min, users 30/min).
- Todo identificador pasa por `validate_identifier` + `quote_identifier`; los valores
  (encoding/locale) por `quote_string_literal`; `datname`/`DB` en las lecturas van como
  **bind param** (nunca interpolados).
- El **catálogo de charsets/collations** es una allowlist: acota lo que puede llegar al DDL a
  valores que salieron de la tabla. Importa sobre todo en PostgreSQL, donde el locale viaja
  como **literal de string** (no es whitelisteable como identificador). Dar de alta una
  combinación custom también pasa por una whitelist sintáctica por familia.
- **Auditoría fail-closed** (`server_database.create` / `server_database.drop`) antes de la
  operación; el `create-only` audita `record_intent` + `record`; el `register` delega su
  auditoría a `/managed-databases`.

## Verificación

Verificación puntual con `FakeAdapter` (sin motor real): emisión/validación del
`confirm_token` (incluye TTL, manipulación y cruce de BD/servidor), guards de sistema,
`confirm_target_name` mismatch, create-only vs register, drop con limpieza de inventario y
`force_disconnect`, y grantees cruzados con inventario.

Catálogo de charsets/collations: `tests/test_api_charset_collation_options.py` (lógica pura,
CRUD del catálogo y enforcement en los dos caminos de creación, con adapter mockeado) y ciclo
`upgrade`/`downgrade`/`upgrade` de la migración `c4d5e6f7a8b9` contra **SQLite**. Sigue
pendiente la corrida contra motores reales: que un `utf8mb4_0900_ai_ci` habilitado falle en
MariaDB y que los locales sembrados existan en el SO del PostgreSQL destino.

> ⚠️ **Pendiente antes de producción (gate):** verificación **e2e contra motores reales**
> (MySQL/MariaDB/PostgreSQL; requiere Docker) del ciclo crear → listar usuarios →
> drop-preview → delete con `force_disconnect`, y en particular las consultas de catálogo de
> PostgreSQL (`aclexplode`/`datacl`, `pg_terminate_backend`).
