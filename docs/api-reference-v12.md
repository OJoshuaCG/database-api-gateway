# API Reference v12 — Re-aprovisionamiento de BDs gestionadas

Addendum al contrato consolidado (`api-reference.md` §9). Ataca un agujero operativo concreto:
**una BD registrada en el inventario que nunca se creó en el motor no tenía forma de crearse**,
y toda la pantalla de migraciones sobre ella fallaba con un 404 opaco que no decía qué hacer.

De dónde sale ese estado: `POST /managed-databases` tiene `?provision=false` por default, así que
el alta deja la fila commiteada en `status=pending` sin tocar el motor. Ese estado era **inerte**
—nada del sistema lo leía como guard—, así que la BD era indistinguible de una real hasta que
algo intentaba conectarse.

---

## 1. `POST /api/v1/managed-databases/{db_id}/provision`

Rate limit **10/minute**. 🔌 Ejecuta `CREATE DATABASE` en el servidor destino.

Ejecuta el DDL que faltaba **sobre la fila existente**. Que sea sobre la fila y no un alta nueva
es el punto: borrarla y recrearla pierde el `id`, y con él las notas, el entorno, el blueprint
asignado y las filas de `database_migration_history` que lo referencian.

**No aplica las migraciones del blueprint** (eso sigue siendo `POST /{id}/migrations/apply`) y
**no otorga privilegios** (se asignan con `POST /server-users/{id}/grants`; en PostgreSQL el
`OWNER` nativo lo pone el propio `CREATE DATABASE`, que es propiedad, no un grant).

### Petición

Sin body. Un solo query param:

| Param | Default | Para qué |
|---|---|---|
| `allow_recreate` | `false` | Permite aprovisionar una fila que el inventario ya marca `active`. Es el caso "la borraron por fuera del gateway": sin el gesto explícito, un `CREATE` silencioso taparía ese borrado. |

### Respuesta — `200`

```jsonc
{
  "data": {
    "database": { /* ManagedDatabaseOut completo, releído tras el cambio de estado */ },
    "provisioned": true,          // false = convergencia por carrera, ver abajo
    "previous_status": "pending", // pending | error | active
    "charset": "utf8mb4",         // forma CANÓNICA del catálogo que viajó al DDL
    "collation": "utf8mb4_0900_ai_ci"
  },
  "message": "Base de datos creada en el motor."
}
```

**`provisioned: false` NO es un fallo.** El chequeo previo contra el motor es consejo, no
barrera: hay una ventana entre él y el `CREATE`. Si dos llamadas simultáneas sobre la misma fila
compiten, la que pierde recibe errno **1007** (MySQL/MariaDB) o SQLSTATE **42P04** (PostgreSQL),
y eso se trata como **éxito por convergencia** — la base existe y es la de esa fila; devolver 409
por un resultado correcto sería mentir. La UI debe distinguirlo en el mensaje, no en el tono.

### Errores

Todos los códigos viajan en **`public_context.code`**, que existe también en producción (a
diferencia de `context`, solo visible en `development`). Es el único canal fiable para elegir el
CTA de recuperación: **no matchees la prosa del mensaje**.

| HTTP | `public_context.code` | Cuándo | Qué ofrecer |
|---|---|---|---|
| 409 | `managed_database.exists_in_engine` | La BD ya existe físicamente. **No se emite DDL.** | Explicar que para traerla al inventario sin recrearla hay que quitar el registro (`DELETE`, sin `drop_remote`) y usar `POST /managed-databases/adopt`. Advertir que se pierde el `id` (notas, entorno, blueprint, historial). |
| 409 | `managed_database.quarantined_not_missing` | La fila está en `error` **y la BD existe**: ese `error` es cuarentena de migraciones, no un `CREATE` pendiente. | Enlazar a `reconcile-partial` o a `apply?force=true`. |
| 409 | `managed_database.already_active` | El inventario ya la marca `active`. | Botón «Recrear» que repite con `allow_recreate=true`. |
| 409 | `managed_database.archived` | La BD está archivada. **Sin escape**, ni con `allow_recreate`. | Sin acción: hay que reactivarla en el inventario primero. |
| 409 | (guard de identificador) | El nombre de la fila es una BD de sistema. | Sin acción automática. |
| 422 | (catálogo de charsets) | La combinación charset/collation de la fila ya no está habilitada. | Corregirla con `PATCH` antes de reintentar. |
| 404 | — | La BD gestionada no existe en el inventario. | — |
| 502 / 504 | — | El motor no responde. La fila queda en `error`. | Reintentar. |

