# 10 — Exportación de bases de datos (equivalente a "Export database as SQL")

> **Estado: IMPLEMENTADO (F1–F6). Verificado sin motores reales.**
>
> Este documento nació como el entregable #1 y #2 del pedido (diseño + supuestos) y **se
> conserva como el registro de las decisiones**, incluidas las que el propio diseño tuvo que
> corregir durante la implementación (están marcadas en línea como *"Corrección al diseño
> original (F2/F3)"* y valen más que la versión que reemplazan: cada una es un lugar donde el
> diseño prometía algo que el motor no da). Los §§ citados entre paréntesis refieren al prompt
> de requisitos.
>
> **Guías de uso derivadas de este plan** (son la documentación viva; este archivo es el
> histórico de por qué): [`docs/features/database-export.md`](../features/database-export.md) y
> [`docs/api-reference-v10.md`](../api-reference-v10.md).
>
> **Verificado** — por ejecución directa de los tests, sin `pytest` (política del proyecto):
> 81 checks HTTP de extremo a extremo (`tests/test_api_database_exports.py`, TestClient +
> SQLite + adapter falso, los 12 endpoints y todos los guards), 27 checks de ciclo real con el
> **writer real** y el **adapter real** sin motor (`execute` → `run_job` → artefacto en disco →
> descarga, incluida una parcial con `Range` → hook `counter_value` → purga por TTL → barrido
> de huérfanos), 78 checks del writer, 76 del spec, 23 de literales, y la migración
> `a9b0c1d2e3f4` con ciclo `upgrade`/`downgrade`/`upgrade` en SQLite.
>
> **NO verificado — nada de este módulo se probó jamás contra un motor real:**
> - `scripts/verify_export_e2e.py` (la **prueba de aceptación principal** del §13) está
>   **escrito pero NUNCA EJECUTADO**: el entorno donde se implementó no tiene Docker ni
>   MySQL/MariaDB/PostgreSQL. Precedente exacto: `scripts/verify_query_console_e2e.py`, en la
>   misma situación. Queda sin confirmar: que el artefacto **se ejecute** contra una instancia
>   limpia y el esquema resultante coincida con el del origen (`diff_snapshots`); que la
>   transacción de `export_session` se abra **de verdad** (el 3 pasos de MySQL/MariaDB y el
>   `postgresql_readonly` de psycopg) y que `SET idle_in_transaction_session_timeout` sea
>   aceptado; `export_counter_value_sql` contra `information_schema.TABLES.AUTO_INCREMENT` y
>   contra `pg_sequence_last_value` (**builtin no documentado de PostgreSQL**); el determinismo
>   byte a byte del §8.3 contra un motor; la reimportación real de un `csv` y de un `ndjson`;
>   y los valores límite del §13.5 sobreviviendo la ida y vuelta por el literal del motor.
> - La migración **contra la BD del gateway real** (solo se corrió en SQLite).
> - El comportamiento a escala: consumo de memoria plano con una tabla de millones de filas y
>   una cancelación que libere de verdad la conexión y la transacción del origen. Está diseñado
>   para eso, pero **medido no está**.
> - La limitación de re-calificación de cuerpos en MySQL/MariaDB (ver más abajo) está
>   **deducida del código**, no medida contra un motor.
> - **`.env.example` no se pudo actualizar** con las 18 variables `EXPORT_*` (el archivo está
>   fuera de los permisos del entorno de implementación); están documentadas en
>   `app/core/environments.py` y en `docs/features/database-export.md`.
>
> **Desviaciones respecto de lo que este documento planificaba**, todas deliberadas y
> documentadas en la guía del feature: las **rutinas se emiten antes que las vistas** (§8.4 las
> tenía al revés y el orden bueno es el que ya tiene el repo); **no se emite `REFRESH
> MATERIALIZED VIEW`** (paso 9); la descarga usa `FileResponse` y no `StreamingResponse`
> (Starlette 0.50 ya implementa `Range` ahí, §10.2); y **los cuerpos de vistas/rutinas no se
> re-califican** al restaurar con otro nombre en MySQL/MariaDB — un volcado se restaura con su
> nombre, igual que `mysqldump`; para renombrar está el módulo de clonado.

## Objetivo

Exportar el contenido de una base de datos gestionada por el gateway —**estructura**, **datos**
o ambos— a un artefacto descargable, en modo **estrictamente de solo lectura** sobre el origen.
Cualquier sentencia destructiva existe únicamente como **texto dentro del artefacto**.

Alcance: **solo backend**. No se diseña ni se implementa interfaz. Pero el backend publica
las reglas como datos (capacidades, matriz de compatibilidad, validación en seco) para que el
cliente no duplique lógica de negocio (§2.3).

---

## 1. Lo que YA existe y se reutiliza (no se reinventa nada)

Este módulo es el **cuarto** de su familia. El patrón está sentado y se copia:

| Pieza | De dónde sale | Se reutiliza |
|---|---|---|
| Flujo plan → inventario → resolve-selection → preview → execute → polling → items → cancel | clon, schema-comparisons, collation-conversion | **tal cual** |
| Worker in-process `ThreadPoolExecutor`, no durable, barrido `interrupted` en el `lifespan` | `clone_runner.py`, `collation_conversion_runner.py` | **tal cual** (3ª copia) |
| Estados `pending→running→succeeded/failed/interrupted/canceled` | `clone_job.py`, `collation_conversion_job.py` | **tal cual** |
| Anti-TOCTOU: `source_fingerprint` + re-chequeo en preview / execute / worker | `clone_controller._snapshot_fingerprint` | **tal cual** |
| Doble factor: `confirm_target_name` + `confirm_token` = hash del **plan resuelto** | clon / collation | **tal cual** |
| Auditoría `<recurso>.plan` / `<recurso>.execute` con `record_intent` fail-closed | `audit.py` | **tal cual** |
| Lectura de catálogo por motor | `ServerAdapter.structural_snapshot` / `dump_structure` | **extendido** (§7) |
| Escapado de literales SQL | `snapshot_data.render_value` + `identifiers.quote_string_literal` | **extraído y generalizado** (§8) |
| Quoting de identificadores, whitelist, BDs reservadas, tablas internas `_gw_*` | `identifiers.py` | **tal cual** |
| Envoltura `DELIMITER` para cuerpos MySQL | `schema_comparison_controller._render_statement_block` | **extraído** |
| Catálogo charset↔collation por familia de motor | `charset_catalog.py` | **tal cual** (§11 del prompt) |
| Descarga sin `ApiResponse` (`Response` + `Content-Disposition`) | `GET /schema-comparisons/{id}/export` | **convenciones sí, buffering no** |

**Lo que NO existe y hay que construir** (hallazgo central de la exploración):

1. **No hay ninguna infraestructura de artefactos.** El único download del repo arma el `.sql`
   completo **en memoria** (`"\n".join(lines)`), no hay `StreamingResponse` ni `FileResponse`
   en todo el proyecto, `uploads/` es un buzón de entrada sin TTL ni limpieza, y **ninguna
   tabla de jobs tiene purga** (solo `migration_results`). Esta sería la primera.
2. **Los adapters no permiten compartir conexión.** Cada método de lectura abre la suya y
   reaplica `AUTOCOMMIT` (`base_adapter._execute_database`). El requisito de punto único en el
   tiempo (§12/§19.4) obliga a inyectar conexión — cambio en código compartido por 5 features.
3. **No hay renderizador de filas en streaming.** `snapshot_data.build_seed` bufferea todo,
   exige PK y tiene techos duros de 5000 filas / 5 MB. Es el contrato de la feature *semilla*
   y **no se toca**: se extrae el primitivo compartido.

---

## 2. Decisiones cerradas del prompt: cómo se resuelven

