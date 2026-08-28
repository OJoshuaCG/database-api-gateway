---
description: Valida, reclama o cierra una tarea del proyecto en ClickUp siguiendo el protocolo anti-colisión
---

Argumento recibido: `$ARGUMENTS`

Cargá primero la skill `clickup-task-flow` (es la fuente del protocolo) y ejecutá el modo que
corresponda.

## Modos

| Argumento | Modo | Qué hacés |
| --- | --- | --- |
| `P-07` / `T-260821-oc-slug` | **RECLAMAR** | Validar y tomar la tarea |
| `P-07 <descripción>` | **RECLAMAR** | Idem, usando la descripción como título si hay que crearla |
| `fin P-07` | **CERRAR** | Cerrar: `complete` o `update required` según si falta frontend |
| `fin P-07 <notas>` | **CERRAR** | Idem, con notas para el comentario |
| `frontend` | **PENDIENTES DE FRONTEND** | Listar lo que espera implementación visual |
| `bloqueos` | **DEVUELTAS POR EL FRONTEND** | Listar las que el frontend devolvió porque el backend no cumplió el handoff. **No salen en ningún otro filtro** |
| `estado` (o vacío) | **CONSULTAR** | Qué hay en curso y qué está libre |
| Texto sin ID | **RESOLVER** | Identificar a qué ítem se refiere antes de seguir |

---

## Modo RECLAMAR

1. Resolvé la identidad del ejecutor: **`git config user.email`**. La identidad es el **email y
   nada más** — este repo tiene 4 nombres distintos para un mismo email, así que incluir el nombre
   haría que la misma persona se viera como dos.
2. Buscá el ID en `TODO.md`. Si el argumento no trae ID, identificá a qué ítem se refiere y
   **confirmalo con el usuario antes de seguir** — reclamar la tarea equivocada es peor que
   preguntar.
3. Si la columna `Subtarea` tiene un ID → `clickup_get_task` con ese ID.
   Si no → `clickup_filter_tasks` con `list_ids: ["901716272178"]`, **`include_closed: true`**,
   `subtasks: true`, paginando con `next_page` mientras `has_more` sea `true`. Compará por
   prefijo del ID, no por título.
