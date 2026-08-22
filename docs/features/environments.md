# Entornos — clasificación de bases y bloqueo de DDL destructivo

Un **entorno** clasifica cada base de datos gestionada según dónde vive (desarrollo, staging,
producción) y, a diferencia de un proyecto, **no es solo una etiqueta**: hoy lleva una política
que el servidor hace cumplir.

## ⚠️ No confundir con `APP_ENV`

Este repo tiene **dos cosas distintas llamadas "environment"**, y hay que tenerlo presente
antes de escribir cualquier guard:

| | Qué es | Dónde vive |
| --- | --- | --- |
| `APP_ENV` | Modo de despliegue **del propio gateway** (`development` / `production`). Gobierna el flag `Secure` de la cookie, la exigencia de `ADMIN_PASSWORD` y `SESSION_SECRET`, el rechazo del wildcard de CORS, y si `context` se expone en las respuestas de error | `app/core/environments.py` |
| `Environment` | Clasificación de las **bases de datos de terceros** que el gateway administra | tabla `environments` |

Usan **los mismos valores** (`development`, `production`) para cosas que no tienen nada que ver,
y `GET /health` ya devuelve un campo llamado `environment` con el valor de `APP_ENV`. Regla del
repo: en el código la config se referencia siempre por su constante (`APP_ENV`) y esta tabla
siempre como `Environment`; nunca "el entorno" a secas.

## Por qué existe

`apply_all` seleccionaba sus objetivos con

```python
scoped.order_by(ManagedDatabase.id.asc()).limit(max_databases)
```

**sin ningún filtro de clasificación**. Una sola llamada con `max_databases=100` alcanzaba
desarrollo y producción en el mismo lote, con la misma confirmación, y el único recorte
disponible era enumerar ids a mano en `database_ids` — exactamente el trabajo manual que la
etiqueta debería eliminar.

## Qué restringe el entorno, y qué NO

Esta sección es la más importante del documento. **La barrera cubre un camino, no todos.**

### Lo que SÍ restringe

`blocks_destructive_migrations` rechaza aplicar una versión con sentencias destructivas
(`DROP`, `TRUNCATE`, `DELETE` sin `WHERE`, `ALTER ... DROP COLUMN`) a una base de ese entorno.
Cubre **los dos** entrypoints de aplicación:

- `POST /database-models/{id}/migrations/apply-all`
- `POST /managed-databases/{id}/migrations/apply` (incluido `?version=`)

Los cubre porque el guard vive dentro de `_run_apply`, sobre las versiones **realmente
pendientes de esa base**, al lado de los dos guards de captura que ya estaban ahí por el mismo
criterio. Si solo cubriera el lote, el incentivo sería perverso: `apply_all` va siempre al head,
así que una versión destructiva trabaría producción de forma permanente mientras el `apply` por
base la aplicaría con `?version=` sin ningún control.

### Lo que NO restringe (todavía)

**Ninguno de estos caminos mira el entorno.** Marcar una base como productiva no los frena:

| Camino | Qué puede hacer igual |
| --- | --- |
| `POST /{id}/migrations/rollback` | Ejecuta `down_sql`, que es destructivo por definición |
| `POST /{id}/migrations/reconcile-partial` | Ejecuta reversos |
| `DELETE /managed-databases/{id}?drop_remote=true` | `DROP DATABASE` |
| `POST /servers/{id}/databases/{db}/drop` | `DROP DATABASE` |
| Clon con `clean_mode='drop_database'` | Destruye el destino |
| Consola SQL | `DROP TABLE` con confirmación. Opera por `(server_id, database_name)` y **nunca mira `ManagedDatabase`**, así que no tiene de dónde sacar el entorno |
| Conversión de collation | Reescribe tablas en sitio |
| Exportación | No destruye, pero **extrae datos productivos** |
| Usuarios / GRANTs del motor | `DROP USER`, `REVOKE ... CASCADE` |

