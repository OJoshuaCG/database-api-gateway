# 13 — Usuarios y autorización del gateway

> **Estado**: propuesta, sin implementar. Relevamiento: **2026-09-09**.
> Head de Alembic al momento de escribir: **`b7c8d9e0f1a2`** (30 revisiones, head único).
>
> Nace del §7.6 del plan 12, que declaró este trabajo fuera de su alcance y explicó por qué el
> MCP lo fuerza: el CRUD de `api_tokens` es un endpoint que **otorga privilegio** y hoy queda
> detrás de una dependencia que no distingue rol.

---

## Objetivo

El gateway administra servidores de bases de datos de terceros con credenciales pseudo-root.
Tiene **un administrador único** y **153 gates de una sola dependencia binaria** protegiendo
**160 endpoints**. Cualquier usuario autenticado puede hacer absolutamente todo: dropear una base
de producción, revelar las contraseñas de los usuarios del motor de un cliente, exportar sus datos
en claro, o apagar la barrera que impide migraciones destructivas en producción.

Este plan introduce usuarios nominales, autorización por capacidad y alcance, y el rastro de
auditoría que hace que la atribución sea confiable.

**Y una advertencia de orden que atraviesa todo el documento:** el retorno está en el orden
**autorización → usuarios nominales → 2FA**, que es el inverso al que se hace habitualmente. Un
segundo factor sin autorización es una cerradura buena en una puerta sin marco: se verifica mejor
quién entra, y adentro todos pueden todo.

---

## 1. Lo que YA existe y se reutiliza

| Pieza | Ruta | Para qué acá |
|---|---|---|
| Tabla `users` multiusuario | `app/models/user.py` | Ya tiene `username`, `email`, `hashed_password`, `is_active`. **No hay que crearla** |
| El relee del usuario por request | `app/core/auth.py:38-53` | `get_current_admin` relee `is_active` de la BD en cada request. **Esa propiedad es la correcta y hay que conservarla**: es lo que hace que desactivar surta efecto de inmediato |
| Hash de contraseñas | `app/utils/security.py` | Argon2id ya configurado |
| Doble intención por nombre | `confirm_target_name` en clone/export/comparison/drop | El gesto que obliga a identificar *cuál* objeto. Se conserva; el step-up es un eje distinto que se **suma** |
| `confirm_token` HMAC | `app/services/confirm_token.py` | HMAC-SHA256 con expiración embebida y campo `subject`. **`subject` es el punto de composición del step-up** |
| Auditoría, en dos sabores | `app/services/audit.py:73` (`record`, **best-effort: nunca lanza**) y `:121` (`record_intent`, **fail-closed**) | La distinción ya está: divulgación ⇒ `record_intent` |
| El molde de ownership | `app/controllers/export_controller.py:2546` (`_guard_owner`) | **Su docstring dice que se escribió "para el día que exista multiusuario".** Es el molde a generalizar, no a reinventar |
| Entornos con política | `app/models/environment.py` | `rank`, `is_default`, `blocks_destructive_migrations`. Y su docstring (`:20-25`) declara la regla de **cero flags inertes**. **Ojo: los tres flags que este plan agrega NO están entre los cuatro que ese docstring dejó diferidos** (`requires_confirmation`, `requires_previous_environment`, `max_databases_per_apply`, `allows_agent_queries` — ése último es del plan 12). Son nuevos; lo que se hereda es la REGLA, no la previsión |
| El patrón de guard sin default | `app/controllers/export_controller.py:478` | `target` es obligatorio y sin default a propósito |
| Catálogos de vocabulario cerrado | `app/services/*_catalog.py` | El molde del catálogo de capacidades |
| Rate limit | `app/core/limiter.py` | SlowAPI. Hay que cambiarle el eje (§7.4) |
| Los escalones de riesgo, ya codificados | los `@limiter.limit` (3/10/20/30/60) y qué exige `confirm_token` | **El repo ya clasificó el riesgo de cada endpoint dos veces de forma independiente.** Los niveles de capacidad no hay que inventarlos: hay que nombrarlos |

---

## 2. El estado real, medido

| | |
|---|---|
| Endpoints en `app/routes/v1/*.py` | **160**, en 20 archivos |
| Ocurrencias del string `AdminDep` | 176 — de las cuales **153 son gates reales** (`admin: AdminDep` como parámetro); el resto son `import` y menciones en docstrings |
| Endpoints **sin gate** | **8**: `POST /auth/login` (correcto) y **los 7 de `test.py`** |
| Sitios que pasan o desestructuran el dict `admin` (`admin=admin`, `admin.get(...)`) | **227** en controllers y rutas |
| Roles | ninguno. `is_superuser` se escribe en 3 lugares y **no se lee en ninguno** |
| Protección CSRF | ninguna. Solo `same_site="lax"` |
| Tope absoluto de sesión | ninguno (§7.1) |
| Logout real | no existe: la cookie es stateless y `clear()` solo borra la del cliente |

**El costo de la migración lo fijan los 208, no los 142.**

### 2.1 Los cinco huecos vivos, verificados

Ninguno es de este plan: son de código desplegado. Tres se pueden arreglar **antes** y por separado.

**(a) `app/routes/v1/test.py`: 7 endpoints sin autenticación, montados en la API.**
Cero `AdminDep`, incluido en `app/routes/v1/routes.py:44`. Contiene `DELETE /test/resource/{id}`,
`POST /test/upload` y `POST /test/upload/multiple` — **dos endpoints de subida de archivos sin
autenticar que escriben a disco**, en un gateway con credenciales pseudo-root. Se saca de
`routes.py` o se condiciona a `APP_ENV == "development"`. **Meterlo en la allowlist del guard de
cobertura (§6.3) sería usar el mecanismo de escape para consagrar el defecto que el mecanismo
existe para detectar.**

**(b) `app/controllers/server_controller.py` no tiene UNA SOLA llamada a auditoría.**
Verificado: cero ocurrencias de `audit`. Y ese controller es el que registra y edita servidores, o
sea **el único camino del repo que combina máximo privilegio con cero rastro**. Alta, edición y
baja de la credencial pseudo-root de un cliente no dejan huella.

**(c) `POST /schema-comparisons/{id}/adopt` no tiene NINGÚN campo de confirmación.**
Verificado en `app/schemas/schema_comparison.py:91-117`: `AdoptComparisonIn` es
`{selected_item_ids, name, description, execute_immediately, auto_resolve_dependencies}`. Con
`execute_immediately=true` **aplica el DDL al target**. Es el único camino de ejecución del repo
sin re-tipeo de confirmación. Debería ser el primer parche, independiente de este plan.

**(d) `POST /servers/{sid}/users/reveal-password` no tiene rate limit.**
Verificado: no hay `@limiter.limit` sobre esa ruta. Devuelve la contraseña en claro de un usuario
del motor, y con el default de 100/min/IP **se puede vaciar el llavero completo**. Está auditado
fail-closed, que es lo único que hay.

**(e) `GET /database-exports/{id}/manifest` no pasa por `_guard_owner`.**
Los hermanos `/download` y `/content` sí. Así que otro usuario ve checksum, lista de objetos y
conteo de filas de un export ajeno. Y `_guard_owner` **falla ABIERTO** si
`created_by_admin_id` es `NULL`.

### 2.2 Cuatro defectos estructurales que este plan hereda

1. **El `confirm_token` no ata la identidad.** `confirm_token.py:29-36` firma
   `(operation, server_id, db_name, exp[, subject])`. En multiusuario, **el token que emite el
   `preview` de A lo puede canjear B** dentro del TTL, y la auditoría no puede probar quién
   confirmó. Con un administrador único es inocuo; con dos usuarios es una escalada.
2. **Solo 4 tablas guardan autor**: `audit_log`, `export_jobs`, `query_executions`,
   `collation_conversion_batches`. **No lo guardan** `clone_jobs`, `collation_conversion_jobs`,
   `schema_comparisons` ni `migration_select_results` — así que ownership sobre esos jobs es
   imposible sin migración.
3. **No existe ninguna acción de auditoría de autenticación.** Entre las 77 del vocabulario no hay
   `auth.login`, `auth.logout` ni `auth.login_failed`. **Un sistema de permisos sin registro de
   autenticación no es auditable.**
4. **`UserModel.update` interpola las CLAVES del dict en el `SET`** (`user_model.py:136-138`). Los
   valores van parametrizados (`:key`), las claves no. Hoy vienen de código — y **este plan es
   justamente el que agrega `PATCH /gateway-users/{id}`**. Hay que whitelistear las columnas
   actualizables **antes** de exponer ese endpoint, no después.

---

## 3. Nomenclatura: dos planos que no se pueden mezclar

Es la trampa de las "dos cosas llamadas environment" que el `CLAUDE.md` ya documenta, con un
agravante: **PostgreSQL llama *roles* a los usuarios del motor**, así que la colisión no es solo
del repo, es del vocabulario del dominio.

El plano del **motor** ya quemó las palabras **permiso, privilegio, perfil, grant**:
`permission_profiles`, `privileges`, `PermissionProfile`, `Privilege`, `privilege_catalog.py`,
`privilege_seed.py`, `apply_profile`.

| Concepto | Plano GATEWAY (este plan) | Plano MOTOR (existente) |
|---|---|---|
| Unidad de autorización | **`Capability`** — `servers.admin` | `Privilege` — `SELECT`, `CREATE` |
| Agrupación | **`GatewayRole`** — `viewer\|operator\|owner` | `PermissionProfile` |
| Catálogo | `app/services/capability_catalog.py` | `app/services/privilege_catalog.py` |
| Sujeto | `users` (usuario del gateway) | `server_users` (usuario del motor) |
| Acciones de auditoría | `access.*`, `auth.*` | `server_user.*`, `privilege.*` |
| Ruta REST | **`/gateway-users`** | `/server-users`, `/servers/{id}/users` |

**Regla para `CLAUDE.md`, calcada de la de entornos: nunca "permisos" a secas.** Es *capacidad del
gateway* o *privilegio del motor*.

**Y se hace cumplir con un guard, no con disciplina** — `tests/test_plane_vocabulary.py`, sin
motor: los archivos del plano gateway no contienen las subcadenas `privileg`/`permission`/`grant`,
y los del plano motor no contienen `capabilit`/`gateway_role`. Cuesta 20 líneas y convierte la
convención en algo que falla en CI.

**Nunca crear `app/services/permissions.py` ni `app/models/permission.py`**: es el nombre que un
lector futuro va a asumir que es una de las dos cosas, y va a acertar la mitad de las veces.

---

## 4. El modelo de autorización: TRES ejes, no uno

La intuición natural —tres roles— es la forma correcta de la **superficie de API**, pero como
modelo completo tiene tres agujeros. Los tres se descubrieron en el relevamiento y los tres son
bloqueantes.

### 4.1 Primer eje: la capacidad, con niveles acumulativos

Ni 150 permisos (nadie los configura bien) ni 3 roles (no alcanzan). **11 módulos con niveles
ordenados y acumulativos** (`read ⊂ write ⊂ execute/drop ⊂ secrets`): se configura "nivel N sobre
módulo M".

