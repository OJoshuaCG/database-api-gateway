# Exportación de bases de datos (estructura y/o datos, multiformato)

Exporta el contenido de una base de datos de cualquier servidor dado de alta —**estructura**,
**datos** o ambos— a un artefacto descargable, en modo **estrictamente de solo lectura** sobre
el origen. Es el equivalente al "Export database as SQL" de un cliente de escritorio, pero
hecho por un gateway que conecta con credencial pseudo-root y que tiene que dejar rastro de lo
que sale.

Cualquier sentencia destructiva (`DROP DATABASE`, `DROP TABLE`) existe **únicamente como texto
dentro del artefacto**: el módulo nunca ejecuta DDL contra el origen. La única escritura que
hace es en su propio disco de spool.

Diseño completo y decisiones: [`docs/plans/10-exportacion-de-bases-de-datos.md`](../plans/10-exportacion-de-bases-de-datos.md).
Contrato para el equipo de frontend: [`docs/api-reference-v10.md`](../api-reference-v10.md).

> ⚠️ **Riesgo aceptado, declarado arriba de todo y no escondido en un apéndice**: este módulo
> **no tiene enmascarado de datos** ([§sin enmascarado](#riesgo-aceptado-no-hay-enmascarado)).
> Permite extraer datos personales o regulados **en claro**, y no ofrece ningún control
> técnico para evitarlo. El único control compensatorio en pie es la auditoría, que por eso no
> es negociable. Si el sistema pasa a tratar datos regulados, esto es un bloqueante de
> cumplimiento: apagá el módulo con `EXPORT_ENABLED=False` hasta implementar el enmascarado.

---

## Flujo (plan → objetos → resolver → preview → ejecutar → polling → descargar)

Mismo patrón seguro que [clon](database-clone.md) y [schema-comparisons](schema-comparison.md):
el servidor es la única fuente de verdad y el cliente confirma con `confirm_token` +
`confirm_target_name`. Es el **cuarto** módulo de esta familia y el flujo se copia tal cual.

1. **`GET /servers/{sid}/databases/{db}/export-capabilities`** — qué admite **este** motor:
   tipos de objeto, formatos, valores válidos de cada opción, la **matriz de compatibilidad**
   y los límites. Es lo que permite armar el formulario sin hardcodear ni una regla; ver
   [más abajo](#capabilities-la-matriz-se-publica-y-además-se-hace-cumplir).
2. **`POST /servers/{sid}/databases/{db}/database-exports`** — crea un **PLAN**. El cuerpo *es*
   el `ExportSpec` completo y se persiste íntegro en `export_jobs.spec`. Snapshotea el catálogo
   del origen (solo lectura), guarda un `source_fingerprint` (anti-TOCTOU) y un TTL
   (`EXPORT_TTL_HOURS`, 24 h). Estado inicial `pending`. **201**.
3. **`GET /database-exports/{id}/objects`** — catálogo en vivo, paginado y filtrable por tipo y
   por subcadena del nombre, con los metadatos que informan la selección (filas estimadas,
   charset, si tiene PK, si tiene triggers). Informa además qué tablas internas del gateway se
   descartaron (`excluded_internal`), para que nadie las busque en el artefacto y crea que se
   perdieron.
4. **`POST /database-exports/{id}/resolve-selection`** — resuelve las **dos** selecciones y su
   **cierre de dependencias**, sin congelar nada. Es el "seleccioná uno y traé lo necesario" de
   la interfaz.
5. **`POST /database-exports/{id}/preview`** — valida el spec entero, **CONGELA** la selección
   resuelta y emite el `confirm_token`. Devuelve los objetos **en el orden exacto en que van a
   salir**, las estimaciones, los avisos y si la entrega en línea es viable. Con
   `dry_run_only: true` valida y reporta **sin** congelar ni emitir token: es el modo "solo
   advertencias" para mostrar consecuencias mientras el usuario todavía elige opciones.
6. **`POST /database-exports/{id}/execute`** — doble factor (`confirm_target_name` +
   `confirm_token`), re-lee el catálogo y compara el fingerprint, audita la intención
   *fail-closed* y **encola** el job. Devuelve de inmediato. Rate limit **3/min**.
7. **`GET /database-exports/{id}`** — polling del estado
   (`pending`→`running`→`succeeded`/`failed`/`interrupted`/`canceled`) + `phase` + `progress`.
   **Sin rate limit a propósito**: es la ruta que el frontend consulta cada pocos segundos y
   limitarla rompería el caso de uso para el que existe.
8. **`GET /database-exports/{id}/items`** — reporte de incidencias por objeto: qué se exportó,
   qué se omitió y **por qué**.
9. **`POST /database-exports/{id}/cancel`** — cancelación cooperativa. Sin rate limit: detener
   una exportación que está degradando el origen no puede quedar bloqueado por una cuota.
10. **`GET /database-exports/{id}/manifest`** — inventario verificable: checksum, tamaño,
    objetos con filas y bytes, `complete`, `structure_drift_detected`. Permite auditar qué
    salió **sin abrir el archivo** — mirar el contenido para saber qué se llevó sería una
    segunda divulgación.
11. **`GET /database-exports/{id}/download`** — el artefacto. **No** usa `ApiResponse`.
12. **`GET /database-exports/{id}/content`** — entrega en línea, `text/plain` sin envolver.

La creación cuelga de `/servers/...` porque la base se identifica por **identidad física**
(`server_id` + nombre), funcione o no adoptada en el inventario — mismo patrón que
collation-conversion y que las referencias crudas de schema-comparisons. Una vez que el job
existe, el resto cuelga de él.

---

## Estructura y datos: **dos conjuntos**, no un booleano por tabla

Es la decisión que más modela el resto del módulo. Se eligen **dos selecciones separadas**
(`selection` para estructura, `data` para datos) con una **restricción de subconjunto que
verifica el servidor**, en vez de una sola lista con una casilla "incluir datos" por fila.

- La restricción: **`data ⊆ selection`**. Pedir la estructura de 12 tablas y los datos de una
  13ª que no está en el artefacto produce `INSERT`s sin la tabla que los recibe — un script que
  falla garantizado. `resolve-selection` y `preview` responden **422
  `export.data_without_structure`** nombrando las tablas huérfanas.
- Cada selección acepta `mode` (`all` | `include` | `all_except`; `data` acepta además `none`),
  `names`, `include_patterns` y `exclude_patterns`. Los tres modos existen porque el flujo real
  es "marco todo y quito tres", y expresarlo como `include` con 200 nombres es peor en todo
  sentido.
- Los patrones son **glob** (`fnmatch`) evaluados **contra los nombres que devolvió el catálogo
  del motor**. Nunca llegan a una consulta: son filtrado en memoria sobre cadenas que el propio
  motor produjo. Se usa `fnmatchcase` y no `fnmatch` a propósito — este último aplica
  `os.path.normcase` y en Windows pasaría todo a minúsculas, con lo que el **mismo spec
  resolvería distinto según el sistema operativo del gateway** y el determinismo se caería.
- Orden de aplicación: `catálogo(tipos) − tablas internas del gateway` → `mode` →
  `include_patterns` → `exclude_patterns`. **La exclusión gana.**
- Los datos solo salen de **tablas**: `data.selection` fuerza `types=("table",)`. No hay forma
  de pedir "los datos de una vista".

### La excepción decidida: exportación "solo datos"

Cuando `structure.scope_ddl == "NONE"` **y** `structure.entity_ddl == "NONE"`, la restricción de
subconjunto **no aplica**. Motivos, los dos legítimos:

- recargar una tabla que **ya existe** en el destino es un caso de uso frecuente, y el artefacto
  resultante tiene sentido: son `INSERT`s sin DDL;
- es la **única** forma en que `csv`/`json`/`ndjson` pueden existir — en esos formatos la
  estructura no es ejecutable, así que la matriz les prohíbe cualquier valor de `structure.*`
  distinto de `NONE`.

Lo que se rechaza no es "datos sin estructura", es la **mezcla incoherente**.

### Cierre de dependencias

Reutiliza el resolvedor de `clone_dependencies` (una vista que lee de otra vista, un trigger sin
su tabla, una FK a una tabla excluida). La política es la misma que en el resto del proyecto y
depende de **quién eligió**:

- **selección explícita** (`mode: "include"`) → **422 `export.missing_dependencies`** con
  `missing_dependencies` + `suggested_names`. **No se recorta en silencio**: quien nombró los
  objetos uno por uno tiene que enterarse de que su lista no es ejecutable. Con
  `auto_resolve_dependencies: true` se agregan y lo agregado viaja en `added`;
- **selección automática** (`all`, `all_except`, patrones) → **poda transitiva** +
  `excluded_by_dependency`. Un `CREATE INDEX` sobre una tabla que un patrón dejó fuera es un
  script roto, y el usuario no eligió esa tabla: no tiene sentido bloquearlo.

---

## `scope_ddl` / `entity_ddl`: un enumerado de cuatro valores, **no dos booleanos**

```
NONE | CREATE | DROP_CREATE | CREATE_IF_NOT_EXISTS
```

Con dos banderas (`drop` + `create`) el estado **"eliminar sin crear" es representable**, y hay
que parchearlo con validación dispersa que tarde o temprano se cuela por la API en algún camino
que nadie revalidó. Con el enumerado sencillamente **no existe**: la regla "si se pide
eliminación, la creación se incluye siempre" es **el tipo**, no un `if`.

Los cuatro valores están y **`DROP_CREATE` y `CREATE_IF_NOT_EXISTS` no son redundantes ni
opuestos por accidente**: son las dos idempotencias que la gente quiere y son **incompatibles
entre sí**. La primera dice *"quiero que quede exactamente esto, destruyendo lo que haya"*; la
segunda, *"quiero que exista, sin tocar lo que ya está"*. Ofrecer solo una obliga a editar el
script a mano, que es justo lo que el módulo debería evitar.

`drop_if_exists` es **ortogonal**: aplica a la sentencia de eliminación de `DROP_CREATE`, porque
un script que aborta al intentar eliminar algo inexistente no sirve para nada en la práctica.

**Confirmación**: `scope_ddl: "DROP_CREATE"` exige `structure.confirm_scope_drop` **igual al
nombre real de la base** (422 si no). El artefacto va a contener un `DROP DATABASE`; obligar a
re-teclear el nombre obliga a identificar **cuál** base, no solo a apretar "sí". Es el mismo
patrón que el borrado de bases y el clon.

**Huecos por motor**, publicados y exigidos por la matriz:
- `scope_ddl: "CREATE_IF_NOT_EXISTS"` es **422 en PostgreSQL**: `CREATE DATABASE IF NOT EXISTS`
  no existe en ninguna versión.
- `entity_ddl: "CREATE_IF_NOT_EXISTS"` es **best-effort por tipo**. Donde el motor no lo
  expresa se emite el `CREATE` normal **y se avisa**. Fail-closed a propósito: el `IF NOT
  EXISTS` de rutinas y triggers depende de la **versión del motor destino** (MySQL 8.0.29+), que
  el gateway no conoce — el artefacto ni siquiera tiene por qué ejecutarse contra el servidor de
  origen.

---

## Consistencia de punto único en el tiempo, **y su asimetría por motor**

El job abre **una sola conexión dedicada** (`export_session`), con una transacción de lectura,
antes del primer objeto y hasta después del último. Ni una conexión por tabla ni un pool (los
engines remotos ya usan `NullPool`).

| Motor | Cómo se abre | Cubre estructura | Cubre datos |
|---|---|---|---|
| PostgreSQL | `execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)` + un `SELECT 1` que fuerza a tomar el snapshot **ya** | **Sí** | Sí |
| MySQL / MariaDB | `SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ` → `SET SESSION TRANSACTION READ ONLY` → `START TRANSACTION WITH CONSISTENT SNAPSHOT` | **No** | Sí (solo InnoDB) |

Dos detalles de implementación que parecen menores y no lo son:

- **PostgreSQL no usa un `BEGIN` crudo.** psycopg abre la transacción por su cuenta al primer
  `execute`, así que un `BEGIN` nuestro llegaría **segundo**, el servidor lo ignoraría con un
  aviso y la sesión quedaría en `READ COMMITTED` — sin snapshot y **sin que nada fallara**. El
  modo silencioso de perder la garantía entera.
- **MySQL/MariaDB se hace en tres pasos** y no con la lista de características separada por
  coma (`START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`). La garantía es idéntica —un
  `START TRANSACTION` sin modo explícito hereda el de la sesión— pero el modo de acceso **por
  sesión** está soportado desde MySQL 5.6 / MariaDB 10.0, mientras que la forma con coma no lo
  está de manera uniforme. La conexión es dedicada al job y con `NullPool`, así que dejarla en
  `READ ONLY` no contamina a nadie.

### El límite irreducible en MySQL/MariaDB — se reporta, no se tapa

**En MySQL/MariaDB, "punto único en el tiempo" para la ESTRUCTURA es técnicamente imposible.**
El snapshot consistente de InnoDB es MVCC **de filas**: el diccionario de datos y
`information_schema` **no participan**. Un `ALTER TABLE` concurrente se ve de inmediato. La
única forma de congelar también el catálogo es `FLUSH TABLES WITH READ LOCK`, que exige
privilegio `RELOAD` y **bloquea las escrituras del servidor entero** — inaceptable en un gateway
que administra bases de terceros. Es exactamente la misma limitación que tiene
`mysqldump --single-transaction`, documentada por el propio MySQL.

Qué se hace en consecuencia, en vez de fingir la garantía:

- PostgreSQL: garantía completa, sin asteriscos (`consistent_structure = true`).
- MySQL/MariaDB: garantía completa **para datos**; para estructura, un **aviso en el preview**
  y un `warning` visible.
- El `source_fingerprint` se **re-verifica al terminar** el job, todavía dentro de la sesión: si
  el catálogo cambió durante la corrida, el job queda con `structure_drift_detected: true`. No
  invalida el artefacto —los datos siguen siendo consistentes— pero el operador se entera.
  En PostgreSQL, por construcción, no puede dispararse.

### Los costos que hay que asumir y monitorear

Una transacción larga retiene versiones viejas: en PostgreSQL **bloquea el `VACUUM`** y hace
crecer las tablas; en la familia MySQL **infla el historial de undo**. En un origen con
escritura intensa, una exportación de horas **degrada el origen**. Por eso:

- `EXPORT_MAX_DURATION_SECONDS` (4 h) con **aborto duro** y cierre garantizado de la transacción
  en un `finally`. Una transacción huérfana contra la base de un tercero es un incidente de
  producción, no una fuga de recursos menor. El plazo es **cooperativo**: se comprueba entre
  trozos, no interrumpe una sentencia en curso.
- `EXPORT_STATEMENT_TIMEOUT_MS` se pasa **explícito** en vez de heredar el par
  interactivo/bulk: el interactivo (15 s) cancelaría el `SELECT` de una tabla grande a mitad, y
  el bulk (1 h) es un techo pensado para la copia de datos del clon, no para una lectura que
  además sostiene un snapshot.
- **`idle_in_transaction_session_timeout` se desacopla** (`EXPORT_IDLE_TRANSACTION_TIMEOUT_MS`,
  5 min). `remote_engine` lo ata al `statement_timeout` en los `connect_args` del engine, así
  que sin esto un export estancado sostendría el snapshot de PG durante media hora. Se desacopla
  con un `SET` a nivel de **sesión** sobre la conexión propia —que gana sobre el `-c` de la
  URL— y **no** cambiando los `connect_args`: eso habría metido otro eje en la clave del cache
  de engines y habría afectado a los otros cinco consumidores de `remote_engine` para resolver
  el problema de uno solo. Es best-effort: si el motor rechaza el `SET`, se anota en
  `progress.degradations` y se sigue.
- Es un argumento fuerte a favor de exportar **contra una réplica de solo lectura** cuando
  exista. Configurable, fuera del alcance de v1.

### Por qué el export **NO toma el advisory lock** (divergencia deliberada)

Clon, schema-comparisons y collation-conversion toman el advisory lock del motor sobre una
conexión dedicada, en un espacio de claves compartido, durante todo el pipeline. **La
exportación no lo toma, y es una decisión, no un olvido:**

- la exportación es de **solo lectura**: no hay nada que serializar por corrección;
- sostener el lock exclusivo **durante horas** bloquearía clones, conversiones y migraciones
  sobre esa base sin ninguna ganancia;
- la consistencia de este módulo la da el **snapshot MVCC**, que es un mecanismo distinto de la
  exclusión mutua — y que el lock además no podría reemplazar, porque el hueco de estructura en
  MySQL es del **diccionario de datos**, no de concurrencia con el gateway.

Lo que sí se toma es el guard **in-process** (`export_runner.database_guard`, misma forma que
`clone_runner.target_guard`), que acota exportaciones simultáneas de la misma base dentro de
este proceso: barato, sin efecto cross-proceso y sin bloquear a nadie más.

> Si alguien "arregla" esto agregando el advisory lock, va a introducir un bloqueo de horas
> sobre la base de un tercero. Está anotado en el docstring de `export_runner.py`.

### Inyección de conexión en `ServerAdapter` (el cambio caro del módulo)

Para que la estructura y los datos salgan del **mismo instante**, el catálogo tiene que leerse
**dentro** de la transacción del job. Hasta F3, `structural_snapshot`, `dump_structure`,
`list_tables` y `list_table_stats` abrían su propia conexión en `AUTOCOMMIT`. Ahora aceptan un
`conn: Connection | None = None` keyword-only, resuelto por `base_adapter._conn_ctx`:

- con `conn` dado: **no se cierra la conexión ni se toca su nivel de aislamiento** — hacerlo
  revertiría la transacción `REPEATABLE READ` que `export_session` acaba de abrir;
- con `conn=None`: exactamente el comportamiento histórico.

Es **aditivo y compatible**, y los cinco features que lo consumen llaman posicionalmente con un
solo argumento y siguen intactos. Pero toca código compartido y crítico, y esa es la parte cara
de este módulo. Es la única forma honesta de cumplir el requisito.

---

## Comentarios de script y comentarios de objeto: **dos opciones separadas**

- `sanitize.script_comments` — el **encabezado** del volcado y los separadores del script.
  Apagarlo es lo que hace el artefacto comparable byte a byte (saca la fecha) y lo que permite
  versionarlo en un repositorio.
- `sanitize.object_comments` — los `COMMENT` del **esquema** (inline en MySQL, `COMMENT ON …`
  aparte en PostgreSQL).

Son opciones distintas porque **perder las segundas es una pérdida de información real**: el
comentario de una columna es parte de la definición, no ruido del generador. Colapsarlas en un
solo interruptor "sin comentarios" habría hecho que quien quiere un diff limpio se lleve por
delante la documentación del esquema.

## `DEFINER`: tres modos + `auto`, y por qué el default es `auto`

`sanitize.definer` acepta `keep` | `omit` | `replace` (+ `definer_value`) y **`auto`**, que es
el default.

**La implicación de seguridad**: en MySQL/MariaDB el `DEFINER` de una vista, rutina, trigger o
evento determina **con qué privilegios corre** ese objeto. Un volcado que conserva
`` DEFINER=`root`@`localhost` `` y se restaura en otro servidor o lo ejecuta otro operador
falla (si esa cuenta no existe) o —peor— **crea objetos que corren como root** en el destino.
Por eso el default efectivo en la familia MySQL es **`omit`**: el objeto queda con el definer de
quien ejecuta el script, que es el principio de menor sorpresa.

En **PostgreSQL el `DEFINER` no es el mismo concepto**: la propiedad del objeto
(`ALTER … OWNER TO`) y `SECURITY DEFINER` son mecanismos distintos. La opción se declara **no
aplicable** en capabilities (`applicable: false`) y la matriz **prohíbe `omit`/`replace` con
422**; `keep` es un no-op.

De ahí `auto`: resuelve a `omit` en la familia MySQL y a `keep` en PostgreSQL. Sin él, la
llamada canónica (`{}`, todo por defecto) daba 422 contra PostgreSQL — un default que rompe un
motor no es un default.

> Nota honesta: en el camino actual, **`keep` no puede conservar nada**. Los cuerpos del
> `SchemaSnapshot` ya vienen sin `DEFINER` (`_strip_definer_clause` corre al capturarlos), así
> que `keep` es hoy indistinguible de `omit`. Se implementa igual para el día que exista una
> fuente que sí lo traiga.

---

## Orden de emisión y determinismo

### Orden

```
 1  preámbulo de sesión            (charset, FK/UNIQUE checks off, sql_mode, zona horaria)
 2  DROP/CREATE del contenedor + fijar contexto (USE db / SET search_path)
 3  tipos definidos por el usuario, extensiones, secuencias
 4  tablas (estructura)            [sin índices ni FKs si constraints_placement=deferred]
 5  datos
 6  índices, UNIQUE, CHECK y FKs   (si deferred)
 7  rutinas                        ← ANTES que las vistas
 8  vistas
 9  vistas materializadas
10  triggers
11  eventos
12  ajuste de contadores y secuencias
13  epílogo: RESTAURA todo lo que tocó el preámbulo
```

**Las rutinas van antes que las vistas**, y no al revés: en PostgreSQL una vista que llama a una
función se **valida al crearse**, así que la función tiene que existir antes. Ese orden se
corrigió una vez en el repositorio por un fallo real y vive en
`schema_diff._BODY_TYPE_ORDER` / `snapshot_layout._CLASS_ORDER`; el export **reusa esa única
fuente** en vez de mantener una lista paralela, que es exactamente cómo se reintroduce el bug.

El orden fino lo calcula `export_controller._order_for_emission` con el `_STEP` de
`schema_diff` y un pase **topológico** (tablas por FK, cuerpos por las referencias de su
cuerpo). El **writer no reordena nada**: emite en el orden que el preview congeló y que el
`confirm_token` hasheó.

**El epílogo es obligatorio cuando hay preámbulo.** Un script que deja la sesión con
`FOREIGN_KEY_CHECKS=0` es un fallo grave, así que el preámbulo guarda los valores previos en
variables de usuario (`@_gw_fk_checks`, …) y el epílogo los **restaura** en vez de fijar un
valor supuesto. En PostgreSQL el epílogo usa `RESET`, por lo mismo.

> **En PostgreSQL NO se emite `SET session_replication_role='replica'`**, que sería la única
> forma de suspender FKs y triggers allá: **exige superusuario**, y emitirlo haría abortar el
> script (con `ON_ERROR_STOP`) para cualquier operador normal. No hace falta, porque el default
> `constraints_placement='deferred'` ya emite índices y FKs después de los datos.

### Determinismo (§8.3): requisito, no adorno

Dos exportaciones del mismo esquema sin cambios producen el **mismo artefacto byte a byte**
—salvo la fecha del encabezado, que se suprime con `script_comments: false`—. Es lo que habilita
versionar el esquema en un repositorio, **diffear dos volcados** y hacer pruebas de regresión.

- Objetos: el orden congelado por el preview.
- Dentro de cada objeto: columnas en orden de catálogo; índices, constraints y FKs por nombre.
- Filas: `ORDER BY` de la clave primaria.
- **Tablas sin PK**: se ordena por la **tupla completa de columnas no generadas**, si *todas*
  son ordenables. Los tipos sin orden útil o sin operador de orden
  (`blob`, `text`, `json`, `xml`, `bytea`, `tsvector`, y en PostgreSQL además `point`,
  `polygon`, `box`, `line`, …) hacen que se emita **sin orden garantizado**: el objeto se marca
  `deterministic: false`, se avisa en el preview y queda en el manifiesto. Fail-closed: ante la
  duda, `false`. **Fingir determinismo ahí sería mentir.**
- Los metadatos volátiles (fecha, id de job) viven en el **manifiesto**, no en el script, para
  que el artefacto sea comparable sin recortarle el encabezado. Las entradas del `zip` llevan
  fecha fija (`1980-01-01`) por el mismo motivo.

---

## Formatos, empaquetado y entrega

### Formatos

| Formato | Estructura | Datos | Notas |
|---|---|---|---|
| `sql` | sí | sí | El único ejecutable. |
| `csv` | no | sí | **Siempre un archivo por tabla** → el artefacto es un `zip`. |
| `json` | `manifest_only` | sí | Un array por tabla; con `output.schema_manifest` agrega un documento **descriptivo** del esquema que se declara `executable: false`. |
| `ndjson` | `manifest_only` | sí | Un registro por línea. |

`csv`/`json`/`ndjson` **prohíben** todo `structure.*` distinto de `NONE`, `data.insert_variant`,
el preámbulo de sesión y el `transaction_wrap`: en esos formatos la estructura no es ejecutable
y emitir sentencias sería inventarse un contrato. `csv` prohíbe además
`output.organization=single` y la entrega en línea.

**Dialecto csv** (`csv.*`): `delimiter`, `quote_char`, `escape_char` (`null` = duplicar la
comilla, RFC 4180), `line_terminator`, `header`, `bom` y `null_representation`. Este último es lo
que hace **distinguibles NULL y cadena vacía**: el nulo sale **sin comillas** y la cadena vacía
**siempre cuoteada** (`""`), igual que `COPY … WITH CSV` de PostgreSQL. Los caracteres se validan
en el módulo puro (uno solo, distintos entre sí, sin saltos de línea) y no solo en el borde HTTP,
para que el rechazo salga con el código estable y la opción culpable en vez de como un error de
validación de Pydantic que el cliente tiene que interpretar aparte.

### Empaquetado

- `output.organization`: `single` | `per_object` (un archivo por objeto — la que permite
  versionar el esquema).
- `output.split_max_bytes`: fragmenta en `{base}.part{NN}{ext}`.
- **Multiarchivo ⇒ zip, siempre.** `per_object` o `split_max_bytes` elevan la compresión a `zip`
  aunque se haya pedido `none`: un multiarchivo no se puede entregar suelto por una descarga.
  Eso **no** es un 422 (es una resolución, y se avisa en el preview). En cambio `gzip` +
  multiarchivo **sí** es 422, porque ahí el usuario tiene una alternativa real.
- El contenedor lleva un índice `000-INDICE.txt` y, si el artefacto quedó incompleto, un
  `000-EXPORTACION-INCOMPLETA.txt`.
- `EXPORT_MAX_PARTS` (500) topea las entradas: el tope por tamaño no alcanza, porque con un
  `split_max_bytes` de 1 KB un artefacto perfectamente legal genera decenas de miles de entradas
  —lento de escribir, inmanejable de descomprimir y con el directorio central creciendo en
  memoria—.

### Entrega: archivo vs. en línea

**`file`** (`GET .../download`). Se usa `FileResponse` y no `StreamingResponse` porque la versión
de Starlette del repositorio (0.50) ya implementa **`Range`** ahí —descarga reanudable,
`Accept-Ranges`, `416`, `If-Range`— y reimplementarlo a mano sería peor. Entrega por trozos desde
disco (nunca en memoria), con `Content-Disposition`, `Content-Length` y `ETag` = el sha256 del
artefacto.

**`inline`** (`GET .../content`). El artefacto como `text/plain` **sin envolver en
`ApiResponse`**: el cliente lo copia al portapapeles tal cual. Solo para `organization=single`,
sin compresión y sin split (la matriz lo exige).

- Tope `EXPORT_INLINE_MAX_BYTES` (1 MB), **publicado en capabilities**.
- Al excederlo: **409 `export.inline_too_large`**, **accionable** — trae `byte_size` real y
  `inline_max_bytes`. **Nunca se trunca en silencio**: un script cortado que alguien pega y
  ejecuta es peor que un fallo.
- El `preview` ya devuelve `inline_delivery_viable` + `estimated_bytes`, así que el cliente sabe
  **antes** de lanzar el job si el modo es viable.

### El artefacto no se conserva

"El artefacto no se conserva" y "siempre asíncrona" se contradicen si no se decide: un job
termina en un momento distinto de aquel en que el cliente recoge el resultado, así que el
artefacto **existe en algún lado durante ese intervalo**. Se eligió **almacenamiento efímero**, y
se descartó la transmisión directa en flujo porque es **incompatible con progreso, cancelación
limpia y reintento** —los tres exigidos— y obligaría a sostener una conexión HTTP larga además
de la transacción contra el origen.

Reglas, para que "no se conserva" no degenere en un temporal olvidado en disco:

- **TTL** `EXPORT_ARTIFACT_TTL_MINUTES` (30), contado desde que el job termina.
- **Descarga de un solo uso** (`EXPORT_SINGLE_USE_DOWNLOAD=True`): al completar la entrega el
  archivo se borra y el artefacto pasa a `consumed` (**410 `export.artifact_consumed`** en el
  siguiente intento). Una descarga **genuinamente parcial** **no** lo consume: borrarlo ahí
  rompería justamente la reanudación que el `Range` habilita.

  Lo que decide es si el rango **cubre el archivo entero**, no la mera presencia de la cabecera:
  con esa lectura ingenua un `Range: bytes=0-` bajaba el artefacto completo y lo dejaba
  disponible, o sea que el "un solo uso" se anulaba con una cabecera. Cuenta como completa la
  petición sin `Range`, la que trae un `If-Range` que no valida (ahí el servidor ignora el rango
  y manda todo el cuerpo) y `bytes=0-` / `bytes=0-<size-1>` / `bytes=-<n≥size>`. Cualquier forma
  que no se entienda (multi-rango, otra unidad, sintaxis rara) se trata como **parcial**:
  equivocarse hacia "no borrar" deja un artefacto vivo hasta su TTL, equivocarse hacia "borrar"
  obliga a rehacer la exportación entera. **El contador de descargas se incrementa siempre**, en
  los dos casos: no contabilizar las parciales convertía el registro en una subestimación
  silenciosa, justo con el método que además evitaba el borrado.
- **Purga periódica** en el `lifespan` (`EXPORT_PURGE_INTERVAL_MINUTES`, 10), con
  `asyncio.to_thread` porque el borrado es I/O síncrono y en el event loop bloquearía todas las
  requests. Hacerlo **solo en el arranque** volvería el TTL una promesa falsa en un proceso que
  corre semanas — es el mismo error que ya se corrigió en la purga de capturas de `SELECT`.
- **Barrido de huérfanos al arrancar**: archivos en el directorio sin fila viva se borran. Sin
  esto, un `kill -9` deja artefactos sensibles en disco para siempre. Y `sweep_interrupted()`
  marca `running → interrupted` los jobs que un reinicio dejó colgados.
- El directorio (`EXPORT_ARTIFACT_DIR`) se crea con modo **0700** y debe ser un **volumen
  propio** (`exports_data:/app/exports`), **no** el de uploads: aquel es un buzón de entrada sin
  TTL y mezclar ambos ciclos de vida es cómo un artefacto sobrevive a su purga.

---

## Capabilities: la matriz **se publica y además se hace cumplir**

`GET /servers/{sid}/databases/{db}/export-capabilities` devuelve, para **ese** motor: tipos de
objeto, formatos, los valores admitidos y el default de cada opción, el dialecto csv, cómo se
empaqueta, los límites numéricos, la lista de códigos de error estables y la **matriz de
compatibilidad**.

La matriz que se publica es **la misma lista que evalúa el servidor** (`compatibility_matrix()`
y `validate_compatibility()` leen la misma estructura). Publicar una promesa que el servidor no
cumple sería peor que no publicar nada: el formulario deshabilitaría lo que no corresponde y
habilitaría lo que después va a dar 422.

Diferencias por motor que aparecen en el payload —y son las **únicas**—:
`object_types` (MySQL/MariaDB agrega `event`; PostgreSQL agrega `materialized_view`, `sequence`,
`enum_type`, `extension`), `sanitize.definer.applicable` y su default ya resuelto,
`scope.scope_note` (PostgreSQL: **solo el schema `public`**, misma limitación que el diff, el
clon y la conversión de collation) y la familia del catálogo de charsets. **La matriz viaja
entera**, incluidas las reglas `engine=postgresql`: filtrar por `when.engine` es del cliente.

Los charsets y collations **no se duplican acá**: capabilities publica la URL del catálogo que ya
existe (`/api/v1/charset-collation-options?family=…`).

---

## Seguridad

### Autorización: hay que decirlo con todas las letras

**El gateway no tiene autorización por objeto.** Hay una sola identidad —un admin único sembrado
en el `lifespan`— y `AdminDep` es el único guard de todo el proyecto. `owner_id` de
`ManagedDatabase` **no es un principal de acceso**: es un FK a `server_users`, o sea una cuenta
del **motor**.

De los dos escenarios posibles aplica el segundo: **cuenta de servicio compartida con
privilegios amplios** (pseudo-root). El motor **no protege nada**. La diferencia con el caso
peligroso es que **no hay vector de escalada entre usuarios porque no hay usuarios**: quien tiene
sesión de admin ya puede dropear cualquier base del inventario por otros endpoints. La
exportación **no amplía la superficie de autorización; amplía la de extracción**.

Este módulo **no inventa un modelo de autorización que el proyecto no tiene**. Los controles
compensatorios son los que el proyecto ya usa en su lugar y acá se aplican todos: auditoría
fail-closed en el momento de la divulgación, doble confirmación, rate limiting y cuotas, TTL
corto y borrado garantizado.

Hay un `job.created_by_admin_id == admin["id"]` en la descarga (**403 `export.not_owner`**). Con
un solo admin es trivial, pero está escrito para que el día que haya multiusuario no sea un
agujero. (Sigue **solo** en la descarga: extenderlo al resto del ciclo es follow-up R7.)

### El destino no puede ser la propia base del gateway

`_validate_scope` rechaza con **409 `export.scope_not_allowed`** un export apuntado a la BD de
metadatos del gateway. Sin esto, si esa base vive en un servidor del inventario —cosa que nada
impide—, el artefacto se llevaría `servers` (incluido `root_password_encrypted`), `server_users`,
el **`audit_log` completo** —que es el único control compensatorio que declara "Riesgo aceptado"
más abajo— y `migration_select_results`.

Se reusa el guard que ya usa la consola SQL (`query_policy.is_gateway_metadata_target`), que
**resuelve ambos hosts a IPs e interseca** en vez de comparar texto: registrar el servidor por su
IP en lugar de su nombre no lo evade. El guard es **por base**, no por servidor: otra base del
mismo host se exporta normalmente.

### Auditoría

| Acción | Cuándo | Tipo |
|---|---|---|
| `database_export.plan` | al crear el plan | `record`, `touched_engine=True` |
| `database_export.execute` | **antes** de encolar | `record_intent` **fail-closed**, `touched_engine=True` |
| `database_export.execute` | al terminar | `record` agregado (objetos, filas, bytes, completa, drift) |
| `database_export.download` | **antes** de abrir el archivo | `record_intent` **fail-closed**, `touched_engine=False` |

`plan` lleva `touched_engine=True` porque el flag significa "esta operación **contactó** el
motor", no "lo mutó" — y el plan snapshotea la estructura en vivo.

El `download` es el punto crítico y replica el patrón de `reveal_password`: **se audita la
intención antes de que salga un solo byte**; si la auditoría no persiste, aborta y el artefacto
no se entrega. Una exportación de datos **es una divulgación**, no una lectura más. El `detail`
es un resumen corto: nunca credenciales, nunca filas.

### Inyección

- **Identificadores**: siempre del catálogo del motor, delimitados con
  `identifiers.quote_identifier`. Nunca concatenación de entrada del cliente.
- **Patrones**: `fnmatch` contra nombres del catálogo. No llegan a SQL.
- `exclude_gateway_internal_tables` es **obligatorio** en todos los caminos que enumeran tablas
  (`_gw_v_`, `_gw_stg_`) — es el fix del incidente de producción que emitía
  `DROP TABLE _gw_v_{slug}`.
- **`data.per_object.{tabla}.where` — el punto más delicado**, porque es entrada arbitraria del
  usuario que termina dentro de una consulta. Se ofrece, pero validado con la maquinaria que ya
  existe, **antes de tocar el motor**:
  0. **ningún token de comentario** (`--`, `/*`, `*/`, y `#` solo en la familia MySQL — en
     PostgreSQL es el XOR de enteros). Un filtro de exportación no tiene ningún uso legítimo
     para un comentario, y admitirlos hacía que toda la validación dependiera de que sqlglot y
     el motor coincidieran en qué es código — **y no coinciden**: sqlglot no tokeniza el
     contenido de un `/*! … */` (ni de un `/*M! … */`), así que
     `1=1 /*!50000 UNION SELECT user,authentication_string FROM mysql.user */` pasaba los cinco
     controles de abajo con un árbol que solo veía `1=1`;
  1. se arma la consulta **real y COMPLETA** —con `ORDER BY` y `LIMIT` incluidos— con
     `export_spec.build_row_select_sql`, que es **el mismo constructor** que usa el writer, y
     con el filtro **entre paréntesis**. Validar un prefijo no alcanzaba: con `where = "1=1 -- "`
     la cola de la sentencia quedaba comentada y la tabla salía entera, sin orden, ignorando el
     `limit` que el operador confirmó y que el `confirm_token` hasheó;
  2. se parsea entera con sqlglot y se clasifica con `query_policy.classify_statement`: debe dar
     `read`, con el criterio fail-closed que ese módulo ya tiene (SQL ilegible, nodo no mapeado o
     `exp.Command` ⇒ peligroso);
  3. se rechazan subconsultas, CTEs y `UNION`;
  4. el **conjunto de tablas del AST debe ser exactamente `{tabla}`** y ninguna columna puede
     venir calificada con otra base o tabla — mismo criterio que `query_runner.estimate_impact`
     usa para descartar un COUNT que no corresponde. Corta las referencias a
     `information_schema` y a otras tablas.

  Cualquier fallo ⇒ **422 `export.invalid_row_filter`** nombrando la tabla y el motivo con un
  vocabulario cerrado (`unparseable`, `not_read_only`, `subquery_not_allowed`,
  `foreign_table_reference`, `foreign_column_qualifier`, `comment_not_allowed`, `too_long`,
  `empty_filter`, `multiple_statements`). **El texto del filtro nunca se devuelve en la
  respuesta** — misma regla anti-reflexión que `validate_identifier`.
- **`output.filename_template`**: sustituciones de una whitelist cerrada (`{database}`,
  `{object}`, `{date}`, `{time}`, `{job_id}`). Un token desconocido o una llave suelta dan 422; el
  nombre final lo construye y **sanea el servidor**, incluido el valor sustituido, así que `../`,
  `/`, `\` y los dos puntos de una unidad de Windows quedan neutralizados. El cliente **nunca**
  recibe ni envía una ruta.

### R4 — nunca `str(exc)` del motor

Los errores de driver van a `map_driver_error`; el detalle crudo va a `logger.exception` con el
Request ID y la respuesta lleva un motivo acotado. **La fase de datos sigue la regla más
estricta**: el mensaje de un driver puede incrustar valores de filas (`Duplicate entry
'alice@x.com'`), así que **no se persiste en el ítem**. El `reason` de un ítem es siempre de
vocabulario cerrado (`structure_disabled`, `no_ddl_rendered`, `all_columns_generated`,
`manifest_only`, `format_data_only`, `unsupported_type:<tipo>`).

### Límites y cuotas

- Rate limit: **3/min** en `execute`, `download` y `content`; **10/min** en
  `create`/`objects`/`resolve-selection`/`preview`; **30/min** en `export-capabilities`.
  Sin límite en el polling, los ítems, el manifiesto y el `cancel`.
- `EXPORT_MAX_WORKERS=1` y `EXPORT_MAX_CONCURRENT_GLOBAL=2` (**409 `export.quota_exceeded`**).
  Una exportación lee la base **entera** del origen; sin techo es un vector de degradación y la
  única defensa barata contra una exfiltración lenta hecha a fuerza de jobs.

  El techo cuenta el trabajo **admitido**, no cuántos jobs corren en este instante:
  `max(running en la BD, export_runner.inflight_count())`. Contando solo `running`, con un pool
  de un worker el techo era siempre 1 y la **cola** quedaba sin acotar. No se cuenta `pending` de
  la base a propósito: incluye los planes creados y nunca ejecutados, que no son trabajo admitido
  y habrían bloqueado el endpoint por planes viejos que nadie va a lanzar.
- `EXPORT_DISK_MIN_FREE_BYTES` se comprueba **antes de abrir nada**, con la estimación del
  preview, y `EXPORT_ARTIFACT_MAX_BYTES` aborta la corrida y borra el parcial. Llenar el disco
  del gateway no degrada la exportación: **tumba el gateway entero**.
- `EXPORT_ENABLED=False` es el **kill switch**: ningún endpoint funciona (409
  `export.disabled`), ni siquiera planear, y el worker lo re-comprueba al arrancar el job — la
  vía de salida tiene que poder cerrarse **sin esperar a que la cola se vacíe**.

### Riesgo aceptado: **no hay enmascarado**

El enmascarado por columna (anular / truncar / valor fijo / derivado determinista) queda **fuera
de alcance**. La consecuencia se registra acá y no se omite:

> **Este módulo permite extraer datos personales o regulados en claro, y no ofrece ningún
> control técnico para evitarlo.**

El único control compensatorio en pie es la auditoría de la sección anterior, que por eso no es
negociable. Si el sistema llega a tratar datos regulados, esto pasa a ser un **bloqueante de
cumplimiento** y hay que implementar el enmascarado antes de usar el módulo contra producción.
Mientras tanto, `EXPORT_ENABLED=False` existe justamente para poder cerrar la vía de salida sin
re-desplegar código.

---

## Errores, resultados parciales y observabilidad

- `on_error`: `stop` (corta al primer objeto fallido) | `continue` (best-effort, reporta).
- **Nunca se entrega un artefacto parcial sin marca inequívoca**. Cuando el job no termina `ok`:
  el manifiesto trae `complete: false`, la descarga responde con la cabecera
  `X-Export-Complete: false`, y el artefacto lleva la marca **por dentro** — un banner
  `-- EXPORTACIÓN INCOMPLETA …` al final del `.sql`, una línea `{"incomplete": true}` en el
  ndjson, `"complete": false` en el json y una entrada `000-EXPORTACION-INCOMPLETA.txt` en el
  zip. Dónde va la marca lo decide el empaquetador **porque depende del formato**: un comentario
  SQL pegado al final de un CSV o de un JSON los corrompe.
- **Reporte de incidencias por objeto** (`GET .../items`): qué se omitió y por qué.
- `progress` trae `phase`, objetos, filas, sentencias, bytes, `warnings`, `generator_version`,
  `engine_version` y `degradations`. Se persiste **throttleado a ~3 s**: sin eso, una tabla de
  millones de filas dispara un `UPDATE` contra la BD del gateway por cada trozo.
- **Nunca datos exportados en los logs.**

---

## Limitaciones conocidas (v1)

1. **La estructura no es consistente en MySQL/MariaDB.** Límite del motor, no del diseño; se
   reporta con `structure_drift_detected` y un aviso. Ver arriba.
2. **Los cuerpos salen SIN el calificador de la base de origen** (corregido; antes era una
   fuga). En MySQL/MariaDB el motor guarda el cuerpo de una vista/rutina con el esquema
   **calificado** (`` `origen`.`tabla` ``), así que emitirlo tal cual hacía que **restaurar el
   artefacto en una base con otro nombre dejara las vistas leyendo de la base de ORIGEN**, en
   silencio. El clon resuelve lo mismo **re-calificando** (`_requalify_body`) porque ahí el
   destino se conoce; en un export no se conoce, así que se **quita** el calificador propio con
   `sql_dialect.strip_self_schema_qualifier` y el motor resuelve contra la base activa de quien
   ejecuta el script — mismo criterio que `mysqldump`. Una referencia a **otra** base se
   conserva: eso es parte de la definición del objeto. PostgreSQL no está afectado (sus cuerpos
   no llevan el nombre de la base; el **esquema** sí es parte de la definición y no se toca).
3. **El artefacto de PostgreSQL con `scope_ddl=DROP_CREATE` no es ejecutable de un tirón.**
   `DROP DATABASE`/`CREATE DATABASE` no se pueden ejecutar desde una conexión a esa misma base ni
   dentro de un bloque transaccional, y el artefacto **no emite el `\connect`** que emitiría
   `pg_dump --create`. El preview lo avisa; quien lo ejecute tiene que conectarse a otra base
   (por ejemplo `postgres`) para esas dos sentencias.
4. **Las particiones no se reproducen.** El `SchemaSnapshot` no captura la cláusula
   `PARTITION BY`, así que `sanitize.partitions: true` **no puede cumplirse**: se emite un aviso
   en vez de dejar creer que viajaron. Una tabla particionada restaurada sin particiones
   "funciona" y se degrada en silencio, que es el peor resultado posible.
5. **No se emite `REFRESH MATERIALIZED VIEW`.** Las matviews se crean con
   `CREATE MATERIALIZED VIEW … AS <def>`, que en PostgreSQL las **puebla al crearlas**; no hay un
   `REFRESH` explícito al final del script.
6. **`sanitize.definer='keep'` es hoy indistinguible de `omit`** en el camino del snapshot (ver
   arriba).
7. **Los jobs no son durables.** Worker in-process con `ThreadPoolExecutor`: si el proceso se
   reinicia, los jobs en curso quedan `interrupted` (barrido en el `lifespan`) y hay que
   relanzarlos a mano. Misma limitación conocida que el clon y la conversión de collation. Una
   cola durable es trabajo futuro.
8. **PostgreSQL cubre solo el schema `public`** (`scope_note`), misma limitación que el diff, el
   clon y la conversión.
9. **Sin muestreo con cierre referencial** ("traeme 1000 pedidos y sus clientes"): exige recorrer
   el grafo de FKs por fila y es un módulo en sí mismo.
10. **Sin plantillas ni exportaciones programadas.** Pero `ExportSpec` es serializable y
    autosuficiente y se persiste íntegro, así que agregarlas después es una tabla y un endpoint,
    no una reescritura.
11. **Tercera copia del runner.** `export_runner.py` es la tercera copia consciente del patrón de
    `clone_runner`/`collation_conversion_runner`, y hay **dos vocabularios de ítem** divergentes
    en el proyecto (`applied/failed/skipped` en el clon, `ok/error/skipped` acá y en collation).
    Es **deuda anotada**: el cuarto módulo de esta familia debería ser el disparador de
    unificarlas, no éste.

---

## Variables de entorno

Todas en `app/core/environments.py`. ⚠️ **`.env.example` no pudo actualizarse** (el archivo está
fuera de los permisos del entorno donde se implementó el módulo): esta tabla y los comentarios de
`environments.py` son, por ahora, la documentación de referencia.

| Variable | Default | Para qué |
|---|---|---|
| `EXPORT_ENABLED` | `True` | **Kill switch** global. `False` = ningún endpoint funciona (409). Existe porque una exportación es una extracción masiva de datos en claro y hay que poder cerrar la salida sin re-desplegar. |
| `EXPORT_TTL_HOURS` | `24` | Vida útil del **PLAN**. Vencido, preview/execute exigen replanear (410): un plan viejo describe un catálogo que ya no existe y su fingerprint deja de ser una defensa real. |
| `EXPORT_ARTIFACT_TTL_MINUTES` | `30` | Retención del **artefacto** desde que el job termina. |
| `EXPORT_SINGLE_USE_DOWNLOAD` | `True` | La descarga completa borra el archivo y marca `consumed`. `False` alarga la exposición. |
| `EXPORT_PURGE_INTERVAL_MINUTES` | `10` | Cada cuánto corre la purga por TTL. `0` desactiva la periódica (la del arranque sigue). |
| `EXPORT_ARTIFACT_DIR` | `/app/exports` | Directorio de spool, modo `0700`. **Volumen propio**, no el de uploads. |
| `EXPORT_DISK_MIN_FREE_BYTES` | `512 MiB` | Espacio libre mínimo tras la estimación del preview. `0` desactiva (desaconsejado). |
| `EXPORT_ARTIFACT_MAX_BYTES` | `5 GiB` | Tope duro del artefacto; al superarlo aborta y borra el parcial. `0` = sin tope. |
| `EXPORT_INLINE_MAX_BYTES` | `1 MiB` | Tope de la entrega en línea. Publicado en capabilities. |
| `EXPORT_MAX_STATEMENT_BYTES` | `1 MiB` | Techo del corte de una sentencia de datos. Un `INSERT` más grande supera el `max_allowed_packet` del destino. |
| `EXPORT_ROWS_PER_STATEMENT` | `200` | Filas por sentencia **por defecto**. Es un techo superior; el corte real lo manda el de bytes. |
| `EXPORT_BATCH_ROWS` | `1000` | `yield_per` del cursor en streaming. Con `LONGTEXT`/`BLOB` conviene bajarlo. |
| `EXPORT_MAX_DURATION_SECONDS` | `14400` (4 h) | Plazo duro de una corrida. Una exportación de horas degrada el origen. |
| `EXPORT_STATEMENT_TIMEOUT_MS` | `1800000` (30 min) | Timeout de sentencia de la conexión dedicada. `0` = sin límite (desaconsejado). |
| `EXPORT_IDLE_TRANSACTION_TIMEOUT_MS` | `300000` (5 min) | Solo PostgreSQL: desacopla `idle_in_transaction_session_timeout` del de sentencia. `0` = sin límite. |
| `EXPORT_MAX_WORKERS` | `1` | Workers del pool. `1` a propósito: una exportación lee la base entera. |
| `EXPORT_MAX_CONCURRENT_GLOBAL` | `2` | Exportaciones **admitidas** a la vez: en cola + en ejecución (409 `export.quota_exceeded`). Contar solo las que corren dejaba la cola sin acotar. |
| `EXPORT_MAX_PARTS` | `500` | Tope de archivos dentro de un artefacto. |

---

## Archivos

| Pieza | Archivo |
|---|---|
| Rutas (12 endpoints) | `app/routes/v1/database_exports.py` |
| Orquestación | `app/controllers/export_controller.py` |
| Modelo puro del spec, matriz, selección, validación del `where`, nombres de archivo | `app/services/db_admin/export_spec.py` |
| Literales SQL y de texto (`render_value` / `render_value_text`) | `app/services/db_admin/sql_literals.py` |
| Generador incremental (sql / csv / json / ndjson) | `app/services/db_admin/export_writer.py` |
| Transacción de lectura y plazo duro | `app/services/db_admin/export_session.py` |
| Organización, fragmentación, compresión, índice y marca de incompleto | `app/services/export_package.py` |
| Spool, checksum, TTL, purga, barrido de huérfanos | `app/services/export_storage.py` |
| Worker in-process (3ª copia) y guard por BD | `app/services/export_runner.py` |
| Modelos y estados | `app/models/export_job.py` (`ExportJob`, `ExportJobItem`, `ExportArtifact`) |
| Schemas Pydantic | `app/schemas/export.py` |
| Métodos `export_*` por dialecto | `app/services/db_admin/base_adapter.py`, `mysql_adapter.py`, `postgres_adapter.py` |
| Migración | `alembic/versions/20260816_1404_a9b0c1d2e3f4_add_export_job_tables.py` |

---

## Qué está verificado y qué **no**

**Verificado** (por ejecución directa de los tests, sin `pytest` — política del proyecto):

- **81 checks HTTP** de extremo a extremo (`tests/test_api_database_exports.py`, 59 funciones)
  con `TestClient` + SQLite + adapter falso: los 12 endpoints, 401 sin sesión, los guards de
  409/410/422, `confirm_token` cruzado, fingerprint cambiado, inline sobre el tope, descarga
  consumida.
- **27 checks de ciclo real** con el **writer real** y el **adapter real** (`MySQLAdapter`), sin
  motor: `execute` → `run_job` → artefacto en disco → descarga (incluida una parcial con
  `Range`) → hook `counter_value` de punta a punta → purga por TTL → barrido de huérfanos.
- **78 checks del writer** (`tests/test_export_writer.py`, 67 funciones): orden de emisión,
  saneamiento, determinismo, los cuatro formatos, columnas generadas excluidas.
- **76 checks del spec** (`tests/test_export_spec.py`, 64 funciones): matriz de compatibilidad
  completa, selección y patrones, subconjunto y su excepción, validación del `where`, plantillas
  de nombre de archivo.
- **23 checks de literales** (`tests/test_export_literals.py`).
- La migración de las tres tablas, con ciclo `upgrade`/`downgrade`/`upgrade` en **SQLite**.

**NO verificado — nada de esto se probó jamás contra un motor real:**

- `scripts/verify_export_e2e.py` está **escrito pero NUNCA EJECUTADO**: el entorno de desarrollo
  no tiene Docker ni MySQL/MariaDB/PostgreSQL. Hay precedente exacto
  (`scripts/verify_query_console_e2e.py`, en la misma situación). **Un script de verificación
  que nadie corrió no verifica nada**, y por eso todo lo que ese script cubre está en esta lista
  y no en la anterior:
  - que el artefacto **se ejecute** contra un motor real y el esquema resultante coincida con el
    del origen (la prueba de aceptación principal);
  - que la transacción de `export_session` se abra **de verdad** — el 3 pasos de MySQL/MariaDB y
    el `postgresql_readonly` de psycopg — y que `SET idle_in_transaction_session_timeout` sea
    aceptado;
  - `export_counter_value_sql` contra `information_schema.TABLES.AUTO_INCREMENT` y contra
    `pg_sequence_last_value` (**un builtin no documentado de PostgreSQL**: su existencia en PG 16
    solo se puede afirmar probándola);
  - el determinismo byte a byte entre dos corridas contra el mismo motor;
  - que un `csv` y un `ndjson` generados se **reimporten** de verdad;
  - los valores límite (binarios con `\x00`, fechas extremas, `Decimal` de precisión arbitraria,
    multibyte) sobreviviendo la ida y vuelta por el literal del motor;
  - la limitación de re-calificación de cuerpos (punto 2 de las limitaciones), que está
    **deducida del código**, no medida.
- La **migración de Alembic contra la BD del gateway real** (solo se probó en SQLite; la
  migración inicial del proyecto usa `batch_alter_table`, así que el caveat de siempre aplica).
- El comportamiento bajo carga: consumo de memoria plano con una tabla de millones de filas y
  una cancelación que libere **de verdad** la conexión y la transacción del origen. Está
  diseñado para eso (streaming con `yield_per`, corte por bytes, cierre en `finally`) pero
  **medido no está**.
- `.env.example` no se pudo actualizar con las 18 variables `EXPORT_*` (permisos del entorno).