**Las notas del operador sobreviven un fallo.** Este endpoint no pisa `notes`: agrega su
diagnóstico en una línea marcada con `[gateway]` y conserva el resto. (`POST /managed-databases`
sigue reemplazándolas, comportamiento que no cambió.)

`allow_recreate` **no es un `force` genérico**: solo levanta el guard de `active`. El nombre es
distinto a propósito — en el módulo de migraciones `force` es override de cuarentena y no saltea
guards, y reusar la palabra invitaba a confundir dos cosas que no lo son.

---

## 2. `database_exists` en el estado de migraciones

### `GET /api/v1/managed-databases/{db_id}/migrations/status`

Campo nuevo en `MigrationStatusOut`, **aditivo** (default `true`; un backend previo que no lo
mande deja la semántica intacta):

```jsonc
{
  "data": {
    "managed_database_id": 5,
    "model_id": 3,
    "slug": "whatsapp",
    "database_exists": false,     // ← NUEVO
    "current_version": null,
    "latest_available": "0003",
    "pending_count": 3,
    "pending_versions": ["0001", "0002", "0003"],
    "has_partial_application": false,
    "partial_application": []
  }
}
```

**Antes esto era un 404** ("El recurso solicitado no existe en el servidor destino"), que la SPA
pintaba como `ErrorState` sin lugar donde poner un CTA — y que además era indistinguible del 404
de "BD gestionada no encontrada en el inventario". Ahora es **200**: `status` es una lectura y
"la base no existe en el motor" es un estado que describir, no un fallo de la petición. Devolver
un error tiraba `pending_versions`, `slug` y `latest_available`, que es justo lo que hace falta
para decidir qué hacer.

**Con `database_exists: false` los contadores mienten si se pintan tal cual:**
`current_version` es `null` por **AUSENCIA** de la base (no por "todavía sin migraciones"), y
`pending_versions` lista **todas** las del blueprint porque ninguna pudo aplicarse. La UI tiene
que decir que la base no existe **antes** que "3 pendientes".

### `POST .../migrations/apply?dry_run=true`

Mismo campo en `MigrationApplyOut`, solo en la rama dry-run. El dry-run **no se bloquea** —es la
llamada de diagnóstico, mismo criterio que la cuarentena— pero fuerza `no_op: true`.

### Los cuatro endpoints que ejecutan

`apply` (real), `rollback`, `stamp` y `reconcile-partial` responden **409** con
`public_context.code = "managed_database.not_provisioned"` **antes** de tocar el motor.

Lo importante para la UI: **la BD NO queda en cuarentena**. Antes, un `apply` sobre una base
inexistente fallaba por conexión y el gateway la marcaba `status=error` con nota de "migración
fallida" — un diagnóstico falso que enmascaraba la causa real. Y el 409 de `rollback` decía "no
tiene ninguna migración aplicada para revertir", que es verdad para una base vacía y mentira para
una que no existe.

Recomendación de UI: con `database_exists: false`, deshabilitar los controles que tocan el motor
con el motivo en el `title` y mostrar el CTA de aprovisionamiento — el mismo gesto que la pantalla
ya hace para la cuarentena.

---

## 3. Nota de producto: el switch «Aprovisionar en el motor»

`?provision=false` **sigue existiendo** en la API: es el default fail-safe correcto para un
endpoint (no ejecuta DDL si no se lo piden) y sirve para scripting.

Lo que se quitó es su switch en el **formulario de alta de la SPA**, que nacía apagado. Era el
único productor de filas `pending` de todo el sistema, y ninguno de los casos que decía cubrir se
sostiene: para traer al inventario una base que ya existe está `adopt` —que verifica su
existencia, la deja `active` y marca `origin='adopted'`—, y el formulario exige un `server_id` de
un servidor ya cargado con credenciales, así que "todavía no tengo acceso al motor" no aplica.

Desde la SPA, **crear una base la crea en el motor**. Las filas históricas se recuperan con el
endpoint de la sección 1.
