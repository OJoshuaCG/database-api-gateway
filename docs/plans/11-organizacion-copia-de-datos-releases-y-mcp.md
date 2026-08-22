# 11 — Organización lógica, copia de datos, releases y acceso para agentes

> **Estado**: propuesta, sin implementar. Fecha de relevamiento: **2026-08-21**.
> Head de Alembic al momento de escribir: `c7d8e9f0a1b2`.

Cinco necesidades que hoy no están cubiertas, agrupadas en un solo documento porque tres de
ellas se apoyan entre sí (entornos → releases → MCP) y las otras dos comparten el mismo patrón
de implementación.

| # | Feature | Estado hoy | Depende de |
|---|---|---|---|
| 1 | Entornos (dev/staging/producción) con política | No existe | — |
| 2 | Proyectos (agrupar blueprints y bases) | No existe | — |
| 3 | Copia parcial y **copia de solo datos** | **Parcialmente implementado** | — |
| 4 | Releases (bundle de versiones promovido por entorno) | No existe; **la maquinaria de base sí** | 1, 2 |
| 5 | Servidor MCP para agentes de IA | No existe | 1 (para el gate de producción) |

---

## Objetivo

1. **Poder decir de qué es y dónde vive cada base.** Hoy el inventario distingue servidor,
   dueño, blueprint y estado, pero no distingue una base de producción de una de desarrollo, ni
   sabe que doce blueprints pertenecen a la misma aplicación. Sin esas dos dimensiones el
   inventario deja de ser navegable en cuanto hay varias aplicaciones × varios entornos.
2. **Hacer que la etiqueta sea una barrera, no un adorno.** El valor de marcar una base como
   productiva no es el color en la tabla: es que el sistema pueda negarse a hacer algo por eso.
3. **Cerrar el hueco de la copia de datos.** Copiar una base con sus tablas y su contenido, sin
   tocar el resto de los objetos, y eligiendo el collation del destino.
4. **Poder desplegar un conjunto de cambios como una unidad**, y promoverlo entorno por entorno
   en vez de base por base.
5. **Abrir las capacidades de lectura y diagnóstico a un agente de IA** sin abrirle nada
   destructivo.

---

## 1. Lo que YA existe y se reutiliza

Sección deliberadamente primera: tres de las cinco features tocan código que ya resolvió el
problema difícil, y planificarlas sin esto es planificar trabajo hecho.

| Pieza existente | Ruta | Para qué se usa acá |
|---|---|---|
| `ManagedDatabase.origin` | `app/models/managed_database.py:86` | Precedente exacto de cómo se agrega un clasificador de baja cardinalidad a una tabla con filas: `String(20)` NOT NULL con `server_default`, sin migración de datos |
| `ManagedDatabaseController.list_databases` | `app/controllers/managed_database_controller.py:110-140` | El set de filtros más rico del repo (`server_id`/`owner_id`/`model_id`/`status`/`engine`, con `join(Server)` para el motor). Los filtros nuevos calcan este patrón |
| `apply_all` | `app/controllers/managed_migration_controller.py:1629-1790` | Ya acepta `database_ids` como subconjunto (422 fail-closed para ids ajenos al blueprint) y `max_databases` como tope. Es el punto de enganche de los guards nuevos: **no hay que construir el fan-out** |
| Rollup de despliegue | `GET /database-models/{id}/databases` → `ModelDatabaseStatusOut` (`app/schemas/database_model.py:208-228`), impl. en `app/controllers/database_model_controller.py:147-252` | La vista "qué versión tiene cada una de mis N bases", con `refresh` para re-leer del motor. Base de la vista de un release |
| Catálogos administrables | `app/models/charset_collation_option.py`, `app/models/permission_profile.py` | Molde de tabla de catálogo con `is_active`, CRUD y filtro por familia de motor |
| `export_spec.py` | `app/services/db_admin/export_spec.py` | Enumerados de DDL, modos de selección y override de charset **ya diseñados y discutidos**; la feature 3 los reusa en vez de inventar otro criterio |
| `query_policy.classify_statement` | `app/services/db_admin/query_policy.py` | Clasificación de SQL por AST, fail-closed. El MCP no necesita su propia política |
| `confirm_token` | `app/services/confirm_token.py` | HMAC con expiración embebida y `subject`. Es lo que un agente **no puede fabricar** |
| `audit.record` / `record_intent` | `app/services/audit.py` | Auditoría fail-closed, ya integrada en todo camino mutante |
| `create_versioned_app()` | `app/core/versioned_app.py` | Montaje de una sub-app con middlewares, handlers y rate limiting heredados |
| Filtros en la SPA | `src/features/managed-databases/pages/ManagedDatabasesPage.tsx:56-90` | Patrón estado local → `Combobox` → query param. Los filtros nuevos se agregan sin diseñar nada |