Todos esos caminos tienen su propio gesto de confirmación (re-tipear el nombre del objetivo +
`confirm_token` firmado), que es lo que hoy los protege. Extenderles el gate de entorno es
trabajo pendiente, anotado en `TODO.md`.

## Los flags que NO existen

El plan original proponía cinco flags de política. Existe **uno**, y es deliberado: **cero flags
inertes.** Un booleano que la API expone y nadie hace cumplir, la SPA lo pinta como un control
activo — es peor que su ausencia. Los otros cuatro se agregan en la tarea que los implemente:

| Flag ausente | Qué falta para que sea real |
| --- | --- |
| `requires_confirmation` | El gesto correcto es `confirm_token` HMAC con `subject` por lote, no un slug que `GET /environments` publica (es público, constante, sin TTL ni anti-replay). Y el guard va **antes** del bucle: desde dentro un 422 es imposible, y en un lote mixto las bases sin entorno se aplicarían primero |
| `requires_previous_environment` | Se apoyaría en la caché `model_version`, que `stamp` escribe **sin ejecutar DDL**: es su puerta trasera. Además hay que restringir el cohorte a `status=active` (si no, una base `archived` traba producción para siempre) y hacer que el conjunto vacío **bloquee** (`all([]) == True` es fail-open en un blueprint nuevo) |
| `max_databases_per_apply` | Con el default de la ruta (`max_databases=10`) un tope de 10 no puede dispararse nunca, y con `id.asc()` en un lote heterogéneo se cumple por accidente. Necesita selección por grupo de entorno |
| `allows_agent_queries` | No tiene consumidor hasta que exista el servidor MCP |

## Modelo de datos

Tabla `environments`: `id`, `name` (único), `slug` (único), `rank`, `color`, `is_default`,
`is_active`, `blocks_destructive_migrations`, + timestamps.

Columna nueva `managed_databases.environment_id`: FK **nullable** con **`ON DELETE RESTRICT`**.

### `rank` no es único

Es el orden de promoción (menor = más temprano), pero **sin restricción de unicidad**. Dos
razones:

1. El único rompía el seed **en silencio**: si el operador renombra el slug `production`, el seed
   idempotente intenta insertar la fila de nuevo con `rank=30`, choca el único, y el `except` del
   patrón de seed se traga el `IntegrityError`. Desde ahí el seed queda muerto para siempre.
2. No hacía falta: el orden total `(rank, id)` ya define el predecesor sin ambigüedad, y sin
   único un swap de ranks no colisiona en el paso intermedio (que en MySQL/MariaDB no se puede
   diferir).

### `ON DELETE RESTRICT`, no `SET NULL`

`managed_databases` tiene dos FKs nullable con criterios opuestos, y esta sigue la segunda:

- `model_id` usa `SET NULL` porque es un puntero de **capacidad**: perderlo significa "esta base
  no replica ningún blueprint". Benigno.
- `owner_id` usa `RESTRICT`, con el comentario *"reasignar antes de borrar"*.

`environment_id` es un puntero de **política**. Con `SET NULL`, borrar una fila convertiría N
bases de producción en bases sin guard, y el guard estaría de acuerdo con que eso está bien.

### `is_default` único, sin constraint

Un índice único parcial (`WHERE is_default`) no es portable: MySQL 8 no tiene índices parciales y
el truco funcional existe en MySQL 8.0.13+ pero **no** en MariaDB 11. Se hace cumplir en el
controller y **con bloqueo de filas** (`SELECT ... FOR UPDATE` sobre el catálogo entero, que son
3-10 filas): el patrón "apagar los demás y después encenderme" tiene una carrera real que deja
DOS defaults sin ningún error, igual en REPEATABLE READ que en READ COMMITTED. SQLite ignora
`FOR UPDATE` en silencio, así que ese invariante no está verificado contra concurrencia real.

> Nota: `charset_catalog.update_option` tiene esta misma carrera **sin cerrar**, y su docstring
> afirma un invariante que el mecanismo no garantiza. No es un precedente a copiar.

