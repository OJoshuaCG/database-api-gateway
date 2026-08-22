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
2. **Cada ítem de este archivo tiene un ID estable (`P-XX`) y la subtarea se llama
   `P-XX — <título>`.** Ese prefijo es la clave única: con nombre libre, dos personas nombran
   lo mismo distinto y duplican aunque hayan buscado bien.
3. **El ID de la subtarea se anota en la columna `Subtarea`** en cuanto se crea. Así este
   archivo es el índice `P-XX → subtarea` y no hace falta buscar en ClickUp.
4. **Las subtareas se crean al tomarlas**, no por adelantado — así no se llena ClickUp de IDs
   que nadie va a ejecutar. Un ítem con `Subtarea: —` todavía no fue tomado.
5. **Al buscar en ClickUp, `include_closed: true` es obligatorio.** Viene apagado por defecto:
   sin él, una tarea ya terminada no aparece y se crea un duplicado exacto.
6. **Antes de empezar**: validar en ClickUp. Si está `in progress`, se **interrumpe** y se avisa
   quién la tiene. Si está `complete`/`reviewed`, se avisa que ya está hecha.
7. **Al empezar** se pone `in progress` (eso es lo que reserva la tarea) y **al terminar**
   `complete` — llegar a `complete` no es opcional: mientras no llegue, bloquea a los demás.
   Ambos con su comentario.
8. **`reviewed` lo pone otra persona**, nunca el que hizo el trabajo. `complete` = "terminé";
   `reviewed` = "alguien más lo verificó".
9. **La identidad va escrita en el texto del comentario.** Todos los comentarios se publican
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

## 🟢 Realizadas

Módulos completos ya entregados. El detalle técnico de cada uno (incluidas las causas raíz de
los bugs corregidos) vive en `CLAUDE.md` y en `docs/features/`.

| Fecha | Ítem | Estado de verificación |
| --- | --- | --- |
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
