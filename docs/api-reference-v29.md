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
(`global_capability`). El orden de chequeos deja este 409 **después** de la política de
asignación (`access.not_assignable`, §9) y de la validación del payload, y **antes** de partir la
elevación: un cambio que viola la regla sin override no llega a crear una solicitud.

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

**Desde C3 es una elevación** (§9): viaja con la solicitud pendiente (`pending_request.sod_override`
o `CapabilityGrantOut.sod_override`) y se aplica recién al APROBARLA. Ahí deja rastro:
`access.sod_override` con `status=attempt` (fail-closed: si no se puede auditar, la escritura no
ocurre) y después `success`, con reglas, fuentes, motivo, vencimiento (contado desde la
aprobación), `approved_by` e ids de excepción. Escribe una fila de `sod_exceptions` por regla con
`requested_by` = quien lo pidió y `approved_by` = el segundo `access_admin`. Solo con
`ACCESS_FOUR_EYES=False` se aplica en el acto, con `approved_by: null`.

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

La siembra (sigue `owner` + las dos globales, heredada). El techo de otorgamiento y los flujos de
aprobación cambian en C3 (§9); la siembra nueva, C4.

## 9. C3 — Política de asignación y segundo aprobador

### 9.1 Por qué cambia

El techo de otorgamiento era por **tenencia** ("no das más de lo que tenés"): para crear un `owner`
había que ser `owner`, y para crear un `security_officer`, serlo. Con las funciones partidas eso
obliga a que quien administra accesos tenga, para siempre, cada deber que reparte: justo la
combinación que la separación de deberes prohíbe. **Se retiró**, junto con su código
`access.grant_ceiling_exceeded` (ya no lo emite ningún endpoint).

Lo reemplazan dos cosas:

- **Asignación por función** (`ASSIGNABLE_BY` en `capability_catalog.py`): `access_admin` asigna
  cualquier rol, cualquier global y cualquier capacidad otorgable. Sin esa función → `409
  access.not_assignable` (en la práctica la ruta corta antes con `403`, porque todas exigen
  `access.admin`).
- **Segundo aprobador para las elevaciones** (`needs_second_approver`). Es lo que impide el títere de
  F-2: un administrador solo ya no puede crearse un `owner`, recibir su invitación y entrar con esa
  cara.

### 9.2 Qué es una elevación

Todo lo exclusivo de `owner`, en cualquier forma:

| Elevación | `elevations[].kind` |
|---|---|
| Rol base `owner` (desde cualquier otro) | `base_role` (`role`) |
| Rol `owner` en un alcance donde no lo tenía | `scope_grant` (`scope_type`, `scope_id`, `role`) |
| **Cualquier** global agregada (`access_admin`, `security_officer`) | `global_capability` (`global_capability`) |
| Una capacidad puntual sensible | (flujo de capacidades puntuales, 9.6) |
| Un `sod_override` | (viaja con la solicitud, `sod_override`) |

`operator` **no** es elevación. **Las bajas nunca lo son y se aplican siempre en el acto**: bajar un
rol, quitar un alcance, quitar una global.

**Capacidades sensibles: de 8 a 11.** Pasan a ser exactamente las exclusivas de `owner` otorgables
(owner − operator): se suman `blueprints.apply`, `schema_diff.execute` y `collation.execute`, que
sin el techo las otorgaba un solo administrador. `capability_matrix()` (`GET /authz/catalog`)
publica `sensitive: true` para las 11 y `catalog_version` cambia. Las puntuales de esas tres que ya
estaban `active` siguen activas.

### 9.3 Escritores: `200`/`201` de siempre, o `202 access.elevation_pending`

`POST /gateway-users`, `PATCH /gateway-users/{id}` y `PUT /gateway-users/{id}/access` **parten** el
cambio:

- Lo que no eleva se aplica en el request (con su auditoría, el corte de sesiones y el candado del
  último administrador, como siempre).
- Lo que eleva nace como **solicitud pendiente** y la respuesta es **`202`**.
- Un cambio **sin** elevación responde exactamente como antes (`200`/`201`, sin campos nuevos).

Forma del `202` (`ApiResponse[GatewayUserPendingOut]`; en el alta,
`ApiResponse[GatewayUserCreatedPendingOut]`, que suma `invite_token` / `invite_expires_at`):

