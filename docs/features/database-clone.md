# Clonado de bases de datos entre servidores

Clona la **estructura** (y opcionalmente **todos los datos**) de una BD ORIGEN hacia una BD
DESTINO en cualquier servidor dado de alta — el mismo u otro, **mismo motor o distinto**. Ni
el origen ni el destino necesitan estar adoptados por el gateway, ni tener un blueprint; el
destino puede no existir todavía. Cierra el hueco entre el
[diff de esquema](schema-comparison.md) (que compara dos BDs existentes) y una copia real
"llevá esta BD a este otro servidor".

## Flujo (plan → preview → confirmar → ejecutar asíncrono)

Mismo patrón seguro que [schema-comparisons](schema-comparison.md): el servidor es la única
fuente de verdad; el cliente confirma con `confirm_token` + `confirm_target_name`.

1. **`POST /database-clones`** — crea un PLAN: resuelve origen/destino, valida existencia en
   vivo, snapshotea el origen (solo lectura) y persiste `CloneJob` (estado `pending`) con un
   `source_fingerprint` (anti-TOCTOU) y TTL (`CLONE_TTL_HOURS`, 24h).
2. **`GET /database-clones/{id}/objects`** — inventario del origen: cada objeto con su
   **portabilidad** al motor destino + el **grafo de dependencias** (aristas autoritativas y
   advisory) para que el frontend arme el árbol de selección.
3. **`POST /database-clones/{id}/resolve-selection`** — dado un conjunto de objetos elegidos,
   devuelve el **cierre de dependencias** (lo que se agrega automáticamente) + las
   **sugerencias advisory**. Es el "seleccioná uno y traé lo necesario" de la UI.
4. **`POST /database-clones/{id}/preview`** — **acá se manda el SPEC** (qué copiar, con qué
   charset, con qué owner), se **congela** y se resuelve el plan final SIN ejecutar:
   sentencias de limpieza + estructura (DDL exacto en el dialecto destino), tablas de datos,
   objetos `skipped` (no portables), avisos y el `confirm_token`.

   El spec va acá y no en `create` porque el catálogo de objetos del origen solo se puede
   listar con un `job_id` (paso 2): pedirlo en `create` obliga a elegir a ciegas y a recrear
   el plan —con su snapshot en vivo, a 10/min— por cada retoque. Mismo criterio que el
   [export](database-export.md).

   Solo se aplica lo que **viene** en el cuerpo; un campo ausente deja el valor que el plan ya
   tenía. Si el esquema del destino impide la copia, la respuesta es **200 con
   `blocking_issues` y `confirm_token` vacío**: el plan se puede ver, pero no confirmar.
5. **`POST /database-clones/{id}/execute`** — valida `confirm_target_name` + `confirm_token` +
   re-chequea el fingerprint del origen (anti-TOCTOU) + cuarentena, registra la intención
   (auditoría fail-closed) y **encola** el job asíncrono. Rate limit 3/min.
6. **`GET /database-clones/{id}`** — resumen + estado (`pending`→`running`→`succeeded`/`failed`/
   `interrupted`/`canceled`) + `phase` + `progress` (para polling).
7. **`GET /database-clones/{id}/items`** — pasos ejecutados (limpieza/estructura/datos/adopt)
   con su resultado por ítem. **`POST /database-clones/{id}/cancel`** — cancelación cooperativa.

## Opciones del plan

### En `POST /database-clones` (identidad y contenedor)

- **`target_mode`**: `new` (crea la BD; 422 si ya existe) | `existing` (404 si no existe).
- **`clean_mode`** (solo destino existente): `none` (preservar) | `objects` (borrar objeto por
  objeto en orden topológico inverso, **preservando la BD y su configuración** —
  charset/collation/grants) | `drop_database` (**reset total**: DROP + CREATE).
- **`adopt_target`** + **`adopt_owner_id`**: solo en clon **completo** desde un origen
  gestionado **con blueprint**: al terminar, adopta el destino (`origin='adopted'`) y le
  **stampa** el `model_id` + `model_version` del origen (sin re-ejecutar DDL). `adopt_owner_id`
  debe ser un `ServerUser` del servidor **destino**.
- **`include_data`** y **`selection`**: atajos **LEGACY** que se siguen aceptando y se traducen
  al spec nuevo (`include_data: true` ⇒ `copy_intent: "structure_and_data"`). Mandar el eje
  legacy y su equivalente nuevo a la vez es 422.

### En `POST /database-clones/{id}/preview` (el spec)

**`copy_intent`** — qué se copia. Es la INTENCIÓN, no un eje técnico:

| Valor | Qué hace |
|---|---|
| `structure_only` | La estructura seleccionada, sin filas. |
| `structure_and_data` | Estructura + filas. |
| `data_only` | **Solo filas**, contra objetos que ya existen en el destino. **No se emite una sola sentencia de DDL.** |

`data_only` exige `target_mode='existing'`, `clean_mode='none'` y `data.on_existing` explícito,
y es incompatible con `adopt_target` (adoptar el destino y stampearle una versión de blueprint
afirma que la estructura la puso este job).

> **Por qué el contrato expone una intención y no el enumerado de cuatro valores del
> [export](database-export.md).** De los cuatro, el clon solo puede cumplir dos:
> `DROP_CREATE` por entidad destruiría los permisos y triggers del objeto en el destino (y su
> caso de uso ya lo cubre `clean_mode='objects'`), y `CREATE_IF_NOT_EXISTS` está **roto** para
> todo lo que no sea una tabla — `export_make_idempotent` filtra por `object_type` y en la
> familia MySQL solo acepta `{table, event}`, mientras el render del diff emite `index`,
> `foreign_key`, `column`… así que esas sentencias saldrían crudas y morirían con 1061/1826
> dejando estructura parcial y la BD en cuarentena. Publicar un enumerado donde la mitad
> responde 422 es una promesa que el servidor no cumple. `EntityDdl` se sigue usando
> internamente. Si algún día se quiere "creá solo lo que falta", el mecanismo correcto no es
> `IF NOT EXISTS` por texto: es diffear contra el snapshot del destino, que es literalmente
> [schema-comparisons](schema-comparison.md).

**`structure`** y **`data`** — selección DECLARATIVA, resuelta contra el catálogo del origen con
la misma función que usa el export:

```jsonc
{
  "copy_intent": "data_only",
  "data": {
    "mode": "all_except",              // none | all | include | all_except
    "exclude_patterns": ["log_*"],     // fnmatch sobre nombres del catálogo, NUNCA SQL
    "on_existing": "append"            // append | upsert
  }
}
```