| Módulo | Niveles | Cubre |
|---|---|---|
| `servers` | `read` · `admin` | CRUD de `/servers` + `test-connection`. **Sin nivel intermedio a propósito** |
| `engine-users` | `read` · `write` · `secrets` | `/server-users/*` + `/servers/{sid}/users/*`, incl. GRANT/REVOKE y perfiles |
| `databases` | `read` · `write` · `drop` | `/managed-databases` (menos migraciones) + `/servers/{sid}/databases*` |
| `blueprints` | `read` · `write` · `apply` · `captures` | `/database-models`, `/…/migrations/*`, `/projects/*`; `select-results` en `captures` |
| `schema-diff` | `read` · `execute` | `/schema-comparisons/*` (`adopt` y `execute` en `execute`) |
| `clones` | `read` · `execute` | `/database-clones/*` |
| `collation` | `read` · `execute` | `/collation-conversions/*` + el lote por blueprint |
| `exports` | `read` · `execute` · `download` | `/database-exports/*` |
| `sql-console` | `history` · `execute` | `/servers/{sid}/query/*` |
| `catalogs` | `read` · `write` | `/privileges`, `/permission-profiles`, `/charset-collation-options` |
| `gateway-admin` | (único) | `/admin/crypto/rotate`, `/environments` mutante, `/gateway-users`, `/api-tokens` |

**Tabla NORMATIVA: cada ruta declara exactamente UNA capacidad** (§6.3 punto 1), así que las
asignaciones no pueden aparecer dos veces con nombres distintos. Tres que hay que fijar acá y no
resolver por lectura:

| Ruta | Capacidad | Rol que la incluye |
|---|---|---|
| `POST/PATCH/DELETE /servers` | `servers.admin` | **solo `security_officer`** — NO `owner` (§4.6: re-apuntar un `server_id` redirige cada operación futura de todo operador) |
| `PATCH /privileges`, `POST/PATCH /permission-profiles`, `POST/PATCH /charset-collation-options` | `catalogs.write` | **solo `security_officer`** (§4.5: son datos de política) |
| `POST/PATCH/DELETE /environments` | `gateway-admin` | **solo `security_officer`** |

Lo que queda en `gateway-admin` para `access_admin` es `/gateway-users`, `/api-tokens` y
`/admin/crypto/rotate`. Sin esta tabla, quien implemente elegiría una de las dos capacidades que el
documento nombraba y la descartada quedaría como **vocabulario muerto que el punto 4 del §6.3 hace
fallar** — o peor, se cablearía `servers.admin` dentro de `owner` y el requisito del §4.6 se
perdería en silencio.

**El criterio que impide que crezca a 150, y va en el docstring del catálogo: una capacidad nace
cuando dos roles necesitan diferir en ella.** Si los tres roles coinciden sobre un conjunto de
endpoints, ese conjunto es **una** capacidad.

**Dónde separar `leer` de `escribir` importa:**

- **`servers`**: `read` ya expone `host`, `port` y `root_username` de todo el parque — es
  reconocimiento de la infraestructura del cliente. Y `admin` es la llave maestra. Son poderes tan
  distintos que un nivel intermedio solo daría falsa sensación de gradualidad.
- **`engine-users`: `write` ≠ `secrets`.** Rotar una contraseña es rutina; **leerla en claro** es
  divulgación, y quien opera no la necesita. Hoy `PATCH …/password` y `POST …/reveal-password` son
  el mismo permiso y no tienen nada que ver.
- **`blueprints`: `write` ≠ `apply`.** Es la separación que el `TODO.md` del repo ya pide por
  escrito: *"la respuesta correcta no es reponer un booleano de request… sino separación de roles:
  quien aprueba ≠ quien aplica"*. Autor de la migración y ejecutor sobre producción son dos
  personas.
- **`exports`: `execute` ≠ `download`.** Planear y generar no divulga nada mientras el artefacto no
  se entregue; `_guard_owner` ya reconoce esa frontera en el código.
- **`sql-console`**: `history` es lectura barata y muy consultada (60/min); `execute` es SQL
  arbitrario. Nunca deberían viajar juntos.

**Dónde NO separar:** `catalogs` (las tres tablas son el mismo objeto, ninguna toca un motor, y
tres módulos serían tres casillas que se configuran igual siempre); `projects` (agrupador sin
motor, va dentro de `blueprints`). Y `clones`/`collation`/`schema-diff` quedan separados **aunque
compartan la forma** plan→preview→execute, porque el perfil de quien los usa difiere: el diff es
del que compara ambientes, el clon del que arma entornos, la conversión del DBA. Fundirlos
obligaría a dar los tres para habilitar uno.

### 4.2 Segundo eje: DIVULGACIÓN, que un modelo destructivo/no-destructivo pierde entero

**Éste es el agujero más grave de la hipótesis de tres roles.**

`reveal-password`, `GET /database-exports/{id}/content`, `GET …/select-results` y
`POST /admin/crypto/rotate` **no destruyen nada**. No borran, no reescriben. Así que un modelo
partido en "destructivo / no destructivo" los deja pasar **completos**.

Consecuencia concreta: **`operator` en producción es exactamente el rol que le darías a quien
aplica migraciones — y en silencio incluye exportar la base entera del cliente en claro y leer las
contraseñas de los usuarios de su motor.**

**Diseño:** cada `CapabilitySpec` lleva **dos flags independientes**, `mutates` y `discloses`. Y la
política de entorno gana un eje propio: **`Environment.blocks_data_disclosure`**, separado de
`blocks_destructive_migrations`. Son riesgos ortogonales y una sola casilla no puede expresar los
dos.

### 4.3 Tercer eje: el alcance — y por qué no puede ser solo el entorno

El caso real es "operador en desarrollo, lector en producción". Pero **`environment_id` existe solo
en `managed_databases`** (verificado: es el único modelo que lo tiene). `servers` no tiene entorno,
y `server_users` tampoco.

Consecuencia: de los 160 endpoints, el entorno es resoluble **solo** en la familia
`managed-databases/{db_id}/*`. Los **29 endpoints de `servers.py`** no tienen a qué anclarse — y
ahí viven `DROP DATABASE`, `DROP USER`, la consola SQL y `reveal-password`. **"Lector en
producción" sería hoy una promesa incumplible sobre la mitad más destructiva de la superficie.**

**Diseño, en dos partes:**

1. **`scope_type ∈ {global, environment, server}`** en la tabla de grants. El alcance por servidor
   es lo que cubre la superficie que no tiene entorno.
2. **Una regla de derivación fail-closed, escrita UNA vez** en `app/core/authz.py`: toda operación
   declara su destino como `(server_id?, managed_database_id?)`. Si el destino es una BD
   gestionada, el entorno es el suyo. **Si el destino es a nivel servidor, el entorno efectivo es
   el de `rank` MÁXIMO entre las bases de ese servidor, tratando `NULL` como el máximo global**
   (§4.5.2). Un servidor sin ninguna base clasificada resuelve entonces al máximo, o sea al más
   protegido: es un caso particular de la misma regla, no una cláusula aparte.

> "Sin entorno derivable" **nunca** significa "permitido". Ese es el default que convierte un
> modelo de alcance en decoración.

**Recomendación adicional (§16.1): agregar `servers.environment_id`.** Sin eso, la derivación del
punto 2 es lo único que cubre `servers.py`, y funciona — pero clasificar el servidor directamente
es más barato de operar y más fácil de auditar.

### 4.4 Y los tres roles no separan "operar" de "otorgar"

Con `viewer`/`operator`/`admin`, el único rol que puede aplicar migraciones a producción es
`admin`. Así que **en un equipo de tres, los tres terminan `admin` en la semana dos.** Y `admin`
incluye apagar `Environment.blocks_destructive_migrations` — o sea, **el rol de la operación diaria
es el mismo que puede desarmar la barrera de producción**.

**Diseño: tres roles CON ALCANCE + dos capacidades globales ORTOGONALES** (no un producto
cartesiano de cinco roles):

| Rol / capacidad | Alcance | Qué puede |
|---|---|---|
| `viewer` | por alcance | inventario + introspección de esquema. **No** consola SQL, **no** export, **no** revelar credenciales |
| `operator` | por alcance | operaciones mutantes dentro del alcance. Destructivo solo si la política del entorno lo permite, y con doble intención |
| `owner` | por alcance | todo lo operativo del alcance, incluido destructivo, `reveal-password` y `download` |
| **`access_admin`** | **global** | usuarios, roles, grants, tokens de API, lectura de auditoría. **Explícitamente NO operativo**: no aplica migraciones, no dropea, no revela contraseñas |
| **`security_officer`** | **global** | escribe **datos de política**: `Environment`, `charset_collation_options`, `privileges.is_active`, `permission_profiles`, y `servers.create/update` |

`access_admin` no operativo es **la mitad de la separación de deberes** que hace implementable el
§9.

**Y las ortogonales van en OTRA tabla, no como valores de `role`.** Si `access_admin` fuera un valor
de `access_grants.role`, esa columna tendría cinco valores de los cuales tres forman cadena y dos no
son comparables con ninguno — y `max({owner, access_admin})` **no tendría valor**. El
invariante 2 del §5 dice que sin monotonía "rol máximo" no significa nada, así que meterlas en la
misma columna rompería el invariante que sostiene toda la resolución de alcance. De ahí las dos
tablas del §14: `access_grants` para la cadena ordenada, `global_capabilities` para las ortogonales.
Y `Actor.capabilities = ROLE_CAPABILITIES[max(cadena)] | GLOBAL[ortogonales]`.

**Tres invariantes, no uno**, porque el self-grant no es la vía de escalada:

1. **Un grant cuyo `user_id` es el actor es inválido**, 409, siempre, sin override.
2. **Todo grant de `owner`, de `access_admin`, de `security_officer` o de alcance `global` exige
   segundo aprobador — INCONDICIONALMENTE, en código, sin flag de entorno.** Ver §9.2: anclar esto
   en `Environment.requires_second_actor` lo haría **inalcanzable por construcción**, porque un
   grant global no tiene entorno cuyo flag consultar.
3. **Un usuario creado por un `access_admin` no puede recibir un grant aprobado por ese mismo
   `access_admin`.** Es el análogo de `approved_by != último editor del SQL` del §9.1.1. Sin él, el
   segundo aprobador se compra creando dos cuentas.

Sin los tres, `access_admin` es `owner` con dos requests: crear usuario, otorgarle `owner`,
autenticarse como él. **Y en la v1 el §8.1 le daba la credencial** — por eso `access_admin` nunca
fija ni resetea una credencial (§8.1).

### 4.5 El dato de política ES privilegio — y la lista completa

Regla general, que se descubrió con `Environment` y hay que escribir generalizada:

> **Toda fila que un guard lee es una frontera de privilegio, así que su escritor necesita al menos
> el privilegio del guard que puede apagar.**

**Y la lista tiene que estar COMPLETA, porque enunciar la regla y dejar una columna afuera es cómo
se abre el agujero.** Ésta es la tabla normativa: toda columna nueva que un guard lea se agrega acá
en la misma entrega.

| Columna | Qué guard la lee | Quién puede escribirla |
|---|---|---|
| **`managed_databases.environment_id`** | **TODOS**: `blocks_destructive_migrations`, `blocks_data_disclosure`, `requires_second_actor`, `allows_emergency_override` y **toda la capa 2** | **`security_officer`**, vía endpoint dedicado (§4.5.1) |
| `servers.environment_id` (§18.1) | la derivación del §4.3 | `security_officer` |
| `Environment.blocks_destructive_migrations` | el guard de migraciones | `security_officer` + segundo actor si debilita |
| `Environment.blocks_data_disclosure` | el eje `discloses` (§4.2) | ídem |
| `Environment.requires_second_actor` | la SoD (§9) | ídem |
| `Environment.allows_emergency_override` | el break-glass de SoD (§10.2.e) | ídem |
| `Environment.rank` | la derivación y el orden de alcance | `security_officer` |
| `Environment.is_default` | **cómo nacen las bases nuevas** — su propio comentario avisa que el default es *el más permisivo*, así que "nace clasificada" no equivale a "nace protegida" | `security_officer` |
| `charset_collation_options.is_active` | qué llega al motor (lo dice su docstring) | `security_officer` |
| `privileges.is_active` | qué se puede otorgar | `security_officer` |
| `permission_profiles.*` | **la plantilla de GRANTs**: quien la edita edita lo que se va a otorgar la próxima vez | `security_officer` |
| `servers.host`, `servers.root_*` | a qué máquina se conecta todo el mundo (§4.6) | `security_officer` + segundo actor si el servidor tiene bases en entorno bloqueante |
| `access_grants.*`, `global_capabilities.*` | el propio modelo de autorización | `access_admin` + **segundo actor incondicional** (§9.2) |

