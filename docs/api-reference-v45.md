# API v45 — Tokens de integración y API `/integration`

Addendum de [v44](api-reference-v44.md). Suma **dos superficies**: la gestión de tokens de integración
(`/api/v1/integration-tokens`, con sesión, para personas) y la API de integración
(`/api/v1/integration/...`, con bearer, para el proyecto web de una persona). El porqué de cada decisión, en
`docs/development/decisiones-e-incidentes.md`; cómo habilitarla y operarla, en
[`integration-api-tokens.md`](features/integration-api-tokens.md).

Nace **apagada** (`INTEGRATION_API_ENABLED=false`). Apagada, `/integration/*` responde `503 integration.disabled`
y la gestión no crea ni edita (listar y revocar siguen andando).

Esta versión cubre los **diez scopes de lectura y escritura**. Revertir migraciones (`rollback`) y marcar
versiones (`stamp`) no existen todavía en el contrato: llegan en su propio addendum.

## El bearer

`Authorization: Bearer datumint.<public_id>.<secret>`. Solo el HMAC del secreto se guarda; el valor completo se
muestra **una vez**, en la respuesta de creación. La cookie de sesión **no** autentica `/integration/*` y el
bearer **no** autentica el resto de la API ni `/mcp/`.

## Gestión de tokens (`/api/v1/integration-tokens`, sesión + CSRF)

Guard: `access.admin` **o** `integration_tokens.own` (los tres roles base). Con `integration_tokens.own` se
administran solo los tokens propios; `access.admin` lista y revoca los de todos, no los edita.

| Método y ruta | Qué hace | Notas |
|---|---|---|
| `GET /integration-tokens` | Lista (paginada) | `suspended_scopes` solo en los tokens propios |
| `GET /integration-tokens/ceiling` | Scopes que quien llama puede otorgar hoy, `enabled`, `max_ttl_days`, `max_write_ttl_days` | Un scope que el rol no tiene **no aparece** (ni como deshabilitado) |
| `POST /integration-tokens` | Emite un token; `201` con `token` (el bearer) | 10/min; `503` con el kill switch apagado |
| `PATCH /integration-tokens/{token_pk}` | Reemplaza `name`, `scopes`, `server_ids`, `blueprint_ids`, `note` (lo enviado reemplaza) | El secreto, el TTL y el emisor no cambian |
| `DELETE /integration-tokens/{token_pk}` | Revoca | `409` si ya estaba revocado |

Todo método no seguro (alta, edición y revocación) exige **step-up fresco** de la persona, por el guard de la
ruta; la máquina que usa el token nunca lo responde, así que la contraseña de quien emite se paga al otorgar el
scope.

`POST` acepta `{name (3..128), scopes[≥1], server_ids[], blueprint_ids[], expires_in_days?, note?}` con
`extra="forbid"`. No hay campo de secreto.

### Reglas de emisión

- **Vocabulario cerrado**: cada scope pertenece a la lista de abajo; cualquier otro valor (incluidas capacidades
  reales como `access.admin`, `data.read` o `blueprints.apply`) es `422 integration_token.unknown_scope`.
- **Techo del emisor**: un scope que quien emite no tiene hoy es `403 integration_token.scope_not_allowed`.
- **Allowlist de servidores obligatoria** (`422 integration_token.server_allowlist_required`).
- **TTL**: 90 días con solo lectura, 30 con cualquier scope de escritura (`INTEGRATION_TOKEN_MAX_TTL_DAYS`,
  `INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS`). Excederlo es `422 integration_token.ttl_too_long` con `max_days`; una
  edición que lo excede deja el token intacto.
- **Sin expiración**: `never_expires: true` en la emisión (excluyente con `expires_in_days`) deja `expires_at: null`.
  Solo con `INTEGRATION_ALLOW_NON_EXPIRING_TOKENS=true`, y nunca con scopes destructivos (tampoco se pueden agregar
  después). `GET /integration-tokens/ceiling` informa `allow_non_expiring`.
