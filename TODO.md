# TODO — Database Gateway Project

> **Espejo detallado de la tarea de ClickUp.** Este archivo es la fuente de verdad del
> **detalle**; ClickUp es la fuente de verdad del **estado** y de **quién** está trabajando.
> El protocolo completo está en `CLAUDE.md`, sección
> "Gestión de tareas (TODO.md + ClickUp)". No se trabaja nada sin pasar por ahí.

## Tarea principal en ClickUp

| Campo | Valor |
| --- | --- |
| **Task ID** | `86e2xzf9d` |
| **URL** | https://app.clickup.com/t/86e2xzf9d |
| **Nombre** | Database Gateway Project |
| **Espacio** | Cero208 (`90172691192`) |
| **Carpeta** | Desarrollo (`901710687203`) |
| **Lista** | Database Gateway (`901716272178`) |
| **Workspace** | `9017559023` |

Estados disponibles en la lista: `to do` · `in progress` · `on hold` · `update required` ·
`reviewed` · `complete`.

### Reglas mínimas (el detalle está en `CLAUDE.md`)

1. **Toda unidad de trabajo es una subtarea de `86e2xzf9d`.** No se crean tareas sueltas.
2. **Cada ítem de este archivo tiene un ID estable y la subtarea se llama con ese ID.** Dos
   esquemas: los ítems del backlog usan **`P-XX — <título>`** (ID ya asignado, sin riesgo); los
   ítems **nuevos** creados al vuelo usan **`T-<YYMMDD>-<iniciales>-<slug>`**. Para algo nuevo
   **nunca** uses "el siguiente `P-XX` libre": es secuencial, dos personas simultáneas calculan
   el mismo, y el prefijo anti-duplicados termina apuntando a dos trabajos distintos.
3. **Después de crear una subtarea, RE-VERIFICÁ que no haya duplicado antes de trabajar.** La
   ventana entre buscar y crear no se puede cerrar (ClickUp no tiene locks), pero sí detectar:
   gana la de `date_created` más antiguo, y si empatan, el `id` menor. Si perdiste la carrera,
   **pará y avisá** — no borres la duplicada por tu cuenta.
4. **El ID de la subtarea se anota en la columna `Subtarea` COMO LINK, en cuanto se crea:**
   `[86e2y1abc](https://app.clickup.com/t/86e2y1abc)`. Un ID pelado obliga a construir la URL a
   mano cada vez; el link es referencia directa de verdad. Así este
   archivo es el índice `P-XX → subtarea` y no hace falta buscar en ClickUp.
5. **Las subtareas se crean al tomarlas**, no por adelantado — así no se llena ClickUp de IDs
   que nadie va a ejecutar. Un ítem con `Subtarea: —` todavía no fue tomado.
6. **Al buscar en ClickUp, `include_closed: true` es obligatorio.** Viene apagado por defecto:
   sin él, una tarea ya terminada no aparece y se crea un duplicado exacto.
7. **Antes de empezar**: validar en ClickUp. Si está `in progress`, se **interrumpe** y se avisa
   quién la tiene. Si está `complete`/`reviewed`, se avisa que ya está hecha.
8. **Al empezar** se pone `in progress` — eso es lo que reserva la tarea, y va antes de escribir
   código, no después.
9. **Al terminar hay una bifurcación obligatoria: ¿necesita frontend?** Si NO, va a `complete`
   directo. Si SÍ, va a **`update required`** con un comentario `HANDOFF FRONTEND` y **no se
   cierra**: la pasa a `complete` el frontend cuando termina. Para decir que NO necesita
   frontend hay que poder afirmar que nada de lo que el frontend ya consume cambió (rutas, forma
   de la respuesta, códigos de error, campos obligatorios). **Ante la duda, `update required`.**
10. **`update required` significa UNA sola cosa: falta el frontend.** Para "quedó a medias" está
   `on hold`. Si se le dan dos significados, el filtro del frontend se llena de ruido y el
   mecanismo pierde el sentido. Y **`reviewed` no se usa** en este flujo.