4. Según el estado:
   - **`in progress`** → leé el último `INICIO` para saber **quién**, **desde cuándo** y con qué
     **rol**:
     - Venís a hacer **lo mismo** que esa persona → **PARÁ.** Informá quién la tiene.
     - El `INICIO` es de **frontend** y vos venís a cambiar **backend** → **no toques esa tarea.**
       Creá una **sub-subtarea** (ver más abajo): el frontend no pierde su trabajo en curso y vos
       no te quedás esperando.
   - **`update required`** → backend hecho, **falta el frontend**. Dos caminos:
     - **Venís a hacer el frontend** → tomala (paso 5).
     - **Venís a hacer más backend** → primero decidí con el usuario si es un fix de esa tarea o
       trabajo nuevo. Si es un fix de esa tarea, la pregunta que define todo es:
       **¿el cambio toca el contrato que el frontend consume?**
       - **NO lo toca** → **no la reabras.** Se **queda** en `update required` (el handoff sigue
         válido) y solo dejás un comentario informativo. Reabrirla la saca del filtro del
         frontend por nada.
       - **SÍ lo toca** → hay que **invalidar el handoff ANTES de tocar código**, porque el
         frontend puede estar implementándolo ahora mismo:
         ```
         clickup_create_comment
           entity_id:  "<subtarea>"
           notify_all: true          ← que se enteren los asignados
           comment_text: bloque HANDOFF INVALIDADO
         ```
         Recién después `in progress` y a trabajar. Al cerrar va un handoff de **RE-ENTREGA** con
         el **delta** (ver modo CERRAR, caso 4c).
   - **`complete`** → **no es un portazo.** Informá el resumen del comentario `FIN`. Después van
     **dos** preguntas, en este orden — la antigüedad primero, porque puede cerrar el caso sola:

     **a) ¿Hace cuánto se cerró?** Mirá `date_closed` contra
     `date -d '30 days ago' +%Y-%m-%d`.
     - **Más de 30 días** → **no se reabre, aunque sea un fix de eso mismo.** Tarea nueva
       `T-<YYMMDD>-<iniciales>-<slug>` + `clickup_add_task_link` a la vieja. Un hilo de hace meses
       ya no describe el estado del código, y reabrirlo mete dos trabajos distintos en la misma
       tarea. **No recicles el ID viejo**: si era `P-07`, la nueva NO se llama `P-07`.
     - **30 días o menos** → seguí a (b).

     **b) Prueba del objetivo declarado:**
     - Es un **fix** de lo que esa tarea entregó → **REABRÍ** la misma tarea: `in progress` +
       comentario `REAPERTURA` con motivo y alcance. Sin ID nuevo.
     - Es trabajo **distinto** o rehacer desde cero → **tarea nueva vinculada** con
       `clickup_add_task_link`.
     - **No está claro** → preguntá al usuario. No crees nada por tu cuenta.
   - **`on hold`** → leé el último comentario. Si es un **`BLOQUEADO POR BACKEND`**, el frontend
     te la devolvió y hay una implementación parada esperándote: andá al **modo DESBLOQUEAR**
     (abajo), que empieza por *verificar* el bloqueo antes de aceptarlo. Si no, dice dónde quedó y
     seguís al paso 5.
   - **`to do`** → seguí al paso 5.
   - **`reviewed`** → estado no usado en este flujo. Preguntá antes de asumir.
   - **No existe** → creála:
     ```
     clickup_create_task
       name:      "P-XX — <título del ítem en TODO.md>"  ← ítem del backlog
                  "T-<YYMMDD>-<iniciales>-<slug>"        ← ítem NUEVO
       # iniciales = parte local del email, 8 chars:
       # git config user.email | cut -d@ -f1 | tr -cd "a-z0-9" | cut -c1-8
       list_id:   "901716272178"
       parent:    "86e2xzf9d"
       assignees: ["<ID ClickUp del creador>"]  ← del registro. NUNCA "me"
       priority:  "normal"                      ← urgent/high/normal/low, ver criterio
     ```
     **Sin fechas al crear.** `start_date` se pone al empezar; `due_date`, solo al cerrar.
     Para un ítem nuevo **NUNCA uses el siguiente `P-XX` libre**: es secuencial y dos personas
     simultáneas calculan el mismo.

     Después de crearla, **RE-VERIFICÁ antes de trabajar** (concurrencia optimista):
     1. Volvé a buscar con `clickup_filter_tasks` + `include_closed: true`.
     2. Contá cuántas hay con tu mismo ID o un slug equivalente reciente.
     3. Si hay más de una: gana la de **`date_created` más antiguo**; si empatan, el **`id`
        menor**. Regla determinística a propósito.
     4. **Si perdiste: PARÁ.** Informá quién reclamó lo mismo y los IDs de ambas. **No borres la
        duplicada por tu cuenta.**

     Con la re-verificación OK: vinculá si deriva de otra tarea, escribí el ID **como link**
     (`[86e2y1abc](https://app.clickup.com/t/86e2y1abc)`) en la columna
     `Subtarea` de `TODO.md`, y agregá el ítem a 🔴 Pendientes si era nuevo.