Todo lo de esta tabla va con auditoría **fail-closed** y con `previous_value`/`new_value`.

#### 4.5.1 La clasificación de una base sale de su CRUD — es un BLOQUEANTE, no una mejora

Hoy `environment_id` es un campo libre del cliente en los tres caminos:
`ManagedDatabaseCreate` (`app/schemas/managed_database.py:33`), `AdoptDatabaseIn` (`:88`) y
`ManagedDatabaseUpdate` (`:61`, donde *"enviar `null` DESCLASIFICA"*). Y en los dos primeros, **si
se omite se usa `is_default`, que es el entorno más permisivo**.

**El escenario, y es la escalada más grave de todo el modelo:** `adopt` **no ejecuta
`CREATE DATABASE`** — registra metadata de una base que **ya existe** en el motor. Así que un
`operator` con alcance `environment=dev` y `databases.write` **adopta la base de producción del
cliente clasificándola como `dev`**. Desde ese instante la capa 2 la resuelve a `dev`, donde él es
`owner`: `DROP DATABASE`, export en claro, consola SQL. No necesitó ningún grant sobre producción.
**Necesitó elegir un entero en un POST.**

Y la capa 2 no lo detiene: en un `adopt` **el objeto todavía no existe**, así que no hay
`environment_id` "cargado" contra el que evaluar (§4.7 es inaplicable a los `create`/`adopt`, que
son justo los que fijan la clasificación). La única lectura posible es la del entero que manda el
atacante.

**Las cuatro correcciones, y ninguna es opcional:**

1. **`environment_id` sale de `ManagedDatabaseCreate`, `AdoptDatabaseIn` y `ManagedDatabaseUpdate`
   como campo libre.** Clasificar y reclasificar pasa a `POST /managed-databases/{id}/classify`,
   capacidad `security_officer`, auditado fail-closed con `previous_value`/`new_value`.
2. **`create` y `adopt` nacen SIN entorno (`NULL`), nunca con `is_default`.** Con la regla del §4.5.2
   (`NULL` = rank máximo), **nacen protegidos** — que es exactamente lo que el comentario de
   `is_default` está pidiendo desde que se escribió.
3. **Reclasificar de mayor a menor rank** (producción → dev) exige **segundo actor**. Es la misma
   operación que apagar `blocks_destructive_migrations`, con otro nombre.
4. **Desadoptar exige `confirm_target_name`.** Verificado: `DELETE /managed-databases/{db_id}`
   (`app/routes/v1/managed_databases.py:115-127`) pide `confirm_name` **solo si
   `drop_remote=true`**; con el default, la fila del inventario se borra **sin ninguna
   confirmación**. En el modelo de hoy eso es metadata; en este modelo **es política**, porque
   mueve el entorno derivado del servidor y borra el ancla de la autorización por objeto. Es el
   segundo caso del patrón que el §2.1.c ya identificó.

#### 4.5.2 `NULL` significa UNA sola cosa, en todos los contextos

**`environment_id IS NULL` cuenta como el `rank` más alto, siempre.** A nivel base y **también en
la derivación por servidor del §4.3**.

Sin esta unificación había dos reglas contradictorias y la diferencia era una primitiva de
escalada: si las bases sin clasificar se excluyeran del mínimo del servidor, entonces
`PATCH {environment_id: null}` —"desclasificar", una operación *menos* privilegiada que dropear—
**bajaría el entorno derivado del servidor** y con él la consola SQL, `reveal-password` y
`DROP DATABASE` a nivel servidor. Desclasificar sería un downgrade de política disfrazado de
limpieza de metadata.

Con la regla única, la derivación del §4.3 se simplifica a **"el `rank` MÁXIMO entre las bases del
servidor, tratando `NULL` como el máximo global"**, y la cláusula "si ninguna está clasificada se
niega salvo `global`" deja de ser una regla aparte: es un caso particular. Un `if` menos y un
agujero menos.
### 4.6 `server.create/update` no es un CRUD: es otorgar poder

Registrar un servidor significa aportar una credencial pseudo-root y un host. Y quien puede
**editar** un servidor puede **re-apuntar un `server_id` existente a un host que controla**: desde
ese momento, **cada operación futura de todo operador con alcance se ejecuta contra su máquina**,
además de cosechar lo que el gateway le mande. El guard anti-SSRF (`net_guard.py`) limita
**dónde**, no **quién**.

Requisito: capacidad global (`security_officer`), step-up, auditoría (que hoy **no existe** en ese
controller, §2.1.b), y aprobación de segundo actor para cambiar host o credencial de un servidor
con BDs en entorno bloqueante.

### 4.7 Autorización a nivel OBJETO (IDOR) es un eje distinto del rol

Alcanzar por entorno o servidor no impide `GET /database-exports/{job_id}` de un job de otro
alcance si el handler carga por id y solo mira el rol. Con un administrador único esta clase de bug
es **invisible**; con roles es explotable de un día para otro. Hay ~15 recursos direccionados por
id (`ExportJob`, `CloneJob`, `SchemaComparison`, `CollationConversionBatch`, `ModelMigration`,
`QueryExecution`…).

**Requisito: la autorización se evalúa contra el `(server_id, environment_id)` del objeto
CARGADO, después de cargarlo, nunca contra los path params solos.** Y **404, no 403**, para objetos
fuera de alcance, así los ids no filtran existencia.

Y el bloqueo real: cuatro de esas tablas **no guardan autor** (§2.2.2), así que el ownership sobre
esos jobs exige una migración.

---

## 5. El catálogo, en CÓDIGO y no en tabla

`app/services/capability_catalog.py`, siguiendo el patrón `*_catalog.py` del repo.

```python
class Capability(StrEnum): ...           # servers.admin, engine_users.secrets, ...

@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    id: Capability
    module: str
    level: str
    label: str              # español, para la SPA
    mutates: bool           # §4.2 — ejes INDEPENDIENTES
    discloses: bool         # §4.2
    requires_step_up: bool
    requires_second_actor: bool
    scope_axis: Literal["global", "environment", "server"]
    agent_allowed: bool     # puede vivir en api_tokens.scopes (plan 12)

CAPABILITIES: tuple[CapabilitySpec, ...]
ROLE_CAPABILITIES: Mapping[GatewayRole, frozenset[Capability]]

def has(actor: Actor, capability: Capability) -> bool     # HACE CUMPLIR
def capability_matrix() -> list[dict]                     # PUBLICA
def parse_scopes(raw: str) -> frozenset[Capability]       # tokens del MCP
```

**Por qué en código y no en una tabla `role_capabilities`.** El argumento decisivo lo da el propio
repo: `app/services/privilege_catalog.py:39-41` hace `except Exception: logger.exception(...)` y
**el arranque sigue**. Eso es correcto para un catálogo informativo y es **inaceptable** para uno de
autorización: si el seed falla a medias hay dos finales y los dos son malos — si la ausencia de fila
deniega, es una caída total sin error visible en el arranque; si permite, es un agujero silencioso.
**Un mapeo en código no puede driftear.**

Tres razones más: una fila podría otorgar una capacidad que el código no contempla, y una capacidad
nueva del código no existiría hasta que corra un seed (lo publicado y lo hecho cumplir dejan de
derivar de la misma estructura *por construcción* y pasan a derivar de ella *si el seed corrió
bien*); 26 capacidades × 3 roles son 78 casillas de política de seguridad para un sistema con un
administrador, y el resultado conocido es todos en `admin`; y una consulta a la BD por request en
el camino caliente.

**Lo que se pierde, declarado: roles personalizados requieren deploy.** Con tres roles y alcance por
entorno el caso real entra; el día que haga falta un cuarto rol es una entrada en un `Mapping`,
revisada en un PR — que para política de autorización es *mejor* que un formulario. La costura para
cambiar de opinión es `ROLE_CAPABILITIES`: se cambia el proveedor sin tocar `has()`.

**Invariantes afirmados AL IMPORTAR** (no en un test que alguien puede no correr; fallar al importar
es fallar al arrancar, que es lo correcto para esto):

1. Cada `Capability` aparece en exactamente un `CapabilitySpec`.
2. **Monotonía:** `viewer ⊆ operator ⊆ owner`. Es lo que hace bien definido el `max` sobre
   alcances; sin esta afirmación, "rol máximo" no significa nada.
3. Ninguna capacidad con `mutates=True` **ni con `discloses=True`** está en `viewer`.
4. `mutates=True or discloses=True ⇒ requires_step_up=True`, o la excepción está listada y
   comentada.
5. `agent_allowed=True ⇒ not mutates and not discloses` — el techo de los tokens del plan 12.

**Formato del identificador: `modulo.accion`, con punto.** Es seguro en URL, query string, clave
JSON y CSV (relevante: `api_tokens.scopes` del plan 12 es un `String(255)` separado por comas), y
el punto ya es el separador de los vocabularios jerárquicos del repo (`public_context["code"]`). **El
valor se declara explícito, no se deriva del nombre del miembro**: `ENGINE_USERS_WRITE` derivaría
`engine.users.write`, que es incorrecto — derivar es el tipo de astucia que rompe en el sexto
miembro.

### 5.1 Un solo modelo para humanos y máquinas

`Actor.capabilities: frozenset[Capability]` se computa **una vez, al autenticar**, por dos
resolvedores distintos con el mismo tipo de salida:

- `kind="admin"` → `ROLE_CAPABILITIES[union_role(base, overrides)]`
- `kind="api_token"` → `parse_scopes(token.scopes) & AGENT_ALLOWED`

La intersección no es defensiva por gusto: **una fila de `api_tokens` manipulada o legada nunca
puede otorgar una capacidad fuera del techo de agente**, incluso si el string lo dice. Fail-closed
en el lector, no solo en el escritor.

`require()` solo pregunta `capability in actor.capabilities`. **Una comprobación, un vocabulario.**
`Actor.role` no es un segundo eje: es *entrada* al conjunto de capacidades para humanos, como
`scopes` lo es para máquinas. El día que llegue OIDC, cambia el resolvedor y nada más.

Y esto **reemplaza** al guard de `is_superuser` que el plan 12 §7.6 propone como puente: el CRUD de
`api_tokens` queda detrás de `Capability.GATEWAY_ADMIN`.

---

## 6. Dónde vive el chequeo

### 6.1 Descartes, con el motivo

**Middleware con tabla path→capacidad: NO.** Dos razones y la segunda es dura. La primera: sería una
segunda tabla de routing que diverge de la real, y su modo de fallo por default es **permitir** lo
que no matchea. La segunda, medida: **`/database-models` lo sirven CUATRO archivos**
(`database_models.py`, `model_migrations.py`, `collation_batches.py`, y el `model_router` de
`projects.py`) y **`/servers` lo sirven TRES**. Un mapeo por prefijo mezclaría "leer un blueprint"
con "convertir la collation de N bases". El mapa tiene que ser **ruta + método → capacidad**, no
prefijo. Y el repo ya se quemó con middlewares y sub-apps montadas: el docstring de
`PathScopedCORSMiddleware` (`versioned_app.py:49-73`, la frase en `:58`) documenta que *"el middleware del ASGI externo
siempre corre antes que el routing interno que resuelve el mount"* — un middleware de autorización
no sabe qué ruta se va a resolver, solo el path crudo.

