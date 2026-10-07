# API v40 — Tokens de agente de autoservicio: `tokens.own`

Addendum de [v39](api-reference-v39.md) y de la sección de tokens de v23.1 §3. **No hay rutas nuevas**:
las cuatro de `/api-tokens` aceptan ahora una segunda capacidad, `tokens.own`, que acota lo que
la persona ve y toca a los tokens que ella misma emitió. `GET /authz/catalog` y `GET /auth/me`
publican la capacidad nueva.

## Qué cambia

| Aspecto | Antes | Ahora |
|---|---|---|
| Quién administra tokens | Solo `access.admin` | `access.admin` (los de **todos**, sin cambios) o `tokens.own` (solo los **propios**) |
| Quién tiene `tokens.own` | — | Los tres roles (`viewer`, `operator`, `owner`), como `self.read`. Nadie necesita que se la asignen |
| Dueño de un token | — | `api_tokens.created_by_admin_id` |

`access.admin` no cambia de contenido (invariante 10 del catálogo: es exactamente `{access.admin}`).

## La capacidad `tokens.own`

| Campo del catálogo | Valor | Por qué |
|---|---|---|
| `module` / `level` | `tokens` / `own` | `modulo.accion` (invariante 6) |
| `scope_axis` | `global` | No hay entorno ni servidor al que anclarla; por eso no es otorgable suelta ni implica una lectura |
| `mutates` | `false` | Está en `viewer` y el invariante 3 prohíbe que `viewer` mute. El riesgo de escribir la fila lo cubre el step-up de la ruta (abajo), no esta spec |
| `discloses` | `false` | Solo expone los tokens del propio actor y jamás el secreto, que no se guarda |
| `requires_step_up` | `false` | El catálogo solo admite step-up en lo que muta o divulga; la ruta lo exige con la spec de `access.admin` |
| `agent_allowed` | `false` | Un token no puede emitir otro token; no está en el techo de agente y `parse_scopes` la descarta |
| Sensible | No | No está en `owner − operator` |

## Comportamiento de `/api-tokens`

| Ruta | Con `access.admin` | Solo con `tokens.own` |
|---|---|---|
| `GET /api-tokens` | Todos los tokens | Solo los propios. El filtro va **antes** del `COUNT`: `total` y la paginación no delatan cuántos ajenos existen |
| `POST /api-tokens` | Igual que antes | Igual, más dos reglas: los scopes tienen que estar entre las capacidades del emisor (403 `access.forbidden`) y el emisor tiene que poder ver el proyecto (422 `project.not_found`) |
| `PATCH /api-tokens/{id}` | Cualquier token | Solo el propio. Uno ajeno es **404 `api_token.not_found`**, idéntico a uno inexistente, y se resuelve **antes** del 409 `already_revoked`. Los scopes que **se agregan** tienen que estar entre las capacidades del editor (403) |
| `DELETE /api-tokens/{id}` | Cualquier token | Solo el propio; un ajeno es el mismo 404 |

### Se conserva todo lo que ya protegía a los tokens

- **Step-up** en POST, PATCH y DELETE (`403 access.step_up_required`). Se evalúa siempre con la spec de
  `access.admin`, no con la de `tokens.own`: si dependiera de la capacidad que el actor tiene, el
  autoservicio sería más laxo que el camino administrativo. El `GET` no pide step-up, como antes.
- **Techo de agente** y scopes de datos (`data.read`, `data.query`, `data.definitions`): siguen siendo solo
  de `owner` (un `viewer` u `operator` recibe 403 al pedirlos), con step-up fresco del emisor, rastro
  `api_token.data_scope_grant` y el TTL propio `MCP_DATA_TOKEN_MAX_TTL_DAYS`.
- **El token ejerce la intersección con su emisor** al autenticar (`token_actor`), como antes.
- **TTL**: `MCP_TOKEN_MAX_TTL_DAYS` sin cambios.

### Proyecto

El proyecto **no tiene ACL por usuario**: `GET /projects/{id}` lo ve cualquiera con `blueprints.read`, y
hasta ahora el alta solo verificaba que el proyecto existiera. Ese es el "puede acceder" que se exige en
autoservicio (`blueprints.read` del emisor). Los tres roles la tienen, así que hoy la comprobación no
rechaza a nadie por rol; existe para que, si alguna vez hay una ACL de proyecto o un rol sin esa lectura,
el autoservicio no sea el camino que la saltea. Cuando falla responde el mismo 422 que un proyecto
inexistente.

### Auditoría

`api_token.create`, `api_token.update` y `api_token.revoke` guardan en `detail`, además de lo anterior,
`emisor=<id del dueño del token>` y `modo=propio|administrador`. `admin_id` sigue siendo quien actuó, así
que una revocación hecha por un administrador sobre el token de otra persona dice las dos cosas.

## Para la SPA

- `tokens.own` está en `CAPABILITIES` (`lib/contracts/auth.ts`). La entrada de menú, la pantalla y el listado
  se habilitan con `access.admin` **o** `tokens.own`.
- La SPA no filtra por dueño: renderiza lo que el servidor devuelve.
- Sin `access.admin`, el selector de permisos deshabilita los que el rol de la persona no tiene; el 403 del
  servidor sigue siendo la barrera.

## Código y casos

| Código | Status | Cuándo |
|---|---|---|
| `api_token.not_found` | 404 | El token no existe **o** es de otra persona y quien pide no tiene `access.admin` |
| `access.forbidden` | 403 | Scope que el emisor no tiene (autoservicio) |
| `project.not_found` | 422 | Proyecto inexistente, o que el emisor no puede ver |
| `access.step_up_required` | 403 | Ventana de step-up cerrada en un método no seguro |

## Compatibilidad

Sin migración de datos: `created_by_admin_id` ya existía. Un token cuyo emisor fue borrado (sin FK) solo lo
ve `access.admin`. Los tokens emitidos antes del cambio siguen siendo de quien los emitió.
