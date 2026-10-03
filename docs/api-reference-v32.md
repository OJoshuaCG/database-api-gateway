# API v32 — Aprovisionar la credencial de solo lectura del servidor (MCP)

Addendum de v30: un solo click reemplaza el trío «el DBA crea la cuenta → `PUT` → `test-connection`».
Lo consume el bloque «Acceso de agentes (MCP)» del detalle de servidor.

## Resumen para el frontend

| Cambio | Ruta | Capacidad |
|---|---|---|
| Nuevo | `POST /api/v1/servers/{server_id}/readonly-credential/provision` | `servers.admin` + step-up |

Aditivo: ningún contrato existente cambia. `PUT`/`DELETE .../readonly-credential` y
`test-connection?credential=readonly` siguen siendo el camino manual.

## `POST /servers/{server_id}/readonly-credential/provision`

- **Sin cuerpo.** Ni usuario, ni host, ni contraseña, ni grants: los fija el servidor
  (`MCP_READONLY_ACCOUNT_USERNAME`, `MCP_READONLY_ACCOUNT_HOST` y una lista fija de privilegios).
  Un cuerpo enviado se ignora.
- **Respuesta 200:** `ApiResponse[ServerOut]` con `has_readonly_credential: true` y
  `readonly_verified_at` cargado. **Nunca** devuelve usuario ni contraseña.
- **Límite de tasa:** 3/minuto.
- **Idempotente solo para cuentas propias:** si la cuenta existe y este servidor ya guarda una
  credencial de solo lectura con ese usuario, rota la contraseña y re-aplica los grants; si existe y
  no es propia, 409 `readonly_account.already_exists` sin cambios. Aprovisionar de nuevo invalida la
  contraseña anterior (el gateway guarda la nueva, sin verificar, antes de que el motor cambie).
  `SHOW_ROUTINE` solo se otorga en MySQL >= 8.0.20.
- **Es una operación sobre el motor de un tercero** con la pseudo-root, y la credencial es **por
  servidor**: alcanza todas sus bases no internas, no solo las de un proyecto. La UI tiene que
  decirlo en la confirmación.
- Auditoría: `server.readonly_credential.provision` (intención `attempt` y resultado
  `success`/`error`), más los `set` y `verify` que ya existían. Sin usuario ni contraseña.
- No existe una tool equivalente en el MCP, a propósito.

Errores con `public_context.code`:

| Código | Status | Cuándo |
|---|---|---|
| `server.readonly_probe_failed` | 422 | La cuenta creada puede escribir; `public_context.violations` lista los motivos. El servidor queda **sin verificar** y la credencial registrada, para corregir grants y reintentar |
| `readonly_account.already_exists` | 409 | En el motor ya existe una cuenta con el usuario configurado y el gateway no la tiene guardada como credencial de solo lectura de este servidor. No se cambió nada: registrarla a mano (`PUT`) o cambiar `MCP_READONLY_ACCOUNT_USERNAME` |
| `readonly_account.has_roles` | 409 | La cuenta propia (MySQL/MariaDB) tiene roles u otros grants que el aprovisionamiento no puede quitar. No se cambió nada: revocarlos en el motor y reintentar |
| `readonly_provision.in_progress` | 409 | Ya hay un aprovisionamiento de este servidor en curso (lock por proceso). No se cambió nada: reintentar al terminar |
| `engine_user.protected_account` | 409 | El nombre configurado es una cuenta reservada, la pseudo-root, o un rol PostgreSQL con privilegios de administración |
| `access.step_up_required` | 403 | Falta la confirmación de contraseña |
| (sin código) | 404 | El servidor no existe |
| (sin código) | 4xx/5xx | Fallo del motor al crear la cuenta; los mismos mapeos que el resto de operaciones del motor. Es reintentable y deja `readonly_verified_at: null` |

Los tres códigos `readonly_account.*` / `readonly_provision.*` viven en `app/services/server_catalog.py`. La UI debería mapearlos a un mensaje claro para el operador (mismas indicaciones que la columna «Cuándo»).

**Sugerencia de UI:** un botón «Aprovisionar credencial» junto a «Registrar manualmente», con
confirmación que diga que se usará la pseudo-root y que el alcance es todo el servidor. Si la base
nueva no aparece en el MCP, la respuesta es repetir el aprovisionamiento (no cubre bases creadas
después).