```json
{"data": {
   "id": 7, "username": "ana", "gateway_role": "viewer", "global_capabilities": [],
   "scope_grants": [], "...": "el resto de GatewayUserOut: la persona como quedó YA",
   "code": "access.elevation_pending",
   "pending_request": {
     "id": 12, "status": "pending", "origin": "create",
     "target": {"id": 7, "username": "ana"},
     "requested_by": {"id": 3, "username": "aa1"},
     "desired": {"gateway_role": "owner", "global_capabilities": ["access_admin"],
                 "scope_grants": []},
     "elevations": [{"kind": "base_role", "role": "owner"},
                    {"kind": "global_capability", "global_capability": "access_admin"}],
     "sod_override": null,
     "created_at": "…", "expires_at": "… (+7 días)",
     "decided_by": null, "decided_at": null, "reason": null}},
 "message": "La elevación quedó pendiente: …"}
```

- **Alta**: la cuenta nace con la parte que no eleva (`viewer`, u `operator` si se pidió; **sin
  globales**) y la invitación se emite igual.
- **PATCH**: el resto del PATCH (contacto, `is_active`) se aplica; el rol queda pendiente.
- **PUT /access**: `desired` es el acceso FINAL completo. Un `owner` nuevo en un alcance deja ese
  alcance como estaba (o sin grant) hasta aprobar; las altas `viewer`/`operator` y las bajas, ya.
- `origin`: `create` | `update` | `set_access`.
- Una solicitud nueva sobre la misma persona **reemplaza** a la pendiente anterior (que pasa a
  `cancelled`, `reason: "superseded"`).
- La separación de deberes (§8) se evalúa sobre el estado FINAL **antes** de partir: un `409
  access.sod_conflict` no deja nada aplicado ni pendiente.

### 9.4 `/access-requests` (todo `access.admin`)

| Método y ruta | Qué hace | Step-up |
|---|---|---|
| `GET /access-requests/pending` | Pendientes vigentes de todas las personas, con `can_decide` y `blocked_reason` (código `access.*`) para quien pregunta. Barre las vencidas antes. `ApiResponse[PendingAccessRequestOut[]]`. | no |
| `GET /access-requests/{id}` | Una solicitud en cualquier estado. `ApiResponse[AccessRequestOut]`. | no |
| `POST /access-requests/{id}/approve` | Aplica la elevación. Body opcional `{"reason": "…"}` (máx. 500). | sí |
| `POST /access-requests/{id}/reject` | `pending` → `rejected`. Cualquier `access_admin`. Body opcional. | sí |
| `POST /access-requests/{id}/cancel` | `pending` → `cancelled`. **Solo quien la pidió.** Body opcional. | **no** (exento: retirar algo propio nunca da acceso) |

`AccessRequestOut` es el `pending_request` de 9.3. `status`: `pending` | `applied` | `rejected` |
`cancelled` | `expired`. `reason`: el de la decisión o el cierre automático (`expired`,
`requester_lost_access`, `superseded`, `stale`).

**Reglas del approve** (las mismas que las capacidades puntuales):

1. Aprueba **otro** `access_admin`: ni quien la pidió (`409 access.self_approval_forbidden`) ni la
   persona destino (`409 access.self_modification_forbidden`).
2. Quien la pidió tiene que seguir siendo `access_admin` activo; si no, la solicitud pasa a
   `cancelled` (`requester_lost_access`) y el approve da `409 access.request_not_pending`. Quitarle
   `access_admin` o desactivarlo ya cancela sus pendientes en el acto.
3. La persona destino tiene que estar activa (`409 access.grant_user_inactive`).
4. **Solicitud vieja**: si el acceso de la persona (rol base, globales, alcances) cambió desde que
   se pidió (`before_hash`), `409 access.request_stale` y la solicitud pasa a `cancelled`
   (`stale`). Hay que pedirla de nuevo sobre el acceso actual.
5. Se re-chequea la separación de deberes sobre el estado final (`409 access.sod_conflict`; la
   solicitud sigue `pending`).
6. Compare-and-set: el reclamo de la fila corre **dentro** de la transacción que escribe el acceso,
   con el candado del último administrador (F-24). Con dos aprobadores simultáneos gana uno; el
   otro recibe `409 access.request_not_pending`.
