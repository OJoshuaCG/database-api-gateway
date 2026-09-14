# Administración de identidades: `/gateway-users`, tokens de agente y confirmaciones

Addendum hermano de `api-reference-v23.md`. Aquel publicó el **modelo** de capacidades —qué
existe, quién lo tiene, cómo llega el 403— y cerró diciendo que `/gateway-users` y `/api-tokens`
todavía no existían. Este documento cubre justamente eso: los módulos que hacen **usable** ese
modelo, más los ajustes de contrato que quedaron fuera.

**No repite v23.** Para el envelope de autorización, `/auth/me`, el catálogo de capacidades, el
CSRF, los vencimientos de sesión, la descarga de exportaciones en dos pasos y el transporte del
MCP, la fuente sigue siendo v23. Acá se referencia por sección.

**Recordatorio que aplica a todo lo que sigue.** La SPA hace `safeParse` del envelope **completo**:
un campo nuevo o divergente descarta la respuesta entera. Cada campo agregado en este documento se
declara `.nullish()` en zod, **nunca `.optional()`**, y cada schema existente que gane un campo hay
que tocarlo antes de desplegar el backend.

---

## 1. Único cambio incompatible: `confirm_target_name` en `adopt` 🔴

`POST /api/v1/schema-comparisons/{comparison_id}/adopt` gana el campo `confirm_target_name`
(`string | null`). **Es obligatorio cuando `execute_immediately` es `true`** y tiene que coincidir
**exacto** con el `target_database_name` de la comparación.

```jsonc
{
  "selected_item_ids": [12, 13, 14],
  "name": "0008_alinear_con_produccion",
  "description": null,
  "execute_immediately": true,
  "confirm_target_name": "tienda_cliente_42",  // == target_database_name, carácter por carácter
  "auto_resolve_dependencies": false
}
```

Si falta o no coincide, **422**:

```jsonc
{ "detail": { "msg": "'confirm_target_name' debe coincidir exactamente con el nombre de la base de datos target para aplicar la versión.",
              "type": "AppHttpException",
              "public_context": { "code": "schema_comparison.adopt_confirmation_required" } } }
```

Lo que hay que saber para adaptarlo:

- **Se ignora cuando `execute_immediately` es `false`.** Crear la versión sin aplicarla es
  escritura de metadatos del gateway, reversible y sin tocar ningún motor del cliente. Pedir
  confirmación ahí sería fricción sobre el caso inofensivo, que es exactamente cómo se entrena el
  reflejo de confirmar sin leer. El formulario debe mostrar el campo **solo** con el toggle de
  ejecutar encendido.
- **El nombre a re-tipear ya está en la pantalla**: es `target_database_name` del `GET
  /schema-comparisons/{id}`. No hace falta pedirlo de nuevo al backend.
- **El detalle del error no llega en producción.** El campo `required` de este 422 viaja en
  `context`, no en `public_context`, y `context` solo se expone en `development`. El `code` es todo
  lo que el cliente puede leer; el copy del diálogo lo pone la UI.
- Capacidades: `schema_diff.execute` **y** `blueprints.write`, las dos siempre (v23 §4). Límite de
  tasa 3/min.

---

## 2. `/gateway-users` — administración de usuarios del gateway

Siete endpoints. **Todos detrás de `gateway.admin`** —que solo tienen las capacidades globales
`access_admin` y `security_officer`, no el rol `owner` (v23 §5)— **excepto aceptar la invitación**,
que es público.

Cuidado con el nombre, porque el gateway tiene dos poblaciones de usuarios y usa las mismas
palabras para las dos: acá se administran los que se autentican **contra el gateway**. Los usuarios
del **motor** viven en `/server-users` y `/servers/{id}/users` y no tienen nada que ver.

| Método y ruta | Envelope | Respuesta |
|---|---|---|
| `GET /api/v1/gateway-users` | **`paginated()`** | `GatewayUserOut[]` + `pagination` |
| `POST /api/v1/gateway-users` | `success()` **201** | `GatewayUserCreatedOut` |
| `POST /api/v1/gateway-users/invite/accept` | `success()` · **público** | `{ "username": "…" }` |
| `GET /api/v1/gateway-users/{user_id}` | `success()` | `GatewayUserOut` |
| `PATCH /api/v1/gateway-users/{user_id}` | `success()` | `GatewayUserOut` |
| `PUT /api/v1/gateway-users/{user_id}/access` | `success()` | `GatewayUserOut` |
| `POST /api/v1/gateway-users/{user_id}/invite` | `success()` | `{ invite_token, invite_expires_at }` |

El listado es el **único** del módulo que trae `pagination`; los demás usan el `success()` plano.
Un cliente que espere `pagination` en el detalle va a fallar el `safeParse`.

### 2.1 `GatewayUserOut` — la forma que consumen todas las pantallas

