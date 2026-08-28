# Migraciones de Blueprints (versionado de esquema)

Permite que un **blueprint** (`DatabaseModel`, p. ej. "Whatsapp", "SMS") tenga un
esquema **versionado** —una secuencia de deltas SQL— y aplicarlo, revertirlo o marcarlo
sobre las **N bases de datos gestionadas** que lo replican, sabiendo en todo momento qué
versión tiene cada una. Se apoya en la [gestión de servidores e introspección](server-management.md),
la [capa de conexión remota](remote-connections.md) y la [autenticación](authentication.md).

> Para el diseño interno (Alembic embebido, advisory locks, AUTOCOMMIT, decisiones de
> arquitectura y la auditoría remediada) ver [`docs/plans/02-migraciones-de-modelos.md`](../plans/02-migraciones-de-modelos.md).

## Concepto

- **Blueprint** (`DatabaseModel`, tabla `database_models`): plantilla lógica versionada.
  Su `current_version` refleja la última migración subida.
- **Migración** (`ModelMigration`, tabla `model_migrations`): un **delta SQL** con una
  `version` (solo dígitos; se ordena **numéricamente**, no lexicográfico). La primera
  puede ser el esquema completo; las siguientes son cambios incrementales.
- **Versión real de cada BD**: la mantiene **Alembic dentro de la propia BD gestionada**
  en una tabla `_gw_v_{slug}`. Es la fuente de verdad.
- **Historial** (`database_migration_history` en el gateway): espejo de auditoría
  (cuándo, resultado, duración, error) para consultar sin abrir N conexiones.

## Anatomía de una migración

| Campo | Obligatorio | Notas |
|---|---|---|
| `version` | **no** | Solo dígitos, 4–10. **Si se omite, el gateway autoasigna la siguiente secuencial** (`max+1`); pásala solo para fijarla. Orden numérico |
| `name` | sí | Descripción corta (≤200) |
| `up_sql` | sí | Delta SQL base, **estilo MySQL de referencia**. ≤256 KB |
| `up_sql_mysql` | no | Override manual para MySQL/MariaDB (si la auto-traducción no basta) |
| `up_sql_postgresql` | no | Override manual para PostgreSQL |
| `down_sql` | no | Rollback **confirmado**. Sin él, `rollback` responde 409 |
| `down_sql_suggested` | (auto) | Rollback sugerido por el gateway para ops aditivas; revisar y confirmar vía `PATCH` |
| `checksum` | (auto) | SHA256 de todo el SQL + versión; detecta alteración antes de aplicar. **No** incluye `kind` (para no invalidar checksums existentes) |
| `kind` | (auto) | `schema` (DDL, default) o `data` (datos-semilla upsert de un snapshot). Una migración `data` está **atada a `source_engine`** (la sintaxis upsert difiere por motor) y **no se traduce** cross-engine |
| `reviewed` | (auto) | `true` para migraciones escritas a mano; toda migración generada por **snapshot** (Plan 09) nace `false` y **no se aplica** hasta aprobarla (`PATCH reviewed=true`) |
| `source_engine` / `is_baseline` / `has_non_portable` | (auto) | Metadatos de una migración generada por snapshot (motor de origen; si trae objetos procedurales no portables). El snapshot puede dividirse en varias versiones — ver [adopción/snapshot](adoption-reconcile-snapshot.md) |

