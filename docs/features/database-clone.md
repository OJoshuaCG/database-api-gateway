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
4. **`POST /database-clones/{id}/preview`** — resuelve el plan final SIN ejecutar: sentencias
   de limpieza + estructura (DDL exacto en el dialecto destino), tablas de datos, objetos
   `skipped` (no portables) y el `confirm_token`.
5. **`POST /database-clones/{id}/execute`** — valida `confirm_target_name` + `confirm_token` +
   re-chequea el fingerprint del origen (anti-TOCTOU) + cuarentena, registra la intención
   (auditoría fail-closed) y **encola** el job asíncrono. Rate limit 3/min.
6. **`GET /database-clones/{id}`** — resumen + estado (`pending`→`running`→`succeeded`/`failed`/
   `interrupted`/`canceled`) + `phase` + `progress` (para polling).
7. **`GET /database-clones/{id}/items`** — pasos ejecutados (limpieza/estructura/datos/adopt)
   con su resultado por ítem. **`POST /database-clones/{id}/cancel`** — cancelación cooperativa.

## Opciones del plan

- **`include_data`**: `false` = solo estructura; `true` = estructura + **todos** los datos.
- **`target_mode`**: `new` (crea la BD; 422 si ya existe) | `existing` (404 si no existe).
- **`clean_mode`** (solo destino existente): `none` (preservar y hacer *upsert* de datos) |
  `objects` (borrar objeto por objeto en orden topológico inverso, **preservando la BD y su
  configuración** — charset/collation/grants) | `drop_database` (**reset total**: DROP + CREATE).
- **`selection`**: lista de objetos a clonar; `null` = clon **completo**. La selección se
  expande por el cierre de dependencias.
- **`adopt_target`** + **`adopt_owner_id`**: solo en clon **completo** desde un origen
  gestionado **con blueprint**: al terminar, adopta el destino (`origin='adopted'`) y le
  **stampa** el `model_id` + `model_version` del origen (sin re-ejecutar DDL). `adopt_owner_id`
  debe ser un `ServerUser` del servidor **destino**.

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
- **Charset/collation del destino nuevo**: se crea con el default del motor (no se copia el del
  origen todavía).
- Verificación e2e contra motores reales: `scripts/verify_clone_e2e.py` (requiere Docker).