```jsonc
{
  "id": 7,
  "username": "mlopez",
  "email": "mlopez@empresa.com",
  "full_name": "Marina López",
  "gateway_role": "operator",             // viewer | operator | owner
  "is_active": true,
  "credential_set": false,                // ← false = invitación pendiente
  "global_capabilities": ["access_admin"],// access_admin | security_officer
  "scope_grants": [
    { "scope_type": "environment", "scope_id": 3, "role": "viewer" }
  ],
  "last_login_at": "2026-09-01T14:02:11Z",
  "previous_login_at": "2026-08-28T09:41:03Z",
  "last_failed_at": null,
  "created_at": "2026-08-20T11:00:00Z"
}
```

**`credential_set: false` es un estado propio y la pantalla tiene que renderizarlo distinto.**
Significa que la cuenta existe, está en el listado, tiene rol y accesos asignados, y **no puede
iniciar sesión**: nadie fijó todavía la contraseña. No es un usuario desactivado (`is_active`
sigue en `true`) ni un usuario roto. Mostrarlo igual que a los demás hace que un administrador
crea que la persona ya tiene acceso, y el error se descubre recién cuando esa persona reporta que
no puede entrar.

`last_login_at` / `previous_login_at` siguen el mismo criterio que `/auth/me` (v23 §7.4).

### 2.2 `POST /gateway-users` → 201 · el alta no lleva contraseña

```jsonc
{
  "username": "mlopez",              // OBLIGATORIO, 2–40, ^[a-z0-9]([a-z0-9._-]{1,38}[a-z0-9])?$
  "email": "mlopez@empresa.com",     // opcional, ≤ 255
  "full_name": "Marina López",       // opcional, ≤ 150
  "notes": "Alta pedida por soporte",// opcional — ver §2.8
  "gateway_role": "operator",        // opcional, default "viewer"
  "global_capabilities": []          // opcional, default []
}
```

**No hay campo de contraseña, y no es un olvido.** Si quien crea la cuenta tipeara la contraseña
inicial, conocería una credencial funcional de esa identidad, y con eso **toda fila de auditoría
atribuida a esa persona sería repudiable** — para un sistema cuyo valor central es el rastro, eso
es fatal. Un "cambio forzado en el primer login" no lo arregla: quien la puso pudo haber entrado
antes. Con `access_admin` en el modelo deja de ser solo repudio y pasa a ser la vía de escalada:
crear una identidad `owner`, conocer su contraseña y operar producción con la cara de otro.

La consecuencia de diseño es que **el formulario de alta no tiene campos de contraseña ni de
confirmación**, y el copy debe explicar que la persona la elige después. Un formulario que pida
"contraseña inicial" no se puede construir contra esta API.

El patrón de `username` es estricto: minúsculas, dígitos, punto, guion y guion bajo, empezando y
terminando en letra o dígito. Conviene validarlo en el cliente, porque el 422 del servidor no es
máquina-legible en producción (§6).

### 2.3 ⚠️ El token de invitación viaja UNA sola vez y el gateway no lo envía a ningún lado

La respuesta del alta es `GatewayUserCreatedOut` = todo `GatewayUserOut` **más** dos campos:

```jsonc
{
  "data": {
    "id": 7, "username": "mlopez", "credential_set": false, /* …resto de GatewayUserOut… */
    "invite_token": "1757462400.9f2c1a…",       // ← SOLO acá
    "invite_expires_at": "2026-09-11T18:00:00Z" // TTL 48 h
  },
  "message": "Usuario creado. Entregale el token de invitación a la persona."
}
```

> **El gateway no tiene sustrato de notificación.** No hay SMTP, ni webhook, ni cola: el token
> **no se envía por ningún canal**. Viaja en esta respuesta y en ninguna otra, y no existe ningún
> endpoint que lo vuelva a mostrar.
>
> **Si la pantalla no lo muestra y no ofrece copiarlo, la cuenta queda inutilizable.** El usuario
> existe, aparece en el listado, tiene rol y accesos — y nadie puede entrar con él. La única
> salida es `POST /{user_id}/invite`, que emite otro.
>
> El alta **no puede** cerrar el modal y volver al listado: tiene que quedarse en una vista de
> entrega, con el token visible, un botón de copiar y la fecha de vencimiento. Quien crea la
> cuenta se lo entrega a la persona por el canal que corresponda.

### 2.4 `POST /gateway-users/invite/accept` — público, sin sesión y sin CSRF

Es una **pantalla nueva de la SPA**, fuera del área autenticada: quien la usa todavía no puede
iniciar sesión, que es justamente el punto del diseño.

```
POST /api/v1/gateway-users/invite/accept      // sin cookie de sesión, sin X-CSRF-Token
```

```jsonc
{
  "token": "1757462400.9f2c1a…",   // min 8
  "password": "…"                  // min 12, max 200 — la elige la persona
}
```