### El default es el entorno más permisivo

El seed marca `development` como `is_default`, así que una base nueva **nace clasificada pero no
nace protegida**. La red de seguridad para encontrar lo que quedó mal clasificado es el filtro
`only_unassigned` del listado y el `database_count` de cada entorno, no el default.

Un caso concreto donde eso importaba y se corrigió: el **auto-adopt del clon** creaba la fila del
destino propagando el blueprint y la versión pero no el entorno, así que un clon completo de una
base productiva —estructura **y datos** de producción— nacía como desarrollo. Ahora hereda el
entorno del origen.

## Semántica del `NULL`

Una base **sin entorno** pasa el guard. Es el compromiso de compatibilidad de la entrega: romper
todos los `apply-all` que hoy funcionan no era aceptable, y toda base existente nació antes de
que existiera la columna.

> **Esta asimetría no se traslada.** El día que exista el gate de consultas para agentes de IA,
> ahí un entorno sin asignar debe **negar**: no hay comportamiento previo que romper, y un default
> permisivo dejaría toda base sin clasificar consultable. Está anotado en el docstring del guard
> para que nadie "unifique" los dos comportamientos.

## Seed: solo si la tabla está vacía

Las tres filas iniciales (`development` rank 10 default, `staging` rank 20, `production` rank 30
con el bloqueo encendido) se declaran **dos veces**: literalmente en la migración, y en
`app/services/environment_catalog.py` para el arranque de un esquema creado con `create_all` (que
no pasa por Alembic). Un test compara las dos listas fila por fila.

El seed del arranque siembra **solo si `COUNT(*) == 0`**, a diferencia del catálogo de charsets,
que hace top-up fila por fila. La diferencia es deliberada: en charsets la fila es una entrada de
menú y el docstring de su migración dice que divergir "no hace daño"; **acá la fila es la
política**, y un top-up tiene tres modos de fallo — resucitar un `production` borrado a propósito
sin restaurar el `environment_id` de sus bases, duplicar el default, y dejar dos políticas
distintas según cómo se provisionó el gateway.

## API

| Método | Ruta | Notas |
| --- | --- | --- |
| `GET` | `/environments` | Ordenado por `(rank, id)`. `?only_active=true`. Incluye `database_count` (una query por página) |
| `POST` | `/environments` | |
| `GET` | `/environments/{id}` | |
| `PATCH` | `/environments/{id}` | `slug` **no** editable. Debilitar exige `?confirm_slug=` |
| `DELETE` | `/environments/{id}` | Exige **cero** BDs asignadas → 409 con `database_count`. **No hay `?force=true`** |

Filtros nuevos en `GET /managed-databases`: `environment_id` y `only_unassigned` (mandar los dos
⇒ 422). Parámetro nuevo en `apply-all`: `environment_id`, que acota el lote y se aplica **antes**
del tope para que `max_databases` no se consuma con bases de otros entornos.

### `confirm_slug`: qué cuenta como debilitar

Apagar `blocks_destructive_migrations`, apagar `is_active`, o quitar `is_default`. Los tres exigen
repetir el slug y se auditan con `record_intent` (**fail-closed**: si el rastro no se puede
persistir, el aflojamiento no se ejecuta) **antes** de tocar la fila.

`is_active → false` cuenta como debilitamiento aunque el toggle se llame "activo": desactivar un
entorno lo saca del cálculo de promoción de los que vengan después.

### `force` NO saltea el guard

`force` es override de **cuarentena** y nada más. Está dicho en el `description` de los dos
endpoints y hay un test que lo fija. Si algún día hace falta un override de política, va un
parámetro propio con su propio nombre.

## Códigos de error

Viajan en `public_context.code` (que se ve **siempre**), no en `context` (solo en development).
Vocabulario cerrado en `app/services/environment_catalog.py`.

