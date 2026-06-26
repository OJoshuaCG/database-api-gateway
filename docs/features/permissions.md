# Permisos Granulares (Plan 07 — Fase 1 ✅)

El módulo de permisos granulares permite otorgar, revocar y consultar privilegios de objetos de base de datos sobre usuarios del motor registrados en el gateway. Opera directamente sobre el motor destino (MariaDB/MySQL o PostgreSQL) a través del admin de conexión configurado en el servidor, con un catálogo cerrado de privilegios que garantiza que ningún token del usuario se interpola directamente en el DCL.

> **Fase 1 cerrada (2026-06-26):** además del núcleo GRANT/REVOKE/LIST, incluye auditoría DCL granular, auditoría de intención fail-closed, guard anti auto-lockout y `REVOKE ... CASCADE` con confirmación. Ver [Pendiente (roadmap Fase 2-3)](#pendiente-roadmap-fase-2-3).

---

## Endpoints

| Método   | Path                                                        | Auth requerida | Descripción                                                               |
|----------|-------------------------------------------------------------|----------------|---------------------------------------------------------------------------|
| `GET`    | `/api/v1/server-users/{user_id}/grants`                     | Sesión admin   | Lista los permisos actuales del usuario en el motor.                     |
| `POST`   | `/api/v1/server-users/{user_id}/grants`                     | Sesión admin   | Otorga privilegios sobre un objeto (GRANT).                              |
| `DELETE` | `/api/v1/server-users/{user_id}/grants`                     | Sesión admin   | Revoca privilegios sobre un objeto (REVOKE). Body opcional `cascade`; query `confirm_grantee` si `cascade=true`. |
| `POST`   | `/api/v1/server-users/{user_id}/apply-profile/{profile_id}` | Sesión admin   | Aplica un perfil de permisos preconfigurado al usuario (best-effort).    |
| `POST`   | `/api/v1/server-users/provision`                            | Sesión admin   | Crea usuario en el inventario + provisiona en el motor + grants iniciales.|
| `POST`   | `/api/v1/servers/{server_id}/grantable`                     | Sesión admin   | Verifica si el admin de conexión puede otorgar los privilegios dados.    |

---

## Niveles de permiso (`GrantLevel`)

Cada operación de grant/revoke actúa sobre un **nivel** concreto. Los niveles soportados varían por motor:

| Nivel      | Valor          | MariaDB/MySQL | PostgreSQL |
|------------|----------------|:-------------:|:----------:|
| Global     | `global`       | —             | —          |
| Database   | `database`     | ✓             | ✓          |
| Schema     | `schema`       | —             | ✓          |
| Table      | `table`        | ✓             | ✓          |
| Column     | `column`       | ✓             | ✓          |
| Sequence   | `sequence`     | —             | ✓          |
| Routine    | `routine`      | ✓             | ✓          |

> El nivel `global` está definido en el enum pero no soportado para otorgar en Fase 1. Los intentos de usarlo reciben 422 con los niveles válidos del motor.

---

## Flujo GRANT

### Paso a paso

1. El API valida la request con `GrantRequest` (Pydantic).
2. El controller carga el `ServerUser` → `Server` → construye el target de conexión admin.
3. **Pre-chequeo de capability**: el adapter llama `can_grant()` con el nivel, objeto y privilegios solicitados. Si el admin de conexión no tiene `WITH GRANT OPTION` para alguno de los privilegios pedidos → **403** inmediato, sin tocar el motor para el GRANT.
4. Si el pre-chequeo pasa, el adapter ejecuta el `GRANT` contra el motor.
5. Se registra en el log de auditoría.

### Ejemplo

**Request:**

```http
POST /api/v1/server-users/42/grants
Content-Type: application/json

{
  "level": "table",
  "object_ref": {
    "database": "mi_app",
    "table": "pedidos"
  },
  "privileges": ["SELECT", "INSERT"],
  "with_grant_option": false
}
```

**Response 200:**

```json
{
  "success": true,
  "data": {
    "granted": true,
    "level": "table",
    "privileges": ["SELECT", "INSERT"],
    "with_grant_option": false
  }
}
```

**Response 403 (admin sin capability):**

```json
{
  "success": false,
  "message": "La credencial del gateway no tiene permisos suficientes para otorgar estos privilegios. Verifica que la cuenta admin tenga WITH GRANT OPTION para los privilegios solicitados."
}
```

---

## Flujo LIST GRANTS

```http
GET /api/v1/server-users/42/grants
GET /api/v1/server-users/42/grants?database=mi_app
```

Devuelve un array de objetos `GrantInfo`:

```json
{
  "success": true,
  "data": [
    {
      "level": "table",
      "object": "mi_app.pedidos",
      "privileges": ["SELECT", "INSERT"],
      "with_grant_option": false
    }
  ]
}
```

### Comportamiento diferente por motor

| Aspecto                   | MariaDB/MySQL                                                                      | PostgreSQL                                                                    |
|---------------------------|------------------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| Parámetro `?database=`    | **Ignorado.** `information_schema` devuelve privilegios de todas las bases.        | **Requerido.** La consulta se ejecuta en la base especificada; sin él, el resultado puede estar incompleto. |
| Fuente de datos           | UNION de `USER_PRIVILEGES`, `SCHEMA_PRIVILEGES`, `TABLE_PRIVILEGES`, `COLUMN_PRIVILEGES` en `information_schema`. | UNION de `role_table_grants`, `role_column_grants`, `role_routine_grants`, `role_usage_grants`. |
| Privilegios globales      | Incluidos (nivel `global` implícito en `USER_PRIVILEGES`).                         | No aplica (PostgreSQL no tiene privilegios globales en el mismo sentido).     |

---

## Flujo REVOKE

El REVOKE no pre-chequea `can_grant` porque revocar no requiere `WITH GRANT OPTION`: solo exige que el admin haya sido quien otorgó el privilegio (o sea superuser). La red de seguridad es el propio error del motor.

**Guards previos (antes de tocar el motor):**

- **Anti auto-lockout (409):** se rechaza el REVOKE cuyo `grantee` coincida (case-insensitive) con la credencial pseudo-root del gateway (`server.root_username`). Revocarle privilegios a la propia cuenta de conexión dejaría al gateway sin acceso al motor. Para degradar esa cuenta hay que hacerlo fuera del gateway.
- **CASCADE con confirmación (solo PostgreSQL):** `cascade: true` en el body revoca en cascada los privilegios que el `grantee` haya re-delegado. Es una operación GATE: exige repetir el username del grantee en el query param `confirm_grantee` (si no coincide → 422). En MySQL/MariaDB no existe `CASCADE` → 422. Por defecto el motor usa `RESTRICT`.

**Auditoría de intención (fail-closed):** *todo* REVOKE registra primero una fila `status="attempt"` en `audit_log` con los campos DCL granulares (`grantee`, `privilege`, `object_level`, `object_name`, `grantor`). Si esa escritura no se persiste, la operación se **aborta** (no es best-effort). Tras ejecutar se registra el resultado (`success`/`error`).

> **Nota sobre el cliente:** `DELETE` con body JSON requiere pasarlo explícitamente:
> ```python
> requests.request("DELETE", url, json=payload, headers=headers)
> ```
> Los clientes HTTP que ignoran el body en DELETE pueden generar un 422 por falta de campos requeridos.

**Request (con CASCADE en PostgreSQL):**

```http
DELETE /api/v1/server-users/42/grants?confirm_grantee=analista
Content-Type: application/json

{
  "level": "table",
  "object_ref": { "database": "mi_app", "schema": "public", "table": "pedidos" },
  "privileges": ["INSERT"],
  "cascade": true
}
```

**Request:**

```http
DELETE /api/v1/server-users/42/grants
Content-Type: application/json

{
  "level": "table",
  "object_ref": {
    "database": "mi_app",
    "table": "pedidos"
  },
  "privileges": ["INSERT"]
}
```

**Response 200:**

```json
{
  "success": true,
  "data": null,
  "message": "Permisos revocados exitosamente."
}
```

---

## Verificar capability (grantable)

Antes de intentar un GRANT, se puede consultar si el admin de conexión de un servidor tiene capacidad de delegar los privilegios deseados. Esto es lo que el controller usa internamente en su pre-chequeo, pero también está disponible como endpoint explícito.

**Cuándo usarlo:**

- Mostrar al usuario de la UI si una operación de grant es viable antes de ejecutarla.
- Diagnosticar por qué un GRANT falla con 403.
- Verificar la configuración del admin durante el onboarding de un nuevo servidor.

**Request:**

```http
POST /api/v1/servers/7/grantable
Content-Type: application/json

{
  "level": "table",
  "object_ref": {
    "database": "mi_app",
    "table": "pedidos"
  },
  "privileges": ["SELECT", "INSERT", "UPDATE"]
}
```

**Response 200:**

```json
{
  "success": true,
  "data": {
    "can_grant": true,
    "level": "table",
    "privileges": ["SELECT", "INSERT", "UPDATE"]
  }
}
```

### Lógica interna por motor

- **MariaDB/MySQL:** consulta `USER_PRIVILEGES` del `CURRENT_USER` en `information_schema` filtrando `IS_GRANTABLE = 'YES'`. El token `GRANT OPTION` nunca aparece como `PRIVILEGE_TYPE` independiente; se infiere del conjunto no vacío de privilegios grantables. Si el admin tiene `ALL PRIVILEGES` con grant option, `can_grant` retorna `True` para cualquier subconjunto.
- **PostgreSQL:** primero verifica si el rol es superuser (`rolsuper = true` en `pg_roles`). Si es superuser, retorna `True` incondicionalmente. De lo contrario, consulta `has_table_privilege`, `has_column_privilege`, `has_function_privilege`, etc. con la cláusula `WITH GRANT OPTION`.

---

## Provisión unificada

El endpoint `/provision` crea el usuario en el inventario del gateway, lo aprovisiona en el motor destino (`CREATE USER`) y aplica un conjunto de grants iniciales, todo en una sola llamada.

### Semántica

- La creación del usuario (inventario + `CREATE USER`) es **atómica**: si falla, se revierte.
- Los grants iniciales son **best-effort**: cada grant se intenta independientemente. Un fallo en un grant individual **no revierte la creación del usuario** ni cancela los grants restantes.
- El resultado incluye cuántos grants se aplicaron correctamente y el detalle de cada uno.

**Request:**

```http
POST /api/v1/server-users/provision
Content-Type: application/json

{
  "server_id": 7,
  "username": "app_reader",
  "host": "%",
  "password": "s3cur3P@ss",
  "initial_grants": [
    {
      "level": "database",
      "object_ref": { "database": "mi_app" },
      "privileges": ["SELECT"],
      "with_grant_option": false
    },
    {
      "level": "table",
      "object_ref": { "database": "mi_app", "table": "auditoria" },
      "privileges": ["INSERT"],
      "with_grant_option": false
    }
  ]
}
```

**Response 201:**

```json
{
  "success": true,
  "data": {
    "user": {
      "id": 15,
      "server_id": 7,
      "username": "app_reader",
      "host": "%",
      "is_active": true,
      "has_password": true,
      "created_at": "2026-06-21T10:00:00Z",
      "updated_at": "2026-06-21T10:00:00Z"
    },
    "grants_applied": 2,
    "grant_results": [
      {
        "level": "database",
        "object": "mi_app",
        "privileges": ["SELECT"],
        "success": true,
        "error": null
      },
      {
        "level": "table",
        "object": "mi_app.auditoria",
        "privileges": ["INSERT"],
        "success": true,
        "error": null
      }
    ]
  }
}
```

Si un grant falla, `success` será `false` y `error` contendrá el mensaje. El usuario queda creado y provisionado de todas formas.

---

## Perfiles de permisos (apply-profile)

Los perfiles de permisos (documentados en detalle en `server-management.md`) definen conjuntos reutilizables de privilegios por nivel para un motor concreto. El endpoint `apply-profile` materializa ese perfil sobre un usuario concreto, mapeando cada nivel abstracto del perfil a un objeto real (tabla, base de datos, etc.) provisto en el request.

### Semántica

- El engine del perfil debe coincidir con el engine del servidor del usuario. Si difieren → **422** inmediato.
- Cada ítem del perfil se intenta de forma independiente (**best-effort**): errores individuales se capturan en `errors[]` sin abortar la operación.
- Los niveles definidos en el perfil para los que no se provee `object_mappings` se reportan en `skipped_levels[]` y se omiten silenciosamente.
- Cada ítem pasa por el pre-chequeo `can_grant` antes de ejecutar el `GRANT`. Si el admin no puede delegar el privilegio, el item va a `errors[]`, no lanza 403 global.

### Ejemplo

El perfil ID 3 `"lector-app"` (motor `mariadb`) define:
- nivel `database`: `SELECT`
- nivel `table`: `SELECT`, `INSERT`

**Request:**

```http
POST /api/v1/server-users/42/apply-profile/3
Content-Type: application/json

{
  "object_mappings": [
    {
      "level": "database",
      "object_ref": { "database": "mi_app" }
    },
    {
      "level": "table",
      "object_ref": { "database": "mi_app", "table": "pedidos" }
    }
  ]
}
```

**Response 200:**

```json
{
  "success": true,
  "data": {
    "profile_id": 3,
    "profile_name": "lector-app",
    "engine": "mariadb",
    "grants_applied": 2,
    "skipped_levels": [],
    "errors": []
  }
}
```

**Ejemplo con nivel omitido y error:**

```json
{
  "profile_id": 3,
  "profile_name": "lector-app",
  "engine": "mariadb",
  "grants_applied": 1,
  "skipped_levels": ["table"],
  "errors": ["database: credencial sin permisos suficientes para ['SELECT']"]
}
```

---

## Catálogo cerrado de privilegios

**Por qué existe:** la primera versión del validador usaba un regex que aceptaba cualquier palabra en mayúsculas como nombre de privilegio. Un payload como `DROP; DELETE FROM users --` podría superar la validación y llegar a la interpolación en el DCL. El catálogo cerrado elimina esa superficie de ataque.

### Principio central

El input del usuario **nunca** se interpola directamente en una sentencia SQL/DCL. El flujo es:

```
token_del_usuario → normalize → lookup en catálogo → token_canónico → interpolar en DCL
```

Si `lookup` falla (token no está en el catálogo para ese motor y nivel) → 422 con la lista de tokens válidos. El 422 **nunca refleja el token crudo** del usuario en el mensaje de error (evita reflejar payloads potencialmente maliciosos).

### Tres clases de privilegio

| Clase  | Descripción                                                                                     | Comportamiento              |
|--------|-------------------------------------------------------------------------------------------------|-----------------------------|
| `ALLOW`| Privilegios de objeto estándar (SELECT, INSERT, USAGE, EXECUTE…).                               | Se otorgan directamente.    |
| `GATE` | Privilegios ampliados (ALL PRIVILEGES, GRANT OPTION, PG MAINTAIN). Operaciones sensibles.| En GRANT se aceptan explícitamente y se **audita la intención** (fail-closed) antes de ejecutar. En REVOKE, `CASCADE` exige doble confirmación (`confirm_grantee`). |
| `DENY` | Privilegios administrativos (SUPER, FILE, REPLICATION, SUPERUSER, CREATEROLE…).                | Rechazados con 422 siempre, en cualquier nivel. |

### Referencia de código

El catálogo vive en `app/services/db_admin/privileges.py`. La función pública es:

```python
validate_privileges(privileges: list[str], dialect: str, level: GrantLevel) -> tuple[list[str], bool]
# Devuelve (tokens_canónicos, requires_confirmation)
# Lanza AppHttpException(422) si algún token es inválido, DENY, o el nivel no es soportado.
```

---

## Diferencias de comportamiento por motor

### `list_grants`

| Aspecto                        | MariaDB/MySQL                                                  | PostgreSQL                                                        |
|-------------------------------|----------------------------------------------------------------|-------------------------------------------------------------------|
| Conexión                      | Usa el servidor como está (sin cambiar de base).               | Requiere conectarse a la base específica (`?database=`).          |
| Parámetro `database`          | Ignorado — `information_schema` es global.                     | Necesario para recuperar grants de esa base.                      |
| Fuentes consultadas           | `USER_PRIVILEGES`, `SCHEMA_PRIVILEGES`, `TABLE_PRIVILEGES`, `COLUMN_PRIVILEGES`. | `role_table_grants`, `role_column_grants`, `role_routine_grants`, `role_usage_grants`. |
| Privilegios globales          | Devueltos en nivel `global`.                                   | No aplica.                                                        |

### `can_grant`

| Aspecto                          | MariaDB/MySQL                                                                   | PostgreSQL                                                                  |
|----------------------------------|---------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| Superuser bypass                 | No tiene un mecanismo explícito de superuser check.                             | Si `rolsuper = true` → retorna `True` sin más consultas.                    |
| Detección de GRANT OPTION        | `GRANT OPTION` nunca aparece como `PRIVILEGE_TYPE`; se infiere del set grantable no vacío. | `has_*_privilege(current_user, obj, 'PRIV WITH GRANT OPTION')`.           |
| ALL PRIVILEGES                   | Si el set de privilegios grantables del admin incluye todos los pedidos → `True`. | El superuser cubre todo; para rol normal, se verifica objeto a objeto.    |
| Privilegios sin objeto concreto  | Soportado a nivel `global` y `database` (sin tabla/columna).                   | Soportado a nivel `database` y `schema`.                                    |

---

## Restricciones y seguridad

### NUNCA se otorga `ALL PRIVILEGES` automáticamente

El gateway no emite `GRANT ALL PRIVILEGES` de forma automática en ningún flujo (creación de usuario, provisión, apply-profile). Los `initial_grants` y los ítems de perfil deben especificar los privilegios explícitamente.

La única cuenta con todos los privilegios en un servidor es el **admin de conexión** configurado en el registro del servidor (`Server.admin_user`). Esta cuenta existe antes del gateway y no es gestionada por él.

### Pre-chequeo 403 antes de tocar el motor

En el flujo de GRANT (endpoint directo), el controller siempre ejecuta `can_grant()` antes de llamar `grant_object()`. Si el resultado es `False`, la operación termina con 403 sin haber emitido ningún DCL al motor.

En `apply_profile`, este mismo pre-chequeo corre por ítem (best-effort): un item sin capability va a `errors[]` pero no aborta los demás.

### Política del catálogo cerrado

- Los privilegios administrativos (`DENY`) son rechazados con 422 en **cualquier nivel**, incluso si no aparecen en el catálogo `ALLOW` de ese nivel (defensa en profundidad).
- Aliases de entrada (`ALL` → `ALL PRIVILEGES`, `TEMP` → `TEMPORARY`) se normalizan antes de la validación; el token canónico resultante es lo único que se interpola.
- El mensaje de error 422 nunca refleja el token crudo del usuario.

---

## Pendiente (roadmap Fase 2-3)

### Fase 1 — cerrada ✅

Todos los pendientes de Fase 1 se completaron (2026-06-26):

- **AuditLog granular:** `audit_log` incluye los campos `grantee`, `privilege`, `object_level`, `object_name`, `with_grant_option`, `grantor` (migración `f6a7b8c9d0e1`). Los eventos `grant_object`/`revoke_object`/`apply_profile` los rellenan.
- **Auditoría de intención fail-closed:** `audit.record_intent()` registra `status="attempt"` antes de ejecutar todo REVOKE y los GRANT GATE; si no persiste, la operación se aborta.
- **Anti auto-lockout (409):** se rechaza el REVOKE a la propia credencial del gateway.
- **CASCADE en REVOKE con confirmación:** soportado en PostgreSQL con `confirm_grantee` (GATE); MySQL/MariaDB → 422.
- **Tests de integración formales:** `tests/test_grants_integration.py` (`@pytest.mark.integration`), parametrizado por motor, ejercita GRANT/REVOKE/LIST y CASCADE contra engines reales (se saltan si no hay Docker).

### Fase 2 (planificada)

- **Membresía de roles:** endpoints para añadir/quitar usuarios a roles/grupos del motor (`GRANT role TO user`).
- **Confirmación de tokens GATE en GRANT:** doble confirmación para `ALL PRIVILEGES`/`GRANT OPTION` al otorgar (hoy se auditan como intención pero se aceptan sin paso extra).
- **Default privileges:** `ALTER DEFAULT PRIVILEGES` para objetos futuros; endpoint de atributos de cuenta (`CREATEDB`, `CREATE USER`).

### Fase 3 (backlog)

- **Niveles PostgreSQL avanzados:** `FOREIGN SERVER`, `FOREIGN DATA WRAPPER`, `LARGE OBJECT`, `TABLESPACE`.
- **Nivel GLOBAL MariaDB:** soporte para grants globales (`*.*`) con catálogo de privilegios de servidor.
- **Auditoría diferencial:** comparar el estado de grants en el motor contra el estado esperado del inventario, y alertar en divergencias.
