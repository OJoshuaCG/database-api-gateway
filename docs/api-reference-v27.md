# Cuántas BDs tienen aplicada cada versión del blueprint

Addendum sobre `GET /database-models/{model_id}/migrations`. Añade el campo
`applied_database_count` en `ModelMigrationSummary`. **No hay cambios incompatibles**: es un
campo nuevo, aditivo; nada existente cambia de forma ni de significado.

## 1. El campo

```jsonc
{
  "version": "0007",
  "name": "añade índice a pedidos",
  "applied_database_count": 3,   // ← nuevo
  "sql_frozen": true,
  "deletable": false
  // …
}
```

| Campo | Tipo | Significado |
|---|---|---|
| `applied_database_count` | `int`, nunca `null`, `>= 0` | Cuántas BDs gestionadas del blueprint tienen esta versión **aplicada hoy**. |

"Aplicada" es la conjunción de dos condiciones, por BD:

1. hay una fila de historial de esa versión con `status = applied` (y dirección no `down`), **y**
2. la versión actual de la BD (cacheada en el inventario) **alcanza** esta versión (`>=`).

Una versión sin BDs, solo con intentos fallidos o revertida en todas sus BDs trae `0`.

## 2. Por qué no se deriva en el cliente

El cliente ve la versión **declarada** de cada BD (`model_version`), y declarada no es
aplicada: `stamp`, la adopción de una BD existente y un apply que arrancó en una versión
intermedia mueven el puntero **sin que la migración haya corrido** en esa BD. Contar BDs por
`model_version >= version` sobreestima; solo el backend tiene el historial para cruzarlo.

## 3. Es un dato, no una regla

Sirve para mostrar ("aplicada en N BDs"). **No lo uses para decidir** si una versión se puede
editar o borrar: para eso siguen `sql_frozen`, `deletable` y `block_reason`, que el backend
decide con más insumos (aplicaciones parciales, posición exacta de cada BD). `count > 0` y
`sql_frozen` coinciden hoy salvo por las parciales, pero esa equivalencia no es contrato.

## 4. Frescura y coste

Sale de la **caché del inventario**, igual que `sql_frozen`: es la misma lectura, sin queries
ni conexiones extra. Si la caché quedó atrasada respecto del motor, el número también. Solo
viaja en el listado; el detalle (`ModelMigrationOut`) no lo trae.
