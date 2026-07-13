# Diff de esquema entre BDs gestionadas + adopción/ejecución

Compara la **estructura** de dos BDs gestionadas del **mismo motor** (o MySQL↔MariaDB) y,
a partir del diff, ofrece dos caminos: **adoptarlo** como una nueva versión del blueprint
del target (Opción A) o **ejecutarlo directamente** sobre el target (Opción B). Cierra un
hueco que ni las [migraciones de blueprints](model-migrations.md) ni el
[snapshot de Plan 09](adoption-reconcile-snapshot.md) resuelven: **comparar dos BDs entre
sí** para detectar drift (dev vs staging, dos clientes que deberían compartir blueprint,
o qué le falta a una BD recién adoptada).

> Diseño e historia completos (decisiones de producto, arquitectura de 3 capas, matriz de
> trampas de normalización, clasificación destructiva): plan de diseño de esta feature
> (no versionado en `docs/plans/` a diferencia de los planes 00-09; ver el historial de
> commits de `app/services/db_admin/schema_diff.py` y `app/controllers/schema_comparison_controller.py`
> para la implementación fase por fase).

## Concepto: 3 capas separables

1. **Introspección → snapshot estructural canónico** (`SchemaSnapshot`,
   `app/services/db_admin/dtos.py`): tablas/columnas/PK/FK/índices/checks/uniques +
   vistas, rutinas, triggers, secuencias, tipos ENUM (PG), extensiones (PG) y events
   (MySQL/MariaDB). `ServerAdapter.structural_snapshot(database)` la produce reusando el
   `Inspector` de SQLAlchemy más hooks por adapter (collation/charset/comentarios que el
   Inspector no expone fiable). **Solo estructura, nunca filas.**
2. **Diff puro** (`app/services/db_admin/schema_diff.py`, función `diff_snapshots`): sin
   conexión a motor, sin ORM — 100% función pura, 100% testeable con fixtures en memoria.
   Aquí vive el matching por **definición** (no por nombre autogenerado) y la clasificación
   de riesgo por ítem.
3. **Generación de DDL** (`ServerAdapter.render_diff(diff)`): específica de dialecto, vive
   en cada adapter (`mysql_adapter.py` / `postgres_adapter.py`).

