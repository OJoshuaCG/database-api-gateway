# Orden del catálogo de versiones y punta autoritativa

Addendum sobre `GET /database-models/{model_id}/migrations`. Añade el query param `order` y el
campo `is_latest` en `ModelMigrationSummary`. **No hay cambios incompatibles**: el default de
`order` conserva el comportamiento previo y `is_latest` es un campo nuevo.

## 1. El problema

El listado siempre ordenaba **ascendente** por número de versión, y `size` está topado por
`PAGINATION_MAX_SIZE` (default 50). Con un blueprint de 53 versiones, `page=1` devolvía las **50
más antiguas** y dejaba fuera las 3 de la punta — que son con las que se trabaja habitualmente.

Un cliente que quisiera abrir sobre lo reciente tenía que pedir una página cualquiera solo para
leer `pagination.pages` y recién entonces pedir la última: dos viajes, y el tamaño de la última
página es el resto de la división, así que ni siquiera trae un bloque completo.

Peor: sin forma de saber cuál es la punta, un cliente la infería del último ítem recibido. Con
el listado recortado eso es **falso** y quedaba al lado de acciones destructivas.

## 2. `?order=asc|desc`

```
GET /api/v1/database-models/{model_id}/migrations?order=desc&page=1&size=50
```

| Valor | Efecto |
|---|---|
| `asc` (**default**) | Orden numérico ascendente. Comportamiento previo, sin cambios. |
| `desc` | Orden numérico descendente: la punta es el primer ítem de `page=1`. |

El orden es **numérico**, no lexicográfico (`0010` es posterior a `0009`), igual que en el
resto del contrato de versiones. Un valor distinto de `asc`/`desc` responde `422`.

El default sigue siendo `asc` a propósito: es el sentido en que `apply` recorre la secuencia, y
cambiarlo rompería a cualquier consumidor que hoy asuma ese orden. `desc` es **opt-in**.

## 3. `is_latest` en `ModelMigrationSummary`

```jsonc
{
  "version": "0053",
  "name": "añade índice a pedidos",
  "is_latest": true,        // ← nuevo
  "deletable": true,
  "delete_requires_stamps": false
  // …
}
```

`true` en la versión de mayor número del blueprint. **Se resuelve sobre el catálogo completo, no
sobre la página**, así que es válido con cualquier `page`, `size` y `order`: en una página que no
contiene la punta, todos los ítems traen `false`.

**El cliente no debe inferir la punta del último ítem que recibió.** Ese era el bug: con
paginación, el último ítem de la página no es el último del blueprint.

### No confundirlo con `delete_requires_stamps`

Se parecen y no lo son. `delete_requires_stamps` sale de `_cached_versions_by_model`: las
versiones donde están **paradas las BDs gestionadas**. «Ninguna BD está más adelante» no
significa «es la punta» — un blueprint con versiones que nadie aplicó todavía tiene muchas
versiones sin BDs por delante y una sola punta.

### Coste

Una query extra por listado (`ORDER BY … DESC LIMIT 1` sobre la misma query ya filtrada, antes
del `LIMIT/OFFSET`). No hay N+1.

## 4. Uso recomendado en un cliente

Para abrir un catálogo largo sobre lo reciente y poder recorrerlo entero:

1. Pedir `?order=desc&page=1` → la primera página contiene la punta.
2. Mostrar la insignia de «más reciente» según `is_latest`, nunca según la posición en la lista.
3. Paginar con `pagination.pages` / `has_next` para llegar a las versiones antiguas.

Un cliente que necesite el conjunto **completo** (p. ej. para calcular qué versiones va a
capturar un apply masivo) tiene que iterar `has_next` hasta agotarlo: una sola página no alcanza
y quedarse con ella produce un cálculo incompleto sin ninguna señal de que lo es.