| § | Decisión cerrada | Resolución en este diseño |
|---|---|---|
| 19.1 | Ámbito apoyado en la abstracción existente | `(server_id, database)` — patrón "por identidad", igual que collation-conversion. PostgreSQL: **solo schema `public`**, misma limitación que diff, clon y conversión. Se declara en `scope_note`. |
| 19.2 | Multiobjeto y multitipo en un artefacto | Sí, con el orden por dependencias de §10. |
| 19.3 | Cualquier conexión configurable | `build_target(server)` → pseudo-root. Ver §9 (autorización). |
| 19.4 | Consistencia punto único en el tiempo | Una conexión, una transacción de lectura. **Con un límite irreducible en MySQL/MariaDB: ver §6 — hay que reportarlo, no taparlo.** |
| 19.5 | Siempre asíncrona, una sola ruta | Sí. Sin variante síncrona ni para 3 filas. |
| 19.6 | Estructura y datos como conjuntos independientes, datos ⊆ estructura | Dos selecciones separadas con restricción de subconjunto verificada en el servidor. **Excepción decidida y documentada: ver §5.3.** |
| 19.7 | El artefacto no se conserva | Almacenamiento efímero con TTL corto, un solo uso y purga garantizada. Tensión resuelta en §10. |
| 19.8 | Enmascarado fuera de alcance | **Riesgo aceptado explícito**, registrado en §9.6. |
| 19.9 | Reutilizar infraestructura existente | Ver §1. |
| 19.10 | Plantillas/programación fuera de alcance, sin cerrar la puerta | `ExportSpec` es una estructura serializable y autosuficiente que se persiste completa en el job. Agregar plantillas después es una tabla y un endpoint, no una reescritura. |

---

## 3. Componentes

```
app/models/export_job.py              ExportJob, ExportJobItem, ExportArtifact
app/schemas/export.py                 ExportSpec y todos los DTOs de API
app/controllers/export_controller.py  orquestación (plan/objects/resolve/preview/execute/run_job/download)
app/services/export_runner.py         3ª copia del runner (~90 líneas)
app/services/export_storage.py        spool, checksum, TTL, purga, entrega
app/services/db_admin/export_spec.py  PURO: resolución de selección, matriz de compatibilidad, validación
app/services/db_admin/export_writer.py PURO/streaming: serializadores sql | csv | json | ndjson
app/services/db_admin/sql_literals.py  extraído de snapshot_data: render_value generalizado
app/routes/v1/database_exports.py     rutas
alembic/versions/xxxx_add_export_jobs.py
docs/features/database-export.md      guía de uso
docs/api-reference-v10.md             contrato para el equipo de frontend
tests/test_export_spec.py, tests/test_export_writer.py, tests/test_api_database_exports.py
scripts/verify_export_e2e.py
```

**La abstracción de dialecto es `ServerAdapter`** (§9 del prompt). No se crea una paralela: el
proyecto ya tiene exactamente esa interfaz y agregar un tercer motor ya significa hoy
"implementar el adapter y nada más". Los métodos nuevos van a `base_adapter` (abstractos o con
default) y los implementan `mysql_adapter` / `postgres_adapter`.

---

## 4. `ExportSpec` — el modelo de la petición

Estructura serializable y **autosuficiente**: basta por sí sola para reproducir el mismo
artefacto (§3 del prompt). Se persiste íntegra en `export_jobs.spec` (JSON).

```jsonc
{
  "format": "sql",                  // sql | csv | json | ndjson

  "structure": {
    "scope_ddl":  "NONE",           // NONE | CREATE | DROP_CREATE | CREATE_IF_NOT_EXISTS
    "entity_ddl": "CREATE",         // idem
    "drop_if_exists": true,         // variante condicional del DROP, independiente
    "drop_cascade": false,
    "confirm_scope_drop": null      // literal obligatorio si scope_ddl == DROP_CREATE
  },

  "selection": {                    // conjunto ESTRUCTURA
    "types": ["table", "view", "routine", "trigger", "event", "sequence", "type"],
    "mode": "all",                  // all | include | all_except
    "names": [],
    "include_patterns": [],         // glob sobre nombres del CATÁLOGO, nunca sobre SQL
    "exclude_patterns": ["tmp_*", "*_log"]
  },

  "data": {                         // conjunto DATOS (⊆ estructura, ver §5.3)
    "mode": "all_except",           // none | all | include | all_except
    "names": ["sessions", "audit_log"],
    "include_patterns": [],
    "exclude_patterns": [],
    "insert_variant": "insert",     // none | insert | insert_ignore | replace | upsert
    "rows_per_statement": 200,
    "max_statement_bytes": 1048576,
    "include_column_list": true,
    "per_object": {
      "orders": { "where": "created_at >= '2026-01-01'", "limit": 100000 }
    }
  },

  "sanitize": {
    "script_comments": true,        // encabezado + separadores del SCRIPT
    "object_comments": true,        // COMMENT ON / COMMENT= del ESQUEMA  ← opción SEPARADA
    "definer": "omit",              // keep | omit | replace
    "definer_value": null,
    "autoincrement": "auto",        // keep | omit | auto (omit si esa tabla va sin datos)
    "engine_specific_options": false, // ENGINE=, ROW_FORMAT=, TABLESPACE, compresión
    "partitions": true,
    "constraints_placement": "deferred", // inline | deferred (índices y FKs al final)
    "session_preamble": true,       // preámbulo + epílogo con restauración
    "transaction_wrap": false,
    "charset_override": { "mode": "keep", "charset": null, "collation": null }
  },

  "output": {
    "organization": "single",       // single | per_object
    "split_max_bytes": null,
    "compression": "none",          // none | gzip | zip
    "filename_template": "{database}-{date}-{job_id}",
    "file_encoding": "utf-8",
    "delivery": "file"              // file | inline
  },

  "on_error": "continue",           // stop | continue
  "idempotency_key": null
}
```

### 4.1 `scope_ddl` / `entity_ddl` como enumerado, no dos booleanos (§5.2)

Con dos banderas, el estado `eliminar sin crear` **es representable** y hay que parchearlo con
validación dispersa que tarde o temprano se cuela por la API. Con el enumerado **no existe**:
la regla "si se pide eliminación, la creación se incluye siempre" es el tipo, no una `if`.

Se incluyen los cuatro valores. `DROP_CREATE` y `CREATE_IF_NOT_EXISTS` **no son redundantes ni
opuestos por accidente**: son las dos idempotencias que la gente quiere y son incompatibles
entre sí — la primera dice "quiero que quede exactamente esto, destruyendo lo que haya", la
segunda "quiero que exista, sin tocar lo que ya está". Ofrecer solo una obliga al usuario a
editar el script a mano. `drop_if_exists` es **ortogonal**: aplica a la sentencia de
eliminación de `DROP_CREATE`, porque un script que aborta al intentar eliminar algo inexistente
no sirve para nada en la práctica.

---

## 5. Selección

### 5.1 Tres modos + patrones

`all` / `include` / `all_except` cubren el flujo real ("marco todo y quito tres").
`include_patterns` / `exclude_patterns` son **glob** (`fnmatch`) evaluados **contra los nombres
que devolvió el catálogo del motor**, nunca inyectados en SQL. Orden de aplicación:

```
candidatos = catálogo(tipos seleccionados)  −  tablas internas del gateway (_gw_v_, _gw_stg_)
si mode=include     → candidatos ∩ names
si mode=all_except  → candidatos − names
si include_patterns → filtrar por match de alguno
si exclude_patterns → quitar los que matchean alguno   (la exclusión gana)
```

### 5.2 Momento de resolución: **se congela en el `preview`**

El catálogo se lee al crear el plan (con su `source_fingerprint`) y los patrones se resuelven a
una **lista explícita de objetos** en el `preview`. El `confirm_token` hashea esa lista
resuelta. Consecuencia: un objeto creado entre el preview y el execute **no entra**, y si el
catálogo cambió, el fingerprint no coincide y hay que volver a previsualizar. Es la única
lectura compatible con la reproducibilidad que exige §3 y con el anti-TOCTOU que ya usa el
resto del proyecto.

### 5.3 Datos ⊆ estructura, y la excepción decidida (§19.6)

Se modela como **dos listas de selección con restricción de subconjunto verificada en el
servidor**, no como un booleano por tabla. `POST /resolve-selection` devuelve 422 con
`data_without_structure: [...]` cuando el conjunto de datos se sale del de estructura.

**Excepción soportada y documentada**: cuando `structure.entity_ddl == "NONE"` y
`structure.scope_ddl == "NONE"`, la exportación es **"solo datos"** y la restricción no aplica.
Motivo: es un caso de uso legítimo y frecuente (recargar una tabla que ya existe en el destino),
y es la **única** forma que tienen `csv`/`json`/`ndjson` de existir — en esos formatos la
estructura no es ejecutable. El artefacto resultante tiene sentido: son `INSERT`s (o filas) sin
DDL. Lo que se rechaza es la mezcla incoherente: pedir estructura de 12 tablas y datos de una
13ª que no está en el artefacto.

### 5.4 Cierre de dependencias