**Decorador: NO.** No participa del grafo de dependencias de FastAPI: no aparece en OpenAPI, no es
enumerable para construir `/auth/me`, y **un decorador no aplicado es invisible**. Es la misma falla
de disciplina de hoy con otra sintaxis.

**Solo en el controller: NO como capa primaria.** Los controllers se llaman desde rutas, desde
workers y (plan 12) desde el dispatch del MCP. Pero es el **único lugar donde el objeto resuelto —y
por tanto el entorno— se conoce**: de ahí la capa 2.

### 6.2 Lo elegido: dependencia por endpoint, como PARÁMETRO

```python
# app/core/authz.py
def require(capability: Capability) -> Callable[[Request], Actor]: ...   # fábrica

# Alias públicos — lo ÚNICO que la capa de rutas importa
ServersRead  = Annotated[Actor, Depends(require(Capability.SERVERS_READ))]
ServersAdmin = Annotated[Actor, Depends(require(Capability.SERVERS_ADMIN))]
```

En cada endpoint, `admin: AdminDep` → `actor: ServersAdmin`. **Parámetro y no `dependencies=[...]`**
por una razón medida: los **227 sitios** que hoy pasan o desestructuran el dict `admin`
necesitan el objeto en mano para auditar. (El acceso por clave está concentrado en `audit.py` y en
`export_controller.py:2557`; en las rutas y controllers lo que circula es el dict entero.) Con `dependencies=[]` habría que inyectar *además* un `ActorDep` sin capacidad — y ese
alias sin capacidad es precisamente el atajo que no hay que tener.

`AdminDep` **se elimina, no se deprecia.** Un endpoint nuevo copiado de uno viejo falla al importar.

**Y la red de seguridad del refactor de los 227 sitios:** `Actor` es `frozen`, `slots=True`, **sin
`.get()` y no subscriptable**. Cada sitio olvidado **explota en la suite**, no en producción. Por eso
**no hay shim de compatibilidad**: con un `Actor.get()`, un `actor.get("id")` mal escrito devuelve
`None` y el `audit_log` queda con `admin_id` nulo — un agujero de auditabilidad **silencioso**, que
es peor que un crash.

### 6.3 Deny-by-default: la misma regla, en tres lugares

Un endpoint escrito de cero puede simplemente **no tomar ningún parámetro de actor**. Eso no lo
resuelve ningún nombre. Una sola implementación de la regla, en
`scripts/check_route_capabilities.py` (misma familia que `scripts/check_migration_graph.py`:
importable sin BD y sin `.env`).

**La regla:** recorrer las rutas reales del sub-app, aplanar `route.dependant` (recursivo: una
dependencia puede tener sub-dependencias) buscando el marcador que `require()` estampa en el
callable (`func.__gw_capability__` — Python permite atributos en funciones, así que el marcador viaja
con la dependencia sin tocar el modelo de FastAPI), y afirmar:

1. Toda ruta declara **exactamente una** capacidad, **o** está en `PUBLIC_ROUTES` — un
   `frozenset[(method, path)]` explícito.
2. Toda entrada de `PUBLIC_ROUTES` corresponde a una ruta viva. Sin esto la lista se podre en un
   comodín acumulado.
3. Toda capacidad declarada es miembro del enum.
4. Todo miembro del enum lo usa al menos una ruta, **o** está en `NON_ROUTE_CAPABILITIES` (tools del
   MCP, workers). Impide vocabulario muerto que `/auth/me` publicaría como promesa.
5. **Toda ruta GET está en una allowlist de solo-lectura** — el invariante que hoy no se sostiene
   (§7.2).
6. **Toda ruta declara exactamente UN resolvedor de destino**, o está en `SCOPE_EXEMPT` (mismo
   ratchet decreciente). **Éste es el punto que hace honesta la capa 1 laxa del §6.4.** El destino
   no es un argumento de función, es una **dependencia declarada**
   (`TargetDatabase = Annotated[Target, Depends(resolve_target_database)]`, que estampa
   `__gw_target__`), así que la ausencia de capa 2 se detecta con el MISMO `route.dependant`
   aplanado que ya se recorre. Sin esto, "la capa 2 no es salteable" se apoya en `TypeError` en la
   suite — que **no es un invariante estructural, es una apuesta a la cobertura de línea**: un
   keyword-only obligatorio solo explota si un test ejecuta esa rama, y el endpoint 151 con
   capacidad declarada y sin resolver destino operaría con el rol **máximo** del actor sobre
   **cualquier** destino.

**Los tres puntos donde corre la misma función:**

- **CI**, en el job de lint (rápido), no en el de tests: por la regla de WSL nadie corre `pytest`
  localmente, así que el gate tiene que estar donde sí corre siempre. *Caveat conocido:* en este
  clon `core.hooksPath` apunta a un `.git/hooks` vacío y **ningún hook corrió nunca**, así que CI es
  hoy el único gate real. Arreglarlo o no contarlo.
- **pytest** (`tests/test_route_capability_coverage.py`), para que el fallo aparezca con el diff que
  lo causó.
- **Una DEPENDENCIA GLOBAL de la sub-app, evaluada en runtime** — no un assert de arranque:

  ```python
  def assert_declared(request: Request) -> None:
      route = request.scope.get("route")          # FastAPI ya resolvió la ruta acá
      if not _has_capability_marker(route) and _key(route) not in PUBLIC_ROUTES:
          raise AppHttpException("No autorizado.", 403,
                                 public_context={"code": "access.undeclared_route"})
  ```

  Da la propiedad que se busca —**un endpoint sin capacidad no puede servir una response**— sin los
  tres modos de fallo del assert de arranque: (i) **el conjunto de rutas depende de variables de
  entorno** (`DOCS_ENABLED` ya monta y desmonta rutas, el router del MCP del plan 12 va a ser
  condicional, y si `test.py` se condiciona a `APP_ENV` según el §18.3 entonces **desarrollo no
  arrancaría**, porque el §2.1.a prohíbe meterlo en la allowlist), o sea que un assert cuyo
  resultado depende del entorno **no es verificable en CI**; (ii) las rutas registradas después del
  `lifespan` y las sub-apps montadas quedarían **invisibles** al inventario, con el agravante de
  que el assert da la sensación de que eso es imposible; (iii) un crashloop en un reinicio **no
  relacionado** (OOM kill, drain de nodo, scale-up) deja un pod que no puede volver con el binario
  que andaba bien hace un minuto — y el argumento "el repo ya tolera crashloop por migración mal
  encadenada" no transfiere, porque esa condición es determinista y no depende del entorno.
  Una dependencia global corre **después** del routing (a diferencia del middleware del §6.1, que
  es la razón por la que el middleware se descarta), así que cubre rutas dinámicas y mounts por
  construcción. El chequeo en el `lifespan` queda como **log de error ruidoso**, no como aborto.

### 6.4 Las dos capas, explícitas

| | Dónde | Con qué rol | Qué responde | Si falta |
|---|---|---|---|---|
| **Capa 1 — capacidad** | dependencia del endpoint | `union_role` (**máximo** sobre alcances) | "¿podría, en algún alcance?" | el proceso no arranca |
| **Capa 2 — alcance + objeto** | resolvedor de destino, **keyword obligatorio sin default** | `effective_role(env_id)` | "¿puede en ESTE destino?" | `TypeError` en la suite |

La capa 1 con el **máximo** y no con el mínimo es deliberado: con el mínimo, "lector en producción"
degradaría también el trabajo en desarrollo, que es justo el caso de uso. La contrapartida —la capa
1 es más laxa que la política real— es aceptable **solo** porque la capa 2 no es salteable, y esa
no-saltabilidad la da el parámetro obligatorio, con el patrón textual de
`export_controller._validate_scope:478`.

**Y la capa 2 es donde vive el chequeo por destino de las operaciones multi-destino.** `apply-all`
cruza entornos en un solo request: el chequeo es **por destino**, y la negación por destino es un
rechazo por destino, no un 403 global — el controller ya responde 200 con rechazos por base y copia
el código a `item["error_code"]`; la autorización se enchufa en esa misma forma.

**Regla para `environment_id IS NULL`:** **no resuelve al entorno `is_default`**, porque el propio
`environment.py` advierte que el default es *el más permisivo*. Un `NULL` resuelve al entorno de
`rank` más alto (el más protegido) para cualquier rol que no sea global. Fail-closed. Costo
operativo real: el día del encendido de la capa 2, un `operator` pierde acceso a toda base sin
clasificar — **por eso la capa 2 va en una fase posterior, precedida de un reporte de
reconciliación** de `environment_id IS NULL` (§14, fase 2).

### 6.5 Los 21 endpoints que operan fuera del inventario

`/servers/{sid}/databases/*` y `/servers/{sid}/users/*` identifican la base por **referencia
cruda** (`server_id` + nombre), sin fila de `managed_databases`. Son **21 rutas**, y el detalle que
importa: **18 viven en `servers.py`, pero 1 está en `collation_conversions.py` y 2 en
`database_exports.py`**. O sea que la superficie fuera del inventario **no coincide con un
prefijo ni con un archivo** — es otra razón, independiente de la del §6.1, por la que el mapeo
tiene que ser ruta + método.

Y **no es un descuido**: el docstring de `database_exports.py:17-19` lo declara como decisión
(*"la BD se identifica por IDENTIDAD FÍSICA… funcione o no adoptada"*).

Consecuencia para este plan: **no hay fila donde colgar autorización por objeto ni por entorno.** El
alcance más fino posible ahí es `server_id`, y la derivación de §4.3 (el entorno más restrictivo del
servidor) es lo único que aplica. Hay que escribirlo, no descubrirlo: publicar en `/auth/me` un
alcance por entorno que no rige sobre estos 13 es la promesa incumplida que el repo prohíbe.

### 6.6 Fuga entre módulos por endpoints que devuelven datos de otro

`GET /server-users/{uid}/databases` devuelve `ManagedDatabaseOut`; `GET /projects/{id}/blueprints`
devuelve `DatabaseModelOut`; `GET /database-models/{id}/databases`, `GET /servers/{sid}/reconcile` y
`/users/grouped` cruzan motor e inventario. Si la capacidad se resuelve solo por endpoint,
**`blueprints.read` se puede eludir vía `projects.read`.**

Regla: la capacidad de un endpoint es la del **dato más sensible que devuelve**, no la del módulo
donde vive el archivo. Y en el sentido de escritura, `schema-comparisons/adopt` y
`collation-…/blueprint-version` **crean versiones de blueprint desde otro módulo**: exigen
`blueprints.write` además de la propia.

---

## 7. Sesiones, CSRF y login

### 7.1 Sesiones server-side: prerequisito, no mejora

Verificado leyendo el `SessionMiddleware` instalado
(`.venv/lib/python3.13/site-packages/starlette/middleware/sessions.py:57-71`): si
`scope["session"]` no está vacío, la cookie se **re-firma con timestamp nuevo en CADA respuesta**, y
`unsign(max_age=...)` valida contra ese timestamp fresco.

**Consecuencias, y son cuatro controles inimplementables tal como está:**

- `SESSION_MAX_AGE=28800` es **timeout de inactividad puro**. Con actividad continua, **la sesión no
  expira nunca**. No hay tope absoluto.