- La **exclusión gana** sobre la inclusión.
- Los datos solo salen de **tablas**.
- `mode: "include"` con un nombre que el origen no tiene es **422**: sin eso, un nombre mal
  tecleado daba un job `succeeded` con 0 filas copiadas.
- El match de `names` es por **nombre, sin tipo**: si una tabla y una rutina se llaman igual,
  entran las dos. `types` desambigua.
- La selección de datos **se cierra por FK**. Pedir solo la tabla hija arrastra a la madre, y
  el preview lo muestra. Sin ese cierre se insertarían filas huérfanas **sin ningún error**,
  porque la fase de datos corre con las FKs desactivadas y el motor nunca las revalida.
- `selection` (refs exactas) y `structure` (declarativa) son **mutuamente excluyentes**.

**`data.on_existing`** — qué hacer si la tabla destino ya tiene filas. **Obligatorio y sin
default en `data_only`**: con la estructura creándose, la fase de datos nunca llegaba a una
tabla preexistente, así que heredar el `upsert` derivado como default sería estrenar un default
destructivo disfrazado de compatibilidad. `upsert` sobre una tabla **sin clave primaria**
degrada a `INSERT` simple, así que reejecutar el job duplicaría filas: el preview lo avisa con
`clone.upsert_without_primary_key`.

> **Vaciar la tabla destino antes de copiar (`truncate`) todavía no existe.** Requiere el
> cierre por FK del lado del DESTINO (un `TRUNCATE` aislado sobre una tabla referenciada falla
> en PostgreSQL incluso con los triggers en `replica`), confirmación propia de pérdida de
> filas, semántica de cancelación, y verificación contra motores reales de un comportamiento
> que en MySQL con `FOREIGN_KEY_CHECKS=0` es **indocumentado**. Mientras tanto, para reemplazar
> el contenido existe `clean_mode='objects'`.

**`target_charset`** — charset/collation de la BD destino, discriminado por `mode`:

```jsonc
{ "target_charset": { "mode": "override", "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci" } }
```

- `keep` (default) = heredar del origen si es el mismo motor, o el default del motor destino si
  es cross-engine. Con `keep` no se admiten `charset`/`collation` (antes se aceptaban y se
  ignoraban en silencio).
- Solo aplica cuando el job **crea** la BD (`target_mode='new'` o `clean_mode='drop_database'`);
  en otro caso, 422. Convertir la collation de una BD existente es
  [otra operación](collation-conversion.md).
- Dos validaciones, y la segunda no es opcional: el par tiene que estar **habilitado en el
  catálogo** del gateway (y lo que viaja al DDL es su forma **canónica**, nunca el texto del
  request) **y** el servidor destino tiene que ofrecerlo. El catálogo es necesario y no
  suficiente: `engine_family` mete MySQL y MariaDB en la misma familia y no comparten todas las
  collations (`utf8mb4_0900_ai_ci` es solo de MySQL 8; las `utf8mb4_uca1400_*` solo de MariaDB
  reciente), y en PostgreSQL la collation es un **locale del sistema operativo** del host. Que
  esto se valide en el `preview` es lo que evita el peor caso: con `clean_mode='drop_database'`
  el worker hace DROP y después CREATE, así que un par que el motor rechaza dejaría el destino
  **borrado**.
- Elegir un charset distinto del origen tiene una consecuencia que el preview avisa: las tablas
  que se crean sin collation explícita heredan el default de la BD, así que un diff posterior
  origen↔destino va a reportar diferencias. Y el síntoma es **asimétrico**: en MySQL/MariaDB el
  diff grita en toda columna textual; en PostgreSQL `collation_name` es NULL cuando se hereda,
  así que el diff se queda **callado** mientras el orden real de los índices cambió.

**`target_owner_user_id`** — `ServerUser` del servidor destino que será `OWNER` de la BD creada.
**Solo PostgreSQL** (en MySQL/MariaDB una base no tiene dueño: 422). Y fija el dueño **de la
base**, nada más: las tablas, vistas y secuencias las crea la credencial administrativa del
gateway y quedan con **su** propiedad, así que el dueño pedido no puede `ALTER` sus propios
objetos. El preview lo dice explícitamente; reasignarlos requiere `SET ROLE`/`REASSIGN OWNED` y
es trabajo pendiente.

## Copia de datos (streaming, asíncrona)

A diferencia del [datos-semilla del snapshot selectivo](adoption-reconcile-snapshot.md) —
capado a propósito como "seed de catálogo, no ETL" — el clon usa un **copiador por streaming**
(`app/services/db_admin/data_copy.py`) sin tope práctico de filas:

- Lee del origen con `stream_results=True` + `yield_per` (memoria acotada); es el ÚNICO punto
  que consume el cursor origen (`_iter_source_rows`), reusado por los tres writers de escritura.
- Orden **topológico** entre tablas (padre antes que hijo) y **FK checks desactivados** durante
  la fase de datos (`SET FOREIGN_KEY_CHECKS=0` / `session_replication_role='replica'`,
  restaurados en `finally`) para tolerar ciclos.
- Tablas **sin PK**: carga plana (sin upsert, sin staging). Con PK y destino preservado:
  **upsert** (`ON DUPLICATE KEY UPDATE` / `ON CONFLICT DO UPDATE`).

### Escritura al destino: protocolo bulk nativo por motor (no `INSERT` genérico)

La escritura NO usa `INSERT` parametrizado por lotes como camino principal — usa el protocolo
de ingestión masiva que cada motor expone nativamente, vía los drivers ya instalados (sin
dependencias ni binarios nuevos). El writer se elige por el **dialecto REAL de la conexión SQLAlchemy
ya abierta** (`dest_conn.dialect.name`), no por el string de negocio `dest_engine` — así un test con
SQLite que pasa `dest_engine="postgresql"` (para ejercer el mismo quoting que Postgres) cae
naturalmente en la vía legacy sin necesidad de mockear nada especial:

- **PostgreSQL** (`dest_conn.dialect.name == "postgresql"`) → `_copy_writer_postgres`:
  `COPY ... FROM STDIN` vía `psycopg3` (`cursor.copy().write_row(tupla)`), con los MISMOS
  dumpers de tipo que un `execute` parametrizado normal — no hace falta pre-serializar nada más
  allá de `_adapt_value`. Atómico: un fallo a mitad del `COPY` aborta el statement completo (0
  filas commiteadas en AUTOCOMMIT), igual que el `INSERT` legacy.