- **Scopes suspendidos**: lo efectivo es `guardado ∩ vocabulario ∩ lo que el emisor tiene hoy`. Si el emisor
  pierde una capacidad, el scope queda suspendido en el acto (se informa en `suspended_scopes`) y vuelve si
  la recupera; no se puede volver a agregar mientras esté suspendido, sí quitar.

## Scopes

| Scope | Nivel | Capacidad evaluada | Ruta |
|---|---|---|---|
| `servers.list` | lectura | `servers.read` | `GET /integration/servers` |
| `databases.list` | lectura | `databases.read` | `GET /integration/databases?server_id=` |
| `blueprint.read_assigned` | lectura | `blueprints.read` | `GET /integration/databases/{db_id}/blueprint` |
| `migrations.read_version` | lectura | `blueprints.read` | `GET /integration/databases/{db_id}/migrations/version` |
| `databases.create` | escritura | `databases.write` | `POST /integration/databases` |
| `engine_users.create` | escritura | `engine_users.write` | `POST /integration/engine-users` |
| `engine_users.assign_profile` | escritura | `engine_users.write` | `POST /integration/engine-users/{user_id}/profiles/{profile_id}` |
| `engine_users.assign_database` | escritura | `engine_users.write` | `POST /integration/engine-users/{user_id}/databases/{db_id}` |
| `databases.assign_blueprint` | escritura | `databases.write` | `PUT /integration/databases/{db_id}/blueprint` |
| `migrations.apply_forward` | escritura | `blueprints.apply` | `POST /integration/databases/{db_id}/migrations/apply` |
| `migrations.rollback` | **destructivo** | `blueprints.apply` | `POST /integration/databases/{db_id}/migrations/rollback` |
| `migrations.stamp` | **destructivo** | `blueprints.apply` | `POST /integration/databases/{db_id}/migrations/stamp` |

Ningún scope se publica en `/authz/catalog`: son un vocabulario propio, que **mapea** a capacidades existentes.

## Operaciones

Todas las respuestas usan `ApiResponse[T]`. Los cuerpos rechazan campos desconocidos (`422`): `force`,
`password`, `model_id` al crear una base, etc. no se ignoran en silencio.

| Operación | Cuerpo | Respuesta |
|---|---|---|
| `GET /servers` | — | `[{id, name, engine}]`: solo los de la allowlist que el emisor puede leer; sin host ni puerto |
| `GET /databases` | `server_id` (query, obligatorio) | `[{id, name, server_id, model_id, model_version, environment_id, status, charset, collation}]` |
| `GET .../blueprint` | — | `{model_id, name, slug, model_version}`; `404 integration.blueprint_not_assigned` si no hay |
| `GET .../migrations/version` | — | `{current_version, latest_version, pending[]}` |
| `POST /databases` | `{name, server_id, owner_id, environment_id?, charset?, collation?, notes?}` | `201`, base **vacía** (el blueprint se asigna y aplica con sus scopes) |
| `POST /engine-users` | `{server_id, username, host?, notes?}` | `201` con `password` generada por el gateway; **única vez**, no se puede releer |
| `POST .../profiles/{profile_id}` | `{object_mappings[≥1]}` (nivel ≠ `global`, `object_ref.database` obligatorio) | `ApplyProfileResult` |
| `POST .../databases/{db_id}` | `{profile_id}` (el mapeo lo arma el gateway) | `ApplyProfileResult` |
| `PUT .../blueprint` | `{model_id}` | Base actualizada; repetir el mismo es éxito sin cambios |
| `POST .../migrations/apply` | `{version?, dry_run?}` | `MigrationApplyOut`; `force=False` y `on_failure="auto"` fijos |
| `POST .../migrations/rollback` | `{from_version, to_version}` (ambas obligatorias) | `MigrationRollbackOut`; sin `dry_run`, `force` ni `purge` (`422`) |
| `POST .../migrations/stamp` | `{expected_current_version, version}` (la clave es obligatoria; puede ser `null`) | `MigrationStatusOut`; sin `force` ni `purge` (`422`) |