- **`logout` no invalida nada.** `session.clear()` borra la cookie *del cliente*; quien tenga una
  copia sigue autenticado. Cambiar la password tampoco invalida.
- No hay límite de sesiones concurrentes ni forma de listarlas.
- El único kill switch real es `is_active=False` — y ese **sí** se relee por request, que es la
  propiedad buena a conservar.

**Diseño: tabla `gateway_sessions`** con `sid` opaco (128 bits), `user_id`, `created_at` (**el ancla
del absoluto**), `last_seen_at`, `ip`, `user_agent_hash`, `revoked_at`, `revoked_reason`,
`reauth_at`. **La cookie firmada lleva solo el `sid`.** Un cambio compra: absoluto, logout real,
revocación al cambiar rol, revoke-all al cambiar password, tope de concurrentes, lista de sesiones
en la SPA, y correlación forense con `audit_log`.

- **Absoluto** `SESSION_ABSOLUTE_MAX_HOURS` (default 12) contra `created_at`; **inactividad**
  `SESSION_IDLE_MINUTES` (default 60 — 8 h es mucho para una herramienta con pseudo-root).
- **Rotación del `sid`** en login, en cambio de privilegio y en step-up.
- **`login_session` debe hacer `request.session.clear()` ANTES de escribir.** Hoy no lo hace
  (`auth.py:28-31`), mientras `logout_session:35` y `get_current_admin:49` sí. Es inocuo mientras la
  sesión guarde solo dos claves; **este plan mete un marcador de step-up y un flag de 2FA
  pendiente**, y ahí un valor plantado sobrevive al login. Fix de una línea, se puede hacer ya,
  independiente de todo el resto.
- **El rol NUNCA va en la cookie.** Se lee de la BD por request, igual que `is_active`. La cookie
  está firmada, no cifrada — pero el problema no es la confidencialidad: **un rol en la cookie es un
  rol que no se puede revocar**, y con el sliding puede no expirar nunca. Guard estructural:
  decodificar el payload sin la firma y afirmar que las claves son solo las mínimas definidas.
- **`SESSION_COOKIE_SECURE=False` en producción es hoy solo un WARNING.** Con multiusuario tiene que
  ser **error de arranque**, o como mínimo negar toda capacidad `owner` y destructiva mientras esté
  apagado. Una cookie sin `Secure` en una red compartida es todo el sistema de autorización.
- **Renombrar la cookie a `__Host-gw_session`** cuando `SESSION_COOKIE_SECURE` (requiere `path=/`,
  `Secure`, sin `Domain` — las tres ya se cumplen). Fija la cookie al host exacto y evita que un
  subdominio hermano la sobrescriba.
- `SESSION_SECRET` cae a `SECRET_KEY` y después al literal `"insecure-dev-session-secret"`. En
  producción hay guard; **en staging no**.

### 7.2 CSRF: `same_site="lax"` no alcanza

**Dos huecos, verificados:**

**(a) Lax es same-*site*, no same-origin.** Si el panel vive en `panel.midominio.com` y cualquier
otra cosa de la organización en `*.midominio.com` sufre XSS o queda dangling, **ese origen puede
hacer POST/PATCH/DELETE con la cookie del admin adjunta**. Para una herramienta con pseudo-root
sobre la producción de terceros, es compromiso total vía un sitio de marketing.

**(b) Hay GETs que MUTAN.** `GET /database-exports/{job_id}/download`
(`database_exports.py:321`, docstring en `:337`) con `EXPORT_SINGLE_USE_DOWNLOAD` **consume y borra
el artefacto** en un `BackgroundTask` (`export_controller.py:2481`). Y Lax **sí** manda la cookie en
navegación GET de primer nivel: un `<img>` en una página maliciosa **destruye el export del
cliente** (denegación + pérdida forense) y fuerza que la entrega quede registrada contra el admin.

**Diseño, las cuatro cosas y no una:**

1. **El token es `HMAC(SESSION_SECRET, sid)`, y se RECOMPUTA server-side.** No double-submit puro:
   comparar cookie contra header lo derrota **el mismo atacante que motiva la defensa** — un
   subdominio hermano puede escribir cookies en el dominio padre (*cookie tossing*:
   `document.cookie = "gw_csrf=X; domain=.midominio.com"`) y después mandar `X-CSRF-Token: X`.
   Coinciden, y pasa. Recomputando el HMAC del `sid` de la sesión, un valor plantado no valida:
   el atacante no conoce el `sid` (es httpOnly) ni el secreto. La cookie `__Host-gw_csrf` (no
   httpOnly, `SameSite=Strict`, `Secure`) es **solo transporte** para que el JS lo lea, y el prefijo
   `__Host-` impide el tossing de entrada. **Las dos cosas, no una.**
2. **Chequeo de `Origin`** en todo método no seguro, dentro de `CORS_ORIGINS`, fail-closed en
   producción — **pero SOLO para `actor.kind == "admin"`**. CSRF es un ataque contra la
   **autenticación ambiental** (la cookie que el navegador adjunta solo); un `Authorization: Bearer`
   no es ambiental, así que las defensas CSRF **no aplican y no deben aplicarse** a esos requests.
   Sin esa condición, el día que se enciende, **todo cliente programático recibe 403 en producción**:
   el CI, `curl`, y el dispatch del MCP del plan 12.
   Y el invariante correlativo, con test: **un `api_token` nunca autentica vía cookie y una cookie
   nunca autentica vía `Authorization`** — si los dos caminos se pueden mezclar, la exención se
   vuelve el bypass.
3. **Arreglar la violación de GET-muta.** Mover el consumo a `POST …/download-ticket` → ticket de un
   solo uso y TTL corto → `GET …/download?ticket=`, que ya no se autoriza solo por cookie.
4. **El guard del §6.3 punto 5** mantiene el invariante para el endpoint 151.

### 7.3 Login

- **Oráculo de timing, vivo.** `auth_controller.py:18-22` cortocircuita con `or`: si el usuario no
  existe, **`verify_password` no se llama**. Inexistente ≈1 ms, existente ≈50-300 ms por Argon2id.
  El mensaje es correctamente genérico; **el tiempo delata**, y es medible con curl. Fix:
  `_DUMMY_HASH` precomputado a nivel de módulo y **siempre** ejecutar `verify_password` (contra el
  real o contra el dummy), evaluando el booleano al final. Cubrir también la rama `is_active`, que
  hoy también cortocircuita y delata una cuenta deshabilitada.
- **Throttling, no lockout puro.** El lockout es un arma de disponibilidad: cualquiera que conozca
  un username puede bloquear al único `access_admin` **exactamente en el momento en que también
  provoca la incidencia**. Híbrido: throttle exponencial **por cuenta** (0,0,0,1s,2s,4s…, tope
  ~60 s); lock temporal de 15 min solo tras un umbral alto (20 fallos/15 min), autoexpirable, nunca
  permanente, y **nunca aplicable al último `access_admin` activo** — para esa cuenta, solo throttle.
- **Por IP *y* por cuenta.** Por IP solo es lo que existe y es evadible; **por cuenta solo deja
  pasar el password spraying** (una password contra todos los usuarios) sin tocar ningún contador de
  cuenta. Más un circuit breaker global de tasa de fallos que **ensancha throttles y alerta**, nunca
  bloquea a todos.
- El 429 **no debe distinguir** "throttled por cuenta existente" de "por IP".
- **Auditar la autenticación.** Hoy no existe ninguna acción `auth.*` entre las 77 del vocabulario.
  Hacen falta `auth.login`, `auth.logout`, `auth.login_failed`, `auth.login_throttled`.
- Exponer `last_login_at` / `last_failed_at` en `/auth/me`, y avisar tras N fallos.

### 7.4 Rate limit por usuario

`key_func=get_remote_address` (`limiter.py:7`). Con N usuarios detrás de una salida NAT **comparten
los 3/min del `DROP DATABASE`**. El eje correcto pasa a ser el usuario.

Dos agravantes que hay que cerrar y son bloqueantes para lanzar login multiusuario:

- **`--forwarded-allow-ips "*"`** permite spoof de `X-Forwarded-For` por request ⇒ el límite por IP
  es **ficticio hoy**. Acotarlo al CIDR del proxy.
- **`RATE_LIMIT_REDIS_ENABLED` default `False`**: con N workers y `memory://`, el límite efectivo es
  N × el configurado. Una vez que exista throttling de login, **multi-worker sin storage compartido
  debe negarse a arrancar** — mismo criterio que el guard de `SESSION_SECRET`. Un límite que la
  gente cree global y no lo es, es peor que ninguno.

Y **`reveal-password` gana rate limit** (§2.1.d): hoy no tiene ninguno.

---

## 8. Ciclo de vida de la cuenta

### 8.1 La password inicial NO la pone el admin

*Escenario:* si un admin tipea la password inicial de otra persona, conoce una credencial funcional
de esa identidad — y por lo tanto **toda fila de `audit_log` atribuida a ese usuario es
repudiable**. Para un sistema cuyo valor central es el rastro, eso es fatal. Y **"cambio forzado en
el primer login" no lo arregla**: el admin pudo haber entrado antes.

**Diseño:** `POST /gateway-users` crea en estado `pending_invite`, **sin password**; token de
invitación de un solo uso con TTL 48 h (patrón HMAC de `confirm_token` sobre
`(user_id, credential_epoch, exp)`); el primer uso fuerza password (+ 2FA cuando exista).

**Y si no hay canal de entrega (§8.4), la salida NO es que el admin ponga la password.** Ese
fallback se elimina del plan: con `access_admin` en el modelo, "el admin conoce una credencial
funcional de una identidad `owner` recién creada" no es solo una brecha de repudio, **es la vía de
escalada del §4.4**. El camino correcto ya está diseñado en este mismo documento para el 2FA
(§11.2): un **código de enrolamiento de un solo uso entregado verbalmente**. Resuelve idéntico este
caso, no necesita canal, y el admin que lo genera **no obtiene ninguna ventana en la que la cuenta
sea usable**. La columna `credentials_set_by_admin_at` desaparece; la que sí hace falta es
`credential_epoch` (§14), que es el mecanismo de un solo uso del token de invitación.

### 8.2 Desactivar, nunca borrar

`UserModel.delete()` es un `DELETE` duro hoy. La historia sobrevive porque `audit_log` desnormaliza
`admin_username` (decisión deliberada, sin FK), pero **borrar libera el username para reuso** y una
persona nueva hereda la apariencia de las filas viejas.

Reglas: (i) prohibido el hard delete mientras exista una fila de auditoría que referencie el id;
(ii) **el username no se reusa jamás**; (iii) `is_active=False` es el interruptor real y ya toma
efecto en el request siguiente.

### 8.3 Huérfanos, uno por uno

- `ExportJob.created_by_admin_id`, `CollationConversionBatch.created_by_admin_id`,
  `QueryExecution.admin_id` son `Integer` **sin FK, a propósito**. Correcto: solo hay que renderizar
  `usuario eliminado (#id)` y no reventar en el join ausente.
