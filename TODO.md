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
| T-260822-lz-clon-reconciliacion-y-cierre | Clon: reconciliación post-copia y cierre accionable de dependencias | **Dos incumplimientos del plan aprobado de `T-260822-lz-clon-solo-datos-collation`.** (a) La reconciliación post-copia no se implementó: `clone.row_count_mismatch` quedó como constante muerta, y es la pieza que sostiene el argumento con el que se calibró todo el guard (en la familia MySQL el motor no puede fallar, así que la detección tiene que venir de otro lado). Alcance real ya decidido: solo concluye en `append` (`count_antes + filas_leídas = count_después`); en `upsert` una fila que actualizó a otra es indistinguible de una descartada, así que ahí NO se emite y se avisa que no hay verificación — un chequeo que no puede concluir es peor que su ausencia si se presenta como si concluyera. (b) El cierre de dependencias re-incluye en silencio: debe dar 422 `clone.missing_dependencies` con los sugeridos cuando re-agrega algo excluido EXPLÍCITAMENTE (por nombre o patrón), y mantener el cierre silencioso cuando solo no se mencionó. Criterio de `plan_integrity`. | — |
| T-260822-lz-clon-contrato-frontend | Clon: lo que el contrato le debe al frontend | Seis adiciones, todas aditivas (verificado que los schemas zod del frontend no usan `.strict()`, así que los campos nuevos se descartan y no rompen la SPA). La importante: **`CloneSummaryOut` no permite reconstruir el plan** — ocho columnas de `clone_jobs` no las expone ningún GET y `preview` da 409 en cuanto el job deja de estar `pending`, así que la información desaparece exactamente cuando se necesita (después de un fallo, que es cuando se usa «Replanear»). Las otras: `severity` sin nivel `danger` obliga a mantener la lista de códigos peligrosos en el cliente; `clone.charset_combination_disabled` emite una forma distinta de la que `errors.ts:497-511` ya parsea y se pierde el repoblado de alternativas; dos notices llegan con `detail` vacío; `CloneItemOut` no expone el `sql` del paso fallido; y `skipped[].object_type` no pertenece al enum documentado (defecto PREEXISTENTE: en un clon cross-engine el `safeParse` del cliente descarta la respuesta entera). | — |
| T-260822-lz-clon-capabilities-frontend | Clon: endpoint de capacidades + matriz publicada (Fase 2) | Es el cambio que elimina la tabla de intenciones del cliente: con la forma del export (`options` con rutas con puntos, `compatibility`, `limits`, `error_codes`) el motor genérico de `database-exports/logic.ts` se reusa sin escribir nada nuevo. **El plan de UI ya está escrito y NO depende de este endpoint** (ver 🔵 Pendiente de frontend). | — |
| T-260822-lz-clon-owner-set-role | Clon: el `owner` de PostgreSQL no es real | `CREATE DATABASE … OWNER x` fija el dueño **de la base**; todos los objetos los crea la conexión pseudo-root, así que el dueño pedido **no puede `ALTER`/`DROP` sus propias tablas** — peor que no pasar `owner`. Requiere `SET ROLE` para las fases de DDL y datos (o `REASSIGN OWNED`/`ALTER … OWNER TO` al cerrar) y validar la membresía del pseudo-root en el rol, o el `CREATE DATABASE` falla con 42501 **dentro del worker** (y con `clean_mode='drop_database'`, después del DROP). | — |
| T-260822-lz-pg-resync-serial | El resync de secuencias de PG no cubre `serial` ni cross-engine | `_resync_postgres_identity_sequences` (`clone_controller.py:740-800`) solo actúa sobre columnas con `col.identity is not None` **del snapshot del ORIGEN**. Una columna `serial` tiene `identity=None` y default `nextval(...)` (por eso existe `PostgresAdapter._serial_type`), y un origen MySQL con `AUTO_INCREMENT` tampoco tiene `identity` ⇒ **la secuencia del destino nunca se resincroniza** y el primer `INSERT` de la aplicación choca la PK (23505). Defecto **preexistente** que el modo solo datos vuelve el camino principal. Fix: elegir las columnas desde el **destino** con `pg_get_serial_sequence(t, c) IS NOT NULL`, que cubre `serial` **e** `identity` y es la función que el código ya usa. | — |
| T-260822-lz-entornos-flags-restantes | Entornos: los 4 flags de política que faltan | Se entregó `blocks_destructive_migrations` y **solo ese**, por la regla de "cero flags inertes". Los otros cuatro del §2 del plan 11, cada uno con lo que le falta para ser real: **`requires_confirmation`** → el gesto correcto es `confirm_token` HMAC con `subject` = (slug + ids del lote + versión objetivo), no un slug que `GET /environments` publica (público, constante, sin TTL ni anti-replay); y el guard va **antes** del bucle de `apply_all`, porque desde adentro el 422 es imposible (el `except` de `:1772` conserva solo `exc.message` y la ruta responde 200) y en un lote mixto las BDs sin entorno se aplicarían primero ⇒ media aplicación. **`requires_previous_environment`** → hay que cerrar `stamp` (escribe `model_version` sin ejecutar DDL: es su puerta trasera), restringir el cohorte a `status=active` (hoy `apply_all` no filtra por estado, así que una BD `pending` o `archived` trabaría producción para siempre sin forma de saber cuál), devolver los ids bloqueantes en `public_context`, y hacer que el conjunto **vacío BLOQUEE** (`all([]) == True` es fail-open en el caso más común: blueprint nuevo cuya primera BD es la de producción). El head sí es exacto hoy (`specs[-1].version`). **`max_databases_per_apply`** → con el default de la ruta (`max_databases=10`) un tope de 10 no puede dispararse nunca, y con `id.asc()` en un lote heterogéneo se cumple por accidente; necesita selección por grupo de entorno y reportar los recortes a nivel de lote (`capped_by_environment`), no como ítems falsos que ensucian `processed` y la auditoría. **`allows_agent_queries`** → la crea la feature 5 (MCP), junto con el gate donde `NULL` **niega** (asimetría deliberada con el `NULL` permisivo de `apply_all`, anotada en el docstring de `_env_policy_for`). | — |
| T-260822-lz-entornos-otros-caminos | El entorno no restringe los demás caminos mutantes | `blocks_destructive_migrations` cubre `apply` y `apply-all`. **No** cubre: `rollback` (`:1004`, ejecuta `down_sql`, destructivo por definición), `reconcile-partial` (`:1452`), el clon con `clean_mode='drop_database'`, la consola SQL (opera por `(server_id, database_name)` y **nunca mira `ManagedDatabase`**, así que no tiene de dónde sacar el entorno), el `DROP DATABASE` a nivel servidor, la conversión de collation, el export (no destruye pero **extrae datos productivos**) y los `DROP USER` / `REVOKE CASCADE`. Todos tienen su propio doble factor (re-tipeo + `confirm_token`), que es lo que hoy los protege. Mientras esto siga así, `docs/features/environments.md` **declara explícitamente** qué restringe el entorno y qué no — si se extiende, hay que actualizar esa sección. | — |
| T-260822-lz-clon-job-sin-autor | El clon adopta el destino sin autor auditado | `_adopt_target` (`clone_controller.py:2461`) llama a `adopt_database` con `admin=None`, así que la adopción del destino queda auditada **sin quién la originó**. No es un olvido del llamador: `clone_jobs` **no persiste** quién pidió el job (no hay columna de autor en el modelo), así que no hay nada que propagar. Requiere columna nueva + migración. Descubierto al propagar `environment_id` en ese mismo lugar. | — |
| T-260822-lz-charset-default-carrera | `charset_catalog.update_option` tiene la carrera de `is_default` sin cerrar | `charset_catalog.py:368-377` apaga los demás defaults y después se enciende, sin bloqueo de filas. Con dos requests concurrentes ninguno ve al otro encendido y quedan **dos defaults sin ningún error**, igual en REPEATABLE READ (MySQL/MariaDB) que en READ COMMITTED (PostgreSQL): el `UPDATE` no toma predicate locks sobre filas que no matchean. Su docstring afirma un invariante que el mecanismo no garantiza. `EnvironmentController._claim_default` ya lo resuelve con `with_for_update()` sobre el catálogo (3-10 filas, costo nulo) y sirve de molde. | — |
| T-260822-lz-contratos-nullish | **13 campos de 7 schemas zod declarados requeridos contra un backend que los tipa `X \| None`** — 2 ROTOS HOY | Causa raíz: `ApiResponse._exclude_none` (`app/utils/response.py:64-67`) filtra **solo las claves del envelope**, así que los `None` anidados salen como `null`; y en zod `.optional()` **rechaza `null`**. Combinado con el `safeParse` del envelope completo (`client.ts:128-133`), una divergencia de UN campo cuesta la respuesta ENTERA. **Rotos hoy:** `database-exports.ts:416-417` (`has_primary_key`/`has_triggers` vs `bool \| None`; el controller emite `None` para todo objeto que no es tabla ⇒ `GET /database-exports/{id}/objects` se descarta entero en cualquier BD con una vista o rutina — es el selector de objetos de esa pantalla) y `database-exports.ts:631-632` (`phase`/`status`, nullables POR DISEÑO ⇒ el polling de `.../items` descarta la lista **mientras el job corre**). **Latentes:** `database-exports.ts:675,680,687` · `:245,256` · `:646` · `migration-select-results.ts:54-55` · `db-migrations.ts:28-29` · `reconcilePartialResultSchema` (`db-migrations.ts:197-198`). Fix mecánico (`.nullish()`) + un test por schema. **Regla:** un campo que el backend tipa `X \| None` va `.nullish()`, nunca `.optional()` ni requerido. | — |
| T-260822-lz-filtros-en-url | Los filtros del inventario de BDs no viven en la URL | `ManagedDatabasesPage` usa `useState` sueltos y no `useSearchParams`, así que los filtros se pierden al recargar y **no son enlazables**. Consecuencia concreta ya visible: la pestaña de Entornos muestra el conteo de BDs por entorno pero **no puede enlazar** a «ver esas N bases» (el link iría sin filtro), y el 409 `environment.has_databases` del borrado por API tampoco tiene una salida de un click. Precedente de cómo hacerlo: `AdminPage` ya sincroniza su pestaña con un validador. | — |
| T-260822-lz-callout-a-ui | Promover `Callout` a `components/ui` | Vive dentro de `database-exports` y su propio docstring advierte el riesgo de inlinear el `div`: *"es donde se cuela la banda roja que debía ser ámbar — y en esta pantalla el color ES la información"*. Entornos ya necesita bandas en tres lugares (diálogo de apply, apply por BD, filtro sin clasificar) y las inlineó; con un cuarto consumidor conviene el primitivo compartido. | — |
| T-260822-lz-frontend-proyectos | **Proyectos no existe en la SPA** | El backend se entregó y la tarea se cerró como `complete` (que en el protocolo significa "no requiere frontend"), pero hay **cero hits de "project" en todo `src/`**: ni contrato zod, ni ruta, ni pantalla. El módulo está entregado e inalcanzable desde la interfaz. Hay que decidir si de verdad iba sin UI o si se cerró mal. | — |
| T-260822-lz-form-bd-dos-componentes | `ManagedDatabaseForm`: partir en dos componentes por modo | `useForm<ManagedDatabaseFormValues>` se tipa con la INTERFAZ, no con el schema (el schema entra solo como `zodResolver`), así que TypeScript ayuda en 1 de 4 caminos: avisa en el mapper por excess-property check, pero no en `register('campo')` ni en los `defaultValues` del modal. Con una unión discriminada por modo —o directamente dos componentes, que ya comparten poco— el `mode` pasa a ser un discriminante que TS puede usar. Descubierto al sacar `model_version` del modo edición. | — |

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