---

## 2. Entornos

### Necesidad

Hoy nada distingue una base de producción de una de desarrollo, y eso tiene una consecuencia
concreta y medible: `apply_all` selecciona sus objetivos con

```python
scoped.order_by(ManagedDatabase.id.asc()).limit(max_databases)   # :1711
```

sin ningún filtro de clasificación. Una sola llamada con `max_databases=100` alcanza desarrollo
y producción indistintamente, en el mismo lote y con la misma confirmación. El único recorte
disponible es enumerar ids a mano en `database_ids`, que es exactamente el trabajo manual que la
etiqueta debería eliminar.

### Enfoque: catálogo con política, no enumerado

Un enumerado fijo (`dev|staging|prod`) alcanzaría para pintar un badge, pero dejaría las reglas
de seguridad dispersas en el código en forma de condicionales sobre literales. La decisión es
modelarlo como **catálogo administrable donde la política es un dato**, para que endurecer un
entorno sea cambiar una fila y no desplegar.

### Modelo de datos

**Tabla `environments`**

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `name` | `String(60)` único | Legible: "Producción" |
| `slug` | `String(60)` único, indexado | Estable: `production` |
| `rank` | Integer | **Orden de promoción.** Menor = más temprano. Es lo que permite responder "¿el entorno anterior ya está al día?" |
| `color` | `String(20)` nullable | Solo presentación |
| `is_default` | Boolean | El entorno que se asigna a una base nueva si no se especifica |
| `is_active` | Boolean | Mismo criterio que el resto de los catálogos |
| `requires_confirmation` | Boolean | Exigir `confirm_target_name` en el apply, como ya hace el borrado de bases |
| `blocks_destructive_migrations` | Boolean | Rechazar versiones con sentencias destructivas. **El dato ya existe**: `ModelMigrationStatement.destructive` |
| `requires_previous_environment` | Boolean | No aplicar acá si el entorno de `rank` anterior no tiene la misma versión |
| `max_databases_per_apply` | Integer nullable | Tope propio del entorno, más estricto que el global |
| `allows_agent_queries` | Boolean | Consumido por la feature 5. Producción en `false` |

**Columna nueva**: `ManagedDatabase.environment_id` → FK a `environments.id`, **nullable**, con
`ON DELETE SET NULL` (mismo criterio que `model_id`). Nullable a propósito: las filas existentes
no tienen entorno y asignarles uno por default sería adivinar cuál.

### Enganche en el código

- **Guard nuevo en `apply_all`**, al lado de los que ya existen (`_guard_quarantine`,
  `_guard_reviewed_baseline`, `_guard_gateway_internal_sql`, `_guard_cross_engine`). Evalúa la
  política del entorno de cada base **dentro del bucle**, para que una base bloqueada no aborte
  el lote — mismo criterio de contención de errores que ya rige en `:1774`.
- **Parámetro `environment_id`** en `apply-all` para acotar el lote a un entorno. Es lo que
  convierte "aplicá a todo" en "aplicá a desarrollo".
- **Filtro `environment_id`** en `list_databases` y en la ruta
  (`app/routes/v1/managed_databases.py:38-59`).

### El borde a decidir: qué significa un entorno sin asignar

Un `environment_id` en `NULL` no puede caer en la política más estricta por default, porque eso
rompería todos los `apply-all` que hoy funcionan. **Recomendación**: `NULL` es permisivo en
`apply_all` (preserva el comportamiento actual) + aviso visible en la SPA de que la base no está
clasificada + un entorno marcado `is_default` para que las bases nuevas nazcan clasificadas.

Esta decisión **no se traslada a la feature 5**, donde `NULL` debe negar. La asimetría es
deliberada y está justificada en §6.

---

## 3. Proyectos

### Necesidad