| Código | HTTP |
| --- | --- |
| `environment.not_found` | 404 |
| `environment.inactive` | 422 |
| `environment.has_databases` (+ `database_count`) | 409 |
| `environment.name_taken` / `environment.slug_taken` | 409 |
| `environment.default_must_be_active` | 422 |
| `environment.default_required` | 409 |
| `environment.filter_conflict` | 422 |
| `environment.confirmation_required` (+ `expected_slug`, `weakened`) | 422 |
| `environment.databases_outside_environment` (+ `database_ids_outside`) | 422 |
| `environment.destructive_blocked` (+ `environment_slug`, `blocked_versions`) | 409, o `error_code` del ítem |

### El rechazo en `apply-all` va en el ítem, no en el `public_context`

`apply_all` captura la excepción por base y **se queda solo con `exc.message`**, descartando el
`status_code` y el `public_context`; la ruta responde **200** con los ítems adentro. Por eso el
ítem lleva campos propios: `error` (prosa para mostrar), **`error_code`** (el código estable),
`environment_slug` y `blocked_by`. Sin `error_code` el cliente tendría que volver a matchear
prosa con expresiones regulares.

Cada denegación deja además su propia entrada de auditoría
(`migration.environment_denied`, `status="denied"`) con la base, el guard y las versiones: un
contador agregado en el `detail` del lote no responde "qué base y por qué".

## Dry-run: informa, no bloquea

El `dry_run` **no falla** por el entorno: devuelve el plan con `blocked_by` (las versiones que el
apply real rechazaría) y `environment_slug`. Bloquear el dry-run le quitaría al operador
justamente la llamada con la que descubre qué lo frena. Precedente del criterio:
`_guard_quarantine` también recibe `dry_run` y se saltea.

## Fuente del veredicto "destructivo"

El guard usa **`migration_facts.analyze`** (AST) sobre el SQL **resuelto para el motor de esa
base** (`select_up_sql`), en OR con el flag del manifiesto cuando existe. Las dos decisiones son
la corrección de un fail-open real:

- **No se lee el manifiesto como fuente primaria.** `ModelMigrationStatement.destructive` parece
  la fuente natural, pero esas filas **casi nunca existen**: el manifiesto es opcional y lo
  escribe únicamente la adopción de un diff estructural. Una migración escrita a mano con
  `up_sql = "DROP TABLE clientes"` no produce **ni una fila**, así que un guard basado en el
  manifiesto la dejaba pasar — por el camino documentado para escribir una migración a mano.
- **Se analiza el SQL resuelto por motor**, no `spec.up_sql`. Una migración puede traer override
  (`up_sql_mysql` / `up_sql_postgresql`) y el `DROP` puede vivir **solo** en el override: mirando
  el SQL base sería invisible justo en el motor donde se ejecuta.

El listado publica su insignia `destructive` con `quick_facts` (regex, más barato). Hay un test
que fija la relación: **todo lo que la insignia llama destructivo, el guard lo bloquea**. Si
alguna vez divergen, el guard es el más estricto, que es la dirección correcta.

## Otros cierres incluidos

- **`model_version` ya no se puede escribir por `PATCH /managed-databases/{id}`.** Era escribible
  a ciegas, sin confirmación y con un `audit.record` sin `detail`, así que "declarar que esta base
  está en la versión X" era un PATCH — y esa caché es la que cualquier gate de promoción tiene que
  leer. Además `max_length=50` no exigía dígitos y `version_sort_key` hace `int(version)`: un
  `"v3-hotfix"` reventaba toda comparación posterior. En el alta se sigue aceptando, pero ahora
  **validado contra el blueprint**, con el mismo criterio que ya usaba `adopt`.
- **La auditoría del update ahora lleva `detail` con `old → new`.** Sin él, una reclasificación de
  entorno era indistinguible de un cambio de `notes`.
- **`stamp` quedó documentado como la puerta trasera** de cualquier política que se apoye en la
  caché de versión, y su auditoría dice explícitamente que DECLARA sin ejecutar DDL.