Reglas que cambian el resultado:

- **Servidor desconocido o fuera de la allowlist**: un mismo `403 integration.server_not_allowed` (no se pueden
  enumerar ids). Un blueprint fuera de la allowlist del token: `403 integration.blueprint_not_allowed`.
- **Perfiles**: un perfil con cualquier ítem que delega poder (`ALL PRIVILEGES`, `GRANT OPTION`) es
  `403 integration.profile_requires_grant_admin` **antes** de aplicar un solo grant. Un usuario de otro servidor
  o inexistente, o una base no gestionada en su servidor, es `409 integration.server_mismatch`.
- **Blueprint**: reemplazar uno ya asignado es `409 integration.blueprint_already_assigned`.
- **Migraciones**: una `version` menor a la actual es `422 integration.migration_target_not_forward`; la actual
  es un no-op exitoso. La guarda de migraciones destructivas de los entornos protegidos rige como para una persona.

### Nivel destructivo (`migrations.rollback`, `migrations.stamp`)

Sobre lo anterior, y siempre antes de tocar nada (ninguna de estas negativas deja una fila `attempt`):

- **Allowlist de blueprints obligatoria** en el token (`422 integration_token.blueprint_allowlist_required` al
  emitir o editar). En la llamada, una base sin blueprint, o fuera de la allowlist, o un token con la allowlist
  vaciada, es `403 integration.blueprint_not_allowed` (falla cerrado).
- **Entorno**: un entorno que bloquea migraciones destructivas es `409 integration.environment_blocks_destructive`; una
  base **sin entorno** es `409 integration.environment_unclassified` (el flujo humano la deja pasar; una máquina no).
- **Cuarentena**: base en `error` es `409 integration.database_quarantined`.
- **Rollback**: compare-and-set en ambos extremos (`from_version` debe ser la versión viva, lo valida el
  controlador con `422`; `to_version` debe ser anterior). No hay "un paso atrás" ni "volver a la base". **Prueba de
  historial**: cada versión que se desharía tiene que constar, en su última fila de `database_migration_history`,
  como `up` exitoso con el `checksum` que el blueprint tiene hoy; si no, `409 integration.rollback_unapplied_version`
  (con `versions[]`). Una versión solo marcada con `stamp`, editada después de correr, o con fila legada sin
  dirección, no tiene prueba.
- **Stamp**: `expected_current_version` distinto de la versión viva es `409 integration.stamp_version_conflict`;
  contabilidad huérfana es `409 integration.stamp_orphan_accounting`; marcar la versión en la que ya está es un
  `200` sin cambios, auditado y sin llegar al controlador. Nunca limpia una cuarentena.
- **Step-up al emitir**: agregar o editar un scope destructivo exige un step-up de `blueprints.apply` **de la persona
  que emite** (en tiempo de ejecución el bearer no lo pide, como en el resto). Con `STEP_UP_ENFORCED=false` se
  respeta el interruptor global. TTL máximo `INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS` (7 días).
- **Auditoría**: `audit.record_intent` (falla cerrado, `500` y no se ejecuta nada) con acción
  `integration.migration.rollback` / `integration.migration.stamp`, antes de delegar en el controlador humano.
- **Cupo**: `INTEGRATION_DESTRUCTIVE_RATE_LIMIT` (`5/minute`) por token, además de los de lectura y escritura.

Los errores propios del controlador (versión de confirmación distinta `422`, falta `down_sql` `409`, captura sin revisar
`409`, checkpoint parcial `409`, versión desconocida `422`) llegan sin cambios.

## Códigos de error (`public_context.code`)