```jsonc
// 200
{ "data": { "username": "mlopez" }, "message": "Contraseña establecida. Ya podés iniciar sesión." }
```

- **El `user_id` viaja DENTRO del token firmado**, no como parámetro ni en la URL. La pantalla
  recibe solo el token —de la URL, de un campo pegado a mano— y no necesita ningún otro dato.
- **TTL de 48 h** y **un solo uso**. El token es un HMAC sobre `(user_id, credential_epoch)`, y
  aceptar la invitación **sube el epoch**: en cuanto se usa, deja de validar. No hace falta una
  tabla de tokens consumidos.
- **No distingue "token inválido" de "usuario inexistente" de "ya se usó".** Los tres casos
  responden **422 `gateway_user.not_found`**, para no convertir el endpoint en un oráculo de qué
  invitaciones hay pendientes. El copy tiene que cubrir los tres con un solo mensaje y ofrecer
  "solicitar una invitación nueva a quien administra accesos".
- Un token **vencido** responde distinto: **410, y sin código** (§6).
- Límite de tasa **10/min**, porque es público y escribe.
- Después del 200 hay que llevar a la persona al login normal. **No** hay sesión automática.

### 2.5 `PATCH /gateway-users/{user_id}` — y por qué `username` no está

Todos los campos son opcionales y el controller aplica `exclude_unset`: lo que no se envía no
cambia.

```jsonc
{ "full_name": "…", "email": "…", "notes": "…", "gateway_role": "viewer", "is_active": false }
```

**`username` no se puede editar, nunca, y no hay endpoint que lo haga.** Es la identidad que se
audita, y `audit_log` la guarda **desnormalizada y sin FK**: renombrar al usuario reescribiría
retroactivamente el significado de todas las filas viejas, que seguirían nombrando al usuario
anterior. En la UI el campo va deshabilitado en la edición, con el motivo a la vista; ofrecerlo
como editable y fallar después es peor que no ofrecerlo.

Dos efectos secundarios que la pantalla tiene que anticipar:

- **Cambiar el rol o desactivar tacha las sesiones de esa persona.** Si un administrador se edita
  a sí mismo el rol, vuelve al login. Conviene advertirlo antes de enviar.
- **Desactivar al último `access_admin` activo devuelve 409** (§2.9).

### 2.6 ⚠️ `PUT /{user_id}/access` — reemplazo TOTAL, no incremental

Es la trampa más cara del documento.

```jsonc
{
  "global_capabilities": ["access_admin"],
  "scope_grants": [
    { "scope_type": "environment", "scope_id": 3, "role": "viewer" },
    { "scope_type": "server",      "scope_id": 8, "role": "operator" }
  ]
}
```

> **Los dos campos tienen `default_factory=list`, así que omitir uno equivale a enviarlo vacío, y
> enviarlo vacío REVOCA todo.** No hay diferencia entre "no lo mandé" y "quiero que quede sin
> nada": un `PUT` con `{"global_capabilities": ["access_admin"]}` y sin `scope_grants` **borra
> todos los alcances de la persona**, en silencio y con 200.
>
> La única forma segura de usar este endpoint es **leer el estado actual, modificarlo entero y
> reenviarlo completo**. Un formulario que envíe solo la sección que el usuario tocó destruye la
> otra.

Es un `PUT` y no N `POST` por grant a propósito: la pregunta que responde una pantalla de accesos
es *"qué acceso tiene esta persona"*, y con endpoints por grant el estado final depende del orden
de N llamadas — y una que falle a mitad deja un acceso que nadie pidió.

**Un grant REEMPLAZA al rol base en su alcance, no se suma.** Es la semántica que más sorprende:

| Rol base | Grant | Resultado |
|---|---|---|
| `operator` | `viewer` en `environment 3` (producción) | **lectora en producción**, operadora en todo lo demás |
| `viewer` | `owner` en `server 8` | dueña en ese servidor, lectora en el resto |
| `operator` | — | operadora en todo |

O sea que un grant puede **bajar** el acceso, no solo subirlo. Una UI que los presente como
"permisos extra" comunica lo contrario de lo que hace el servidor. **Dos grants sobre el mismo
destino resuelven al más restrictivo.**

`scope_type` acepta exactamente `"environment"` y `"server"`; `scope_id` es `>= 1`. Antes de
otorgar el primer grant conviene consultar `GET /authz/scope-readiness` (v23 §8): una base sin
entorno se trata como el entorno **más protegido**, no como el default.

Igual que el `PATCH`, **este endpoint tacha todas las sesiones** de la persona afectada.

### 2.7 `POST /{user_id}/invite` — reinvitar es también revocar

```jsonc
{ "data": { "invite_token": "…", "invite_expires_at": "…" },
  "message": "Invitación reemitida. La anterior quedó inválida." }
```