- **Los tokens de API emitidos por esa persona (plan 12): la desactivación FALLA con 409 y enumera
  los tokens.** Un token es una **delegación** de la autoridad del emisor, así que no puede
  sobrevivirlo sin decisión — si no, el offboarding es cosmético. Pero una **cascada silenciosa** es
  peor: revoca el token del CI de tres repos un lunes a la mañana, sin canal para avisar (§8.4), y
  en la práctica **casi todos los tokens van a colgar del admin único de hoy**, así que desactivar
  esa cuenta —lo primero que se hace al terminar la migración a usuarios nominales— revocaría todos
  los tokens del sistema de una vez.
  Se reusa el patrón del §10.2.a: el 409 **enumera** los tokens (nombre, último uso, scopes) y el
  operador elige explícitamente `?transfer_tokens_to=<user_id>` o `?revoke_tokens=true`. Ningún
  canal de notificación hace falta: la lista está en la respuesta.
  Dos detalles a escribir: **`revoked_at` no se deshace al reactivar la cuenta**, así que un ciclo
  offboard/onboard rompe el CI de forma permanente y silenciosa; y un token **transferido** cambia de
  dueño, así que su `audit_log` necesita registrar la transferencia o la atribución histórica queda
  ambigua.
  (Y el techo del daño está acotado por el invariante 5 del §5: un token nunca puede tener una
  capacidad `mutates` ni `discloses`, así que la delegación sobreviviente es de solo lectura no
  divulgante.)
- **Jobs en curso: desactivar a alguien NO los cancela** (matar una migración a mitad es peor que
  dejarla terminar), **pero toda continuación que requiera una decisión de autorización nueva se
  niega**: job encolado sin arrancar, aprobación pendiente, descarga de artefacto. Concreto:
  `prepare_download` chequea el acceso del **actor actual**, no el del creador del job.
- **Aprobaciones pendientes** firmadas por un usuario desactivado quedan nulas.
- **Grants con alcance a un objeto borrado**: borrar un `Server` o un `Environment` cascadea sus
  grants, o un id recreado re-otorga en silencio.

### 8.4 No hay sustrato de notificación, y la mitad de los controles lo asume

Alerta de break-glass, reset de 2FA out-of-band, aviso de logins fallidos, entrega de invitaciones,
acknowledgement de un override: **nada de eso tiene canal hoy.**

O se acepta y **cada control se diseña para funcionar sin canal** (aprobaciones in-app, códigos de
enrolamiento entregados verbalmente, banner de override en la UI) — que es lo que hace este
documento — o "un canal de salida" pasa a ser prerequisito explícito. **Lo que no se puede es
diseñar controles que asuman email y después shipearlos inertes.**

---

## 9. Separación de deberes

El repo **ya descubrió el problema y lo escribió**: el consentimiento por corrida de la captura de
`SELECT` se retiró porque *"no había un segundo par de ojos, solo un segundo momento"*. La premisa
era falsa **porque hay un administrador único**. Con roles deja de ser falsa.

Y el mismo docstring dice qué **no** hacer: ese consentimiento **no dejaba rastro** y `apply_all` lo
contradecía con un query param. Cualquier SoD que repita esos dos defectos hay que retirarla igual.

### 9.1 Dónde exigir aprobador ≠ ejecutor

Solo donde compra algo real. **Poner SoD en todas partes entrena el reflejo "siempre que sí"** — y
ese reflejo después se aplica también en producción. Es el argumento textual del repo.

1. **Aplicar o revertir una versión de blueprint** sobre una base de un entorno con
   `blocks_destructive_migrations`. Con **dos** invariantes, no uno: `approved_by != requested_by`
   **y** `approved_by != último editor del SQL de esa versión`. Sin el segundo, el autor aprueba su
   propio código — que es el mismo modo de fallo que el plan 12 §2.4 encontró con el nivel `author`.
2. **Otorgar o elevar acceso al gateway.** Nunca self-grant. Elevar a `owner` o a una capacidad
   global exige segundo aprobador.
3. **`reveal-password` y export de datos en claro** contra un entorno con
   `blocks_data_disclosure`.
4. **Escribir datos de política** (§4.5): apagar `blocks_destructive_migrations`, cambiar host o
   credencial de un servidor con bases en entorno bloqueante.
5. **`DROP DATABASE` / `DROP USER`** sobre destinos de entorno bloqueante.

**Dónde NO:** `POST /admin/crypto/rotate` (no daña a terceros; un actor + auditoría alcanza), nada
de lectura, nada en desarrollo ni staging.

### 9.2 Cómo se implementa sin ser inaplicable en un equipo de tres

- **La regla de dos personas se activa por POLÍTICA DE ENTORNO, no globalmente.**
  `Environment.requires_second_actor` — un flag **nuevo**: no está entre los cuatro que el
  docstring de `environment.py:20-25` dejó diferidos. Lo que se hereda de ahí es la **regla de cero
  flags inertes**, y alcanza para el argumento: entra **con su guard en esta misma entrega**,
  porque un booleano que la API expone y nadie hace cumplir, la SPA lo pinta como un control
  activo. Solo producción lo prende.
- **La aprobación es un artefacto durable y estrecho, no un motor de workflow.** Extiende
  `confirm_token.py` pero **no puede ser stateless**: hay que persistir la segunda identidad. Tabla
  `access_approvals`: `(operation, target_fingerprint, requested_by, approved_by, justification,
  expires_at, consumed_at)`.
- **`target_fingerprint` = hash de exactamente lo que va a correr** (checksum de la versión +
  `db_id` + plan renderizado). Así **la aprobación se auto-revoca si el plan cambia** — la propiedad
  que hizo sobrevivir a `_guard_reviewed_capture` mientras el flag de consentimiento se retiraba
  (*"aprueba una CONSULTA concreta y se revoca sola si el SQL cambia"*). Es el criterio del repo, no
  uno nuevo.
- **Un solo uso** (`consumed_at`), TTL corto (30 min, configurable por entorno), atada a **UN**
  destino. `apply-all` necesita N aprobaciones, o una de lote que **enumere** las N bases y sus
  versiones. **Nunca "aprobá el lote, el contenido puede variar"** — ése es el defecto exacto que el
  repo le achaca al query param retirado.
- **Todo se audita, incluida la negación** (`access.approval_missing`, `status="denied"`). Un gate
  que niega en silencio no se distingue de uno que nunca corrió.
- **El aprobador tiene que ser una IDENTIDAD distinta, no un click distinto.** El precedente está en
  el `CLAUDE.md`: `force` es *"un `Switch` sin fricción en la SPA"*, y por eso no saltea el guard de
  entornos.

### 9.3 Step-up: es un atributo de la capacidad, no un segundo sistema

`confirm_token._sign` ya firma `(operation, server_id, db_name, exp[, subject])`, y **`subject` es
el punto de composición** — su docstring explica que la consola SQL lo usa para atar el token al
hash del SQL.

1. **Corregir el defecto de §2.2.1 de paso:** el token no está atado al actor.
   `verify(token, operation, server_id, db_name, *, actor, capability)` con `actor` y `capability`
   **keyword-only y sin default**. Patrón `_validate_scope:478`: un llamador nuevo que olvide atar
   el token al actor es un `TypeError` en la suite, no un token replayable.
2. `POST /auth/step-up` con `{password, capability, server_id, database}`, rate-limitado,
   `record_intent` **fail-closed antes de emitir**, TTL **60 s** (más corto que el default de 120).
3. **Qué operaciones lo piden se declara en `CapabilitySpec.requires_step_up`**, no en el handler.
   `require()` exige la **presencia** del token; el controller, con el destino en mano, exige el
   **binding** vía `verify`. Dos mitades, ninguna opcional.
4. **Se publica** en `/auth/me`, para que la SPA pida la contraseña *antes* de mandar la operación.
5. **Orden: capacidad primero, step-up después.** Nunca revelar "podrías si te reautenticaras" a
   quien no puede — eso filtra la matriz de política a un actor sin permiso.
6. **Se acumula con la doble intención por nombre**, no la reemplaza: el nombre obliga a identificar
   *cuál* objeto; el token da frescura y anti-replay; el step-up agrega *quién* y *para qué
   capacidad*. El docstring de `confirm_token.py` ya dice "COMPLEMENTA —no reemplaza—".

---

## 10. Break-glass

### 10.1 Por qué NO "la variable de entorno siempre gana"

1. **Convierte una superficie de configuración en un bypass de autenticación.** Quien pueda
   reiniciar el proceso o editar el entorno (acceso al orquestador, a Docker, un CI comprometido) se
   vuelve administrador sin autenticarse y sin dejar rastro autenticado.
2. **Obliga a que la password viva permanentemente en el entorno del proceso**: legible por
   `/proc/self/environ`, por un crash dump, por `docker inspect`.
3. **Rompe la desactivación como control.** Si el arranque re-afirma rol y password, desactivar a
   alguien es reversible **por reinicio** — y el reinicio es la operación más común del mundo. El
   control deja de existir y nadie se da cuenta.

### 10.2 Diseño, en cuatro piezas

**(a) Invariante en la capa de datos:** *siempre debe existir al menos un usuario activo con
`access_admin` global*. Toda operación que lo violaría (desactivar, borrar, revocar el último grant,
degradar al último) se rechaza 409 `access.last_admin_protected`. Esto elimina la mayoría de los
escenarios de bloqueo antes de necesitar recuperación.

*Escenario que cubre:* dos admins, A le revoca el admin a B "para ordenar", después se desactiva a
sí mismo por error. Hoy: bloqueo total, salida solo por SQL manual contra la BD de metadatos, hecho
en plena incidencia por alguien con pseudo-root y sin auditoría.

**(b) `bootstrap_admin` se re-ancla al INVARIANTE, no al username.** Hoy hace
`if find_by_username(ADMIN_USERNAME): return` (`auth.py:72-73`), así que si el admin se renombra o
se desactiva **el seed no repara nada**. Nuevo criterio: **si hay cero usuarios activos con
`access_admin` global**, sembrar/reparar desde `ADMIN_USERNAME`/`ADMIN_PASSWORD` y auditar como
`access.bootstrap_recovery` con `record_intent`. **Nunca** tocar la password ni el rol de un admin
existente. Misma idempotencia, keyed en la condición que importa.

**(c) Un segundo camino, OFFLINE:** CLI `uv run python -m app.cli.breakglass --username X`. Exige un
**secreto de un solo uso en un ARCHIVO** que el operador crea y la herramienta borra. **Por qué
archivo y no variable de entorno:** una env var es ambiente y persiste toda la vida del contenedor;
un archivo que alguien crea y la herramienta consume es una capacidad **con principio y fin**, y su
creación es visible en la auditoría del host. Crea un **grant de recuperación acotado en el tiempo**
(30 min), no un admin permanente; fuerza re-enrolamiento; audita fail-closed; y se niega a correr
con `APP_ENV=production` sin un flag extra explícito.

**(d) Break-glass NUNCA por HTTP.** No hay forma de distinguir "el operador" de "alguien que leyó el
entorno en una config de deploy filtrada".

**(e) Break-glass de la SoD** (equipo de tres, dos de vacaciones): `emergency_override=true` +
`justification` con largo mínimo, aceptado solo si `Environment.allows_emergency_override`;
`record_intent` fail-closed con `override=true`; rate-limit por semana por usuario; y **deja la BD
marcada** para que el próximo login de cualquier otro usuario muestre un banner. **Un break-glass
que no hace ruido es el camino normal.** No se implementa como un flag de config que apaga la SoD en
silencio.

---

## 11. 2FA, y su reset

### 11.1 La amenaza precisa

`access_admin` resetea el TOTP de un `owner`, enrola su propio autenticador, y opera en producción
**con la identidad de la víctima**. Incluso sin la password: el reset **quita un control de la
cuenta de otro en silencio**, y el `DROP DATABASE` posterior queda atribuido a la víctima. En un
sistema cuyo único control compensatorio de un log no append-only es que la atribución sea
confiable, eso es fatal.

### 11.2 Controles

