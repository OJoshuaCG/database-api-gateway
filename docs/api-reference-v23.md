# Contrato de autorización: capacidades, `/auth/me` y el 403

Addendum sobre **toda** la API v1. Antes, un endpoint solo exigía **sesión válida**: quien entraba
podía hacer todo. Ahora **cada endpoint declara una capacidad** de un vocabulario cerrado, y el
servidor la exige.

**No hay cambios incompatibles para el administrador sembrado**: nace con rol `owner` y las dos
capacidades globales, o sea las 29 capacidades. Todo lo que la SPA hace hoy sigue funcionando
igual. Lo que cambia es que ahora **existe** una respuesta 403 donde antes no podía haberla, y
que `/auth/me` publica con qué decidir la UI.

---

## 1. `GET /api/v1/auth/me` — aditivo

Sigue devolviendo `id` y `username`; se agregan campos. **Ojo con el `safeParse` del envelope
completo que hace la SPA de este repo**: una divergencia de un campo descarta la respuesta
entera, así que cada campo nuevo va `.nullish()`, nunca `.optional()`.

```jsonc
{
  "id": 1,
  "username": "admin",
  "role": "owner",                    // rol efectivo = máximo sobre los alcances
  "capabilities": ["databases.read", "..."],   // las EFECTIVAS
  "global_capabilities": ["access_admin", "security_officer"],
  "scope_roles": [{ "scope_type": "environment", "scope_id": 3, "role": "operator" }],
  "step_up_capabilities": ["databases.drop", "..."],
  "catalog_version": "9f2c1a…"        // sha256 corto, para invalidar caché del cliente
}
```

- **`capabilities` no es una lista paralela**: se deriva del MISMO predicado que hace cumplir el
  servidor. Es la única fuente para habilitar o deshabilitar controles.
- **Es una pista de UI. Decide el servidor, siempre.** Ocultar un botón no es autorización.
- **`step_up_capabilities` se publica y todavía NO se exige.** Está para que la UI pueda pedir la
  contraseña *antes* de mandar la operación en vez de descubrirlo por un error; el mecanismo de
  reautenticación es una fase posterior. **No construyas un flujo que dependa de que el servidor
  rechace por falta de step-up: hoy no lo hace.**

## 2. `GET /api/v1/authz/catalog` — el vocabulario completo

Detrás de `self.read`, o sea cualquier sesión. Devuelve una fila por capacidad con los metadatos
que la UI necesita para una pantalla de administración de accesos:

| Campo | Para qué sirve |
|---|---|
| `id`, `module`, `level` | `modulo.nivel`, p. ej. `databases.drop` |
| `label` | etiqueta en español, lista para mostrar |
| `mutates` | si cambia estado |
| `discloses` | **si expone datos o credenciales** — es un eje INDEPENDIENTE de `mutates` |
| `requires_step_up` | si va a pedir reautenticación (ver la advertencia de arriba) |
| `agent_allowed` | techo de lo que puede vivir en un token del servidor MCP |
| `scope_axis` | `global` \| `environment` \| `server` |
| `roles`, `global_capabilities` | quién la tiene |

`catalog_version` de `/auth/me` cambia cuando cambia el catálogo: úsalo como clave de caché.

**Los dos ejes son independientes y eso importa para la UI.** `exports.download`,
`engine_users.secrets` y `blueprints.captures` **no destruyen nada** pero divulgan: una pantalla
que agrupe por "peligrosidad" mirando solo `mutates` las va a pintar como inofensivas.

## 3. El 403

```jsonc
{ "detail": { "msg": "No tienes permiso para esta operación.",
              "type": "AppHttpException",
              "public_context": { "code": "access.forbidden" } } }
```

**El código es cerrado y NO nombra la capacidad que falta**, a propósito: un mensaje como "falta
`servers.admin`" le da a un atacante un mapa de la superficie por fuerza bruta de 403. No intentes
parsear qué faltó — usá `capabilities` de `/auth/me` para no llegar hasta acá.

## 4. Rutas donde un PARÁMETRO sube el requisito

Cinco endpoints piden **más** capacidad según el payload. Son los que la UI tiene que reflejar
deshabilitando el control, porque el usuario ya está en la pantalla y el 403 llega recién al
enviar:

