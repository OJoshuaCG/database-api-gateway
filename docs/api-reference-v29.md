# Separación de deberes: `gateway.admin` se parte en `access.admin` y `policy.admin`

Addendum sobre el catálogo de capacidades (`GET /authz/catalog`, `/auth/me`) y sobre las rutas que
declaraban `gateway.admin`. **Hay un cambio incompatible para la SPA**: el id `gateway.admin`
**deja de existir** y lo reemplazan dos ids nuevos. Ningún path, payload ni envelope cambia; lo que
cambia es qué capacidad declara cada ruta y, por lo tanto, quién recibe `403`.

## 1. Por qué

`gateway.admin` la tenían las **dos** capacidades globales, `access_admin` y `security_officer`.
Así un `security_officer` sin `access_admin` administraba usuarios, tokens y capacidades puntuales
igual que un administrador de accesos: la separación de deberes estaba en el papel. Partida en
dos, cada global tiene la suya y los conjuntos son disjuntos.

## 2. Las capacidades

| Id | Qué cubre | La tiene | `mutates` | `requires_step_up` | `scope_axis` | `agent_allowed` |
|---|---|---|:--:|:--:|---|:--:|
| `access.admin` | `/gateway-users` (todo menos aceptar la invitación), `/api-tokens`, `/capability-grants`, `GET /authz/scope-readiness` | solo `access_admin` | ✅ | ✅ | `global` | ❌ |
| `policy.admin` | `POST /admin/crypto/rotate` (y la lectura de auditoría cuando exista) | solo `security_officer` | ✅ | ✅ | `global` | ❌ |
| ~~`gateway.admin`~~ | **retirada** | — | | | | |

Las capacidades globales quedan así:

| Global | Capacidades |
|---|---|
| `access_admin` | `access.admin` — **exactamente** esa, nada más |
| `security_officer` | `policy.admin`, `servers.admin`, `catalogs.write`, `environments.write` |

Ninguna de las dos es otorgable suelta (`grantable: false`): son de eje global. Un
`POST /gateway-users/{id}/capability-grants` con `access.admin`, `policy.admin` o `gateway.admin`
responde `422 access.capability_not_grantable`.

**Step-up.** Las dos exigen step-up, con la misma regla de siempre: los `GET` de una capacidad que
no divulga **no** lo piden (listar usuarios, tokens o la bandeja de pendientes sigue sin
interrumpir); los métodos que mutan, sí.

## 3. Rutas

| Rutas | Antes | Ahora |
|---|---|---|
| `GET/POST /gateway-users`, `GET/PATCH /gateway-users/{id}`, `PUT /gateway-users/{id}/access`, `POST /gateway-users/{id}/invite`, `GET/POST /gateway-users/{id}/capability-grants`, `DELETE /gateway-users/{id}/capability-grants/{grant_id}`, `GET /gateway-users/{id}/effective-access` (10) | `gateway.admin` | `access.admin` |
| `GET/POST /api-tokens`, `DELETE /api-tokens/{token_pk}` (3) | `gateway.admin` | `access.admin` |
| `GET /capability-grants/pending`, `POST /capability-grants/{id}/approve`, `POST /capability-grants/{id}/reject` (3) | `gateway.admin` | `access.admin` |
| `GET /authz/scope-readiness` | `gateway.admin` | `access.admin` |
| `POST /admin/crypto/rotate` | `gateway.admin` | `policy.admin` |

`POST /gateway-users/invite/accept` sigue siendo **público**.

## 4. Quién pierde qué

- **Un `security_officer` sin `access_admin`** recibe ahora `403 access.forbidden` en
  `/gateway-users`, `/api-tokens`, `/capability-grants` y `scope-readiness`. Antes pasaba la ruta
  y, en las capacidades puntuales y `effective-access`, el controller lo frenaba igual con el
  mismo 403;
  en el resto **administraba de verdad**. Es el efecto buscado.
- **Un `access_admin` sin `security_officer`** recibe `403 access.forbidden` en
  `POST /admin/crypto/rotate`.
- **Quien tiene las dos** (el admin sembrado, por ejemplo) no pierde nada.

El 403 es el opaco de siempre: no nombra la capacidad que falta.

## 5. Lo que la SPA tiene que cambiar

- **Los ids:** donde se gateaba con `gateway.admin`, usar `access.admin` para administración de
  usuarios/accesos/tokens/capacidades puntuales y `policy.admin` para la pestaña de cifrado.
- `catalog_version` cambia (es una huella de la matriz), así que la caché del catálogo se
  invalida sola.
- Gatear por **capacidad** (`can('access.admin')`) y no por global
  (`global_capabilities.includes('access_admin')`): hoy son equivalentes, pero la capacidad es lo
  que hace cumplir el servidor.

## 6. Datos

**No hay migración.** Las globales se guardan como los strings `access_admin` /
`security_officer` y el mapeo global → capacidades es código. Un `gateway.admin` en
`api_tokens.scopes` o en `capability_grants.capability` no puede existir legítimamente (nunca fue
`agent_allowed` ni otorgable) y los lectores ya descartan las capacidades desconocidas o
globales: no acuña nada.

## 7. Lo que este addendum NO cambia

El techo de otorgamiento, el usuario sembrado, la regla de separación de deberes entre `owner` y
`security_officer` y los flujos de aprobación siguen exactamente igual.
