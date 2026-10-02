# API v30 — Credencial de solo lectura del servidor (MCP)

Addendum de la credencial de **solo lectura** que el MCP usa para leer el catálogo de un motor
(plan 12 §5.2). Lo consume la pantalla de detalle de servidor del frontend.

## Resumen para el frontend

| Cambio | Ruta | Capacidad |
|---|---|---|
| Campos nuevos en `ServerOut` | todas las que devuelven un servidor | — |
| Nuevo | `PUT /api/v1/servers/{server_id}/readonly-credential` | `servers.admin` + step-up |
| Nuevo | `DELETE /api/v1/servers/{server_id}/readonly-credential` | `servers.admin` + step-up |
| Parámetro nuevo | `POST /api/v1/servers/{server_id}/test-connection?credential=readonly` | `servers.read`, y escala a `servers.admin` + step-up con `credential=readonly` |
| Campo nuevo en `ConnectionInfo` | respuesta de `test-connection` | — |

Ningún contrato existente cambia de forma: todo es aditivo.

## `ServerOut`: dos campos nuevos

```jsonc
{
  "has_readonly_credential": true,          // hay usuario y contraseña de solo lectura
  "readonly_verified_at": "2026-10-02T15:04:05" // null = sin verificar (el MCP no lo lee)
}
```

**Nunca** sale el usuario ni la contraseña de solo lectura, cifrados o en claro.

`readonly_verified_at` vuelve a `null` cuando:

- se registra o reemplaza la credencial (`PUT`);
- la sonda negativa falla;
- se quita la credencial (`DELETE`).

Además, un `PATCH /servers/{id}` que **re-apunta** el servidor (cambia `host`, `port` o `engine`, o
debilita un TLS `require` o más fuerte) **descarta la credencial de solo lectura** entera:
`has_readonly_credential` vuelve a `false`. El `audit_log` lo registra como «credencial de solo
lectura descartada por re-apuntado».

## `PUT /servers/{server_id}/readonly-credential`

```json
{ "username": "mcp_ro", "password": "…" }
```

- `extra="forbid"`: un campo de más es `422`.
- Responde `ServerOut`, con `readonly_verified_at: null`. El mensaje recuerda que falta verificarla.
- Auditado como `server.readonly_credential.set`, sin usuario ni contraseña.

## `DELETE /servers/{server_id}/readonly-credential`

- Idempotente: quitarla dos veces no es un error.
- Responde `ServerOut` con `has_readonly_credential: false`.
- Auditado como `server.readonly_credential.clear`.

## `POST /servers/{server_id}/test-connection?credential=readonly`

`credential` admite `root` (default, comportamiento de siempre) o `readonly`.

Con `readonly` corre la **sonda negativa**: conecta con la credencial de solo lectura y exige que
el motor observe que no puede escribir. Si pasa, fija `readonly_verified_at` y lo devuelve en
`ConnectionInfo`:

```jsonc
{ "ok": true, "dialect": "mysql", "server_version": "8.0.36",
  "readonly_verified_at": "2026-10-02T15:04:05" }
```

No cambia el `status` del servidor, que describe la conexión con la pseudo-root.

Errores con `public_context.code`:

| Código | Status | Cuándo |
|---|---|---|
| `server.readonly_credential_missing` | 409 | El servidor no tiene credencial de solo lectura registrada |
| `server.readonly_probe_failed` | 422 | La credencial puede escribir. `public_context.violations` lista los motivos (`privilege:insert`, `global_privilege:select`, `select_on_mysql_schema`, `grant_option`, `unrecognized_grant`, `role_attribute:rolsuper`, `default_transaction_read_only_off`, `create_on_database`, `create_on_schema_public`, `member_of:pg_write_all_data`, `table_write_privileges`, `write_attempt_succeeded`) |
| `access.step_up_required` | 403 | Falta la confirmación de contraseña (`servers.admin` la exige) |

Los errores de conexión del motor son los mismos que en la prueba con `root`.

**Sugerencia de UI:** un bloque «Acceso de agentes (MCP)» en el detalle del servidor, con el estado
(sin credencial / sin verificar / verificada el …, vencida a los `MCP_READONLY_MAX_AGE_DAYS` días),
un formulario de alta o reemplazo, el botón «Verificar» y el botón «Quitar». Cuando
`server.readonly_probe_failed`, listar las `violations` como texto para el DBA.

## Scopes nuevos para tokens de agente

`POST /api/v1/api-tokens` acepta ahora, además de `blueprints.read`, `databases.read` y
`schema_diff.read` (ya existían en el techo), estos cuatro: `environments.read`, `exports.read`,
`clones.read` y `catalogs.read`. El catálogo (`GET /authz/catalog`) los publica con
`agent_allowed: true`. Un selector de scopes en el alta de tokens tiene que ofrecer los siete.