Con un blueprint por aplicación el inventario es legible. Con una aplicación de N microservicios
replicada en M clientes, no lo es. Y el listado de blueprints es hoy el más pobre del repo:

```python
def list_models(self, *, limit, offset):   # database_model_controller.py:50-63
```

**no acepta ni un filtro** — solo paginación. La lista crece sin ninguna forma de acotarla.

### Modelo de datos

**Tabla `projects`**: `id`, `name` (`String(100)` único), `slug` (`String(120)` único, indexado),
`description` (Text nullable), `is_active` (Boolean), + `TimestampMixin`.

**Columnas nuevas**: `DatabaseModel.project_id` y `ManagedDatabase.project_id`, ambas FK
nullable con `ON DELETE SET NULL`.

### Alcance cerrado, y por qué

El proyecto agrupa **solo blueprints y bases gestionadas**.

- **Los servidores quedan como infraestructura global.** Un mismo servidor puede hospedar bases
  de dos proyectos distintos; scopearlo obligaría a registrarlo dos veces o a modelar una
  relación N:N que no mejora el filtrado que motivó la feature.
- **Los usuarios del motor y los jobs quedan fuera** en esta vuelta. Se pueden etiquetar más
  adelante sin romper nada, y meterlos ahora multiplica las columnas nuevas sin resolver el
  problema planteado.
- **Los releases sí nacen dentro de un proyecto**, por definición (§5).

### La incoherencia que hay que resolver explícitamente

Si una base declara el proyecto A y su blueprint declara el B, el sistema tiene dos respuestas
para la misma pregunta. Dos salidas:

1. **Derivar**: la base no tiene columna propia y su proyecto es el de su blueprint. Una sola
   fuente de verdad, pero las bases **sin blueprint** quedan sin proyecto — y son un caso real
   (toda base adoptada nace sin `model_id`).
2. **Columna propia + validación cruzada** al crear y al reasignar, con el precedente de la
   validación owner↔server que ya existe en el inventario.

**Recomendación: la opción 2**, justamente por las bases sin blueprint.

### Filtros

`project_id` en los listados de blueprints y de bases, combinable con `environment_id`. La
combinación de los dos es el objetivo real de las features 2 y 3 juntas.

> **Límite a no dar por incluido**: no hay búsqueda por texto en **ningún** listado del repo
> (cero usos de `ilike`/`like` en `app/controllers/`). Con muchos proyectos va a hacer falta, y
> es trabajo aparte de esta feature.

---

## 4. Copia parcial y copia de solo datos

La feature con más matiz del documento, porque **buena parte ya está implementada**. Lo primero
es separar con precisión qué existe de qué falta, o se va a reimplementar lo que ya funciona.

### Ya existe

| Capacidad | Dónde |
|---|---|
| Elegir crear la base destino o usar una existente (`target_mode: new \| existing`) | `app/schemas/clone.py:47-49`; validado en vivo en `app/controllers/clone_controller.py:405-417` |
| **Clonar dentro del mismo servidor** | Permitido. Solo se bloquea el par idéntico origen==destino (`clone_controller.py:388-393`) |
| Elegir qué objetos copiar, por lista explícita `(object_type, name)` | `clone.py:71-74`; cierre de dependencias en `clone_controller.py:555-578` |
| Copiar un **subconjunto de tablas** | `clone_controller.py:640-642` |
| Copiar estructura, o estructura + todos los datos (`include_data`) | `clone.py:52` |
| El destino nuevo **hereda el charset/collation del origen** (mismo motor) | `clone_controller.py:1228-1244` |
| Limpieza del destino: preservar, borrar objeto por objeto, o reset total (`clean_mode`) | `clone.py:53-57` |

### Falta — y es el pedido real

**1. Modo "solo datos"** — copiar tablas y su contenido **sin emitir DDL**.

Hoy no es representable. Solo existe `include_data: bool`, que significa *estructura* vs
*estructura + datos*; las sentencias de estructura se construyen siempre desde el snapshot
filtrado (`clone_controller.py:601-623`) y se ejecutan siempre que no estén vacías (`:1266`). El
apaño actual —destino existente + `clean_mode='none'` + selección de solo tablas— sigue emitiendo
los `CREATE TABLE`, que fallan contra tablas que ya existen.