`POST /resolve-selection` reutiliza el criterio de `plan_integrity.expand_selection` /
`check_closure`: una vista que lee de otra vista, un trigger sin su tabla, una FK a una tabla
excluida. Política idéntica a la del resto del proyecto:

- selección explícita (`include`) → **422** con `missing_dependencies` + `suggested_names`,
  salvo `auto_resolve_dependencies=true`. No se recorta en silencio.
- selección automática (`all`, `all_except`, patrones) → **poda transitiva** +
  `excluded_by_dependency` en la respuesta.

---

## 6. Consistencia: punto único en el tiempo (§12/§19.4) — **y su límite irreducible**

### 6.1 Implementación

Una **única conexión** dedicada al job, tomada con
`database_connection(target, db, bulk=True, statement_timeout_ms=EXPORT_STATEMENT_TIMEOUT_MS)`,
abierta antes del primer objeto y cerrada después del último. Nada de una conexión por tabla ni
de un pool (los engines remotos ya usan `NullPool`).

| Motor | Transacción | Cubre estructura | Cubre datos |
|---|---|---|---|
| PostgreSQL | `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` | **Sí** (el catálogo entra en el snapshot) | Sí |
| MySQL / MariaDB | `SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ` + `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` | **No** | Sí (solo InnoDB) |

> **Corrección al diseño original (F3)**: la familia MySQL se implementa en **tres pasos**
> (`SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ` → `SET SESSION TRANSACTION READ
> ONLY` → `START TRANSACTION WITH CONSISTENT SNAPSHOT`) y no con la lista de características
> separadas por coma. La garantía es idéntica —un `START TRANSACTION` sin modo explícito
> hereda el de la sesión— pero el modo de acceso por SESIÓN está soportado desde MySQL 5.6 /
> MariaDB 10.0, mientras que la forma con coma no lo está de manera uniforme. La conexión es
> dedicada al job y con `NullPool`, así que dejarla en READ ONLY no contamina a nadie.
> En PostgreSQL la transacción se abre con `execution_options(isolation_level="REPEATABLE
> READ", postgresql_readonly=True)` y **no** con un `BEGIN` crudo: psycopg abre la
> transacción por su cuenta al primer `execute`, así que un `BEGIN` nuestro llegaría segundo
> y el servidor lo ignoraría con un aviso, dejando la sesión en READ COMMITTED —sin snapshot
> y sin que nada fallara—. **Nada de esto se ha verificado contra motores reales todavía.**

### 6.2 El límite que hay que reportar, no tapar

**En MySQL/MariaDB, "punto único en el tiempo" para la ESTRUCTURA es técnicamente imposible con
la infraestructura existente.** El snapshot consistente de InnoDB es MVCC de **filas**: el
diccionario de datos y `information_schema` **no** participan. Un `ALTER TABLE` concurrente se
ve inmediatamente, y peor: en MySQL un DDL concurrente sobre una tabla ya leída puede invalidar
la vista consistente de esa tabla. La única forma de congelar también el catálogo es
`FLUSH TABLES WITH READ LOCK`, que exige privilegio `RELOAD` y **bloquea escrituras en el
servidor entero** — inaceptable en un gateway que administra bases de terceros. Es exactamente
la misma limitación que tiene `mysqldump --single-transaction`, documentada por el propio MySQL.

**Qué se hace en consecuencia:**
- PostgreSQL: garantía completa, sin asteriscos.
- MySQL/MariaDB: garantía completa **para datos**; para estructura, se emite un **aviso en el
  preview** y una nota en el encabezado del artefacto y en el manifiesto.
- El `source_fingerprint` se re-verifica **al terminar** el job además de al arrancarlo: si el
  catálogo cambió durante la corrida, el artefacto se marca `structure_drift_detected: true` en
  el manifiesto. No lo invalida —los datos siguen siendo consistentes— pero el operador se
  entera.

### 6.3 Los costos que hay que asumir y monitorear

- Una transacción larga retiene versiones viejas: en PG bloquea el `VACUUM` y hace crecer las
  tablas; en la familia MySQL infla el historial de undo. En un origen con escritura intensa,
  una exportación de horas **degrada el origen**.
- Por eso: `EXPORT_MAX_DURATION_SECONDS` (default 4 h) con **aborto duro y cierre garantizado
  de la transacción** en un `finally`. Una transacción huérfana es un incidente de producción.
- **Conflicto con la infraestructura actual, a resolver en la implementación**:
  `remote_engine` ata `idle_in_transaction_session_timeout` **al mismo valor** que
  `statement_timeout`. Con `bulk=True` ambos pasan a 1 h. Para el export hay que pasar
  `statement_timeout_ms` explícito y —idealmente— desacoplar el `idle_in_transaction` para que
  un export estancado no sostenga un snapshot de PG durante horas.
- Es un argumento fuerte a favor de exportar **contra una réplica de solo lectura** cuando
  exista. Configurable, fuera del alcance de v1.

### 6.4 Cambio requerido en `ServerAdapter` (código compartido)

Hoy `structural_snapshot`, `dump_structure`, `list_tables`, `list_table_stats` y los hooks
`_snapshot_*` **abren su propia conexión en AUTOCOMMIT**. Para leer la estructura dentro del
snapshot hay que inyectar la conexión:

```python
def structural_snapshot(self, database: str, *, conn: Connection | None = None) -> SchemaSnapshot
def dump_structure(self, database: str, *, conn: Connection | None = None) -> StructureDump
```

con un helper `_conn_ctx(database, conn)` que devuelve la conexión dada o abre una nueva. El
default `None` preserva el comportamiento actual, así que **es aditivo y no rompe a los 5
features que lo consumen** — pero toca código compartido y crítico, y esa es la parte cara de
este módulo. Es la única forma honesta de cumplir §19.4.

### 6.5 Advisory lock: **divergencia deliberada del precedente**

Clon, schema-comparisons y collation-conversion toman el advisory lock del motor sobre una
conexión dedicada, en un espacio de claves compartido, durante todo el pipeline. **La
exportación NO lo toma**, y es una decisión, no un olvido:

- La exportación es de **solo lectura**. Tomar el lock exclusivo durante horas bloquearía
  clones, conversiones y migraciones sobre esa BD sin ninguna necesidad de corrección.
- La consistencia de este módulo la da el **snapshot MVCC**, no la exclusión mutua. Son dos
  mecanismos distintos y el segundo no aporta nada que el primero no cubra ya para datos (y no
  arregla el hueco de estructura en MySQL, que es de diccionario de datos, no de concurrencia
  con el gateway).

Lo que **sí** se toma es el guard **in-process** (`export_runner.database_guard`, misma forma
que `clone_runner.target_guard`) para acotar exportaciones simultáneas de la misma BD dentro
del proceso. Barato y sin efecto cross-proceso.

---

## 7. Compatibilidad entre motores

Toda diferencia vive detrás de `ServerAdapter`. Métodos nuevos:

```python
# base_adapter.py — nuevos, con default o abstractos
def export_supported_types(self) -> frozenset[str]
def export_scope_ddl(self, database, mode, *, charset, collation, if_exists) -> list[str]
def export_entity_drop(self, object_type, name, *, if_exists, cascade) -> str
def export_session_preamble(self, *, charset, collation) -> list[str]
def export_session_epilogue(self) -> list[str]        # RESTAURA lo que el preámbulo cambió
def export_use_scope(self, database) -> str           # USE db  /  SET search_path
def export_counter_reset(self, table, value) -> str|None   # AUTO_INCREMENT= / setval()
def export_body_wrapper(self, object_type) -> tuple[str, str] | None   # DELIMITER $$ ... $$
def export_row_order_by(self, table_schema) -> list[str]   # PK, o determinismo degradado
```

### 7.1 Tabla de equivalencias y huecos (se publica en `/export-capabilities`)