7. Al aplicar se tachan las sesiones de la persona (`role_change`).

Vencimiento: 7 días. Perezoso (listado y cada decisión) y una vez al arrancar.

**Errores** (`detail.public_context.code`):

| Código | HTTP | Cuándo |
|---|---|---|
| `access.request_not_found` | 404 | La solicitud no existe. |
| `access.request_not_pending` | 409 | Ya aplicada, rechazada, cancelada o vencida; o el solicitante perdió `access_admin`. |
| `access.request_stale` | 409 | El acceso de la persona cambió desde el pedido (la solicitud se cancela). |
| `access.request_not_requester` | 409 | `cancel` de alguien que no la pidió (que la rechace). |
| `access.self_approval_forbidden` | 409 | Quien la pidió intenta aprobarla. |
| `access.self_modification_forbidden` | 409 | La persona destino intenta aprobar su propia elevación. |
| `access.grant_user_inactive` | 409 | La persona destino está desactivada. |
| `access.not_assignable` | 409 | La función del actor no asigna eso. |
| `access.sod_conflict` | 409 | El estado final viola la separación de deberes sin cobertura. |
| `access.last_admin_protected` | 409 | (Candado de F-24, dentro de la transacción.) |

**Auditoría**: `access_request.created`, `access_request.approved` (`success` / `failure` con el
código como `reason`), `access_request.rejected`, `access_request.cancelled` y
`access_request.expired` (`target_type: access_request`, `target_id` = la solicitud, `grantee` =
la persona). El **efecto** de una aprobación se audita además como `gateway_user.access_set` con el
`before`/`after` completo, `request_id`, `requested_by` y `approved_by`: "¿quién le dio `owner` y
qué tenía antes?" se sigue respondiendo en un solo lugar. Vencidas y canceladas por pérdida del rol
van con `actor_type: system`.

### 9.5 `ACCESS_FOUR_EYES`

Variable de entorno, **`True` por defecto**. `False` es solo para instalaciones de **un único
administrador**: las elevaciones (incluidas las capacidades sensibles y el `sod_override`) se
aplican en el acto, sin solicitud, el arranque loguea un warning y cada una se audita
`access.elevation_unapproved` (`detail`: `origin` = `create` | `update` | `set_access` |
`capability_grant`, `elevations`, `sod_override`).

> **Instalaciones con un solo `access_admin` (incluido el admin sembrado de una instalación
> nueva).** Con `ACCESS_FOUR_EYES=True` y nadie más que pueda aprobar, toda elevación queda
> pendiente — incluida la de crear el segundo `access_admin`. Hasta que C4 traiga la ventana de
> arranque, el camino es arrancar con `ACCESS_FOUR_EYES=False`, crear el segundo `access_admin` y
> volver a `True`. Lo que no eleva (operadores, lectores, bajas) funciona igual.

### 9.6 Capacidades puntuales

- **Alta**: el techo ("tenés que tener la capacidad en ese alcance") se reemplaza por la asignación
  por función (`409 access.not_assignable`). Las 11 sensibles nacen `pending`; una alta con
  `sod_override` también, y el override queda en `CapabilityGrantOut.sod_override` (campo nuevo,
  `null` en el resto).
- **Approve**: basta con que el aprobador sea `access_admin` (un `viewer` aprueba). Si trae
  `sod_override`, la excepción se escribe al aprobar con `approved_by`. `blocked_reason` puede ser
  ahora `access.not_assignable` o `access.sod_conflict`; ya no `access.grant_ceiling_exceeded`.

### 9.7 Lo que la SPA tiene que cambiar

- `POST /gateway-users`, `PATCH /gateway-users/{id}` y `PUT /gateway-users/{id}/access` pueden
  responder **`202`**: mirar `data.code === "access.elevation_pending"` y `data.pending_request`.
  La persona en `data` es el estado YA aplicado, no el pedido.
- Bandeja de elevaciones (`GET /access-requests/pending`), como la de capacidades puntuales.
- Mensajes para `access.elevation_pending`, `access.request_stale`, `access.request_not_pending`,
  `access.request_not_requester`, `access.request_not_found`, `access.not_assignable`. Retirar
  `access.grant_ceiling_exceeded`.
- `sensitive` en el catálogo cambia para tres capacidades (refrescar con `catalog_version`).