- **MySQL/MariaDB** (`dest_conn.dialect.name == "mysql"`, mismo driver `pymysql` para ambos) →
  `_copy_writer_mysql`: `LOAD DATA LOCAL INFILE` alimentado por un **FIFO en tmpfs** (`/dev/shm`,
  no toca disco), con un hilo escritor que formatea/escapa cada fila a TSV (`FIELDS TERMINATED
  BY '\t' ESCAPED BY '\\' LINES TERMINATED BY '\n'`, `NULL` → `\N`) mientras el hilo principal
  dispara el `LOAD DATA` (coordinación por bloqueo de apertura del FIFO, no hay archivo real de
  por medio). Requiere `local_infile=True` en la conexión (agregado a `_connect_args` en
  `remote_engine.py`, **solo** para conexiones `bulk=True` — el resto de conexiones no lo
  habilita) Y `local_infile=ON` en el SERVIDOR destino (variable global del motor, fuera del
  control del gateway). **Capability probe**: antes de copiar la primera tabla del job,
  `SHOW VARIABLES LIKE 'local_infile'` decide de una vez para todo el job si se usa `LOAD DATA` o
  se degrada a la vía legacy (`_copy_writer_insert`) con un `logger.warning` — es una comprobación
  de capacidad determinística, no un fallback oportunista ante fallos de datos.
  **Gotcha de integridad (por qué SIEMPRE hay staging con PK, no solo con upsert)**: la
  documentación oficial de MySQL confirma que `LOAD DATA LOCAL INFILE` **siempre** se comporta
  como `INSERT IGNORE` ante una clave duplicada ("the LOCAL modifier has the same effect as
  IGNORE" — no hay sintaxis que lo haga abortar como un `INSERT` plano, porque el servidor no
  puede cortar la transmisión del archivo a mitad). Cargar directo a la tabla final rompería el
  modelo fail-closed (una fila en conflicto se saltearía en silencio en vez de marcar la tabla
  `failed`). Por eso, con PK (haya o no upsert), el `LOAD DATA` carga siempre a una tabla
  *staging* vacía —ahí nunca hay conflicto, porque las filas del origen ya son únicas entre sí— y
  el conflicto real contra el destino se resuelve en UN SOLO statement server-side final
  (`INSERT INTO final SELECT FROM staging`, plano o con `ON DUPLICATE KEY UPDATE` según
  corresponda). PostgreSQL no tiene este problema (`COPY` directo a la final ya aborta
  atómicamente ante conflicto), así que ahí la staging solo se usa cuando hay upsert.
- **Cualquier otro dialecto** (SQLite en tests, motor no cubierto) → `_copy_writer_insert`: la vía
  legacy (`INSERT` parametrizado por lotes, `executemany`), conservada tal cual como
  compatibilidad y como respaldo.
- **Kill switch** `CLONE_BULK_COPY_ENABLED` (default `True`): en `False`, fuerza la vía legacy
  para TODO el job sin necesidad de re-desplegar código, por si el protocolo bulk da problemas en
  un destino puntual en producción.
- **Staging temporal por motor** (cuando aplica): `CREATE TEMP TABLE (LIKE final)` **plana** en
  PostgreSQL (sin `ON COMMIT DROP`/`DELETE ROWS` — en AUTOCOMMIT cada statement es su propia
  transacción y la destruiría/vaciaría de inmediato tras el `CREATE`) / `CREATE TEMPORARY TABLE
  ... LIKE final` en MySQL/MariaDB (session-scoped). Nombre con sufijo aleatorio
  (`_gw_stg_<uuid12>`, validado por la misma whitelist de identificadores que cualquier objeto),
  dropeada explícitamente en `finally` de cada tabla (la conexión de destino se reusa para todas
  las tablas del job).
- **Limitación preexistente, no una regresión de este cambio**: `_adapt_value` convierte
  `list`/`dict` Python a texto JSON antes de escribir — una columna PostgreSQL de tipo ARRAY
  nativo (`int[]`/`text[]`) alimentada con un `list` no queda bien representada. Afecta por igual
  al `INSERT` legacy y al `COPY` (ambos reciben el mismo valor ya adaptado); no se resolvió aquí.
- **Columnas `GENERATED`/computadas**: se excluyen de la copia de datos (`c.computed is None`
  al armar `_DataSpec.columns` en `CloneController`) — el motor las recalcula solo; escribirles
  un valor explícito dispara el warning 1906 de MySQL, que en `sql_mode` estricto se promueve a
  error y aborta la tabla completa.