**No hay endpoint de revocación de invitaciones, y no hace falta**: emitir una nueva sube el
`credential_epoch` y con eso **invalida la anterior**. Si una invitación se filtró por un canal
equivocado, la acción correcta —y la única— es reinvitar.

**El botón tiene que desaparecer cuando `credential_set` es `true`.** Sobre una cuenta que ya fijó
su contraseña devuelve 409 `gateway_user.credential_already_set`: la invitación es solo para la
primera credencial, y para reemplazar la contraseña la persona la cambia desde su propia sesión.

### 2.8 Dos defectos de contrato, documentados como tales

No son decisiones de diseño; son diferencias entre lo que la API acepta y lo que devuelve, y las
dos muerden a un formulario.

- **`notes` se escribe y nunca se lee.** Se acepta en el alta y en el `PATCH`, y **no está en
  `GatewayUserOut`**. Un formulario que muestre el campo lo va a recibir vacío en cada recarga,
  va a reenviarlo vacío, y el valor anterior se pierde **sin que nada falle**. Hasta que el campo
  exista en la respuesta: no lo pongan en el formulario de edición, o márquenlo explícitamente
  como de solo escritura y nunca lo reenvíen con el valor leído.
- **`email` es opcional pero el servidor lo rellena.** Si se omite, el controller guarda
  `{username}@gateway.local`. O sea que el listado va a mostrar una dirección sintética que nadie
  escribió y que no recibe correo. La UI debería marcarla como "sin correo declarado" en vez de
  presentarla como un dato de contacto, y el alta debería pedir el correo real aunque la API no lo
  exija.

### 2.9 Vocabulario de códigos del módulo

Todos en `detail.public_context.code`.

| `code` | HTTP | Dónde llega | Qué mostrar |
|---|---|---|---|
| `access.last_admin_protected` | 409 | `PATCH` con `is_active: false`; `PUT /access` sin `access_admin` | "Es el último administrador de accesos activo. Hay que otorgar `access_admin` a otro usuario activo primero." **No reintentar.** |
| `gateway_user.not_found` | **404** | `GET`, `PATCH`, `PUT /access`, `POST /{id}/invite` | Volver al listado y refrescar. |
| `gateway_user.not_found` | **422** | `POST /invite/accept` | "La invitación no es válida o ya se usó." Pedir una nueva. |
| `gateway_user.username_taken` | 409 | `POST` | Pedir otro `username`. **No reintentar igual.** |
| `gateway_user.credential_already_set` | 409 | `POST /{id}/invite` | La cuenta ya tiene contraseña: ocultar el botón cuando `credential_set` es `true`. |
| `gateway_user.invalid_role` | 422 | `POST`, `PATCH`, y el `role` de cada grant en `PUT /access` | Trae `public_context.allowed[]` con los roles válidos: es la fuente del selector. |
| `gateway_user.invalid_global_capability` | 422 | `POST` y `PUT /access` (capacidad global), y **también** `scope_type` inválido en `PUT /access` | Ver la nota de abajo. |
| `gateway_user.weak_password` | 422 | `POST /invite/accept` | Trae `public_context.min_length` (12). |

**`gateway_user.not_found` llega con dos status distintos y significan cosas distintas.** El 404 es
"ese id no existe" y manda a refrescar el listado. El 422 es "esta invitación no sirve" y manda a
pedir otra. Un cliente que enrute solo por `code` va a mostrar el mensaje equivocado en uno de los
dos casos: hay que mirar `code` **y** status.

**`gateway_user.invalid_global_capability` está mal nombrado y cubre dos errores distintos.** El
mismo código llega cuando `scope_type` no es `"environment"` ni `"server"` en `PUT /access`, que no
tiene nada que ver con una capacidad global. Y los dos casos no traen la misma información: la
capacidad inválida trae `public_context.allowed[]` con las capacidades globales; el `scope_type`
inválido **no trae `allowed` en absoluto**. Un cliente que asuma que el campo siempre está va a
romper en ese caso. Hasta que se separen los códigos, el mensaje genérico del módulo tiene que
servir para los dos.

**`gateway_user.weak_password` es casi inalcanzable en la práctica**: el schema Pydantic ya rechaza
`password` con menos de 12 caracteres antes de llegar al controller, y ese rechazo sale como 422 de
validación **sin `public_context`** (§6). La validación de largo va del lado del cliente; este
código es la red de contención, no la vía normal.

---

## 3. `/api-tokens` — el contrato que v23 §9.1 no da

v23 §9.1 publica las **reglas** de los tokens de agente (por qué `project_id` es obligatorio, por
qué no hay perpetuos, por qué `DELETE` no se deshace). Acá va la forma.