| Endpoint | Con este parámetro | Pide además |
|---|---|---|
| `POST /database-models/from-snapshot` | `data_tables` (datos-semilla) | `blueprints.captures` |
| `POST /database-models/{id}/migrations` | `capture_selects: true` | `blueprints.captures` |
| `PATCH /database-models/{id}/migrations/{v}` | `capture_selects: true` | `blueprints.captures` |
| `DELETE /managed-databases/{id}` | `drop_remote=true` | `databases.drop` |
| `DELETE /server-users/{id}` | `drop_remote=true` | `engine_users.drop` |

Y dos que exigen dos capacidades **siempre**, porque crean una versión de blueprint desde otro
módulo: `POST /schema-comparisons/{id}/adopt` y
`POST /database-models/{id}/collation-conversions/{batch}/blueprint-version` piden
`blueprints.write` además de la propia.

En los tres primeros, **apagar** la captura no pide nada extra: solo encenderla.

## 5. Módulos, niveles y quién los tiene

| Módulo | Niveles | `viewer` | `operator` | `owner` | Solo global |
|---|---|:--:|:--:|:--:|---|
| `self` | `read` | ✅ | ✅ | ✅ | |
| `servers` | `read` | ✅ | ✅ | ✅ | |
| | `admin` | | | | `security_officer` |
| `engine_users` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | ✅ | ✅ | |
| | `drop` | | | ✅ | |
| | `secrets` 🔓 | | | ✅ | |
| `databases` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | ✅ | ✅ | |
| | `drop` | | | ✅ | |
| `blueprints` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | ✅ | ✅ | |
| | `apply` | | | ✅ | |
| | `captures` 🔓 | | | ✅ | |
| `schema_diff` | `read` | ✅ | ✅ | ✅ | |
| | `execute` | | | ✅ | |
| `clones` | `read` | ✅ | ✅ | ✅ | |
| | `execute` 🔓 | | | ✅ | |
| `collation` | `read` | ✅ | ✅ | ✅ | |
| | `execute` | | ✅ | ✅ | |
| `exports` | `read` | ✅ | ✅ | ✅ | |
| | `execute` | | ✅ | ✅ | |
| | `download` 🔓 | | | ✅ | |
| `sql_console` | `history` | ✅ | ✅ | ✅ | |
| | `execute` 🔓 | | | ✅ | |
| `catalogs` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | | | `security_officer` |
| `environments` | `read` | ✅ | ✅ | ✅ | |
| `gateway` | `admin` | | | | `access_admin`, `security_officer` |

🔓 = **divulga** (`discloses: true`).

**Tres asignaciones que sorprenden y son deliberadas:**

- **`servers.admin` no lo tiene `owner`.** Editar un servidor puede **re-apuntar un `server_id` a
  otro host**, y con eso se redirige cada operación futura de todo operador. Es política de
  infraestructura, no operación.
- **`catalogs.write` y los mutantes de `/environments` tampoco.** `privileges.is_active` decide
  qué se puede otorgar y `blocks_destructive_migrations` decide si una migración destructiva
  corre: **toda fila que un guard lee es una frontera de privilegio**, así que su escritor
  necesita al menos el privilegio del guard que puede apagar.
- **`clones.execute` está en `owner` y no en `operator`** aunque su nombre lo emparente con
  `collation.execute`: un clon **copia DATOS**, y meter la base de producción de un cliente en un
  entorno de desarrollo es divulgación.

## 6. Lo que este addendum NO trae todavía

- **Alcance por destino.** `scope_roles` se publica, pero el chequeo por entorno o por servidor de
  cada objeto concreto es una fase posterior: hoy la capacidad se evalúa global. **No presentes en
  la UI un alcance que el servidor todavía no aplica.**
- **Step-up.** Ver §1.
- **Administración de usuarios.** `/gateway-users` y `/api-tokens` no existen aún; el único usuario
  es el administrador sembrado. Su rol se cambia hoy en la BD.

---

## 7. Cambios de autenticación que la SPA tiene que absorber

Entregados junto con lo de arriba. **Tres rompen el cliente actual si no se adaptan.**

### 7.1 CSRF: header obligatorio en todo método no seguro 🔴

```jsonc
// 403 si falta o no valida
{ "detail": { "msg": "…", "public_context": { "code": "auth.csrf_missing" } } }
{ "detail": { "msg": "…", "public_context": { "code": "auth.csrf_invalid" } } }
{ "detail": { "msg": "…", "public_context": { "code": "auth.origin_rejected" } } }
```

