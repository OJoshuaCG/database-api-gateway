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

- Lee del origen con `stream_results=True` + `yield_per` (memoria acotada) y escribe al destino
  con `INSERT` **parametrizado** por lotes (`executemany`) — nunca literales (a prueba de
  inyección). `CLONE_DATA_BATCH_ROWS` (1000) controla el lote.
- Orden **topológico** entre tablas (padre antes que hijo) y **FK checks desactivados** durante
  la fase de datos (`SET FOREIGN_KEY_CHECKS=0` / `session_replication_role='replica'`,
  restaurados en `finally`) para tolerar ciclos.
- Tablas **sin PK**: `INSERT` plano (sin upsert). Con PK y destino preservado: **upsert**
  (`ON DUPLICATE KEY UPDATE` / `ON CONFLICT DO UPDATE`).
- **Los datos NUNCA se traducen cross-engine** por sintaxis, pero los valores escalares se
  adaptan por driver; tipos riesgosos (arrays/enums/JSON/geometría) pueden fallar por tabla y
  se reportan (best-effort por tabla, el resto continúa).

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