| Método y ruta | Envelope | Respuesta |
|---|---|---|
| `GET /api/v1/api-tokens` | **`paginated()`** | `ApiTokenOut[]` + `pagination` |
| `POST /api/v1/api-tokens` | `success()` **201** | `ApiTokenCreatedOut` |
| `DELETE /api/v1/api-tokens/{token_pk}` | `success()` | `ApiTokenOut` (la fila ya revocada) |

Todo detrás de `gateway.admin`. El `POST` tiene límite de tasa 10/min.

```jsonc
// POST — request
{
  "name": "ci-tienda-retail",   // OBLIGATORIO, 3–128: describe la máquina o el repo destino
  "project_id": 4,              // OBLIGATORIO, >= 1
  "scopes": [],                 // opcional; VACÍO ⇒ ["blueprints.read"]
  "expires_in_days": 30,        // opcional, >= 1; OMITIDO ⇒ 90 días (no "sin vencimiento")
  "note": "Pipeline de nightly" // opcional
}
```

```jsonc
// POST — respuesta 201
{
  "data": {
    "id": 12,                                  // la PK: es el {token_pk} de la URL del DELETE
    "token_id": "k3f9qm2x",                    // la parte PÚBLICA del bearer, la que audita
    "name": "ci-tienda-retail",
    "scopes": ["blueprints.read"],             // los EFECTIVOS — ver abajo
    "project_id": 4,
    "expires_at": "2026-10-09T12:00:00Z",
    "last_used_at": null,
    "revoked_at": null,
    "note": "Pipeline de nightly",
    "active": true,                            // revoked_at nulo Y expires_at futuro
    "created_at": "2026-09-09T12:00:00Z",
    "token": "dbgw.k3f9qm2x.<secreto>"         // SOLO en el POST, una sola vez
  },
  "message": "Token emitido. Copialo ahora: no se vuelve a mostrar."
}
```

**`id` y `token_id` no son lo mismo y confundirlos rompe el `DELETE`.** `id` es la PK numérica y es
lo que va en `DELETE /api-tokens/{token_pk}`. `token_id` es la parte pública del bearer
(`dbgw.<token_id>.<secreto>`) y es lo que aparece en el rastro de auditoría: sirve para cruzar una
fila `mcp.*` con el token que la originó, no para direccionar el recurso.

**`scopes` de la respuesta son los scopes EFECTIVOS, no el eco del request.** El servidor
intersecta lo pedido con el techo de agente, así que la respuesta **puede traer menos de lo que se
envió**. La pantalla tiene que mostrar lo que devolvió el servidor, no lo que el operador eligió:
mostrar el pedido convierte la revisión de accesos en una afirmación falsa.

**`expires_in_days` omitido no significa "sin vencimiento": significa 90 días.** El copy del
formulario tiene que decirlo, y el default visible debería ser un número, no un campo vacío.

Códigos, todos en `public_context.code`:

| `code` | HTTP | Cuándo | Qué mostrar |
|---|---|---|---|
| `api_token.project_required` | 422 | Falta `project_id` | Pedir el proyecto. Un token sin proyecto no alcanzaría ninguna base. |
| `api_token.ttl_too_long` | 422 | `expires_in_days` fuera de rango | Trae `public_context.max_days` (90): usarlo como tope del control. |
| `api_token.scope_not_allowed` | 422 | Un scope fuera del techo de agente | Trae `public_context.allowed[]` con el **techo completo**. |
| `api_token.not_found` | 404 | `DELETE` sobre un id inexistente | Refrescar el listado. |
| `api_token.already_revoked` | 409 | `DELETE` sobre un token ya revocado | "No fue esta acción la que cortó el acceso." No reintentar. |

**`api_token.scope_not_allowed` con su `allowed[]` es la fuente para armar el selector de scopes
sin hardcodear nada.** Es el techo de agente completo, que es exactamente el conjunto de opciones
que el control debería ofrecer. La alternativa —filtrar el catálogo de v23 §2 por `agent_allowed`—
también sirve y evita provocar un error a propósito.

Un detalle: el mismo código llega cuando el string **no es una capacidad conocida** (un typo), y en
ese caso **no trae `allowed[]`**. Si el selector se alimenta del backend, ese caso no debería
ocurrir nunca.

---

## 4. Entornos: `allows_agent_access`

`PATCH /api/v1/environments/{environment_id}` acepta un campo nuevo, **y `EnvironmentOut` también
lo devuelve**:

```jsonc
{
  "id": 3, "name": "Producción", "slug": "production", "rank": 100, "color": "red",
  "is_default": false, "is_active": true,
  "blocks_destructive_migrations": true,
  "allows_agent_access": false,   // ← CAMPO NUEVO en la respuesta
  "database_count": 12,
  "created_at": "…", "updated_at": "…"
}
```

**La forma de la respuesta cambió**, así que el `safeParse` de la SPA descarta todo `EnvironmentOut`
hasta que el campo esté en el zod. Es el caso exacto de la advertencia de la cabecera: `.nullish()`,
no `.optional()`.

