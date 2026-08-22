---
name: clickup-task-flow
description: Protocolo obligatorio de gestión de tareas del proyecto database-api-gateway — validar, reclamar, comentar y cerrar tareas en ClickUp vía MCP, y mantener TODO.md sincronizado. Usar SIEMPRE antes de empezar cualquier tarea, implementación, fix, verificación o refactor en este repo, y otra vez al terminarla. También al preguntar qué hay pendiente o en qué estado está algo.
---

# Gestión de tareas: ClickUp + TODO.md

Este protocolo existe para que **dos personas no trabajen lo mismo en paralelo**. En un gateway
que administra servidores de BD con credenciales pseudo-root, eso no es prolijidad: es riesgo
operativo.

## Coordenadas

| Campo | Valor |
| --- | --- |
| Tarea principal (paraguas) | `86e2xzf9d` |
| URL | https://app.clickup.com/t/86e2xzf9d |
| Espacio | Cero208 (`90172691192`) |
| Carpeta | Desarrollo (`901710687203`) |
| Lista | Database Gateway (`901716272178`) |
| Workspace | `9017559023` |

Estados: `to do` · `on hold` · `in progress` · `update required` · `reviewed` · `complete`.

## Reparto de autoridad — quién manda sobre qué

| Fuente | Manda sobre | No manda sobre |
| --- | --- | --- |
| **ClickUp** | El **estado** y **quién** trabaja. Es el árbitro de colisiones. | El detalle técnico. |
| **`TODO.md`** (raíz) | El **detalle completo**. | El estado — puede quedar viejo si alguien no lo actualiza. |

Ante discrepancia: para el estado gana **ClickUp**; para el detalle gana **`TODO.md`**.

En ClickUp va un **resumen simple**. El detalle largo va a `TODO.md`. Duplicarlo en ambos lados
garantiza que se desincronicen.

## Nomenclatura obligatoria: `P-XX — <título>`

Cada ítem de `TODO.md` tiene un ID estable (`P-01`, `P-02`, …). La subtarea de ClickUp se llama
**siempre**:

```
P-XX — <título corto del ítem>
```

Y en cuanto se crea, **su ID de ClickUp se escribe de vuelta** en la columna `Subtarea` de
`TODO.md`.

**Por qué es obligatorio:** con nombre libre, una persona escribe "e2e del export" y otra
"verificar exportación" — dos tareas, mismo trabajo, ninguna búsqueda las cruza. El prefijo
`P-XX` es la clave única: se busca `P-07` y solo puede existir una. Y como `TODO.md` queda como
índice `P-XX → subtarea`, en el caso normal ni hace falta buscar en ClickUp.

Un ítem que no está en `TODO.md` se agrega **primero al archivo** con el siguiente ID libre, y
después se crea la subtarea. Nunca al revés.

## Identidad del ejecutor

```bash
git config user.name && git config user.email
```

Formato: `nombre <email>`, más el host (`whoami`) si ayuda a desambiguar.

**Va escrita DENTRO del texto del comentario.** Todos los comentarios se publican con la cuenta
del token de la integración MCP (hoy `Orlando Carrasco`), sin importar quién ejecute. El campo
"autor" de ClickUp es por lo tanto **inútil para detectar colisiones**. Si la identidad no está
en el cuerpo del comentario, el mecanismo entero no sirve.

## Paso 1 — Validar antes de empezar (sin excepciones)

En este orden, y el orden importa:

**1.1** Buscar el ítem en `TODO.md`. Si la columna `Subtarea` ya tiene un ID:

```
clickup_get_task
  task_id: "<id de la subtarea>"
```

Ese es el camino corto y exacto. Listo.

**1.2** Si no tiene ID, buscar en ClickUp **antes de crear nada**:

```
clickup_filter_tasks
  list_ids:        ["901716272178"]
  include_closed:  true          ← OBLIGATORIO
  subtasks:        true
```

**`include_closed: true` no es opcional.** Viene **apagado por defecto**, así que sin él una
tarea ya `complete` **no aparece** y se crea un duplicado exacto. Es la forma más común de
duplicar.

Si la respuesta trae `has_more: true`, seguir paginando con `next_page` hasta que sea `false`.
Una sola llamada no garantiza haber visto todo.

**1.3** Comparar por el **prefijo `P-XX`**, no por el título.

### Qué hacer según lo que se encuentre

| Estado | Acción |
| --- | --- |
| `in progress` | **INTERRUMPIR.** No se toca. Leer el último comentario `INICIO` con `clickup_get_task_comments` e informar al usuario **quién** la tiene y **desde cuándo**. Se puede ofrecer otra cosa; no se avanza sobre esa tarea. |
| `complete` | **INTERRUMPIR.** Ya se hizo: informar con el resumen del comentario `FIN`. Si lo que falta es verificarla, eso es pasarla a `reviewed`, no rehacerla. |
| `reviewed` | **INTERRUMPIR.** Hecha y verificada. Si el usuario insiste en rehacerla, es un ítem **nuevo** en `TODO.md` con su propio `P-XX` y su propia subtarea — nunca una reapertura silenciosa. |
| `to do` | Libre. Se puede tomar. |
| `on hold` / `update required` | Se puede tomar, pero **leer primero el último comentario**: hay contexto de por qué quedó ahí y dónde había quedado. |
| No existe | Crear la subtarea (abajo). |