| Concepto | MySQL / MariaDB | PostgreSQL | Si no existe |
|---|---|---|---|
| Ámbito | `DATABASE` = `SCHEMA` | `DATABASE` ⊃ `SCHEMA`; se exporta **solo `public`** | `scope_note` en la respuesta |
| Fijar contexto | `USE db;` | `SET search_path TO "public";` | — |
| `DROP DATABASE` en el script | sí | sí, **pero no ejecutable desde una conexión a esa misma BD ni dentro de un bloque transaccional** → aviso obligatorio en el preview | — |
| `DEFINER` | cláusula real en rutinas/vistas/triggers/eventos | **no es el mismo concepto**: propiedad del objeto (`ALTER … OWNER TO`) y `SECURITY DEFINER` son mecanismos distintos | la opción se declara **no aplicable** en capabilities; `keep` es no-op, `omit`/`replace` dan 422 |
| Autoincremento | `AUTO_INCREMENT=n` en la definición | `SERIAL`/`IDENTITY` + `setval()` al final | `autoincrement: omit` suprime ambos |
| Opciones de motor | `ENGINE=`, `ROW_FORMAT=`, `KEY_BLOCK_SIZE` | `TABLESPACE`, `WITH (fillfactor=…)` | `engine_specific_options: false` las omite |
| Particiones | `PARTITION BY …` | tablas particionadas declarativas | `partitions: false` las omite |
| Delimitador de cuerpos | `DELIMITER $$` obligatorio | dollar-quoting, **no lleva `DELIMITER`** | — |
| Suspender verificaciones | `SET FOREIGN_KEY_CHECKS=0`, `SET UNIQUE_CHECKS=0` | `SET session_replication_role='replica'` (requiere superusuario) | se emite y se **restaura**; si no aplica, se omite con nota |
| Vistas materializadas | no existen | sí (+ `REFRESH`) | tipo ausente en capabilities |
| Eventos | sí | no (pg_cron es extensión) | tipo ausente en capabilities |
| Secuencias autónomas | no | sí | tipo ausente en capabilities |
| `COMMENT` de objeto | inline en el DDL | `COMMENT ON …` aparte | `object_comments: false` los omite en ambos |

**Criterio de aceptación arquitectónico**: agregar un tercer motor = implementar los métodos
`export_*` del adapter. Nada más. El generador no lleva ni un `if engine ==`.

> **Correcciones al diseño original (F3)**, todas del tipo "el diseño prometía algo que el
> motor no da":
>
> - **`SET session_replication_role='replica'` NO se emite en PostgreSQL.** Es la única
>   forma de suspender FKs y triggers allá, pero **exige superusuario**: emitirlo haría
>   abortar el script (con `ON_ERROR_STOP`) para cualquier operador normal. Y no hace falta,
>   porque el default `constraints_placement='deferred'` ya emite índices y FKs después de
>   los datos. El preámbulo de PostgreSQL es el de `pg_dump` (`client_encoding`,
>   `standard_conforming_strings`, `check_function_bodies=false`) y el epílogo usa `RESET`,
>   que devuelve cada parámetro al valor de la sesión en vez de fijar uno supuesto.
> - **`scope_ddl='CREATE_IF_NOT_EXISTS'` es 422 en PostgreSQL**: `CREATE DATABASE IF NOT
>   EXISTS` no existe en ninguna versión. Es una regla nueva de la matriz (se publica y se
>   hace cumplir), no un caso especial escondido en el adapter.
> - **`entity_ddl='CREATE_IF_NOT_EXISTS'` es best-effort por tipo** (`export_make_idempotent`
>   devuelve `None` cuando no es expresable). Se emite el `CREATE` normal y se declara en los
>   avisos. Fail-closed: el `IF NOT EXISTS` de rutinas y triggers depende de la VERSIÓN del
>   motor destino (MySQL 8.0.29+), que el gateway no conoce — el artefacto ni siquiera tiene
>   por qué ejecutarse contra el servidor de origen.
> - **`sanitize.definer='keep'` no puede conservar nada** en el camino del snapshot: los
>   cuerpos de `SchemaSnapshot` ya vienen sin `DEFINER` (`_strip_definer_clause` corre al
>   capturarlos). En la práctica `keep` es indistinguible de `omit`; se implementa igual para
>   el día que exista una fuente que sí lo traiga. Nuevo valor **`auto`** (default): resuelve
>   a `omit` en la familia MySQL y a `keep` en PostgreSQL, para que un cuerpo `{}` no sea un
>   422 contra PG.
> - **Las particiones NO se reproducen.** El `SchemaSnapshot` no captura la cláusula
>   `PARTITION BY`, así que `partitions: true` no puede cumplirse: se emite un aviso en vez
>   de dejar creer que viajaron (una tabla particionada restaurada sin particiones "funciona"
>   y se degrada en silencio).
> - **`constraints_placement='inline'` emite índices y FKs como `ALTER` contiguos** al
>   `CREATE TABLE`, no dentro de la sentencia. El renderer compartido (`render_diff`) los
>   emite así, y escribir un segundo renderer de `CREATE TABLE` reintroduciría los bugs que
>   ese ya tiene resueltos con test (`AUTO_INCREMENT` que no encabeza la PK, `UNIQUE KEY`
>   duplicada, `serial` de PostgreSQL, fidelidad de `ENUM`/`UNSIGNED`).
> - **Métodos `export_*` añadidos a la lista del §7** porque sin ellos el writer necesitaría
>   un `if engine ==`: `export_definer_clause`, `export_make_idempotent` y
>   `export_insert_wrapper` (las variantes `insert_ignore`/`replace`/`upsert` son sintaxis
>   propietaria; `replace` no tiene equivalente en PostgreSQL y devuelve 422).
>   `export_counter_reset` recibe además la **columna**: en PostgreSQL el contador vive en
>   una secuencia asociada a ella, no en la tabla.

---

## 8. Generación del artefacto

### 8.1 Streaming obligatorio

Prohibido materializar el artefacto ni el contenido de una tabla en memoria (§12). El writer es
un **generador incremental** que escribe a un archivo de spool:

```
export_writer.write(spec, source_reader, sink) -> ExportStats
```

- Lectura de filas: `conn.execution_options(stream_results=True, yield_per=EXPORT_BATCH_ROWS)`
  sobre `SELECT <cols> FROM <t> [WHERE …] ORDER BY <pk> [LIMIT …]` — mismo mecanismo que
  `data_copy._iter_source_rows`, que ya está probado contra los 3 motores.
- Escritura: buffer acotado por **bytes**, no por filas (§6). `rows_per_statement` es un techo
  superior; el corte real lo manda `max_statement_bytes`, porque una tabla con `LONGTEXT`
  revienta cualquier límite basado en conteo.
- Consumo de memoria **plano** e independiente del tamaño de la tabla. Es un criterio de
  aceptación con test (§13).

### 8.2 Escapado de valores

Se extrae `snapshot_data.render_value` a `sql_literals.py` y se **generaliza**, sin tocar
`build_seed` (su contrato de semilla —PK obligatoria, techos 5000/5 MB— se preserva):

- se mantiene: `None`→`NULL`; `bool`; `int`; `float` (no finito → error); **`Decimal` → `str`**
  (nunca por punto flotante, §6); `bytes` → `x'…'` / `decode(…,'hex')`; fechas ISO; `dict`/`list`
  → JSON; `str` con `quote_string_literal` (que ya cubre `NO_BACKSLASH_ESCAPES` y el `E'…'` de PG).
- **se agrega**: `timedelta` (el `TIME` de MySQL, hoy explotaría) y `UUID` — ambos ya resueltos
  en `value_json.py`, se unifica el criterio.
- **se mantiene fail-closed**: un tipo desconocido **no** se serializa "a lo que salga". Aborta
  el objeto con `reason="unsupported_type:<nombre>"` y lo reporta en el ítem.
- `\x00` sigue siendo rechazado.
- Para `csv`/`json`, los binarios usan `hex` o `base64` según `binary_encoding` (opción nueva,
  publicada en capabilities).

**Columnas generadas/calculadas se EXCLUYEN de los `INSERT`** (`ColumnInfo.computed` ya está en
el DTO). Incluirlas produce un script que falla — es un caso con test.

### 8.3 Determinismo (§10.2) — requisito, no adorno

Dos exportaciones del mismo esquema sin cambios producen el **mismo artefacto byte a byte**,
salvo la fecha del encabezado (suprimible con `script_comments: false`).

- Objetos: orden `(paso, rango topológico, nombre)` — el `paso` es el `_STEP` fino que ya usa
  `schema_diff.order_diff_items`, no la fase gruesa.
- Dentro de cada objeto: columnas en orden de catálogo; índices, constraints y FKs por nombre.
- Filas: `ORDER BY` de la PK.
- **Tablas sin PK**: se ordena por la tupla completa de columnas si todas son ordenables; si no
  (BLOB/JSON/tipos no comparables), se emite **sin orden garantizado**, se marca
  `deterministic: false` en el manifiesto para ese objeto y se avisa en el preview. Es la
  degradación honesta; fingir determinismo ahí sería mentir.
