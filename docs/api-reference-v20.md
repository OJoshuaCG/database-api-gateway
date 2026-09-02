# Addendum — los dos relojes de una fila de lote

Cambio en `CloneBatchItemOut` (`GET /database-clones/batches/{id}/items` y el detalle del lote).
No hay migración: los cuatro valores ya estaban en la BD, el serializer devolvía dos de ellos.

## Qué cambia

| Campo | Antes | Ahora |
|---|---|---|
| `started_at` | el del **job**, en cuanto la fila tenía uno | el de la **fila** |
| `finished_at` | el del **job** | el de la **fila** |
| `job_started_at` | — | **nuevo**: cuando el worker reclama el job |
| `job_finished_at` | — | **nuevo**: cuando el job cierra |

`job_*` son `null` mientras la fila no se haya materializado en un job.

## Por qué

La fila **envuelve** al job: `started_at` se sella antes de `create_plan`
(`clone_batch_controller.py`, `_run_item`) y `finished_at` después de que el job cerró. Entre
`started_at` y `job_started_at` corren, por base y sin emitir un solo paso:

- `list_databases()` del origen y del destino,
- **`structural_snapshot(origen)` completo** en `create_plan`,
- **`structural_snapshot(origen)` completo otra vez** en `preview`,
- `list_table_stats` del origen, que es una consulta de catálogo **por tabla**.

El cliente resta `started_at`/`finished_at` para mostrar «cuánto tardó cada base». Con el
serializer anterior esa resta daba la duración del **job**, así que toda esa preparación caía
fuera y aparecía como un bloque «sin atribuir». En dos corridas reales del mismo lote de 5
bases, con cargas de trabajo muy distintas, ese bloque fue **idéntico (2 m 6 s)** — la firma de
un costo fijo por base, no de tiempo de cola.

`structural_snapshot` **no acepta parámetro de selección**: lee siempre todas las tablas,
vistas, rutinas, triggers y eventos, y el filtro por tipos se aplica después en memoria. Por eso
elegir «Solo tablas y datos» no movió ese costo ni un segundo.

## Compatibilidad

Un cliente que ignore los campos nuevos sigue funcionando, pero **la semántica de
`started_at`/`finished_at` cambió**: ahora incluyen la preparación, así que la duración por fila
que calcule será mayor que antes. Es el valor correcto — el anterior omitía trabajo real.

Para reproducir el comportamiento viejo exacto, restar `job_finished_at − job_started_at`.
