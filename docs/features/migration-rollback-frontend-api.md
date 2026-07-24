# Confirmar y ejecutar rollback de migraciones — Documentación de API para Frontend

> El gateway soporta desde el Plan 02 el rollback de migraciones de blueprint, pero el
> frontend nunca implementó la pantalla para **confirmar el `down_sql`** de una versión
> (requisito previo obligatorio) ni el panel para **ejecutar el rollback** con la doble
> confirmación que exige el backend. Esta guía cierra ese hueco: contrato completo,
> flujo de navegación y manejo de errores.

---

## Resumen ejecutivo

Un admin que intenta revertir una BD gestionada puede recibir un **409** si alguna
versión del camino a revertir no tiene `down_sql` (SQL de reversión) confirmado. Hoy
**no existe pantalla** para ver qué versiones les falta ese rollback ni para
confirmarlo — el admin queda bloqueado sin ruta de salida dentro de la app, aunque el
backend soporta el flujo completo.

Este documento cubre dos pantallas nuevas y su conexión:

1. **Confirmar rollback de una versión** (nivel *blueprint*, no toca ningún motor):
   revisar/editar el `down_sql` sugerido por el gateway y guardarlo.
2. **Ejecutar el rollback** de una BD gestionada (nivel *BD real*, SÍ toca el motor):
   doble confirmación (repetir versión actual + elegir destino), operación destructiva.

El error 409 de la pantalla 2 debe guiar directo a la pantalla 1 para las versiones
específicas que faltan — el backend ahora expone esa lista **siempre**, no solo en
desarrollo (ver sección de errores).

---

## Envelope y autenticación (común a todo)

- **Envelope de éxito**: `{ "data": <payload>, "message": "..." }`. Los campos `null`
  se omiten del JSON.
- **Envelope de error**: `{ "detail": { "msg": "...", "type": "AppHttpException", ... } }`.
  El status HTTP real viene en el status code de la respuesta (404/409/422/429/500), no
  hay que parsearlo del body.
- **Autenticación**: sesión de admin por cookie (`AdminDep`). Sin sesión válida → 401 en
  cualquier endpoint.
- **Prefijo**: todos los paths cuelgan de `/api/v1`.
- **Single-admin**: no hay roles ni multi-tenant. No hace falta ocultar acciones por
  permisos de usuario.
- **Versiones**: strings de 4 a 10 dígitos (`"0008"`, `"0009"`...). Se comparan y
  ordenan **numéricamente**, nunca lexicográficamente (`"0010"` > `"0009"`).

---

## Endpoints

### 1. Status de migraciones de una BD gestionada

Punto de entrada del panel de rollback: da la versión actual (para armar
`confirm_version`) y el blueprint asociado (para enlazar a la pantalla de
confirmación).

```
GET /api/v1/managed-databases/{db_id}/migrations/status
```

Respuesta (`data`):
```jsonc
{
  "managed_database_id": 1,
  "model_id": 3,
  "slug": "ecommerce",
  "current_version": "0009",      // null = sin ninguna migración aplicada
  "latest_available": "0011",
  "pending_count": 2,
  "pending_versions": ["0010", "0011"]
}
```

Errores: `404` (BD inexistente).

---

### 2. Historial de versiones aplicadas (para poblar el selector de destino)

Usar este endpoint para saber **qué versiones están realmente aplicadas** en esa BD (y
por lo tanto son destinos válidos de rollback) — no inferirlo del blueprint.

```
GET /api/v1/managed-databases/{db_id}/migrations/history?page=1&size=20
```

Respuesta paginada (`data: MigrationHistoryOut[]`):
```jsonc
{
  "id": 55,
  "managed_database_id": 1,
  "model_migration_id": 20,
  "version": "0009",
  "applied_at": "2026-07-20T10:00:00Z",
  "status": "applied",           // "applied" | "failed"
  "error": null,
  "execution_ms": 120
}
```

Filtrar por `status: "applied"` para armar las opciones del selector "revertir hasta
la versión X" (deben ser versiones `< current_version`).

---

### 3. Listar migraciones del blueprint (ver cuáles tienen rollback confirmado)

```
GET /api/v1/database-models/{model_id}/migrations?page=1&size=20
```