**Encenderlo exige `?confirm_slug=<slug>` como query param**, porque habilita una superficie de
lectura nueva sobre bases de terceros y por eso cuenta como debilitamiento de la política, igual que
apagar el bloqueo de migraciones destructivas:

```
PATCH /api/v1/environments/3?confirm_slug=production
{ "allows_agent_access": true }
```

Si falta o no coincide, **422**:

```jsonc
{ "detail": { "msg": "Este cambio debilita la política del entorno (allows_agent_access). Repetí el slug 'production' en 'confirm_slug' para confirmarlo.",
              "type": "AppHttpException",
              "public_context": {
                "code": "environment.confirmation_required",
                "expected_slug": "production",
                "weakened": ["allows_agent_access"]
              } } }
```

`expected_slug` permite **pre-llenar el diálogo de confirmación** sin volver a pedir el entorno, y
`weakened[]` dice **qué** debilita este PATCH: puede traer más de un elemento si la misma llamada
apaga varias palancas (`blocks_destructive_migrations`, `is_active`, `is_default`), y el diálogo
debería enumerarlas todas.

**`POST /environments` NO acepta `allows_agent_access`.** Crear un entorno abierto a agentes son
siempre **dos llamadas**: el `POST` normal y después el `PATCH` con `confirm_slug`. El asistente de
alta tiene que modelarlo así; no hay forma de hacerlo en un paso.

Recordatorio de v23 §9.3: el flag del entorno **no alcanza solo**. Cada base necesita además su
propio opt-in — y ahí está el problema del §9 de este documento.

---

## 5. Exportaciones: el manifiesto ahora es del dueño

`GET /api/v1/database-exports/{job_id}/manifest` pasa por el mismo guard de propiedad que las dos
entregas, que ya documentaba v23 §7.2 para `/download` y `/content`. El manifiesto quedaba afuera y
la asimetría no era defendible: expone checksum, lista de objetos y conteo de filas del export de
otra persona.

```jsonc
// 403
{ "detail": { "msg": "Esta exportación la creó otro administrador.",
              "type": "AppHttpException",
              "public_context": { "code": "export.not_owner" } } }
```

Capacidad: `exports.read`. **Dos usuarios con `exports.read` sobre el mismo servidor ya no
comparten manifiestos.** Una pantalla que liste exportaciones de todo el equipo va a recibir 403 en
las ajenas: conviene ocultar la acción en vez de ofrecerla y fallar.

Y el **ticket de descarga está atado a `(job_id, user_id)`**, no solo al job. El ticket de otra
persona no sirve, aunque esté vigente. Es relevante si la SPA cachea tickets o los comparte entre
pestañas con sesiones distintas: hay que pedirlo por usuario y por job, en el momento del click.

---

## 6. Respuestas sin `public_context.code`

Son una **limitación conocida del contrato**, no una decisión de diseño. En estos casos el cliente
solo puede enrutar por status, y el copy tiene que salir del contexto de la llamada y no de la
respuesta.

| Caso | Status | Qué llega |
|---|---|---|
| `POST /auth/login` con credenciales inválidas | 401 | `msg: "Credenciales inválidas."`, sin `public_context` |
| Sesión de un usuario desactivado, en cualquier endpoint | 401 | `msg: "Sesión inválida o usuario inactivo."`, sin `public_context` |
| `POST /gateway-users/invite/accept` con invitación vencida | 410 | sin `public_context` |
| `GET /database-exports/{id}/download` con ticket ajeno o malformado | 422 | sin `public_context` |
| `GET /database-exports/{id}/download` con ticket vencido | 410 | sin `public_context` |
| **Cualquier** 429 por límite de tasa | 429 | `{"msg": "Demasiadas solicitudes. Límite: …", "type": "RateLimitExceeded"}` |
| **Cualquier** 422 de validación de Pydantic | 422 | `{"msg": "Error de validación en: username", "type": "RequestValidationError"}` |

Cuatro consecuencias concretas:

- **El 401 de login es el caso más frecuente de la aplicación y no tiene código.** Hay que
  distinguirlo del resto por el endpoint que se llamó, no por el cuerpo.
- **El 401 del usuario desactivado no es ninguno de los `auth.session_*` que tabula v23 §7.3.** Un
  cliente que espere siempre un `code` en los 401 de sesión va a caer en su rama por defecto. El
  mensaje correcto es "la cuenta fue desactivada", que es distinto de "la sesión expiró".
- **El 429 no trae código ni header `Retry-After`.** No hay con qué calcular un backoff: el cliente
  solo puede aplicar una espera fija que conozca de antemano. Esto vale para **todos** los
  endpoints con límite propio, no solo para los dos más sensibles.
