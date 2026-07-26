# API Reference v5 — Cierre de dependencias en la selección + reconciliación de aplicaciones parciales

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md),
> [`api-reference-v2.md`](api-reference-v2.md), [`api-reference-v3.md`](api-reference-v3.md) y
> [`api-reference-v4.md`](api-reference-v4.md). Documenta dos cambios de contrato que afectan
> pantallas YA IMPLEMENTADAS del módulo de schema-comparison y del panel de migraciones —
> no son features aisladas, son correcciones de un supuesto que la UI actual tiene mal.
>
> Mismo formato que v3/v4: **problema → qué debe pasar → escenarios → flujos → endpoints →
> ejemplos → interpretación visual**.
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie, errores,
> paginación) idénticas al documento original ([§3](api-reference.md#3-convenciones-de-la-api)).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## ⚠️ Corrección urgente sobre un documento anterior

[`docs/frontend/plan-schema-comparison.md`](frontend/plan-schema-comparison.md) (línea ~1558)
dice: *"El significado exacto de `phase` (orden de ejecución) se usa solo como metadato de
ordenamiento"*. **Eso ya no es correcto y nunca debió serlo del todo.** Si la UI actual ordena
o ejecuta las sentencias por `phase`, tiene un bug latente: hay combinaciones reales (una FK
que depende de una PK creada en otra fase, una vista que lee de otra vista, un `CHECK` sobre
una columna nueva) donde ordenar por `phase` produce una secuencia que el motor **rechaza**.
Ver la sección 1 para el contrato correcto.

---

## Índice

- [0. Los dos problemas que resuelve esta versión](#0-los-dos-problemas-que-resuelve-esta-versión)
- [1. `seq` es el orden; `phase` es solo una etiqueta](#1-seq-es-el-orden-phase-es-solo-una-etiqueta)
- [2. Grupos atómicos (`op_group`) y dependencias (`depends_on`)](#2-grupos-atómicos-op_group-y-dependencias-depends_on)
- [3. Cerrar una selección: `POST .../resolve-selection`](#3-cerrar-una-selección-post-resolve-selection)
- [4. `adopt` con `auto_resolve_dependencies`](#4-adopt-con-auto_resolve_dependencies)
- [5. Modos automáticos (`all`/`all_except_destructive`): poda en vez de fallo](#5-modos-automáticos-allall_except_destructive-poda-en-vez-de-fallo)
- [6. `plan_warnings`: avisos no bloqueantes](#6-plan_warnings-avisos-no-bloqueantes)
- [7. Aplicación parcial: qué pasa cuando un `apply` falla a mitad](#7-aplicación-parcial-qué-pasa-cuando-un-apply-falla-a-mitad)
- [8. `on_failure` en `apply`: la BD ya no queda "rota" sola](#8-on_failure-en-apply-la-bd-ya-no-queda-rota-sola)
- [9. Ver si una BD quedó a medias: `GET .../migrations/status`](#9-ver-si-una-bd-quedó-a-medias-get-migrationsstatus)
- [10. Reconciliar a mano: `POST .../migrations/reconcile-partial`](#10-reconciliar-a-mano-post-migrationsreconcile-partial)
- [11. El anti-patrón que hay que dejar de sugerir en la UI](#11-el-anti-patrón-que-hay-que-dejar-de-sugerir-en-la-ui)
- [12. PostgreSQL vs. MySQL/MariaDB: la pantalla de "aplicación parcial" casi no aplica a PG](#12-postgresql-vs-mysqlmariadb)
- [13. Tipos nuevos (referencia rápida)](#13-tipos-nuevos-referencia-rápida)
- [14. Matriz de errores](#14-matriz-de-errores)
- [15. Recomendaciones de UX](#15-recomendaciones-de-ux)

---

## 0. Los dos problemas que resuelve esta versión

**Problema A — la selección de ítems de un diff no se validaba.** El panel de
schema-comparison deja marcar ítems sueltos (`selected_item_ids`) para `adopt`/`execute
mode=custom`. Hasta ahora era posible marcar "la vista" sin "la tabla que lee", o el `CREATE`
de un índice redefinido sin su `DROP` previo (son dos sentencias del mismo cambio) — y el
error aparecía recién contra el motor real, a mitad de la migración, con la BD en un estado
intermedio. El backend ahora **valida el cierre de la selección** antes de tocar nada, y
expone un endpoint (`resolve-selection`) para que la UI arme una selección correcta *antes*
de pedirle confirmación al usuario.

**Problema B — un `apply` que fallaba a mitad dejaba a la BD en un estado que solo el admin
podía entender.** El panel de migraciones no tenía forma de mostrar "quedó a medias" ni de
resolverlo sin salir de la app (el flujo real terminaba en `stamp --force` + `rollback` desde
la consola, un anti-patrón que puede romper la BD todavía más — ver §11). Ahora existe un
endpoint dedicado y el propio `apply` puede auto-resolverlo.

Ninguno de los dos cambia el flujo de pantallas que ya existe (`plan-schema-comparison.md`,
`migration-rollback-frontend-api.md` siguen vigentes) — los **complementa**.

---

## 1. `seq` es el orden; `phase` es solo una etiqueta

```jsonc
// GET /schema-comparisons/{id}/items — un ítem
{
  "id": 507, "comparison_id": 42,
  "seq": 9,      // ← ORDENAR Y EJECUTAR por este campo, siempre
  "phase": 3,    // ← solo para agrupar/filtrar en la UI ("fase 3: aditivos"). NO ordena.
  "object_type": "foreign_key", "object_name": "child.fk_c_p", "change_type": "new",
  "sql": "ALTER TABLE `child` ADD CONSTRAINT `fk_c_p` FOREIGN KEY (`pcode`) REFERENCES `parent` (`code`)"
}
```

`phase` (1..9) es una etiqueta gruesa del pipeline conceptual (prerrequisitos → tablas →
aditivos → cuerpos → destructivos → …), útil para un filtro o un badge de color en la UI. Pero
el orden **real** de ejecución puede cruzar fases: una FK (fase 3) puede depender de una
`PRIMARY KEY` que se agrega en fase 4 sobre la tabla referida, y en ese caso la FK se ejecuta
**después**, aunque su `phase` sea menor. `seq` ya viene con eso resuelto — es el resultado de
un ordenador topológico, no de un simple `sort by phase`.

**Qué hacer en la UI:**
- Cualquier tabla/lista de `GET .../items` debe ordenar por `seq`, no por `phase` ni por
  `id`.
- Si hay agrupación visual por fase (por ejemplo, secciones colapsables "Prerrequisitos",
  "Estructura", "Destructivos"), seguirá funcionando para *mostrar*, pero el orden **dentro**
  y **entre** esas secciones al ejecutar tiene que respetar `seq`.

---

## 2. Grupos atómicos (`op_group`) y dependencias (`depends_on`)

Dos campos nuevos en `SchemaComparisonItemOut` (`GET .../items`):

```jsonc
{
  "id": 510, "seq": 12,
  "object_type": "index", "object_name": "t.ix_x", "change_type": "modified",
  "op_group": "index|t.ix_x|modified",
  "depends_on": []
},
{
  "id": 511, "seq": 13,
  "object_type": "index", "object_name": "t.ix_x", "change_type": "modified",
  "op_group": "index|t.ix_x|modified",   // ← MISMO grupo que el ítem anterior
  "depends_on": []
}
```

**`op_group` — la unidad de selección es el CAMBIO, no la fila.** Un índice/UNIQUE/CHECK/FK
*redefinido* (mismo nombre, definición distinta) se renderiza como **dos filas**: un `DROP` del
viejo y un `CREATE`/`ADD` del nuevo. Ambas comparten `op_group`. Si la UI permite tildar
checkboxes por fila suelta, hay que agruparlas: **si el usuario marca una fila de un
`op_group`, se marcan todas las de ese grupo** (o se deshabilita el checkbox individual y se
muestra un solo control por grupo). Enviar solo la mitad de un grupo en `selected_item_ids`
ahora se **rechaza con 422** (antes fallaba contra el motor).

```jsonc
{
  "id": 512, "seq": 20,
  "object_type": "view", "object_name": "v_reporte",
  "op_group": "view|v_reporte|new",
  "depends_on": ["table|clientes|new"]   // ← esta vista necesita que "clientes" esté seleccionada
}
```

**`depends_on` — lista de `op_group` que deben ir ANTES.** Solo lista dependencias que hay que
*crear en esta misma comparación*: algo que ya existe en el destino no aparece acá. Úsalo para:
- Deshabilitar o advertir si el usuario intenta desmarcar un ítem del que otros dependen.
- Mostrar un tooltip: "esta vista necesita la tabla `clientes` (fase 2)".
- Como alternativa más simple a construir esa UI, ver la sección 3: el backend lo resuelve por
  vos.

⚠️ **`op_group` puede venir `null`** en ítems de comparaciones creadas ANTES de esta versión
(no se recalcula retroactivamente sobre datos ya persistidos). Tratalo como "ítem suelto, sin
grupo atómico ni dependencias conocidas" — seleccionable individualmente, sin agrupar con
nada. No asumas que `null` es un error de datos.

---

## 3. Cerrar una selección: `POST .../resolve-selection`

Endpoint nuevo, **solo lectura** (no adopta ni ejecuta nada):

```
POST /api/v1/schema-comparisons/{comparison_id}/resolve-selection
Content-Type: application/json

{ "selected_item_ids": [507, 512] }
```

Respuesta (`data: ResolveSelectionOut`):
```jsonc
{
  "comparison_id": 42,
  "requested_item_ids": [507, 512],
  "resolved_item_ids": [501, 507, 512],   // ← EN ORDEN DE EJECUCIÓN, no en el orden enviado
  "added_item_ids": [501],
  "added_reasons": { "view|v_reporte|new": ["table|clientes|new"] },
  "added": [
    { "item_id": 501, "object_type": "table", "object_name": "clientes",
      "change_type": "new", "sql": "CREATE TABLE `clientes` (...)" }
  ],
  "total": 3
}
```

**Flujo recomendado:** el usuario tilda ítems libremente en la UI (checkboxes por
`op_group`, no por fila) → al pasar al paso de confirmación, llamar a `resolve-selection`
con lo tildado → si `added_item_ids` no está vacío, mostrar un aviso tipo *"se agregaron 1
sentencia(s) porque las que elegiste dependen de ellas"* con el detalle de `added` (para que
el usuario entienda el porqué, no solo el qué) → usar `resolved_item_ids` (ya en orden) como
el `selected_item_ids` real que se manda a `adopt`/`execute`.

Esto reemplaza el ciclo anterior de "intento → 422 → leo el error → agrego a mano → reintento".

⚠️ **`resolve-selection` no valida si la comparación expiró** (a diferencia de `adopt` y
`execute`, que sí lo hacen). Un resultado de este endpoint sobre una comparación vieja puede
parecer válido y aun así fallar al confirmar con `adopt`/`execute` por expiración — no lo
uses como sustituto de refrescar la comparación si pasó mucho tiempo entre que se creó y que
el usuario confirma.

---

## 4. `adopt` con `auto_resolve_dependencies`

```
POST /api/v1/schema-comparisons/{comparison_id}/adopt
{
  "selected_item_ids": [507, 512],
  "name": "Agregar vista de reportes",
  "auto_resolve_dependencies": false   // ← nuevo, default false
}
```

- **`false` (default, fail-closed):** si la selección no cierra, **422** con
  `public_context.missing_dependencies` (mismo shape que `added_reasons` de arriba) y
  `public_context.suggested_item_ids` (el resultado que hubiera dado `resolve-selection`).
  Si tu pantalla YA llama a `resolve-selection` antes de confirmar (§3), este 422 no debería
  aparecer nunca en producción — es una segunda barrera, no el flujo principal.
- **`true`:** el backend cierra la selección por su cuenta (mismo algoritmo que
  `resolve-selection`) y la incluye. La respuesta trae qué agregó:

```jsonc
// AdoptComparisonOut
{
  "comparison_id": 42, "model_id": 3, "version": "0012",
  "statements": 3, "executed": false,
  "migration": { /* ModelMigrationOut */ },
  "added_item_ids": [501],
  "plan_warnings": []
}
```

Si tu UX prefiere no mostrar el paso intermedio de `resolve-selection`, usar
`auto_resolve_dependencies: true` y mostrar `added_item_ids` en el mensaje de éxito ("se
agregaron N sentencias automáticamente") es válido — pero entonces el usuario no ve el
*porqué* de cada una (no viaja `added_reasons` en `AdoptComparisonOut`, solo los ids). Para
transparencia total, preferí el flujo de §3.

---

## 5. Modos automáticos (`all`/`all_except_destructive`): poda en vez de fallo

Este caso es distinto: no hay selección manual, así que no puede pedirse "agregá la
dependencia". El filtro por riesgo (`all_except_destructive` excluye lo `destructive`) puede
dejar fuera una tabla marcada como *posible rename* (heurística `possible_rename_of`) pero
dejar adentro sus índices (que no son destructivos por sí solos) — ejecutar el índice sin su
tabla fallaría. El backend ahora **poda transitivamente** eso, en vez de fallar:

```
POST /schema-comparisons/{id}/execute-preview
{ "mode": "all_except_destructive" }
```

```jsonc
// ExecutePreviewOut
{
  "comparison_id": 42, "target_database_id": 7, "mode": "all_except_destructive",
  "statements": [ /* ... las que SÍ se van a ejecutar ... */ ],
  "excluded_by_dependency": ["index|nueva_tabla.ix_x|new"],
  "plan_warnings": [],
  "confirm_token": "..."
}
```

**Mostrar `excluded_by_dependency` en el preview**, antes de que el usuario confirme — es
información que cambia lo que realmente va a pasar (menos sentencias de las que el modo
"debería" incluir a simple vista) y el usuario tiene que verla, no solo el conteo final. Mismo
campo en la respuesta de `POST .../execute`.

---

## 6. `plan_warnings`: avisos no bloqueantes

Aparece en `AdoptComparisonOut`, `ExecutePreviewOut` y `ExecuteComparisonOut`. Es una lista de
`{ "code": string, "message": string, "op_group": string | null }`. A diferencia de
`excluded_by_dependency` o del 422 de cierre, **esto no bloquea ni cambia lo que se ejecuta** —
son cosas que vale la pena que el usuario sepa:

| `code` | Qué significa |
|---|---|
| `create_and_drop_same_object` | El plan crea y borra el mismo objeto — casi siempre un rename que el sistema no auto-detectó como tal. |
| `destructive_without_rollback` | Hay cambios destructivos en el plan sin `down_sql`: la versión resultante no se podrá revertir automáticamente. |

Mostrar como banner informativo (no modal bloqueante) antes o después de confirmar, según tu
patrón de UX para avisos no críticos.

---

## 7. Aplicación parcial: qué pasa cuando un `apply` falla a mitad

Contexto que la UI necesita para no confundir al usuario: el DDL de MySQL/MariaDB hace commit
por sentencia (no es transaccional). Si una migración de 50 sentencias falla en la 10, la BD
queda **físicamente** con 10 cambios aplicados, pero el "ledger" de versión (que decide qué
dice `current_version`) recién se actualiza cuando la migración *entera* termina. Resultado:
`current_version` sigue mostrando la versión anterior, aunque la BD ya no esté 100% en ese
estado. Antes esto era invisible para la UI — ahora hay campos y un endpoint dedicados.

**PostgreSQL es distinto — ver §12: ahí esto casi nunca puede pasar.**

---

## 8. `on_failure` en `apply`: la BD ya no queda "rota" sola

```
POST /api/v1/managed-databases/{db_id}/migrations/apply?on_failure=auto
```

Query param nuevo, **default `auto`** (no hace falta que la UI lo mande explícitamente salvo
que quiera ofrecer el control al usuario):

| Valor | Comportamiento |
|---|---|
| `auto` (default) | Si falla a mitad, el backend deshace automáticamente lo aplicado — **pero solo si puede deshacerlo TODO**. Si lo logra, la BD queda limpia en su versión anterior y **no entra en cuarentena**. |
| `reconcile` | Igual que `auto`, pero si hay sentencias sin reverso las saltea y las reporta (en vez de no intentar nada). |
| `leave` | No deshace nada — comportamiento histórico (cuarentena + checkpoint, requiere intervención manual). |

`MigrationApplyOut` ahora trae `reconciliation` (`MigrationAutoReconcileOut | null`):

```jsonc
// apply?on_failure=auto, la migración 0008 falló en la sentencia 6 de 12
{
  "managed_database_id": 1, "from_version": "0007", "to_version": "0007",
  "applied_count": 0, "failed": true, "quarantined": false,   // ← false: se auto-resolvió
  "reconciliation": {
    "version": "0008", "attempted": true,
    "undone_count": 5, "statements_to_undo": 5,
    "fully_reconciled": true,
    "unconfirmed_reverses": [], "unreversible_statements": [],
    "error": null
  },
  "results": [ /* ... */ ]
}
```

**Mensaje sugerido para la UI** cuando `reconciliation.fully_reconciled: true`: *"La migración
0008 falló, pero el sistema deshizo automáticamente los cambios que había aplicado. La base de
datos volvió a la versión anterior (0007) sin intervención necesaria. Corregí la migración y
reintentá."* — **esto reemplaza cualquier mensaje de "la BD quedó en cuarentena, contactá a un
DBA"** para el caso feliz (que va a ser la mayoría).

Si `reconciliation.fully_reconciled: false` (algunas sentencias no tenían reverso seguro),
mostrar `unconfirmed_reverses`/`unreversible_statements` y dirigir a
`/migrations/reconcile-partial` con `force=true` (§10) o a corrección manual.

⚠️ **`reconciliation: null` con `failed: true` NO significa "no pasó nada".** Puede ser
cualquiera de tres casos distintos, y la UI no debe asumir que todo está bien solo porque no
hay objeto de reconciliación:
1. La migración falló en su **primera** sentencia — no había nada aplicado, no hay nada que
   reconciliar (el caso más benigno).
2. Se llamó con `on_failure=leave` explícito — la auto-reconciliación ni se intentó a
   propósito.
3. `on_failure=auto` (el default) pero había sentencias sin reverso seguro — el sistema
   **deliberadamente no intentó** una reconciliación parcial (a diferencia de `reconcile`,
   que sí la intenta salteando lo irreversible) y dejó la BD en cuarentena tal cual.

**La única forma confiable de saber cuál es** es consultar `GET .../migrations/status`
después: si `has_partial_application: true`, es el caso 2 o 3 (hay algo pendiente de
resolver); si `false`, es el caso 1 (nada que hacer, solo corregir la migración).

---

## 9. Ver si una BD quedó a medias: `GET .../migrations/status`

Dos campos nuevos:

```jsonc
{
  "managed_database_id": 1, "model_id": 3, "slug": "ecommerce",
  "current_version": "0007", "latest_available": "0009",
  "pending_count": 2, "pending_versions": ["0008", "0009"],
  "has_partial_application": true,
  "partial_application": [
    {
      "version": "0008", "model_migration_id": 20,
      "applied_statements": 6, "total_statements": 12,
      "reconcilable": true, "reason": null,
      "statements_to_undo": 6
    }
  ]
}
```

**Esto persiste solo si el `apply` que falló usó `on_failure=leave`, o si `auto`/`reconcile`
no pudieron reconciliar del todo** (sentencias sin reverso, con `reconcile` sin `force`, o
`auto` que directamente no lo intentó — ver el ⚠️ de la sección 8). Con el default `auto` y
sin sentencias irreversibles, un `apply` fallido ya se auto-resuelve en la misma llamada y
este estado no debería acumularse.

⚠️ **Puede haber MÁS de una entrada en `partial_application[]`** (migraciones distintas que
quedaron a medias en momentos distintos, sin reconciliar entre sí). `reconcile-partial` solo
resuelve **la de versión más alta** en cada llamada (es la última que se intentó aplicar) —
si hay varias, hace falta llamar al endpoint varias veces, una por versión, de la más alta a
la más baja. La UI debería iterar mostrando "Reconciliar 0009" primero y no ofrecer "0008"
hasta que la de arriba esté resuelta (el backend además lo exige: `confirm_version` debe
coincidir con la versión que reconciliaría esa llamada, que siempre es la más alta pendiente).

La UI del panel de migraciones debe:
- Mostrar un banner **persistente** (no un toast que desaparece) si `has_partial_application:
  true` — es más urgente que "hay pendientes".
- Por cada entrada de `partial_application`, si `reconcilable: true`, ofrecer el botón
  "Reconciliar" → §10. Si `reconcilable: false`, mostrar `reason` (texto explicando por qué
  no se puede automáticamente) y sugerir reintentar `apply` o resolución manual.
- **Bloquear/advertir sobre `rollback` mientras `has_partial_application: true`** — el
  backend ya lo rechaza con 409, pero es mejor UX deshabilitar el botón con un tooltip que
  explique por qué, en vez de dejar que el usuario lo intente y reciba el error.

---

## 10. Reconciliar a mano: `POST .../migrations/reconcile-partial`

```
POST /api/v1/managed-databases/{db_id}/migrations/reconcile-partial?confirm_version=0008&dry_run=true
```

Rate limit: **10/minute**. Query params:

| Param | Requerido | Descripción |
|---|---|---|
| `confirm_version` | sí | Debe ser la versión que aparece en `partial_application[].version` (doble intención). |
| `dry_run` | no | `true` devuelve los reversos exactos SIN ejecutar nada. **Recomendado llamarlo primero siempre.** ⚠️ Ver la nota de abajo: no siempre alcanza para previsualizar. |
| `force` | no | Procede aunque haya sentencias sin reverso — las saltea y reporta (409 sin esto). |

⚠️ **`dry_run=true` NO garantiza una previsualización si hay sentencias sin reverso.** El
backend valida `force` ANTES de mirar `dry_run`: si el plan tiene sentencias sin `down_sql`
(`unreversible_statements` no vacío) y no se pasó `force=true`, la llamada responde **409
incluso en modo dry-run** — no hay forma de "solo mirar" ese caso sin comprometerse de
antemano a saltear lo irreversible. Para la UI: si el primer intento con `dry_run=true` da
409 con `public_context.unreversible_statements`, el siguiente intento debe repetirse con
`dry_run=true&force=true` para ver el plan completo antes de decidir si se ejecuta de
verdad. No asumas que un dry-run exitoso implica ausencia de sentencias irreversibles —
puede ser que ya viniste con `force=true` desde el paso anterior.

Respuesta con `dry_run=true` (`data: MigrationReconcilePartialOut`):
```jsonc
{
  "managed_database_id": 1, "database_name": "db1", "server_id": 2,
  "version": "0008", "applied_statements": 6, "total_statements": 12,
  "statements_to_undo": 6, "unreversible_statements": [], "unconfirmed_reverses": [],
  "dry_run": true,
  "statements": [
    { "seq": 6, "sql": "DROP INDEX \"ix_z\"" },
    { "seq": 5, "sql": "ALTER TABLE \"t\" DROP CONSTRAINT \"fk_x\"" }
  ]
}
```

Con `dry_run=false` (default): mismo shape pero con `undone_count`, `failed`,
`fully_reconciled`, `remaining_applied_statements` y `results[]` (con `status`/`error`/
`execution_ms` por sentencia, igual que `apply`/`rollback`). Si algún reverso ejecutado no era
demostrablemente seguro (recrea una tabla borrada sin sus datos, por ejemplo),
`unconfirmed_reverses` lo lista — mostrarlo como aviso, no como error: el reverso SÍ se
ejecutó, solo no hay garantía de que restauró el estado exacto.

**Flujo de UI recomendado:**
```
Banner "aplicación parcial detectada" (desde §9)
  → botón "Reconciliar"
  → llamar con dry_run=true → mostrar la lista de "statements" como preview
      ("esto es lo que se va a deshacer")
  → confirmación explícita del usuario (operación destructiva, aunque compensatoria)
  → llamar con dry_run=false
      fully_reconciled: true  → éxito, refrescar status
      fully_reconciled: false → mostrar resultados parciales + ofrecer force=true si
                                 el motivo es "sentencias sin reverso"
  409 sin manifiesto → mensaje: "esta versión no se puede reconciliar automáticamente
                                 (motivo: <reason>); reconciliá el estado a mano y usá
                                 stamp?force=true"
```

---

## 11. El anti-patrón que hay que dejar de sugerir en la UI

Si en algún lado de la documentación de frontend o en mensajes de error propios de la app
existe la sugerencia de *"si `apply` falla, hacé `stamp --force` a esa versión y después
`rollback`"* — **hay que eliminarla**. Ese camino empeora el problema: el `stamp` afirma que
la migración se aplicó completa, así que el `rollback` posterior intenta deshacer sentencias
que nunca corrieron (fallan con "no existe") y el proceso muere a mitad, dejando un tercer
estado peor que el original. El backend ahora lo bloquea explícitamente con un 409 que explica
la secuencia correcta — pero si la UI tiene algún atajo o tooltip que insinúe ese flujo,
corregirlo para apuntar a `on_failure=auto` (§8) o `/reconcile-partial` (§10).

---

## 12. PostgreSQL vs. MySQL/MariaDB

**Esto es importante para no sobre-construir UI que nunca se va a activar en PostgreSQL.**
PostgreSQL ejecuta DDL **transaccional**: si una migración falla en la sentencia 10 de 50, el
propio motor deshace las 10 automáticamente al momento del fallo. No queda estado parcial, no
hay nada que reconciliar, `has_partial_application` prácticamente nunca será `true` para una
BD en PostgreSQL (solo si el admin fuerza sentencias específicas que PostgreSQL no permite
dentro de una transacción, un caso borde). MySQL/MariaDB, en cambio, hacen commit por
sentencia — ahí sí es el caso común que estas pantallas cubren.

**Para la UI:** todo lo de las secciones 7 a 10 sigue siendo válido y hay que implementarlo
igual (el panel de migraciones es agnóstico de motor), pero si querés priorizar dónde probarlo
manualmente o dónde puede aparecer con más frecuencia en producción, es en BDs MySQL/MariaDB.
No hace falta lógica condicional por motor en el frontend — el backend ya decide todo esto y
los campos (`has_partial_application`, `reconciliation`, etc.) simplemente no se activan en
PostgreSQL.

---

## 13. Tipos nuevos (referencia rápida)

```ts
// Ya existentes, con campos NUEVOS resaltados
interface SchemaComparisonItemOut {
  id: number; comparison_id: number;
  seq: number;              // orden de ejecución — ordenar/ejecutar por esto
  phase: number;            // SOLO informativo, no ordena
  object_type: string; object_name: string; change_type: "new"|"modified"|"dropped";
  sql: string; risk_flags: Record<string, unknown>;
  down_sql: string | null; down_confirmed: boolean;
  op_group: string | null;       // NUEVO — unidad de selección
  depends_on: string[];          // NUEVO — op_group requeridos antes
  execution_status: string | null; execution_error: string | null; executed_at: string | null;
}

// Nuevos
interface ResolveSelectionIn { selected_item_ids: number[]; }
interface ResolveSelectionOut {
  comparison_id: number;
  requested_item_ids: number[];
  resolved_item_ids: number[];      // orden de ejecución
  added_item_ids: number[];
  added_reasons: Record<string, string[]>;   // op_group -> op_group faltantes
  added: { item_id: number; object_type: string; object_name: string; change_type: string; sql: string }[];
  total: number;
}

interface PartialApplicationOut {
  version: string | null; model_migration_id: number;
  applied_statements: number; total_statements: number;
  reconcilable: boolean; reason: string | null;
  statements_to_undo: number;
}

interface MigrationAutoReconcileOut {
  version: string; attempted: boolean;
  undone_count: number; statements_to_undo: number;
  fully_reconciled: boolean;
  unconfirmed_reverses: Record<string, unknown>[];
  unreversible_statements: Record<string, unknown>[];
  error: string | null;
}

interface MigrationReconcilePartialOut {
  managed_database_id: number; database_name: string; server_id: number;
  version: string; applied_statements: number; total_statements: number;
  statements_to_undo: number;
  unreversible_statements: Record<string, unknown>[];
  unconfirmed_reverses: Record<string, unknown>[];  // reversos ejecutados no 100% seguros (revisar)
  dry_run: boolean;
  // dry_run=true:
  statements?: { seq: number; sql: string }[];
  // dry_run=false:
  undone_count?: number; failed?: boolean; fully_reconciled?: boolean;
  remaining_applied_statements?: number;
  results?: { seq: number; status: "applied"|"failed"; error: string | null; execution_ms: number | null }[];
}

// Campos NUEVOS en tipos existentes
interface AdoptComparisonIn {
  selected_item_ids: number[]; name: string; description?: string | null;
  execute_immediately?: boolean;
  auto_resolve_dependencies?: boolean;   // NUEVO, default false
}
interface AdoptComparisonOut {
  // ...campos existentes...
  added_item_ids: number[];              // NUEVO
  plan_warnings: { code: string; message: string; op_group: string | null }[];  // NUEVO
}
interface ExecutePreviewOut {
  // ...campos existentes...
  excluded_by_dependency: string[];      // NUEVO
  plan_warnings: { code: string; message: string; op_group: string | null }[];  // NUEVO
}
interface ExecuteComparisonOut {
  // ...campos existentes...
  excluded_by_dependency: string[];      // NUEVO
  plan_warnings: { code: string; message: string; op_group: string | null }[];  // NUEVO
}
interface MigrationStatusOut {
  // ...campos existentes...
  has_partial_application: boolean;              // NUEVO
  partial_application: PartialApplicationOut[];  // NUEVO
}
interface MigrationApplyOut {
  // ...campos existentes...
  reconciliation: MigrationAutoReconcileOut | null;  // NUEVO
}
```

---

## 14. Matriz de errores

```
422 — adopt/execute(custom) con selección que no cierra (sin auto_resolve_dependencies)
      public_context: { missing_dependencies, suggested_item_ids, would_add_item_ids }
422 — mode inválido en apply?on_failure= (solo auto|reconcile|leave)
409 — rollback con has_partial_application=true (aplicación parcial sin resolver)
409 — apply con un ROLLBACK parcialmente ejecutado (caso simétrico, poco común)
409 — reconcile-partial sin aplicación parcial que reconciliar
409 — reconcile-partial: la versión no tiene manifiesto (migración escrita a mano) →
      no reconciliable automáticamente; ver "reason" en el body
409 — reconcile-partial: hay sentencias sin reverso y no se pasó force=true
409 — stamp con checkpoint parcial pendiente (en cualquier dirección) sin force=true
422 — reconcile-partial: confirm_version no coincide con la versión parcial real
429 — reconcile-partial (10/minute), igual que apply/rollback/stamp
```

Todos estos 409/422 traen `public_context` con la data estructurada necesaria para la UI (no
depender de parsear `msg`).

---

## 15. Recomendaciones de UX

- **Selección de ítems por grupo, no por fila.** Si hoy el checkbox de la tabla de
  `GET .../items` es por `id`, migrarlo a por `op_group`: al tildar una fila, tildar todas las
  del mismo grupo (normalmente son 1 o 2 filas contiguas en `seq`).
- **Llamar a `resolve-selection` en el momento de pasar de "elegir ítems" a "confirmar"**, no
  solo cuando el 422 ya ocurrió. Es la diferencia entre una UI que informa proactivamente
  ("se van a incluir estas 2 sentencias más, por esto") y una que solo reacciona a errores.
  Es una llamada barata (solo lectura, no re-snapshotea nada).
- **El banner de aplicación parcial (§9) va arriba y persiste** — no es un toast. Mientras
  `has_partial_application: true`, el botón de rollback debería estar deshabilitado con
  explicación, no solo fallar al tocarlo.
- **El mensaje de éxito de `on_failure=auto` (§8) debe sonar tranquilizador, no alarmante.**
  El usuario ve "la migración falló" pero el sistema ya la resolvió — la redacción debe dejar
  claro que no hace falta ninguna acción manual, solo corregir el SQL y reintentar.
- **No agregues lógica condicional por motor (MySQL vs PostgreSQL) en el frontend** para estas
  pantallas (§12) — el backend ya decide cuándo aplican los campos nuevos.
