# API v36 — Scopes de datos de un token y opt-in de datos por base (MCP)

Addendum de [v35](api-reference-v35.md) (credencial de DATOS por base). Entrega 4 de
`mcp-readonly-query-execution`: el catálogo gana `data.read` y `data.query`, el emisor se
re-autentica al darlos, cada base tiene un opt-in de datos con segundo aprobador y hay dos kill
switches. **Ninguna tool de datos existe todavía** (llegan en las entregas 5 y 6): hasta entonces
estos scopes son inertes aunque se otorguen.

## Resumen para el frontend

| Cambio | Ruta | Capacidad |
|---|---|---|
| Nuevo | `GET /api/v1/managed-databases/{db_id}/data-credential` | `databases.read` |
| Nuevo | `POST /api/v1/managed-databases/{db_id}/data-access/request` | `data.read` en el entorno + step-up |
| Nuevo | `POST /api/v1/managed-databases/{db_id}/data-access/approve` | `data.read` en el entorno + step-up |
| Nuevo | `DELETE /api/v1/managed-databases/{db_id}/data-access` | `data.read` en el entorno + step-up |
| Cambia | `POST /api-tokens`, `PATCH /api-tokens/{id}` | aceptan `data.read` y `data.query` (ver abajo) |
| Cambia | `GET /authz/catalog` | dos filas nuevas; `agent_allowed` + `discloses` + `requires_step_up` a la vez |

`DataCredentialOut` gana `data_access_state` (`closed` / `pending` / `open`) y
`data_access_second_approver_required` (aditivo, con default). Variables nuevas:
`MCP_DATA_READ_ENABLED=false`, `MCP_DATA_QUERY_ENABLED=false`, `MCP_DATA_TOKEN_MAX_TTL_DAYS=30`.

## Catálogo: `data.read` y `data.query`

- Divulgan, exigen step-up, son del techo de agente, **no mutan**, eje `environment`, solo `owner`
  (nunca `viewer`/`operator`) y **sensibles** (otorgarlas sueltas pide segundo aprobador): el conjunto
  sensible pasa de 11 a 13.
- Son la ÚNICA excepción cerrada a "un token nunca divulga" y a "step-up implica no agente"
  (`AGENT_DATA_EXCEPTIONS`, invariante 13 del catálogo). Mutar no tiene excepción.

## Tokens (`/api-tokens`)

- Un scope de datos exige un **step-up fresco del emisor** al crear y al editar: 403
  `access.step_up_required` si la ventana venció. El token no tiene contraseña; la da quien lo emite.
- Rastro `api_token.data_scope_grant` con `record_intent` **fail-closed** (500 y no se otorga si el
  rastro no se persiste).
- Vida máxima `MCP_DATA_TOKEN_MAX_TTL_DAYS` (30): 422 `api_token.ttl_too_long` con `max_days`. En el
  PATCH cuenta la vida **restante** del token. Sin `expires_in_days` el default (90) excede el tope y
  también da 422: hay que pedir el TTL explícito.
- Con el kill switch de la capacidad apagado el scope se **guarda y se lista** pero el token no lo
  ejerce (inerte). `GET /api-tokens` lo muestra igual para que un PATCH no lo pierda.

## Opt-in de datos por base

Sin cuerpo en los tres. Capa 2 sobre la base: hace falta `data.read` EN su entorno (owner).

- `POST .../data-access/request`: en `production` (y en bases sin entorno) el pedido queda `pending`;
  en los demás entornos abre en el acto (`open`). Exige credencial de datos (409
  `data_credential.missing`). 409 `data_access.already_open` si ya estaba abierto. Audita
  `managed_database.data_access_request` / `_open` con `record_intent` fail-closed.
- `POST .../data-access/approve`: lo aprueba **otro** owner. 403
  `data_access.self_approval_forbidden` para el solicitante; 409 `data_access.not_pending` sin pedido.
- `DELETE .../data-access`: cierra o cancela el pedido. Inmediato e idempotente; no toca el motor ni la
  credencial (borrarla es `DELETE .../data-credential`). Audita `managed_database.data_access_close`.
- Abrir NO alcanza para leer: el gate exige además credencial con sonda verde reciente y el kill
  switch encendido.

## Errores con `public_context.code`

| Código | Status | Cuándo |
|---|---|---|
| `data_access.self_approval_forbidden` | 403 | El solicitante intentó aprobar su propio pedido |
| `data_access.not_pending` | 409 | `approve` sin un pedido pendiente |
| `data_access.already_open` | 409 | `request` con el acceso ya abierto |
| `data_access.identity_required` | 403 | El actor no es un usuario del gateway (fail-closed) |
| `data_credential.missing` | 409 | La base no tiene credencial de datos |
| `api_token.ttl_too_long` | 422 | Token con scope de datos que excede `MCP_DATA_TOKEN_MAX_TTL_DAYS` |
| `access.step_up_required` | 403 | Falta la confirmación de contraseña |

Los de `data_access.*` viven en `app/services/data_credential_catalog.py`.
