# API v35 — Credencial de DATOS por base gestionada (MCP)

Addendum de [v32](api-reference-v32.md) (credencial de solo lectura **por servidor**, que sirve a la
estructura). Esta es la hermana **por base**: una cuenta del motor con `SELECT` sobre UNA base y nada
más. Entregas 2 y 3 de `mcp-readonly-query-execution`: **ninguna tool del MCP la usa todavía**. Es
la pieza que las tools de datos (`sample_rows`, `run_select`) van a necesitar, y queda inerte hasta
entonces. La entrega 3 agrega la sonda (`POST .../verify`).

## Resumen para el frontend

| Cambio | Ruta | Capacidad |
|---|---|---|
| Nuevo | `POST /api/v1/managed-databases/{db_id}/data-credential/provision` | `servers.admin` + step-up |
| Nuevo | `DELETE /api/v1/managed-databases/{db_id}/data-credential` | `servers.admin` + step-up |
| Nuevo | `POST /api/v1/managed-databases/{db_id}/data-credential/verify` | `servers.admin` + step-up |

Aditivo. Nueva tabla `managed_database_data_credentials` (migración `e4a6c8f0b2d5`, vacía al
desplegar) y nuevas variables `MCP_DATA_ACCOUNT_PREFIX` (default `mcp_d_`) y
`MCP_DATA_CREDENTIAL_MAX_AGE_DAYS` (default `7`). No cambia ningún contrato
existente ni `ManagedDatabaseOut`.

## `POST /managed-databases/{db_id}/data-credential/provision`

- **Sin cuerpo.** Usuario (`MCP_DATA_ACCOUNT_PREFIX` + id de la base, p. ej. `mcp_d_42`), host
  (`MCP_READONLY_ACCOUNT_HOST`), grants y contraseña los fija el servidor. Un cuerpo enviado se
  ignora.
- **Alcance POR BASE:** MySQL/MariaDB `GRANT SELECT ON <base>.*` y nada más (sin `SHOW VIEW`,
  `TRIGGER`, `EVENT` ni privilegios globales), con `MAX_USER_CONNECTIONS 3` (y `MAX_STATEMENT_TIME 30`
  en MariaDB). PostgreSQL: rol `LOGIN` sin atributos de administración, `CONNECTION LIMIT 3`,
  `default_transaction_read_only = on`, `statement_timeout = '30s'`, `CONNECT` sobre la base y
  `USAGE` + `SELECT` por cada esquema de usuario. Una tabla creada después no queda cubierta hasta
  repetir el aprovisionamiento.
- **Respuesta 200:** `ApiResponse[DataCredentialOut]`: `managed_database_id`, `has_data_credential`,
  `verified_at`, `probed_at`, `probe_violations`, `probe_warnings`, `data_access_allowed`. **Nunca**
  devuelve usuario, contraseña ni el cifrado. Tras aprovisionar, `verified_at` es `null`: la sonda es
  un paso aparte (`POST .../verify`) y hasta que pase ninguna tool de datos puede usar la credencial.
- **Límite de tasa:** 3/minuto.
- **Elegibilidad:** la base tiene que estar `active` (existir en el motor) y no ser una base de
  sistema ni la base de metadatos del propio gateway.
- **Idempotente solo para cuentas propias:** si la cuenta existe y esta base ya guarda una
  credencial de datos con ese usuario, rota la contraseña y re-aplica el grant. Si existe y no es
  propia: 409 sin cambios. El gateway guarda la contraseña nueva (cifrada, sin verificar) **antes**
  de que el motor cambie, así que un reintento tras una falla a medias converge.
- Auditoría: `managed_database.data_credential.provision` (intención `attempt`, fail-closed, y
  resultado `success`/`error`). Sin usuario ni contraseña.
- No existe una tool equivalente en el MCP, a propósito.

## `DELETE /managed-databases/{db_id}/data-credential`

Palanca de emergencia, **idempotente**: sin credencial no hace nada. Con credencial, primero la
des-verifica y cierra el opt-in (el corte es inmediato aunque el motor no conteste), después borra
la cuenta del motor (`DROP USER` / `DROP ROLE`) y, solo si eso funcionó, la fila. Si el motor falla,
responde el error del motor y la fila queda para que el reintento termine la revocación. Nunca borra
una cuenta que el gateway no creó. **Hacerlo antes de bajar la migración**: borrar la tabla pierde
la contraseña y deja huérfanas las cuentas del motor. Auditoría: `managed_database.data_credential.clear`.

