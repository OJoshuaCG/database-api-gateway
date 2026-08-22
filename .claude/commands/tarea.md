---
description: Valida, reclama o cierra una tarea del proyecto en ClickUp siguiendo el protocolo anti-colisión
---

Argumento recibido: `$ARGUMENTS`

Cargá primero la skill `clickup-task-flow` (es la fuente del protocolo) y ejecutá el modo que
corresponda según el argumento.

## Modos

| Argumento | Modo | Qué hacés |
| --- | --- | --- |
| `P-07` (o un `P-XX` cualquiera) | **RECLAMAR** | Validar y tomar la tarea |
| `P-07 <descripción libre>` | **RECLAMAR** | Idem, usando la descripción como título si hay que crearla |
| `fin P-07` | **CERRAR** | Cerrar la tarea que se estaba haciendo |
| `fin P-07 <notas>` | **CERRAR** | Idem, con notas para el comentario `FIN` |
| `estado` (o vacío) | **CONSULTAR** | Informar qué hay en curso y qué está libre |
| Texto sin `P-XX` | **RESOLVER** | Buscar a qué ítem de `TODO.md` corresponde antes de seguir |

---

## Modo RECLAMAR

1. Resolvé la identidad del ejecutor: `git config user.name && git config user.email` (+ `whoami`).
2. Buscá el `P-XX` en `TODO.md`. Si el argumento no trae `P-XX`, identificá a qué ítem se
   refiere y **confirmalo con el usuario antes de seguir** — reclamar la tarea equivocada es
   peor que preguntar.
3. Si la columna `Subtarea` tiene un ID → `clickup_get_task` con ese ID.
   Si no → `clickup_filter_tasks` con `list_ids: ["901716272178"]`, **`include_closed: true`**,
   `subtasks: true`, paginando con `next_page` mientras `has_more` sea `true`. Comparás por
   prefijo `P-XX`, no por título.
4. Según el estado encontrado:
   - **`in progress`** → **PARÁ ACÁ.** Leé el último comentario `INICIO` con
     `clickup_get_task_comments` y decile al usuario **quién** la tiene y **desde cuándo**. No
     toques la tarea ni empieces a trabajar. Ofrecé alternativas si querés, pero no avances.
   - **`complete`** → **PARÁ ACÁ.** Ya se hizo. Informá el resumen del comentario `FIN`. Si lo
     que falta es verificarla, eso es pasarla a `reviewed`, no rehacerla.
   - **`reviewed`** → **PARÁ ACÁ.** Hecha y verificada. Rehacerla es un ítem nuevo con su
     propio `P-XX`.
   - **`on hold` / `update required`** → leé el último comentario antes de seguir (hay contexto
     de dónde quedó) y continuá al paso 5.
   - **`to do`** → continuá al paso 5.
   - **No existe** → creála con el ID que corresponda:
     ```
     clickup_create_task
       name:    "P-XX — <título del ítem en TODO.md>"   ← ítem del backlog
                "T-<YYMMDD>-<iniciales>-<slug>"          ← ítem NUEVO
       list_id: "901716272178"
       parent:  "86e2xzf9d"
     ```
     Para un ítem nuevo **NUNCA uses el siguiente `P-XX` libre**: es secuencial y dos personas
     simultáneas calculan el mismo, así que el prefijo anti-duplicados terminaría apuntando a
     dos trabajos distintos. El esquema `T-<fecha>-<iniciales>-<slug>` es único sin coordinarse.

     Después de crearla, **RE-VERIFICÁ antes de trabajar** (concurrencia optimista — la ventana
     entre buscar y crear no se puede cerrar, pero sí detectar):

     1. Volvé a buscar con `clickup_filter_tasks` + `include_closed: true`.
     2. Contá cuántas subtareas hay con tu mismo ID, o con un slug equivalente creado en los
        últimos minutos.
     3. Si hay más de una: gana la de **`date_created` más antiguo**; si empatan al segundo, gana
        el **`id` menor** en orden lexicográfico. La regla es determinística a propósito, para
        que los dos lados concluyan lo mismo sin hablarse.
     4. **Si perdiste la carrera: PARÁ.** No trabajes. Informá quién reclamó lo mismo (comentario
        `INICIO` de la ganadora) y los IDs de ambas. **No borres la duplicada por tu cuenta** —
        proponelo y que decida el usuario.

     Recién con la re-verificación OK, escribí el ID en la columna `Subtarea` de `TODO.md` (y
     agregá el ítem a 🔴 Pendientes si era nuevo).