11. **La identidad es el EMAIL** (`git config user.email`), no el nombre — este repo tiene 4
   nombres distintos para un mismo email. Va escrita en el texto del comentario: todos se publican
   con la cuenta del token de la integración, así que ClickUp por sí solo no distingue quién
   ejecuta.
---

## 🔴 Pendientes

### Verificación contra motores reales (requiere Docker)

Es la deuda más grande del proyecto: hay módulos completos cuya única verificación fue
ejecución directa de funciones y SQLite. Los scripts existen; nunca se corrieron.

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-01 | e2e de exportación de BDs | `scripts/verify_export_e2e.py` **escrito pero NUNCA ejecutado**. Falta confirmar: que el artefacto se ejecute y el esquema resultante coincida vía `diff_snapshots`; que la transacción de `export_session` se abra de verdad (3 pasos de MySQL, `postgresql_readonly` de psycopg); `SET idle_in_transaction_session_timeout`; `export_counter_value_sql` (`pg_sequence_last_value` es un builtin **no documentado**); determinismo byte a byte; reimportar csv/ndjson; valores límite (`\x00`, fechas extremas, `Decimal`, multibyte); que quitar el calificador propio del cuerpo produzca objetos válidos al restaurar con otro nombre de BD. | — |
| P-02 | e2e de la consola SQL | `scripts/verify_query_console_e2e.py` escrito, no ejecutado. Lo crítico: (a) que la tx READ ONLY **rechace de verdad** una escritura mal clasificada — es la garantía central del diseño; (b) `SET ROLE` con RLS en PG; (c) que el `LIMIT` empujado evite bajar la tabla entera; (d) mensajes nativos de rechazo por permisos en los 3 motores. | — |
| P-03 | e2e de la captura de `SELECT` en migraciones | Buffer de PG vs `idle_in_transaction_session_timeout` y marca `rolled_back`; captura inmediata en MySQL/MariaDB sobreviviendo a un fallo posterior; tipos nativos (`JSONB`, `TIME`, `bytea`). | — |
| P-04 | e2e del checkpoint de sentencia | Ciclo apply-parcial → retoma contra los 3 motores reales. Implementado sin Docker disponible. | — |
| P-05 | e2e del ciclo de vida de BDs a nivel servidor | Crear / listar grantees / drop con `force_disconnect`; en PG las consultas `aclexplode` / `datacl` / `pg_terminate_backend`. Verificado hasta hoy solo con `FakeAdapter`. | — |
| P-06 | e2e de usuarios del motor | `add-host` y `copy-grants` contra MySQL/MariaDB reales. | — |
| P-07 | e2e del cruce de familia MySQL↔MariaDB en perfiles de permisos | Validar el filtro por familia y la recanonicalización de tokens contra motores reales. | — |
| P-08 | e2e de clon cross-engine | `scripts/verify_clone_e2e.py` cubre tablas/datos; **MySQL→PostgreSQL pendiente de corrida**. | — |
| P-09 | Re-correr `verify_schema_diff_e2e.py` | Los renderers de redefinición (`_ri_index_modified`, `_ri_unique_modified`, `_ri_check_modified`, `_ri_pk_changed`) solo se verificaron con tests unitarios/SQLite. | — |
| P-10 | Migraciones Alembic nuevas contra la BD del gateway real | `d1e2f3a4b5c6` (statement progress), `e2f3a4b5c6d7` (manifiesto de sentencias), `f8a9b0c1d2e3` (select results), `a9b0c1d2e3f4` (export jobs). Solo ciclo upgrade/downgrade/upgrade en SQLite. | — |
| T-260822-lz-clon-e2e-solo-datos | e2e del clon en modo **solo datos** | Lo que solo un motor real confirma, y de lo que depende la calibración del guard: (a) que `LOAD DATA LOCAL` se **trague** un truncado de string (premisa de que en la familia MySQL esos casos deban BLOQUEAR); (b) el **colapso de filas** por collation en una columna de PK; (c) que `COPY` de PostgreSQL falle **atómico** (premisa de que allá sean solo avisos); (d) `supports_charset_combination` en MySQL 8 / MariaDB 11 / PostgreSQL; (e) el `owner` de PG; (f) que `session_replication_role` falle en silencio sin superusuario (premisa del aviso de triggers en PG). Extender `scripts/verify_clone_e2e.py`. | — |

