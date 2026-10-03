# API v35 — Credencial de DATOS por base gestionada (MCP)

Addendum de [v32](api-reference-v32.md) (credencial de solo lectura **por servidor**, que sirve a la
estructura). Esta es la hermana **por base**: una cuenta del motor con `SELECT` sobre UNA base y nada
más. Entrega 2 de `mcp-readonly-query-execution`: **ninguna tool del MCP la usa todavía**. Es la
pieza que las tools de datos (`sample_rows`, `run_select`) van a necesitar, y queda inerte hasta
entonces.

## Resumen para el frontend

| Cambio | Ruta | Capacidad |
|---|---|---|
| Nuevo | `POST /api/v1/managed-databases/{db_id}/data-credential/provision` | `servers.admin` + step-up |
| Nuevo | `DELETE /api/v1/managed-databases/{db_id}/data-credential` | `servers.admin` + step-up |

Aditivo. Nueva tabla `managed_database_data_credentials` (migración `e4a6c8f0b2d5`, vacía al
desplegar) y nueva variable `MCP_DATA_ACCOUNT_PREFIX` (default `mcp_d_`). No cambia ningún contrato
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
  devuelve usuario, contraseña ni el cifrado. Tras aprovisionar, `verified_at` es `null`: la sonda
  llega en una entrega posterior y hasta entonces ninguna tool de datos puede usar la credencial.
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

## Errores con `public_context.code`

| Código | Status | Cuándo |
|---|---|---|
| `data_credential.account_already_exists` | 409 | En el motor ya existe una cuenta con ese usuario y el gateway no la creó. No se cambió nada: configurar otro `MCP_DATA_ACCOUNT_PREFIX` |
| `data_credential.provision_in_progress` | 409 | Ya hay un aprovisionamiento o una revocación de esta base en curso (lock por proceso). No se cambió nada |
| `data_credential.database_not_eligible` | 409 | La base no está `active`, es de sistema o es la base de metadatos del gateway |
| `engine_user.protected_account` | 409 | El usuario resultante es una cuenta reservada o un rol PostgreSQL con privilegios de administración |
| `access.step_up_required` | 403 | Falta la confirmación de contraseña |
| (sin código) | 404 | La base no existe |
| (sin código) | 4xx/5xx | Fallo del motor; reintentable |

Los tres códigos `data_credential.*` viven en `app/services/data_credential_catalog.py`.
