# Autorización del gateway: roles, capacidades y controles

Resumen **vigente** del modelo de autorización. Reúne en un solo lugar lo que está repartido entre
[`authentication.md`](authentication.md), los addenda `api-reference-v23.md`, `v24.md` y `v29.md` y
la §19 de [`api-reference.md`](../api-reference.md). Los contratos completos (forma de cada
respuesta, cada código de error) siguen en esos addenda; acá se explica **cómo encajan** y
**por qué**.

> **Fuente de verdad: el código.** La tabla de capacidades sale de
> `app/services/capability_catalog.py` (`capability_matrix()`, la misma estructura que publica
> `GET /authz/catalog`). Si este documento y el catálogo difieren, manda el catálogo, y el
> documento tiene que corregirse en el mismo cambio.
>
> **Ojo con el plano.** Todo esto son capacidades **del gateway** (`Capability`, `GatewayRole`).
> Los privilegios **del motor** (`Privilege`, `PermissionProfile`, `GRANT`) son otro plano y otro
> vocabulario: ver [permissions.md](permissions.md).

Módulos: `app/services/capability_catalog.py`, `app/core/{authz,scope,step_up,separation_of_duties,assignment_policy,auth,limiter,session_store,denial_audit}.py`,
`app/services/{bootstrap_window,sod_service,audit}.py`, `app/controllers/{gateway_user,capability_grant,access_request,audit_log}_controller.py`,
`scripts/check_route_capabilities.py`.

---

## 1. Roles, globales y las 32 capacidades

Cada usuario del gateway tiene:

- un **rol base** (`gateway_role`): `viewer` ⊆ `operator` ⊆ `owner`, en cadena monótona;
- **roles por alcance** (`access_grants`): un rol sobre un entorno o un servidor, que **reemplaza**
  al rol base en ese alcance (no se suma; ver §2);
- **capacidades globales** (`access_admin`, `security_officer`): funciones ortogonales a la cadena
  de roles;
- **capacidades puntuales** (`capability_grants`): una capacidad suelta sobre un entorno o
  servidor, que se **suma** a lo que da el rol (ver §3).

Los dos ejes de riesgo son independientes: `mutates` (cambia estado) y `discloses` (saca datos o
credenciales del tercero del perímetro). `destructive` es un **subconjunto** de `mutates`: destruye
o cambia de forma irreversible datos o estructura del tercero.

### 1.1 Tabla del catálogo

Generada desde `capability_matrix()` (32 filas). `✓` = sí, `—` = no. *Cursiva* = capacidad global.
"Sensible" = otorgarla suelta pide un segundo aprobador (§3).

| Capacidad | Módulo | Nivel | Eje | mutates | discloses | destructive | step-up | agente | otorgable | sensible | Roles / global |
|---|---|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|---|
| `self.read` | self | read | global | — | — | — | — | — | — | — | viewer, operator, owner |
| `servers.read` | servers | read | server | — | — | — | — | — | ✓ | — | viewer, operator, owner |
| `servers.admin` | servers | admin | global | ✓ | — | — | ✓ | — | — | — | *security_officer* |
| `engine_users.read` | engine_users | read | server | — | — | — | — | — | ✓ | — | viewer, operator, owner |
| `engine_users.write` | engine_users | write | server | ✓ | — | — | — | — | ✓ | — | operator, owner |
| `engine_users.drop` | engine_users | drop | server | ✓ | — | ✓ | ✓ | — | ✓ | ✓ | owner |
| `engine_users.secrets` | engine_users | secrets | server | — | ✓ | — | ✓ | — | ✓ | ✓ | owner |
| `engine_users.credentials` | engine_users | credentials | server | ✓ | ✓ | — | ✓ | — | ✓ | ✓ | owner |
| `databases.read` | databases | read | environment | — | — | — | — | ✓ | ✓ | — | viewer, operator, owner |
| `databases.write` | databases | write | environment | ✓ | — | — | — | — | ✓ | — | operator, owner |
| `databases.drop` | databases | drop | environment | ✓ | — | ✓ | ✓ | — | ✓ | ✓ | owner |
| `blueprints.read` | blueprints | read | environment | — | — | — | — | ✓ | ✓ | — | viewer, operator, owner |
| `blueprints.write` | blueprints | write | environment | ✓ | — | — | — | — | ✓ | — | operator, owner |
| `blueprints.apply` | blueprints | apply | environment | ✓ | — | ✓ | ✓ | — | ✓ | ✓ | owner |
| `blueprints.captures` | blueprints | captures | environment | — | ✓ | — | ✓ | — | ✓ | ✓ | owner |
| `schema_diff.read` | schema_diff | read | environment | — | — | — | — | ✓ | ✓ | — | viewer, operator, owner |
| `schema_diff.execute` | schema_diff | execute | environment | ✓ | — | ✓ | ✓ | — | ✓ | ✓ | owner |
| `clones.read` | clones | read | environment | — | — | — | — | — | ✓ | — | viewer, operator, owner |
| `clones.execute` | clones | execute | environment | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | owner |
| `collation.read` | collation | read | environment | — | — | — | — | — | ✓ | — | viewer, operator, owner |
| `collation.execute` | collation | execute | environment | ✓ | — | ✓ | ✓ | — | ✓ | ✓ | owner |
| `exports.read` | exports | read | environment | — | — | — | — | — | ✓ | — | viewer, operator, owner |
| `exports.execute` | exports | execute | environment | ✓ | — | — | — | — | ✓ | — | operator, owner |
| `exports.download` | exports | download | environment | — | ✓ | — | ✓ | — | ✓ | ✓ | owner |
| `sql_console.history` | sql_console | history | environment | — | — | — | — | — | ✓ | — | viewer, operator, owner |
| `sql_console.execute` | sql_console | execute | environment | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | owner |
| `catalogs.read` | catalogs | read | global | — | — | — | — | — | — | — | viewer, operator, owner |
| `catalogs.write` | catalogs | write | global | ✓ | — | — | ✓ | — | — | — | *security_officer* |
| `environments.read` | environments | read | global | — | — | — | — | — | — | — | viewer, operator, owner |
| `environments.write` | environments | write | global | ✓ | — | — | ✓ | — | — | — | *security_officer* |
| `access.admin` | access | admin | global | ✓ | — | — | ✓ | — | — | — | *access_admin* |
| `policy.admin` | policy | admin | global | ✓ | — | — | ✓ | — | — | — | *security_officer* |