5. Reclamala, **en este orden**:
   - `clickup_update_task` → `status: "in progress"` (esto es lo que la reserva)
   - `clickup_create_comment` con el bloque `INICIO` y la identidad del ejecutor
   - Mové el ítem a **🟡 En curso** en `TODO.md`, con ejecutor y fecha
   - Escribí el claim local para el hook de recordatorio (una sola línea):
     ```bash
     echo "P-XX — <título> (subtarea <id>, reclamada por <ejecutor>)" > .claude/.tarea-actual
     ```
     Sin esto, el recordatorio de cada prompt sigue diciendo "ninguna tarea reclamada" y no te
     avisa que tenés algo abierto. El archivo es local y está gitignored: es estado de esta
     máquina, no del repo.
6. Recién ahora empezá a trabajar. Confirmale al usuario que la tarea quedó reservada, con el
   ID de la subtarea.

---

## Modo CERRAR

1. Resolvé la identidad del ejecutor.
2. Buscá la subtarea por el `P-XX` (por el ID de `TODO.md`, o con `clickup_filter_tasks` +
   `include_closed: true`).
3. Verificá que esté en `in progress`. Si está en otro estado, **decilo** en vez de forzar:
   cerrar algo que nadie reclamó suele significar que se saltó el paso de reclamar, y eso hay
   que corregirlo, no tapar.
4. `clickup_update_task` → `status: "complete"`.
5. `clickup_create_comment` con el bloque `FIN`: resumen simple, y el campo **`Sin verificar:`**
   completo con honestidad — si algo no se probó, va escrito ahí. Si todo se verificó, `nada`.
6. Mové el ítem a **🟢 Realizadas** en `TODO.md` **con el detalle completo**: archivos tocados,
   decisiones tomadas, y qué quedó sin verificar.
7. **No pongas `reviewed`.** Ese estado lo pone otra persona; nadie se auto-revisa.
8. **Borrá el claim local**, que es lo que apaga el recordatorio:
   ```bash
   rm -f .claude/.tarea-actual
   ```
   Si lo dejás, cada prompt va a seguir insistiendo con una tarea que ya cerraste.

Si el trabajo quedó a medias: `update required` (necesita más trabajo) u `on hold` (trabado por
algo externo) en lugar de `complete`, siempre con un comentario que diga **dónde quedó**. Nunca
lo dejes en `in progress`.

---

## Modo CONSULTAR

```
clickup_filter_tasks
  list_ids:       ["901716272178"]
  include_closed: true
  subtasks:       true
```

Paginá hasta `has_more: false` y presentá:

- Qué está **`in progress`**, con quién la tiene (del comentario `INICIO`) y desde cuándo
- Qué está **`on hold` / `update required`**, con el motivo
- Cuántas hay **`complete`** sin pasar a **`reviewed`** — o sea, terminadas pero **sin verificar
  por nadie más**. En este proyecto ese número importa: es la deuda central
- Qué ítems de `TODO.md` siguen sin subtarea (libres para tomar)
- **Duplicados sospechosos**: dos o más subtareas con el mismo ID, o con slugs equivalentes
  creados el mismo día. Es el residuo de una carrera perdida que nadie detectó a tiempo —
  reportalos con sus `date_created` para que se pueda decidir cuál sobrevive

---

## Reglas que no se negocian en ningún modo

- **`include_closed: true`** en toda búsqueda. Viene apagado por defecto y sin él una tarea ya
  terminada no aparece → se crea un duplicado exacto.
- **La identidad del ejecutor va DENTRO del texto** del comentario. El campo "autor" de ClickUp
  siempre dice `Orlando Carrasco` (la cuenta del token), así que no sirve para detectar
  colisiones.
- **Toda subtarea cuelga de `86e2xzf9d`.** Nunca una tarea suelta en la lista.
- **`in progress` va antes de escribir código**, no después. Si va después, la ventana de
  colisión sigue abierta justo cuando más importa.
- Nada de credenciales, `.env`, ni datos de clientes en los comentarios.

---

## El claim local (`.claude/.tarea-actual`)

Es lo que alimenta al hook `UserPromptSubmit` (`.claude/hooks/recordar-protocolo.sh`), que
inyecta el recordatorio del protocolo en **cada** prompt. Con claim, el recordatorio te dice
qué tenés abierto y cómo cerrarlo; sin claim, te recuerda validar antes de empezar.

- Lo escribe el modo **RECLAMAR**, lo borra el modo **CERRAR**.
- Está **gitignored**: es estado de esta máquina y de esta persona, no del repo.
- **No es fuente de verdad del estado.** El árbitro sigue siendo ClickUp. Si el claim y ClickUp
  discrepan (alguien movió el estado desde la web, o quedó un claim viejo), **gana ClickUp** y
  el claim se corrige.
