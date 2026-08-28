# API Reference v16 — Proyectos: agrupación de blueprints

Addendum al contrato consolidado (`api-reference.md`). Cubre la entidad **Project**, que
agrupa blueprints (`database_models`) vía una relación **N:M**. Un blueprint puede estar en
varios proyectos, en uno, o en ninguno.

**Este addendum se escribe después de que el backend ya estaba implementado**, así que
describe lo que hay, no lo que se planea. Todos los endpoints existen y están cubiertos por
tests.

> **Códigos de error nuevos en esta entrega.** Las siete excepciones del módulo antes viajaban
> solo en `context`, que el gateway expone **únicamente en `development`**. En producción el
> `missing_model_ids` del 422 —el dato que hace utilizable ese error— desaparecía. Ahora todas
> llevan `public_context.code`, que se envía en todos los entornos. Si algún prototipo leía
> `context`, hay que moverlo a `public_context`.

---

## 1. Qué es un proyecto, y qué NO es

Un proyecto es **nombre + descripción + una lista de blueprints**. No tiene servidor, ni
credenciales, ni versión, ni entorno. **No toca ningún motor de base de datos.** Es
organización pura.

### La regla dura, que la UI tiene que comunicar

> **Borrar un proyecto NO borra blueprints.** Borra la entidad y sus vínculos. Los blueprints
> quedan intactos, con sus migraciones y sus bases de datos.

Está implementada en tres capas del backend y tiene tests propios, en los dos sentidos. Es
deliberada: un blueprint es el esquema que replican N bases de datos **reales con datos
reales**, y que un agrupador pueda arrastrarlas sería pérdida de datos causada por una
operación de organización.

**Consecuencia para el diseño:** el `DELETE` de un proyecto **no** es una operación
destructiva y **no pide confirmación por nombre**, a diferencia de los borrados reales del
gateway. Tratarlo con el mismo ceremonial (re-tipear el nombre, `confirm_token`) le enseña al
operador que todo es peligroso y le quita valor a la fricción donde sí hace falta. Un
`confirm()` simple alcanza.

El mensaje del 200 ya trae el conteo listo para mostrar:
`"Proyecto eliminado. 3 blueprint(s) desvinculado(s); ninguno fue borrado."`

---

## 2. Envelope (igual que el resto de la API)

Éxito:

```jsonc
{ "data": …, "message": "…", "pagination": { … } }
```

`message` y `pagination` **se omiten** cuando no aplican (no llegan como `null`).

Error:

```jsonc
{ "detail": { "msg": "…", "type": "AppHttpException", "public_context": { "code": "…" }, "loc": { … } } }
```

**Clasificá siempre por `detail.public_context.code`, nunca por la prosa de `msg`.**
`detail.context` existe solo en `development`: no lo uses.

---

## 3. Los nueve endpoints

Todos exigen sesión de admin. Ninguno tiene rate limit propio.

### 3.1 `GET /api/v1/projects` — listado **paginado**

Query params: `?page=1&size=20` (`size` tope según `PAGINATION_MAX_SIZE`).

```jsonc
{
  "data": [
    {
      "id": 1,
      "name": "Clientes Retail",
      "description": "Blueprints de las tiendas.",
      "blueprint_count": 3,
      "created_at": "2026-08-22T10:00:00Z",
      "updated_at": "2026-08-22T10:00:00Z"
    }
  ],
  "pagination": { "page": 1, "size": 20, "total": 7, "pages": 1, "has_next": false, "has_prev": false }
}
```

`blueprint_count` se calcula con **una sola query para toda la página**: podés mostrarlo en la
tabla sin costo por fila.

### 3.2 `POST /api/v1/projects` → **201**

```jsonc
{
  "name": "Clientes Retail",          // requerido, 1–150, se le hace trim
  "description": "…",                 // opcional, ≤ 5000
  "model_ids": [4, 9]                 // OPCIONAL: vincula en el mismo alta
}
```

- `name` es **único** y se le recortan los espacios de los extremos. Solo-espacios → 422 de
  validación de Pydantic.
- `description` acepta hasta **5000** caracteres.

> ### ⚠️ La trampa más importante de todo el módulo
>
> Si mandás `model_ids` y **alguno no existe**, la respuesta es **422
> `project.blueprints_not_found`** — pero **el proyecto YA quedó creado**, vacío. Los vínculos
> se validan después de que la fila del proyecto existe.
>
> **Qué tiene que hacer la UI en ese 422:** *no* reintentar el `POST` (daría 409
> `project.name_taken` por el nombre que acaba de tomar). Hay que **buscar el proyecto recién
> creado y reintentar solo `POST /projects/{id}/blueprints`** con los ids corregidos.
>
> **Recomendación de diseño:** evitá el problema — mandá el alta **sin** `model_ids` y vinculá
> en una segunda llamada. Así el 422 de vinculación nunca queda a mitad de camino. Usá
> `model_ids` en el alta solo si los ids salen de un selector que los tomó del propio backend.

### 3.3 `GET /api/v1/projects/{project_id}`

Devuelve un `ProjectOut` (misma forma que en el listado, con su `blueprint_count`).

### 3.4 `PATCH /api/v1/projects/{project_id}`

```jsonc
{ "name": "Nuevo nombre", "description": null }
```

**Parcial de verdad.** Sin campos, no cambia nada. Y ojo con la semántica de `description`:

| Qué mandás | Qué pasa |
|---|---|
| `description` ausente | no se toca |
| `description: null` | **se limpia** |
| `description: "texto"` | se reemplaza |

