# API Reference v15 — Eliminar una versión intermedia de un blueprint

Addendum al contrato consolidado (`api-reference.md`) para el módulo de migraciones de
blueprints. Continúa [`api-reference-v14.md`](api-reference-v14.md), que fijó el criterio
"aplicada hoy". Acá cambia **qué versiones se pueden borrar** y aparece un endpoint nuevo.

Ataca un límite duro: **solo se podía borrar la punta**. Una versión intermedia que ya no
describía nada útil no tenía forma de salir del blueprint.

---

## 1. Lo que cambia, en una tabla

| | Antes | Ahora |
|---|---|---|
| Qué versiones se pueden borrar | Solo la **punta** | **Cualquiera**, punta o intermedia |
| Qué bloquea el borrado | Alguna BD está en esa versión **o en una posterior** (`>=`) | Alguna BD está **exactamente** en ella (`==`) |
| Una BD **adelante** de la versión | Bloqueaba | **No bloquea**: se le mueve el puntero |
| Qué pasa con las versiones posteriores | — | **Bajan un escalón** (`0016`→`0015`, `0017`→`0016`…) |
| `block_reason: "not_tip"` | Existía | **Eliminado del vocabulario** |
| Respuesta del `DELETE` | `data: null` | Objeto con `renumbered` y `stamped` |

> **La edición no cambia.** `sql_frozen` y el 409 `model_migration.sql_frozen` conservan el
> criterio `>=` de v14. Los dos criterios **divergen a propósito**, y por eso `block_reason` ya
> no repite lo que dice `sql_frozen`: ver §6.

---

## 2. Qué hace exactamente el borrado

1. La versión desaparece del blueprint.
2. Las posteriores **bajan un escalón**. Las anteriores no se tocan.
3. A las BDs que están **adelante** se les mueve el puntero a la etiqueta nueva de la **misma**
   migración. Una BD en `0020` queda en `0019`, y `0019` es la migración que antes se llamaba
   `0020`: **no retrocede de esquema, sigue un renombre**.

**No se ejecuta ningún SQL del blueprint. No es un rollback.**

> ⚠️ **Esto hay que decirlo en la UI.** Las BDs que ya habían aplicado esa versión **conservan
> físicamente** sus objetos. Tras el renumerado la cadena del blueprint ya no los describe, y
> el borrado no los revierte. El plan (§3) los nombra en `warnings`; mostralos.

Mover el puntero **escribe dentro de cada base gestionada** (`UPDATE` de la tabla de versión de
Alembic, con conexión y advisory lock). No es una operación local del gateway, y por eso ese
caso exige confirmación.

---

## 3. `GET /database-models/{model_id}/migrations/{version}/delete-plan` — NUEVO

Preview. **No modifica nada**, pero abre conexión a cada BD del blueprint para leer su versión
en vivo: es lo que hace que el veredicto sea autoritativo y no la caché del listado.

```jsonc
{
  "data": {
    "model_id": 7,
    "version": "0015",
    "deletable": true,
    "renumber": [
      { "from_version": "0016", "to_version": "0015" },
      { "from_version": "0017", "to_version": "0016" }
    ],
    "stamp_plan": [
      {
        "managed_database_id": 3,
        "database_name": "clientes_prod",
        "server_id": 1,
        "from_version": "0017",
        "to_version": "0016"
      }
    ],
    "blockers": [],
    "unstampable": [],
    "partial_applications": [],
    "requires_confirmation": true,
    "confirm_token": "1772… .a91f…",
    "expires_at": "2026-08-28T13:42:00Z",
    "warnings": [
      "Las BDs 3 conservan FÍSICAMENTE los objetos que creó la versión 0015: …",
      "Se moverá el puntero de 1 BD(s): es una escritura sobre cada motor destino …"
    ]
  }
}
```

| Campo | Para qué sirve en la UI |
|---|---|
| `deletable` | Habilitar o no el botón. Es el veredicto **en vivo**. |
| `renumber` | Mostrar la re-etiquetación antes de confirmar. |
| `stamp_plan` | Listar las BDs a las que se les va a escribir. Cada ítem es una escritura remota. |
| `blockers` | Por qué no se puede (§5). |
| `unstampable` | BDs que quedarían en una etiqueta inexistente por un hueco en la numeración. |
| `partial_applications` | Aplicaciones a medias que bloquean. |
| `requires_confirmation` | Si es `false`, el `DELETE` **no** pide token. |
| `confirm_token` | Se reenvía al `DELETE`. `null` si no hace falta o si el plan está bloqueado. |
| `warnings` | Texto listo para mostrar. El primero no es opinable. |

---

## 4. `DELETE /database-models/{model_id}/migrations/{version}`

Gana el query param `confirm_token`, **obligatorio solo si el plan implica mover punteros**.

```
DELETE /database-models/7/migrations/0015?confirm_token=1772….a91f…
```

**Compatibilidad**: borrar la punta de un blueprint sin BDs adelante sigue funcionando sin
token, igual que antes. Un cliente que no se actualice conserva ese caso.

La respuesta dejó de ser vacía:

```jsonc
{
  "data": {
    "model_id": 7,
    "version": "0015",
    "renumbered": [
      { "from_version": "0016", "to_version": "0015" },
      { "from_version": "0017", "to_version": "0016" }
    ],
    "stamped": [
      { "managed_database_id": 3, "database_name": "clientes_prod", "server_id": 1,
        "from_version": "0017", "to_version": "0016" }
    ]
  },
  "message": "Migración eliminada."
}
```

Usala para refrescar la lista de versiones **y** las versiones que la SPA muestre de cada BD:
las de `stamped` cambiaron de número.