- **El 422 de validación no es máquina-legible en producción.** El detalle por campo viaja en
  `context`, que solo se expone en `development`; en producción llega el `msg` con los nombres de
  los campos incrustados en la prosa. Parsear ese texto es frágil: **la validación de formato
  (patrón de `username`, largo de contraseña, rangos) tiene que estar del lado del cliente**, y el
  422 del servidor tratarse como un error genérico de formulario.

---

## 7. Límites de tasa concretos

v23 explica el **eje** (por sesión y no por IP; el login sigue por IP porque todavía no hay sesión)
pero no publica ningún número. Estos son los que están en las rutas:

| Endpoint | Límite |
|---|---|
| `POST /auth/login` | 5/min (por IP) |
| `POST /gateway-users/invite/accept` | 10/min |
| `POST /servers/{id}/users/reveal-password` | **3/min** |
| `POST /database-exports/{id}/download-ticket` | 10/min |
| `GET /database-exports/{id}/download` | 3/min |
| `POST /api-tokens` | 10/min |
| `POST /schema-comparisons/{id}/adopt` | 3/min |
| `GET /managed-databases/{id}/migrations/{v}/select-results` | 20/min |
| `POST /mcp` | 120/min **por token** |

El escalón de 3/min es el de **divulgación**: cada llamada entrega una credencial en claro o un
artefacto con datos del cliente. Bajó desde el default de 100/min, que alcanzaba para vaciar el
llavero entero mientras la auditoría registraba el saqueo sin poder frenarlo.

**El impacto de diseño es concreto: una pantalla que revele credenciales desde una lista muere al
cuarto click.** Si el flujo es "ver la contraseña de cada usuario del motor de este servidor", los
primeros tres funcionan y el cuarto devuelve 429 sin `Retry-After`. Hay que diseñarlo como una
acción deliberada por fila —con confirmación, y sin botón de "revelar todas"— y dejar el error
visible en vez de reintentar solo.

---

## 8. Endpoints desmontados: los siete de `/test` dan 404

Los endpoints de demostración del template ya no se montan. **Los siete responden 404**:

```
GET    /api/v1/test/ping
GET    /api/v1/test/paginated
DELETE /api/v1/test/resource/{id}
PUT    /api/v1/test/custom-error
POST   /api/v1/test/syntax-error
POST   /api/v1/test/upload
POST   /api/v1/test/upload/multiple
```

Ninguno exigía sesión, así que montados dejaban siete rutas sin autenticar en un gateway con
credenciales pseudo-root — dos de ellas escribiendo archivos a disco.

**La SPA no los usa funcionalmente, pero esto sí rompe algo hoy:** cualquier health-check, smoke
test de CI o sonda de monitoreo que apunte a `GET /api/v1/test/ping` está fallando. El reemplazo es
**`GET /health`**, que es público a propósito, no está versionado y no tiene límite de tasa:

```jsonc
// GET /health — 200
{ "status": "ok", "service": "…", "environment": "…" }
```

No usa el envelope `ApiResponse[T]`: es una sonda, no un recurso. Para comprobar que además puede
atender tráfico está `GET /health/ready`, que verifica la base de metadatos y devuelve **503** si
no la alcanza.

---

## 9. Migraciones y MCP: complementos de v23

### 9.1 Los resultados capturados suben de capacidad

`GET /managed-databases/{db_id}/migrations/{version}/select-results` ahora exige
**`blueprints.captures`**, que según v23 §5 solo tiene `owner`.

Es un endpoint de **lectura** que un `operator` deja de poder llamar, y eso es deliberado: devuelve
**datos de negocio** de la base gestionada —la única excepción del gateway a no almacenar datos—,
así que pertenece al eje de **divulgación** y no al de lectura (v23 §2). El `DELETE` de purga del
mismo recurso exige `blueprints.write`.

Consecuencia para la UI: el enlace a los resultados capturados tiene que condicionarse a
`blueprints.captures` en `capabilities` de `/auth/me`, no a `blueprints.read`.

### 9.2 Códigos del MCP que v23 §9.4 no lista

Viven en el vocabulario cerrado del módulo y llegan como error de **tool** (`result` con
`isError: true`), no como error de protocolo — la distinción de v23 §9.2 sigue rigiendo.

| `code` | Qué significa |
|---|---|
| `mcp.too_many_objects` | Se superó el tope de objetos (`MCP_MAX_OBJECTS`, 500). **Corta con error y nunca trunca.** |
| `mcp.environment_unassigned` | La base no tiene entorno. Un agente nunca alcanza una base sin clasificar. |
| `mcp.environment_denies_agents` | El entorno tiene `allows_agent_access` en `false` (§4). |
| `mcp.database_not_opted_in` | Falta el opt-in por base (`agent_access_allowed`). |
| `mcp.database_blocked` | Veto de emergencia activo (`agent_access_blocked`). **Sin override.** |
| `mcp.readonly_credential_missing` | Falta la credencial de solo lectura del servidor. |
| `mcp.reference_not_supported` | Se pasó una referencia cruda (servidor + nombre) en vez de un `database_id`. La v1 no la acepta. |