### Crear la subtarea (solo si de verdad no existe)

```
clickup_create_task
  name:     "P-XX — <título>"
  list_id:  "901716272178"
  parent:   "86e2xzf9d"        ← siempre subtarea, nunca tarea suelta
```

Y acto seguido, **escribir el ID devuelto** en la columna `Subtarea` de `TODO.md`. Sin eso, el
próximo que busque no la encuentra por el camino corto.

## Paso 2 — Al empezar

```
clickup_update_task
  task_id: "<subtarea>"
  status:  "in progress"
```

**Esto es lo que reserva la tarea:** es la señal que hace que otro se detenga. Va **antes** de
escribir la primera línea de código, no después.

```
clickup_create_comment
  entity_type:  "task"
  entity_id:    "<subtarea>"
  comment_text: <bloque INICIO>
```

Y en el repo: mover el ítem a la sección **🟡 En curso** de `TODO.md`.

## Paso 3 — Al terminar

```
clickup_update_task
  task_id: "<subtarea>"
  status:  "complete"
```

**Llegar a `complete` no es opcional.** Mientras no llegue, la tarea sigue apareciendo como
tomada y bloquea a los demás.

```
clickup_create_comment  → bloque FIN
```

Y en el repo: mover el ítem a **🟢 Realizadas** en `TODO.md` **con el detalle completo**:
archivos tocados, decisiones, y muy especialmente **qué quedó sin verificar**. Es normal que
algo quede sin correr (ver la política de `pytest` en `CLAUDE.md`); lo que no es aceptable es
que no esté dicho.

### `complete` vs `reviewed` — quién pone cada uno

| Estado | Lo pone | Significa |
| --- | --- | --- |
| `complete` | **El ejecutor**, al terminar | "Yo terminé lo que me tocaba" |
| `reviewed` | **Otra persona**, después | "Alguien distinto lo verificó" |

**Nadie pone `reviewed` sobre su propio trabajo.** La distinción importa mucho acá: la deuda
central del proyecto es código **escrito y nunca verificado contra motores reales** (ítems
`P-01` a `P-10` de `TODO.md`). Un `complete` sin `reviewed` es exactamente ese estado, y tiene
que verse.

## Paso 4 — Si se abandona a mitad

Nunca se deja en `in progress`. Pasa a:

- `on hold` — trabada por algo externo
- `update required` — quedó a medias y necesita trabajo

Siempre con un comentario explicando **dónde quedó**. Una tarea colgada en `in progress`
bloquea a todos los demás por nada.

## Formato de los comentarios

```
**Ejecutor:** nombre <email> (host: hostname)
**Acción:** INICIO — <nombre de la tarea>
**Resumen:** <una o dos líneas de qué se va a hacer>
```

```
**Ejecutor:** nombre <email> (host: hostname)
**Acción:** FIN — <nombre de la tarea>
**Resumen:** <qué se hizo, en simple>
**Sin verificar:** <lo que quedó sin probar, o "nada">
```

## Resumen: qué cambia y en qué momento

Todos los cambios de estado ocurren en **ClickUp vía MCP**. Ningún estado vive solo en el repo.

| Momento | ClickUp | `TODO.md` |
| --- | --- | --- |
| Se detecta un ítem nuevo | — | Se agrega con el siguiente `P-XX` |
| No existe la subtarea | `create_task` (`parent: 86e2xzf9d`) | Se anota el ID devuelto |
| Al empezar | `update_task` → `in progress` + comentario `INICIO` | Ítem → 🟡 En curso |
| Al terminar | `update_task` → `complete` + comentario `FIN` | Ítem → 🟢 Realizadas, con detalle |
| Al verificarlo otro | `update_task` → `reviewed` + comentario | Se anota quién verificó |
| Se abandona | `update_task` → `on hold` / `update required` + comentario | Ítem vuelve a 🔴 Pendientes |

## Qué NO va a ClickUp

Credenciales, contenido de `.env`, volcados de datos de clientes, ni fragmentos de sentencias
con datos reales. ClickUp es un sistema externo: lo que se escribe ahí sale del repo.

## Límites conocidos de la API vía MCP

Verificados, no asumidos:

- **No existe** herramienta para crear **espacios**. Solo carpetas, listas, tareas, documentos.
- **No se puede mover una lista** entre espacios o carpetas: `clickup_update_list` solo cambia
  `name` / `content` / `status`.
- **No existe** `delete_list` ni `delete_folder`. Una lista creada por error la borra el usuario
  a mano desde la UI.
- **Sí** se puede mover una tarea (`clickup_move_task`) y **el ID de tarea sobrevive** el
  movimiento. Reubicar es barato: cambian los IDs de carpeta y lista, no el de la tarea.
- Una carpeta creada con `override_statuses: false` **hereda** los estados del espacio.