### El token está atado al estado del parque

No es solo anti-replay: el `confirm_token` incluye una huella de la versión en la que estaba
**cada BD** cuando se pidió el plan. Si alguna se movió en el medio (un `apply` concurrente),
el token deja de verificar y el `DELETE` responde **422**. Es correcto: el plan congelado ya no
describe la realidad. La UI debe volver a pedir `delete-plan`.

TTL de 2 minutos → **410** si venció.

---

## 5. Códigos de error

Todos en `public_context.code`, que se envía en todos los entornos (a diferencia de `context`,
visible solo en `development`).

| HTTP | `public_context.code` | Cuándo | Qué ofrecer |
|---|---|---|---|
| 409 | `model_migration.version_in_use` | Alguna BD está parada **exactamente** en esa versión. | Nombrar esas BDs (`blocking_databases`) y ofrecer moverlas con apply o rollback. |
| 409 | `model_migration.unreadable_databases` | No se pudo leer la versión de alguna BD. **Fail-closed.** | «Reintentar». Es un problema de acceso a esa BD, no del blueprint. |
| 409 | `model_migration.renumber_confirmation_required` | El borrado mueve punteros y no llegó `confirm_token`. | Pedir `delete-plan` y mostrar el diálogo de confirmación. Trae `stamp_plan`. |
| 422 / 410 | *(del token)* | Token que no corresponde al plan congelado / vencido. | Volver a pedir `delete-plan` y rehacer la confirmación. |
| 409 | `model_migration.renumber_stamp_failed` | Falló el re-stamp de una BD, **o** falló el renumerado local con punteros ya movidos. En los dos casos **el blueprint no se modificó**. | Mostrar `compensated`: si es `true`, todo volvió a su lugar y se puede reintentar. Si es `false`, `left_moved` lista **solo** las BDs que quedaron mal marcadas (con `from_version` y `to_version`) — esas requieren un `stamp` manual antes de reintentar. |
| 409 | `model_migration.renumber_target_missing` | Una BD quedaría en una etiqueta inexistente (hueco en la numeración justo debajo de donde está parada). | Explicar el hueco: `unstampable_databases` trae `current_version` y `missing_target`. |
| 409 | `model_migration.affected_partial_application` | Alguna versión afectada tiene una aplicación a medias. | Enlazar a `reconcile-partial` de esa BD. |

Los que nombran BDs traen `version` y `blocking_databases[]` con `managed_database_id`,
`reason` y —cuando aplica— `current_version`.

### `reason`

| `reason` | Significa |
|---|---|
| `in_use` | Está **exactamente** en esa versión. Es el motivo del borrado. |
| `still_applied` | Está en esa versión o en una posterior. Es el motivo de la **edición** (`sql_frozen`). |
| `unreadable` | No se pudo leer su versión (motor caído, base sin aprovisionar, credencial rota, o un puntero no numérico). |
| `unknown_database` | Historial contra una BD que ya no está en el inventario. |
| `unknown_blueprint` | No se pudo resolver el blueprint. |

El `message` **nunca** transcribe el error del motor (puede llevar host, usuario o fragmentos de
sentencia). No intentes parsearlo: clasificá por `code` y `reason`.

---

## 6. Banderas del listado y del detalle

`GET /database-models/{id}/migrations` y `.../migrations/{version}`:

| Campo | Cambio |
|---|---|
| `sql_frozen` | **Sin cambios** (criterio `>=`). |
| `deletable` | Ya **no** exige ser la punta. Ahora es "ninguna BD parada exactamente acá y sin aplicación parcial". |
| `block_reason` | `"not_tip"` **eliminado**. Valores: `"in_use"`, `"partial"`, `null`. |
| `delete_requires_stamps` | **NUEVO**. `true` = borrarla implicaría escribir en el motor de alguna BD. |

> **`deletable` y `sql_frozen` ya no se mueven juntos**, y no es un bug. Una versión con una BD
> **adelante** tiene `sql_frozen: true` (describe lo que ya corrió allí) y `deletable: true` (el
> borrado le mueve el puntero). Si la UI derivaba una de la otra, hay que separarlas.

`delete_requires_stamps` sale de la **caché** del inventario: es una pista para elegir el
diálogo de confirmación, **no un veredicto**. El autoritativo es `delete-plan`. Puede pasar que
el listado ofrezca el botón y el plan después lo rechace — la divergencia es siempre en esa
dirección, nunca al revés.

---

## 7. Flujo recomendado para la SPA

```
1. El usuario pulsa «Eliminar» en una versión con deletable: true
2. GET .../{version}/delete-plan
   ├─ deletable: false        → mostrar blockers y salir
   ├─ requires_confirmation: false → DELETE directo (sin token)
   └─ requires_confirmation: true  → diálogo con renumber, stamp_plan y warnings
3. DELETE .../{version}?confirm_token=…
   ├─ 200 → refrescar versiones Y las versiones mostradas de las BDs de `stamped`
   ├─ 422/410 → el parque cambió: volver al paso 2
   └─ 409 → clasificar por public_context.code (§5)
```

> El intento queda **auditado antes** de que se toque un solo motor (`migration.delete` con
> `touched_engine: true`), así que un renumerado que falla a mitad deja rastro de qué punteros
> se iban a mover. El éxito escribe una segunda entrada con el resultado.

El diálogo de confirmación **debe** mostrar los `warnings` del plan. El primero dice que las
BDs conservan físicamente los objetos de la versión que se borra, y es la consecuencia que el
operador tiene que entender antes de confirmar.