Los cuatro de política traen además un mensaje propio que dice **qué palanca falta**, para que el
operador que lea el error del agente sepa dónde ir. Los de autorización comparten uno genérico, a
propósito: distinguirlos convertiría los códigos en un oráculo de inventario.

### 9.3 `list_databases` devuelve dos campos más

v23 §9.2 enumera los seis campos por base. La respuesta completa tiene además dos campos de nivel
superior:

```jsonc
{
  "databases": [
    { "database_id": 41, "name": "tienda_42", "engine": "mysql",
      "environment": "production", "blueprint": "retail", "applied_version": "0007" }
  ],
  "count": 1,
  "note": "Vacío significa que ninguna base del proyecto tiene el opt-in de agentes todavía, no que haya fallado la consulta."
}
```

`note` viene **siempre**, no solo con la lista vacía. Y **no se devuelve `server_id`**: ninguna tool
de la v1 lo acepta, y un identificador que nada consume es superficie sin uso.

---

## 10. Lo que este addendum NO puede prometer todavía

Dos cosas están bloqueadas por backend. Conviene saberlas **antes** de planificar las pantallas,
porque las dos se ven construibles desde el contrato y no lo son.

### 10.1 El estado de acceso de agentes no es legible por ninguna vía 🔴

`PUT /managed-databases/{db_id}/agent-access` **escribe** `agent_access_allowed` y
`agent_access_blocked`, y es el opt-in por base del que dependen el §4 y v23 §9.3. Pero esas dos
columnas **no aparecen en ninguna respuesta**:

- no están en `ManagedDatabaseOut`;
- no están en la serialización del controller, así que tampoco llegan por `GET /managed-databases`
  ni por `GET /managed-databases/{id}`;
- **ni siquiera están en la respuesta del propio `PUT`**, que devuelve un `ManagedDatabaseOut` sin
  ellas.

**La pantalla de administración de agentes no se puede construir todavía.** Se puede escribir el
estado, pero no leerlo, ni confirmarlo, ni mostrar qué bases están abiertas, ni renderizar un
toggle con su valor real. Un toggle que no puede leer su propio estado es peor que no tener
toggle: afirma algo que no verificó.

Lo único observable hoy es indirecto y desde el otro lado: `list_databases` del MCP, con un token
del proyecto, muestra las bases que pasaron **las cinco condiciones** del gate (v23 §9.3) — sin
decir cuál de las cinco falló para las demás.

Es un follow-up de backend: agregar los dos campos a `ManagedDatabaseOut` y a la serialización.
Cuando llegue, será un cambio de forma del envelope y aplicará la advertencia de la cabecera.

### 10.2 Un snapshot o un export puede venir incompleto sin que la respuesta lo diga 🔴

Cuando el gateway no tiene privilegio sobre un catálogo del motor —un `42501` de PostgreSQL, un
`1142`/`1227` de MySQL— la consulta de catálogo devuelve vacío, y **un vacío por falta de
privilegio es indistinguible de "no hay objetos"**. Un blueprint o un export pueden salir sin
vistas, sin rutinas, sin triggers o sin secuencias, con **200** y sin ninguna marca.

La señal existe del lado del servidor: cada consulta de catálogo clasifica el resultado como
`ok`, `denied` o `unsupported`. Pero **esa señal solo sale a `logger.warning` y no está en ningún
schema**, así que no llega al frontend por ninguna vía.

Lo mismo pasa con `requires_manual_credentials`: marca un objeto cuyo DDL traía una credencial
embebida (tablas FEDERATED/CONNECT) que el gateway **redactó a `***`**, y por lo tanto **no es
re-aplicable tal cual** — recrearlo exige reponer la contraseña a mano. El flag existe por
sentencia y agregado a nivel del dump, y **tampoco llega a ninguna respuesta HTTP**.

Qué significa para la UI mientras tanto:

- **No se puede mostrar "snapshot completo" ni "export íntegro" como una afirmación del sistema**,
  porque el sistema no la está haciendo. El copy tiene que ser descriptivo ("se exportaron N
  objetos") y no afirmativo.
- **No se puede advertir que un blueprint necesita intervención manual** antes de re-aplicarlo. Un
  flujo de "clonar este blueprint a otro servidor" va a fallar contra el motor sin que nada lo
  haya anticipado.
- El `X-Export-Complete` de la descarga (v23 §7.2) cubre otra cosa: que el **job** no terminó
  bien. No cubre un job que terminó bien sobre un catálogo que el gateway no pudo leer.

Las dos señales existen y están clasificadas; lo que falta es exponerlas. Hasta entonces, ninguna
pantalla debería prometer integridad estructural.
