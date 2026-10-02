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

## 7. Lo que la partición (C1) NO cambia

El techo de otorgamiento, el usuario sembrado y los flujos de aprobación siguen exactamente igual.
La regla de separación de deberes llega en C2 (sección 8).

## 8. C2 — Regla de separación de deberes

### 8.1 La regla

Una cuenta con `security_officer` **no puede** tener además:

| Regla (`rule`) | Choca con `security_officer` |
|---|---|
| `owner_security_officer` | rol base `owner`, **o** un rol `owner` por alcance, **o** una capacidad puntual viva (pendiente o activa) exclusiva de `owner`: `blueprints.apply`, `blueprints.captures`, `clones.execute`, `collation.execute`, `databases.drop`, `engine_users.credentials`, `engine_users.drop`, `engine_users.secrets`, `exports.download`, `schema_diff.execute`, `sql_console.execute` |
| `access_admin_security_officer` | la global `access_admin` |

Se evalúa sobre el **estado resultante** de la cuenta, no sobre el payload.

### 8.2 Escritores: `409 access.sod_conflict`

`POST /gateway-users`, `PATCH /gateway-users/{id}` (cuando cambia `gateway_role`),
`PUT /gateway-users/{id}/access`, `POST /gateway-users/{id}/capability-grants` y
`POST /capability-grants/{id}/approve` responden `409` si el resultado viola una regla que
**ninguna excepción viva cubre**:

```json
{"code": "access.sod_conflict",
 "rules": ["owner_security_officer"],
 "conflicts": [{"rule": "owner_security_officer",
                "sources": [{"kind": "base_role", "role": "owner"}]}],
 "override": {"field": "sod_override", "reason_min_length": 20, "max_hours": 168}}
```

`sources[].kind`: `base_role` | `scope_grant` (`scope_type`, `scope_id`, `role`) |
`capability_grant` (`capability`, `scope_type`, `scope_id`) | `global_capability`
(`global_capability`). El orden de chequeos deja este 409 **después** del techo de otorgamiento
(`access.grant_ceiling_exceeded`) y de la validación del payload.

Una cuenta **heredada** (8.4) sigue editable mientras el cambio no agregue una regla nueva: su
excepción la cubre. Cuando un cambio deja de violar una regla, la excepción de esa regla se
**cierra** (`closed_reason: resolved`) y volver a juntar las funciones pide otro override.

### 8.3 Break-glass: `sod_override`

Los cuatro escritores con payload (todos menos `approve`) aceptan:

```json
"sod_override": {"reason": "Incidente 4711: no hay otro security_officer", "expires_in_hours": 24}
```

- `reason`: obligatorio, **mínimo 20** caracteres, máximo 500.
- `expires_in_hours`: 1–168 (7 días). Por defecto 168.
- Inválido → `422 access.sod_override_invalid` (`reason_min_length`, `max_hours`).
- Sin conflicto, se ignora.

**En C2 se aplica en el acto** y siempre deja rastro: `access.sod_override` con `status=attempt`
(fail-closed: si no se puede auditar, la escritura no ocurre) y después `success`, con reglas,
fuentes, motivo, vencimiento e ids de excepción. Escribe una fila de `sod_exceptions` por regla con
`approved_by: null`. **C3 lo va a enrutar por el segundo aprobador.**

### 8.4 Lector: neutralización y herencia

- **Al leer** (cada request): si la cuenta viola una regla sin excepción viva —un `UPDATE` a mano,
  un override vencido—, se **descartan las capacidades de `security_officer`** (`policy.admin`,
  `servers.admin`, `catalogs.write`, `environments.write`). `owner` y `access_admin` se conservan.
  Se audita como `access.denied` con `check: "sod"` (agregado por ventana).
- **Herencia.** La migración `f8b0d2e4a6c9` crea `sod_exceptions` e inserta una fila
  `reason='grandfathered'`, `expires_at=null` por cada combinación que ya existe. El admin
  sembrado (`owner` + `access_admin` + `security_officer`) queda heredado en las dos reglas y **no
  pierde nada**; `bootstrap_admin` hace lo mismo al sembrarlo o revivirlo.
- **Arranque.** Cada fila heredada viva se loguea (warning) y se audita `access.sod_grandfathered`
  (`actor_type: system`) una vez por arranque.

### 8.5 `/auth/me`: `sod_warnings`

Campo nuevo, lista (vacía en el caso normal). **Declararlo `.nullish()` en la SPA.**

```json
"sod_warnings": [{"rule": "owner_security_officer", "status": "grandfathered",
                  "reason": "grandfathered", "since": "2026-10-02T10:00:00", "expires_at": null}]
```

`status`: `grandfathered` | `override` (vigente; `expires_at` dice cuándo vence) |
`neutralized` (sin excepción: `security_officer` ya está descartado; `reason`/`since` en null).

### 8.6 `GET /authz/sod-report` (`access.admin`)

```json
{"exceptions": [{"id": 1, "user": {"id": 1, "username": "admin"}, "user_active": true,
                 "rule": "owner_security_officer", "kind": "grandfathered",
                 "reason": "grandfathered", "since": "…", "expires_at": null,
                 "requested_by": null, "approved_by": null, "still_violating": true}],
 "uncovered": [{"user": {"id": 7, "username": "x"}, "user_active": true,
                "rules": ["owner_security_officer"]}]}
```

`exceptions`: las vivas (`kind`: `grandfathered` | `override`). `uncovered`: cuentas que violan
una regla sin excepción. Sin step-up (es un `GET` que no divulga). `security_officer` solo → 403.

### 8.7 Lo que C2 NO cambia

El techo de otorgamiento, la siembra (sigue `owner` + las dos globales, heredada) y los flujos de
aprobación. Las solicitudes de acceso con segundo aprobador son C3; la siembra nueva, C4.