- Los metadatos volátiles (fecha, id de job) viven en el **manifiesto**, no en el script, para
  que el artefacto sea comparable sin recortarle el encabezado.

Esto es lo que habilita versionar el esquema en un repositorio y **diffear dos volcados**
(§16.3/§16.4), y lo que hace posibles las pruebas de regresión.

### 8.4 Orden de emisión (§10.1)

```
1  preámbulo de sesión                      (charset, FK checks off, zona horaria, sql_mode)
2  DROP/CREATE del contenedor + fijar contexto
3  tipos definidos por el usuario, extensiones, secuencias
4  tablas (estructura)                      [sin índices ni FKs si constraints_placement=deferred]
5  datos
6  índices, UNIQUE, CHECK y FKs             (si deferred)
7  rutinas                                  ← ANTES que las vistas, ver nota
8  vistas                                   (topológico vista→vista)
9  vistas materializadas (+ REFRESH)
10 triggers
11 eventos
12 ajuste de contadores y secuencias
13 epílogo: RESTAURA todo lo que tocó el preámbulo
```

> **Corrección al diseño original (F2)**: la primera versión de esta lista ponía las vistas
> antes que las rutinas. Es **incorrecto** y el orden bueno es el que ya tienen
> `schema_diff._BODY_TYPE_ORDER` y `snapshot_layout._CLASS_ORDER`: en PostgreSQL una vista que
> llama a una función se **valida al crearse**, así que la función tiene que existir antes.
> Ese orden se corrigió una vez por un fallo real; mantener dos órdenes distintos para lo mismo
> es cómo se reintroduce el bug. **Una sola fuente: la del repo.**

Los ciclos de dependencia se **detectan y reportan en la validación en seco**, no se descubren a
mitad de un artefacto que falla. Un script que deja la sesión con `FOREIGN_KEY_CHECKS=0` es un
fallo grave: el epílogo es obligatorio cuando hay preámbulo.

---

## 9. Seguridad

### 9.1 Autorización: hay que decirlo con todas las letras (§13)

**El gateway no tiene autorización por objeto.** Hay una sola identidad —un admin único sembrado
en el `lifespan`— y `AdminDep` es el único guard de todo el proyecto. `owner_id` de
`ManagedDatabase` **no es un principal de acceso**: es un FK a `server_users`, o sea una cuenta
del **motor**. `is_superuser` se escribe y nunca se lee.

De los dos escenarios que plantea §13, aplica el segundo: **cuenta de servicio compartida con
privilegios amplios** (pseudo-root). El motor **no protege nada**. La diferencia con el caso
peligroso que describe el prompt es que **no hay vector de escalada entre usuarios porque no hay
usuarios**: quien tiene sesión de admin ya puede dropear cualquier BD del inventario por otros
endpoints. La exportación no amplía la superficie de autorización; amplía la de **extracción**.

**Este módulo no inventa un modelo de autorización que el proyecto no tiene.** Los controles
compensatorios son los que el proyecto ya usa en su lugar, y acá se aplican todos:

1. auditoría **fail-closed** en el momento de la divulgación,
2. doble confirmación (`confirm_target_name` + `confirm_token`),
3. rate limiting y cuotas,
4. TTL corto y borrado garantizado del artefacto.

Si en el futuro aparece un modelo multiusuario, el punto de enganche es una sola función
(`_authorize_objects(admin, scope, objects)`) que hoy devuelve todo. Queda señalizada.

### 9.2 Inyección

- Identificadores: **siempre** del catálogo del motor, delimitados por
  `identifiers.quote_identifier`. Nunca concatenación de entrada del cliente.
- Patrones de inclusión/exclusión: `fnmatch` **contra nombres del catálogo**. No llegan a SQL.
- `exclude_gateway_internal_tables` **obligatorio** en todos los caminos que enumeran tablas —
  es el fix del incidente de producción de `_gw_v_*`.
- `ensure_not_reserved_database` sobre el ámbito.
- **`where` por objeto — el punto más delicado**: es entrada arbitraria del usuario que termina
  dentro de una consulta. Se ofrece, pero validado con la maquinaria que ya existe:
  1. se arma `SELECT <cols> FROM <t> WHERE <expr>`;
  2. se parsea **entero** con sqlglot y se clasifica con `query_policy.classify_statement`:
     debe dar `read`, con el criterio fail-closed que ya tiene (SQL ilegible, nodo no mapeado o
     `exp.Command` ⇒ peligroso);
  3. el **conjunto de tablas del AST resultante debe ser exactamente `{t}`** — mismo criterio
     que `query_runner.estimate_impact` usa para descartar un COUNT que no corresponde. Esto
     corta subconsultas a otras tablas, CTEs y `information_schema`;
  4. cualquier fallo ⇒ **422 antes de tocar el motor**, nombrando el objeto.
- `filename_template`: sustituciones de una **whitelist** (`{database}`, `{object}`, `{date}`,
  `{time}`, `{job_id}`). El nombre final lo construye y sanea el servidor
  (`_sanitize_filename`); el cliente **nunca** recibe ni envía una ruta.

### 9.3 El artefacto es un objeto sensible en reposo

- Directorio `EXPORT_ARTIFACT_DIR` (default `/app/exports`, volumen nombrado propio; el compose
  ya monta `uploads_data:/app/uploads` y corre como `appuser`, así que el patrón está
  disponible), creado con modo `0700`.
- Nombre de archivo = `secrets.token_urlsafe(32)`. El cliente solo maneja el **id del job**.
- `sha256` + tamaño persistidos en `export_artifacts`.
- Verificación de que quien descarga es quien exportó: con un solo admin es trivial, pero el
  chequeo se escribe igual (`job.created_by_admin_id == admin["id"]`) para que el día que haya
  multiusuario no sea un agujero.

### 9.4 Auditoría (§13, obligatoria e inmutable)

| Acción | Cuándo | Tipo |
|---|---|---|
| `database_export.plan` | al crear el plan | `record`, **`touched_engine=True`** |
| `database_export.execute` | **antes** de encolar | `record_intent` fail-closed, `touched_engine=True` |
| `database_export.execute` | al terminar | `record` agregado (objetos, filas, bytes, resultado) |
| `database_export.download` | **antes** de abrir el archivo | `record_intent` fail-closed, `touched_engine=False` |
| `database_export.purge` | al purgar | `record` |

> **Corrección al diseño original (F2)**: `plan` lleva `touched_engine=True`, no `False`. El
> flag significa "esta operación **contactó** el motor", no "lo mutó" — y el plan snapshotea la
> estructura en vivo. Por eso `clone.plan` usa `True` y el `export` de schema-comparisons usa
> `False` (ese solo lee filas ya persistidas, nunca abre conexión).

El `download` es el punto crítico y por eso replica el patrón de `reveal_password`: **se audita
la intención antes de que un solo byte salga**; si la auditoría no persiste, aborta y el
artefacto no se entrega. Una exportación de datos **es una divulgación**, no una lectura más.

`detail` es un resumen corto: nunca credenciales, nunca filas.

### 9.5 R4 — nunca `str(exc)` del motor

Errores de driver → `map_driver_error`. El detalle crudo va a `logger.exception` con el Request
ID; la respuesta lleva un motivo acotado. **Los errores de la fase de datos siguen la regla más
estricta del clon**: el mensaje del driver puede incrustar valores de filas
(`Duplicate entry 'alice@x.com'`), así que **no se persiste en el ítem** — solo al logger.

### 9.6 Riesgo aceptado explícito: sin enmascarado (§19.8)

El enmascarado por columna queda **fuera de alcance**. Consecuencia registrada, no omitida:
**este módulo permite extraer datos personales o regulados en claro, y no ofrece ningún control
técnico para evitarlo.** El único control compensatorio en pie es la auditoría de §9.4, que por
eso no es negociable. Si el sistema llega a tratar datos regulados, esto pasa a ser un
bloqueante de cumplimiento y hay que implementar §13 (anular / truncar / valor fijo / derivado
determinista) antes de usar el módulo contra producción.

### 9.7 Límites

- Rate limit: `3/minute` en `execute` y `download`, `10/minute` en `create`/`objects`/`preview`.
- `EXPORT_MAX_CONCURRENT_GLOBAL` y `EXPORT_MAX_WORKERS=1`: una exportación es cara para el
  origen; sin techo es un vector de degradación y de denegación de servicio.