El gateway **auto-traduce** `up_sql` de MySQL a PostgreSQL con `sqlglot`; el campo
calculado `translated` muestra el SQL efectivo por motor. Los overrides solo se necesitan
cuando la traducción no es fiable (ver [matriz de equivalencia](#matriz-de-equivalencia-ddl)).

## Flujo de la feature (MVC)

```
routes/v1/model_migrations.py     →  controllers/model_migration_controller.py   (CRUD, BD gateway)
routes/v1/managed_databases.py    →  controllers/managed_migration_controller.py →  services/db_admin/migrations.py
   (/migrations/*)                                                                   (MigrationRunner → motor destino)
```

- El CRUD del blueprint **no toca ningún motor** (solo la BD de metadatos del gateway).
- `apply`/`rollback`/`stamp` sí tocan el motor destino vía `MigrationRunner` (Alembic
  embebido) bajo un advisory lock por BD.

## Endpoints

> Todos requieren sesión de administrador (`AdminDep`).

### Migraciones del blueprint (solo BD del gateway)

```http
GET    /api/v1/database-models/{id}/migrations            # lista paginada (?page=&size=)
POST   /api/v1/database-models/{id}/migrations            # crea una versión
GET    /api/v1/database-models/{id}/migrations/{version}  # detalle (con translated + sugerencia)
PATCH  /api/v1/database-models/{id}/migrations/{version}  # confirma down_sql / añade overrides / corrige up_sql (si no aplicada)
DELETE /api/v1/database-models/{id}/migrations/{version}  # solo la ÚLTIMA versión, sin aplicación exitosa ni parcial
```

### Aplicación sobre una BD gestionada (tocan el motor)

```http
GET    /api/v1/managed-databases/{id}/migrations/status     # versión actual vs. pendientes
POST   /api/v1/managed-databases/{id}/migrations/apply      # ?version= ?force= ?dry_run= — UNA llamada, secuencial (10/min)
POST   /api/v1/managed-databases/{id}/migrations/rollback   # ?confirm_version= (OBLIG.) ?target_version= — secuencial (10/min)
POST   /api/v1/managed-databases/{id}/migrations/stamp      # ?version=  (marca sin ejecutar) (10/min)
GET    /api/v1/managed-databases/{id}/migrations/history    # historial paginado
GET    /api/v1/managed-databases/{id}/migrations/{version}/select-results     # resultados capturados (AUDITADO) (20/min)
DELETE /api/v1/managed-databases/{id}/migrations/{version}/select-results     # purga manual de esas capturas
```

`apply` **y `rollback`** responden **409** `migration.capture_unreviewed` si alguna versión
del camino (pendientes hacia adelante, o versiones a revertir hacia atrás) tiene
`capture_selects=true` **sin aprobar**. El rollback lo exige igual que el apply porque el
`down_sql` **también** captura. No hay consentimiento por corrida: se retiró (ver
[Capturar el resultado de los `SELECT`](#capturar-el-resultado-de-los-select-de-una-migración)).

### Aplicación masiva

```http
POST   /api/v1/database-models/{id}/migrations/apply-all    # ?max_databases=(1..100) ?force= ?dry_run= (3/min)
```

`apply-all` es **síncrono y acotado** (`max_databases` ≤100); continúa con las demás BDs
aunque una falle. El fan-out asíncrono real es del Plan 06.

## Flujo de trabajo (ejemplos)

### 1. Crear el blueprint y subir migraciones

```bash
# Crear la primera migración (esquema inicial, estilo MySQL).
# "version" es OPCIONAL: si se omite, el gateway autoasigna la siguiente (0001, 0002…).
curl -X POST .../api/v1/database-models/1/migrations -b cookie.txt \
  -H 'Content-Type: application/json' -d '{
    "name": "Esquema inicial",
    "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)"
  }'
```

La respuesta incluye la traducción por motor y un rollback **sugerido** (no confirmado):

```json
{
  "success": true,
  "message": "Migración creada.",
  "data": {
    "version": "0001", "name": "Esquema inicial",
    "up_sql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
    "down_sql": null,
    "down_sql_suggested": "DROP TABLE IF EXISTS orders;",
    "translated": {
      "mysql": "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, total INT)",
      "postgresql": "CREATE TABLE orders (id INT GENERATED BY DEFAULT AS IDENTITY NOT NULL PRIMARY KEY, total INT)"
    },
    "checksum": "…"
  }
}
```

### 2. Confirmar el rollback sugerido

```bash
curl -X PATCH .../api/v1/database-models/1/migrations/0001 -b cookie.txt \
  -H 'Content-Type: application/json' -d '{"down_sql": "DROP TABLE IF EXISTS orders"}'
```

### 3. Ver estado y previsualizar (dry-run)

```bash
curl .../api/v1/managed-databases/5/migrations/status -b cookie.txt
# → { "current_version": null, "pending_count": 1, "pending_versions": ["0001"], ... }

curl -X POST '.../api/v1/managed-databases/5/migrations/apply?dry_run=true' -b cookie.txt
# → { "dry_run": true, "from_version": null, "to_version": "0001",
#     "pending_versions": ["0001"], "no_op": false }
```

### 4. Aplicar (una sola llamada, secuencial)

```bash
curl -X POST .../api/v1/managed-databases/5/migrations/apply -b cookie.txt
# → { "from_version": null, "to_version": "0002", "target_version": null,
#     "applied_count": 2, "failed": false, "no_op": false,
#     "pending_versions": ["0001","0002"], "results": [ … ] }
```

`?version=0003` aplica en **una sola llamada** todas las pendientes hasta `0003` (inclusive), en
orden. Forward-only; `422` si la versión no existe; `409` si hay un baseline de snapshot sin
revisar (R1).

### 5. Historial y rollback (target-based, secuencial)

```bash
curl .../api/v1/managed-databases/5/migrations/history -b cookie.txt
# → [{ "version": "0001", "status": "applied", "applied_at": "…", "execution_ms": 42 }]

# Rollback DESTRUCTIVO: confirm_version = versión actual; target_version = destino (anterior).
# En UNA llamada revierte secuencialmente todas las necesarias (p. ej. 0010 → 0007).
curl -X POST '.../api/v1/managed-databases/5/migrations/rollback?confirm_version=0010&target_version=0007' -b cookie.txt
# → { "from_version": "0010", "to_version": "0007", "reverted_count": 3,
#     "reverted_versions": ["0010","0009","0008"], "failed": false }
```

Sin `target_version` revierte solo la última. Sin `confirm_version` (o si no coincide con la
versión actual) → **422**. Si **alguna** versión del camino no tiene `down_sql` confirmado → **409**.

### 6. Marcar una BD pre-existente (stamp)

Si una BD ya tiene el esquema de una versión pero el gateway no lo sabe, `stamp` registra
la versión **sin ejecutar SQL**:

```bash
curl -X POST '.../api/v1/managed-databases/5/migrations/stamp?version=0003' -b cookie.txt
```

`stamp` es una **afirmación explícita** del admin ("esta BD está en la versión X"): además
de marcar la versión, **saca la BD de cuarentena** (`status=error → active`) si un `apply`
previo la dejó ahí. Es la vía correcta para reconciliar una BD adoptada cuyo esquema ya
coincide con el baseline (evita reintentar un `CREATE TABLE` de algo que ya existe).

**`stamp` bloquea (409) si detecta un checkpoint de aplicación PARCIAL** para esa BD (ver
"Checkpoint de sentencia" más abajo): stampear a ciegas por encima de un fallo a mitad de
camino dejaría un `rollback` posterior ejecutando el `down_sql` completo contra una BD que
solo tiene una fracción de los cambios físicos. `?force=true` es la vía explícita para
"ya reconcilié el estado físico a mano" — descarta el checkpoint y procede.

## Matriz de equivalencia DDL

El gateway auto-traduce `up_sql` (MySQL → PostgreSQL) para DDL común. **Escribe un
`up_sql_postgresql` manual** cuando uses construcciones que `sqlglot` no traduce de forma
fiable:

| Construcción MySQL | Auto-traducción | Acción |
|---|---|---|
| `INT AUTO_INCREMENT` | → `IDENTITY` / `SERIAL` | Automática ✅ |
| backticks, `DATETIME` | → comillas, `TIMESTAMP` | Automática ✅ |
| `ENUM('a','b')` inline | no fiable | **Override PG** (crear `TYPE … AS ENUM`) |
| `ON UPDATE CURRENT_TIMESTAMP` | se pierde (en PG es trigger) | **Override PG** |
| `UNSIGNED` / `ZEROFILL` | se descartan | **Override PG** si importan |
| `ALTER … MODIFY … AUTO_INCREMENT` | sin equivalente | **Override PG** |
| Rutinas `BEGIN…END` con `;` internos | el splitter las parte mal | Subir como un solo delta / **override** |

## Integridad, cuarentena y recuperación

- **La BD tiene que EXISTIR en el motor.** Una BD registrada sin aprovisionar (`status=pending`)
  o borrada por fuera del gateway no es un caso teórico: es el estado en que queda toda alta
  hecha con `?provision=false`. Antes, `GET /migrations/status` propagaba el 404 crudo del
  driver ("El recurso solicitado no existe en el servidor destino", errno 1049 / SQLSTATE
  3D000), indistinguible del 404 de "BD gestionada no encontrada", y un `apply` la marcaba en
  **cuarentena** —enmascarando la causa raíz con un diagnóstico falso—. Ahora:
  - `GET /migrations/status` responde **200** con `database_exists: false`. Es una lectura y su
    trabajo es describir la realidad; devolver un error tiraría `pending_versions`, `slug` y
    `latest_available`, que es justo lo que hace falta para decidir. Con ese flag en `false`,
    `current_version` es `null` por **ausencia** (no por "todavía sin migraciones") y las
    pendientes son **todas** las del blueprint.
  - `apply`, `rollback`, `stamp` y `reconcile-partial` responden **409**
    `managed_database.not_provisioned` **antes** de tocar nada, así que la BD **no** queda en
    cuarentena. La salida está en el mensaje: `POST /managed-databases/{id}/provision`.
  - El **dry-run no se bloquea** (mismo criterio que la cuarentena): informa
    `database_exists: false` y `no_op: true`. Es la llamada de diagnóstico.
  - El `status` de la fila **no** se usa como atajo del guard, y es deliberado: está rancio en
    las dos direcciones (una BD creada con `POST /servers/{id}/databases?register=false` existe
    con la fila en `pending`; una fila `active` puede apuntar a una base ya borrada). La única
    fuente de verdad es el plano físico. Un 1049 con la base **presente** tiene otra causa
    (permisos, carrera con un drop) y se propaga tal cual: afirmar "no existe" mandaría al
    operador a crear una base que sí está.
- **Checksum**: antes de aplicar, el gateway re-valida el `checksum` (cubre SQL + versión).
  Si la fila fue alterada directamente en la BD del gateway → **409** (no aplica SQL no
  verificado).
- **Editar `up_sql` (corrección)**: vía `PATCH` puedes corregir `up_sql` (y overrides)
  **mientras ninguna BD tenga la versión aplicada HOY** (ver
  [§ Cuándo se puede editar o eliminar una versión](#cuándo-se-puede-editar-o-eliminar-una-versión)).
  Un intento que solo *falló* no congela el SQL, y una versión **revertida** en todas las BDs
  vuelve a ser editable. Si alguna BD sigue en esa versión (o en una posterior) → **409**
  `model_migration.sql_frozen`: usa **fix-forward** (nueva migración correctiva).
  Al cambiar `up_sql` se regenera el `down_sql_suggested`; si existen overrides por-motor
  debes **reenviarlos corregidos o limpiarlos (null)** en el mismo `PATCH` (409 si no), para
  que no quede SQL viejo aplicándose en silencio.
- **Eliminar una versión**: `DELETE` permite borrar **cualquier** versión —punta o
  intermedia— **mientras ninguna BD esté parada exactamente en ella** (409
  `model_migration.version_in_use` en otro caso). Al borrar una intermedia, las posteriores
  **bajan un escalón** y a las BDs que estaban adelante se les mueve el puntero a la etiqueta
  nueva de su misma migración; eso son escrituras remotas, así que ese caso exige el
  `confirm_token` de `GET .../{version}/delete-plan`. **No se ejecuta ningún SQL: no es un
  rollback**, y las BDs que ya la habían aplicado conservan sus objetos. Ver
  [§ Cuándo se puede editar o eliminar una versión](#cuándo-se-puede-editar-o-eliminar-una-versión).
- **Cuarentena (fallo parcial)**: como el DDL no es transaccional en MySQL/MariaDB (y el
  runner corre en AUTOCOMMIT), una migración multi-sentencia que falla a mitad puede dejar
  estado parcial. El gateway marca la BD con `status=error` + nota; el siguiente `apply`
  responde **409** hasta que inspecciones y reintentes con **`?force=true`**. Un `apply`
  exitoso —o un `stamp`— limpia la cuarentena.
- **Checkpoint de sentencia (resume automático)**: cuando una migración de N sentencias
  falla a mitad (p. ej. 3 de 50 ya commitearon), el gateway graba un checkpoint por
  sentencia en su propia BD de metadatos (`migration_statement_progress`, tabla
  **efímera**: una fila existe solo mientras hay progreso incompleto). El próximo
  `apply` sobre esa BD **retoma automáticamente desde la sentencia 4**, sin re-ejecutar
  las 3 que ya corrieron ni requerir inspección manual — esto es lo que reemplaza el
  flujo manual anterior de "inspeccionar y stampear a mano".
  - **Fail-closed por diseño** (`app/services/db_admin/migration_progress.py`): el
    checkpoint solo se activa (`is_resumable`) para SQL de esquema "plano". Se
    **deshabilita** (todo-o-nada, como antes) si la migración es `kind='data'`, incluye
    objetos no portables (`has_non_portable`), o contiene sentencias que dependen de
    **estado de sesión** (`SET`, `PREPARE`, `LOCK TABLES`, `USE`, transacciones
    explícitas) — un reintento abre una conexión NUEVA y ese estado (p. ej.
    `SET FOREIGN_KEY_CHECKS=0` de la sentencia 1) no sobrevive.
  - El checkpoint queda **ligado al `checksum`** de la migración: si el admin edita el
    `up_sql` mientras hay un checkpoint incompleto, el `PATCH` responde **409** (no se
    permite editar con un resume en curso — el índice del checkpoint dejaría de
    corresponder al SQL nuevo).
  - **Límite irreducible, no resuelto por el checkpoint**: si la sentencia que falló
    está genuinamente rota (error de sintaxis, tipo, o un `ALTER` multi-cláusula que
    MySQL/MariaDB aplica parcialmente — p. ej. `ADD COLUMN a, ADD COLUMN b` donde `a`
    commitea y `b` falla), el resume solo evita re-tropezar con las sentencias previas
    ya aplicadas; la sentencia rota vuelve a fallar igual y requiere corrección manual
    (nueva migración fix-forward o reconciliación directa). El checkpoint reduce
    drásticamente la intervención manual, no la elimina.
  - **Ventana de doble escritura**: el commit del DDL (BD destino) y el commit del
    checkpoint (BD del gateway) son dos datastores independientes sin transacción
    compartida. Si el proceso muere justo entre ambos, el checkpoint puede quedar un
    statement atrasado — el resume re-ejecuta a lo sumo UNA sentencia ya aplicada
    (falla ruidosa tipo "ya existe"), nunca salta una sentencia que no corrió (el
    checkpoint se graba SIEMPRE después del `op.execute`, nunca antes).
- **Recomendación**: escribe migraciones **idempotentes** (`CREATE TABLE IF NOT EXISTS`,
  `ADD COLUMN IF NOT EXISTS`) para que un reintento sea seguro.

## Cuándo se puede editar o eliminar una versión

> Contrato para el frontend: [`docs/api-reference-v15.md`](../api-reference-v15.md)
> (el borrado con renumerado) y [`docs/api-reference-v14.md`](../api-reference-v14.md)
> (el criterio de congelamiento del que parte).

Editar el `up_sql` de una versión o borrarla **no ejecuta ni deshace nada en el motor**: solo
cambia la descripción que el gateway guarda. Por eso hay un guard, y por eso el criterio del
guard importa tanto.

### Dos reglas, no una

La **edición** y el **borrado** dejaron de compartir criterio, y la diferencia es el punto
entero de esta sección.

> **Editar** el SQL de una versión: congelada mientras alguna BD esté en esa versión **o en
> una posterior** (criterio `>=`).
>
> **Borrar** una versión: bloqueado solo si alguna BD está parada **exactamente** en ella
> (criterio `==`). Las que están adelante o atrás no bloquean.

Por qué difieren:

- Para **editar**, una BD en `0007` sí depende de la `0003`: las migraciones son forward-only
  encadenadas, así que tiene aplicadas todas las `<= 0007`. Cambiar el `up_sql` de la `0003`
  dejaría la metadata describiendo algo distinto de lo que realmente corrió allí.
- Para **borrar**, esa misma BD **no** es un problema, porque el borrado **renumera** lo que
  sigue y le mueve el puntero a la etiqueta nueva de su misma migración. Solo la BD parada
  justo en la versión que desaparece se queda sin etiqueta a la que apuntar.

Lo que **no** congela ninguno de los dos: haber corrido alguna vez. Ver abajo por qué es la
mitad importante.

### Borrar una versión intermedia: qué pasa exactamente

Antes solo se podía borrar la **punta**. Ahora se puede borrar cualquier versión libre, y el
gateway cierra el hueco:

1. Las versiones posteriores **bajan un escalón** (`0016` → `0015`, `0017` → `0016`…). Las
   anteriores no se tocan.
2. A las BDs que están **adelante** se les mueve el puntero a la etiqueta nueva de la **misma**
   migración (una BD en `0020` queda en `0019`, y `0019` es la migración que antes se llamaba
   `0020`).
3. **No se ejecuta ningún SQL del blueprint.** Ni el `up_sql` de la versión borrada, ni ningún
   `down_sql`. **No es un rollback.**

> ⚠️ **Consecuencia que hay que tener presente**: las BDs que ya habían aplicado esa versión
> **conservan físicamente** sus objetos. Tras el renumerado, la cadena del blueprint ya no los
> describe. El preview lo advierte nombrando esas BDs; el borrado no las repara.

**El orden no es negociable: los stamps van ANTES del renumerado.** El `revision` de los
archivos de revisión que genera el gateway es literalmente el string de versión
(`_render_revision`), y `command.stamp` necesita resolver el valor **actual** del puntero antes
de moverlo. Renumerar primero dejaría a cada BD adelantada nombrando una revisión inexistente,
y Alembic la rechazaría con `Can't locate revision identified by …`: sin apply, sin rollback y
sin stamp. Con este orden ninguna BD queda huérfana — si la fase de stamps falla, los punteros
ya movidos se devuelven a su valor original y el blueprint no se toca; si falla la fase local,
es una sola transacción y revierte sola.

**El escape para una BD que sí quedó huérfana** (por una carrera, o por un renumerado hecho por
otra vía) es `MigrationRunner.stamp(..., purge=True)`: vacía la tabla de versión antes de
escribir, así que no necesita resolver el valor viejo. Es la única salida, y no debe usarse
fuera de ese caso.

### Es una escritura REMOTA, no una operación local

Mover el puntero de una BD **escribe dentro de esa base**: `UPDATE _gw_v_{slug} SET
version_num = …`, más la conexión y el advisory lock que la rodean. O sea que borrar una
versión con BDs adelante son **N escrituras remotas** más una transacción local en el gateway,
y los dos lados **no comparten transacción**.

Por eso el borrado con punteros a mover exige un doble paso:

```
GET    /database-models/{model_id}/migrations/{version}/delete-plan   → plan + confirm_token
DELETE /database-models/{model_id}/migrations/{version}?confirm_token=…
```

El `confirm_token` está atado a la **huella del parque** que congeló el preview: si alguna BD se
movió de versión en el medio, deja de verificar y la operación se rechaza en vez de ejecutar un
plan que ya no describe la realidad.

**Si no hay punteros que mover, el token no se pide.** Borrar la punta de un blueprint sin BDs
adelante sigue siendo la operación local de siempre, y el cliente que ya lo hacía no se rompe.

### Por qué el historial NO es el criterio

`database_migration_history` es un **log de eventos**, no un estado. Su fila
`status='applied'` significa "esta versión corrió con éxito sobre esta BD alguna vez", y **no
se revoca nunca**: `ManagedMigrationController._record_history` se llama igual desde el camino
`apply` que desde el `rollback`, y ninguno de los dos deja rastro de la **dirección** — ni la
tabla ni `MigrationResult` tienen columna `direction`.

Con el historial como criterio, una versión **revertida correctamente en todas las BDs** quedaba
congelada **de por vida**, sin ninguna salida:

- no existe purga de historial;
- el `DELETE` no acepta `force`;
- el `ondelete='CASCADE'` que se llevaría esas filas cuelga del borrado de la migración, que es
  justo lo que el historial bloquea.

La única salida era fix-forward: acumular versiones correctivas para describir cambios que ya no
existían en ningún motor.

Para la **edición**, el historial sigue siendo el primer filtro y es barato: sin ninguna fila
`applied` no se abre ni una conexión.

Para el **borrado** el filtro barato es otro, porque su criterio es la POSICIÓN de cada BD y no
el resultado de un intento: se saltean las BDs que **nunca fueron posicionadas** (`model_version`
nulo **y** sin historial exitoso). Es sound —los cuatro caminos que mueven una BD (apply,
rollback, stamp y el stamp-on-adopt) escriben esa caché—, y es lo que evita que un solo motor
caído en el blueprint bloquee el borrado de cualquier versión, incluidas las que nadie aplicó.

### Dos lecturas distintas, a propósito

| Camino | Fuente de la versión | Por qué |
|---|---|---|
| **Listado / detalle** (`sql_frozen`, `deletable`, `block_reason`, `delete_requires_stamps`) | Caché del inventario (`ManagedDatabase.model_version`) | Corre por cada fila de cada página: abrir una conexión por BD para pintar un botón no se sostiene. |
| **`delete-plan` / `PATCH` / `DELETE`** (el 409) | **El motor**, vía `MigrationRunner.get_current_version` | Es el veredicto autoritativo: se está por autorizar algo irreversible, y la caché puede estar rancia. |

La divergencia posible es en la dirección segura: si la caché quedó atrasada, el listado puede
ofrecer un botón que después el guard rechaza con 409. Al revés no puede pasar sin que la caché
**sobreestime** la versión, y eso solo congela de más.

### Los códigos de error

Viajan en `public_context.code` — **nunca** en `context`, que solo se expone en `development`:
en producción el operador recibiría el mensaje sin poder clasificarlo ni elegir la salida.
Vocabulario cerrado en `app/services/migration_freeze_catalog.py`.

| HTTP | `public_context.code` | Cuándo | Salida |
|---|---|---|---|
| 409 | `model_migration.sql_frozen` | `PATCH` que cambia el SQL efectivo de una versión que alguna BD tiene aplicada (criterio `>=`). | Fix-forward con una versión nueva, **o** revertir en las BDs que la nombran. |
| 409 | `model_migration.version_in_use` | `DELETE` de una versión en la que alguna BD está parada exactamente. | Mover esa BD (apply o rollback) y reintentar. |
| 409 | `model_migration.unreadable_databases` | No se pudo leer la versión de alguna BD del blueprint. **Fail-closed**. | Recuperar el acceso a esa BD. |
| 409 | `model_migration.renumber_confirmation_required` | El borrado implica mover punteros y no llegó `confirm_token`. | Pedir `delete-plan` y reenviar su token. |
| 422 / 410 | *(del `confirm_token`)* | Token que no corresponde al plan congelado, o vencido. | Volver a pedir `delete-plan`. |
| 409 | `model_migration.renumber_stamp_failed` | Falló el re-stamp de una BD. El blueprint **no** se modificó; `compensated` dice si los punteros ya movidos volvieron. | Revisar esa BD y reintentar. |
| 409 | `model_migration.renumber_target_missing` | Una BD adelantada quedaría en una etiqueta inexistente (hueco en la numeración justo debajo de donde está parada). | Rellenar el hueco con una versión, o mover esa BD antes. |
| 409 | `model_migration.affected_partial_application` | Alguna versión afectada tiene una aplicación a medias. El renumerado cambia su `checksum` y el checkpoint dejaría de corresponder. | `reconcile-partial`, o completar el `apply`. |
| 409 | `model_migration.still_applied` | Histórico del criterio `>=`. Sigue vigente en los caminos que lo usan. | Revertir primero. |

Los que nombran BDs traen `version` y `blocking_databases[]`, con un ítem por BD:
`managed_database_id`, `reason` y —cuando aplica— `current_version`.

### Los `reason`

| `reason` | Significa | Trae `current_version` |
|---|---|---|
| `in_use` | El motor reporta que la BD está **exactamente** en esa versión. Es el motivo del borrado. | Sí |
| `still_applied` | El motor reporta que la BD está en esa versión o en una posterior. Es el motivo de la edición. | Sí |
| `unreadable` | No se pudo leer la versión de esa BD: motor caído, base sin aprovisionar, credenciales rotas, o un puntero no numérico que no se puede ubicar en la secuencia. **Fail-closed**: cuenta como bloqueante. | No |
| `unknown_database` | Hay historial contra una BD que ya no está en el inventario. No debería ocurrir (el `CASCADE` se lleva esas filas), pero no se puede probar lo contrario. | No |
| `unknown_blueprint` | No se pudo resolver el blueprint de la migración, así que no hay `slug` con el que ubicar la tabla de versión `_gw_v_{slug}` dentro de cada BD. | No |

**`unreadable` es fail-closed y no es negociable**: tratar un fallo de lectura como "esa BD ya
no la tiene" convertiría un corte de red en autorización para destruir metadata. Para el
borrado es doblemente necesario: a una BD que no se puede leer tampoco se le puede mover el
puntero, así que renumerar la dejaría huérfana.

El `message` del 409 nombra la BD y el motivo, y **nunca** transcribe el error del motor
(criterio R4): ese mensaje puede llevar host, usuario o fragmentos de sentencia. El detalle va
al log, correlacionado por Request ID.

### Lo que el renumerado toca por debajo

Dos cosas que parecen internas y no lo son:

- **El `checksum` se recalcula en cada versión renumerada.** `compute_checksum` incluye la
  `version`, y `ManagedMigrationController._verify_integrity` lo recomputa y compara en cada
  apply, rollback, stamp y apply-all. Renumerar sin recalcularlo dejaría el blueprint **entero**
  respondiendo 409 *"la migración X fue alterada"*.
- **Las capturas de `SELECT` se reapuntan** al checksum nuevo (`migration_results.rekey_checksum`).
  Su SQL no cambió, así que marcarlas `stale` por un renombre sería mentir.

Los `UPDATE` del renumerado van **de a uno y en orden ascendente**: el `UniqueConstraint(model_id,
version)` hace que un UPDATE masivo colisione consigo mismo.

### ⚠️ Límite conocido: `stamp` es la puerta trasera

`stamp` mueve el puntero de versión **sin ejecutar ni deshacer una sola sentencia**. Una BD
stampeada hacia atrás reporta una versión anterior mientras **conserva físicamente** los
cambios de la versión que dejó de nombrar.

Consecuencia directa: sobre esa BD, este guard deja **editar o borrar la descripción de cambios
que siguen en el motor**. No es un descuido — es el mismo límite que tiene cualquier control
basado en la versión (el gate de entornos lo declara igual), y `stamp` existe justamente para
que un operador pueda declarar a mano un estado que el gateway no puede inferir.

Si stampeás hacia atrás, la versión que "liberaste" describe cambios reales: no la borres sin
haberlos revertido o adoptado en otra versión.


## PostgreSQL: el estado parcial no existe

Es la diferencia de motor más importante de todo el módulo. **PostgreSQL ejecuta DDL
transaccional**: una migración de 50 sentencias que falla en la 10 se deshace **sola**, el
ledger de Alembic y el plano físico nunca divergen, y el `rollback` posterior opera sobre un
estado conocido. MySQL/MariaDB hacen **COMMIT IMPLÍCITO** en cada DDL: ahí la atomicidad es
imposible y el checkpoint por sentencia es la única defensa.

El `env.py` compartido siempre pidió `transaction_per_migration=True`, pero el runner
forzaba la conexión a AUTOCOMMIT y lo anulaba — así que PostgreSQL sufría un problema que
no le corresponde. Ahora `MigrationRunner.use_transactional_ddl` decide por motor:

| | PostgreSQL | MySQL / MariaDB |
|---|---|---|
| Conexión | transaccional | AUTOCOMMIT |
| Advisory lock | en **otra sesión** (`advisory_lock`) | en la misma conexión |
| Fallo a mitad | **el motor revierte todo** | quedan k sentencias aplicadas |
| Checkpoint por sentencia | **desactivado** | activo |
| Reconciliación | no hace falta | `reconcile-partial` |

Dos detalles que no son opcionales:

- **El lock va en su propia sesión.** Los advisory locks de *sesión* de PostgreSQL
  sobreviven a COMMIT y a ROLLBACK (los de transacción son `pg_advisory_xact_lock`, que no
  se usan), pero tenerlo en la misma conexión metería la adquisición dentro de la
  transacción de la migración. Con una sesión aparte, la transacción queda limpia.
- **El checkpoint se desactiva.** No es una optimización: el checkpoint se graba en la BD
  del *gateway*, otra conexión con su propio commit. Si la transacción de la migración se
  revierte en el destino, un checkpoint sobreviviente afirmaría "10 sentencias aplicadas"
  sobre una BD virgen y el resume arrancaría en la 11.

**Excepciones.** Si alguna migración del blueprint contiene una sentencia que PostgreSQL no
admite en una transacción (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`,
`VACUUM`, `ALTER SYSTEM`, `CREATE/DROP DATABASE`, `ALTER TYPE … ADD VALUE`) se cae al modo
AUTOCOMMIT para toda la operación y se registra en el log qué versión lo desactivó. Es
conservador a propósito: se evalúan todas las migraciones, no solo las pendientes, porque la
decisión se toma antes de saber cuáles van a correr. `ALTER TYPE … ADD VALUE` se excluye
aunque PostgreSQL 12+ lo permita: el valor nuevo no se puede *usar* en la misma transacción.

## Qué hace el sistema cuando un apply falla

`POST .../migrations/apply?on_failure=` — solo relevante en MySQL/MariaDB (en PostgreSQL el
motor ya deshizo todo):

| Modo | Comportamiento |
|---|---|
| `auto` (default) | Deshace lo aplicado **solo si puede deshacerlo todo**. Si lo logra, la BD vuelve limpia a su versión anterior y **no queda en cuarentena**: solo hay que corregir la migración. |
| `reconcile` | Deshace igual, salteando y reportando las sentencias sin reverso. |
| `leave` | No toca nada (cuarentena + checkpoint). Comportamiento anterior. |

La respuesta trae `reconciliation` con qué se deshizo, si quedó completo y qué reversos no
son demostrablemente seguros. Con `auto` el flujo del admin pasa a ser: apply falla →
la BD ya está limpia → corregir el SQL → reintentar. Sin pasos manuales.

### ⚠️ El anti-patrón: `stamp --force` + `rollback`

La reacción intuitiva ante un apply que falla a mitad es stampear la versión que falló y
después revertirla. **Eso empeora el problema:**

1. `stamp 8` **afirma** que las 50 sentencias de la versión 8 se aplicaron.
2. `rollback` ejecuta los **50 reversos** contra una BD que solo tiene 10 cambios físicos.
3. Los 40 reversos de lo que nunca corrió fallan (`doesn't exist`) y el rollback muere a
   mitad, dejando un **tercer** estado inconsistente.
4. Encima, `force=true` descarta el checkpoint — la única prueba de dónde había quedado.

Las vías correctas, en orden: `on_failure=auto` (automático), `reconcile-partial`
(explícito), o reintentar `apply` (retoma desde el checkpoint). `stamp --force` es solo para
"ya reconcilié el estado físico a mano"; su 409 lo explica.

## Traducción MySQL → PostgreSQL: lo que sqlglot dejaba roto

Una migración sin `up_sql_postgresql` se auto-traduce con sqlglot. Transpila bien
expresiones y tipos, pero emitía **verbatim** varias formas de DDL de MySQL:

| Entrada (MySQL) | Salía | Debe ser |
|---|---|---|
| `DROP INDEX i ON t` | `DROP INDEX "i" ON "t"` ❌ | `DROP INDEX "i"` |
| `ALTER TABLE t DROP FOREIGN KEY f` | `… DROP FOREIGN KEY "f"` ❌ | `… DROP CONSTRAINT "f"` |
| `ALTER TABLE t DROP INDEX u` | `… DROP INDEX "u"` ❌ | `… DROP CONSTRAINT "u"` |
| `ALTER TABLE t DROP CHECK c` | ``… DROP CHECK `c` `` ❌ (¡backticks!) | `… DROP CONSTRAINT "c"` |

Las cuatro tienen reescritura exacta y ahora se aplican **por sentencia y con contexto**
(aplicarlas al script completo hacía que la segunda pisara el resultado de la primera).

`MODIFY COLUMN`, `CHANGE COLUMN`, `DROP PRIMARY KEY`, `ENGINE=` y `AUTO_INCREMENT=` **no**
tienen traducción exacta (la primera hay que partirla semánticamente; la tercera necesita el
nombre del constraint, que en PostgreSQL es convencionalmente `<tabla>_pkey` pero no está
garantizado). Antes se emitían roto; ahora `apply` responde **422** antes de tocar el motor
pidiendo un `up_sql_postgresql` explícito. Devolver `None` no alcanzaba como defensa:
`select_up_sql` caía al `up_sql` base, en dialecto MySQL crudo, igual de inválido.

**Rendimiento**: el transpilado está memoizado (`lru_cache`, 256 entradas) — el `up_sql` de
una versión es inmutable, así que cachear por (sql, dialectos) es seguro. Medido: 80 ms la
primera vez, 0,02 ms las siguientes. El guard usa además un pre-filtro por regex sobre el
SQL crudo, así que solo paga el transpilado si hay algo sospechoso.

## Aplicación parcial: el rollback ya no opera a ciegas

### El problema

Cuando el `apply` de la versión N muere en la sentencia k de N, quedan **dos verdades en
desacuerdo**:

- Alembic escribe la versión en `_gw_v_{slug}` recién al **terminar** el `upgrade()`, así
  que el ledger sigue diciendo *"estoy en N-1"*;
- la BD tiene, físicamente, las primeras k sentencias de N **ya commiteadas** (AUTOCOMMIT:
  el DDL de MySQL/MariaDB no es transaccional).

`rollback` no veía nada raro: leía `current = N-1` y se ponía a ejecutar el `down_sql` de
**N-1** contra una BD contaminada con parte de N. O falla a mitad —dejando un tercer estado
inconsistente— o "funciona" por casualidad y deja objetos huérfanos de N que el gateway ya
no sabe que existen. `current_version` tampoco delataba el problema: la versión parcial
nunca se registró.

Y con solo los blobs `up_sql`/`down_sql` era **imposible** arreglarlo: el `down_sql` es una
secuencia independiente, con otra cantidad de sentencias (los cambios sin reverso
simplemente no aparecen), así que "deshacé las k primeras del up" era ininferible.

### La solución: manifiesto de sentencias + reconciliación

**`model_migration_statements`** (tabla nueva, opcional) guarda **una fila por sentencia**
de la versión, con su reverso **emparejado** y su `seq` coincidiendo exactamente con el
índice del checkpoint (`migration_statement_progress.last_statement_index`). Ese acople es
lo que hace posible saber qué se aplicó y qué hay que deshacer.

Lo escribe el flujo que conoce el emparejamiento sentencia↔reverso: la **adopción de un
diff estructural** (`POST /schema-comparisons/{id}/adopt`). Una migración escrita a mano no
lo tiene y sigue funcionando como antes (todo-o-nada).

Tres barreras fail-closed antes de confiar en un manifiesto:

1. tiene que ser del **mismo motor** que la BD destino (el SQL traducido cross-engine puede
   no partirse en la misma cantidad de sentencias);
2. concatenar sus sentencias tiene que **reproducir exactamente** el `up_sql` vigente — es
   la misma operación con la que se construyó, así que es una igualdad exacta y no depende
   del splitter. Si no coincide, el SQL fue editado y el manifiesto NO se usa;
3. el `PATCH` que cambia el SQL **borra** el manifiesto (barrera primaria).

### Qué cambió en la práctica

- **`rollback` responde 409** mientras haya una aplicación parcial sin resolver (guard
  ROB2). El mensaje ofrece las dos salidas reales: completar con `apply` (que retoma desde
  el checkpoint) o reconciliar.
- **`GET /migrations/status`** informa `has_partial_application` y, por versión,
  `applied_statements`/`total_statements`, si es `reconcilable` y `statements_to_undo`. El
  frontend puede ofrecer el botón solo cuando sirve.
- **`POST /managed-databases/{id}/migrations/reconcile-partial`** deshace las sentencias que
  **sí** se aplicaron: ejecuta el reverso exacto de las k primeras, **en orden inverso**,
  hasta que el plano físico vuelve a coincidir con el ledger.
  - **No es un `downgrade` de Alembic**: la versión N nunca se aplicó, no hay nada que bajar
    en el ledger — y por eso **no se toca** la tabla de versión. Es una compensación.
  - `confirm_version` obligatorio (la versión parcialmente aplicada: el admin tiene que
    haber mirado el estado antes).
  - `dry_run=true` devuelve los reversos exactos sin tocar el motor. **Recomendado siempre.**
  - El checkpoint se **decrementa** después de cada reverso exitoso, así que si la
    reconciliación misma falla a mitad, el checkpoint sigue describiendo con exactitud qué
    queda aplicado y un reintento retoma donde quedó. Se limpia al llegar a 0, y ahí la BD
    sale de cuarentena.
  - `force=true` procede aunque alguna sentencia aplicada **no tenga reverso**: la saltea y
    la reporta en `unreversible_statements` (esos cambios quedan en la BD). Sin ese
    override, una sola sentencia irreversible dejaría al admin sin salida automática.
  - Sin manifiesto responde **409 con el motivo**, nunca reconcilia a ciegas. La salida ahí
    sigue siendo reconciliar a mano + `stamp?force=true`.

### Efecto secundario bueno: cuerpos procedurales resumibles

El checkpoint excluía las migraciones con rutinas/triggers (`has_non_portable`, o cualquier
`CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT`) por prudencia: el riesgo era indexar mal por una
duda del splitter. Con un manifiesto **no hay splitter** — el índice es dato persistido —
así que esas migraciones ahora **sí** se pueden resumir (`is_resumable(..., manifest_pinned=True)`).
Las exclusiones por **estado de sesión** (`SET`, `LOCK TABLES`, transacciones explícitas) y
por `kind='data'` se mantienen siempre: no dependen de cómo se obtuvieron las sentencias
sino de que un resume abre una conexión **nueva** que pierde ese estado.

### Pendiente

`create_from_snapshot` (baseline de Plan 09) **no** escribe manifiesto todavía: un baseline
que falla a mitad sigue siendo todo-o-nada. Es viable (el `StructureDump` tiene
`object_type` + `name`, así que el reverso `DROP <tipo> <nombre>` es derivable) y quedó como
follow-up.

## Capturar el resultado de los `SELECT` de una migración

### El problema

Una migración a veces necesita **verificar** algo en la BD destino: cuántas filas quedaron
sin backfill, qué valores violan la `CHECK` que se está por crear, qué duplicados bloquean
el `UNIQUE`. Ese `SELECT` se ejecutaba y su resultado **se tiraba**: Alembic no devuelve
nada y el gateway solo informaba "aplicada / falló". Justo en el caso que importa —una
migración que murió en la sentencia *k*— el estado que se quería mirar ya cambió.

### Cómo se activa (dos llaves, no una)

Esta es la **única** vía por la que el gateway persiste **datos de negocio** (el módulo de
auditoría declara explícitamente que nunca lo hace: esta es la excepción deliberada). Por eso
hay que girar dos llaves distintas:

1. **Opt-in por versión**: `capture_selects: true` al crear la migración (o por `PATCH`).
   Activarlo pone la versión en `reviewed=false`.
2. **Revisión**: `PATCH .../migrations/{version}` con `reviewed=true`. Mientras no se
   apruebe, `apply`/`apply-all` responden **409** `migration.capture_unreviewed` (mismo
   mecanismo que el gate R1 de los baselines de snapshot, con su propio mensaje). El gate se
   evalúa **solo sobre las versiones realmente pendientes** de esa BD, igual que en el rollback
   (ver abajo). Y la aprobación es de una **consulta concreta**: cambiar el SQL la revoca.

Y un **kill switch** global por encima: `MIGRATION_CAPTURE_ENABLED=False` desactiva la captura
sin tocar ningún blueprint (el SQL se sigue ejecutando idéntico). Con el switch apagado el gate
**no bloquea nada**: el codegen no emite una sola llamada de captura, así que capturar es
*físicamente* imposible y un 409 solo cerraría la vía de recuperación (el `rollback` no tiene
ningún `force` con el que saltearlo).

El gate corre **después** de leer la versión actual del destino —hace falta para saber qué está
pendiente—, así que abre una conexión de solo lectura; lo que garantiza es que el 409 llega
**antes de ejecutar cualquier sentencia de la migración**. Con `dry_run=true` no bloquea:
previsualizar no ejecuta nada.

#### Hubo una tercera llave, y por qué se retiró

Existió un **consentimiento por corrida** (`?allow_result_capture=true`) que había que repetir
en cada `apply` y cada `rollback`. Se eliminó (contrato `api-reference-v13.md`). El motivo
importa, porque "agregar una confirmación más" siempre suena a mejora:

- **La premisa no aplicaba.** Se justificaba con *"un blueprint se replica sobre N BDs de dueños
  potencialmente distintos, y quien aplica sobre UNA tiene que saber"*. Esos dueños son los
  `ServerUser` de las bases **destino**; a nivel gateway hay un **administrador único**
  (`app/core/auth.py`: "no gestiona múltiples usuarios", sin roles ni permisos). La misma
  persona activa la captura, aprueba `reviewed` y dispara el apply: no era un segundo par de
  ojos, solo un segundo momento.
- **No dejaba rastro.** Pasar el flag **no se auditaba**. Lo único auditado es la escritura
  efectiva, que ocurre con o sin gate — o sea, fricción sin evidencia forense. (El guard de
  entorno, en cambio, sí registra `migration.environment_denied` al rechazar.)
- **`apply-all` ya lo contradecía**: un único query param autorizaba **N bases** de entornos
  distintos, exactamente lo contrario de "conciencia de ESTA base".
- **Saltaba donde el riesgo era menor.** Una BD nueva arranca sin versión, así que recibe la
  cadena completa y arrastra versiones históricas cuya captura tenía sentido sobre bases con
  datos. Sobre una base recién creada esos `SELECT` devuelven cero filas. Un gate que se dispara
  sobre todo en el caso inofensivo entrena el reflejo "siempre que sí", y ese reflejo después se
  aplica también en producción: el control quedaba **más débil**, no más fuerte.

**Qué lo reemplaza** (información, no fricción):

- `apply?dry_run=true` devuelve `will_capture_versions`: el pronóstico de qué versiones van a
  extraer filas, en la llamada que existe para decidir.
- La respuesta real trae `captured_select_count` y `captured_versions` — esta última es lo que
  permite enlazar a `…/{version}/select-results` sin adivinar.
- La auditoría de **intento** (`status="attempt"`, escrita antes de tocar el motor) nombra las
  versiones con captura de esa corrida. Eso es evidencia que el consentimiento nunca produjo, y
  queda incluso si la migración muere a mitad.

**Compatibilidad**: FastAPI ignora los query params que no declara, así que un cliente que
siga mandando `?allow_result_capture=true` recibe 200 en vez de romperse.

#### Las dos llaves rigen en AMBAS direcciones

El codegen emite `capture_statement` también para las sentencias del `down_sql`, así que un
**`rollback` extrae y persiste datos exactamente como un `apply`**. Por eso
`POST .../migrations/rollback` responde **409** si alguna versión del camino a revertir tiene la
captura activada y no está revisada.

El gate de `reviewed` del rollback se evalúa **solo sobre las versiones del camino**, no sobre
el blueprint completo. El rollback es la vía de **recuperación** ante una migración mala;
bloquearlo por una versión futura sin revisar —que no se va a ejecutar— le quitaría al
operador su única salida. `apply` sigue **el mismo criterio** (sobre sus pendientes reales):
mirar todo el blueprint partía de una premisa falsa —que lo aplicado es siempre la cadena
completa— cuando `apply?version=X` aplica un prefijo **estricto**, así que con 0001..0010 y
solo 0010 sin revisar, un `apply?version=0007` devolvía 409 nombrando una versión que esa
corrida no iba a tocar. En `apply-all` el 409 sale **por BD**, con las pendientes de cada una.

`stamp` no ejecuta SQL, pero **es lo que habilita el `rollback` de una versión**: marcar una
versión con captura aún sin revisar responde **409**. `force=true` lo omite, con el mismo
significado que ya tiene en ese endpoint ("reconcilié el estado físico a mano") — hace falta
como escape real: una versión aplicada hace meses a la que después se le activó la captura
queda `reviewed=false`, y una BD que perdió su puntero de versión necesita poder
re-stampearla.

#### La aprobación es de una CONSULTA, no de la versión

Si el SQL de una versión con captura cambia por `PATCH` (`up_sql`, los overrides por motor o
el `down_sql`), `reviewed` **vuelve a `false`** automáticamente y hay que re-aprobar. Sin
esto había un camino real: crear con `capture_selects=true` y `up_sql='SELECT 1'` → aprobar
(revisión legítima de algo inocuo) → `PATCH up_sql='SELECT * FROM clientes'` (permitido
mientras no haya una aplicación **exitosa**) → `apply` pasaba el gate sin que nadie hubiera
visto la consulta que realmente se iba a ejecutar y capturar.

El reset **gana** sobre un `reviewed=true` enviado en la misma llamada que el SQL nuevo
(aprobar y reescribir en un solo request no es una revisión verificable) y se **audita** con
su motivo. En una versión **sin** captura, cambiar el SQL no toca `reviewed`: el gate R1 de
los baselines de snapshot sigue funcionando como antes.

### Qué se captura y qué no

Candidata solo la **lectura pura**, decidida por `query_policy.classify_statement` (AST con
sqlglot, nunca por palabra clave) tras un pre-filtro barato de forma
(`^(select|with|table|values)`). Un `WITH d AS (DELETE … RETURNING *) SELECT * FROM d`
tiene raíz `Select` y **no** se captura: el clasificador ve la escritura. Un
`CREATE PROCEDURE … BEGIN … SELECT … END` es **una** sentencia del splitter que arranca con
`CREATE`, así que no pasa el pre-filtro y no necesita ningún caso especial.

**Los comentarios iniciales no cuentan.** El pre-filtro salta blancos **y comentarios** antes
de la palabra clave (`sql_dialect.strip_leading_noise`: `-- …`, `/* … */` multilínea, y `#`
**solo** en MySQL/MariaDB — en PostgreSQL `#` es el XOR de enteros, mismo matiz que
`query_policy._scan_normalize`). No es un detalle cosmético: `split_sql_statements` **conserva**
los comentarios dentro de la sentencia que emite, y una sentencia de verificación real casi
siempre viene precedida del comentario que explica qué verifica — anclado solo en blancos, el
pre-filtro rechazaba el caso de uso más común y no capturaba nada, **en silencio** (el endpoint
de lectura deriva `expected_indexes` con la misma función, así que `missing_indexes` tampoco lo
denunciaba). El clasificador AST sí parsea comentarios, y el texto que se ejecuta y se hashea
nunca se altera. Un comentario de bloque **sin cerrar** no se salta: la sentencia no pasa el
pre-filtro (fail-closed).

En este camino, un veredicto `blocked` del clasificador significa **únicamente "no
capturar"**, nunca "rechazar la migración": un `GRANT` o un `SET FOREIGN_KEY_CHECKS=0` dentro
de un blueprint se sigue ejecutando exactamente como antes.

### Lectura

```bash
# Resultados capturados de una versión sobre esta BD (lectura AUDITADA, fail-closed)
curl -b cookies.txt \
  "$API/managed-databases/12/migrations/0007/select-results"

# Purga manual (además expiran solas por TTL)
curl -b cookies.txt -X DELETE \
  "$API/managed-databases/12/migrations/0007/select-results"
```

La respuesta trae, además de `items[]` (con `statement_index`, `sql`, `columns`, `rows`,
`row_count`, `truncated`, `status`, `durability`, `captured_at`):

- `stale`: alguna captura se tomó con un `checksum` distinto al actual de la versión;
- `expected_indexes` / `missing_indexes`: qué índices de sentencia serían capturables hoy y
  cuáles no tienen captura (típicamente porque la migración murió antes de llegar);
- `durability_warning`: presente si alguna fila quedó `rolled_back`.

`apply`/`rollback` **no** devuelven filas, solo punteros (`captured_select_count`,
`select_results_available`): con `LOGGER_MIDDLEWARE_SHOW_BODY=true` esas filas terminarían en
el log de aplicación. Ese contador es **lo que escribió esa corrida** (lo devuelve
`migration_results.finalize`), no un `COUNT` de la tabla: contando la tabla, una versión
aplicada con captura y después revertida con un `down_sql` sin lecturas hacía que el
`rollback` informara `captured_select_count: 1` y **auditara una escritura que nunca
ocurrió**. Un `0` no significa "no hay nada que leer": las capturas de corridas anteriores se
siguen leyendo con `GET .../select-results`.

### Durabilidad: la diferencia de motor que hay que entender

| Motor | Cómo se persiste | `durability` |
|---|---|---|
| MySQL / MariaDB (AUTOCOMMIT) | cada captura se escribe **de inmediato** en la BD del gateway, con su propia sesión corta | `committed` desde el origen |
| PostgreSQL (transaccional) | las capturas se **acumulan en memoria** y se vuelcan en un lote al salir de `command.upgrade()`/`downgrade()` | `committed` o `rolled_back` según el desenlace real |

El buffer de PostgreSQL no es una optimización: escribir en la BD del gateway mientras la
transacción del destino sigue abierta deja esa conexión `idle in transaction`, y el
`idle_in_transaction_session_timeout` (15 s por defecto en `remote_engine`) puede **abortar
la migración** por culpa de una escritura lenta a la BD de metadatos.

Una fila `rolled_back` describe datos que el motor **deshizo**: es diagnóstico válido de lo
que se vio durante el intento, **no** el estado final de la base. El endpoint lo avisa.

### Garantías que no se negocian

- **Nunca aborta la migración.** El `SELECT` se ejecuta primero; si la *captura* falla (tipo
  no serializable, encoding roto, la BD del gateway no responde) se registra
  `status='error'` con un motivo acotado —nunca `str(exc)` del motor— y la migración sigue.
- **Nunca reescribe el SQL.** A diferencia de la [consola SQL](sql-query-console.md), que
  empuja un `LIMIT` al motor, acá el texto que corre tiene que ser byte a byte el del
  `checksum`. Los topes se aplican al capturar (`fetchmany(max_rows + 1)`, para poder marcar
  `truncated` con certeza).
- **Nunca toca la lista de sentencias.** El índice persistido es el mismo del checkpoint y la
  lista sale siempre de `statement_lists`; si la captura re-partiera el `up_sql` por su
  cuenta, el conteo cambiaría y `_resolve_resume_offset` dispararía un 409 espurio.
- **Con `capture_selects=false` el archivo de revisión generado es byte a byte el de
  siempre** (hay un test que lo verifica): cero riesgo de regresión sobre el checkpoint.
- Un **resume** no re-ejecuta ni re-captura una sentencia ya aplicada; la captura del intento
  anterior se conserva tal cual.

### Seguridad y retención

- **Cifrado en reposo**: `columns_json`/`rows_json` van cifrados con la DEK
  (`app/core/crypto.py`, envelope KEK/DEK). Efecto colateral buscado: no son legibles por SQL
  directo contra la BD del gateway, así que **todo** acceso pasa por el endpoint auditado.
- **Auditoría**: la lectura audita con `record_intent` (fail-closed) **antes** de descifrar,
  igual que `reveal-password`; la escritura audita conteos (`migration.select_results.write`),
  nunca valores; la purga también.
- **TTL**: `MIGRATION_CAPTURE_TTL_HOURS` (default `168` = 7 días). La purga corre en el
  arranque (`lifespan`), **se repite cada `MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES`** (default
  60) mientras el proceso vive, y hay endpoint de purga manual. La tarea periódica no es un
  adorno: con la purga solo del arranque, un gateway que corre semanas —lo normal— nunca
  volvía a purgar y el TTL era una promesa falsa. Corre en un hilo
  (`asyncio.to_thread`) porque `purge_expired` hace I/O síncrono, y se cancela y se espera en
  el apagado del `lifespan` (sin eso el intérprete emite `Task was destroyed but it is
  pending`).
- **Purga en cascada por edición**: un `PATCH` que cambia el SQL —`up_sql`, un override por
  motor **o** el `down_sql`— borra las capturas de esa versión en la misma transacción. No
  pueden quedar colgadas de un SQL que dejó de existir: su `statement_index` apuntaría a otra
  sentencia. (El `down_sql` no entra en el freeze de "ya aplicada" ni borra el manifiesto del
  `up`, pero sí purga: reordena `down_statements`.) **Limpiar un override con `null` cuenta
  como cambio de SQL** —el SQL efectivo de ese motor pasa a ser la traducción del `up_sql`
  base—, así que también purga y también revoca `reviewed`; exigir un valor no nulo dejaba
  abierto el camino "aprobar un override inocuo → borrarlo".
- **Topes en BYTES UTF-8**: `MIGRATION_CAPTURE_MAX_BYTES` y el `payload_bytes` que viaja por
  la API se miden con el JSON **codificado**. Medidos en caracteres, un resultado con
  CJK/emoji pesaba 3-4× el tope y el campo reportaba caracteres con nombre de bytes.

### Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `MIGRATION_CAPTURE_ENABLED` | `True` | Kill switch global |
| `MIGRATION_CAPTURE_MAX_ROWS` | `100` | Filas por sentencia (recorte solo de la captura) |
| `MIGRATION_CAPTURE_MAX_CELL_CHARS` | `1024` | Caracteres por celda |
| `MIGRATION_CAPTURE_MAX_BYTES` | `262144` | Bytes del JSON en claro de una captura |
| `MIGRATION_CAPTURE_SQL_MAX_CHARS` | `4096` | SQL guardado junto a la captura (redactado) |
| `MIGRATION_CAPTURE_TTL_HOURS` | `168` | Retención (`0` = indefinida) |
| `MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES` | `60` | Cada cuánto se repite la purga por TTL (`0` = solo en el arranque) |

### Archivos

`app/services/db_admin/migration_results.py` (clasificación + empaquetado + persistencia
híbrida), `app/services/db_admin/value_json.py` (serializador de valores del driver,
compartido con la consola SQL), `app/models/migration_select_result.py`, migración
`f8a9b0c1d2e3`, hook en `migrations.py::_render_statement_calls`, guards y endpoints en
`managed_migration_controller.py` (`_guard_reviewed_capture`, `_guard_capture_consent`,
`_guard_stamp_unreviewed_capture`) / `routes/v1/managed_databases.py`, revocación de la
aprobación al editar el SQL en `model_migration_controller.py::update_migration`, y la tarea
periódica de purga en `main.py::_purge_captures_periodically`.

## Límites y consideraciones

| Límite | Valor |
|---|---|
| Tamaño de cada campo SQL | 256 KB (422 si se excede) |
| `apply-all` por request | `max_databases` ≤ 100 (síncrono) |
| Rate limit `apply`/`rollback`/`stamp`/`reconcile-partial` | 10/min |
| Rate limit `apply-all` | 3/min |
| Rate limit `GET .../select-results` | 20/min |
| Concurrencia | advisory lock por BD; `command.*` serializado en el proceso (multiprocessing = Plan 06) |

## Verificación

- Tests unitarios (SQLite): `tests/test_api_model_migrations.py`,
  `tests/test_migration_runner.py`, `tests/test_migration_integrity.py`,
  `tests/test_sql_dialect.py`, `tests/test_api_migrations_apply_flow.py`,
  `tests/test_api_migrations_rollback_flow.py`.
- Verificación e2e contra motores reales (MySQL 8 / MariaDB 11 / PostgreSQL 16): script
  `scripts/verify_migrations_e2e.py` (requiere Docker) — **ejecutado: 153 checks / 0 fallos**
  (2026-06-29), cubre apply/rollback secuencial, gate `reviewed`, autoasignación de versión y los
  flujos de [adopción/snapshot](adoption-reconcile-snapshot.md) (Plan 09). Pendiente: integrarlo en
  CI con testcontainers (Plan 08).
- **Checkpoint de sentencia (resume automático) — pendiente de verificación e2e**: revisado
  en diseño con los agentes `gateway-senior-python`/`gateway-db-dialects` (ventana de doble
  escritura, binding por checksum, exclusión de `BEGIN...END`/estado de sesión), verificado
  con `ast.parse`/compilación del codegen y ejecución directa de las funciones puras
  (`is_resumable`, generación del archivo de revisión) — **no** verificado contra un motor
  real (sin Docker/MySQL disponibles en el entorno donde se implementó) ni con la suite
  `pytest`. Antes de confiar en producción: correr el ciclo apply parcial → reintento
  contra MySQL/MariaDB/PostgreSQL reales, y la migración Alembic
  (`d1e2f3a4b5c6_add_migration_statement_progress`) con upgrade/downgrade/upgrade contra la
  BD del gateway real.
- **Captura de resultados de `SELECT` — pendiente de verificación e2e**: `tests/test_migration_results.py`
  (funciones puras, dobles de conexión e invariantes del codegen, 69 casos — incluye el
  pre-filtro con comentarios por motor, los topes en bytes UTF-8, el conteo por corrida y el
  alcance del gate de `reviewed`) + los casos de API del gate acotado y del kill switch en
  `tests/test_api_migrations_apply_flow.py` y de la revocación/purga por `PATCH` en
  `tests/test_api_migrations_stamp_and_edit.py` + ciclo Alembic
  real `command.upgrade` contra **SQLite** con captura activada (el resultado se persistió
  cifrado y se descifró de vuelta) + `upgrade/downgrade/upgrade` de la migración
  `f8a9b0c1d2e3` contra SQLite. **No** verificado contra MySQL/MariaDB/PostgreSQL reales
  (sin Docker en el entorno donde se implementó). Lo crítico a confirmar ahí: (a) que el
  buffer de PostgreSQL evite de verdad el `idle_in_transaction_session_timeout` y que un
  fallo marque `rolled_back`; (b) que en MySQL/MariaDB la captura inmediata sobreviva a un
  fallo posterior de la migración; (c) tipos nativos por motor en el serializador
  (`JSONB`, `TIME` de MySQL como `timedelta`, `bytea`/`BLOB`); (d) la migración Alembic con
  ciclo completo contra la BD del gateway real.

---

**Siguiente:** [Clonado de bases de datos](../plans/05-clonado-de-bases-de-datos.md) ·
[Operación: seguridad, auditoría y observabilidad](../plans/06-operacion-seguridad-observabilidad.md)