Es la única forma de distinguir "no enviado" de "vaciar", así que el formulario tiene que
mandar `null` explícito para borrar la descripción — mandar `""` guarda una cadena vacía, que
no es lo mismo.

### 3.5 `DELETE /api/v1/projects/{project_id}`

Sin body, sin confirmación. `data` viene ausente; el `message` trae el conteo de
desvinculados. Ver §1.

### 3.6 `GET /api/v1/projects/{project_id}/blueprints` — **sin paginar**

Devuelve la lista completa de `DatabaseModelOut` del proyecto:

```jsonc
{
  "data": [
    {
      "id": 4, "name": "Whatsapp", "slug": "whatsapp", "description": null,
      "current_version": "0007", "is_active": true,
      "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci",
      "created_at": "…", "updated_at": "…"
    }
  ]
}
```

**No acepta `page`/`size`** — son unidades, no miles. No armes paginador acá.

### 3.7 `POST /api/v1/projects/{project_id}/blueprints`

```jsonc
{ "model_ids": [4, 9, 12] }   // requerido, al menos 1
```

**Idempotente y todo-o-nada.** Las dos propiedades importan y son distintas:

```jsonc
// 200
{
  "data": {
    "project_id": 1,
    "linked": [9, 12],            // vinculados en ESTA llamada
    "already_linked": [4],        // ya pertenecían: NO es un error
    "blueprint_count": 3          // total del proyecto tras la operación
  },
  "message": "Blueprints vinculados al proyecto."
}
```

- Reenviar un blueprint ya vinculado **no falla**: sale en `already_linked` con 200. Podés
  reintentar sin miedo.
- Si **algún** id no existe: **422 `project.blueprints_not_found`** y **no se vincula ninguno**.
  El `public_context` trae `missing_model_ids` para que señales las filas malas en vez de
  invalidar la selección entera.

### 3.8 `DELETE /api/v1/projects/{project_id}/blueprints/{model_id}`

Suelta el vínculo. El blueprint queda intacto. Si ese blueprint **no pertenece** al proyecto:
404 `project.blueprint_not_linked` (distinto de "el blueprint no existe").

### 3.9 `GET /api/v1/database-models/{model_id}/projects` — vista inversa

Los proyectos a los que pertenece un blueprint. Puede ser lista vacía. Sin paginar. Devuelve
`ProjectOut[]`.

Es el endpoint que permite mostrar, en la pantalla de un blueprint, a qué proyectos pertenece
— y desde ahí ofrecer desvincular.

---

## 4. Códigos de error

Todos en `detail.public_context.code`.

| HTTP | `code` | Cuándo | Qué ofrecer |
|---|---|---|---|
| 404 | `project.not_found` | El proyecto no existe o se borró entre dos llamadas. | Volver al listado y refrescar. |
| 422 | `project.blueprints_not_found` | Hay ids de blueprint inexistentes. **No se vinculó ninguno.** Trae `missing_model_ids`. | Marcar esas filas y reintentar solo con las válidas. |
| 409 | `project.name_taken` | Ya existe otro proyecto con ese nombre. | Pedir otro nombre. **No reintentar igual.** |
| 409 | `project.link_conflict` | Dos vinculaciones simultáneas chocaron. | **Reintentar** — es transitorio. |
| 404 | `project.blueprint_not_linked` | Se pidió desvincular algo que no está en ese proyecto. | Refrescar la lista del proyecto. |
| 404 | `project.blueprint_not_found` | El blueprint no existe. | Refrescar el catálogo de blueprints. |

**Los dos 409 no son intercambiables.** `name_taken` se resuelve cambiando un dato que el
usuario escribió; `link_conflict` se resuelve **repitiendo la misma llamada**. Ofrecer
"reintentar" en el primero manda al usuario a un bucle, y ofrecer "cambiá el nombre" en el
segundo lo manda a arreglar algo que no está roto.

**Los dos 404 de blueprint tampoco.** `blueprint_not_linked` = el blueprint existe, el vínculo
no. `blueprint_not_found` = el blueprint no existe.

---

## 5. Checklist para la SPA

- [ ] Clasificar por `detail.public_context.code`. **No leer `detail.context`**: no llega en
      producción.
- [ ] En el alta, **no mandar `model_ids`** salvo que vengan de un selector alimentado por el
      backend. Si se manda y da 422, recordar que **el proyecto ya existe**: reintentar la
      vinculación, no el alta.
- [ ] Renderizar `missing_model_ids` marcando las filas concretas del selector.
- [ ] Tratar `already_linked` como **éxito**, no como advertencia de error. Es lo que hace que
      la vinculación se pueda reintentar sin consecuencias.
- [ ] Distinguir los dos 409 por código, con CTAs distintos (§4).
- [ ] En el formulario de edición, mandar `description: null` para vaciar. `""` guarda una
      cadena vacía.
- [ ] **No** poner paginador en `GET /projects/{id}/blueprints` ni en
      `GET /database-models/{id}/projects`: no aceptan `page`/`size`.
- [ ] En el `DELETE` del proyecto, confirmación **simple** y copy que diga explícitamente que
      los blueprints no se borran. Nada de re-tipear el nombre: no es una operación
      destructiva y tratarla como tal desgasta la fricción que sí importa en el resto del
      gateway.
- [ ] Usar `blueprint_count` del listado para la columna — ya viene calculado, no hace falta
      pedir los blueprints de cada proyecto.
- [ ] En la pantalla de un blueprint, usar §3.9 para mostrar sus proyectos.