5. Reclamala, **en este orden**:
   - `clickup_update_task` → `status: "in progress"` (esto es lo que la reserva), **en el mismo
     llamado**:
     ```
     start_date: "<date +%F>"   ← SOLO si venía vacío. Si ya tenía, OMITILO (no se pisa)
     assignees:  [<los que ya estaban>, <el ejecutor>]   ← UNIÓN, nunca solo el tuyo
     due_date:   "none"         ← SOLO si venís de reabrir una tarea que estaba complete
     ```
     Leé la tarea antes (`clickup_get_task`): necesitás `start_date` y `assignees` actuales.
   - `clickup_create_comment` con el bloque `INICIO`, la identidad del ejecutor y el **rol**
     (`backend` o `frontend`) — obligatorio: `in progress` no dice por sí solo quién es
   - Mové el ítem a **🟡 En curso** en `TODO.md`, con ejecutor y fecha
   - Escribí el claim local:
     ```bash
     echo "P-XX — <título> (subtarea <id>, reclamada por <ejecutor>)" > .claude/.tarea-actual
     ```
     Sin esto, el recordatorio de cada prompt sigue diciendo "ninguna tarea reclamada".
6. Recién ahora empezá a trabajar. Confirmale al usuario que quedó reservada, con el ID.

---

### Sub-subtarea: el frontend está trabajando y el backend necesita cambiar algo

**Este es el ÚNICO escenario que autoriza un tercer nivel de jerarquía.** No se crean
sub-subtareas para descomponer trabajo grande, agrupar, separar backend de frontend, ni "para que
quede más ordenado". Si aparece un caso que parece justificarlo, **no lo crees: planteáselo al
usuario** y que se establezca como regla nueva.

Invalidar el handoff acá sería destructivo (le tirás el trabajo en curso) y esperar a que termine
bloquea al backend sin motivo. Así que **no se toca la tarea en curso**:

```
clickup_create_task
  name:    "T-<YYMMDD>-<iniciales>-<slug>"
  list_id: "901716272178"
  parent:  "<ID de la SUBTAREA que el frontend está haciendo>"   ← anidada
```

Verificado: este workspace de ClickUp **acepta tres niveles** y respeta el `parent`.

Después:

1. `clickup_add_task_link` entre la sub-subtarea y la madre.
2. Comentario en la **madre** con `notify_all: true`:
   ```
   **Rol:** backend
   **Acción:** TRABAJO DERIVADO — <tarea madre>
   **Motivo:** <por qué no se toca: el frontend la está haciendo>
   **Sub-subtarea:** <ID> — <título>
   **Impacto en lo que estás haciendo:** <NINGUNO | qué se va a ver afectado>
   ```
   El último campo no es opcional: si no afecta lo que el frontend está haciendo, que siga
   tranquilo; si **sí** lo afecta, que lo sepa **ahora** y no cuando termine.
3. La sub-subtarea sigue el ciclo completo por su cuenta, y **la madre la cierra el frontend**
   cuando termina lo suyo, sin esperarla.
4. **No hay cuarto nivel.** Si sobre una sub-subtarea hace falta otra derivación, va como
   **hermana** (mismo `parent`) y vinculada.

---

## Modo CERRAR

1. Resolvé la identidad del ejecutor: **`git config user.email`** (solo el email), y su **ID de
   ClickUp** en el registro de identidades de la skill `clickup-task-flow`.
2. Buscá la subtarea por su ID (en `TODO.md`, o con `clickup_filter_tasks` +
   `include_closed: true`).
3. Verificá que esté en `in progress`. Si está en otro estado, **decilo** en vez de forzar:
   cerrar algo que nadie reclamó suele significar que se saltó el paso de reclamar.

4. **LA PREGUNTA OBLIGATORIA: ¿esto necesita trabajo de frontend?**

   No alcanza con que vos no hayas tocado el frontend. Para decir **NO** hay que poder afirmar
   que **nada de lo que el frontend ya consume cambió**: ni rutas, ni forma de la respuesta, ni
   códigos de error, ni campos obligatorios del request. **Si dudás, va a `update required`** —
   un handoff de más cuesta un comentario; uno de menos deja el frontend roto sin que nadie se
   entere.

### 4a. NO requiere frontend

- `clickup_update_task` → `status: "complete"` **+ `due_date: "<date +%F>"`** — el único momento
  en que se escribe la fecha de fin. Los `assignees` no se tocan: quedan todos los que trabajaron