## `POST /managed-databases/{db_id}/data-credential/verify`

Sonda NEGATIVA. Sin cuerpo; 6/minuto; mismo lock por base que aprovisionar y revocar. Conecta con la
cuenta de DATOS de esa base (nunca la pseudo-root) y exige que el motor muestre SELECT sobre
**exactamente esa base**:

- MySQL/MariaDB (`SHOW GRANTS FOR CURRENT_USER()`): sin privilegios globales salvo `USAGE`, sin ningún
  privilegio que no sea `SELECT`, sin patrones con `_`/`%` sin escapar, sin grants sobre otras bases,
  sin `GRANT OPTION`/roles/`PROXY` ni líneas que la sonda no entienda. El nombre se compara
  distinguiendo mayúsculas salvo `lower_case_table_names` 1 o 2. Tablas `FEDERATED`/`CONNECT`/`SPIDER`
  en la base **bloquean**.
- PostgreSQL: sin atributos de administración, sin pertenencia a roles, cero privilegios de escritura
  sobre relaciones de usuario (también los de `PUBLIC`), `default_transaction_read_only = on` e intento
  real de escritura rechazado, `CONNECTION LIMIT` entre 1 y 3, `statement_timeout` fijado, sin `CONNECT`
  explícito a otras bases y sin `dblink`/`postgres_fdw`/`mysql_fdw`/`file_fdw` (bloquean).

**200** (`DataCredentialOut`): `verified_at` fijado y `probe_warnings` con lo no bloqueante
(`cross_schema_view_reference`, `definer_views_present`, `public_connect_other_databases`,
`create_on_database`, `create_on_schema_public`). **422** `managed_database.data_probe_failed`:
`verified_at` queda en `null` (se BORRA la verificación anterior), `public_context.reasons` lleva
códigos públicos cerrados y `public_context.violations` los motivos cortos; nunca texto de grants ni
mensaje del motor. Si la sonda no pudo correr (motor caído), también se des-verifica y responde el error
del motor. Auditoría: `managed_database.data_credential.verify`.

| `reasons` | Motivos cortos (`violations`) |
|---|---|
| `CREDENTIAL_TOO_BROAD` | `global_privilege:*`, `extra_privilege:*`, `wildcard_database_pattern`, `select_outside_database`, `grant_option`, `unrecognized_grant`, `role_attribute:*`, `member_of:*`, `member_of_role` |
| `WRITE_PRIVILEGE_PRESENT` | `privilege:*`, `all_privileges`, `table_write_privileges`, `default_transaction_read_only_off`, `write_attempt_succeeded` |
| `FEDERATED_TABLE_PRESENT` | `foreign_engine_table`, `foreign_access_extension` |
| `PROBE_NOT_GREEN` | `missing_select_on_database`, `connection_limit`, `statement_timeout_unset`, `engine_unsupported` |

La verificación vale `MCP_DATA_CREDENTIAL_MAX_AGE_DAYS` (7) días; pasado el plazo, las tools de datos
(entregas 5 y 6) la rechazan con `PROBE_NOT_GREEN`. Re-aprovisionar borra la verificación: la
contraseña nueva no está verificada.

## Errores con `public_context.code`

| Código | Status | Cuándo |
|---|---|---|
| `data_credential.account_already_exists` | 409 | En el motor ya existe una cuenta con ese usuario y el gateway no la creó. No se cambió nada: configurar otro `MCP_DATA_ACCOUNT_PREFIX` |
| `data_credential.provision_in_progress` | 409 | Ya hay un aprovisionamiento o una revocación de esta base en curso (lock por proceso). No se cambió nada |
| `data_credential.database_not_eligible` | 409 | La base no está `active`, es de sistema o es la base de metadatos del gateway |
| `data_credential.missing` | 409 | `verify` sobre una base sin credencial de datos |
| `managed_database.data_probe_failed` | 422 | La sonda observó que la credencial no es SELECT-only sobre esa base (ver `reasons` y `violations`) |
| `engine_user.protected_account` | 409 | El usuario resultante es una cuenta reservada o un rol PostgreSQL con privilegios de administración |
| `access.step_up_required` | 403 | Falta la confirmación de contraseña |
| (sin código) | 404 | La base no existe |
| (sin código) | 4xx/5xx | Fallo del motor; reintentable |

Los códigos `data_credential.*` y `managed_database.data_probe_failed` viven en `app/services/data_credential_catalog.py`.