**Reusar el criterio ya resuelto en el export, no inventar otro**: `ScopeDdl` y `EntityDdl` son
enumerados de cuatro valores (`NONE|CREATE|DROP_CREATE|CREATE_IF_NOT_EXISTS`) en vez de dos
booleanos, precisamente porque *"el estado 'eliminar sin crear' no es representable"*
(`export_spec.py:101-131`). Y el modo solo-datos ya tiene su regla escrita: cuando ambos `*_ddl`
valen `NONE`, la restricción "datos ⊆ estructura" no aplica (`check_data_subset`,
`export_spec.py:806-834`). El clon debe adoptar ese mismo modelo.

**2. Collation del destino elegible** — `target_charset` / `target_collation` en `CloneCreate`.

**El adapter ya lo soporta**; lo único que falta es exponerlo:

```python
# app/services/db_admin/base_adapter.py:306-310
def create_database(self, db_name: str, charset: str | None = None,
                    collation: str | None = None, owner: str | None = None) -> None: ...
```

Implementado en `mysql_adapter.py:519-530` (`CHARACTER SET` + `COLLATE`) y
`postgres_adapter.py:533-556` (`ENCODING` + `LC_COLLATE`/`LC_CTYPE`, con la advertencia de que
en PostgreSQL el locale tiene que existir en el sistema operativo del host).

Requisito: el valor elegido debe pasar por `charset_catalog.resolve_enabled_combination`, que el
clon **hoy saltea** — le pasa los valores crudos del origen directo al adapter. Comparar con
`server_database_controller.py:76-83`, donde la creación de una base por servidor sí valida
contra el catálogo. Precedente de diseño interno para la forma del campo: el `CharsetOverride` de
`export_spec.py:432-436`.

**3. Selección por tipo y por patrón** — el clon solo admite la lista explícita de objetos. El
export ya tiene `SelectionMode` (`all|include|all_except`) y `DataSelectionMode`
(`none|all|include|all_except`) con soporte de patrones (`export_spec.py:229-256`). Es lo que
hace usable un formulario de "copiá solo las tablas" sin obligar al cliente a enumerar 200
nombres.

### Deuda descubierta al relevar esto

- **`row_estimate` siempre vale `None`.** El campo está en el contrato del inventario y del
  preview, pero el diccionario que lo alimenta se inicializa vacío y nunca se puebla
  (`clone_controller.py:300`, `:307`, `:657`) — aun con `include_data=True`. Los adapters sí
  tienen `_estimate_rows` (`base_adapter.py:295-301`, con implementación por motor en
  `mysql_adapter.py:1432` y `postgres_adapter.py:1345`), simplemente no se llama desde acá. La
  UI no puede mostrar el tamaño de lo que se va a copiar.
- **`docs/features/database-clone.md:406` está desactualizado**: afirma que el destino nuevo se
  crea con el default del motor, cuando el código copia el del origen. Corregirlo al implementar.
- **`create_database` acepta `owner` y el clon nunca lo pasa** (`clone_controller.py:1237`,
  `:1244`). En PostgreSQL eso significa que la base clonada queda con el dueño de la conexión.

### Relación con el módulo de conversión de collation

Son dos operaciones distintas y conviene decirlo para que no se confundan:

- **Convertir el collation de una base existente ya existe** —
  `collation_conversion_controller.py:352-460`, modo `universal` en MySQL/MariaDB (incluida la
  recreación de los objetos que congelan la collation) y modo `columns` en PostgreSQL.
- **Elegir el collation al crear la copia** es lo que falta, y no lo cubre lo anterior.

---

## 5. Releases

### Respuesta directa: no existe

Se barrió el repositorio por `release`, `changeset`, `bundle`, `batch`, `tag` y `label`: **no hay
tabla, columna, schema, ruta ni sección de documentación** que agrupe varias filas de
`model_migrations` en una unidad nombrada. Los únicos hits son palabras clave de SQL
(`RELEASE SAVEPOINT` en las listas de sentencias bloqueadas).

Existen dos agrupamientos, y ninguno es un release:

- **`op_group`** agrupa sentencias **dentro de una** versión, para poder deshacerlas juntas.
- Un snapshot crea las versiones `0001..000N` en una llamada, pero salen como versiones
  sueltas, sin ningún identificador compartido.

