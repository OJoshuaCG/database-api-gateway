# Autor de una versión del blueprint

Addendum sobre las versiones de blueprint. Añade tres campos de autoría a
`ModelMigrationSummary` (listado, `GET /database-models/{model_id}/migrations`) y a
`ModelMigrationOut` (detalle, `GET /database-models/{model_id}/migrations/{version}`, y la
respuesta de `POST` y `PATCH`). **No hay cambios incompatibles**: son campos nuevos, aditivos y
nullables; nada existente cambia de forma ni de significado.

## 1. Los campos

```jsonc
{
  "version": "0007",
  "name": "añade índice a pedidos",
  "created_by_admin_id": 4,          // ← nuevo
  "created_by_username": "mlopez",   // ← nuevo
  "created_by_actor_type": "admin",  // ← nuevo: "admin" | "api_token"
  "created_at": "2026-09-30T10:00:00"
  // …
}
```

| Campo | Tipo | Significado |
|---|---|---|
| `created_by_admin_id` | `int \| null` | ID del usuario del gateway (o del token de API) que creó la versión. |
| `created_by_username` | `string \| null` | Su nombre **al momento de crearla**. Es una copia congelada: renombrar o borrar al usuario no lo cambia. |
| `created_by_actor_type` | `string \| null` | `admin` (una persona) o `api_token` (un agente vía token). Mismo vocabulario que `actor_type` del log de auditoría. |

El autor es el actor de la request que creó la versión, por cualquiera de los caminos: el
`POST` manual, la adopción de un diff desde una comparación, la versión que genera una
conversión de collation y las N versiones de un blueprint creado desde snapshot (todas llevan
al autor del snapshot).

**No hay FK al usuario, a propósito.** Si el usuario se borró, `created_by_admin_id` y
`created_by_username` siguen ahí: la UI puede mostrar "usuario eliminado (#4)" sin que la
versión pierda su historia.

## 2. `null` = autor desconocido

Los tres campos vienen en `null` cuando **no se sabe** quién creó la versión. Solo pasa con
versiones anteriores a este cambio que no se pudieron recuperar de la auditoría (§3). La UI
debería mostrarlo como "autor desconocido", **no** como un error ni como "sistema": no afirma
nada sobre quién fue. Las versiones creadas a partir de este cambio siempre traen autor.

El que decide es `created_by_actor_type`: si viene en `null`, el autor es desconocido. Cuando
viene informado, lo normal es que traiga también id y nombre, pero un registro histórico puede
traer solo uno de los dos; mostrá el que haya.

## 3. Versiones anteriores: backfill desde la auditoría

La migración de esquema que agrega las columnas (`c5e7a9b1d3f6`) recupera el autor de las
versiones existentes desde las entradas `migration.create` del log de auditoría, que nombran
el id de la versión. Solo atribuye cuando el blueprint de la entrada coincide con el de la
versión, nunca pisa un autor ya escrito, y ante varias entradas para la misma versión toma la
más antigua.

Quedan como **desconocidas**:

- versiones cuya entrada de auditoría no nombra el id (formato anterior);
- versiones cuya entrada se purgó o nunca se escribió (la auditoría es best-effort);
- las versiones de blueprints creados **desde snapshot** antes de este cambio: ese camino
  audita una sola entrada por blueprint, sin ids de versión.

## 4. Coste

Son columnas de la propia tabla de versiones: ni queries ni joins extra, en el listado ni en el
detalle.