| Ítem | Subtarea ClickUp | Backend cerrado por | Fecha | Breaking changes | Contrato |
| --- | --- | --- | --- | --- | --- |
| Clon: copia de solo datos, collation/owner del destino y selección declarativa | [`86e2xzzyh`](https://app.clickup.com/t/86e2xzzyh) | LeoZubiri@outlook.com | 2026-08-22 | **No** para la SPA actual (los schemas zod no usan `.strict()`, así que los campos nuevos se descartan y nada se rompe). Sí hay cambios de comportamiento que el wizard tiene que absorber: el SPEC se manda ahora en `preview` (no en `create`), `confirm_token` puede llegar **vacío** cuando hay `blocking_issues`, y los mensajes de error cambiaron — `wizard/messages.ts` los matchea con expresiones regulares sobre la prosa. | `docs/features/database-clone.md` + `de73439` (schemas y rutas). Plan de UI COMPLETO ya escrito, con recorrido paso por paso, copy, mapeo de códigos `clone.*` y plan de pruebas. |

---

## 🟢 Realizadas

Módulos completos ya entregados. El detalle técnico de cada uno (incluidas las causas raíz de
los bugs corregidos) vive en `CLAUDE.md` y en `docs/features/`.

| Fecha | Ítem | Estado de verificación |
| --- | --- | --- |
| 2026-08-22 | **Entornos — clasificación de BDs + bloqueo real de DDL destructivo, backend Y frontend** (`T-260822-lz-entornos-clasificacion`, subtarea [`86e2y21kh`](https://app.clickup.com/t/86e2y21kh)) — cuatro entornos fijos (`local`/`development`/`staging`/`production`), administración por API a propósito (sin CRUD en la SPA), badge con la política, filtro por entorno / sin clasificar, integración con los DOS entrypoints del apply y pestaña de solo lectura en Administración | Backend: 55 tests + ciclo real de las 2 migraciones contra SQLite con idempotencia comprobada + head único + `alembic check` sin drift nuevo. Frontend: 21 tests nuevos (608 pasados, con los mismos **9 fallos preexistentes** que `main`), `typecheck` limpio, `lint` 0 errores y `build` en verde. **Sin la migración contra la BD del gateway real** (hereda P-10), **sin verificar la unicidad de `is_default` bajo concurrencia** (SQLite ignora `FOR UPDATE`) y **sin e2e contra motores reales** |
| 2026-08-22 | **Incidente de producción: el gateway no arrancaba por dos heads de Alembic** (`T-260822-lz-fix-alembic-heads-bifurcados`, subtarea [`86e2y1rhk`](https://app.clickup.com/t/86e2y1rhk)) — linealización de `d8e9f0a1b2c3` sobre `d3e4f5a6b7c8` + guard `scripts/check_migration_graph.py` en hook de pre-push, CI y pre-vuelo del `entrypoint.sh` | Guard verificado en sus 4 escenarios (dos heads / IDs duplicados / padre huérfano / bytecode rancio), cada uno reproducido y con el fallo esperado; cadena completa `base → head` (26 migraciones) y ciclo `upgrade/downgrade/upgrade` de las dos puntas en SQLite; `alembic heads` coincide con el guard; `alembic check` sin drift nuevo; `ruff check` limpio; `bash -n` de entrypoint y hook. **Sin la migración contra la BD del gateway real** (hereda P-10) |
| 2026-08-22 | **Fix P0 del clon: el atajo legacy `include_data` rompía todo clon con datos** (`T-260822-lz-clon-fix-legacy-on-existing`, subtarea [`86e2y15bm`](https://app.clickup.com/t/86e2y15bm)) | 5 tests nuevos, los 5 verificados contra un worktree de `HEAD` (fallan sin el arreglo); 176 tests del clon y vecinos en verde; Ruff sin violaciones nuevas |
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

## Detalle — `T-260822-lz-entornos-clasificacion` (Feature 1 del §2 del plan 11)

Clasificación de BDs gestionadas por entorno + **un** flag de política efectivamente aplicado.
Plan completo: `docs/plans/11-organizacion-copia-de-datos-releases-y-mcp.md` §2. Guía del módulo:
`docs/features/environments.md`.

**El plan se rehízo antes de implementar.** La primera versión se sometió a cuatro revisiones
(seguridad/bypass, Alembic+modelo de datos, contrato API+frontend, internals de `apply_all`) y
**no sobrevivió**: de los tres guards que proponía, uno nacía ciego, uno no podía devolver 422
desde donde estaba puesto, y el tercero se satisfacía con un `PATCH`. Lo que sigue son las
decisiones del rediseño que no se leen del código.

**Decisiones de diseño que no son obvias:**

1. **Cero flags inertes.** El §2 propone cinco flags; se creó UNO
   (`blocks_destructive_migrations`), porque es el único que el servidor hace cumplir hoy. Un
   booleano que la API expone y nadie lee, la SPA lo pinta como control activo: es peor que su
   ausencia. Los otros cuatro tienen ítem propio con lo que le falta a cada uno.
2. **El guard NO lee el manifiesto de sentencias.** `ModelMigrationStatement.destructive` parece
   la fuente natural y era la del plan original, pero esas filas **casi nunca existen**: el
   manifiesto es opcional y lo escribe únicamente la adopción de un diff estructural
   (`_write_statement_manifest` corta con `if not statements or not source_engine: return`, y
   `ModelMigrationCreate` no tiene ni campo `statements`). Una migración escrita a mano con
   `up_sql = "DROP TABLE clientes"` produce **cero** filas ⇒ el guard la dejaba pasar, por el
   camino documentado para escribir una migración a mano. La fuente es `migration_facts.analyze`
   (AST), que además es lo mismo que el listado ya publica como insignia `destructive`. El
   manifiesto se usa en OR, donde exista.
3. **Se analiza el SQL RESUELTO POR MOTOR** (`select_up_sql`), no `spec.up_sql`: el `DROP` puede
   vivir solo en `up_sql_postgresql`, y mirando el SQL base es invisible justo en el motor donde
   se ejecuta.
4. **El guard va en `_run_apply`, no en el bucle de `apply_all`.** Punto de inserción: justo
   después de `pending = compute_pending(...)`, al lado de `_guard_reviewed_capture` y
   `_guard_capture_consent`, que están ahí por el mismo criterio (el comentario del archivo lo
   explica). Beneficio: cero lecturas extra al motor, ninguna ventana TOCTOU nueva, y **cubre el
   `apply` por BD gratis**. Sin eso el incentivo era perverso: `apply_all` va siempre al head, así
   que una versión destructiva trabaría producción de forma permanente en el lote mientras el
   `apply` por BD la aplica con `?version=` sin gate. El plan original usaba `_dry_run_plan` por
   BD desde el bucle: es read-only e idempotente (verificado: no crea la tabla de versión, no
   audita, no toma el advisory lock) pero era una TERCERA lectura del motor.
5. **`rank` NO es único.** El único rompía el seed en silencio (slug renombrado ⇒ el seed reinserta
   con el mismo rank ⇒ `IntegrityError` que el `except` del patrón de seed se traga ⇒ seed muerto
   para siempre), y no hacía falta: el orden total `(rank, id)` ya da un predecesor determinista.
   Además sin único un swap de ranks no colisiona en el paso intermedio, que en MySQL/MariaDB no
   se puede diferir.
6. **`ON DELETE RESTRICT`, no `SET NULL`.** No calca `model_id` (puntero de capacidad) sino
   `owner_id` (`RESTRICT`, "reasignar antes de borrar"): `environment_id` es un puntero de
   POLÍTICA, y con `SET NULL` borrar una fila convertiría N BDs de producción en BDs sin guard, con
   el guard de acuerdo en que eso está bien.
7. **`DELETE` sin `force`.** Exige cero BDs asignadas (409 con el conteo). Ningún `DELETE` del repo
   tiene `force` — los destructivos exigen re-tipear el identificador — y un flag en una URL
   termina en un script. La vía de retiro de un entorno con BDs es `is_active=false`.
8. **`is_default` con `with_for_update()`.** El patrón "apagar los demás y después encenderme"
   tiene una carrera real que deja DOS defaults **sin ningún error** (ver el ítem de
   `charset_catalog`, que la tiene sin cerrar). El índice único parcial no es portable: MySQL 8 no
   tiene índices parciales y el truco funcional no existe en MariaDB 11. SQLite ignora
   `FOR UPDATE`, así que este invariante NO está verificado contra concurrencia real.
9. **El seed siembra solo si la tabla está VACÍA**, a diferencia de `charset_catalog`, que hace
   top-up. Su docstring dice que divergir "no hace daño"; **eso no vale acá** porque la fila es la
   política: un top-up resucita un `production` borrado a propósito sin restaurar el
   `environment_id` de sus BDs, puede duplicar el default, y deja dos políticas distintas según
   cómo se provisionó el gateway.
10. **`slug` se normaliza y compara en Python.** MySQL/MariaDB comparan case-insensitive por
    default y PostgreSQL no: la misma fila sería duplicado en un motor y dos filas en el otro, y
    `confirm_slug=PRODUCTION` confirmaría en uno y no en el otro.
11. **El default es el entorno MÁS PERMISIVO** (`development`, decisión explícita del usuario). Se
    documenta la consecuencia: una BD nueva nace clasificada pero **no** nace protegida. La red es
    el filtro `only_unassigned` y el `database_count` del listado.
12. **`NULL` es permisivo en el guard** (compromiso de compatibilidad), y esa asimetría **NO se
    traslada** al futuro gate de agentes, donde `NULL` debe negar. Anotado en el docstring de
    `_env_policy_for` para que nadie lo "unifique".
13. **El código de error del rechazo va en un campo del ÍTEM, no en `public_context`.** El
    `except` de `apply_all` conserva solo `exc.message` y la ruta responde 200, así que para los
    rechazos por BD el canal habitual no existe. De ahí `error_code` en `ApplyAllItemOut`.
14. **El dry-run informa y no bloquea** (`blocked_by`), porque es la llamada con la que el operador
    descubre qué lo frena. Precedente: `_guard_quarantine` también se saltea en dry-run. Sale
    gratis: ni `apply` ni `apply_all` llaman a `_run_apply` cuando `dry_run=True`.
15. **`force` no saltea el guard**, dicho en el `description` de los dos endpoints y fijado con un
    test. `force` es override de cuarentena y nada más; en la SPA es un `Switch` sin fricción.

**Agujeros adyacentes cerrados** (sin ellos la barrera nacía burlable):

- **`model_version` fuera del `PATCH`**: era escribible a ciegas, sin confirmación y con un
  `audit.record` sin `detail`, así que "promover a producción" se lograba tipeando un número. Y
  `max_length=50` no exigía dígitos mientras `version_sort_key` hace `int(version)`: un
  `"v3-hotfix"` era un 500 latente. En el ALTA se sigue aceptando pero ahora **validado contra el
  blueprint**, con el mismo criterio que ya usaba `adopt`.
- **`environment_id` propagado en el auto-adopt del clon** (`clone_controller.py`): propagaba
  blueprint y versión pero no el entorno, así que un clon completo de una base productiva
  —estructura Y DATOS de producción— nacía como desarrollo.
- **La auditoría del update lleva `detail` con `old → new`**: sin él una reclasificación de entorno
  era indistinguible de un cambio de `notes`.
- **`stamp` documentado como la puerta trasera** de cualquier gate que se apoye en la caché de
  versión, con su auditoría diciendo que DECLARA sin ejecutar DDL.

### Verificado

**Con `pytest` de verdad** (este entorno es Fedora nativo, no WSL2, así que la política de no
correr la suite por I/O lento no aplica acá): **1652 pasados, 6 skipped, 3 fallos PREEXISTENTES**
(`test_gateway_internal_tables`, el CORS de `test_health` y `test_snapshot_layout`, los tres
verificados uno por uno contra el checkout en la punta, donde fallan igual).

- **36 tests nuevos**: 25 de CRUD/invariantes (`tests/test_api_environments.py`) + 11 del guard
  (`tests/test_environment_guard.py`).
- **Ciclo real `upgrade → downgrade → upgrade`** contra SQLite, más 13 checks del esquema
  resultante con `PRAGMA foreign_keys=ON`: el seed, la columna nullable, el índice, la FK con su
  `RESTRICT`, los dos únicos rechazando duplicados, `rank` aceptando repetidos, y que el `RESTRICT`
  **dispara de verdad a nivel motor** (los tests del repo usan `create_all` sin activar las FKs, así
  que sin esto el 409 del controller aparentaría validar producción).
- **`scripts/check_migration_graph.py`**: 27 revisiones, head único `e4f5a6b7c8d9`.
- **`alembic check`**: el drift reportado es **solo el preexistente** (`charset_collation_options`,
  `migration_statement_progress`, `model_migration_statements`, `query_executions`) y ni una entrada
  de `environments` ni de `managed_databases`.
- **Ruff**: los archivos nuevos producen exactamente los mismos códigos y cantidades que sus
  equivalentes preexistentes (1 BLE001, 1 RUF100, 3 UP007, 1 UP035 — el `Union[]` de la plantilla
  de Alembic y el `noqa` del seed). En los 9 archivos modificados, paridad exacta con la punta:
  cero violaciones nuevas.

**Dos tests con dientes**, que fallan si se revierte el rediseño: el del **manifiesto ausente**
(migración escrita a mano con `DROP TABLE` y cero filas de manifiesto ⇒ tiene que bloquear; falla
contra la implementación del plan original) y el del **override por motor** (`DROP` solo en
`up_sql_postgresql` ⇒ tiene que bloquear; falla si alguien "simplifica" a `spec.up_sql`). Más el de
**contención del lote**, que es el que distingue un guard bien ubicado de uno que aborta el lote
entero — un test que mire solo la BD bloqueada no lo ve.

### Qué quedó SIN verificar

1. **La migración contra la BD del gateway real** (hoy solo SQLite) — se suma a `P-10`.
2. **La unicidad de `is_default` bajo concurrencia real.** SQLite ignora `FOR UPDATE` en silencio y
   los tests son single-thread, así que el `with_for_update()` está puesto por análisis del modo de
   fallo, no verificado contra dos requests simultáneos en MySQL/PostgreSQL.
3. **El `RESTRICT` en los tres motores.** Verificado contra SQLite con las FKs activadas a mano; en
   MySQL/MariaDB/PostgreSQL se asume el comportamiento estándar.
4. **El frontend**: sin tocar. La feature es usable por API; la SPA todavía no tiene selector de
   entorno ni pinta el badge.

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

### Corrección posterior (2026-08-22)

Esta entrega se reportó como completa y **el flujo con datos no funcionaba**: el atajo legacy
`include_data` persistía un `data_on_existing` derivado por el servidor, y `validate_spec` lo
leía como una elección del cliente, así que todo preview de la SPA —que manda siempre
`{selection: …}`— respondía 422 `clone.conflicting_options` sin salida posible. Arreglado en
`T-260822-lz-clon-fix-legacy-on-existing` ([`86e2y15bm`](https://app.clickup.com/t/86e2y15bm)).

La causa de la ceguera fue el arnés de tests, no el diseño: el helper `_preview_and_execute`
manda `json={}`, la única forma del cuerpo que la SPA nunca usa. Los 5 tests del arreglo cubren
las formas reales y se verificó que fallan sin él.

Además se detectaron **dos incumplimientos de este mismo plan** que quedaron como pendientes
propios: la reconciliación post-copia y el 422 accionable del cierre de dependencias (ver
🔴 Pendientes).

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