- **`ENUM` con valores "grandfathered" fuera de rango (MySQL/MariaDB, decisión de producto)**:
  MySQL representa un valor de `ENUM` inválido, insertado alguna vez sin modo estricto, como el
  string vacío `''` (el "valor especial de error" del ENUM — documentado en el manual, "13.3.5
  The ENUM Type"). Si el origen tiene filas viejas con ese `''`, reinsertarlo en un destino con
  `STRICT_TRANS_TABLES`/`STRICT_ALL_TABLES` activo (default en MySQL 8/MariaDB moderno) lo
  rechaza (1265) en vez de aceptarlo como hace el origen. `copy_tables` relaja **solo esos dos
  modos** (`_relax_strict_mode`/`_restore_sql_mode`, preserva el resto del `sql_mode`) en la
  conexión de escritura, **solo durante la fase de datos** (no DDL), y los restaura al terminar
  — best-effort, solo MySQL/MariaDB (PostgreSQL no tiene `sql_mode` ni esta ambigüedad, sus enums
  son siempre estrictos). **Trade-off explícito**: cualquier OTRO truncamiento de datos en
  cualquier tabla/columna del mismo job también se coerciona en silencio en vez de fallar esa
  tabla — decisión de producto para priorizar fidelidad con el origen sobre fail-closed en este
  caso puntual, no un descuido.
- **Pendiente de verificación e2e contra motores reales** (no hay Docker disponible en el entorno
  donde se implementó este cambio): coordinación del hilo escritor del FIFO bajo carga real,
  escaping de BLOB/JSON/timestamptz con datos reales, que el `INSERT ... SELECT` desde staging
  efectivamente aborte con `ER_DUP_ENTRY`/`23505` ante conflicto real, y el comportamiento del
  probe de `local_infile` contra un servidor con la variable en `OFF`. Extender
  `scripts/verify_clone_e2e.py` con estos casos antes de confiar en el camino bulk en producción.
- **Fidelidad del tipo (MySQL/MariaDB)**: el tipo de columna se captura de
  `information_schema.COLUMN_TYPE`, NO de `str(reflected_type)` — que pierde detalle crítico
  (`ENUM`/`SET` **sin su lista de valores** → CREATE TABLE inválido; `UNSIGNED` → rango corrupto;
  display width). Así el DDL del clon reproduce `enum('a','b')`, `bigint(20) unsigned`,
  `tinyint(1)`, etc. exactamente.
- **Los datos NUNCA se traducen cross-engine** por sintaxis, pero los valores escalares se
  adaptan por driver; tipos riesgosos (arrays/enums/JSON/geometría) pueden fallar por tabla y
  se reportan (best-effort por tabla, el resto continúa).

### Asimetrías MySQL/MariaDB ↔ PostgreSQL que el pipeline compensa explícitamente

Dos comportamientos que MySQL/MariaDB resuelve solo (por su motor de almacenamiento) y que
PostgreSQL requiere un paso explícito — sin este, el clon quedaría con datos/comportamiento
incompleto en el destino, en silencio:

- **Secuencias `IDENTITY`/`SERIAL` desincronizadas en destino PostgreSQL**: MySQL/InnoDB ajusta
  `AUTO_INCREMENT` solo al insertar un valor explícito mayor al contador actual; PostgreSQL NO
  hace eso — ni `INSERT ... OVERRIDING SYSTEM VALUE` ni `COPY` (que es como este módulo escribe
  los datos; confirmado contra la documentación oficial que `COPY` sí puede escribir valores
  explícitos en una columna `IDENTITY` sin fallar) avanzan la secuencia asociada. Sin corregirlo,
  el primer `INSERT` real de la aplicación que dependa del default/identity generaría un ID que
  ya existe en una fila clonada → choque de PK. `CloneController._resync_postgres_identity_sequences`
  corre, tras copiar los datos, `SELECT setval(pg_get_serial_sequence(tabla, columna), MAX(columna), true)`
  por cada columna `identity` de cada tabla copiada con éxito (solo si tuvo ≥1 fila) — solo
  cuando el destino es PostgreSQL. Best-effort: un fallo aquí no revierte los datos ya copiados.
- **`ON UPDATE CURRENT_TIMESTAMP` (columna MySQL/MariaDB) se pierde en silencio al clonar hacia
  PostgreSQL**: PostgreSQL no tiene una cláusula de columna equivalente — se implementa con un
  `TRIGGER`. `PostgresAdapter._render_on_update_trigger_statements` (que engancha en
  `_ri_table_new`/`_ri_column_new`, compartidos con `render_diff` de
  [schema-comparison](schema-comparison.md) — **el fix aplica a ambas features por igual**)
  genera, para cada columna origen con `on_update`, una función `CREATE OR REPLACE FUNCTION` +
  `DROP TRIGGER IF EXISTS`/`CREATE TRIGGER BEFORE UPDATE` que fija `CURRENT_TIMESTAMP` en esa
  columna. Nombre determinista (hash de tabla+columna, no aleatorio) → idempotente entre
  corridas, no acumula duplicados. Solo se activa cross-engine (MySQL/MariaDB → PostgreSQL);
  PostgreSQL nunca setea `on_update` en su propia introspección.

Ambos confirmados contra la documentación oficial de PostgreSQL 16 / MySQL 8.0 (sin verificación
e2e contra motores reales en el entorno donde se implementaron — ver limitaciones más abajo).

### Tablas grandes (consideraciones)

- **Timeout de volcado (bulk)**: la copia NO usa el timeout interactivo de 15s
  (`REMOTE_STATEMENT_TIMEOUT_MS`) que cancelaría lotes de tablas grandes y dejaría datos
  parciales. Usa un timeout separado `REMOTE_BULK_STATEMENT_TIMEOUT_MS` (default **1 hora**;
  `0` = sin límite). Aplica a la conexión de lectura (origen) y de escritura (destino); en PG
  es `statement_timeout`, en MySQL/MariaDB los `read/write_timeout` de socket.
- **Commit por lote (AUTOCOMMIT)**: cada lote se confirma solo → no se acumula una transacción
  gigante (que reventaría undo/redo). El reverso: un fallo a mitad de una tabla grande deja los
  lotes ya confirmados; **no hay reanudación** todavía — reintentar exige `clean_mode=drop_database`
  o dropear el destino y recopiar desde cero.
- **Aislamiento de lectura**: la sesión de origen baja a `READ COMMITTED` para que una lectura
  larga no pinnee un read-view e infle el undo/history del ORIGEN (la consistencia point-in-time
  cross-tabla ya no está garantizada de todas formas — se copia tabla por tabla, con FKs off).
- **Progreso throttleado**: el avance por filas se persiste a la BD del gateway a lo sumo cada
  ~3s (no un `UPDATE` por lote), para no martillar la metadata en tablas de millones de filas.
- **Límites conocidos**: el lote es por **filas** (`CLONE_DATA_BATCH_ROWS`, default 1000), no por
  bytes — en tablas MUY anchas con `TEXT`/`BLOB`/JSON grandes puede acercarse a
  `max_allowed_packet` (MySQL); si aparece "packet too large", bajar `CLONE_DATA_BATCH_ROWS`. Sin
  paralelismo entre tablas (secuencial). Batching adaptativo por bytes y reanudación = futuro.

## Guard de compatibilidad del destino (`copy_intent: data_only`)

Cuando este job **no** crea la estructura, las filas van a objetos que el gateway no construyó,
así que antes de tocar el motor se compara el esquema del origen contra el del destino para las
tablas que reciben datos. Vive en `clone_spec.data_compat_issues` (puro, sin motor) y **no
lanza**: devuelve una lista, el `preview` la muestra y el `execute` la rechaza. Si lanzara desde
el armado del plan, el operador recibiría "incompatible" sin poder ver el plan ni el resto de
los avisos.

**La calibración de bloqueante vs aviso es POR MOTOR, y no es una preferencia:**

- En **PostgreSQL** `COPY … FROM STDIN` valida los tipos y aborta la tabla de forma atómica: el
  motor es la red de seguridad y un aviso alcanza.
- En **MySQL/MariaDB el motor no puede fallar.** El pipeline escribe con `LOAD DATA LOCAL
  INFILE`, y el modificador `LOCAL` se comporta **siempre** como `IGNORE` y **anula el
  `sql_mode` restrictivo**. Un truncado de string, un `DECIMAL` redondeado, un `unsigned` fuera
  de rango, un valor de `ENUM` que el destino no tiene o una colisión de clave única se
  convierten en **warnings o filas descartadas, sin error** — y `rows_copied` cuenta filas
  escritas al FIFO, no filas insertadas, así que el job reporta éxito con datos perdidos. Ahí
  este guard es la **única** defensa.

| Caso | MySQL/MariaDB | PostgreSQL |
|---|---|---|
| La tabla no existe en el destino (o el nombre es una vista) | bloquea | bloquea |
| Columna del origen ausente en el destino | bloquea | bloquea |
| Columna del destino `NOT NULL` sin default que el origen no aporta | bloquea | bloquea |
| Columna del destino `GENERATED` o `IDENTITY ALWAYS` | bloquea | bloquea |
| Narrowing de tipo/longitud/precisión, `ENUM` que no cubre, `unsigned`→firmado | **bloquea** | aviso |
| Collation distinta en una columna de PK/UNIQUE | **bloquea** | aviso |
| UNIQUE o CHECK presente **solo** en el destino | **bloquea** | aviso |
| FK del destino hacia una tabla que este job no puebla | bloquea | bloquea |
| Otras diferencias de tipo | aviso | aviso |

La collation en una columna de clave bloquea por una razón concreta: `utf8mb4_bin` → `_general_ci`
hace que `'Alice'` y `'alice'` sean dos filas en el origen y **la misma clave** en el destino, así
que una se pierde —por el upsert o por el `IGNORE` implícito— sin ningún error.

**Cross-family (MySQL↔PostgreSQL) los tipos no se comparan.** `diff_snapshots` y `canonical_type`
están definidos para un mismo dialecto y entre familias los nombres difieren por diseño, así que
compararlos daría un falso bloqueo en cada columna. Se verifica presencia y nulabilidad, se avisa
que la fidelidad de tipos **no se verificó**, y se bloquea la lista corta de tipos del destino que
el pipeline no puede alimentar (arrays, geométricos, `tsvector`, `bit varying`).

> **Detalle de implementación que no se puede perder.** `schema_diff.is_narrowing(src, tgt)` está
> documentado como *"¿convertir la columna de `tgt` (actual) a `src` (deseado) pierde datos?"* —
> la dirección del **diff**. Una **copia** va al revés (el origen provee, el destino recibe), así
> que los argumentos van **invertidos** respecto del uso del diff. Leer `DiffItem.risk`, o pasar
> los argumentos en el orden "natural", clasifica al revés el 100% de los casos de longitud,
> rango y precisión: `varchar(50)` → `varchar(20)` sale como inofensivo. Hay un test dedicado.

### Triggers vivos en el destino

Toda la maquinaria que difiere la creación de triggers hasta después de la fase de datos (ver
[más abajo](#objetos-con-cuerpo-vistas-rutinas-funciones-triggers-eventos)) protege solo los
triggers que **este job crea**. En `data_only` no se crea ninguno: los del destino ya están,
vivos, y van a dispararse durante la copia.

El preview lo avisa (`clone.target_triggers_will_fire`) nombrándolos, **en los dos motores**. En
MySQL/MariaDB porque no hay defensa portable (`FOREIGN_KEY_CHECKS=0` no apaga triggers). En
PostgreSQL porque la garantía no es firme: `session_replication_role='replica'` los apaga, pero es
un `SET` de superusuario cuyo error se ignora best-effort, así que un pseudo-root sin ese
privilegio deja los triggers activos **sin que nada falle**. Decir "allá no hay nada que advertir"
sería fail-open.

## Cross-engine (portabilidad)

Clonar entre motores distintos está permitido pero es **best-effort**: se clona lo portable y
se **reporta lo omitido** (`skipped` en el preview). Reglas (`CloneController._portability`):

- Mismo motor / familia (MySQL↔MariaDB): todo portable.
- Cross-family: **tablas** portables (estructura renderizada nativamente por el adapter destino
  vía `diff(origen vs vacío)` → `render_diff`); **vistas** best-effort; **rutinas/triggers/
  events** NO portables (cuerpo procedural atado al motor de origen); **sequences/enum_types/
  extensions/materialized_views** sin equivalente directo. La traducción nativa de estructura
  es fiable en la dirección MySQL→PostgreSQL; para otras direcciones cross-family solo lo
  trivial es portable.

## Objetos con cuerpo (vistas, rutinas, funciones, triggers, eventos)

El clon **sí** replica vistas, procedimientos, funciones, triggers y eventos (no solo tablas):
se capturan en el snapshot, se rinden con `render_diff` y se ejecutan como una sola sentencia
(`exec_driver_sql`, sin re-partir por `;`, por lo que los cuerpos `BEGIN…END` no se rompen). En
el preview aparecen en `structure_statements` **después** de todas las tablas/FKs/índices
(fase 5 del pipeline de diff) — si la UI trunca la respuesta, quedan al final.

Dos cuidados propios de estos objetos (`CloneController`):

- **Re-calificación de esquema** (`_requalify_body`): MySQL/MariaDB inyectan el esquema **origen**
  en las referencias del cuerpo (p. ej. `VIEW_DEFINITION` siempre trae `` from `origen`.`tabla` ``).
  Sin corregirlo, el objeto clonado seguiría leyendo de la **BD origen** (fuga cross-database; clon
  roto si el origen cambia/desaparece). Se reescribe **solo** el calificador del esquema origen →
  destino (`` `origen`. `` → `` `destino`. ``), preservando referencias intencionales a otras bases.
  Solo aplica a la familia MySQL/MariaDB (PostgreSQL usa el schema `public`, igual en ambos lados).
- **Reintento diferido** (`_run_body_statements`): estos objetos pueden depender entre sí en
  cualquier orden (vista→vista, rutina→vista). Se ejecutan en pasadas: los que fallan por una
  dependencia aún no creada se reintentan en la siguiente. Un objeto se marca **fallido** solo
  cuando una pasada completa no crea ninguno de los pendientes (sin progreso = fallo real, no de
  orden). Las tablas/FKs/índices siguen con orden determinista de una sola pasada.
- **Triggers y eventos se crean DESPUÉS de la fase de datos** (no en la fase de estructura,
  como vistas y rutinas). Motivo: un trigger del origen con efectos secundarios de escritura
  (p. ej. `AFTER INSERT ON users` que hace `INSERT INTO users_modules_permissions ...` para
  sembrar permisos por defecto) se **dispararía durante la copia de datos** — las tablas se
  copian en orden padre→hijo, así que al copiar el padre el trigger puebla la tabla hija, y
  cuando la copia llega a esa tabla hija choca con lo ya insertado:
  `(1062, "Duplicate entry '…' for key 'PRIMARY'")`. Esto se manifiesta cuando la copia usa
  `INSERT` plano (`upsert=False`, i.e. destino **nuevo o limpiado**); con `upsert=True`
  (destino existente + `clean_mode=none`) el `ON DUPLICATE KEY UPDATE` lo enmascararía pero
  igual dejaría filas espurias. **Clave**: `SET FOREIGN_KEY_CHECKS=0` de la fase de datos
  **no** desactiva triggers en MySQL/MariaDB (PostgreSQL sí los apaga con
  `session_replication_role='replica'`, por lo que el bug era MySQL-específico). La defensa
  portable es el **orden de creación** — se difieren hasta que los datos ya están cargados,
  igual que hace `mysqldump` al recrear los triggers al final del dump. Los eventos se difieren
  junto con los triggers por el mismo motivo (pueden mutar datos). Corren aunque
  `include_data=false` (sin datos que copiar, simplemente se crean en esta fase final).

## AUTO_INCREMENT que no encabeza la PRIMARY KEY (MySQL/MariaDB)

MySQL/MariaDB (InnoDB) exige que una columna `AUTO_INCREMENT` sea la **primera columna de
alguna clave** definida en la **misma sentencia** `CREATE TABLE` (un índice creado en una
sentencia posterior no cuenta para esta validación). Algunas tablas origen "heredadas"
(p. ej. migradas de MyISAM a InnoDB sin corregir el orden, o restauradas desde un backup
antiguo) tienen una PK compuesta donde el autoincrement quedó al final
(`PRIMARY KEY (col_a, col_b, id)` con `id` AUTO_INCREMENT) — MyISAM lo tolera, InnoDB no.
Reproducir esa PK tal cual en el destino dispara `(1075, 'Incorrect table definition; there
can be only one auto column and it must be defined as a key')` al primer `CREATE TABLE`.

El clon **no reordena la PK** (se preserva fiel al origen): en cambio,
`MySQLAdapter._render_create_table` detecta si el autoincrement queda cubierto por la PK o
un `UNIQUE` inline y, si no, agrega automáticamente una `KEY` de apoyo en la misma
sentencia — no cambia el conjunto de columnas de la PK ni ningún otro objeto, solo satisface
el requisito del motor. Esto se avisa en `warnings` del preview (`POST .../preview`) para que
el operador lo vea antes de ejecutar, no solo leyendo el DDL en `GET .../items`.

**Nombre EXPLÍCITO de la KEY de apoyo (`` `_gw_autoinc_{columna}` ``, mismo patrón que
`_gw_v_`/`_gw_stg_` en otros módulos) — no dejarla sin nombre.** Regresión real encontrada:
si la KEY de apoyo se crea SIN nombre, MySQL/MariaDB la auto-nombra igual que su columna
(`id`). Cuando el origen YA tiene un índice real sobre esa columna (frecuente: es común que
una tabla con este patrón de PK tenga además un índice suelto sobre el autoincrement,
también auto-nombrado `id`), la sentencia posterior que recrea ESE índice capturado del
origen choca con `(1061, "Duplicate key name 'id'")` — el nombre ya está tomado por la KEY
de apoyo. El resultado final SÍ queda con dos índices sobre la misma columna (uno sintético,
uno real) — redundancia inofensiva, preferible a que el clon completo falle — pero deben
tener nombres DISTINTOS para coexistir.

## `%` literal en el DDL ejecutado (MySQL/MariaDB)

`MigrationRunner.execute_adhoc` (usado por el clon y por la ejecución ad-hoc de
schema-comparisons) ejecuta cada sentencia con `conn.exec_driver_sql(stmt)`, sin bind
params — deliberado para no romper `::` de PostgreSQL (`text()` lo malinterpretaría como
bind param). Pero SQLAlchemy distila un `parameters` ausente a una tupla vacía `()` (nunca
a `None`) antes de llegar al DBAPI, y **los 3 motores soportados usan un DBAPI con
paramstyle `pyformat`/`format`** (pymysql para MySQL/MariaDB, psycopg para PostgreSQL):
ambos parsean la sentencia buscando placeholders `%s`/`%(name)s` en cuanto reciben params
no-`None`, así que cualquier `%` **literal** en el DDL (una columna `GENERATED ALWAYS AS
(id % 10)`, un `CHECK`/`DEFAULT` o el cuerpo de una vista/rutina con `LIKE '%...%'` o
`DATE_FORMAT(..., '%Y-%m-%d')`) revienta al ejecutarse — pymysql con `ValueError:
unsupported format character`, psycopg con `ProgrammingError: incomplete placeholder` /
`only '%s', '%b', '%t' are allowed as placeholders`. Verificado invocando el parser real
de ambos drivers (no solo por lectura de código): un `LIKE '%bad%'` revienta igual en
PostgreSQL, algo que se asumió incorrectamente descartado en la primera versión de este
fix (el escape solo cubría MySQL/MariaDB).

Fix: `MigrationRunner._escape_percent` duplica cada `%` → `%%` **incondicionalmente**
(sin distinguir por `engine` — es seguro para los 3 motores) justo antes de pasar la
sentencia a `exec_driver_sql`. El escape es solo para la ejecución — el DDL guardado en el
preview/historial conserva el texto original sin escapar.

## FK checks durante la limpieza objeto-por-objeto (`clean_mode=objects`)

Los `DROP TABLE` de la fase de limpieza ya se ordenan en **topológico inverso** (hija antes
que padre) vía `schema_diff.py::order_diff_items`/`_table_dep_order` — la misma función que
ordena los `INSERT` de la fase de datos, reutilizada en reversa. Eso cubre el caso normal,
pero por construcción **no puede ver**:

- Una FK desde una tabla de **OTRA base de datos del mismo servidor** hacia una tabla de la
  BD que se está limpiando — el snapshot que arma el orden es de una sola BD, así que esa
  tabla externa ni siquiera es un `DiffItem` (nunca recibe su propio `DROP TABLE`). Es el
  candidato más probable ante `(1451, 'Cannot delete or update a parent row...')` en un
  `DROP TABLE` aislado (p. ej. una BD de control tipo "servers"/"databases" con otras BDs
  del mismo servidor apuntándole por FK).
- Un **ciclo de FKs** dentro de la misma BD: el fallback de `_table_dep_order` para tablas
  no resolubles topológicamente ("ciclo/dep externa") las deja al final con el mismo rango,
  desempatadas por nombre — sin garantía de que ese orden sea drop-safe.

Fix (defensa en profundidad, no reemplaza el orden topológico): `execute_adhoc` gana un
parámetro `disable_fk_checks: bool = False`. `MigrationRunner._toggle_fk_checks` desactiva
el chequeo de FKs de la sesión antes del lote y lo restaura al final (MySQL/MariaDB:
`FOREIGN_KEY_CHECKS`; PostgreSQL: `session_replication_role`) — mismo mecanismo que
`data_copy.py::_set_fk_enforcement` usa para la fase de **datos**, ahora también para la
fase de **limpieza**. `CloneController` lo activa SOLO para la sentencia de limpieza
(`CLONE_ITEM_CLEAN`); la fase de estructura (CREATE) no lo necesita — ya tiene su propio
orden padre-antes-que-hijo y FKs en fase aditiva separada. Best-effort: si el `SET` falla
(motor sin soporte, o el pseudo-root de PostgreSQL sin el permiso), se ignora — el orden
topológico sigue siendo la garantía primaria para el caso común.

**Advertencia proactiva (visibilidad, no solo mitigación)**: desactivar los FK checks
resuelve el bloqueo, pero no informa POR QUÉ hacía falta. `ServerAdapter.
external_fk_dependents(database)` consulta `information_schema.KEY_COLUMN_USAGE` a nivel
de **servidor** (no de una sola BD, vía `server_connection`) para detectar columnas de
CUALQUIER otra base de datos del servidor cuya FK referencia una tabla de `database` —
exactamente lo que el snapshot de una sola BD no puede ver. Solo MySQL/MariaDB lo
implementan (`MySQLAdapter`, heredado por `MariaDBAdapter`); PostgreSQL no soporta FKs
cross-database por arquitectura, así que hereda el default de `ServerAdapter` (`[]`).
`CloneController._external_fk_warnings` lo consulta en el preview (`POST .../preview`)
SOLO cuando el destino ya existe (`target_mode='existing'`) y el `clean_mode` implica
DROPear algo (`objects` o `drop_database`) — un destino `new` no puede tener dependents
todavía. Si encuentra columnas dependientes, las lista (hasta 5, con conteo del resto) en
`warnings` para que el operador decida si es crítico revisarlas en esas otras bases ANTES
de continuar, en vez de enterarse solo si el `DROP` llega a fallar.

## Dependencias (auto-selección inteligente)

`app/services/db_admin/clone_dependencies.py` (módulo puro):

- **Autoritativas** (fiables, se agregan al cierre): FK tabla→tabla (`ForeignKeyInfo`) y
  trigger→tabla (`TriggerInfo.table`). Seleccionar `child` arrastra `parent`.
- **Advisory** (best-effort, NO se agregan solas): escaneo por nombre de los cuerpos de
  vistas/rutinas para sugerir tablas/objetos referenciados. La UI las **resalta** ("probablemente
  también necesitás esto"); no se auto-agregan porque los cuerpos no se parsean de forma fiable
  (misma filosofía que `possible_rename_of` del diff).

## Ejecución asíncrona (jobs)

`app/services/clone_runner.py`: worker **in-process** (`ThreadPoolExecutor`, `CLONE_MAX_WORKERS`).
El estado vive en `clone_jobs` (polling cross-worker). **No es una cola durable**: si el proceso
se reinicia, los jobs `running` quedan `interrupted` (barrido en el `lifespan` de `main.py`) y se
reintentan a mano. Un guard in-process por BD destino serializa clones concurrentes al mismo
destino dentro del proceso; las fases DDL usan el advisory lock del motor
(`MigrationRunner.execute_adhoc`, con clave sintética negativa para BDs crudas).

## Seguridad

- Todo detrás de `AdminDep`. Identificadores validados+quoteados; valores de datos siempre
  parametrizados. `confirm_token` (SHA256 del plan exacto) + `confirm_target_name` +
  anti-TOCTOU (`source_fingerprint`). `record_intent` fail-closed ANTES de tocar el motor.
  Credenciales pseudo-root solo en memoria (`ServerTarget`), nunca logueadas; errores limpiados
  antes de persistir.

## Seguridad — revisión y decisiones

Revisada por `gateway-security`. Corregido (BLOQUEANTE): el advisory lock del motor ahora se
sostiene UNA vez durante TODO el pipeline (`MigrationRunner.advisory_lock` + `already_locked`),
no por sentencia — serializa cross-proceso clones al mismo destino y DROP/CREATE + datos, no
solo el DDL. Endurecido además: reclamo atómico del job (`UPDATE ... WHERE status='pending'`),
cuarentena del destino gestionado ante fallo, auditoría de resultado (`clone.execute`) además
del `record_intent`, `clean_mode`/`target_mode` en el detalle de intención, rate-limit en los
endpoints de lectura que tocan el motor, y **no se persiste el error crudo del driver en pasos
de datos** (podría filtrar valores de filas; se guarda un motivo genérico y el detalle va solo a
los logs).

## Clonar VARIAS bases en un lote

Contrato completo en [`api-reference-v19.md`](../api-reference-v19.md). Acá va lo operativo.

`POST /database-clone-batches` arma un lote: dos servidores, un perfil común y N filas
(origen → destino). Una sola confirmación lo ejecuta, y **cada fila termina siendo un `CloneJob`
real** — con su snapshot, su fingerprint, su advisory lock y su pantalla de detalle de siempre.
El lote no es un motor nuevo, es la capa de orquestación.

### Tres restricciones que no son temporales

**1. El lote no borra el destino.** `clean_mode` no existe en su contrato. Un modo destructivo
multiplicado por N y autorizado con un solo gesto es exactamente lo que tiene que seguir siendo
de a una, con el re-tipeo del nombre de esa base.

Consecuencia práctica: sobre un destino **existente** la única intención admitida es `data_only`
(con `structure_and_data` los `CREATE TABLE` chocarían contra las tablas que ya están). Y como
`data.on_existing='truncate'` no existe, **no hay "vaciar y recargar"**. Para bajar producción a
staging por completo: borrar las bases destino desde la pantalla de ciclo de vida del servidor
—que tiene su propia confirmación— y correr el lote con `target_mode='new'`.

**2. Las filas van en serie.** Una base por vez. `CLONE_BATCH_MAX_WORKERS` (default 1) controla
cuántos *lotes* corren a la vez, no cuántas filas dentro de un lote: eso no es configurable. La
serie no se eligió por rendimiento —no está medido que una copia sature el destino— sino por
control del daño: un solo destino escribiéndose a la vez, cancelación con semántica clara, y un
lote que no puede monopolizar el pool de `CLONE_MAX_WORKERS` y dejar sin turno a los clones
sueltos (el lote tiene executor propio).

**3. El plan de cada base se arma cuando le toca el turno.** Al planear el lote solo se valida
lo barato (identificadores, alcance, existencia del destino, coherencia del modo), sin
fotografiar ninguna base. Así se evitan N snapshots antes de confirmar, se evita que las últimas
filas venzan por `CLONE_TTL_HOURS` en un lote de horas, y se evita ejecutar un DDL calculado seis
horas antes. Lo que el operador confirma es la **intención**, no el DDL.

### Confirmación: una sola, sobre el servidor

Se re-tipea el nombre del **servidor destino**, una vez. Con doce bases, doce re-tipeos se
vuelven copiar y pegar sin leer, y además protegen el eje equivocado: en un lote el error
catastrófico no es escribir mal un nombre, es que la lista entera apunte al servidor que no era.
El otro eje lo cierra `confirm_token` (sha256 del conjunto ORDENADO de pares origen→destino):
agregar, quitar o editar una fila lo invalida.

### Filas bloqueadas vs. lote rechazado

Lo que el operador corrige en el formulario da **422 del lote entero** (vacío, tope excedido,
nombres destino repetidos, `clean_mode` destructivo). Lo que depende del estado del servidor y
varía por base marca **esa fila** como `blocked` con su código, y el lote se crea igual: rebotar
la petición por el primer problema obliga a corregir un lote de 12 bases de a una.

### Reintento: dos grupos, y por qué

Al terminar, `GET .../retry-candidates` parte las filas no exitosas según si el destino quedó
**intacto** — que es lo único que el lote sabe manejar, porque no puede limpiar:

- **Reintentables**: las que nunca llegaron a tener job (bloqueadas, salteadas, canceladas antes
  de arrancar). Es el caso que importa después de un reinicio. `POST .../retry-failed` arma un
  lote **nuevo** con ellas, que hay que volver a confirmar.
- **Requieren intervención manual**, por dos motivos distintos: (a) la fila alcanzó a copiar
  filas, así que el destino tiene **datos parciales** commiteados y reintentar duplicaría; (b) la
  fila creaba el destino y el intento anterior alcanzó a crearlo antes de fallar. Las dos se
  resuelven con el asistente de a una, que sí ofrece `drop_database` con su re-tipeo.

### Durabilidad, y un requisito operativo

El worker es in-process, como el resto de la familia: **un reinicio deja el lote `interrupted`**
y no se reanuda solo (el fingerprint del origen pudo cambiar y el token autorizaba otro
conjunto). El barrido de arranque lo cierra y marca `skipped` lo que no llegó a correr.

**Mientras exista el lote, `WORKERS=1` deja de ser un default cómodo y pasa a ser un
requisito.** `sweep_interrupted` marca `interrupted` todo lo que esté `running` sin distinguir de
qué proceso es, y un lote de horas agranda muchísimo la ventana en que dos procesos se pisan.

---

## Limitaciones conocidas (v1)

- **Durabilidad**: los jobs no sobreviven un reinicio del proceso (quedan `interrupted`). En
  despliegue **multi-worker HA**, el barrido de arranque de un worker puede marcar `interrupted`
  el job vivo de otro (falso positivo, mayormente benigno): el modelo asume ejecución
  efectivamente single-process. Una cola durable + heartbeat por job es endurecimiento futuro.
- **Anti-TOCTOU del destino**: se re-verifica el fingerprint del **origen** antes de ejecutar;
  el **destino** no fija fingerprint. Con el lock del pipeline sostenido, ninguna operación del
  gateway puede alterar el destino a mitad; queda una ventana pequeña para cambios EXTERNOS al
  gateway sobre el destino entre el `execute` y la toma del lock por el worker (para
  `clean_mode=objects`). Persistir un `target_fingerprint` y re-chequearlo es mejora pendiente.
- **Integridad referencial**: con FK-checks apagados durante la fase de datos, una tabla que
  falle a mitad (best-effort) puede dejar filas huérfanas; el destino gestionado pasa a
  cuarentena para forzar revisión.
- **Atribución del auto-adopt**: el worker corre fuera del ciclo de request, así que la adopción
  y la auditoría de resultado no llevan identidad de admin (la intención SÍ, vía `record_intent`
  al encolar). Propagar el admin creador del job es mejora pendiente.
- **Fidelidad de tipos cross-engine**: la estructura cross-family es best-effort (los tipos del
  origen se renderizan tal cual; sin mapeo de tipos exhaustivo). Revisar el preview.
- **Charset/collation del destino nuevo**: si el operador no elige nada, la BD hereda el
  charset/collation **del origen** cuando es el mismo motor (para que el default no derive y el
  diff no reporte falsos positivos), y cae al default del motor destino cuando es cross-engine.
  Con `target_charset.mode='override'` se elige explícitamente. *(Esta línea afirmaba lo
  contrario del código hasta 2026-08-22.)*
- **`data.on_existing='truncate'` no existe**: en `data_only` se puede cargar y actualizar
  filas, no *reemplazar* el contenido. Para reemplazar, `clean_mode='objects'`.
- **El owner de PostgreSQL es solo de la base**: los objetos quedan con la propiedad de la
  credencial administrativa del gateway (ver `target_owner_user_id`).
- **Solo el schema `public` en PostgreSQL**: un destino con las tablas en otro schema produce un
  "la tabla no existe en el destino" que es correcto pero engañoso.
- **Las estimaciones de filas son del catálogo**, no un `COUNT`: pueden estar atrasadas
  (`information_schema_stats_expiry` en MySQL 8) y `row_estimate_known: false` significa que el
  catálogo no las sabe (PostgreSQL sin `ANALYZE`).
- Verificación e2e contra motores reales: `scripts/verify_clone_e2e.py` (requiere Docker).