Conteos: 16 con step-up, 7 destructivas, 24 otorgables, 11 sensibles, 3 permitidas a agentes.

**Qué tiene cada rol**, en palabras:

- `viewer`: todas las lecturas. No muta ni divulga (invariante 3 del catálogo).
- `operator`: `viewer` + la escritura **no destructiva y no divulgante**: `engine_users.write`,
  `databases.write`, `blueprints.write` (solo autoría) y `exports.execute`. No divulga nada
  (invariante 7b).
- `owner`: todo lo **operativo** del alcance: `operator` + las 11 capacidades exclusivas de `owner`
  (las *sensibles* de la tabla). No incluye ninguna capacidad global.

`self.read`, `catalogs.read` y `environments.read` son globales pero de la cadena de roles: las
tiene todo usuario.

### 1.2 Las dos globales: disjuntas y con deberes distintos

| Global | Capacidades | Deber |
|---|---|---|
| `access_admin` | `access.admin` (exactamente esa, invariante 10) | Administra usuarios del gateway, sus accesos, capacidades puntuales, tokens de agente, sesiones de otras personas, `scope-readiness` y `sod-report`. **Explícitamente no operativo**: no aplica migraciones, no dropea, no revela contraseñas. |
| `security_officer` | `policy.admin`, `servers.admin`, `catalogs.write`, `environments.write` | Escribe **datos de política**: el inventario de servidores (host y credencial), los catálogos de privilegios/perfiles/charsets, los entornos y su política (incluida la apertura de una BD a agentes y la reclasificación), la rotación del cifrado y la **lectura de la auditoría**. No administra usuarios. |

Los conjuntos son **disjuntos de a pares** (invariante 9) y ninguna global está en la cadena de
roles (invariante 7). `gateway.admin`, la capacidad que tenían las dos, está **retirada**
(`RETIRED_CAPABILITIES`, invariante 12) y `scripts/check_route_capabilities.py` rechaza cualquier
ruta que la declare.

La regla que justifica a `security_officer`: **toda fila que un guard lee es una frontera de
privilegio**, así que su escritor necesita al menos el privilegio del guard que puede apagar. Sin
`security_officer` asignado, esas escrituras quedan bloqueadas a propósito; no hay fallback a
`access.admin`.

### 1.3 Invariantes que el catálogo afirma al importar

Fallar al importar es fallar al arrancar: un cambio de catálogo que rompa estas reglas no despliega.
Las relevantes para entender el modelo: monotonía de roles (2), `viewer` no muta ni divulga (3),
todo lo que divulga pide step-up (4), el techo de agente no muta ni divulga (5), destructiva ⇒
`mutates` + step-up + solo `owner` (7c), el conjunto sensible es exactamente `owner − operator`
otorgable (8), globales disjuntas (9), `access_admin = {access.admin}` (10), step-up ⇒ no agente
(11) y `gateway.admin` no vuelve (12). Están en `_assert_invariants()`.

---

## 2. Las dos capas: unión y destino

Cada endpoint declara su capacidad **como parámetro** (`require(cap)` o `require_at(cap, target=…)`
en `app/core/authz.py`). No hay dependencia que solo verifique sesión: `AdminDep` se retiró y
`scripts/check_route_capabilities.py` exige que toda ruta declare una capacidad del catálogo.