Qué hacer: leer la cookie **`__Host-gw_csrf`** (o `gw_csrf` sin TLS) y mandarla en el header
**`X-CSRF-Token`** en todo `POST`/`PATCH`/`DELETE`. La cookie **no** es httpOnly justamente para
eso.

Dos cosas que no son obvias:

- **El token ROTA con la sesión.** Se deriva del identificador de sesión, y ese identificador
  cambia en cada login. Un token cacheado en memoria de un login anterior da `auth.csrf_invalid`:
  hay que releer la cookie después de cada login.
- **No es double-submit.** El servidor lo recomputa; plantar la cookie no sirve. No intentes
  "arreglar" un 403 seteando la cookie desde el JS.

### 7.2 La descarga de exportaciones es de DOS pasos 🔴

`GET /database-exports/{id}/download` ahora exige `?ticket=`:

```
POST /api/v1/database-exports/{id}/download-ticket   → { ticket, expires_at, filename }
GET  /api/v1/database-exports/{id}/download?ticket=…
```

El ticket **vence en 60 segundos**, así que se pide en el momento del click y no al cargar la
pantalla. El POST corre los mismos guards que la descarga, o sea que un 409/410 llega ahí y no en
el GET.

Y `GET /database-exports/{id}/content` —la entrega en línea para el portapapeles— **exige el
header `X-CSRF-Token` aunque sea un GET**. Los dos endpoints consumen el artefacto, y una
navegación GET lleva la cookie: sin esto, un `<img>` en cualquier página destruía el export.

### 7.3 La sesión ahora vence de verdad 🔴

Dos vencimientos nuevos, y el 401 dice cuál fue:

| `public_context.code` | Qué pasó | Qué mostrar |
|---|---|---|
| `auth.session_absolute` | 12 h desde el login, **haya habido actividad o no** | "la sesión alcanzó su duración máxima" |
| `auth.session_idle` | 60 min sin requests | "expiró por inactividad" |
| `auth.session_logout` | se cerró en otra pestaña | "la sesión se cerró" |
| `auth.session_password_change` · `auth.session_role_change` · `auth.session_admin_revoked` | revocada | el motivo correspondiente |
| `auth.session_unknown` · `auth.session_missing` | no hay sesión | login normal |

**El absoluto es el que va a sorprender**: antes la sesión no expiraba nunca mientras hubiera
actividad. Una SPA que asuma "si el usuario está usando la app, la sesión sigue viva" va a tirar
al login a mitad de una operación. Conviene avisar antes de que llegue.

### 7.4 Aditivo, sin romper nada

- `GET /auth/sessions` — las sesiones vivas del propio usuario (`sid_prefix`, `current`,
  `created_at`, `last_seen_at`, `ip`). **Nunca el identificador completo**: es la credencial de
  sesión.
- `POST /auth/sessions/revoke-others` — cierra las demás y conserva la actual.
- `/auth/me` gana `previous_login_at` y `last_failed_at`. Es el **anterior** al actual a
  propósito: cuando la SPA pide `/auth/me`, el último ya es el login en curso.
- El límite de tasa pasa a contarse **por sesión** y no por IP, así que varias personas detrás de
  la misma salida NAT ya no comparten cupo. El login sigue por IP: todavía no hay sesión.

---

## 8. Alcance por destino (capa 2)

`GET /authz/scope-readiness` (detrás de `gateway.admin`) — **se pide ANTES de otorgar el primer
acceso por alcance.**

```jsonc
{ "total_databases": 42, "unclassified_databases": 7, "ready": false,
  "fallback_environment_slug": "production",
  "servers": [ { "server_id": 3, "server_name": "…", "engine": "mysql",
                 "databases": 12, "unclassified": 7,
                 "derived_environment_slug": "production", "derived_from_gap": true } ] }
```

**Por qué existe**: una BD sin `environment_id` **no** resuelve al entorno por defecto —ése es el
más permisivo— sino al **más protegido**. Así que otorgar "lector en producción" también le saca a
esa persona el acceso a toda base que nadie clasificó. `derived_from_gap: true` marca las filas que
hay que arreglar; `ready: true` dice que se puede otorgar sin sorpresas.

**Qué cambia en la superficie**: las operaciones con destino resoluble (por ahora el borrado, el
aprovisionamiento y el apply/rollback de `/managed-databases/{id}/*`) evalúan la capacidad **dos
veces**: una global —"¿podría en algún alcance?"— y otra en el destino. El 403 es el mismo
`access.forbidden` en los dos casos: **no distingue cuál de las dos capas negó**, porque decir "no
la tenés *acá*" le regala a un atacante el mapa de sus propios alcances por fuerza bruta.