Respuesta paginada (`data: ModelMigrationSummary[]`):
```jsonc
{
  "id": 12,
  "model_id": 3,
  "version": "0008",
  "name": "add_orders_table",
  "has_mysql_override": false,
  "has_postgresql_override": false,
  "has_rollback": false,          // ← señal para el badge / acción "Confirmar rollback"
  "checksum": "...",
  "kind": "schema",               // "schema" | "data"
  "is_baseline": false,
  "reviewed": true,
  "created_at": "2026-01-01T00:00:00Z"
}
```

---

### 4. Detalle de una migración (para ver el `down_sql` sugerido antes de confirmar)

```
GET /api/v1/database-models/{model_id}/migrations/{version}
```

Respuesta (`data: ModelMigrationOut`):
```jsonc
{
  "id": 12, "model_id": 3, "version": "0008", "name": "add_orders_table",
  "up_sql": "CREATE TABLE orders (...);",
  "up_sql_mysql": null, "up_sql_postgresql": null,
  "down_sql": null,                                  // null = aún no confirmado
  "down_sql_suggested": "DROP TABLE IF EXISTS orders;", // borrador auto-generado; puede ser null
  "translated": { "mysql": "...", "postgresql": "..." },
  "checksum": "...", "kind": "schema",
  "source_engine": null, "is_baseline": false, "has_non_portable": false,
  "reviewed": true,
  "created_at": "...", "updated_at": "..."
}
```

`down_sql_suggested` solo cubre operaciones aditivas simples. El admin debe poder
**editarlo** antes de confirmar, no solo aceptarlo. Si viene `null` (el gateway no supo
sugerir nada), mostrar el editor vacío.

---

### 5. Confirmar el `down_sql` — el endpoint que faltaba usar

Es el mismo PATCH de edición de migración (comparte body con otras ediciones, no es un
endpoint dedicado). Para este flujo se envía **solo** `down_sql`:

```
PATCH /api/v1/database-models/{model_id}/migrations/{version}
Content-Type: application/json

{ "down_sql": "DROP TABLE IF EXISTS orders;" }
```

Respuesta (`data: ModelMigrationOut` actualizado):
```jsonc
{ "data": { /* ...mismo shape del detalle, con down_sql ya seteado... */ },
  "message": "Migración actualizada." }
```

**Regla clave**: `down_sql` se puede confirmar/editar **siempre**, incluso si la
migración ya se aplicó exitosamente en alguna BD (a diferencia de `up_sql`, que se
congela tras una aplicación exitosa — 409 si se intenta cambiar). No hace falta ningún
guard adicional en el frontend para este campo.

Si el formulario llegara a tocar también `up_sql` (fuera de alcance de este flujo, pero
por si se combina a futuro): cambiar `up_sql` exige reenviar o limpiar (`null`) en el
mismo PATCH cualquier override (`up_sql_mysql`/`up_sql_postgresql`) existente, o
responde 409.

Errores: `404` (migración inexistente), `422` (version fuera de pattern o body
inválido).

---

### 6. Ejecutar el rollback real (operación destructiva)

```
POST /api/v1/managed-databases/{db_id}/migrations/rollback?confirm_version={X}&target_version={Y}
```

Rate limit: **10/minute**. Query params (no hay body):

| Param | Requerido | Descripción |
|---|---|---|
| `confirm_version` | sí | Debe ser **igual** a `current_version` (doble intención). 422 si no coincide. |
| `target_version` | no | Versión destino, debe ser **anterior** a la actual. Si se omite, revierte solo un paso. El rollback es **secuencial**: una sola llamada deshace todas las versiones entre la actual y el destino. |

Respuesta exitosa (`data: MigrationRollbackOut`):
```jsonc
{
  "managed_database_id": 1, "database_name": "db1", "server_id": 2,
  "from_version": "0009", "to_version": "0007", "target_version": "0007",
  "reverted_count": 2, "failed": false, "quarantined": false, "no_op": false,
  "reverted_versions": ["0009", "0008"],
  "results": [
    { "migration_id": 20, "version": "0009", "status": "applied", "error": null, "execution_ms": 120,
      "resumed": false, "resumed_from_statement": null, "statement_total": 12, "failed_at_statement_index": null },
    { "migration_id": 19, "version": "0008", "status": "applied", "error": null, "execution_ms": 80,
      "resumed": true, "resumed_from_statement": 3, "statement_total": 8, "failed_at_statement_index": null }
  ]
}
```