- `clickup_create_comment` → bloque `FIN`, con **`Sin verificar:`** completo y honesto
- Mové el ítem a **🟢 Realizadas** en `TODO.md` con el detalle completo

### 4b. SÍ requiere frontend

- `clickup_update_task` → `status: "update required"` (**no** `complete`) — **sin `due_date`**:
  el backend terminó lo suyo, la tarea no. La fecha de fin la escribe el frontend al cerrarla
- `clickup_create_comment` → bloque **`HANDOFF FRONTEND`**:

  ```
  **Ejecutor:** <email>
  **Acción:** FIN BACKEND — <tarea>
  **Resumen:** <qué se hizo>
  **Sin verificar:** <lo que quedó sin probar, o "nada">

  **⚠️ REQUIERE FRONTEND**

  **Endpoints nuevos o cambiados:**
  - `POST /api/v1/<ruta>` — <qué hace>

  **Breaking changes:** <NO | SÍ + qué se rompe>
  **Schemas afectados:** <nombres>
  **Contrato:** `docs/api-reference-vN.md` § <sección> — commit `<hash corto>`
  **Qué tiene que hacer el frontend:** <2 o 3 líneas concretas>
  ```

  El hash lo saco con `git rev-parse --short HEAD`. **Referenciá, no adjuntes:** un adjunto queda
  congelado y miente en cuanto el doc cambia. Adjuntá el `api-reference` con
  `clickup_attach_task_file` **solo** si el equipo de frontend no tiene acceso al repo backend, y
  dejá igual el hash.

- Mové el ítem a la sección **🔵 Pendiente de frontend** de `TODO.md`
- **La tarea NO se cierra.** La pasa a `complete` el frontend cuando termina.

### 4c. RE-ENTREGA: la tarea ya había pasado por el frontend

Si esta tarea ya tenía un `HANDOFF FRONTEND` previo (venías de invalidarlo), el handoff nuevo no
repite el contrato entero: informa el **delta**. El frontend puede tener media implementación
hecha y lo que necesita saber es qué cambió respecto de lo que ya tenía.

- `clickup_update_task` → `status: "update required"` (tampoco acá va `due_date`)
- `clickup_create_comment`:

  ```
  **Ejecutor:** <email>
  **Acción:** FIN BACKEND (RE-ENTREGA) — <tarea>
  **Resumen:** <qué se cambió>

  **⚠️ REQUIERE FRONTEND — SEGUNDA ENTREGA**

  **Qué cambió respecto del handoff anterior:**
  - `POST /api/v1/<ruta>` — <antes X, ahora Y>
  - <campo> pasó de opcional a obligatorio
  **Lo que NO cambió:** <para que no se rehaga trabajo ya hecho>
  **Breaking changes respecto de la primera entrega:** <NO | SÍ + qué>
  **Contrato:** `docs/api-reference-vN.md` § <sección> — commit `<hash nuevo>`
  ```

**La línea "Lo que NO cambió" no es relleno:** sin ella el frontend rehace trabajo que estaba
bien.

- Mové el ítem de vuelta a **🔵 Pendiente de frontend** en `TODO.md`, anotando que es re-entrega

5. En los dos casos: **borrá el claim local**, que es lo que apaga el recordatorio:
   ```bash
   rm -f .claude/.tarea-actual
   ```
6. **Nunca pongas `reviewed`** — no se usa en este flujo.

Si el trabajo quedó a medias: **`on hold`** con un comentario que diga dónde quedó. **No uses
`update required` para eso**: ese estado significa "falta el frontend" y nada más; usarlo para
otra cosa le mete ruido al filtro del frontend.

---

## Modo PENDIENTES DE FRONTEND

```
clickup_filter_tasks
  list_ids: ["901716272178"]
  statuses: ["update required"]
  subtasks: true
```

Para cada una, leé el comentario `HANDOFF FRONTEND` con `clickup_get_task_comments` y presentá:

- El ID y título de la tarea
- Endpoints nuevos o cambiados
- Si hay **breaking changes** (esas van primero: rompen lo que ya está en producción)
- La referencia al contrato con su commit
- Qué tiene que hacer el frontend

**Buscá también los handoff INVALIDADOS.** Una tarea que estaba esperando frontend y volvió a
`in progress` desaparece de este filtro, pero el frontend necesita saber que le va a volver.
Revisá las `in progress` cuyo último comentario sea `HANDOFF INVALIDADO` y reportalas aparte como
"vuelve al frontend, backend trabajando".

Si alguna está en `update required` **sin** comentario de handoff, marcala como tal: es un cierre
mal hecho y hay que pedirle el contexto a quien la dejó ahí, no adivinarlo.

**Al tomar una de estas, el frontend la pasa a `in progress`** con un `INICIO` de rol frontend.
Eso es lo que avisa que ya la está haciendo y lo que le dice al backend que no la toque (si el
backend necesita cambiar algo mientras tanto, abre una sub-subtarea).

Este listado es el insumo natural del agente `frontend-planning`.

---

## Modo DEVUELTAS POR EL FRONTEND (`bloqueos`)

```
clickup_filter_tasks
  list_ids: ["901716272178"]
  statuses: ["on hold"]
  subtasks: true
```

De las que vuelvan, quedate con las que tengan un comentario **`BLOQUEADO POR BACKEND`**
(`clickup_get_task_comments`). Las demás son tareas que quedaron a medias y no son esto.

**Corré esto al arrancar el día, antes de sacar trabajo nuevo del backlog.** Una tarea en
`on hold` no está en `to do`, ni en `in progress`, ni en `update required`: **no aparece en ningún
filtro que mires por costumbre**, y del otro lado hay una implementación de frontend parada. Es el
único tramo del flujo con alguien esperando.

Para cada una, presentá:

- ID, título y link
- **Qué necesita del backend** (el campo del comentario), que es lo accionable
- **Qué esperaba vs. qué encontró** — de acá sale si el bloqueo es real
- **Dónde quedó el frontend**: cuánto trabajo ya existe del otro lado y sigue siendo válido
- Desde cuándo está parada

Ordenalas por antigüedad: la que lleva más tiempo parada tiene más trabajo detenido detrás.

---

## Modo DESBLOQUEAR

Se entra acá desde `bloqueos`, o desde RECLAMAR cuando una tarea en `on hold` resulta ser un
`BLOQUEADO POR BACKEND`.

**1. Verificá el bloqueo ANTES de aceptarlo.** Reproducí lo que dice `Cómo lo reproduzco`. Hay
tres salidas honestas, y las dos últimas se saltean seguido:

- **El bloqueo es real** → seguí al paso 2.
- **El backend sí cumple; el frontend leyó mal el contrato** → **no cambies el backend.** Dejá un
  comentario con la evidencia (la request correcta y su respuesta) y devolvela a
  `update required`. Cambiar el backend para acomodar una lectura equivocada del contrato es cómo
  se rompe lo que ya andaba en producción.
- **El contrato estaba mal escrito, pero el código está bien** → arreglá el **documento**, no el
  código. Devolvela a `update required` citando el hash nuevo.

**2. Reclamala:** `clickup_update_task` → `status: "in progress"` **+ `priority: "urgent"`**
(hay un frontend parado) **+ `assignees` con la unión**, y comentario `INICIO` con
**`Rol: backend`**. `start_date` normalmente ya lo tiene de la primera vuelta: **no lo pises**.

**3. Arreglá solo lo que desbloquea.** No es la oportunidad de mejorar el endpoint de paso: hay
una implementación parada del otro lado y el campo **`Dónde quedé`** te dice qué no podés romper.
Si de verdad hace falta un cambio más grande, eso es **trabajo nuevo**: aplicá la prueba del
objetivo declarado y abrí una tarea vinculada.

**4. Cerrá a `update required`** —nunca a `complete`: el frontend todavía tiene que terminar lo
suyo— con un handoff que **responda al bloqueo punto por punto**:

```
**Ejecutor:** <email>
**Rol:** backend
**Acción:** FIN BACKEND (DESBLOQUEO) — <tarea>
**Resumen:** <qué se cambió>

**⚠️ REQUIERE FRONTEND — DESBLOQUEO**

**Bloqueo que se resuelve:** comentario `BLOQUEADO POR BACKEND` del <fecha>
**Qué encontraste vs. qué hay ahora:** <punto por punto, contra el campo "Qué encontré">
**Lo que NO cambió:** <para que no se rehaga trabajo que ya estaba bien>
**Sigue sin resolverse:** <lo que pidió y NO se hizo, y por qué. O "nada">
**Contrato:** `docs/api-reference-vN.md` § <sección> — commit `<hash nuevo>`
```

**`Sigue sin resolverse` es el campo que más se omite, y el que más caro sale.** Si pidió tres
cosas y arreglaste dos, el frontend tiene que enterarse **ahora** y no cuando choque con la
tercera. Un desbloqueo parcial anunciado como completo hace que la tarea rebote una segunda vez, y
esa vuelta cuesta mucho más que escribir la línea.

**`Lo que NO cambió` tampoco es relleno**, y en un desbloqueo menos que nunca: el frontend ya tenía
media implementación hecha.

**5. Si NO lo podés desbloquear** (hace falta una decisión de producto, o depende de algo externo):
**no la dejes en `in progress`.** Volvé a `on hold` con un comentario que diga qué falta y de quién
depende, y **`notify_all: true`**. Que quede parada es aceptable; que quede parada **en silencio**,
no.

**6. Borrá el claim local** (`rm -f .claude/.tarea-actual`) cuando la cierres o la vuelvas a dejar
en `on hold`.

---

## Modo CONSULTAR

Son **dos** llamadas, y no se pueden fusionar en una:

```
# 1. El panorama de lo VIVO (y la base para detectar duplicados de ID)
clickup_filter_tasks
  list_ids:       ["901716272178"]
  include_closed: true
  subtasks:       true

# 2. Lo cerrado RECIENTE, para no listar el archivo entero
clickup_filter_tasks
  list_ids:         ["901716272178"]
  include_closed:   true
  subtasks:         true
  date_closed_from: "<date -d '30 days ago' +%Y-%m-%d>"
```

**Por qué dos y no una con el filtro de fecha:** verificado contra la API, `date_closed_from`
devuelve **solo tareas cerradas**. Si lo ponés en la llamada principal desaparece todo lo que está
en `to do`, `in progress`, `update required` y `on hold`.

Paginá cada una hasta `has_more: false` y presentá:

- Qué está **`in progress`**, con quién la tiene (del comentario `INICIO`) y desde cuándo
- Qué está **`update required`** — o sea, backend listo y **frontend pendiente**
- Qué está **`on hold`**, con el motivo y dónde quedó. **Separá las que tengan un comentario
  `BLOQUEADO POR BACKEND`**: no son tareas dormidas, es el frontend devolviéndote trabajo con una
  implementación parada del otro lado. Van primero
- Qué ítems de `TODO.md` siguen sin subtarea (libres para tomar)
- Qué se cerró en los **últimos 30 días** (de la segunda llamada). Ese es el tramo donde un fix
  todavía **reabre** la tarea original; más viejo que eso va como tarea nueva vinculada
- **Duplicados sospechosos**: dos o más subtareas con el mismo ID, o con slugs equivalentes
  creados el mismo día. Es el residuo de una carrera perdida que nadie detectó a tiempo —
  reportalos con sus `date_created` para que se pueda decidir cuál sobrevive. **Esto sale de la
  PRIMERA llamada, la que no tiene ventana**: un duplicado de ID hay que verlo contra todo el
  historial, no contra los últimos 30 días
- Si aparece alguna en **`reviewed`**: señalala como anomalía, ese estado no se usa

---

## Reglas que no se negocian en ningún modo