Mientras nadie tenga grants por alcance —el estado de un despliegue recién migrado— la capa 2 no
cambia ningún resultado, y no toca la BD para decidirlo.

---

## 9. Tokens de agente y el servidor MCP (v1 parcial)

### 9.1 `/api-tokens` — detrás de `gateway.admin`

`POST` devuelve el bearer **una sola vez** (`dbgw.<id>.<secreto>`). Lo que persiste es su HMAC, así
que **no hay forma de volver a mostrarlo**: si se pierde, se emite otro.

| Regla | Por qué |
|---|---|
| `project_id` obligatorio | Un token sin proyecto no alcanza ninguna base, así que lo único que un nulo podría significar es "token global" |
| `expires_in_days` ≤ 90 | Sin tokens perpetuos: un token de agente vive en el `.mcp.json` del repo de otra gente |
| `scopes` dentro del techo de agente | Un token **no puede** recibir una capacidad que mute o divulgue, ni por error del operador |
| `DELETE` no se deshace | Un token que alguien creyó muerto y no lo está es peor que emitir uno nuevo. Revocar dos veces da 409 |

**Distribución**: en el `.mcp.json` del repo consumidor va por **expansión de variable de
entorno**, nunca el literal. El gate de secretos del CI protege *este* repo; el token se commitea
en los repos de otra gente, así que ahí hace falta su propia regla de escaneo.

### 9.2 `POST /mcp` — JSON-RPC 2.0, bearer, DOS eras del protocolo

**Nace apagado** (`MCP_ENABLED=false`). Apagado, todo request recibe 503 `mcp.disabled`, incluido
uno con token válido y uno con token basura — así que apagado tampoco es un oráculo.

Funciona con `/mcp` **y** `/mcp/`, sin redirección. Es deliberado: la URL natural de un
`.mcp.json` es `https://host/mcp`, y un `307` obliga al cliente a reintentar el POST — hay
clientes HTTP que **no reenvían `Authorization`** en el salto, y el síntoma sería "el token no
funciona".

#### Configuración en Claude Code

```bash
claude mcp add --transport http gateway https://host/mcp \
  --header "Authorization: Bearer $GATEWAY_MCP_TOKEN"
```

```jsonc
// .mcp.json del repo consumidor
{ "mcpServers": { "gateway": {
    "type": "http",
    "url": "https://host/mcp",
    // Por VARIABLE DE ENTORNO, nunca el literal: el gate de secretos del CI protege el repo
    // del gateway, no el de quien consume.
    "headers": { "Authorization": "Bearer ${GATEWAY_MCP_TOKEN}" }
} } }
```

#### Las dos eras, y por qué se soportan las dos

La revisión **2026-07-28** rehízo el transporte: **no hay handshake** (`initialize` y
`notifications/initialized` desaparecieron), no hay sesiones de protocolo (`Mcp-Session-Id` se
retiró) y no hay stream por GET. Cada request trae su contexto en `params._meta`.

El servidor **detecta la era por la presencia del header `MCP-Protocol-Version`** —lo que la
propia spec autoriza— y habla las dos, porque el parque real tiene las dos:

| | Era stateless (`2026-07-28`, `2025-11-25`) | Era del handshake (`2025-06-18` y anteriores) |
|---|---|---|
| Handshake | **no existe**: `initialize` da 404 | `initialize` → `notifications/initialized` |
| Headers | `MCP-Protocol-Version`, `Mcp-Method`, y `Mcp-Name` en `tools/call` — **obligatorios y validados contra el cuerpo** | ninguno |
| `params._meta` | `protocolVersion` y `clientCapabilities` **obligatorios** | no se pide |
| Sesión | ninguna | ninguna |

#### Métodos

`tools/list`, `tools/call`, `ping`, y `initialize` **solo** en la era del handshake.

**La v1 tiene UNA tool: `list_databases`**, que lee el inventario y **no abre ninguna conexión a
los motores**. Devuelve por base: `database_id`, `name`, `engine`, `environment`, `blueprint`,
`applied_version`. Faltan `list_objects`, `get_schema` y `check_freshness`.

#### Status HTTP: es parte del contrato, no un detalle