Semántica a reflejar en UI:
- **`failed: true`** — se detuvo en la primera versión que falló; mostrar el item de
  `results` con `status: "failed"` y su `error`.
- **`quarantined: true`** — la BD quedó en estado que exige `force=true` en el próximo
  `apply`. Mostrar aviso **persistente**, no ocultarlo.
- **`no_op: true`** — no había nada que revertir (ya estaba en la versión destino);
  informativo, no es error.
- **`results[].resumed: true`** — este intento retomó automáticamente desde un fallo
  parcial previo (checkpoint de sentencia); `resumed_from_statement` indica desde cuál.
  Útil para mostrar "reanudada desde la sentencia 3/8" en vez de "aplicando 0008" a secas.
- **`results[].failed_at_statement_index`** — si `status: "failed"`, la sentencia
  (1-based) en la que murió, de `statement_total`. Puede ser `null` si la migración no es
  elegible para checkpoint (datos-semilla, objetos no portables, o sentencias con estado
  de sesión) — en ese caso el fallo sigue siendo todo-o-nada, como antes. **No** se expone
  el SQL de la sentencia (puede contener secretos); solo el índice.
- Aplican los mismos campos (`resumed`, `resumed_from_statement`, `statement_total`,
  `failed_at_statement_index`) en `results[]` de `MigrationApplyOut`
  (`POST .../migrations/apply`), con la misma semántica.

---

## Errores de `rollback` y el campo `public_context`

```
409 — falta down_sql confirmado en el camino
422 — confirm_version no coincide con la actual, o target_version inválido/no anterior
409 — la BD no tiene ninguna migración aplicada (current_version es null)
429 — rate limit (10/minute)
```

Body del 409 por `down_sql` faltante:
```jsonc
{
  "detail": {
    "msg": "No se puede revertir: las versiones 0008 no tienen rollback (down_sql) confirmado. Confírmalo con PATCH en cada migración.",
    "type": "AppHttpException",
    "public_context": { "missing_down_sql": ["0008"] }
  }
}
```

**`public_context.missing_down_sql` viaja siempre, en cualquier entorno** (a diferencia
de `context`, que es debug info y solo aparece con `APP_ENV=development`). Es el campo
que el frontend debe leer para armar el flujo guiado: listar exactamente esas versiones
y enlazar a la pantalla de confirmación (endpoint 5) para cada una. No depender de
`context` para esta lógica — puede no estar presente en producción.

En `429`, mostrar mensaje de espera y **no reintentar automáticamente**.

---

## Flujo de navegación recomendado

```
Panel de rollback de la BD gestionada
  → GET .../migrations/status            (current_version, model_id)
  → GET .../migrations/history            (versiones aplicadas → opciones de target_version)
  → [usuario elige target_version + confirma explícitamente la versión actual]
  → Modal de confirmación destructiva (irreversible, ejecuta SQL real)
  → POST .../migrations/rollback?confirm_version=X&target_version=Y
      200 → Resumen del resultado (reverted_versions / failed / quarantined)
      409 (public_context.missing_down_sql) →
          Listado de migraciones del blueprint, resaltando esas versiones
          → por cada una: GET detalle → editar down_sql_suggested → PATCH confirmar
          → volver al panel de rollback y reintentar el POST
      422 → refrescar status (la versión actual pudo cambiar) y reintentar
      429 → mensaje de espera, sin reintento automático
```

---

## Cambios de backend hechos para habilitar este flujo

- `app/exceptions/AppHttpException.py` — nuevo parámetro `public_context: dict | None`,
  distinto de `context` (debug-only): se incluye **siempre** en la respuesta.
- `app/exceptions/HandlerExceptions.py` — `app_exception_handler` agrega
  `detail.public_context` sin condicionarlo a `APP_ENV`.
- `app/controllers/managed_migration_controller.py::rollback()` — el 409 de
  `missing_down_sql` ahora pasa `public_context={"missing_down_sql": missing}` además
  del `context` existente (que sigue siendo dev-only, por compatibilidad con logs).

Ningún otro `AppHttpException` del proyecto se vio afectado: `public_context` es
opcional y por defecto no aparece si no se pasa explícitamente.