| Código | HTTP | Cuándo |
|---|---|---|
| `integration.disabled` | 503 | Kill switch apagado |
| `integration.token_invalid` | 401 | Bearer ausente, malformado, desconocido, revocado, vencido o con emisor inactivo (misma respuesta byte a byte) |
| `integration.scope_missing` | 403 | El token no tiene el scope efectivo de la ruta |
| `integration.server_not_allowed` | 403 | Servidor fuera de la allowlist o inexistente |
| `integration.blueprint_not_allowed` | 403 | Blueprint fuera de la allowlist del token |
| `integration.profile_requires_grant_admin` | 403 | El perfil delega poder |
| `integration.blueprint_already_assigned` | 409 | La base ya tiene otro blueprint |
| `integration.server_mismatch` | 409 | Usuario o base de otro servidor |
| `integration.blueprint_not_assigned` | 404 | La base no tiene blueprint |
| `integration.migration_target_not_forward` | 422 | Versión objetivo anterior a la actual |
| `integration.environment_blocks_destructive` | 409 | El entorno de la base bloquea operaciones destructivas |
| `integration.environment_unclassified` | 409 | La base no tiene entorno asignado |
| `integration.database_quarantined` | 409 | La base está en cuarentena |
| `integration.rollback_unapplied_version` | 409 | Una versión a revertir no consta como aplicada por el gateway con su definición actual |
| `integration.stamp_version_conflict` | 409 | `expected_current_version` no coincide con la versión viva |
| `integration.stamp_orphan_accounting` | 409 | La base tiene contabilidad de migraciones huérfana |
| `integration_token.blueprint_allowlist_required` | 422 | Un scope destructivo exige allowlist de blueprints |
| `integration_token.not_found` | 404 | Token ajeno o inexistente |
| `integration_token.ttl_too_long` | 422 | TTL por encima del tope del nivel (`max_days`); también `never_expires` con un scope destructivo |
| `integration_token.non_expiring_not_allowed` | 422 | `never_expires: true` con `INTEGRATION_ALLOW_NON_EXPIRING_TOKENS` apagado |
| `integration_token.scope_not_allowed` | 403 | El emisor no tiene la capacidad del scope |
| `integration_token.unknown_scope` | 422 | Scope fuera del vocabulario |
| `integration_token.server_allowlist_required` | 422 | Falta la allowlist de servidores |
| `integration_token.already_revoked` | 409 | El token ya estaba revocado |
| `integration_token.server_not_found` | 422 | Un `server_ids` no existe |
| `integration_token.blueprint_not_found` | 422 | Un `blueprint_ids` no existe |

Además, `429` con `Retry-After` al superar el cupo por token (`INTEGRATION_RATE_LIMIT`, y el de escritura
`INTEGRATION_WRITE_RATE_LIMIT` en los de escritura, `INTEGRATION_DESTRUCTIVE_RATE_LIMIT` en los destructivos) y `429` por IP al superar los rechazos de credencial
(`INTEGRATION_AUTH_FAILURE_RATE_LIMIT`).

## Auditoría

Cada llamada autenticada deja `integration.call` con `actor_type = "integration"`, `integration_token_id` = PK del
token, `admin_id` = el emisor y `admin_username = "integration:<public_id>"`. `GET /audit-log?actor_type=integration`
filtra por ellas. La gestión deja `integration_token.create|update|revoke` con la persona como actor. Ninguna fila
lleva el secreto ni la contraseña generada.

## Variables de entorno

`INTEGRATION_API_ENABLED` (`false`), `INTEGRATION_TOKEN_MAX_TTL_DAYS` (`90`), `INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS`
(`30`, no puede superar la anterior), `INTEGRATION_RATE_LIMIT` (`120/minute`), `INTEGRATION_WRITE_RATE_LIMIT`
(`20/minute`), `INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS` (`7`, no puede superar el de escritura: con
`INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS` menor a 7 hay que bajar también este), `INTEGRATION_DESTRUCTIVE_RATE_LIMIT`
(`5/minute`), `INTEGRATION_AUTH_FAILURE_RATE_LIMIT` (`30/minute`). Se leen una vez al importar
`app/core/environments.py`: cambiarlas exige reinicio.