### Suite de tests

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-11 | Correr la suite `pytest` completa | ~690 tests. Por política del proyecto no se ejecuta automáticamente (I/O lento de WSL2 sobre `/mnt/`). Hay archivos de test **escritos y nunca ejecutados con `pytest`**: `test_migration_results.py`, `test_api_model_migrations.py`, `test_api_permission_profiles.py`, `test_grant_guards.py`, `test_api_database_exports.py`, `test_export_hardening.py`. | — |

### Configuración y entorno

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-12 | Actualizar `.env.example` | No se pudo escribir por permisos del entorno. Faltan las **18 variables `EXPORT_*`** y las **6 `MIGRATION_CAPTURE_*`**. Hoy solo están documentadas en `app/core/environments.py` y en `docs/features/`. | — |
| P-13 | TLS para MySQL (P0 #3 del Plan 08) | Estado 🟡 parcial. | — |
| P-14 | Gestión de secretos (P0 #5 del Plan 08) | Estado 🟡 parcial. | — |
| P-15 | CI inexistente (P1 del Plan 08) | Sin pipeline: ni Ruff, ni tests, ni gate de migraciones, ni escaneo de seguridad. | — |

### Follow-ups de revisiones de seguridad (no aplicados, decididos como no bloqueantes)

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-16 | Export **R6** | El `confirm_token` no incluye `job_id` en su `subject`. | — |
| P-17 | Export **R7** | `_guard_owner` solo corre en la descarga. | — |
| P-18 | Export **R8** | `_render_plan` materializa **todo** el DDL en memoria antes de emitir. | — |
| P-19 | Export **R9** | `preview` y `objects` no auditan. | — |
| P-20 | Usuarios del motor **R2** | Falta rate-limit en `reveal-password`. | — |
| P-21 | Usuarios del motor **R3** | No se audita cada grant copiado individualmente en `copy_grants`. | — |
| P-22 | Usuarios del motor **R5** | La whitelist estricta rechaza usernames legacy (dígito inicial, `.`, `-`, `$`). | — |

### Deuda técnica y huecos conocidos

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-23 | Unificar el worker in-process | Van **3 copias** (`clone_runner`, collation, `export_runner`) y **2 vocabularios de ítem divergentes**: `applied/failed/skipped` (clon) vs `ok/error/skipped` (collation y export). El 4º módulo de la familia debería unificarlos. | — |
| P-24 | Cola durable de jobs | El worker es in-process: clones y exports **no sobreviven un reinicio** (quedan `interrupted`). | — |
| P-25 | `snapshot_layout`: baseline no reconciliable | `_persist_snapshot_versions` no escribe manifiesto (un baseline que falla a mitad no se puede reconciliar) y `filter_statements` no valida cierre de dependencias (excluir un `type` deja un baseline inaplicable **en silencio**). | — |
| P-26 | `create_from_snapshot` sin manifiesto | Solo `adopt` de schema-comparisons escribe el manifiesto de sentencias. | — |
| P-27 | Auditar `downgrade()` rotos en migraciones anteriores | Se encontró y corrigió uno (soltaba un índice FK-backed antes de `drop_table`, que MySQL/MariaDB rechaza). **El mismo patrón está presente en migraciones anteriores del repo y no se corrigió** (quedó fuera de alcance en su momento). | — |
| P-28 | Particiones no viajan en el export | El `SchemaSnapshot` no captura `PARTITION BY` → hoy solo se emite un aviso. | — |
| P-29 | Export: `REFRESH MATERIALIZED VIEW` no se emite | Las matviews salen vacías en el destino. | — |
| P-30 | Export: artefacto de PG con `DROP_CREATE` no es ejecutable de un tirón | Falta el `\connect` que emite `pg_dump --create`. Hoy se avisa en el preview. | — |
| T-260822-lz-clon-truncate-datos | Clon: `data.on_existing='truncate'` (Fase 2) | Vaciar las tablas del destino antes de copiar, que es lo que pide el caso de uso real ("bajar producción a staging"). Requiere: **cierre FK del destino** (un `TRUNCATE` aislado sobre una tabla referenciada falla en PG aunque los triggers estén en `replica` — BUG #15657 abierto; y en PG hay que listar todas las tablas en UNA sentencia), `confirm_row_loss` propio (hoy el gesto para vaciar 3 tablas es idéntico al de resetear la base), semántica de cancelación (cancelar entre el vaciado y la copia hoy reporta `canceled` **sin cuarentena**, con el destino vacío) y verificación contra motores reales: que `FOREIGN_KEY_CHECKS=0` habilite `TRUNCATE` sobre una tabla referenciada es **indocumentado** en MySQL. `CASCADE` no puede ser el atajo (vaciaría tablas fuera de la selección). Alternativa portable a evaluar: `DELETE FROM` en orden hijo→padre. | — |
| T-260822-lz-clon-capabilities-frontend | Clon: endpoint de capacidades + matriz publicada, y el frontend (Fase 2) | Lo que evita que el formulario **reimplemente** las reglas cruzadas del servidor y divergan. El export ya publica y hace cumplir con la misma estructura (`ExportCapabilitiesOut`, `compatibility_matrix()`), y el frontend tiene un motor genérico que las consume (`src/features/database-exports/logic.ts`). Incluye lo que rompe hoy en el wizard: `WizardNav` bloquea el avance con selección de estructura vacía (que en solo-datos es la definición del modo), "replanear" rehidrata leyendo solo `include_data`/`clean_mode`/`target_mode` (un job de solo datos vuelve como `CREATE` — el fallo que la feature arregla, y ese camino se recorre justo después de un `failed`), y `warnings: list[str]` se renderiza en gris `text-xs`. | — |
| T-260822-lz-clon-owner-set-role | Clon: el `owner` de PostgreSQL no es real | `CREATE DATABASE … OWNER x` fija el dueño **de la base**; todos los objetos los crea la conexión pseudo-root, así que el dueño pedido **no puede `ALTER`/`DROP` sus propias tablas** — peor que no pasar `owner`. Requiere `SET ROLE` para las fases de DDL y datos (o `REASSIGN OWNED`/`ALTER … OWNER TO` al cerrar) y validar la membresía del pseudo-root en el rol, o el `CREATE DATABASE` falla con 42501 **dentro del worker** (y con `clean_mode='drop_database'`, después del DROP). | — |
| T-260822-lz-pg-resync-serial | El resync de secuencias de PG no cubre `serial` ni cross-engine | `_resync_postgres_identity_sequences` (`clone_controller.py:740-800`) solo actúa sobre columnas con `col.identity is not None` **del snapshot del ORIGEN**. Una columna `serial` tiene `identity=None` y default `nextval(...)` (por eso existe `PostgresAdapter._serial_type`), y un origen MySQL con `AUTO_INCREMENT` tampoco tiene `identity` ⇒ **la secuencia del destino nunca se resincroniza** y el primer `INSERT` de la aplicación choca la PK (23505). Defecto **preexistente** que el modo solo datos vuelve el camino principal. Fix: elegir las columnas desde el **destino** con `pg_get_serial_sequence(t, c) IS NOT NULL`, que cubre `serial` **e** `identity` y es la función que el código ya usa. | — |

### Riesgo de cumplimiento (aceptado explícitamente, anotado para revisión)

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-31 | El export **no tiene enmascarado de datos** | Permite extraer datos personales o regulados **en claro**, sin ningún control técnico que lo evite. El único control compensatorio es la auditoría (por eso no es negociable). `EXPORT_ENABLED=False` es el kill switch. **Si el gateway pasa a tratar datos regulados, esto es un bloqueante de cumplimiento.** | — |

---

## 🟡 En curso

_Nada en curso._

> Cuando alguien toma un ítem, se mueve acá con: subtarea de ClickUp, ejecutor, fecha de
> inicio y qué está tocando. La verdad sobre "quién lo tiene" la manda **ClickUp**, no este
> archivo — este archivo puede quedar viejo si alguien no lo actualiza, ClickUp no.

| Ítem | Subtarea ClickUp | Ejecutor | Inicio | Qué está tocando |
| --- | --- | --- | --- | --- |
| — | — | — | — | — |

---

## 🔵 Pendiente de frontend

Backend **terminado**, esperando implementación visual. En ClickUp estas tareas están en
**`update required`** — es el estado que el equipo de frontend filtra para saber qué le toca, sin
crear tareas nuevas ni salir a buscar contexto.

Cada una tiene en ClickUp un comentario **`HANDOFF FRONTEND`** con los endpoints nuevos o
cambiados, si hay breaking changes, los schemas afectados, y la referencia al contrato
(`docs/api-reference-vN.md` + hash del commit). **La tarea la cierra el frontend**, no el backend.

**Al tomar una, el frontend la pasa a `in progress`** con un `INICIO` de rol frontend. Si el
backend necesita cambiar algo MIENTRAS el frontend trabaja, **no toca esa tarea**: abre una
**sub-subtarea** anidada y avisa en la madre el impacto.

Si el frontend todavía NO la tomó y el backend **cambia el contrato**, primero deja un comentario
`HANDOFF INVALIDADO` (con notificación) y la tarea vuelve a 🟡 En curso hasta la re-entrega. Si el
cambio **no** toca el contrato, se queda acá y solo se comenta.

Atajo: `/tarea frontend`.

_Nada pendiente de frontend._

| Ítem | Subtarea ClickUp | Backend cerrado por | Fecha | Breaking changes | Contrato |
| --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | — |

---

## 🟢 Realizadas

Módulos completos ya entregados. El detalle técnico de cada uno (incluidas las causas raíz de
los bugs corregidos) vive en `CLAUDE.md` y en `docs/features/`.

| Fecha | Ítem | Estado de verificación |
| --- | --- | --- |
| 2026-08-22 | **Clon: copia de solo datos, collation/owner del destino elegible y selección declarativa** (`T-260822-lz-clon-solo-datos-collation`, subtarea [`86e2xzzyh`](https://app.clickup.com/t/86e2xzzyh)) | 48 unit del spec/guard + 30 HTTP de la feature + 14 HTTP de no regresión + 23 del ciclo de la migración en SQLite; `alembic check` sin drift nuevo; Ruff limpio en lo nuevo; resultados idénticos al baseline de `HEAD` en 10 módulos vecinos. **Sin e2e contra motores reales** |
| 2026-08-22 | **Proyectos — agrupación de blueprints** (entidad nombre+descripción≤5000, pivote N:M, CRUD + vincular/desvincular + vista inversa) | 22 checks HTTP por ejecución directa + ciclo upgrade/downgrade/upgrade de la migración en SQLite. **Sin `pytest`** (política del repo) y **sin la migración contra la BD del gateway real** |
| 2026-08-21 | **Flujo de gestión de tareas** — lista y tarea principal en ClickUp, `TODO.md`, protocolo en `CLAUDE.md` | Verificado por lectura; IDs confirmados contra la API de ClickUp |
| 2026-08-16 | **Plan 10 — Exportación de BDs** (estructura y/o datos, sql/csv/json/ndjson, plan→preview→execute→download) | 87 checks HTTP + 27 de ciclo real + 81 writer + 96 spec + 23 literales + 17 endurecimiento. **Sin e2e contra motores reales** (P-01) |
| 2026-08-16 | **Revisión de seguridad del export** — 3 bloqueantes + 5 recomendaciones + 1 fuga, todos aplicados. Incluye `/*M!` de MariaDB, que evadía la blocklist entera de la **consola SQL** (vuln preexistente) | Cada fix con test de regresión que falla sin el fix |
| 2026-08-14 | **Captura de resultados de `SELECT` en migraciones** (P0) + 3 bloqueantes de seguridad + 2ª pasada adversarial (2 bloqueantes + 4 menores) | 69 casos + 24 HTTP, ejecución directa. **Sin e2e** (P-03) |
| 2026-08-04 | **Perfiles de permisos: familia MySQL↔MariaDB** + fallo silencioso de `apply-profile` | 21 checks HTTP. **Sin e2e** (P-07) |
| 2026-08-02 | **Consola SQL** (queries ad-hoc en modo seguro) + 2ª pasada adversarial con 4 bloqueantes | 145 casos en 4 archivos. **Sin e2e** (P-02) |
| 2026-07-30 | **Fix `DELIMITER $$` leído como dollar-quoting de PG** + 2 bugs hermanos del scanner | 37/37 por ejecución directa |
| 2026-07-29 | **Fix `:` literal en el DDL roto como bind param** (codegen de Alembic) | Script puntual + ciclo `command.upgrade` real contra SQLite |
| 2026-07-27 | **Fix: la contabilidad interna del gateway no es esquema del usuario** (`_gw_v_*` emitía `DROP TABLE` de su propia tabla de versión) — 3 capas de fix | Test de invariante sobre `version_table_name` |
| 2026-07-27 | **Fix: falso positivo de vista/rutina/trigger/evento al diffear una BD contra su clon** (esquema calificado en el cuerpo) | `tests/test_body_schema_qualifier_diff.py` |
| 2026-07-26 | **Orden de ejecución del diff + cierre de dependencias + reverso de redefiniciones + reconciliación de aplicaciones parciales** | 219 checks e2e reales en los 3 motores para el diff |
| 2026-07-19 | **Usuarios del motor** (vista agrupada, CRUD por identidad, add-host, revelar password) | 16 tests. **Sin e2e** (P-06) |
| 2026-07-18 | **Clonado de BDs** entre servidores (estructura + datos por streaming, cross-engine best-effort) | e2e contra MariaDB 11 real ejecutado. Cross-engine pendiente (P-08) |
| 2026-07-17 | **Diff de esquema entre dos BDs** + sincronización (adopt / execute ad-hoc) | 219 checks / 0 fallos en MySQL/MariaDB/PostgreSQL |
| — | **Plan 09 — Adopción, reconciliación y snapshot** | Cubierto por los 153 checks e2e del runner |
| — | **Plan 02 — Migraciones de blueprints** (Alembic embebido, apply/rollback/stamp, checkpoint, manifiesto) | 153 checks / 0 fallos contra motores reales |
| — | **Plan 07 Fase 1 — Permisos granulares** (GRANT/REVOKE/LIST/GRANTABLE/PROVISION/APPLY-PROFILE) | Fases 2 y 3 pendientes |
| — | **Ciclo de vida de BDs a nivel servidor** (crear/borrar/usuarios por identidad) | Solo `FakeAdapter`. **Sin e2e** (P-05) |
| — | **Cifrado de sobre KEK/DEK** (MultiFernet, rotación, guards de producción) | — |

---

## Detalle — `T-260822-lz-clon-solo-datos-collation` (Fase 1 del §4 del plan 11)

Copia de **solo datos** en el clon, collation/charset del destino elegible, selección declarativa
y la deuda de `row_estimate`. Plan completo: `docs/plans/11-organizacion-copia-de-datos-releases-y-mcp.md` §4.

**Decisiones de diseño que no son obvias del código:**

1. **Se expone la INTENCIÓN, no el enumerado.** `copy: structure_only | structure_and_data |
   data_only`; `EntityDdl` del export queda **interno** (para seguir hablando el idioma de
   `check_data_subset`). Motivo: de los cuatro valores del enumerado, `DROP_CREATE` y
   `CREATE_IF_NOT_EXISTS` responden 422 en el clon (ver más abajo), y un enumerado donde la mitad
   se rechaza es una promesa que el servidor no cumple. Mismo criterio por el que **no** se expone
   `scope_ddl`: el eje del contenedor ya lo cubren `target_mode` + `clean_mode`.
2. **El spec se manda en `preview`, no en `create`.** El wizard resuelve la selección en `preview`
   porque el catálogo necesita `job_id`; con el spec en `create` el operador elige a ciegas y hay
   que recrear el plan (snapshot en vivo, 10/min) por cada retoque. `preview` congela el spec y
   emite el token, igual que el export.
3. **`CREATE_IF_NOT_EXISTS` NO se implementa** (422): `export_make_idempotent` filtra por
   `object_type` y en MySQL/MariaDB acepta solo `{table, event}`, mientras `render_diff` emite los
   tipos del diff (`index`, `foreign_key`, `column`, …) → sentencia cruda → 1061/1826 y cuarentena
   con estructura parcial. Falla exactamente en el caso para el que se pide. Hacerlo bien no es
   `IF NOT EXISTS` por texto: es diffear contra el snapshot del destino, que es schema-comparisons.
4. **El guard de compatibilidad se calibra POR MOTOR.** En PostgreSQL `COPY` falla atómico, así que
   un aviso alcanza. En MySQL/MariaDB `LOAD DATA LOCAL` se comporta **siempre** como `IGNORE` y
   **anula el `sql_mode` restrictivo** (documentado en `data_copy.py:588`): truncado de strings,
   redondeo de `DECIMAL`, `unsigned` fuera de rango, `ENUM` con valor ausente y colisión de UNIQUE
   son warnings o filas descartadas **sin error**, y `rows_copied` cuenta escrituras al FIFO, no
   filas insertadas. Ahí el guard es la única defensa ⇒ esos casos **bloquean**.
5. **El guard no puede leer `DiffItem.risk`.** `is_narrowing` está definido en la dirección del
   diff (`src` = deseado), que es la **inversa** de la copia (`source` provee). Leer `risk`
   clasifica al revés el 100% de los casos de longitud/rango/precisión.
6. **`truncate` queda para la Fase 2** (ítem propio abajo). Es un agregado que salió de la revisión,
   no del pedido original, y su comportamiento en MySQL con `FOREIGN_KEY_CHECKS=0` es
   **indocumentado**. Consecuencia explícita: en Fase 1 el modo solo datos **carga y actualiza**
   filas, no reemplaza contenido (para reemplazar existe `clean_mode='objects'`).
7. **`row_estimate` no cambia el DTO compartido.** Se agrega `TableStat.estimated_rows_known: bool`
   en vez de pasar `estimated_rows` a `int | None`: ese cambio rompe el zod del frontend (declarado
   no nullable y embebido en `structureDumpSchema` ⇒ un `null` mata toda la respuesta de
   `GET /snapshot`) y vuelve fail-open el guard de disco del export (`unknown → 0` alimentando
   `ensure_capacity`).

**Correcciones de corrección incluidas** (encontradas al revisar el plan, verificadas en el código):

- `_run_phases` es una cadena `if/elif` gobernada por `target_mode`/`clean_mode`, no por la
  presencia de sentencias, y el modo solo datos exige `clean_mode='none'`: cualquier sub-fase nueva
  colgada de `clean_statements` **no se ejecutaría**, mostrándose igual en el preview y en el token.
- El auto-adopt usa `selection is None` como proxy de "clon completo" (`:427` y `:665`): con la
  selección declarativa resuelta a lista explícita, `adopt_target` da 422 pidiendo el clon completo,
  y relajar el guard deja `will_adopt=False` **para siempre sin que nada falle**. Pasa a ser un
  predicado explícito persistido.
- **El clon no tiene guard de alcance**: cero usos de `ensure_not_reserved_database` y de
  `is_gateway_metadata_target`. Nada impide apuntarlo a la BD de metadatos del gateway
  (`clean_mode='objects'` dropea `audit_log`/`servers`, el único control compensatorio del repo).
- **Los códigos de error no tenían transporte**: los 17 `AppHttpException` del módulo usan
  `context=`, que solo se expone en development, mientras el frontend lee
  `detail.public_context.code` y hoy matchea prosa con regex.
- El snapshot del destino y el armado del plan quedaban **fuera** del advisory lock.
- `session_replication_role` es un `SET` de superusuario cuyo error se traga (`except: pass`) ⇒
  declarar "PostgreSQL no necesita el aviso de triggers" es fail-open. Se verifica con `SHOW`.

**Fase 2 (no en esta tarea, ítems propios):** `data.on_existing='truncate'` con cierre FK del
destino + `confirm_row_loss` + semántica de cancelación; endpoint de capacidades con matriz publicada
y el frontend; `SET ROLE` para que el `owner` de PostgreSQL sea real (hoy `CREATE DATABASE … OWNER`
fija el dueño de la base y los objetos quedan del pseudo-root, así que el dueño no puede `ALTER` sus
propias tablas).

### Verificado

- **48** casos unitarios de `clone_spec` (`tests/test_clone_spec.py`): derivaciones, las reglas
  de coherencia y el guard de compatibilidad con su calibración por motor.
- **30** casos HTTP de la feature (`tests/test_api_clone_data_only.py`, TestClient + SQLite +
  adapter falso, worker síncrono).
- **14** casos HTTP preexistentes del clon, sin regresión.
- **23** checks del ciclo `upgrade → downgrade → upgrade` de la migración contra SQLite REAL,
  incluido el relleno de datos de los jobs históricos y la expiración de los `pending`.
- `alembic check`: el drift que reporta es **preexistente** (`charset_collation_options`,
  `migration_statement_progress`, `model_migration_statements`, `query_executions`); ni una
  entrada de `clone_jobs`.
- Comparación contra un worktree limpio en `HEAD`: resultados **idénticos** en
  `test_export_spec`, `test_export_hardening`, `test_api_database_exports`, `test_introspection`,
  `test_data_copy`, `test_api_schema_comparisons`, `test_api_managed_databases`,
  `test_snapshot_layout`, `test_migration_runner` y `test_query_policy`.
- Ruff: limpio en todos los archivos nuevos; en `clone_controller.py` quedan las **12
  violaciones preexistentes** y ninguna nueva.

**Dos tests con dientes, que fallan si el fix se revierte:** el de la polaridad de
`is_narrowing` (`varchar(50)`→`varchar(20)` tiene que BLOQUEAR; con los argumentos en el orden
"natural" del diff se aprueba la pérdida de datos) y el que ejercita el **worker** en modo solo
datos (un test de preview no habría visto que la fase de datos se decidía en una cadena `if/elif`
sobre `clean_mode`).

### Qué quedó SIN verificar (lo que decide si esto se puede confiar en producción)

Todo se verificó por **ejecución directa** de las funciones de test (política del repo: no se
corre `pytest`) y contra **SQLite**. Nada se probó contra MySQL, MariaDB ni PostgreSQL reales:
no hay Docker en este entorno. Lo que solo un motor real puede confirmar:

1. **Que `LOAD DATA LOCAL` se trague de verdad un truncado de string**, y que el guard lo haya
   bloqueado antes. Es la premisa sobre la que se calibró TODO el guard: si fuera falsa, varios
   casos estarían bloqueando de más.
2. El **colapso de filas por collation** en una columna de PK (`utf8mb4_bin` → `_general_ci`).
3. Que `COPY` de PostgreSQL falle **atómico** ante una diferencia de tipo — es lo que justifica
   que allá esos casos sean aviso y no bloqueo.
4. `supports_charset_combination` contra MySQL 8, MariaDB 11 y PostgreSQL, incluida la asimetría
   decidida (en PG el encoding se verifica; el locale del SO solo lo confirma el `CREATE`).
5. El `owner` de PostgreSQL, y que el aviso sobre la propiedad de los objetos sea exacto.
6. La migración `d3e4f5a6b7c8` contra la **BD del gateway real** (hoy solo SQLite) — se suma a
   P-10.
7. `session_replication_role` con un pseudo-root **sin** superusuario: confirmar que el `SET`
   falla en silencio, que es la premisa del aviso de triggers en PostgreSQL.

Tampoco se corrió la suite con `pytest` (P-11) ni se tocó el **frontend**: hasta
`T-260822-lz-clon-capabilities-frontend`, el wizard **no puede armar un job de solo datos** (el
paso de selección bloquea el avance con la selección de estructura vacía, que en este modo es la
definición del modo, y "replanear" rehidrata el plan leyendo solo los campos legacy). La feature
es usable **por API**.