- **Nadie resetea el 2FA de otro. Nunca.** El reset se modela como *"invalidar las credenciales de
  la cuenta"*, no como *"dame acceso a la cuenta"*: un `access_admin` solo puede poner al usuario en
  `credentials_reset_required`, que (i) revoca todas sus sesiones, (ii) revoca sus tokens de API,
  (iii) **bloquea el login por completo** hasta que el propio usuario complete el re-enrolamiento.
  **La cuenta queda congelada, no usable-sin-2FA.** Esa distinción es todo el control.
- Sin canal out-of-band (§8.4), el re-enrolamiento exige **dos admins** (pide + aprueba) y produce
  un **código de enrolamiento de un solo uso que se entrega verbalmente**. El admin que lo genera no
  obtiene una ventana en la que la cuenta sea usable.
- **Los códigos de recuperación son el camino self-service primario.** 10 códigos, mostrados una
  vez, hasheados **con Argon2**. Y el contraste con el plan 12 §7.5 es **deliberado**: allá Argon2 se
  rechazó porque un token de 256 bits no es indexable y verificar sería O(N) Argon2 por request;
  acá son 10 por usuario y los códigos **sí** tienen entropía baja que conviene estirar.
- **Step-up antes que TOTP al login**, si hay que elegir. Exigir un factor fresco para: cambiar tus
  propias credenciales, otorgar acceso, `reveal-password`, destructivo en entorno bloqueante, rotar
  crypto.
- El secreto TOTP se cifra en reposo con el sobre KEK/DEK existente, no se devuelve nunca después
  del enrolamiento, y el enrolamiento **se compromete solo tras validar un código** (si no, se
  brickean cuentas).

---

## 12. OIDC/SSO

La costura ya está puesta a propósito: el docstring de `app/core/auth.py` dice que la indirección
por `get_current_admin` existe *"de modo que migrar a OIDC/SSO no requiera tocar los endpoints"*. Y
este diseño la preserva: `Actor` se acuña en un único lugar.

**Pero es una costura de AUTENTICACIÓN, no de autorización.** Los riesgos son del mapeo:

1. **El namespace de grupos del IdP no es tuyo.** Quien tenga derechos en el IdP, o un grupo
   demasiado amplio ("Everyone", un grupo dinámico), puede otorgar `owner` del gateway **sin tocar
   el gateway ni su auditoría**. Controles: mapear solo desde una **allowlist de identificadores
   exactos** (nunca `startswith`, nunca regex, nunca "contiene admin"); cambiar el mapeo es
   operación de `security_officer` con auditoría fail-closed; y el resultado está **topeado**: **un
   grupo del IdP otorga como máximo `operator`**. `owner` y las capacidades globales exigen
   **además** un grant local. La decisión de mayor privilegio se queda en el sistema que tiene el
   rastro y los gates destructivos.
2. **JIT es un otorgamiento.** JIT crea la cuenta **sin ningún grant** (autentica y nada más).
   Cualquier otra cosa y un ingreso nuevo en el grupo equivocado tiene pseudo-root el día uno.
3. **Vinculación de identidad por el `sub` inmutable** (`users.oidc_subject`, único), **nunca por
   email ni `preferred_username`**: el email es mutable y reasignable, y matchear por él permite que
   un admin del IdP —o un usuario que puede cambiarse el email— **tome una cuenta existente con sus
   grants**. Verificar `iss`, `aud`, `nonce`. Y `is_active=False` local **gana** sobre un token
   válido del IdP.
4. **Latencia de desaprovisionamiento.** OIDC no da revocación: un usuario eliminado del IdP
   conserva sesión válida todo el absoluto y sus tokens de API para siempre. Absoluto corto,
   revalidación de grupos a intervalo acotado, y `is_active` local como autoridad.
5. **SSRF desde el plano de control:** cachear JWKS, pinnear `iss`, no seguir discovery a hosts
   arbitrarios. `net_guard.py` existe para el plano gestionado; la URL de discovery merece el mismo
   tratamiento.

**Cuando el IdP se cae:** todos afuera de la herramienta cuyo propósito incluye arreglar emergencias
de producción. Diseño: **auth local como fallback permanente, estrecho y no removible, solo para las
identidades de `access_admin`/break-glass** (`users.auth_source = 'oidc' | 'local'`). **No**
construir un "si el IdP no responde, caer a auth local" global: es un downgrade de autenticación que
un atacante induce haciéndole DoS al IdP.

---

## 13. Impersonación: no se construye

**En contra**, específico de este sistema:

1. **Destruye lo único que hace valioso al `audit_log`.** Si un `DROP DATABASE` puede ejecutarse
   "como" otro, cada fila pasa de evidencia a afirmación — y el control compensatorio declarado para
   un log que no es append-only es justamente que la atribución sea confiable.
2. **Es un primitivo de escalada por construcción**, y es **el único camino de código donde "quién
   es el actor" y "qué política aplica" divergen**. Todos los bugs reales de impersonación viven en
   esa divergencia.
3. **La necesidad de soporte que la justifica no existe acá.** No hay clientes externos con cuenta;
   todos los que tienen cuenta son colegas con los que se comparte pantalla.
4. **Costo permanente:** los 227 sitios, cada llamada de auditoría y cada invariante de SoD
   (`approved_by != requested_by`) tendrían que responder "¿qué identidad?".

**Los sustitutos que compran el 90%:**

- **`GET /access/explain?user_id=&capability=&server_id=&database_id=`** → la decisión **y el grant
  que la produjo**. Es lo que realmente se necesita para "por qué Ana no puede X". Solo lectura.
- Dry-run como otro usuario **solo para decisiones de lectura**, nunca ejecución.
- El `audit_log` + correlación por `request_id` para "qué hizo".

---

## 14. Modelo de datos y migración

> **El ID de revisión se elige AL IMPLEMENTAR, no ahora.** Este plan proponía `b7c8d9e0f1a2`
> y para cuando se terminó de escribir **ya lo había tomado** la migración de `clone_batches`,
> que entró en `main` el mismo día. Es exactamente la colisión que el `CLAUDE.md` documenta como
> **peor que una bifurcación** —una de las dos migraciones queda inalcanzable y su DDL nunca se
> aplica sin que nada falle— y ocurrió sola, en el intervalo entre planear e implementar. Elegí el
> id contra el head del día y verificá con `scripts/check_migration_graph.py`.

Una revisión de Alembic, `down_revision = ` el head del día, `revision` **elegido a mano** en la
forma secuencial del repo, verificada con `python scripts/check_migration_graph.py`. **Encadenar, nunca
`alembic merge heads`.** Constraints por `NAMING_CONVENTION`, `comment=` en español, `downgrade()`
inverso. Y `app/models/__init__.py` debe importar los modelos nuevos o Alembic no los ve.

| Tabla | Cambio |
|---|---|
| `users` | + `gateway_role` `String(16)` NOT NULL (ver los tres pasos abajo) · + `credential_epoch` `Integer` NOT NULL default 0 · + `credentials_reset_required` Boolean default false · **− `is_superuser`** |
| `access_grants` | **nueva**, SOLO la cadena ordenada: `user_id` FK CASCADE · `role ∈ {viewer, operator, owner}` · `scope_type` (`global\|environment\|server`) · `scope_id` **NOT NULL** con sentinela `0` para `global` + `CHECK(scope_type='global' ⇒ scope_id=0)` · `UNIQUE(user_id, scope_type, scope_id)` |
| `global_capabilities` | **nueva**, las ORTOGONALES: `user_id` FK CASCADE · `capability ∈ {access_admin, security_officer}` · `UNIQUE(user_id, capability)` |
| `gateway_sessions` | **nueva**: `sid` unique · `user_id` · `created_at` · `last_seen_at` · `ip` · `user_agent_hash` · `revoked_at` · `revoked_reason` · `reauth_at` |
| `access_approvals` | **nueva** (fase 4): §9.2 |
| `audit_log` | + `actor_type` · + `api_token_id` · + `role_at_time` · + `previous_value` · + `new_value` |
| `environments` | + `blocks_data_disclosure` · + `requires_second_actor` · + `allows_emergency_override` — **cada uno con su guard en esta entrega** (cero flags inertes) |
| `api_tokens` (plan 12) | + `created_by_user_id` FK, para la revocación en cascada del §8.3 |
| `servers` | + `environment_id` nullable FK RESTRICT (§16.1) |

**Y NO hay `session_epoch`.** Es la técnica de invalidación para cookies *stateless*: sin poder
borrar lo que no guardás, metés un contador en el payload y lo comparás. El §7.1 elimina esa
premisa — la cookie lleva solo el `sid` y hay una fila por sesión con `revoked_at`, así que "revocar
todo" es un `UPDATE` sobre un índice que además deja `revoked_reason`, algo que un epoch no puede
expresar. Tener las dos cosas serían **dos fuentes de verdad para la misma decisión**, que es el
defecto exacto que este plan le achaca a `is_superuser`. Lo que sí hace falta es `credential_epoch`,
que es el mecanismo de un solo uso del token de invitación del §8.1 (sin él, un HMAC stateless es
replayable hasta el `exp`) y sirve además para invalidar los códigos de recuperación del §11.2.

**`gateway_role`: tres pasos, y el default NUNCA es `admin`:**

```
1. add_column(gateway_role, String(16), nullable=True)                       # sin default
2. UPDATE users SET gateway_role = CASE WHEN is_superuser THEN 'admin' ELSE 'viewer' END
3. alter_column(gateway_role, nullable=False, server_default="viewer")        # un solo alter
4. drop_column(is_superuser)
```

**Nunca existe un instante en el que el default de la columna sea `admin`.** La variante de dos
pasos (default `admin` y después `viewer`) tiene una ventana **real**, no teórica: en MySQL/MariaDB
el DDL es **auto-commit**, así que los tres pasos son tres transacciones separadas con la app
posiblemente sirviendo en un rolling deploy — y hay un insertador concreto en ese intervalo,
`bootstrap_admin()`, que corre en el `lifespan` de **cada pod que arranca** (`app/core/auth.py:60-88`)
y cuyo `create()` pasa un dict que **no incluye la columna**: un pod que bootea entre el paso 1 y el
3 insertaría con el default vigente. Con la variante de tres pasos la ventana no se mitiga, **se
elimina**.

Y el `UPDATE` va con `CASE WHEN is_superuser THEN` y **no** con `WHERE is_superuser = 1`:
`is_superuser` es `Boolean`, y **PostgreSQL no coerciona `integer` a `boolean`** (`operator does not
exist: boolean = integer`). La BD de metadatos del gateway puede ser Postgres.

**Y el `downgrade()` no puede ser "inverso" a secas:** dropear `access_grants` borraría la
autorización completa del sistema de forma irreversible, y restaurar `is_superuser` no restaura
ningún comportamiento (nadie la lee). Diseño: (a) el `downgrade()` **se niega a correr** con un
`raise` explícito si hay más de un usuario o filas de grants que no sean del admin original; (b) el
docstring de la revisión declara que **exige rollback de código simultáneo**; (c) `is_superuser` se
restaura desde `gateway_role` antes de dropear la columna.

**`is_superuser` se elimina, no se deja.** Habilitado por un hecho verificado: `AdminOut` es solo
`{id, username}`, así que **retirarlo no rompe el contrato con la SPA**. Dejarla escrita-y-no-leída
es el defecto que el plan 12 §7.6 diagnostica; agregarle un lector ahora sería consagrar una segunda
autoridad.

**Caveat cross-engine, sin verificar:** el `server_default` de un `String` se emite como literal SQL
y el paso 3 sobre SQLite exige `batch_alter_table`. **La migración se autogenera contra SQLite y hay
que regenerarla/verificarla contra el motor real de la BD del gateway antes de desplegar.**