- `EXPORT_ARTIFACT_MAX_BYTES` + chequeo de espacio libre (`EXPORT_DISK_MIN_FREE_BYTES`) **antes**
  de arrancar, usando la estimación del preview. Llenar el disco del gateway es una caída total.
- Alerta ante exportaciones anómalamente grandes o frecuentes: señal técnica **y** posible
  indicio de exfiltración.

---

## 10. Entrega y persistencia del artefacto (§8.3, §8.4)

### 10.1 La tensión de §8.4, resuelta explícitamente

"El artefacto no se conserva" (§19.7) y "siempre asíncrona" (§19.5) se contradicen si no se
decide: un job asíncrono termina en un momento distinto de aquel en que el cliente recoge el
resultado, así que el artefacto **existe en algún lado durante ese intervalo**.

**Se elige almacenamiento efímero**, y se descarta la transmisión directa en flujo porque es
**incompatible con progreso, cancelación limpia y reintento** —los tres exigidos por §19.5— y
obligaría a sostener una conexión HTTP larga además de la transacción contra el origen.

Reglas, para que "no se conserva" no degenere en un temporal olvidado en disco:

- `EXPORT_ARTIFACT_TTL_MINUTES` (default **30**), contado desde que el job termina.
- **Descarga de un solo uso** por default (`EXPORT_SINGLE_USE_DOWNLOAD=True`): al completarse la
  descarga, el archivo se borra y el job pasa a `artifact_state='consumed'`.
- **Purga garantizada**: tarea `asyncio` periódica en el `lifespan`
  (`EXPORT_PURGE_INTERVAL_MINUTES`, default 10), copiando el patrón exacto de
  `_purge_captures_periodically` — `asyncio.to_thread` porque el borrado es I/O síncrono y en el
  event loop bloquearía todas las requests, `cancel()` + `await` en el apagado, un fallo de una
  pasada no mata el bucle.
- **Barrido de huérfanos al arrancar**: archivos en el directorio sin fila viva se borran. Sin
  esto, un `kill -9` deja artefactos sensibles en disco para siempre.
- Nada de esto existe hoy en el proyecto: sería **la primera purga de artefactos**, y también
  la primera purga de una tabla de jobs.

### 10.2 Los dos modos de entrega

**`file`** — `GET /database-exports/{id}/download`. `StreamingResponse` con lectura por chunks,
`Content-Disposition`, `Content-Length` y `ETag` = sha256. **Descarga reanudable** vía `Range`.
> *A verificar en la implementación*: la versión de Starlette pinneada debe soportar `Range` en
> `FileResponse`; si no, hay que implementarlo a mano. No se da por hecho.

**`inline`** — `GET /database-exports/{id}/content`, `text/plain` **sin envolver** (nada de
anidarlo en `ApiResponse`: el cliente lo copia al portapapeles tal cual). Requisitos propios:

- tope `EXPORT_INLINE_MAX_BYTES` (default 1 MB), **publicado en capabilities**;
- al excederlo: **409 accionable** con el tamaño estimado y la indicación de usar modo archivo.
  **Nunca se trunca en silencio** — un script truncado que el usuario pega y ejecuta es peor que
  un fallo;
- el `preview` ya devuelve `inline_delivery_viable` + `estimated_bytes`, así que el cliente sabe
  **antes** de lanzar el job si el modo es viable;
- **solo** para `organization=single`, sin compresión y sin split. Figura así en la matriz.

### 10.3 Organización, split y compresión

`single` | `per_object` (un archivo por objeto — la que permite versionar el esquema),
`split_max_bytes` con convención de nombres `{base}.part{NN}.sql` y orden de ejecución
documentado, y `gzip` (single) / `zip` (multiarchivo). Multiarchivo siempre se entrega dentro de
un contenedor.

### 10.4 Manifiesto e inventario (§8.2)

`GET /database-exports/{id}/manifest` (con `ApiResponse`): checksum, tamaño, objetos exportados
con filas y bytes por objeto, opciones aplicadas, versión del generador, motor y versión del
origen, `structure_drift_detected`, `deterministic` por objeto, y el **reporte de incidencias**
(§14). Permite verificar integridad y auditar **sin abrir el archivo**.

---

## 11. Contratos de API

Todos bajo `admin: AdminDep`. Todos devuelven `ApiResponse[T]` **salvo** `download` y `content`,
que son descargas y lo dicen en su docstring (precedente: el `export` de schema-comparisons).

| # | Método y ruta | Límite | Entrada → Salida |
|---|---|---|---|
| 1 | `GET /servers/{sid}/databases/{db}/export-capabilities` | 30/min | → `ExportCapabilitiesOut` |
| 2 | `POST /servers/{sid}/databases/{db}/database-exports` | 10/min | `ExportCreate` → `ExportSummaryOut` (**201**) |
| 3 | `GET /database-exports/{id}/objects` | 10/min | `PaginationDep` + `type`/`name_like` → `ExportCatalogOut` |
| 4 | `POST /database-exports/{id}/resolve-selection` | 10/min | `ExportResolveIn` → `ExportClosureOut` |
| 5 | `POST /database-exports/{id}/preview` | 10/min | `ExportPreviewIn` → `ExportPreviewOut` |
| 6 | `POST /database-exports/{id}/execute` | 3/min | `ExportExecuteIn` → `ExportSummaryOut` |
| 7 | `GET /database-exports/{id}` | — | → `ExportSummaryOut` (**polling**) |
| 8 | `GET /database-exports/{id}/items` | — | `PaginationDep` → `list[ExportItemOut]` |
| 9 | `POST /database-exports/{id}/cancel` | — | → `ExportSummaryOut` |
| 10 | `GET /database-exports/{id}/manifest` | — | → `ExportManifestOut` |
| 11 | `GET /database-exports/{id}/download` | 3/min | → `StreamingResponse` (sin `ApiResponse`) |
| 12 | `GET /database-exports/{id}/content` | 3/min | → `text/plain` (sin `ApiResponse`) |

### 11.1 Capacidades (§2.3.1) — el endpoint que evita que el cliente adivine

```jsonc
{
  "engine": "mysql", "engine_version": "8.0.36",
  "scope": { "kind": "database", "name": "tienda", "scope_note": null },
  "object_types": ["table","view","routine","trigger","event"],
  "formats": [
    { "name": "sql",   "supports_structure": true,  "supports_data": true },
    { "name": "csv",   "supports_structure": false, "supports_data": true, "one_file_per_table": true },
    { "name": "json",  "supports_structure": "manifest_only", "supports_data": true },
    { "name": "ndjson","supports_structure": "manifest_only", "supports_data": true }
  ],
  "options": {
    "structure.scope_ddl":  { "values": ["NONE","CREATE","DROP_CREATE","CREATE_IF_NOT_EXISTS"],
                              "default": "NONE", "destructive": ["DROP_CREATE"] },
    "sanitize.definer":     { "values": ["keep","omit","replace"], "default": "omit",
                              "applicable": true },
    "data.insert_variant":  { "values": ["none","insert","insert_ignore","replace","upsert"],
                              "default": "insert" }
    // …
  },
  "compatibility": [
    { "when": {"format":"csv"}, "forbids": ["structure.*","data.insert_variant",
        "sanitize.session_preamble","output.organization=single"],
      "reason": "El formato delimitado solo transporta datos, un archivo por tabla." },
    { "when": {"output.delivery":"inline"},
      "forbids": ["output.organization=per_object","output.split_max_bytes","output.compression"],
      "reason": "La entrega en línea solo admite un artefacto único sin comprimir." }
  ],
  "limits": { "inline_max_bytes": 1048576, "max_statement_bytes": 1048576,
              "artifact_ttl_minutes": 30, "max_duration_seconds": 14400 },
  "charset_collation_catalog_url": "/api/v1/charset-collation-options?family=mysql"
}
```

La matriz **se publica y además se hace cumplir en el servidor** (§17). Publicarla sin
validarla sería peor que no publicarla.

### 11.2 Errores estructurados y accionables (§2.3.5)

Código estable + opción culpable + mensaje legible, en `public_context` —**no** en `context`,
que solo se ve en `development`; en producción el operador recibiría "hay una opción inválida"
sin saber cuál. Este es el mismo error que ya se corrigió en `apply-profile`.

```jsonc
{ "detail": { "msg": "El formato 'csv' no admite sentencias de estructura.",
              "type": "AppHttpException",
              "public_context": { "code": "export.incompatible_option",
                                  "field": "structure.entity_ddl",
                                  "format": "csv",
                                  "allowed": ["NONE"] } } }
```