### Lo que sí existe y lo sostiene

No se arranca de cero. La maquinaria de fondo está construida:

- **`version` es una secuencia entera monótona por blueprint**: `String(10)` con padding a cuatro
  dígitos, único en `(model_id, version)`, ordenado **numéricamente** (`version_sort_key` en
  `app/services/db_admin/migration_integrity.py`, y el orden SQL equivalente por
  longitud-luego-lexicográfico en el controller).
- **La versión aplicada de cada base la posee Alembic**, en la tabla `_gw_v_{slug}` *dentro de
  la base destino*; el gateway la cachea en `managed_databases.model_version` y la re-lee con el
  endpoint de `refresh`. Un release **no debe** introducir una tercera fuente de verdad: tiene
  que leer de acá.
- **El rollup por blueprint ya está** (`GET /database-models/{id}/databases`).
- **`apply_all` es secuencial y tolerante a fallos**: una base que falla no aborta el lote
  (`:1774`), acepta subconjunto explícito y deja una fila de auditoría agregada.

### Lo que falta

**Tablas nuevas**

| Tabla | Columnas |
|---|---|
| `releases` | `id`, `project_id` (FK), `name`, `description`, `status` (`draft\|ready\|promoting\|done`), + timestamps |
| `release_items` | `id`, `release_id` (FK CASCADE), `model_id` (FK), `target_version` (`String(10)`); único en `(release_id, model_id)` |

Un release cruza **varios blueprints del mismo proyecto** — que es exactamente el caso de la
aplicación con microservicios, donde un cambio de negocio toca tres esquemas a la vez.

**Requisito previo en `apply-all`: aceptar una versión objetivo.** Hoy es imposible:

```python
# managed_migration_controller.py:1759
... up_to_version=None, ...      # hardcodeado: apply-all SIEMPRE va a la punta
```

Un release apunta a una versión **concreta** por blueprint, así que exponer `up_to_version` en
`apply-all` no es un extra: es condición para que la feature exista. El endpoint de una sola base
ya lo acepta (`?version=`), así que el camino está probado.

**Gate de promoción.** No aplicar el release al entorno de `rank` N si el de `rank` N-1 no lo
tiene completo. Acá se cierran las tres features entre sí, y es la razón del orden propuesto en
§7: sin entornos no hay `rank`, y sin proyecto no se sabe qué blueprints agrupa el release.

**Rollup por release.** Hoy el rollup es por blueprint y por base. Falta el agregado que
responde "de las 30 bases de producción, 24 están en el release v2.3 y 6 no" — o sea, agregado
por versión y cruzando blueprints.

---

## 6. Servidor MCP para agentes de IA

Exponer el gateway a un agente autónomo **amplía la superficie de ataque más que ninguna otra
feature de esta lista**, porque el gateway conecta a servidores de terceros con credenciales
pseudo-root. Esta sección se escribe en ese registro.

### Necesidad

Toda la capacidad del gateway es hoy alcanzable únicamente por una persona con sesión en la SPA.
Un agente que ayuda a diagnosticar —por qué la base del cliente A difiere de la del B, qué
versiones tiene pendientes, qué índice falta, qué tabla creció— tiene que reconstruir a mano lo
que el gateway ya sabe responder. Un servidor MCP convierte las capacidades **de lectura y de
planificación** en herramientas invocables.

### Prerrequisito bloqueante: la autenticación actual no sirve

El gateway autentica con **cookie de sesión httpOnly firmada** y un único administrador
(`app/core/auth.py`, dependencia `get_current_admin`). Un cliente MCP no tiene navegador ni
cookie.

Hace falta una tabla **`api_tokens`**: token hasheado con Argon2 reusando `app/utils/security.py`
(nunca en claro), `scopes`, `expires_at`, `last_used_at`, `revoked_at`, y una dependencia hermana
de `get_current_admin` que resuelva token → identidad → scopes. **Sin esto la feature no
arranca**, y conviene tratarlo como trabajo propio y no como un detalle del MCP.

### La tesis de diseño: el agente planifica, la persona ejecuta

Los cuatro asistentes del gateway ya están partidos en `plan → preview → confirm → execute`, y
esa partición es exactamente la línea de corte que necesita un agente:

- `plan` y `preview` **no mutan nada**: snapshotean y calculan.
- `execute` exige un `confirm_token` HMAC con expiración embebida, que **un agente no puede
  fabricar**.

La propuesta es aprovecharlo literalmente: el MCP expone plan y preview, devuelve el plan como
texto legible, y el `execute` lo dispara una persona desde la SPA. **No es una limitación que el
MCP tenga que sortear: es la razón por la que este gateway puede tener uno.**

### Niveles de herramienta

Los scopes del token deciden hasta dónde llega cada cliente.

| Nivel | Qué expone | Toca el motor | Muta |
|---|---|---|---|
| `inspect` | Inventario, listar tablas, schema de una tabla, snapshot estructural, estado de despliegue de un blueprint, versiones pendientes | Sí, solo lectura | No |
| `analyze` | Diff de esquema entre dos bases, validar el SQL de una migración, preview de clon/export/conversión, cierre de dependencias de una selección | Sí, solo lectura | No |
| `author` | Crear o editar un **borrador** de migración en la base del gateway (no aplicarlo) | No | Solo metadatos del gateway |
| `query` | Ejecutar SQL de **solo lectura** | Sí, solo lectura | No |
| — | `execute` de cualquier asistente, DCL, `DROP`, revelar contraseña, descargar un export, leer capturas de `SELECT` | **Fuera de alcance, sin excepción** | — |

### El nivel `query` se apoya en garantías existentes, no en confianza en el agente

- `query_policy.classify_statement` clasifica **por AST** y es fail-closed: SQL ilegible,
  sentencia opaca o tipo no mapeado ⇒ peligroso.
- El modo lectura **lo hace cumplir el motor**, con una transacción de solo lectura real. Ninguna
  clasificación estática puede saber si `SELECT mi_funcion()` escribe — y eso importa el doble
  cuando quien redacta el SQL es un modelo.

Dos restricciones propias del MCP encima de eso:

1. **Nivel `read` obligatorio**: un veredicto `write`/`ddl`/`blocked` es **rechazo, no
   confirmación**. El agente no tiene ninguna vía para elevar.
2. **Tope de filas más agresivo** que el de la consola humana, porque la salida entra en el
   contexto del modelo.

### Gate de alcance: un componente codeado, no una opción de configuración

El agente debe poder consultar en desarrollo y quedar **bloqueado en producción y en cualquier
base marcada como prohibida**. Eso tiene que ser una función del servidor que corre antes de
tocar el motor, no un flag de configuración que alguien puede dejar mal puesto.

**1. Dos ejes de decisión, y cualquiera niega.**
`Environment.allows_agent_queries` (producción en `false`) **y**
`ManagedDatabase.agent_queries_blocked` (columna nueva, Boolean). Son dos ejes porque hay bases
sensibles que no son de producción, y bases de producción intocables por motivos distintos.

**2. Fail-closed ante un entorno sin asignar.** Si la base no tiene `environment_id`, el gate
**niega**.

> Esto es una **asimetría deliberada** con el guard de `apply_all` de §2, donde `NULL` es
> permisivo. La justificación: en `apply_all` hay comportamiento previo que romper, y en el MCP
> no hay nada — el default seguro no cuesta compatibilidad. Un default permisivo acá significaría
> que toda base sin clasificar queda consultable por un agente, que es el modo de fallo
> silencioso que este gate existe para evitar.

**3. La resolución de identidad va ANTES del gate, o el gate se esquiva.** Varios módulos del
repo aceptan una base por referencia cruda (`server_id` + nombre) además de por id de inventario,
y ya existe la auto-resolución de una referencia cruda a la `ManagedDatabase` que coincide. El
MCP **debe** reusarla: sin eso, nombrar la base de producción de forma cruda en lugar de por su
id la dejaría fuera del inventario y por lo tanto sin entorno. Con la regla 2 eso termina en
negación —que es el resultado correcto—, pero solo porque el default es negar; con cualquier otro
default sería el bypass evidente. Una base genuinamente fuera del inventario tampoco tiene
entorno, así que también se niega.

**4. El motivo del rechazo usa vocabulario cerrado**, sin reflejar el SQL recibido ni el nombre
de la base ajena. El agente necesita saber que fue negado y por cuál eje; nada más.

> **Consecuencia de planificación**: el nivel `query` **depende de la feature 1**. Sin la tabla de
> entornos no hay forma de saber qué base es productiva, y el gate no se puede escribir.