**Capa 1 — "¿podría, en algún alcance?"** (`require`). Usa el rol **unión**: el máximo sobre el rol
base y los roles por alcance, más las globales y las capacidades puntuales activas. Es más laxa que
la política real a propósito: con el mínimo, "lector en producción" degradaría también el trabajo
en desarrollo.

**Capa 2 — "¿puede en ESTE destino?"** (`require_at` + `app/core/scope.py`). Solo es aceptable que
la capa 1 sea laxa porque la capa 2 no es salteable:

- **Toda ruta con destino declara `require_at`.** `SCOPE_PENDING` está vacío y el chequeo 6 del
  script es estricto: una ruta con capacidad de alcance (eje `environment`/`server` y por encima del
  piso `viewer`) sin destino declarado rompe CI.
- **El rol por alcance reemplaza al rol base en su alcance.** Sin rol aplicable manda el rol
  **base**, nunca la unión. Con dos aplicables (uno por entorno, otro por servidor) manda el **más
  restrictivo**. Un rol por alcance ilegible vale como `viewer` en ese alcance.
- **Una BD sin entorno resuelve al entorno más protegido** (el de `rank` máximo), nunca al default,
  que es el más permisivo. Un destino a nivel servidor resuelve al peor entorno entre sus bases, y
  cualquier base sin clasificar lo lleva al más protegido. Por eso existe
  `GET /authz/scope-readiness`: se clasifica primero y se otorga después.
- **Jobs de doble extremo** (clon, export, collation, diff) exigen la capacidad en los dos extremos.
- **Operaciones sobre todo un blueprint** (renombrar slug, migrar la tabla de versión, borrar
  versiones) se evalúan contra el entorno más protegido entre sus bases; sin bases, contra el rol
  base.
- **Lotes implícitos** (`apply-all`, lotes de collation y de clonado) **omiten** las bases sin
  permiso y las devuelven con su id y `access.forbidden`; con ids explícitos, o si no queda ninguna
  permitida, responden `403`.
- El `403 access.forbidden` es el mismo en las dos capas y **no nombra la capacidad que falta**:
  distinguirlos le daría a un atacante el mapa de sus propios alcances.

**Rutas exentas de la capa 2** (`SCOPE_EXEMPT`, 11 entradas, cada una con su motivo en el script):
crear, editar y borrar un blueprint; escribir, editar y previsualizar una versión; y el CRUD de
proyectos y de su relación con blueprints. Todas son **autoría** sobre la BD de metadatos del
gateway: no apuntan a ninguna BD del tercero a la que anclar el alcance. Borrar un blueprint exige
`blueprints.apply` y responde `409` si alguna BD lo referencia, así que cuando procede no queda
ninguna BD a la que anclarlo.

**Orden de los chequeos:** autenticación → CSRF (solo sesión por cookie) → capa 1 → capa 2 →
step-up (§6). Nadie confirma su contraseña para enterarse después de que igual no podía.

---

## 3. Capacidades puntuales