Esta separación importa porque la capa 2 es 100% verificable en CI sin Docker; las capas 1
y 3 requieren motores reales (ver [Verificación](#verificación)).

## Decisiones de producto

1. **Dirección explícita, nunca inferida.** Toda comparación exige `source_database_id`
   (estado deseado/referencia) y `target_database_id` (la que se modificaría). Todo el DDL
   generado es "qué correr sobre TARGET para que quede como SOURCE".
2. **Adoptar como blueprint (Opción A) solo si el TARGET tiene `model_id`.** La nueva
   versión se agrega a ESE blueprint.
3. **Ejecución directa (Opción B) queda BLOQUEADA (409) si el target tiene blueprint
   asignado** — evita que el target quede desincronizado de su propio blueprint sin que el
   sistema se entere. Sin blueprint, Opción B está disponible sin restricciones.
4. **El modo automático `all_except_destructive` excluye TODO lo potencialmente
   destructivo**, no solo `DROP` literal: también achicar un tipo, quitar valores de un
   ENUM, cambiar collation/charset, `DROP DEFAULT`/`DROP CONSTRAINT`, objetos con
   `possible_rename_of`, etc. — ver [clasificación destructiva](#clasificación-destructiva).
5. **Se permite comparar MySQL↔MariaDB** (misma familia SQL); PostgreSQL solo consigo
   mismo. La comparación cruzada marca `cross_flavor_warning=true` en cada ítem afectado
   (ruido esperable: JSON como LONGTEXT en MariaDB, `utf8mb4_0900_ai_ci` solo en MySQL 8,
   `CREATE SEQUENCE` nativo de MariaDB, etc.).

## Matching por definición, no por nombre (garantía central)

Constraints/FKs/índices autogenerados (`tabla_ibfk_1`, `tabla_pkey`) difieren entre
instancias estructuralmente idénticas. El diff los empareja por **firma de definición**
(columnas + tabla referida + opciones), nunca por nombre — dos BDs con la misma estructura
pero nombres de constraint distintos dan **cero ítems de diff**. Verificado empíricamente
contra los 3 motores (ver [Verificación](#verificación)).

## Normalización anti-falsos-positivos

- **Tipos**: canonicalizados vía `sqlglot` por dialecto (`int(11)` == `int` en MySQL 8+).
- **Defaults**: PG agrega casts (`'x'::character varying`); MySQL/MariaDB difieren en
  `CURRENT_TIMESTAMP` vs `current_timestamp()` — se normalizan antes de comparar.
- **Collation/charset**: "igual al default de la tabla/BD" no es diff; solo divergencia
  explícita se reporta (limitación metodológica documentada: `information_schema` reporta
  el valor efectivo, no si fue explícito).
- **Estado, nunca estructura**: `AUTO_INCREMENT` actual, `last_value` de secuencia,
  versión de extensión — excluidos de raíz.
- **Orden de columnas**: no es diff.
- **ENUM**: MySQL en el string de tipo; PostgreSQL como objeto de catálogo
  (`EnumTypeInfo.values` ordenados).
- **Cuerpos procedurales** (rutinas/vistas/triggers): sin diff semántico fiable — se
  normalizan (`DEFINER` fuera, whitespace colapsado) y se comparan "cambió/no cambió".
- **Renombrados**: **v1 no genera `RENAME` automático** (alto riesgo de mapear mal una
  columna). El diff naive los ve como DROP+CREATE; se agrega una heurística *advisory* —
  si un objeto eliminado tiene firma muy similar a uno recién creado, se marca
  `possible_rename_of: <nombre>` para que el operador lo note antes de aplicar el DROP.

## Clasificación destructiva (fail-closed, por ítem)

Calculada en el motor de diff — **nunca por regex sobre el SQL final**. Un ítem que no se
pueda demostrar aditivo/seguro se clasifica destructivo por defecto.

**Excluido del modo automático `all_except_destructive`:** cualquier `DROP` (tabla,
columna, índice, constraint, PK, FK, vista, rutina, trigger, secuencia, tipo, extensión,
evento); achicar un tipo (`is_narrowing`: menor longitud/precisión, `BIGINT→INT`,
`TEXT→VARCHAR`, quitar valores de ENUM); cambio de collation/charset; `DROP DEFAULT`;
cualquier ítem con `possible_rename_of`.

**Incluido (aditivo seguro):** `CREATE TABLE` nuevo, `ADD COLUMN` nullable/con default,
`CREATE INDEX`/`ADD CONSTRAINT`, widening de tipo (`_is_safe_widening`), objetos nuevos.

**Objetos procedurales** (vistas/rutinas/triggers/events), aunque sean "nuevos" (no
destructivos), llevan `requires_individual_review=true` y **nunca entran en `all` ni
`all_except_destructive`** — solo vía `mode=custom`, para que el operador vea el cuerpo
exacto antes de confirmarlo.

## Modelo de datos (persistido, anti-TOCTOU)

La comparación se **persiste** (no es efímera por token del cliente): el servidor sigue
siendo la única fuente de verdad del SQL a ejecutar.

- `SchemaComparison` (`app/models/schema_comparison.py`): `source_database_id`,
  `target_database_id`, `source_engine`/`target_engine`, `source_fingerprint`/
  `target_fingerprint` (hash del snapshot normalizado), `cross_flavor_warning`,
  `scope_note`, `expires_at` (TTL).
- `SchemaComparisonItem` (`app/models/schema_comparison_item.py`): `object_type`,
  `object_name`, `change_type` (`new`/`modified`/`dropped`), `phase`, `sql` (DDL exacto),
  `risk_flags` (JSON), `down_sql`/`down_confirmed`, `execution_status`/`execution_error`/
  `executed_at` (solo si se ejecutó vía Opción B).

**Anti-TOCTOU:** antes de adoptar/ejecutar, el target (y en algunos casos el source) se
**re-snapshotea** y se recompara el fingerprint contra el guardado. Si difiere → `409`
"el esquema cambió; recalcula". No hay `force` para saltear esto (a diferencia de la
cuarentena, que sí lo tiene).

## Endpoints

> Todos requieren sesión de administrador (`AdminDep`). 🔌 = tocan el motor destino
> (solo lectura salvo `adopt`/`execute`).

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/api/v1/schema-comparisons` 🔌 | Body `{source_database_id, target_database_id}`. Valida motor compatible, snapshotea ambas BDs, corre el diff puro, renderiza el DDL para el motor del TARGET y persiste cabecera + ítems + fingerprints. Rate limit 10/min. |
| `GET` | `/api/v1/schema-comparisons/{id}` | Resumen: `counts` (object_type → change_type → nº de objetos), `has_destructive`, `cross_flavor_warning`, `scope_note`, `expired`. |
| `GET` | `/api/v1/schema-comparisons/{id}/items` | Detalle paginado con el **DDL exacto** (dry-run/preview obligatorio — nunca se ejecuta sin haberlo mostrado). Filtra por `object_type`/`change_type`. |
| `POST` | `/api/v1/schema-comparisons/{id}/adopt` 🔌 | **Opción A.** Body `{selected_item_ids, name, description?, execute_immediately}`. `422` si el target no tiene `model_id`. Reusa `ModelMigrationController.create_migration` (checksum, autoversión). `execute_immediately=true` aplica por el camino normal (`ManagedMigrationController.apply`, con todos sus guards). Rate limit 3/min. |
| `POST` | `/api/v1/schema-comparisons/{id}/execute-preview` | Resuelve un `mode`/selección de Opción B **sin ejecutar nada**: devuelve las sentencias exactas + el `confirm_token` a reenviar. Solo lectura (no toca el motor). Ver [El `confirm_token`](#el-confirm_token-opción-b). |
| `POST` | `/api/v1/schema-comparisons/{id}/execute` 🔌 | **Opción B.** Body `{mode: all\|all_except_destructive\|custom, selected_item_ids?, confirm_target_name, confirm_token}` + query `force` (cuarentena). `409` si el target TIENE `model_id` (usar `adopt`). Ejecuta con `MigrationRunner.execute_adhoc` (sin Alembic, sin tabla de versión). Rate limit 3/min. |

### El `confirm_token` (Opción B)

Hash SHA256 del conjunto **exacto** a ejecutar: `target_database_id + target_engine +
lista ordenada de (sql, risk_flags)`. Se recalcula server-side sobre el `sql` real de
cada ítem — nunca se confía en SQL que reenvíe el cliente. El algoritmo es
`SchemaComparisonController.execution_token(...)`.

**El cliente NUNCA debe calcular este token por su cuenta.** Reproducirlo requeriría
replicar `_resolve_mode` (paginar TODOS los ítems y filtrar por `risk_flags` exactamente
como lo hace el servidor) y el formato EXACTO de serialización JSON (orden de claves,
separadores) — ambos son detalles de implementación que pueden cambiar. Por eso existe
`POST .../execute-preview`: recibe el mismo `{mode, selected_item_ids?}` que `execute`,
devuelve las sentencias resueltas + el `confirm_token` listo para reenviar tal cual en
`POST .../execute`. Flujo esperado del cliente: `execute-preview` → mostrar
confirmación al usuario → `execute` con el token recibido.

## Flujo típico

1. `POST /schema-comparisons` con `source_database_id` (referencia) y
   `target_database_id` (a modificar).
2. `GET /schema-comparisons/{id}` para el resumen (¿hay destructivos? ¿warning
   cross-flavor?).
3. `GET /schema-comparisons/{id}/items` para el DDL exacto por ítem (paginado).
4. Según si el target tiene blueprint:
   - **Con blueprint** → `POST .../adopt` con los `selected_item_ids` elegidos
     (`execute_immediately` opcional).
   - **Sin blueprint** → `POST .../execute` con `mode` + confirmaciones.

## Reutilización de infraestructura existente

- **Opción A** reusa `ModelMigrationController.create_migration` (checksum,
  autoasignación de versión, `_bump_model_version`) — la versión nace `is_baseline=true`
  y `reviewed=execute_immediately` (si se difiere la aplicación, el gate R1 la protege
  hasta que un admin la apruebe; si se aplica de inmediato, la selección explícita del
  admin ES la revisión).
- **Opción B** usa `MigrationRunner.execute_adhoc` (nuevo, ligero): reusa conexión
  AUTOCOMMIT, advisory lock por BD y `map_driver_error`, pero **no** usa Alembic ni toca
  `_gw_v_{slug}`/`database_migration_history` (esa FK es NOT NULL hacia
  `model_migrations`; el resultado por sentencia se persiste en
  `schema_comparison_items`).
- El `down_sql` se infiere con precisión donde el diff conoce el estado "antes" exacto
  (mejor que `RollbackGenerator`, que no lo tiene); se auto-confirma solo si **todo** el
  conjunto seleccionado es claramente reversible.

## Limitaciones conocidas de v1

- **PostgreSQL: el diff solo cubre el schema `public`.** Es una limitación preexistente
  de `_inspect_schema()` (no nueva de esta feature). Se avisa explícitamente con el campo
  `scope_note` en la respuesta y aquí: objetos en otros schemas quedan fuera del diff
  silenciosamente si no se lee ese campo.
- **No hay `RENAME` automático.** Un rename se ve como DROP+CREATE (con
  `possible_rename_of` como advertencia heurística no autoritativa).
- **Cuerpos procedurales**: el diff es "cambió/no cambió", nunca diff semántico de
  lógica interna.
- **Adoptar (Opción A) una rutina/trigger de MySQL/MariaDB con cuerpo `BEGIN...END`
  puede FALLAR al aplicarse** — hallazgo confirmado empíricamente en la verificación
  e2e (Fase 7), **no arreglado en este plan** (ver detalle abajo). **Workaround:**
  adoptar esos objetos vía Opción B (`/execute`, `mode=custom`), que no tiene este
  problema, o editar manualmente el `up_sql` de la versión ya creada antes de aplicarla.
- **`ALTER TYPE ... ADD VALUE` (PostgreSQL, enums)** no es siempre transaccional según
  la versión — el valor agregado no puede usarse en la misma transacción en la que se
  agregó; no es un problema aquí porque cada sentencia de Opción B corre en su propia
  transacción AUTOCOMMIT.
- **`DROP FUNCTION`/`DROP ROUTINE` sin firma** (best-effort): puede fallar ante
  sobrecarga de funciones (overloads) con el mismo nombre y distinta firma.

### Detalle del hallazgo: `split_sql_statements` y rutinas MySQL/MariaDB

`app/services/db_admin/sql_dialect.py::split_sql_statements` (infraestructura de
[Plan 02](model-migrations.md), reusada por Opción A al generar la revisión de Alembic)
respeta el dollar-quoting de PostgreSQL (`$$...$$`) pero **no reconoce los bloques
`BEGIN...END` de MySQL/MariaDB** — cualquier `;` interno se trata como fin de sentencia.
Confirmado con un caso mínimo:

```pycon
>>> split_sql_statements("CREATE PROCEDURE sp_x() BEGIN UPDATE t SET x=x; END")
['CREATE PROCEDURE sp_x() BEGIN UPDATE t SET x=x', 'END']
```

Esto ya estaba **documentado como limitación aceptada** en el docstring del módulo desde
el Plan 02 ("deben subirse con cuidado, una por migración"), pero no se había verificado
contra un motor real hasta esta fase. El impacto concreto para este plan: **Opción A**
(que concatena el `up_sql` y lo pasa por Alembic → `split_sql_statements`) falla al
aplicar cualquier rutina/trigger MySQL/MariaDB con cuerpo multi-sentencia; **Opción B**
(`execute_adhoc`) NO tiene este problema porque ejecuta cada ítem ya renderizado como
sentencia completa, sin volver a partirlo. No se modificó `sql_dialect.py` en este plan
(el fix genérico — parsear anidamiento `BEGIN...END` con sus terminadores `END IF`/
`END CASE`/`END LOOP`/`END WHILE`/`END REPEAT` — es una pieza de infraestructura
compartida por TODAS las migraciones de blueprint, no solo por esta feature; tocarla
está fuera del alcance de esta fase y merece su propio plan + batería de tests dedicada).
El comportamiento observado es seguro (falla limpio, sin corrupción: la sentencia
partida es sintácticamente inválida y el motor la rechaza antes de crear nada), pero
bloquea un caso de uso legítimo — quedó verificado y documentado, no oculto.

## Seguridad

- Todo identificador derivado del diff pasa por `validate_identifier(..., dialect,
  allow_existing=True)` + `quote_identifier` — nunca se interpola DDL crudo del motor
  origen sin revalidar, ni siquiera viniendo de introspección "de confianza".
  `_strip_definer_clause` se aplica a todo DDL procedural capturado.
- Confirmación verificable (hash) en Opción B, no booleana; anti-TOCTOU con recompute de
  fingerprint dentro del advisory lock.
- Auditoría fail-closed (`audit.record_intent` antes de tocar el motor) para toda
  ejecución real (Opción A inmediata y Opción B), incluyendo si el target tenía
  blueprint al momento (para dejar constancia aunque el camino directo esté bloqueado).
- Errores de motor siempre enrutados por `map_driver_error`/`_clean_error` — nunca se
  reflejan host/usuario/SQL con literales al cliente.
- Guard de cuarentena (`status=error` → `409` salvo `force=true`) igual que `apply`.

## Verificación

- **Motor de diff puro** (sin motor real, 100% CI): `tests/test_schema_diff.py` — un
  caso por trampa de normalización (debe NO reportar diff) y un caso por tipo de cambio
  (DDL/flags esperados).
- **API con adapter mockeado** (SQLite + fake adapter): `tests/test_api_schema_comparisons.py`
  — creación, paginación de ítems, Opción A (adopt, confirmación de `down_sql`,
  `execute_immediately`), Opción B (los 3 modos, corte en el primer fallo, bloqueo con
  blueprint, anti-TOCTOU).
- **e2e contra motores reales** (MySQL 8 / MariaDB 11 / PostgreSQL 16, Docker):
  `scripts/verify_schema_diff_e2e.py` — **ejecutado, 219 checks / 0 fallos** en los 3
  motores (corrida combinada e individual, ambas limpias e idempotentes). Cubre:
  - Cero-diff entre BDs idénticas con nombres de constraint/índice/FK **distintos**
    (matching por definición) en los 3 motores.
  - Detección de cada tipo de cambio (tabla nueva, columna nueva/eliminada, narrowing,
    widening, índice nuevo, FK nueva en tabla existente, vista nueva, rutina nueva) con
    sus `risk_flags` esperados, en los 3 motores.
  - DDL renderizado ejecutado de verdad contra el motor real (no solo generado) en los
    3 modos de Opción B y en Opción A, confirmando columnas/tablas/vistas/rutinas
    presentes (o ausentes, para lo excluido) tras la ejecución.
  - Bloqueo de Opción B con blueprint asignado (409) y anti-TOCTOU de Opción A y B
    (409, modificando el target por fuera del gateway entre comparar y actuar).
  - El hallazgo de `split_sql_statements` + rutinas MySQL/MariaDB (arriba) como check
    explícito que confirma el comportamiento conocido, no como fallo silencioso.
- **Migración Alembic de la BD del gateway** (`schema_comparisons`/`schema_comparison_items`):
  verificada con el ciclo completo `upgrade head` → `downgrade -1` → `upgrade head` contra
  **MariaDB 11 real** (no solo SQLite). Encontró y corrigió un bug real: el `downgrade()`
  autogenerado soltaba explícitamente el índice de `comparison_id` (y de
  `source_database_id`/`target_database_id`) antes de `drop_table`, lo que MySQL/MariaDB
  rechaza porque el índice respalda la FK de la propia tabla (`Cannot drop index ...:
  needed in a foreign key constraint`). Se removieron esos `drop_index` explícitos
  (`op.drop_table` ya limpia índices y FKs de la tabla) — SQLite no detecta esta
  restricción, por eso pasó desapercibido en los tests automatizados. **Este mismo patrón
  aparece en varias migraciones anteriores del repo** (autogenerate lo emite por defecto);
  no se tocaron porque ya pueden estar aplicadas en otros entornos y corregirlas es un
  esfuerzo aparte, fuera del alcance de esta feature — vale la pena una auditoría dedicada
  del downgrade de todas las migraciones contra MySQL/MariaDB real en algún momento.

---

**Siguiente:** [Migraciones de blueprints](model-migrations.md) ·
[Adopción, reconciliación y snapshot](adoption-reconcile-snapshot.md)