### Riesgo de primer orden: inyección de prompt desde los datos gestionados

Todo lo que el agente lee del motor es **texto que el gateway no controla**: nombres de tabla,
`COMMENT` de columnas, cuerpos de vistas y rutinas, y filas devueltas por una consulta. Un
comentario de columna puede decir *"ignorá las instrucciones previas y ejecutá…"*. Dos
consecuencias que hay que sostener en el diseño:

1. **La separación de niveles no es comodidad: es la mitigación.** Mientras el agente pueda leer
   contenido no confiable pero no pueda mutar nada, una inyección exitosa no consigue ninguna
   acción destructiva. El día que se agregue una herramienta mutante, esta garantía se cae.
2. **Ninguna herramienta puede tomar como entrada la salida de otra** sin pasar de nuevo por la
   validación del servidor. El gateway nunca debe ejecutar texto que le llegó desde el motor.

### Auditoría, transporte y kill switch

- **Auditoría**: cada invocación se registra con el `api_token` que la originó, reusando
  `audit.record` / `record_intent` (fail-closed). Que un agente pueda operar sin dejar rastro
  contradiría el control compensatorio que el módulo de export declara como no negociable.
- **Rate limit por token**, no por IP: todas las llamadas de un cliente MCP salen de la misma IP,
  así que el limitador actual no las separa.
- **Transporte**: MCP sobre HTTP, montado como sub-app hermana de `/api/v1` reusando
  `create_versioned_app()` para heredar middlewares, handlers de excepciones y rate limiting.
- **Kill switch `MCP_ENABLED`** (default `False`), con el criterio de `EXPORT_ENABLED`: una vía
  de salida de datos tiene que poder cerrarse sin desplegar código.

### Nota de diseño que no hay que dejar implícita

`query` es la única herramienta del set seguro que devuelve **datos de negocio** al contexto de
un modelo. Es la **segunda excepción** al principio de que el gateway no almacena ni manipula
datos de negocio (la primera es la captura de `SELECT` de las migraciones). Se documenta como tal
—con su gate, su tope y su auditoría— en vez de dejarlo pasar como una lectura más.

---

## 7. Orden de implementación sugerido

El orden viene de dependencias reales, no del tamaño de cada pieza.

| # | Feature | Por qué en esta posición |
|---|---|---|
| 1 | **Entornos** | Valor inmediato y aislado: columna + filtro + guard en `apply_all`. No depende de nada |
| 2 | **Proyectos** | Mismo patrón que 1, y habilita el filtro combinado proyecto × entorno |
| 3 | **Copia de solo datos + collation elegible** | Independiente de 1 y 2. Mejor relación valor/esfuerzo del documento: el adapter y los patrones del export ya están |
| 4 | **Releases** | Necesita entornos (para promover), proyectos (para saber qué blueprints agrupa) y `up_to_version` en `apply-all` |
| 5 | **MCP** | Su nivel `query` **no se puede construir sin la feature 1** (el gate necesita saber qué base es productiva). Y conviene que el modelo lógico exista antes de congelar el contrato de herramientas, o el MCP nace describiendo un inventario sin la estructura que lo hace navegable |

Dos cosas se pueden adelantar sin romper el orden: la tabla `api_tokens` (no depende de nada) y
los niveles `inspect`/`analyze` del MCP (no necesitan el gate, porque no devuelven filas).

---

## 8. Modelo de datos — resumen

Cinco migraciones Alembic sobre la base del gateway, encadenadas al head actual
**`c7d8e9f0a1b2`** (cadena lineal de 24 revisiones, sin ramas).

| Migración | Contenido |
|---|---|
| 1 | `CREATE TABLE environments` + `managed_databases.environment_id` (FK nullable) |
| 2 | `CREATE TABLE projects` + `database_models.project_id` + `managed_databases.project_id` (FK nullable) |
| 3 | Columnas de spec del clon en `clone_jobs` (modo de DDL, charset/collation destino) |
| 4 | `CREATE TABLE releases` + `CREATE TABLE release_items` |
| 5 | `CREATE TABLE api_tokens` + `managed_databases.agent_queries_blocked` |

