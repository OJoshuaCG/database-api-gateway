# Addendum — el alta ejecuta las migraciones en vez de declararlas

Cambio en `POST /managed-databases`. **Rompedor**, chico y deliberado.

## Qué cambia

| Campo | Antes | Ahora |
|---|---|---|
| `model_version` | se aceptaba y se escribía en el inventario | **422** `managed_database.model_version_not_writable` |
| `apply_migrations` | — | **nuevo**: aplica las migraciones del blueprint tras crear la BD |
| `target_version` | — | **nuevo**: versión objetivo inclusive; omitirla aplica hasta la última |
| `migration` (respuesta) | — | **nuevo**: desenlace de la migración, solo si se pidió |

Además, la ruta pasa a tener **`10/minute`**, el mismo cubo que `/provision` y `/migrations/apply`:
emite el mismo `CREATE DATABASE` que aquél y ahora además ejecuta migraciones, así que era la
puerta sin límite a una operación que sí lo tiene.

## Por qué `model_version` deja de aceptarse

Se escribía en la fila del inventario **sin tocar el motor**. La pantalla de migraciones lee la
versión del motor, así que una base creada «en la versión 0007» aparecía como «0 aplicadas, N
pendientes»: estaba vacía.

Y no era solo cosmético. Esa columna alimenta `_policy_flags`, que decide si una versión del
blueprint se puede borrar: declararla dejaba esa versión congelada como `in_use` **sin que
ninguna base la tuviera aplicada**. Lo mismo con `pending_count` del listado de bases del
blueprint.

Es exactamente el agujero que el `PATCH` ya había cerrado, con este argumento textual:

> «era escribible a ciegas por el cliente, sin confirmación y sin rastro de qué cambió, y esa
> caché es la que cualquier gate de promoción entre entornos tiene que leer».

El alta era el que quedaba abierto. Ahora `model_version` es **puramente derivada**: la escriben
`apply`, `rollback` y `stamp` releyendo el motor.

Se **rechaza** en vez de ignorarse porque quien la mandaba creía estar fijando el estado inicial,
y tragárselo en silencio lo dejaría creyendo lo mismo.

## Migración para un cliente existente

- ¿Querías **crear la base ya migrada**? → `apply_migrations: true` (+ `target_version` si querés
  una versión concreta). Exige `provision=true` y `model_id`.
- ¿Querías **registrar una base que YA está físicamente en esa versión**? → `POST
  /managed-databases/adopt`, donde `model_version` sí dispara un `stamp` real y por eso se
  conserva. La asimetría es correcta: `adopt` parte de objetos que existen; el alta los crea.
- ¿Solo querías **anotar** la versión? No hay reemplazo, y es a propósito: esa columna es una
  caché del estado real, no un campo libre.

## `stamp` no se ofrece en el alta

No es una omisión. Sobre una base que el gateway acaba de crear vacía, marcarla como migrada es
una mentira **irreversible por API**: la ruta de stamp exige una versión `^\d{4,10}$` y no expone
`purge`, así que no existe «stampear a cero». La única reparación sería borrar y recrear.

## El desenlace de la migración no cambia el código HTTP

El alta responde **201** aunque la migración falle, y el resultado va en `migration`. Si la BD se
creó y la migración no, la petición **no** fracasó: hay una base real en el servidor de un
tercero, y un 4xx sugeriría que no quedó nada. `migration.error_code` dice a qué endpoint volver
(`apply?force=true`, `reconcile-partial`) en vez de recrear la base.

**Nunca hay un DROP compensatorio.** Borrar la base porque falló la migración sería una acción
destructiva que nadie pidió, y rodearía el `confirm_name` que `DELETE` exige.