Códigos: `export.incompatible_option`, `export.data_without_structure`,
`export.missing_dependencies`, `export.invalid_row_filter`, `export.inline_too_large`,
`export.fingerprint_changed`, `export.artifact_expired`, `export.artifact_consumed`,
`export.quota_exceeded`.

### 11.3 Catálogos auxiliares (§2.3.6)

Charsets y collations **ya existen** (`/charset-collation-options`, por familia de motor) y
`charset_catalog.resolve_enabled_combination` ya devuelve los valores canónicos. **Se reutiliza,
no se duplica** — es conocimiento de dialecto, exactamente el tipo de reutilización que §11 del
prompt considera legítima. Formatos, variantes de `INSERT` y valores de cada enumerado salen de
capabilities.

---

## 12. Modelo de datos

### `export_jobs`
`id`, `server_id` (FK, CASCADE, idx), `database_name`, `database_id` (FK `managed_databases`,
SET NULL), `engine`, `spec` (Text/JSON — el `ExportSpec` completo), `resolved_selection`
(Text/JSON, se llena en el preview), `source_fingerprint`, `confirm_token`, `expires_at`,
`status` (idx), `phase`, `progress` (JSON), `error`, `cancel_requested`,
`created_by_admin_id`, `idempotency_key` (unique, nullable), `started_at`, `finished_at`,
`structure_drift_detected`, + `TimestampMixin`.

### `export_job_items`
`id`, `job_id` (FK CASCADE, idx), `seq`, `object_type`, `object_name`, `phase`,
`status` (`ok` | `error` | `skipped`), `reason` (motivo acotado — **nunca** `str(exc)`),
`rows_exported`, `bytes_written`, `deterministic`, `execution_ms`, `executed_at`.

> **Decisión sobre el vocabulario de ítems**: el proyecto ya tiene dos divergentes
> (`applied/failed/skipped` en el clon, `ok/error/skipped` en collation). Se adopta el
> **segundo**: acá no se "aplica" nada, se lee. Vale la pena dejar anotado que la divergencia es
> deuda y que un cuarto módulo debería ser el disparador de unificarla.

### `export_artifacts`
`id`, `job_id` (FK CASCADE, unique), `storage_name` (token opaco), `byte_size`, `sha256`,
`content_type`, `part_count`, `state` (`available` | `consumed` | `purged`), `expires_at`,
`downloaded_at`, `download_count`.

---

## 13. Plan de pruebas (§17)

Siguiendo la política del proyecto: se escriben con `pytest` pero **no se ejecutan salvo pedido
explícito**; la verificación intermedia es lectura del diff, `ast.parse` y ejecución directa de
las funciones puras.

**Unitarias puras** (`export_spec.py`, `export_writer.py`, `sql_literals.py` — sin motor):
1. **Regla de dependencia**: `DROP_CREATE` implica creación en ambos niveles, y el estado
   inválido **no es representable** (test de tipo, no de validación).
2. Selección: listas explícitas, exclusiones, patrones, objetos inexistentes, política de datos
   por objeto, subconjunto datos ⊆ estructura y la excepción "solo datos".
3. Saneamiento: suprimir comentarios **no deja ninguno**; `definer` produce las tres variantes;
   `autoincrement: omit` lo saca de la definición **y** no emite `setval`; las opciones de motor
   desaparecen con `engine_specific_options: false`.
4. **Determinismo**: dos corridas idénticas → artefactos byte a byte idénticos; tabla sin PK →
   `deterministic: false` y aviso.
5. Valores límite: nulos vs cadena vacía, binarios, comillas, saltos de línea, multibyte, fechas
   extremas, `Decimal` de precisión arbitraria, `timedelta`, columnas generadas.
6. Matriz de compatibilidad: cada combinación prohibida devuelve el error accionable correcto.

**De API** (`tests/test_api_database_exports.py`, `admin_client` + `_FakeAdapter` +
`monkeypatch.setattr(ec, "get_adapter", …)` + runner síncrono, patrón de
`test_api_database_clones.py`): los 12 endpoints, 401 sin sesión, 409/410/422 de cada guard,
`confirm_token` cruzado, fingerprint cambiado, inline sobre el tope, descarga consumida.

**Seguridad**: inyección por nombre de objeto, por patrón, por `where` y por
`filename_template`; recorrido de rutas; descarga de un artefacto ajeno/purgado; enumeración de
catálogo.

**Escala**: tabla de millones de filas y tabla con campos muy grandes → **consumo de memoria
plano** (medido) y cancelación que **libera de verdad** la conexión y la transacción del origen.

**Prueba de aceptación principal** (`scripts/verify_export_e2e.py`, requiere Docker —
misma forma que los cuatro `verify_*_e2e.py` que ya existen): generar el artefacto, **ejecutarlo
contra una instancia limpia** y **comparar el esquema resultante contra el origen** con
`diff_snapshots` (que ya está y es puro). Debe cubrir FKs cruzadas, vistas sobre vistas, rutinas
con `;` en el cuerpo, triggers y columnas generadas, en MySQL 8, MariaDB 11 y PostgreSQL 16.
**Cualquier otra prueba es secundaria frente a esta.**

---

## 14. Errores, resultados parciales y observabilidad

- `on_error`: `stop` (corta al primer fallo) | `continue` (best-effort, reporta).
- **Nunca se entrega un artefacto parcial sin marca inequívoca**: si el job no terminó `ok`, el
  archivo lleva un banner al final (`-- EXPORTACIÓN INCOMPLETA — ver el reporte de incidencias
  del job N`), el manifiesto trae `complete: false`, y la descarga responde con la cabecera
  `X-Export-Complete: false`.
- **Reporte de incidencias por objeto** al finalizar: qué se omitió y por qué (sin permiso, tipo
  no soportado, definición ilegible, opción no aplicable, tipo de valor no soportado).
- Encabezado de metadatos (sujeto a `script_comments`): fecha, motor y versión del origen,
  ámbito, opciones aplicadas, versión del generador, id de job. Vale su peso en oro seis meses
  después.
- Logging estructurado con Request ID: inicio, fin, duración, objetos, filas, bytes, motivo de
  fallo. **Nunca datos exportados en los logs.**
- Métricas: exportaciones por estado, duración por percentiles, tamaño de artefacto, tasa de
  cancelación, concurrencia, tiempo de consulta al origen, ocupación del directorio de spool.

---

## 15. Extensiones sugeridas (§16): qué entra y qué no

| # | Extensión | Decisión |
|---|---|---|
| 1 | Plantillas / preajustes guardados | **Fuera** (§19.10), pero `ExportSpec` ya es serializable y autosuficiente |
| 2 | Exportaciones programadas | **Fuera** (§19.10) |
| 3 | Solo esquema apto para control de versiones | **DENTRO**: es `per_object` + determinismo + `script_comments:false`. Costo marginal cero |
| 4 | Comparación entre dos exportaciones | **Fuera del módulo**, pero habilitada por el determinismo; `diff_snapshots` ya existe |
| 5 | Muestreo con cierre referencial | **FUERA de v1, declarado**. Alto valor pero exige recorrer el grafo de FKs por fila; es un módulo en sí mismo |
| 6 | Enmascarado por columna | **FUERA** — riesgo aceptado explícito (§9.6) |
| 7 | Estimación previa | **DENTRO** — está en el `preview` |
| 8 | Exportación a destino remoto | **Fuera de v1**; el punto de enganche es `export_storage` |
| 9 | Matviews, comentarios de objeto, CHECKs, collations personalizadas | **DENTRO** — son justamente los que se olvidan |
| 10 | Modo "solo advertencias" | **DENTRO** — es `preview` con `dry_run_only=true` |

---

## 16. Riesgos y bloqueantes a validar antes de implementar

1. **🔴 Estructura consistente en MySQL/MariaDB es imposible** sin bloquear el servidor entero
   (§6.2). Es un límite del motor, no del diseño. **Requiere tu confirmación de que se acepta**
   la garantía asimétrica (datos sí, estructura con aviso).
2. **🟠 `idle_in_transaction_session_timeout` está atado al `statement_timeout`** en
   `remote_engine`. Hay que desacoplarlo o un export largo en PG queda expuesto.
   > **Resuelto en F3, sin tocar `remote_engine`**: `export_session` lo desacopla con un
   > `SET` a nivel de SESIÓN sobre su propia conexión (`EXPORT_IDLE_TRANSACTION_TIMEOUT_MS`,
   > default 5 min), que tiene precedencia sobre el `-c` de la URL. Cambiar los
   > `connect_args` habría metido otro eje en la clave del cache de engines y habría afectado
   > a los otros cinco consumidores para resolver el problema de uno solo. Es best-effort: si
   > el motor rechaza el `SET`, se anota en `degradations` y se sigue.