- **`include_closed: true`** en toda búsqueda. Viene apagado por defecto y sin él una tarea ya
  terminada no aparece → se crea un duplicado exacto.
- **`date_closed_from` NUNCA va en la búsqueda de validación.** Devuelve **solo tareas cerradas**
  (verificado contra la API), así que ahí haría desaparecer todo lo que está en `to do`,
  `in progress`, `update required` y `on hold`. Solo como **segunda** llamada en CONSULTAR.
- **La ventana de 30 días es de DECISIÓN, no de búsqueda.** Un `complete` de hace más de 30 días
  **no se reabre**: va tarea nueva `T-…` vinculada. Pero el ID sigue siendo único contra **todo**
  el historial — si existe una `P-07` cerrada hace un año, no se crea otra `P-07`.
- **La identidad del ejecutor es el EMAIL** (`git config user.email`), no el nombre, y va DENTRO
  del texto del comentario. El campo "autor" de ClickUp
  siempre dice la cuenta del token, así que no sirve para detectar colisiones.
- **Para ASIGNAR, el email de git no sirve: no es un usuario de ClickUp.** Se traduce con el
  registro de identidades de la skill `clickup-task-flow`. Si el email no está en el registro,
  **PARÁ y preguntá** — no lo adivines por parecido de nombre: el workspace tiene ~85 miembros de
  tres empresas y la notificación le llega a un humano real.
- **`"me"` está PROHIBIDO como assignee.** Resuelve siempre al dueño del token, no al ejecutor.
- **`assignees` se manda como UNIÓN, leyendo los actuales primero.** Mandar solo el tuyo puede
  desasignar en silencio a quien hizo la otra mitad. Nadie se saca al cerrar.
- **`start_date` se escribe una sola vez**, al primer `in progress`, y no se pisa nunca más.
- **`due_date` SOLO en `complete`.** Ni en `update required`, ni en `on hold`, ni en
  `in progress`. Al **reabrir** algo cerrado se limpia con `due_date: "none"`.
- **Las fechas van sin hora** (`date +%F`), y salen de bash, no de memoria.
- **Ninguna tarea se crea sin `priority`.** `urgent` = producción/seguridad/datos o tarea devuelta
  con `BLOQUEADO POR BACKEND`; `high` = bloquea a alguien; `normal` = default; `low` = docs y
  refactors. Ante la duda, `normal`.
- **Toda subtarea cuelga de `86e2xzf9d`.** Nunca una tarea suelta en la lista.
- **La jerarquía es de DOS niveles.** El tercer nivel (sub-subtarea) existe para **un solo
  escenario de emergencia**: el backend necesita cambiar algo que el frontend está haciendo en ese
  momento. En ningún otro caso.
- **`in progress` va antes de escribir código**, no después.
- **`update required` significa una sola cosa: falta el frontend.** Para "quedó a medias" está
  `on hold`.
- **`on hold` es también tu bandeja de entrada.** El frontend devuelve ahí lo que el backend no
  cumplió (`BLOQUEADO POR BACKEND`), porque no puede usar `update required` sin metérselo en su
  propio filtro. Esas tareas **no aparecen en ningún filtro habitual**: `/tarea bloqueos`.
- **Un desbloqueo se cierra en `update required`, nunca en `complete`** — el frontend todavía tiene
  que terminar lo suyo.
- **`reviewed` no se usa.**
- Nada de credenciales, `.env`, ni datos de clientes en los comentarios **ni en los adjuntos**.

---

## El claim local (`.claude/.tarea-actual`)

Alimenta al hook `UserPromptSubmit` (`.claude/hooks/recordar-protocolo.sh`), que inyecta el
recordatorio del protocolo en **cada** prompt.

- Lo escribe el modo **RECLAMAR**, lo borra el modo **CERRAR**.
- Está **gitignored**: es estado de esta máquina y de esta persona, no del repo.
- **No es fuente de verdad del estado.** Si discrepa con ClickUp, **gana ClickUp**.