| Caso | Status | Cuerpo |
|---|---|---|
| request atendido | `200` | `result` con `resultType: "complete"` y `_meta.serverInfo` |
| **notificación** (sin `id`) | `202` | **vacío** — JSON-RPC prohíbe responderla |
| método desconocido | `404` | `-32601` |
| headers que no calzan con el cuerpo | `400` | `-32020` `HeaderMismatch` |
| versión no soportada | `400` | `-32022`, con `data.supported` |
| falta un `_meta` obligatorio | `400` | `-32602` |
| `Origin` presente y ajeno | `403` | — |
| `GET` o `DELETE` al endpoint | `405` | — |
| cuerpo > 256 KiB | `413` | — |
| credencial inválida | `401` | `mcp.token_invalid`, **un solo código opaco** |

**`Origin` ausente NO rechaza**: un cliente que no es un navegador no lo manda y no está sujeto a
DNS rebinding. Se rechaza un `Origin` **presente y ajeno**, que es la señal positiva.

#### Errores: protocolo vs. tool

| Dónde | Cuándo |
|---|---|
| campo `error` de JSON-RPC | el mensaje está mal formado o pide algo que no existe |
| `result` con `isError: true` | la operación se entendió y se negó (`mcp.scope_denied`, `mcp.not_found`, …) |

Mezclarlos rompe el cliente de dos maneras: un error de tool en `error` hace que el agente crea
que el servidor está roto y **reintente**; uno de protocolo en `result` hace que lo muestre como
contenido.

### 9.3 El gate: niega por default, y hay que abrirlo base por base

Para que una base aparezca hacen falta **las cinco**:

1. pertenece al **proyecto del token**;
2. **su blueprint pertenece a un solo proyecto** — un blueprint compartido deja la base fuera del
   alcance de *todos* los agentes;
3. tiene `environment_id` (una base sin clasificar nunca es alcanzable);
4. su entorno tiene `allows_agent_access = true`;
5. la base tiene `agent_access_allowed = true` y `agent_access_blocked = false`.

La condición 2 no estaba y era una **fuga cross-tenant**: `managed_databases` no tiene
`project_id`, así que el alcance se resolvía por el pivote de blueprints — que es N:M por diseño —
y un token de un cliente veía nombres, entorno y versión de las bases de otro.

**Cómo se abre** (todo detrás de `gateway.admin`):

```
PATCH /environments/{id}   {"allows_agent_access": true}   + ?confirm_slug=<slug>
PUT   /managed-databases/{id}/agent-access   {"allowed": true, "blocked": false}
```

Encender el flag del entorno **exige `confirm_slug`**: habilita una superficie de lectura nueva
sobre bases de terceros, así que cuenta como debilitamiento de la política igual que apagar el
bloqueo de destructivas. Y abrir una base se audita **fail-closed**: si el rastro no se puede
persistir, la apertura no ocurre.

**Las tres columnas nacen en `false`**, y la asimetría está en el DDL. **El opt-in por base es el
eje que decide el alcance, no el veto**: con solo un opt-out, habilitar un entorno abriría de golpe
todas sus bases — incluidas las que nadie revisó. `agent_access_blocked` es el veto de emergencia
y **no tiene override**: ni `force`, ni nada.

**El listado nunca enumera lo negado.** Una base de otro proyecto simplemente no está.

### 9.4 Topes

| Tope | Default | Variable |
|---|---|---|
| objetos por respuesta, **antes** de consultar | 500 | `MCP_MAX_OBJECTS` |
| bytes de la respuesta, después de serializar | 512 KiB | — |
| cuerpo del request | 256 KiB | `MCP_MAX_BODY_KIB` |
| tasa, **por token** | 120/min | `MCP_RATE_LIMIT` |

Los topes de tamaño **cortan con un error y nunca truncan**: una lista cortada le haría creer al
agente que no hay más, y un JSON cortado que parsea a medias le haría creer que el esquema es más
chico de lo que es. El límite de tasa es **por token y no por IP** porque un agente en CI comparte
IP con todos los demás jobs.

### 9.5 Lo que el MCP nunca va a hacer

**No acepta SQL del agente, en ninguna versión.** No es prudencia genérica: `sqlglot` no tokeniza
los comentarios ejecutables `/*!` de MySQL ni `/*M!` de MariaDB, así que todo guard por AST sobre
SQL arbitrario es **evadible** — fue una vulnerabilidad real de la consola SQL de este repo,
corregida en dos rondas. Cuando haga falta ver datos, la vía son tools **parametrizados**.