Convenciones a respetar (ya establecidas en el repo): nombres de constraint derivados de
`NAMING_CONVENTION` (`app/models/base.py:15-21`), `comment=` en español en cada columna espejando
el comentario del ORM, `downgrade()` que revierte en orden inverso, y para columnas NOT NULL
sobre tablas con filas el patrón `nullable=False` + `server_default` (ver el precedente de
`origin` y de `kind`).

**Todas las columnas FK nuevas nacen nullable**, así que ninguna fila existente se invalida y
ningún endpoint actual cambia de comportamiento.

---

## 9. Riesgos y decisiones a confirmar

| Riesgo / decisión | Detalle |
|---|---|
| **`NULL` en entorno** | Permisivo en `apply_all` (compatibilidad) pero **negado** en el MCP. La asimetría es intencional y hay que dejarla escrita en el código, no solo acá |
| **Proyecto duplicado base↔blueprint** | Confirmar la opción 2 de §3 (columna propia + validación cruzada) antes de escribir la migración |
| **"Solo datos" contra un esquema distinto** | Falla en la copia, no en la validación. Decidir si se compara el esquema antes con `diff_snapshots` (ya existe) o si se documenta como responsabilidad del operador. Recomendación: comparar, porque el costo es un snapshot que el plan ya hace |
| **`apply-all` sigue siendo síncrono** | Acotado a 100 bases. Un release sobre cientos de bases necesita el fan-out asíncrono del plan 06, que sigue pendiente. Es un límite del release, no un detalle |
| **Segundo esquema de autenticación** | Toda la autorización pasa hoy por `get_current_admin`. Agregar tokens sin unificar el punto de decisión es cómo se termina con dos políticas que divergen en silencio |
| **El gate del MCP no puede tener puerta de atrás** | Si una herramienta nueva alcanza una base sin pasar por el gate, el bloqueo de producción deja de existir **sin que nada falle**. Hacer que el gate sea un parámetro **obligatorio y sin default** de la función que resuelve el destino, con el mismo criterio ya aplicado en el export: `_validate_scope(database, dialect, target)` (`app/controllers/export_controller.py:478`) recibe `target` sin default justamente para que un llamador nuevo no lo saltee |
| **Inyección de prompt** | Mitigada por la ausencia de herramientas mutantes. Cualquier propuesta futura de agregar una debe reabrir este análisis |

---

## 10. Verificación

Por feature, y con el criterio del repo (**no se corre `pytest` salvo pedido explícito**):

**Entornos y proyectos**
- Migración con ciclo `upgrade` → `downgrade` → `upgrade` en SQLite, y contra la base real del
  gateway antes de desplegar.
- `alembic check` sin drift para las tablas nuevas.
- Tests de API: filtros combinados (`project_id` × `environment_id` × los cinco filtros que ya
  existen), y que un filtro ausente no cambie el resultado actual.
- **El test que importa**: `apply-all` sobre un blueprint con bases en dos entornos, con el
  entorno productivo en `blocks_destructive_migrations=true`, debe procesar las de desarrollo y
  reportar las de producción como bloqueadas **sin abortar el lote**.

**Copia de solo datos**
- Que con el modo de solo datos **no se emita ni una sentencia de estructura** en el preview
  (verificable sin motor).
- Que el charset/collation elegido pase por el catálogo y que una combinación deshabilitada
  responda 422 antes de tocar el motor.
- E2E contra motores reales: copiar solo datos a una tabla existente en MySQL/MariaDB y
  PostgreSQL, y el caso de un collation destino distinto del origen.

**Releases**
- `apply-all` con `up_to_version` explícito: que se detenga en la versión pedida y no en la punta.
- Gate de promoción: que un release aplicado solo en `rank` 1 sea rechazado en `rank` 3 mientras
  el 2 esté incompleto.

**MCP**
- Que un token sin el scope correspondiente reciba rechazo por cada herramienta.
- **El test central del gate**: la misma base de producción referida (a) por id de inventario,
  (b) por referencia cruda `server_id`+nombre, y (c) sin entorno asignado, debe ser **negada en
  los tres casos**. Es la prueba de que el gate no se esquiva cambiando cómo se nombra el destino.
- Que un `SELECT` que la política clasifica como `write` sea rechazado y **no** ofrezca vía de
  confirmación.
- Que cada invocación deje fila de auditoría con el token que la originó.