3. **🟠 Inyección de conexión en `ServerAdapter`** (§6.4): aditiva y compatible, pero toca código
   compartido por 5 features. Es el cambio más invasivo del módulo.
   > **Hecho en F3.** `_conn_ctx(database, conn)` en `base_adapter` + parámetro keyword-only
   > `conn: Connection | None = None` en `structural_snapshot`, `dump_structure`,
   > `list_tables` y `list_table_stats`. Con `conn` dado **no se cierra la conexión ni se
   > toca su nivel de aislamiento** (hacerlo revertiría la transacción REPEATABLE READ del
   > job). Los hooks `_snapshot_*`/`_column_extras`/`_database_defaults`/
   > `_table_storage_options` **no cambiaron**: ya recibían `conn` como primer parámetro.
   > Los cinco consumidores llaman posicionalmente con un solo argumento y siguen intactos,
   > verificado con un test que compara el resultado con y sin `conn` sobre SQLite real.
4. **🟠 Volumen de disco para el spool**: los compose montan `uploads_data:/app/uploads`; hace
   falta un volumen propio `exports_data:/app/exports` y una decisión de tamaño. Sin cuota, una
   exportación grande puede llenar el disco del gateway.
5. **🟡 `Range` en la descarga reanudable**: verificar soporte en la versión de Starlette
   pinneada antes de prometerlo en capabilities.
6. **🟡 Tercera copia del runner**: consistente con el precedente explícito del proyecto, pero es
   el momento razonable para extraer la abstracción compartida. Se recomienda **copiar ahora** y
   anotar la deuda: unificar tres runners y dos vocabularios de ítem es un refactor propio.
7. **🟡 Sin enmascarado** (§9.6): riesgo aceptado, con la auditoría como único control en pie.

### 16.1 Revisión de seguridad post-implementación (2026-08-16)

`gateway-security` revisó F1–F6 y emitió **3 bloqueantes + 5 recomendaciones + 1 fuga**.
Todos **aplicados**, cada uno con su test de regresión (falla sin el fix, verificado).

| # | Hallazgo | Estado |
|---|---|---|
| **B1a** | `query_policy._scan_normalize` reconocía `/*!` pero **no `/*M!`**, el comentario ejecutable de MariaDB: su contenido se descartaba como comentario común, la blocklist nunca lo veía y el motor lo ejecutaba igual. **Afectaba a la consola SQL en producción**, no solo al export — con la credencial pseudo-root, un `/*M!100000 INTO OUTFILE …*/` es escritura de archivo arbitraria en el host del cliente. | ✅ `_EXECUTABLE_COMMENT_PREFIXES`; se reconoce en los 3 motores (fail-closed: un MariaDB dado de alta como `mysql` es un error de inventario frecuente) |
| **B1b** | `export_spec.validate_row_filter` se apoyaba en sqlglot, que **no tokeniza el contenido de `/*! */`**: pasaban `1=1 /*!50000 UNION SELECT user,authentication_string FROM mysql.user */` y `1=1 /*M!100000 INTO OUTFILE …*/`. | ✅ se rechaza **cualquier** token de comentario en el filtro (`--`, `/*`, `*/`, y `#` solo en la familia MySQL — en PG es el XOR de enteros) |
| **B2** | La cadena **validada** no era la **ejecutada**: el validador armaba un prefijo y `export_writer._select_sql` le pegaba `ORDER BY`/`LIMIT` detrás. Con `where = "1=1 -- "` la cola quedaba comentada → tabla entera, sin orden, ignorando el `limit` que el `confirm_token` hasheó, con el manifiesto afirmando `deterministic: true`. | ✅ constructor **único** `export_spec.build_row_select_sql` (lo llaman validador y writer), filtro entre paréntesis y validación de la cadena FINAL |
| **B3** | Nada impedía apuntar el export a la **propia BD de metadatos del gateway** si su servidor está en el inventario: el artefacto se llevaría `servers` (con `root_password_encrypted`), `server_users`, el `audit_log` completo —el único control compensatorio del §9.6— y `migration_select_results`. | ✅ `_validate_scope` llama `query_policy.is_gateway_metadata_target` (resuelve host a IP, no compara texto) → **409 `export.scope_not_allowed`** |
| **Fuga F6** | El export **no re-calificaba el esquema en los cuerpos**: en MySQL/MariaDB `VIEW_DEFINITION` trae la BD de origen calificada, así que restaurar el artefacto en una base con otro nombre dejaba las vistas leyendo de la base de **ORIGEN**. | ✅ como el destino es desconocido, se **quita** el calificador propio con `sql_dialect.strip_self_schema_qualifier` (`export_writer._strip_own_schema`, solo tipos con cuerpo). Una referencia a OTRA base se conserva |
| **R1** | `Range` anulaba el "un solo uso": bastaba la presencia de la cabecera para no consumir, así que un `Range: bytes=0-` bajaba el archivo entero y lo dejaba disponible. | ✅ `_range_covers_whole_file` (pura, fail-closed hacia no borrar) decide por COBERTURA; `download_count` se incrementa **siempre** |
| **R2** | `preview` sobre un job ya ejecutado reescribía `spec`/`resolved_selection`/`fingerprint`/`token`, y `GET /manifest` dejaba de describir el artefacto entregado. | ✅ `_guard_still_pending` (mismo código `export.already_executed` que ya usaba `execute`, que ahora lo comparte) |
| **R3** | `Connection.execution_options()` muta la conexión **in-place** y esa conexión vive todo el job: `stream_results` quedaba pegado al re-snapshot final de drift y a `counter_value`. Es el fallo documentado en `query_runner`. | ✅ opciones **por sentencia** (`text(sql).execution_options(...)`) |
| **R4** | `output.file_encoding` aceptaba cualquier cadena; como se codifica **por trozo**, `utf-16` incrusta un BOM por `write` y el artefacto sale corrupto con un sha256 que lo declara íntegro. | ✅ whitelist `utf-8`/`utf-8-sig`/`latin-1`/`cp1252` (+ alias) vía la matriz, código `export.incompatible_option` |
| **R5** | `_guard_concurrency` contaba solo `running`: con un worker el techo era 1 y la **cola** quedaba sin acotar. | ✅ `max(running_en_BD, export_runner.inflight_count())`. **No** se cuenta `pending` de la base: incluye los planes creados y nunca ejecutados, que no son trabajo admitido |

**Follow-up (no aplicados)**: **R6** el `confirm_token` no lleva `job_id` en su `subject`;
**R7** `_guard_owner` solo corre en la descarga; **R8** `_render_plan` materializa TODO el DDL
en memoria antes de emitir; **R9** `preview`/`objects` no auditan.

---

## 17. Fases de implementación propuestas

| Fase | Contenido | Entregable verificable | Estado |
|---|---|---|---|
| F1 | `export_spec.py` + `sql_literals.py` + capabilities + matriz | tests puros; endpoint 1 en verde | ✅ 76 + 23 checks |
| F2 | Modelo, migración Alembic, endpoints 2–5 (plan/objects/resolve/preview) | dry-run completo sin ejecutar nada | ✅ |
| F3 | `export_writer.py` (sql), inyección de conexión en el adapter, consistencia | artefacto SQL correcto contra SQLite y MariaDB | ✅ 78 checks — **contra SQLite; MariaDB nunca se probó** |
| F4 | `export_runner.py`, `export_storage.py`, endpoints 6–12, purga en el `lifespan` | ciclo completo con descarga y TTL | ✅ 27 checks con writer y adapter reales, sin motor |
| F5 | Formatos csv / json / ndjson + organización, split, compresión | matriz de compatibilidad exigida en servidor | ✅ 81 checks HTTP |
| F6 | `scripts/verify_export_e2e.py` contra los 3 motores reales | **prueba de aceptación principal** | 🔴 **script escrito, NUNCA EJECUTADO** (sin Docker) + docs y `CLAUDE.md` ✅ |

Cada fase cierra con la documentación correspondiente (`docs/features/database-export.md` y
`docs/api-reference-v10.md`), suficiente para que el equipo de cliente construya la interfaz
**sin leer el código del backend** (§18.5).