Una capacidad puntual le suma a una persona **una** capacidad sobre **un** entorno o servidor, sin
cambiarle el rol. Contrato: [`api-reference.md` §19](../api-reference.md#19-capacidades-puntuales-gateway-usersidcapability-grants).

- Solo se otorgan las de eje `environment`/`server` (24). Las globales no se otorgan nunca.
- Escribir o ejecutar **trae implícita la lectura** de su módulo (`IMPLIED_READ`; p. ej.
  `sql_console.execute` → `sql_console.history`). La lectura implícita es siempre de nivel `viewer`.
- **Las 11 sensibles** (todo lo exclusivo de `owner`: `engine_users.drop`, `engine_users.secrets`,
  `engine_users.credentials`, `databases.drop`, `blueprints.apply`, `blueprints.captures`,
  `schema_diff.execute`, `clones.execute`, `collation.execute`, `exports.download`,
  `sql_console.execute`) nacen `pending` y no surten efecto hasta que **otro** `access_admin` las
  apruebe en `/capability-grants`. El resto nace `active`.
- Una solicitud pendiente **vence a los 7 días** (`PENDING_TTL`). El vencimiento es **perezoso**: se
  evalúa en cada lectura o decisión y una vez al arrancar, y se audita con `actor_type="system"`.
- Revocar lo hace un solo `access_admin`, con efecto inmediato (activa → `revoked`, pendiente →
  `cancelled`). Las filas no se borran.
- Nadie se otorga ni se revoca capacidades a sí mismo (`access.self_modification_forbidden`).
- **Las capacidades activas no vencen.** Solo vence la solicitud pendiente.

Lo que se le quitó al rol `operator` (operaciones de blueprint sobre toda la flota, `collation.execute`,
elegir la credencial de una cuenta del motor) **se otorga puntualmente** a quien lo necesite.

---

## 4. Política de asignación y elevaciones (`access_change_requests`)

**Quién asigna.** `ASSIGNABLE_BY` (código, no tabla): `access_admin` asigna cualquier rol, cualquier
global y cualquier capacidad otorgable, **tenga o no** lo que asigna. Reemplazó al techo por
tenencia ("no das más de lo que tenés"; `access.grant_ceiling_exceeded`, retirado), que obligaba a
quien administra accesos a tener cada deber que reparte.

**Qué es una elevación** (`needs_second_approver`): el rol `owner` (base o por alcance), **cualquier**
global, una capacidad puntual sensible o un `sod_override`. `operator` no es elevación.

**Cómo se parte un cambio** (`app/core/assignment_policy.py`). `POST /gateway-users`,
`PATCH /gateway-users/{id}` (rol) y `PUT /gateway-users/{id}/access`:

- aplican **en el acto** todo lo que no eleva, y **siempre las bajas**: retirar acceso no espera a
  nadie;
- crean una solicitud pendiente por la parte que eleva y responden **`202 access.elevation_pending`**
  con la solicitud. `POST /gateway-users` crea la cuenta sin la parte elevada y devuelve igual el
  token de invitación;
- una solicitud nueva sobre la misma persona **reemplaza** a la pendiente anterior (`superseded`).

**Reglas de aprobación** (`/access-requests`, todo `access.admin`; contrato en
`api-reference-v29.md` §9.3–§9.4):

- aprueba **otro** `access_admin`: ni quien pidió (`access.self_approval_forbidden`) ni la persona
  destino (`access.self_modification_forbidden`);
- quien pidió tiene que seguir siendo `access_admin` activo; si no, la solicitud se cancela;
- si el acceso de la persona cambió desde el pedido (`before_hash`), la solicitud se cancela y se
  responde `409 access.request_stale`;
- se re-chequea la separación de deberes (§5) sobre el estado final;
- la decisión es compare-and-set y se aplica **dentro** de la transacción que protege al último
  `access_admin`; al aplicar se cierran las sesiones de la persona;
- vencen a los 7 días, con el mismo vencimiento perezoso;
- solo quien pidió puede **cancelar** (`POST /access-requests/{id}/cancel`); los demás rechazan.

**Último `access_admin`.** No se puede desactivar ni quitarle la global al último `access_admin`
activo con credencial: `409 access.last_admin_protected`. El candado corre dentro de la transacción
de escritura, no solo como pre-chequeo.

---

## 5. Separación de deberes

Regla pura en `app/core/separation_of_duties.py`; escritor y reportes en `app/services/sod_service.py`.
Contrato en `api-reference-v29.md` §8.

**Las dos reglas.** Una cuenta con `security_officer` no puede tener además:

1. `owner` en ninguna forma (`owner_security_officer`): rol base, rol `owner` por alcance o una
   capacidad puntual exclusiva de `owner`. Quien opera producción no puede ser quien apaga su
   barrera.
2. `access_admin` (`access_admin_security_officer`): las dos globales juntas reconstruyen el
   administrador combinado que la partición de `gateway.admin` deshizo.

**Al escribir.** Crear o editar un usuario, guardar accesos, otorgar o aprobar una capacidad puntual
cuyo estado **resultante** viole una regla sin excepción viva responde `409 access.sod_conflict`, con
`rules` y `conflicts` (qué fuente choca con qué).

**Break-glass: `sod_override`.** El payload acepta `sod_override: {reason, …}` con un motivo de al
menos 20 caracteres y una vigencia de hasta 7 días (`OVERRIDE_MAX_HOURS`). Es una **elevación**: viaja
en la solicitud pendiente y se aplica al aprobarla, escribiendo una fila en `sod_exceptions` con
`approved_by`. Se audita `access.sod_override`. Un override mal formado es `422
access.sod_override_invalid`.

**Herencia (grandfathering).** Las instalaciones que ya existían conservan su cuenta combinada
(`owner` + `access_admin` + `security_officer`): la migración `f8b0d2e4a6c9` le escribió una
excepción `reason='grandfathered'` sin vencimiento. **No se parte sola**: partirla podía dejar la
instalación sin `security_officer` o sin `owner`, y la persona no podría arreglarlo porque nadie se
modifica a sí mismo. El arranque la reporta (`access.sod_grandfathered`).

**Al leer (defensa en profundidad).** `parse_access_context` descarta las capacidades de
`security_officer` si la combinación está presente sin excepción viva (una fila editada a mano, una
excepción vencida) y deja rastro (`access.denied`, `check="sod"`). Conserva `owner` y `access_admin`:
quitar `access_admin` podría dejar al gateway sin nadie que lo repare; lo que se cae es la política.

**Dónde se ve.** `/auth/me.sod_warnings` (`grandfathered` | `override` | `neutralized`) para la propia
cuenta, y `GET /authz/sod-report` (`access.admin`) con las excepciones vivas y las cuentas que violan
una regla sin excepción. Una excepción se cierra sola cuando la combinación desaparece.

---

## 6. Step-up ("sudo mode")

Diseño en el docstring de `app/core/step_up.py`; contrato en
[`api-reference.md` §5](../api-reference.md#post-apiv1authstep-up).

- **La ventana es de la sesión**: `gateway_sessions.step_up_at` + `STEP_UP_TTL_SECONDS` (300 s).
  La abre el **login** y la renueva `POST /auth/step-up {password}`. **No es deslizante**: usarla no
  la estira. No es de un solo uso ni está atada a una capacidad o destino: cubre los flujos preview
  → execute (el binding operación ↔ destino ya lo dan los `confirm_token`).
- **Cuándo se pide**: la capacidad tiene `requires_step_up` (16 de 32) **y** el método no es seguro,
  **o** la capacidad divulga (un `GET` que entrega datos, como `exports/{id}/content` o las
  capturas), **o** el método es desconocido (fail-closed). Un `GET` que no divulga no lo pide.
- **Orden**: es el último chequeo, después de las capas 1 y 2. El `403` sale antes de cualquier
  efecto, así que reintentar tras el prompt es seguro.
- **Códigos**: `403 access.step_up_required` (con `step_up_ttl_seconds`); `400 auth.step_up_failed`
  con `attempts_remaining` (no `401`, para no disparar el logout de la SPA); al **quinto fallo
  seguido** la sesión se revoca y responde `401 auth.session_step_up_failed`; `429` por encima de
  5/min por usuario + IP.
- El `sid` **no rota** en el step-up: el token CSRF de los requests en vuelo sigue valiendo.
- **Agentes**: un token nunca tiene una capacidad con step-up (invariante 11).
- `STEP_UP_ENFORCED=false` lo apaga: el arranque avisa y `/auth/me.step_up_enforced` sale `false`.

**Exenciones: solo cancelaciones** (`STEP_UP_EXEMPT`, chequeo 7 del script). Frenar una operación
nunca puede costar más que lanzarla. Hoy son **cinco**, todas `POST …/cancel`:

| Ruta | Motivo |
|---|---|
| `/database-clones/{job_id}/cancel` | Cancelar un clonado. |
| `/database-clone-batches/{batch_id}/cancel` | Cancelar un lote de clonado. |
| `/collation-conversions/{job_id}/cancel` | Cancelar una conversión de collation. |
| `/database-models/{model_id}/collation-conversions/{batch_id}/cancel` | Cancelar un lote de conversión. |
| `/access-requests/{request_id}/cancel` | Retirar una elevación propia pendiente: nunca da acceso. |

Las capas 1 y 2 siguen valiendo en las cinco. El chequeo 7 rechaza una exención que no esté en la
lista, que no sea un `POST …/cancel` o cuya capacidad no pida step-up. La cancelación de una
exportación usa `exports.execute`, que no pide step-up, así que no figura.

---

## 7. Arranque: siembra, ventana de arranque y recuperación

Detalle operativo en [authentication.md](authentication.md#primer-arranque-la-ventana-de-arranque);
contrato en `api-reference-v29.md` §10.

- **Siembra** (`bootstrap_admin`, solo con la tabla `users` vacía): `ADMIN_USERNAME` como
  **`viewer` + `access_admin`**, nada más. Con usuarios ya creados no siembra ni repara nada.
- **Ventana de arranque** (`app/services/bootstrap_window.py`, `ACCESS_BOOTSTRAP_WINDOW_HOURS`,
  default 72). Mientras está abierta **y** quien pide es el único `access_admin` activo con
  credencial, sus elevaciones se aplican en el acto, auditadas `access.bootstrap_assignment`. Se
  cierra **para siempre** cuando un segundo `access_admin` acepta su invitación (`second_admin`) o al
  vencer el plazo (`deadline`). El cierre se evalúa de forma perezosa (en cada decisión de elevación,
  en `/auth/me`, al aceptar una invitación y al arrancar). Ante cualquier error de lectura queda
  cerrada: las elevaciones esperan al segundo aprobador.
- **Primer arranque esperado**: entrar con la cuenta sembrada → crear el `security_officer` → crear
  el `owner` → crear el segundo `access_admin` y entregarle la invitación → la ventana se cierra
  cuando la acepta.
- **`ADMIN_RECOVERY=1`** (solo al arrancar): reactiva `ADMIN_USERNAME` (o la crea como la siembra),
  le devuelve **solo** `access_admin`, no toca la contraseña, reabre la ventana y audita
  `access.admin_recovery`. Hay que quitarlo después: cada arranque con él reabre la ventana. El
  ancla de confianza es el acceso al servidor.
- **`ACCESS_FOUR_EYES`** (default `true`). En `false`, pensado para una instalación con un solo
  administrador real, las elevaciones se aplican sin segundo aprobador; el arranque avisa y cada una
  se audita `access.elevation_unapproved`.

---

## 8. Auditoría

`audit_log` registra, entre otras cosas, todo lo que cambia el acceso:

- **Cambios de acceso con antes y después completos** (`gateway_user.update`,
  `gateway_user.access_set`): rol base, estado, globales y alcances, con tope de 200 por lado. Los
  datos de contacto se nombran pero no se copian.
- Capacidades puntuales (`capability_grant.created|requested|approved|rejected|revoked|cancelled|expired`)
  y elevaciones (`access_request.created|approved|rejected|cancelled|expired`),
  `access.sod_override`, `access.sod_grandfathered`, `access.bootstrap_assignment`,
  `access.bootstrap_window_opened|closed`, `access.elevation_unapproved`, `access.admin_recovery`.
- Autenticación: `auth.login`, `auth.login_failed`, `auth.step_up`, `auth.step_up_failed`, `auth.password_changed`,
  `auth.password_change_failed`, `auth.sessions_revoked`, `gateway_user.sessions_revoked`.
- **Denegaciones** (`access.denied`, `app/core/denial_audit.py`): los `403` por capacidad, alcance,
  CSRF/Origin y la neutralización de separación de deberes. **Agregadas**: como mucho una fila por
  (actor, código, método, ruta normalizada) cada 60 s, con la cuenta de lo que quedó sin fila.
  Nunca cambian el `403` ni filtran la capacidad al cliente.
- **Atribución de tokens**: una acción de un token de agente deja `admin_id = NULL`,
  `api_token_id` = PK del token y `admin_username = "token:<token_id>"`. Un token nunca se lee como
  una persona.

**Quién lee la auditoría.** `GET /audit-log` y `GET /audit-log/{id}` exigen **`policy.admin`**
(`security_officer`). `access_admin` recibe `403`: quien hace los cambios de acceso no revisa el
rastro que los registra. La lectura **no divulga** (dice quién hizo qué, no datos del tercero), así
que un `GET` no pide step-up. Filtros y forma en `api-reference-v29.md` §11.3.

---

## 9. Sesiones y contraseña

Sesiones server-side (`gateway_sessions`); la cookie lleva solo el `sid` firmado. Vida absoluta
`SESSION_ABSOLUTE_MAX_HOURS` (12 h) e inactividad `SESSION_IDLE_MINUTES` (60 min). Cada revocación
tiene motivo y el request siguiente responde `401 auth.session_<motivo>`.

| Acción | Quién | Efecto |
|---|---|---|
| `POST /auth/sessions/revoke-others` | la propia persona (`self.read`) | Cierra sus sesiones **menos la actual**. |
| `POST /gateway-users/{id}/sessions/revoke` | `access.admin` + step-up | Cierra **todas** las sesiones de otra persona (`401 auth.session_access_admin_revoked`). Sobre uno mismo, `409`. No toca la contraseña. |
| `POST /auth/password` | la propia persona | Cierra **todas** sus sesiones (`password_change`) y la respuesta abre una nueva (el `sid` y el token CSRF rotan). Límite 5/min por usuario + IP. |
| Cambiar rol, accesos o desactivar | `access.admin` | Corta las sesiones de la persona (`role_change`). También al aprobar una elevación. |
| Cinco fallos seguidos de step-up | — | Revoca esa sesión (`step_up_failed`). |

**Login**: 20/min por IP, **5/min por IP + usuario** normalizado y 20/hora por usuario desde cualquier
IP (`LOGIN_USERNAME_RATE_LIMIT`, vacío lo apaga). Se cuentan todos los intentos y el límite corre
antes de verificar la contraseña. Aceptar una invitación: 10/min por IP, y toda falla responde el
mismo `422 gateway_user.not_found` (sin `410`, que era un oráculo de cuentas pendientes).

---

## 10. Tokens de agente (MCP)

- **Techo de agente** (`AGENT_ALLOWED`): `databases.read`, `blueprints.read`, `schema_diff.read`.
  Nada que mute ni divulgue (invariante 5) y nada con step-up (invariante 11).
- `parse_scopes` **intersecta** los scopes guardados con el techo al leer: una fila manipulada nunca
  otorga más, aunque el string lo diga.
- Crear, listar, editar y revocar los tokens de TODOS es `access.admin`; cada persona administra los suyos con `tokens.own` (los tres roles; ver `api-reference-v40.md`). Un token está atado a un proyecto, vence
  (`MCP_TOKEN_MAX_TTL_DAYS`, default 90) y el servidor MCP está apagado por defecto (`MCP_ENABLED`).
- El dispatcher MCP exige el scope de cada herramienta (`mcp.scope_denied`, auditado).
- Qué BD ve un agente lo decide la **política**, no el acceso: `allows_agent_access` del entorno y
  `PUT /managed-databases/{id}/agent-access`, ambos `environments.write` (`security_officer`).

Contrato en `api-reference-v23.md` §9 y `api-reference-v24.md` §3; guía de uso en
[mcp-para-colaboradores.md](mcp-para-colaboradores.md).

---

## 11. Datos que no salen: errores del motor e historial SQL

- **Errores del motor saneados.** Migraciones, collation, copia de datos y clones devuelven un
  `error_code` de vocabulario cerrado (`engine.duplicate_key`, `engine.fk_violation`,
  `engine.data_truncated`, …; `app/services/engine_error_catalog.py`) y un mensaje sin valores de
  filas. El texto crudo del motor va solo al log, con el Request ID. Las filas viejas se sanean al
  leerlas.
- **Historial de la consola SQL enmascarado.** `GET …/query/history` es de `viewer`, pero el texto
  completo solo lo ve quien tiene `sql_console.execute` **en el destino de esa fila**; el resto
  recibe los literales como `?` y `sql_masked: true`. Detalle en
  [sql-query-console.md](sql-query-console.md) §7.

---

## 12. Endpoints de autorización

| Ruta | Capacidad | Contrato |
|---|---|---|
| `GET /auth/me` | `self.read` | `api-reference.md` §5 y §19; `api-reference-v29.md` §8.5 y §10.4 |
| `POST /auth/step-up`, `POST /auth/password` | `self.read` | `api-reference.md` §5 |
| `GET /auth/sessions`, `POST /auth/sessions/revoke-others` | `self.read` | `api-reference-v23.md` §7.4 |
| `GET /authz/catalog` | `self.read` | `api-reference-v23.md` §2 |
| `GET /authz/scope-readiness`, `GET /authz/sod-report` | `access.admin` | `api-reference-v23.md` §8; `api-reference-v29.md` §8.6 |
| `/gateway-users/*` (salvo `invite/accept`, público) | `access.admin` | `api-reference-v24.md` §2; `api-reference-v29.md` §9 y §11 |
| `/gateway-users/{id}/capability-grants`, `/capability-grants/*` | `access.admin` | `api-reference.md` §19 |
| `/access-requests/*` | `access.admin` | `api-reference-v29.md` §9.4 |
| `/api-tokens/*` | `access.admin` (todos) o `tokens.own` (solo los propios) | `api-reference-v24.md` §3 |
| `/audit-log`, `/audit-log/{id}` | `policy.admin` | `api-reference-v29.md` §11.3–§11.4 |
| `POST /admin/crypto/rotate` | `policy.admin` | `api-reference.md` §13 |

---

## Decisiones y por qué

Decisiones tomadas el 2026-10-01 y el 2026-10-02 al cerrar la separación de funciones (F-21) y el
step-up. El razonamiento largo vive en los docstrings citados.

| Decisión | Por qué |
|---|---|
| `gateway.admin` se parte en `access.admin` (`access_admin`) y `policy.admin` (`security_officer`), con globales disjuntas. | Las dos globales tenían `gateway.admin`, así que `security_officer` también administraba usuarios: la separación existía solo en el papel. |
| `access_admin` ≠ `security_officer` en una misma cuenta (segunda regla de separación). | Juntas reconstruyen el administrador combinado que la partición vino a deshacer. |
| `owner` ≠ `security_officer` en cualquier forma, incluida una capacidad puntual exclusiva de `owner`. | Quien opera producción no puede ser quien apaga su barrera; una capacidad puntual de `owner` es `owner` en sustancia. |
| Se retira el techo por tenencia; manda la política de asignación + segundo aprobador. | El techo obligaba a quien asigna a tener cada deber que reparte, justo la combinación prohibida. La protección contra el títere `owner` pasa al segundo aprobador. |
| Sensibles = **todo** lo exclusivo de `owner` (11), no solo lo que divulga o es `drop`. | Sin el techo, `blueprints.apply`, `schema_diff.execute` y `collation.execute` los habría otorgado un solo administrador. |
| `collation.execute` pasa a `owner` y queda destructiva. | `ALTER TABLE … CONVERT` reescribe tablas del tercero y no se deshace. |
| Las operaciones de blueprint sobre toda la flota exigen `blueprints.apply` (o `blueprints.captures` / `databases.drop` según el caso). Un `operator` las obtiene **solo por capacidad puntual**. | Tocan bases de terceros, incluida producción, con permisos de autoría. `blueprints.write` queda solo como autoría. |
| Elegir la contraseña de una cuenta del motor es `engine_users.credentials` (divulga). | Quien elige la credencial la conoce y entra al motor por fuera del gateway. |
| Las cuentas combinadas existentes se **heredan** (`grandfathered`), no se parten. | Partirlas podía dejar la instalación sin `security_officer` o sin `owner`, sin forma de repararlo (nadie se modifica a sí mismo). |
| La siembra nueva es `viewer` + `access_admin`, con ventana de arranque de 72 h. | La cuenta inicial no junta deberes; la ventana deja crear al segundo aprobador sin apagar los cuatro ojos para siempre. |
| El arranque no revive ni re-eleva cuentas sin `ADMIN_RECOVERY=1`, y la recuperación solo devuelve `access_admin`. | Si un reinicio reparara solo, desactivar al administrador sería reversible por reinicio. |
| Step-up activo por defecto, ventana de 5 min por sesión, no deslizante; el login cuenta. | Fricción mínima en flujos de varios requests sin dejar que una cookie robada mantenga abierta la ventana. |
| Las cancelaciones están exentas de step-up (lista cerrada, chequeada en CI). | Frenar una operación destructiva nunca puede costar más que lanzarla. |
| Leer la auditoría es `policy.admin`, no `access.admin`; no pide step-up. | El revisado no se revisa a sí mismo; la auditoría no divulga datos del tercero. |
| `access_admin` puede ver y cerrar las sesiones de otra persona; no toca la contraseña. | Responder a un incidente sin tocar la credencial; ante una filtración se combina con desactivar la cuenta. |
| Al leer, una combinación sin excepción pierde `security_officer`, no `owner` ni `access_admin`. | Quitar `access_admin` podría dejar al gateway sin quien lo repare; lo que se cae es la política. |
| Una BD sin clasificar cuenta como el entorno más protegido. | "Sin entorno derivable" nunca puede significar "permitido". |

---

## Changelog

| Fecha | Commit | Cambio |
|---|---|---|
| 2026-09-09 | `e9bbc08` … `4c611e9` | Catálogo de capacidades, `Actor`, `require()`, migración de todas las rutas y retiro de `AdminDep`. |
| 2026-09-09 | `3a0c4bf`, `14756ca` | Sesiones server-side y CSRF derivado del `sid`. |
| 2026-09-09 | `f9d7edf`, `09ae317` | Capa 2 (rol en el destino) e invariante del último `access_admin`. |
| 2026-10-01 | `6ca2d69` | Editar el catálogo de charsets exige `catalogs.write`. |
| 2026-10-01 | `4a66cb2`, `2580259` | Un grant elevado no se filtra fuera de su alcance; sin auto-escalada de accesos. |
| 2026-10-01 | `f551fa6` | Aplicar migraciones al crear una base exige `blueprints.apply`. |
| 2026-10-01 | `b0d4b4a`, `6072820`, `bfa3d40`, `5256f9f`, `00ed5c8`, `abe240f` | `require_at` en toda ruta con destino, `environments.write`, doble extremo, entorno más protegido del blueprint, lotes que omiten y chequeo de alcance estricto. |
| 2026-10-01 | `7a97abe`, `5699d4d`, `4ed027d`, `b5d0da0`, `91b8306`, `85ae1f3`, `b8bfde6` | Capacidades puntuales: tabla, resolvedor, endpoints, aprobación por segundo admin, vencimiento de pendientes, `/auth/me` y catálogo. |
| 2026-10-01 | `e647ca6` | `blueprints.apply` para adoptar y aplicar; `exports.download` para la muestra. |
| 2026-10-02 | `66c5d0e` | Login limitado por IP, IP + usuario y usuario. |
| 2026-10-02 | `f98a2be`, `3289038` | MCP: rechazos de credencial limitados por IP y auditados; el dispatcher exige el scope. |
| 2026-10-02 | `46f5ac0` | Invitaciones: firma antes del vencimiento, respuesta única `422`. |
| 2026-10-02 | `608f82a` | Cambiar la propia contraseña. |
| 2026-10-02 | `4c32832`, `a3b1541` | Errores del motor saneados en migraciones, collation, copia de datos y clones. |
| 2026-10-02 | `8520481`, `1137b48`, `ef2dec0` | Grant restrictivo ilegible = `viewer`; sin permisos sobre alcances inexistentes; candado del último `access_admin` dentro de la transacción. |
| 2026-10-02 | `9c7dffe`, `b359305`, `4041419` | Auditoría: antes/después completos, atribución al token, denegaciones agregadas. |
| 2026-10-02 | `f671a67`, `c5edee5`, `e216df1` | Flag `destructive` e invariantes; `collation.execute` a `owner`; operaciones de flota exigen `blueprints.apply`. |
| 2026-10-02 | `dab9ada` | Historial SQL enmascarado para quien no puede ejecutar. |
| 2026-10-02 | `eb49132` | Step-up exigido. |
| 2026-10-02 | `ab999e3` | `engine_users.credentials`. |
| 2026-10-02 | `394611b` | C1: partición de `gateway.admin`. |
| 2026-10-02 | `c352803` | C2: separación de deberes, `sod_override`, herencia. |
| 2026-10-02 | `326c20b` | C3: política de asignación, `access_change_requests`, 11 sensibles. |
| 2026-10-02 | `27b898a` | C4: siembra `viewer` + `access_admin`, ventana de arranque, `ADMIN_RECOVERY`. |
| 2026-10-02 | `2412363` | Lectura de auditoría para `policy.admin`. |
| 2026-10-02 | `b10f1e2` | `access_admin` ve y cierra las sesiones de otra persona. |