**Y la ventana de grants vacíos:** hay un momento (primer deploy, y la propia migración) donde un
usuario existe sin grants. **Fail-closed: sin grant, sin acceso.** No *"si la tabla está vacía
tratamos a todos como admin"*, que es el agujero de retrocompatibilidad clásico. El admin único
existente se promueve **dentro de la propia revisión**.

---

## 15. El contrato con el frontend

**Hoy el hueco es total, y verificado:** `GET /auth/me` devuelve `{id, username}` y nada más
(`app/schemas/auth.py:11-13`, y el contrato en `docs/api-reference.md`). No hay `role`, ni
`capabilities[]`, ni `can_*`. **La SPA no tiene forma de saber qué puede hacer el usuario, porque
hoy la respuesta es siempre "todo".** Y **ningún `403` de autorización está documentado**, así que
el cliente no tiene camino para ese código.

Todas las apariciones de "permiso"/"rol"/"scope" en el api-reference se refieren a **usuarios y
privilegios del motor destino**, no al gateway.

### 15.1 Se extiende `/auth/me`, no se crea `/me`

Se agregan campos, no se quita ninguno ⇒ la SPA de hoy sigue funcionando.

```
data: {
  id, username, full_name,
  role,                              # rol base
  capabilities: [...],               # UNIÓN efectiva: exactamente lo que require() aceptará
  scope_roles: [                     # capa 2, por alcance
    { scope_type, scope_id, slug, rank, role, capabilities: [...] }
  ],
  step_up_capabilities: [...],
  last_login_at, last_failed_at,
  catalog_version                    # sha256[:12] de capability_matrix()
}
```

Más `GET /authz/catalog` → `capability_matrix()` completa, para que la SPA renderice etiquetas sin
hardcodear el vocabulario.

`scope_roles[].capabilities` **no es adorno**: la pregunta real de la SPA es *"¿puedo apretar
Aplicar migración en ESTA base?"*, y eso depende del alcance. Sin el desglose, la SPA ofrece el
botón y cobra un 403 al apretarlo — que es divergir del servidor por otra vía.

### 15.2 Publicado == hecho cumplir, por construcción

Más fuerte que el precedente del repo (`export_spec` tiene *dos* funciones sobre una tupla de
reglas). Acá es **una sola función**:

- `require(cap)` → `capability_catalog.has(actor, cap)`
- `/auth/me`.`capabilities` → `[c for c in Capability if has(actor, c)]`

El mismo predicado, iterado sobre el enum. **No hay una segunda lista que mantener sincronizada.**

Y el test que lo fija: para un actor de cada rol × cada override de alcance, el conjunto publicado
en `/auth/me` es **exactamente** el conjunto de capacidades para las que la dependencia no lanza
403. Ni uno más (promesa incumplida) ni uno menos (funcionalidad escondida).

### 15.3 La regla que hay que escribir, y la trampa del `safeParse`

> `/auth/me` es una **pista de UI**. Decide el servidor, siempre.

Y una trampa concreta del frontend de este repo, ya documentada en su `TODO.md`: **la SPA hace
`safeParse` del envelope completo, así que una divergencia de un campo descarta la respuesta
entera.** Agregar campos es aditivo y seguro (los schemas no usan `.strict()`), pero **cada campo
nuevo nullable tiene que ir `.nullish()`, nunca `.optional()`**.

**Y hay que documentar el `403` de autorización con su `public_context.code` en un addendum nuevo
ANTES de que el backend empiece a devolverlos** a una SPA que no sabe interpretarlos. El addendum se
referencia **por título**, no por número.

*Nota:* `docs/frontend/` está en `.gitignore`; 2 de sus 13 archivos quedaron versionados de antes de
la regla. Los otros 11 planes de UI **no están en el repo**, así que un handoff que los referencie
por ruta apunta a la nada en un clon nuevo.

---

## 16. Verificación

Todo esto vive en el plano de control (la BD de metadatos), así que **es unit puro o `TestClient` +
SQLite**. No hace falta motor real ni el job de integración que sí necesita el plan 12.

**Los estructurales, que son los que más valen:**

| Guard | Qué asserta |
|---|---|
| `test_every_route_declares_a_capability_or_is_allowlisted` | La regla del §6.3, enumerando `app.routes` y aplanando `route.dependant` recursivamente |
| `test_public_routes_allowlist_is_short_and_alive` | Cada entrada corresponde a una ruta viva; el contador **solo baja** (ratchet) |
| `test_every_get_route_is_in_the_readonly_allowlist` | El invariante que hoy no se sostiene (§7.2.b) |
| `test_no_route_imports_admin_dep` | Cerrado el swap, cero importaciones |
| `test_signed_cookie_payload_never_carries_role_or_capabilities` | Decodificar sin la firma y verificar que solo están las claves mínimas |
| `test_plane_vocabulary` | §3: ningún archivo del plano gateway menciona `privileg`/`permission`/`grant` |
| `test_capability_catalog_invariants` | Los cinco del §5, afirmados al importar |
| `test_me_matches_enforcement` | §15.2, publicado == hecho cumplir |

**Los de propiedad sobre el catálogo** (unit puro, milisegundos, escalan con el catálogo y no con las
rutas — así se evita la matriz de 150 × N):

- `test_viewer_grants_no_mutating_nor_disclosing_capability`
- `test_every_role_is_a_subset_of_the_next` (la monotonía que hace bien definido el `max`)
- `test_mutating_or_disclosing_implies_step_up`
- `test_agent_allowed_implies_neither_mutating_nor_disclosing`

**Los de escalada:** un `operator` no puede cambiarse el rol ni el de otro, no puede emitir tokens
de API, no puede resetear el 2FA de nadie, no puede editar un servidor, y
**`test_cannot_deactivate_the_last_admin`** — el que más vale, porque sin él un bug de UI deja el
gateway sin ningún admin activo.

**Los de sesión:** invalidación al cambiar rol, al cambiar password y al desactivar; rotación del
`sid` al loguear; y `test_login_clears_stale_session_data` (§7.1).

**Los de auditoría de eventos de acceso:** otorgar, revocar, cambiar rol, resetear credenciales,
crear y desactivar usuario dejan fila con actor, objetivo, `previous_value` y `new_value`. Más
`test_audit_write_failure_aborts_the_access_mutation` — el que distingue estos eventos de la
auditoría normal: son `record_intent`, fail-closed.

**Los de no-filtración:** un 403 sobre un recurso invisible devuelve **lo mismo** que uno
inexistente (comparar cuerpo byte a byte), y el `code` es cerrado (`access.forbidden`), **no** "falta
`servers.admin`" — eso le da a un atacante un mapa de la superficie por fuerza bruta de 403.

**Y el guard de la migración, que corre DURANTE y no al final:** cada ruta debe tener **una de dos**
cosas, nunca ninguna — o `AdminDep` (legado, en una allowlist decreciente) o una capacidad. Una ruta
sin ninguna rompe el build, así que **nunca existe una ventana donde algo quede sin guard**. Más un
test de que el conteo de rutas migradas es **monótono creciente**, para que un revert parcial no lo
baje en silencio.

*(El assert de arranque del §6.3 hace esto casi redundante: con él encendido, una rama en estado
intermedio simplemente no arranca, o sea no es mergeable. El guard de migración es para el período
en que el assert todavía está apagado.)*

---

## 17. Orden de implementación

Los cuatro primeros son **arreglos de código existente**, van antes y cada uno en su commit.

| # | Entrega | Por qué acá |
|---|---|---|
| 1 | Sacar `test.router` de `routes.py` (o condicionarlo a `APP_ENV`) | 7 endpoints sin autenticar, dos escriben a disco (§2.1.a) |
| 2 | `confirm_*` en `AdoptComparisonIn` | El único camino de ejecución sin re-tipeo (§2.1.c) |
| 3 | Auditoría en `server_controller` + rate limit en `reveal-password` + `_guard_owner` en `/manifest` | Máximo privilegio con cero rastro; el llavero a 100/min (§2.1.b, d, e) |
| 4 | `request.session.clear()` en `login_session` | Una línea, independiente de todo (§7.1) |
| 5 | `--forwarded-allow-ips` al CIDR del proxy + Valkey obligatorio con multi-worker | Sin esto, el anti-fuerza-bruta que documentes **no existe** (§7.4) |
| 6 | `Actor` + `capability_catalog` + `require()` + los tres puntos del guard (sin el assert de arranque) + `/auth/me` extendido + `/authz/catalog` + la migración | **Fase 0: comportamiento idéntico.** El único usuario queda `admin` con todo ⇒ ningún 403 posible el día del deploy |
| 7 | El swap: 153 gates + 227 sitios que pasan el dict `admin` → `Actor`; borrar `AdminDep`; **encender el assert de arranque** | **Fase 1, un commit.** Diff grande y 100% mecánico; la red es el `frozen`/`slots` |
| 8 | Sesiones server-side + CSRF + timing del login + auditoría `auth.*` | Fase 2 |
| 9 | Capa 2 (alcance), precedida del **reporte de reconciliación** de `environment_id IS NULL` | Fase 3. Recién acá "lector en producción" muerde |
| 10 | CRUD de `/gateway-users` + el primer usuario no-admin + invariante del último admin + break-glass | **Fase 4. NUNCA antes de la 9**: crear un segundo usuario mientras el alcance no se hace cumplir es exactamente el agujero que este plan existe para cerrar |
| 11 | Separación de deberes (`access_approvals`) + step-up | Fase 5 |
| 12 | TOTP + códigos de recuperación | Fase 6 |
| 13 | OIDC | Fase 7 |

**La capacidad de cada endpoint se fija en la fase 1 y para siempre.** Lo que itera después es
`ROLE_CAPABILITIES` — un `Mapping` en un archivo, sin tocar rutas. Esa separación es el punto de todo
el diseño: **la política cambia en un archivo, la superficie no se vuelve a tocar.** Por eso
**tampoco hay capacidad puente tipo `legacy.admin`**: un permiso transitorio único no se refina
nunca, se vuelve `AdminDep` con pasos extra.

---

## 18. Decisiones que necesitan a un humano

1. **`servers.environment_id`: ¿se agrega?** Recomiendo **sí**. La derivación de §4.3 funciona sin
   él, pero clasificar el servidor es más barato de operar y más fácil de auditar.
2. **`environment_id IS NULL` → `rank` más alto (fail-closed) o `is_default` (permisivo)?**
   Recomiendo fail-closed, con el reporte de reconciliación como mitigación operativa.
3. **`test.py`: se saca de `routes.py` o se condiciona a `APP_ENV`?**
4. **Código HTTP del step-up: 428 + `code` vs 403 + `code`.** 428 es lo semánticamente correcto pero
   necesita acuerdo con la SPA. 401 queda descartado: disparía su flujo de logout.
5. **¿Hay canal de notificación (§8.4)?** Si no, las invitaciones no entran en la v1 y la brecha de
   repudio se escribe en la BD.
6. **¿Se acepta que `audit_log` siga sin ser append-only?** Con multiusuario pasa a ser **la
   evidencia en una disputa entre colegas**. Mínimo: rol dedicado para la conexión propia del
   gateway, sin `UPDATE`/`DELETE` sobre `audit_log` — **viable solo si el gateway no usa la misma
   credencial para todo**, y separar eso es un prerequisito más grande que este plan. Hay que
   decidirlo, no suponerlo.
7. **`docs/frontend/`**: ¿se versionan los 11 planes de UI que no están en el repo?

