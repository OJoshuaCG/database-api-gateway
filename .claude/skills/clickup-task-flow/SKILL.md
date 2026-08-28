---
name: clickup-task-flow
description: Protocolo obligatorio de gestión de tareas del proyecto database-api-gateway — validar, reclamar, comentar y cerrar tareas en ClickUp vía MCP, decidir si algo es tarea nueva o va sobre una existente, hacer el handoff al frontend, y mantener TODO.md sincronizado. Usar SIEMPRE antes de empezar cualquier tarea, implementación, fix, verificación o refactor en este repo, y otra vez al terminarla. También al preguntar qué hay pendiente o en qué estado está algo.
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

## Los estados y su significado EXACTO

La lista trae 6 estados. Este flujo usa 5, y cada uno tiene **un solo** significado. Eso no es
pedantería: el frontend filtra por estado, así que un estado ambiguo le trae trabajo que no es
suyo.

| Estado | Significa | Lo pone |
| --- | --- | --- |
| `to do` | Libre, nadie la tomó | — |
| `in progress` | Alguien la está haciendo. **Es lo que reserva la tarea.** Vale para backend Y para frontend: el **rol va declarado en el comentario `INICIO`** | Quien la toma |
| `on hold` | Detenida: trabada por algo externo, **o quedó a medias** y hay que retomarla. **Incluye el caso en que el frontend la devuelve** porque el backend no entregó lo que el handoff prometía — ver "El frontend devolvió una tarea" | Quien la deja |
| `update required` | **Backend terminado. PENDIENTE DE FRONTEND.** Nada más. | Quien termina el backend |
| `complete` | Cerrada del todo. No queda nada pendiente en ningún lado. | Quien la termina |
| `reviewed` | **NO SE USA en este flujo.** | Nadie |

**El frontend también reclama.** Cuando el equipo de frontend toma una tarea que está en
`update required`, la pasa a **`in progress`** — igual que el backend. Eso es lo que avisa que ya
la está haciendo, y es lo que permite que el backend sepa que no debe tocarla.

Consecuencia: **`in progress` no dice por sí solo QUIÉN está trabajando.** Puede ser backend o
frontend. Lo resuelve el comentario `INICIO`, que **declara el rol obligatoriamente**. Antes de
asumir de quién es una tarea en `in progress`, leé su último `INICIO`.

**`reviewed` no se usa a propósito.** Está documentado acá para que nadie le invente un
significado: si aparece una tarea en `reviewed`, es un error o quedó de antes, y hay que
preguntar antes de asumir qué quiso decir.

**`update required` tiene UN solo significado: falta el frontend.** Si se usa también para
"necesita corrección", el filtro del frontend se llena de ruido y el mecanismo pierde el sentido.
Para "quedó a medias" está `on hold`.

**`on hold` también es TU bandeja de entrada, y es fácil no darse cuenta.** El frontend no puede
devolver una tarea a `update required` —ese estado significa "falta el frontend", así que
devolverla ahí la dejaría en su propio filtro y vos nunca te enterarías—. Cuando descubre que el
backend no entregó lo que el handoff prometía, la deja en **`on hold`** con un comentario
`BLOQUEADO POR BACKEND`. Esas tareas **no aparecen en ningún filtro que mires por costumbre**:
revisalas explícitamente con `/tarea bloqueos`.

## Reparto de autoridad — quién manda sobre qué

| Fuente | Manda sobre | No manda sobre |
| --- | --- | --- |
| **ClickUp** | El **estado** y **quién** trabaja. Es el árbitro de colisiones. | El detalle técnico. |
| **`TODO.md`** (raíz) | El **detalle completo**. | El estado — puede quedar viejo si alguien no lo actualiza. |

Ante discrepancia: para el estado gana **ClickUp**; para el detalle gana **`TODO.md`**.

En ClickUp va un **resumen simple**. El detalle largo va a `TODO.md`. Duplicarlo en ambos lados
garantiza que se desincronicen.

## Nomenclatura obligatoria — dos esquemas, y el motivo de que sean dos

El nombre de la subtarea **es** la clave de identidad. Con nombre libre, una persona escribe
"e2e del export" y otra "verificar exportación": dos tareas, mismo trabajo, ninguna búsqueda las
cruza.

### A) Ítems del backlog de `TODO.md` → `P-XX — <título>`

Los ítems sembrados en `TODO.md` tienen un ID **estable y ya asignado** (`P-01`…`P-31`):

```
P-07 — e2e del cruce de familia MySQL↔MariaDB en perfiles de permisos
```

Sin riesgo de colisión: el ID ya existe en el archivo, nadie lo inventa.

### B) Ítems nuevos, creados al vuelo → `T-<YYMMDD>-<iniciales>-<slug>`

Para trabajo que **no está** en `TODO.md`, **NUNCA** uses "el siguiente `P-XX` libre". Ese
esquema es secuencial y **colisiona**: dos personas que arrancan a la vez calculan `P-32` las
dos, y el prefijo —que existe justamente para evitar duplicados— pasa a apuntar a dos trabajos
distintos.

```
T-<YYMMDD>-<iniciales>-<slug>

T-260821-oc-timeout-pool-conexiones
T-260821-jc-fix-collation-mariadb
```

`YYMMDD` = hoy · `iniciales` = de la parte local del **email** (ver "Identidad del ejecutor") ·
`slug` = 2 a 4
palabras en kebab-case. Dos personas distintas nunca generan el mismo ID.

## REGLA DURA: las sub-subtareas son una excepción de emergencia, y una sola

La jerarquía normal es **dos niveles**: tarea principal `86e2xzf9d` → subtareas. Y se queda ahí.

**Existe UN solo escenario que autoriza un tercer nivel:**

> El backend necesita cambiar algo de una tarea que el **frontend está haciendo en este momento**
> (`in progress` con un `INICIO` de rol frontend).

**Nada más. En ningún otro caso se crea una sub-subtarea.** En particular, **NO** se usan para:

- Descomponer una tarea grande en partes. Eso se hace en el **detalle del ítem en `TODO.md`**, o
  con tareas **hermanas vinculadas** si de verdad son unidades de trabajo separadas.
- Organizar, agrupar o clasificar trabajo.
- Separar backend de frontend. Eso lo hace el **estado** (`update required`), no la jerarquía.
- Registrar un fix de algo cerrado. Eso **reabre la tarea existente**.
- "Que quede más ordenado."

**Por qué la restricción es dura:** un árbol de tres niveles usado libremente se vuelve ilegible
en los filtros y en la UI, y rompe el mecanismo que sostiene todo esto — que alguien pueda mirar
el tablero y saber en diez segundos qué está tomado y por quién. La excepción existe porque en ese
escenario puntual la alternativa es peor: o le tirás al frontend el trabajo en curso, o dejás al
backend esperando sin poder hacer un fix que ya puede hacer.

**Si aparece un caso nuevo que parece justificar un tercer nivel: no lo crees. Planteáselo al
usuario y que se establezca como regla nueva.** Una excepción sin discutir se convierte en la
norma en dos semanas.

## ¿Tarea nueva, o va sobre una que ya existe?

Esta es la decisión más frecuente y la que más fácil llena ClickUp de basura. **No se decide por
tamaño** — nadie estima igual y en dos semanas cada uno decide distinto. Se decide con una
prueba verificable:

> **¿El trabajo nuevo se puede describir sin cambiar el objetivo declarado de la tarea original?**

**Sí → va sobre la misma tarea. No → tarea nueva, vinculada a la original.**

| Situación | Qué se hace |
| --- | --- |
| **Fix** de algo que la tarea entregó mal, y la tarea sigue abierta | **Misma tarea.** Comentario explicando el fix. Sin ID nuevo. |
| **Fix** de algo que ya está `complete` **hace 30 días o menos** | **Misma tarea: se REABRE** a `in progress` con un comentario que diga qué se rompió y por qué se reabre. Cerrarla de nuevo al terminar. |
| **Fix** de algo `complete` de **hace más de 30 días** | **NO se reabre nunca. Tarea nueva `T-…`, vinculada** (ver "La ventana de 30 días"). |
| **Feature** que extiende la tarea sin cambiar su objetivo | **Misma tarea.** Se actualiza la descripción + comentario. |
| **Feature** que cambia el objetivo, o toca módulos que la original no tocaba | **Tarea nueva, vinculada.** |
| Alguien quiere **rehacer** desde cero algo ya `complete` | **Tarea nueva, vinculada.** No es un fix: es trabajo distinto sobre el mismo terreno. |

### La ventana de 30 días: qué se reabre y qué no

La prueba del objetivo declarado decide **si es la misma historia**. La antigüedad decide **si vale
resucitar el hilo**. Son dos preguntas distintas y hay que hacer las dos — la de antigüedad
primero, porque puede cerrar el caso sola.

**Si la coincidencia está `complete`, mirá su `date_closed`:**

- **Cerrada hace ≤ 30 días** → prueba del objetivo declarado, como siempre. Si es un fix, se
  **reabre**.
- **Cerrada hace > 30 días** → **no se reabre, aunque sea un fix de eso mismo.** Va **tarea nueva
  `T-<YYMMDD>-<iniciales>-<slug>`**, vinculada con `clickup_add_task_link`.

**Por qué el corte:** una tarea de hace meses arrastra un hilo de comentarios que ya no describe el
estado del código. Reabrirla mete dos trabajos separados por meses en la misma tarea, y el `FIN`
original —que alguien va a leer como el resumen de lo entregado— pasa a describir algo que ya no
es. La vinculación conserva la historia sin resucitar el hilo.

La fecha de corte sale de bash, no la calcules a ojo:

```bash
date -d '30 days ago' +%Y-%m-%d
```

**⚠️ La ventana NO se aplica a la búsqueda, solo a la decisión.** La búsqueda de validación sigue
yendo con `include_closed: true` **y sin filtro de fecha**, por dos motivos:

1. **`date_closed_from` devuelve SOLO tareas cerradas** — verificado contra la API. En la búsqueda
   principal haría desaparecer todo lo que está en `to do`, `in progress`, `update required` y
   `on hold`.
2. **El ID tiene que seguir siendo único contra TODO el historial.** Si existe una `P-07` cerrada
   hace un año, no podés crear otra `P-07` — el prefijo dejaría de identificar un solo trabajo.
   Por eso el trabajo derivado de algo viejo usa un ID **nuevo** (`T-…`) y se vincula, en vez de
   reciclar el original.

### Cómo vincular

Cuando sí corresponde tarea nueva, **la relación se registra en los dos lados**:

```
clickup_add_task_link
  task_id:  "<la nueva>"
  links_to: "<la original>"
```

Es bidireccional y no bloquea (para dependencias reales de orden existe
`clickup_add_task_dependency` con `waiting_on`/`blocking`, pero acá casi nunca aplica).

Y en `TODO.md`, el ítem derivado anota de dónde sale:

```
| T-260821-oc-export-particiones | ... | 86e2yyyy | deriva de P-01 |
```

Sin la vinculación, en tres meses nadie sabe que esas dos tareas eran la misma historia.

### Ante la duda, preguntá

Si no está claro si es fix o trabajo nuevo, **preguntale al usuario** antes de crear nada. Una
tarea de más es ruido; una tarea partida en dos cuando era una sola pierde la historia. El
humano tiene contexto que el protocolo no.

## La ventana de colisión no se puede cerrar — se detecta

El chequeo es *buscar, después crear*. No hay lock ni "crear si no existe" atómico en ClickUp:

```
A: busca → no existe
B: busca → no existe        ← ambos pasaron
A: crea la subtarea
B: crea la subtarea         ← duplicado
```

La ventana es de segundos y solo afecta a ítems **nuevos**. No se elimina, pero **sí se detecta**
con una re-verificación después de crear (concurrencia optimista):

**Inmediatamente después de `clickup_create_task`, y antes de empezar a trabajar:**

1. Volvé a buscar con `clickup_filter_tasks` (`include_closed: true`).
2. Contá cuántas subtareas hay con tu mismo ID, o con un **slug equivalente** creado en los
   últimos minutos.
3. Si hay **más de una**, resolución **determinística** para que los dos lados concluyan lo mismo
   sin hablarse:
   - Gana la de **`date_created` más antiguo**.
   - Si empatan al segundo, gana el **`id` de tarea menor** en orden lexicográfico.
4. **Si ganaste:** seguí normalmente.
5. **Si perdiste: PARÁ.** No trabajes. Informá quién reclamó lo mismo (comentario `INICIO` de la
   ganadora) y los IDs de las dos. **No borres la duplicada por tu cuenta** — proponelo y que
   decida el usuario.

Sin este paso el duplicado es **invisible** hasta el merge.

## Identidad del ejecutor: SOLO el email

```bash
git config user.email
```

**La identidad es el email, y nada más.** No se usa `user.name`.

**Por qué:** el historial de este repo tiene **cuatro** nombres distintos (`ojoshuacg`,
`Joshua CG`, `Joshua`, `Joshua Carrasco`) para **un mismo email**. Si la identidad incluyera el
nombre, la misma persona trabajando desde dos máquinas se vería como dos personas y el protocolo
la interrumpiría contra sí misma. El email es lo único estable.

El nombre se puede mencionar como cortesía en el comentario, pero **lo que identifica es el
email** — y es lo que hay que comparar cuando se decide si alguien más tiene la tarea.

**Va escrito DENTRO del texto del comentario.** Todos los comentarios se publican con la cuenta
del token de la integración MCP (hoy `Orlando Carrasco`), sin importar quién ejecute. El campo
"autor" de ClickUp es por lo tanto **inútil para detectar colisiones**.

### Las iniciales del ID también salen del email

El esquema `T-<YYMMDD>-<iniciales>-<slug>` necesita un fragmento por persona. Sale de la **parte
local del email** (lo que va antes del `@`), en minúsculas, solo `[a-z0-9]`, truncada a 8:

```bash
git config user.email | cut -d@ -f1 | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cut -c1-8
```

```
ojoshuacg@gmail.com   → ojoshuac
LeoZubiri@outlook.com → leozubir
ocarrasco@inbtel.com  → ocarrasc
```

Derivarlo del nombre sería frágil por lo mismo de arriba: cambia entre máquinas y el ID dejaría de
ser estable para la misma persona.

### El email de git NO es un usuario de ClickUp

Verificado contra el workspace: **ninguno de los emails que commitean en este repo existe como
miembro de ClickUp.** `clickup_resolve_assignees` con `ocarrasco@inbtel.com` devuelve `null`, y el
workspace usa otro dominio. El email de git dice **quién trabaja**; para **asignar** hay que
traducirlo.

**`"me"` está PROHIBIDO como assignee.** Resuelve al dueño del token de la integración
(`138069418`, Orlando Carrasco) sin importar quién ejecute — el mismo motivo por el que el campo
"autor" de los comentarios es inútil para detectar colisiones. Usar `"me"` le asigna a una sola
persona todas las tareas de todo el equipo.

**El registro de identidades:**

| Email de git | Usuario ClickUp | ID |
| --- | --- | --- |
| `ojoshuacg@gmail.com` | Orlando Carrasco | `138069418` |
| `LeoZubiri@outlook.com` | Hedson Zubiri | `180236627` |

**Si el email de git no está en la tabla: PARÁ y preguntá.** No inventes el mapeo por parecido de
nombre ni por dominio. El workspace tiene ~85 miembros de tres empresas, con nombres repetidos y
personas con dos cuentas (`igomez@inbtel.com` y `igomez.inbtel@gmail.com` son la misma humana con
IDs distintos). Asignar por corazonada le tira trabajo ajeno a un compañero real que no tiene nada
que ver con este repo, y le llega la notificación.

Cuando entra alguien nuevo al equipo, la fila se agrega **acá**; no se resuelve al vuelo.

## Fechas, asignados y prioridad

Tres campos que el tablero muestra y que hasta ahora nadie llenaba. Se escriben **en el mismo
`clickup_update_task` que cambia el estado**, no en un llamado aparte: si van separados y el
segundo falla, queda una tarea `complete` sin fecha de fin y nadie se entera.

### `start_date` — cuándo se empezó de verdad

**Se pone al pasar a `in progress`, no al crear.** Una tarea creada y no empezada no tiene inicio;
si la escribís al crear, lo que mide es cuánto estuvo en el backlog.

**Solo si está vacío.** Antes de escribirla, mirá `start_date` con `clickup_get_task`. Si ya tiene
valor **no se pisa**: ni en una reapertura, ni cuando el frontend releva al backend, ni al volver
de un `on hold`. Marca cuándo arrancó el trabajo **sobre esa tarea**, y pisarlo borra el único dato
que dice cuánto lleva abierta.

### `due_date` — cuándo se terminó

**SOLO al pasar a `complete`. Ningún otro estado la toca.**

El error fácil es escribirla en `update required`: para el backend "ya terminó", pero la tarea
**no** terminó — le falta el frontend, y es el frontend quien la cierra. Si el backend pone la
fecha ahí, el tablero muestra como terminadas tareas que siguen abiertas y el campo deja de
significar nada. `in progress` y `on hold` tampoco la tocan.

**Al reabrir una tarea cerrada** (`complete` → `in progress`): `due_date: "none"`, en el mismo
llamado que cambia el estado. Una tarea abierta no puede tener fecha de fin; si no la limpiás,
queda una tarea en curso que "terminó" hace dos semanas. Cuando vuelva a cerrarse se escribe la
fecha nueva.

### Solo fecha, nunca hora — y sale de bash

```bash
date +%F      # 2026-08-25
```

Formato `YYYY-MM-DD`, sin `HH:MM`. La API acepta las dos formas; acá se usa **solo fecha** a
propósito, porque la hora que registraría es la del llamado MCP y no la del trabajo real: sería
precisión falsa. **No la calcules de memoria** — mismo motivo que la ventana de 30 días.

### `assignees` — todos los que trabajaron, no el último

Quien crea la tarea queda asignado. Quien la reclama, se agrega. El frontend que la toma **se suma
al backend, no lo reemplaza**. Y **nadie se saca al cerrar**: la tarea guarda quiénes participaron.
Una tarea puede tener varios asignados; eso no es desprolijidad, es el registro de la colaboración.

**REGLA DURA: leé los assignees actuales y mandá la UNIÓN.**

```
clickup_get_task     →  assignees actuales
                        ↓
                        unión con el ID del ejecutor (registro de identidades)
                        ↓
clickup_update_task     assignees: [<todos>]
```

Nunca mandes solo tu ID. El parámetro es una **lista completa**, y **no está verificado** si el
wrapper MCP la reemplaza o la agrega: mandar la unión es correcto en los dos casos, mandar el tuyo
solo es correcto en uno. Si reemplaza y mandaste solo el tuyo, **desasignaste en silencio** a quien
hizo la mitad del trabajo y no queda rastro de que estuvo.

Si el ejecutor **ya está** en la lista, no hace falta reescribirla.

### `priority` — se mide por a quién hace esperar, no por esfuerzo

Se pone **al crear** y se re-evalúa solo si **cambia el impacto**. No sube por antigüedad: una
tarea vieja que no bloquea a nadie sigue siendo `low`.

| Prioridad | Cuándo |
| --- | --- |
| `urgent` | Producción caída o comprometida, vulnerabilidad, pérdida o corrupción de datos, bifurcación de migraciones — y **toda tarea devuelta con `BLOQUEADO POR BACKEND`**, porque hay una implementación de frontend parada del otro lado. |
| `high` | Bloquea a otra persona o a otra tarea: un handoff que el frontend está esperando, el fix de un bug que ya está en manos de un usuario, la dependencia de otro ítem del backlog. |
| `normal` | **Default.** Ítem planificado del backlog (`P-XX`) que nadie está esperando ahora. |
| `low` | Documentación, refactor interno, limpieza, deuda técnica sin impacto visible. |

**Ante la duda, `normal`.** Si todo es urgente, nada lo es — y el `urgent` deja de servir para lo
único que sirve: encontrar en el tablero lo que está roto **ahora**.

**Ninguna tarea se crea sin prioridad.** Un campo vacío no se distingue de una decisión: nadie
puede saber si es de baja prioridad o si todavía nadie la miró.

## Paso 1 — Validar antes de empezar (sin excepciones)

**1.1** Buscá el ítem en `TODO.md`. Si la columna `Subtarea` ya tiene un ID → `clickup_get_task`
con ese ID. Camino corto y exacto.

**1.2** Si no tiene ID, buscá en ClickUp **antes de crear nada**:

```
clickup_filter_tasks
  list_ids:        ["901716272178"]
  include_closed:  true          ← OBLIGATORIO
  subtasks:        true
                                 ← SIN date_closed_from: devuelve solo cerradas
```

**Esta búsqueda NO lleva `date_closed_from`.** Verificado contra la API: ese filtro devuelve **solo
tareas cerradas**, así que acá haría desaparecer todo lo que está en `to do`, `in progress`,
`update required` y `on hold`. El recorte a 30 días es una regla de **decisión** sobre lo que
encontrás, no un filtro de la consulta (ver "La ventana de 30 días").

**`include_closed: true` no es opcional.** Viene **apagado por defecto**, así que sin él una
tarea ya `complete` **no aparece** y se crea un duplicado exacto. Si la respuesta trae
`has_more: true`, paginá con `next_page` hasta que sea `false`.

**1.3** Compará por el **prefijo del ID**, no por el título.

### Qué hacer según lo que se encuentre

| Estado | Acción |
| --- | --- |
| `in progress` | Leé el último `INICIO` con `clickup_get_task_comments` para saber **quién** la tiene, **desde cuándo** y con qué **rol**. Si venís a hacer lo mismo que esa persona → **INTERRUMPIR**. Si el `INICIO` es de **frontend** y vos venís a cambiar backend → **no la toques: sub-subtarea** (ver "El backend necesita volver a tocar…"). |
| `update required` | **Backend hecho, falta el frontend.** Si lo que venís a hacer es el **frontend**, tomala. Si venís a hacer **más backend**, avisá que hay un handoff pendiente y decidí con el usuario si es un fix de esa tarea o trabajo nuevo. |
| `complete` | **NO es un portazo:** informá que ya se hizo, con el resumen del comentario `FIN`. Después mirá su **`date_closed`**: si se cerró hace **más de 30 días**, no se reabre — va tarea nueva vinculada, con ID nuevo. Si es más reciente, aplicá la prueba de "¿tarea nueva o la misma?" (arriba): si es un **fix**, se **reabre**; si es trabajo distinto, **tarea nueva vinculada**. |
| `to do` | Libre. Se puede tomar. |
| `on hold` | **Leé primero el último comentario**, que dice por qué se detuvo y dónde quedó. Si es un **`BLOQUEADO POR BACKEND`**, el frontend te la devolvió: es trabajo tuyo y tiene prioridad — hay una implementación de frontend parada esperándote (ver "El frontend devolvió una tarea"). Si no, es una tarea que quedó a medias y se puede retomar normalmente. |
| `reviewed` | Estado **no usado** en este flujo. Preguntá antes de asumir qué significa. |
| No existe | Crear la subtarea (abajo). |

### Crear la subtarea (solo si de verdad no existe)

```
clickup_create_task
  name:      "P-XX — <título>"        ← ítem del backlog de TODO.md
             "T-260821-oc-<slug>"     ← ítem nuevo: NUNCA el siguiente P-XX libre
  list_id:   "901716272178"
  parent:    "86e2xzf9d"              ← siempre subtarea, nunca tarea suelta
  assignees: ["<ID del creador>"]     ← del registro de identidades. NUNCA "me"
  priority:  "normal"                 ← ante la duda, normal
```

**Sin `start_date` ni `due_date`.** Crear no es empezar, y empezar no es terminar.

Después de crearla, **en este orden**:

1. **Re-verificá** que no haya duplicado (ver "La ventana de colisión").
2. Si deriva de otra tarea, **vinculala** con `clickup_add_task_link`.
3. Escribí el ID devuelto en la columna `Subtarea` de `TODO.md`, **como link Markdown**:
   `[86e2y1abc](https://app.clickup.com/t/86e2y1abc)`. Un ID pelado obliga a armar la URL a mano.
4. Si el ítem era nuevo, agregalo a la tabla de 🔴 Pendientes con su ID `T-…`.

## Paso 2 — Al empezar

```
clickup_update_task
  task_id:    "<subtarea>"
  status:     "in progress"
  start_date: "<date +%F>"        ← SOLO si venía vacío; si ya tenía valor, OMITILO
  assignees:  [<unión de los actuales + el ejecutor>]
```

**Esto es lo que reserva la tarea.** Va **antes** de escribir la primera línea de código.

Los dos campos nuevos necesitan leer la tarea primero (`clickup_get_task`): `start_date` para no
pisarlo, `assignees` para no borrar a nadie. Ver "Fechas, asignados y prioridad".

Después: `clickup_create_comment` con el bloque `INICIO`, y mové el ítem a **🟡 En curso** en
`TODO.md`.

## Paso 3 — Al terminar: la bifurcación del frontend

**Antes de cerrar, la pregunta obligatoria: ¿esto necesita trabajo de frontend?**

```
                        ¿el cambio necesita implementación visual?
                                    │
                 ┌──────────────────┴──────────────────┐
                SÍ                                     NO
                 │                                      │
        status: update required                  status: complete
        + comentario HANDOFF FRONTEND            + comentario FIN
```

### 3a. NO requiere frontend → `complete` directo

Es el caso cuando el cambio es interno del backend, un fix que no altera ningún contrato, o algo
que el frontend ya consume igual que antes.

```
clickup_update_task  →  status: "complete"  +  due_date: "<date +%F>"
clickup_create_comment  →  bloque FIN
```

**Este es el único momento en que se escribe `due_date`**, y los `assignees` **no se tocan**:
quedan todos los que trabajaron.

**Criterio para decir "no requiere frontend" con honestidad:** no alcanza que *vos* no hayas
tocado el frontend. Hay que poder afirmar que **nada de lo que el frontend ya consume cambió**:
ni rutas, ni forma de la respuesta, ni códigos de error, ni campos obligatorios del request.
Si tenés dudas, **va a `update required`** — un handoff de más cuesta un comentario; uno de
menos deja el frontend roto sin que nadie se entere.

### 3b. SÍ requiere frontend → `update required` + handoff

```
clickup_update_task  →  status: "update required"     ← SIN due_date
clickup_create_comment  →  bloque HANDOFF FRONTEND
```

**Acá NO va la fecha de fin.** El backend terminó lo suyo, la tarea no. La escribe el frontend
cuando la pase a `complete`.

La tarea **no se cierra**. Queda en `update required` hasta que el frontend termine, y **es el
frontend quien la pasa a `complete`**. Así el equipo de frontend filtra por ese estado y
encuentra exactamente lo que le toca, con el contexto ya escrito — sin crear tareas nuevas ni
salir a buscar qué cambió.

### En los dos casos

Mové el ítem en `TODO.md`:
- `complete` → **🟢 Realizadas**, con el detalle completo: archivos tocados, decisiones, y muy
  especialmente **qué quedó sin verificar**.
- `update required` → sección **🔵 Pendiente de frontend**, con el resumen del handoff.

Es normal que algo quede sin verificar (ver la política de `pytest` en `CLAUDE.md`); lo que no es
aceptable es que no esté dicho.

## El handoff al frontend

El comentario de handoff **es el insumo del frontend**. Si está pobre, el frontend igual sale a
buscar contexto y no ganamos nada. Formato:

```
**Ejecutor:** <email>          ← la identidad es el EMAIL, no el nombre
**Acción:** FIN BACKEND — <nombre de la tarea>
**Resumen:** <qué se hizo, en simple>
**Sin verificar:** <lo que quedó sin probar, o "nada">

**⚠️ REQUIERE FRONTEND**

**Endpoints nuevos o cambiados:**
- `POST /api/v1/<ruta>` — <qué hace>
- `GET /api/v1/<ruta>` — <qué cambió>

**Breaking changes:** <NO | SÍ + qué se rompe en lo que el frontend ya tiene>
**Schemas afectados:** <nombres de los schemas Pydantic>
**Contrato:** `docs/api-reference-vN.md` § <sección> — commit `<hash corto>`
**Qué tiene que hacer el frontend:** <2 o 3 líneas concretas, no "implementar la UI">
```

### El contrato se REFERENCIA, no se adjunta

Poné **ruta + sección + hash del commit**. Un archivo adjunto queda **congelado**: en cuanto el
doc cambia, el adjunto miente y el frontend implementa contra un contrato viejo. El hash le dice
exactamente qué versión mirar.

**Excepción:** si el equipo de frontend **no tiene acceso al repo del backend**, ahí sí adjuntá el
`docs/api-reference-vN.md` con `clickup_attach_task_file` — pero dejá igual el hash en el
comentario, para que se sepa a qué versión corresponde el adjunto.

Y antes de adjuntar cualquier cosa, releé "Qué NO va a ClickUp" abajo. Un `api-reference` no
lleva credenciales, así que está bien; un `.env` o un volcado de datos, nunca.

### El agente `frontend-planning`

Este repo tiene un agente `frontend-planning` que convierte especificaciones de backend en planes
de implementación de frontend. El comentario de handoff es **exactamente** su insumo: si está
completo, ese agente puede producir el plan sin volver a leer el backend.

## Paso 4 — Si se abandona a mitad

Nunca se deja en `in progress`. Pasa a **`on hold`**, con un comentario que diga **dónde quedó**.
Una tarea colgada en `in progress` bloquea a todos los demás por nada.

**No uses `update required` para esto.** Ese estado significa "falta el frontend" y nada más; si
lo usás para "quedó a medias", el frontend recibe trabajo que no es suyo.

**No se toca ninguna fecha.** `start_date` ya está puesto y se conserva — cuando se retome sigue
siendo el mismo trabajo, no uno nuevo. `due_date` no corresponde: la tarea no terminó. Los
`assignees` tampoco se tocan: quien la dejó a medias sigue siendo quien sabe dónde quedó.

## El frontend devolvió una tarea: `BLOQUEADO POR BACKEND`

Es el camino de vuelta del handoff, y **es el único punto del flujo donde hay alguien parado
esperándote**. Tratalo con esa prioridad.

### Qué pasó

El frontend reclamó una tarea que vos dejaste en `update required`, empezó a implementar, y
descubrió que el backend **no entrega lo que el handoff prometía**: otra forma de respuesta, falta
un campo, el código de error no es el documentado, o la ruta directamente no existe.

No puede devolverla a `update required` —ese estado significa "falta el frontend", así que la
dejaría en su propio filtro y vos no te enterarías nunca—. La deja en **`on hold`** con un
comentario `BLOQUEADO POR BACKEND` y `notify_all: true`.

**Por qué esto te puede pasar de largo:** una tarea en `on hold` no está en `to do`, ni en
`in progress`, ni en `update required`. No aparece en ningún filtro que mires por costumbre. Por
eso existe **`/tarea bloqueos`** — usalo al arrancar el día, antes de sacar trabajo nuevo del
backlog.

### Qué te llega

```
**Ejecutor:** <email>
**Rol:** frontend
**Acción:** BLOQUEADO POR BACKEND — <nombre de la tarea>
**Qué esperaba (según el handoff):** <lo prometido, citando el comentario y su fecha>
**Qué encontré:** <respuesta real, código de estado, forma del payload>
**Cómo lo reproduzco:** <request concreto>
**Qué necesito del backend:** <concreto>
**Dónde quedé:** <qué parte del frontend ya está hecha y sirve igual>
```

**El campo `Dónde quedé` es el que decide cómo cerrás.** Te dice cuánto trabajo de frontend ya
existe y sigue siendo válido. Si hay una implementación a medio hacer del otro lado, **cualquier
cambio tuyo que vaya más allá de lo estrictamente necesario para desbloquear se la rompe**.

### Qué hacés

**1. Verificá el bloqueo antes de aceptarlo.** Reproducí lo que dice `Cómo lo reproduzco`. Hay tres
salidas honestas, y la segunda y la tercera se saltean seguido:

- **El bloqueo es real** → seguí al paso 2.
- **El backend sí cumple, y el frontend leyó mal el contrato** → **no cambies el backend.** Dejá un
  comentario con la evidencia (la request correcta y su respuesta) y devolvela a
  `update required`. Cambiar el backend para acomodar una lectura equivocada del contrato es cómo
  se rompe lo que ya funcionaba en producción.
- **El contrato estaba mal escrito, pero el código está bien** → arreglá el **documento**, no el
  código, y devolvela a `update required` citando el hash nuevo.

**2. Reclamala:** `status: "in progress"` + comentario `INICIO` con **`Rol: backend`**. Sigue
siendo lo que reserva la tarea. En el mismo llamado: `priority: "urgent"` (hay un frontend parado),
`assignees` con la unión, y `start_date` **solo si venía vacío** — normalmente ya lo tiene de la
primera vuelta, y esa es la fecha que vale.

**3. Arreglá solo lo que desbloquea.** Esta no es la oportunidad de mejorar el endpoint de paso.
Hay una implementación parada del otro lado y **`Dónde quedé` te dice qué no podés romper.** Si de
verdad hace falta un cambio más grande, eso es **trabajo nuevo**: aplicá la prueba del objetivo
declarado y abrí una tarea vinculada.

**4. Cerrá a `update required`** —no a `complete`—, con un handoff que **responda al bloqueo punto
por punto**:

```
**Ejecutor:** <email>
**Rol:** backend
**Acción:** FIN BACKEND (DESBLOQUEO) — <nombre de la tarea>
**Resumen:** <qué se cambió>

**⚠️ REQUIERE FRONTEND — DESBLOQUEO**

**Bloqueo que se resuelve:** comentario `BLOQUEADO POR BACKEND` del <fecha>
**Qué encontraste vs. qué hay ahora:** <punto por punto, contra el campo "Qué encontré">
**Lo que NO cambió:** <para que no se rehaga trabajo que ya estaba bien>
**Sigue sin resolverse:** <lo que pediste y NO se hizo, y por qué. O "nada">
**Contrato:** `docs/api-reference-vN.md` § <sección> — commit `<hash nuevo>`
```

**`Sigue sin resolverse` no es opcional, y es el campo que más se omite.** Si pediste tres cosas y
se arreglaron dos, el frontend tiene que enterarse **ahora** y no cuando vuelva a chocar contra la
tercera. Un desbloqueo parcial que se anuncia como completo hace que la tarea rebote una segunda
vez, y esa vuelta cuesta mucho más que escribir la línea.

**`Lo que NO cambió` tampoco es relleno:** sin esa línea el frontend rehace trabajo que estaba
bien — y en un desbloqueo eso es casi seguro, porque ya tenía media implementación hecha.

**5. Si NO lo podés desbloquear** (necesitás una decisión de producto, o depende de algo externo):
**no la dejes en `in progress`.** Volvé a `on hold` con un comentario que diga qué falta y de quién
depende, y `notify_all: true`. Que quede parada es aceptable; que quede parada **en silencio**, no.

## Formato de los comentarios

```
**Ejecutor:** <email>          ← la identidad es el EMAIL, no el nombre
**Rol:** backend | frontend        ← OBLIGATORIO: `in progress` no dice quién es
**Acción:** INICIO — <nombre de la tarea>
**Resumen:** <una o dos líneas de qué se va a hacer>
```

```
**Ejecutor:** <email>          ← la identidad es el EMAIL, no el nombre
**Acción:** FIN — <nombre de la tarea>
**Resumen:** <qué se hizo, en simple>
**Sin verificar:** <lo que quedó sin probar, o "nada">
```

Para el cierre con frontend pendiente, usá el bloque `HANDOFF FRONTEND` de arriba.
Para una reapertura:

```
**Ejecutor:** <email>          ← la identidad es el EMAIL, no el nombre
**Acción:** REAPERTURA — <nombre de la tarea>
**Motivo:** <qué se rompió o qué faltó>
**Alcance:** <qué se va a corregir, sin cambiar el objetivo de la tarea>
```

## Los ciclos completos, escritos de punta a punta

Las transiciones se **componen**, y eso hay que leerlo explícito o se asume mal. `(be)` = lo hace
el backend, `(fe)` = lo hace el frontend.

**1. Backend puro (nunca toca al frontend)**

```
to do → in progress (be) → complete (be)
```

**2. Backend que necesita frontend**

```
to do → in progress (be) → update required → in progress (fe) → complete (fe)
                            (HANDOFF)         (el fe la reclama)
```

El frontend **reclama** pasándola a `in progress` con un `INICIO` de rol frontend. Eso avisa que
ya la está haciendo y es lo que le dice al backend que no la toque.

**3. Fix de algo cerrado que NO afecta al frontend**

```
complete → in progress (be) → complete (be)
            (REAPERTURA)
```

**4. Fix de algo cerrado que SÍ afecta al frontend**

```
complete → in progress (be) → update required → in progress (fe) → complete (fe)
            (REAPERTURA)        (HANDOFF)
```

**Sí: pasa por `in progress`.** No se salta de `complete` a `update required` directo, y tampoco
termina en `complete` como terminaría un fix normal. La reapertura es lo que **reserva** la tarea
mientras se trabaja, y el cierre en `update required` es lo que la manda a revisión del frontend.

**5. Re-entrega: el backend re-toca algo esperando frontend, y el frontend NO empezó**

```
update required → in progress (be) → update required → in progress (fe) → complete (fe)
  (handoff viejo)  (INVALIDACIÓN)     (RE-ENTREGA con delta)
```

**6. El frontend YA está trabajando y el backend necesita cambiar algo** → no se toca esa tarea,
se abre una **sub-subtarea** con su ciclo propio:

```
subtarea madre:   in progress (fe) ───────────────────────────→ complete (fe)
                         │ (no se toca: sigue su curso)
                         └── sub-subtarea: to do → in progress (be) → update required
                                                → in progress (fe) → complete (fe)
```

Así **nadie se bloquea**: el frontend no pierde el trabajo en curso y el backend no se queda
esperando para hacer un fix que puede hacer ya.

**7. El frontend devuelve la tarea porque el backend no cumplió el handoff**

```
update required → in progress (fe) → on hold → in progress (be) → update required → in progress (fe) → complete (fe)
                   (empieza y choca)  (BLOQUEADO   (DESBLOQUEO)
                                       POR BACKEND)
```

Es el **camino de vuelta** del handoff, y el único tramo del flujo donde hay alguien parado
esperándote. El paso por `on hold` no es un descuido: el frontend no puede devolverla a
`update required` sin volver a metérsela en su propio filtro. **Esas tareas no aparecen en ningún
filtro que mires por costumbre** — se ven con `/tarea bloqueos`.

## El backend necesita volver a tocar algo que ya se entregó al frontend

Este es el caso más delicado del flujo, y **no tiene una sola respuesta**: depende de si el
frontend **ya empezó**. La regla de oro es que **nadie se bloquea y nadie pierde trabajo hecho**.

| Estado de la tarea | Qué está pasando | Qué hace el backend |
| --- | --- | --- |
| `update required` | El frontend **todavía no la tomó** | **Reabre la misma tarea.** Invalida el handoff y re-entrega. Nadie perdió nada. |
| `in progress` con `INICIO` de **frontend** | El frontend **la está haciendo ahora** | **NO la toca.** Crea una **sub-subtarea** con su propio ciclo. |

### Caso A — el frontend no empezó: se reabre la misma tarea

Si además el cambio **no toca el contrato** que el frontend consume, ni hace falta reabrir: la
tarea **se queda** en `update required`, el handoff sigue válido, y solo se deja un comentario
informativo. Reabrirla la saca del filtro del frontend por nada.

Si **sí toca el contrato**:

**1. Antes de tocar código,** comentario de invalidación **con notificación**:

```
clickup_create_comment
  entity_id:    "<subtarea>"
  notify_all:   true          ← que se enteren los asignados
  comment_text: <bloque HANDOFF INVALIDADO>
```

```
**Ejecutor:** <email>          ← la identidad es el EMAIL, no el nombre
**Rol:** backend
**Acción:** HANDOFF INVALIDADO — <nombre de la tarea>
**⚠️ NO IMPLEMENTES CONTRA EL HANDOFF ANTERIOR.** El contrato va a cambiar.
**Qué se está por cambiar:** <una o dos líneas>
**Handoff invalidado:** comentario del <fecha del handoff anterior>
```

**2. Recién ahí** `status: "in progress"` (con `INICIO` de rol backend) y se trabaja.

**3. Al cerrar,** handoff de **RE-ENTREGA** con el **delta**, no el contrato entero:

```
**Acción:** FIN BACKEND (RE-ENTREGA) — <nombre de la tarea>

**⚠️ REQUIERE FRONTEND — SEGUNDA ENTREGA**

**Qué cambió respecto del handoff anterior:**
- `POST /api/v1/<ruta>` — <antes X, ahora Y>
**Lo que NO cambió:** <para que no se rehaga trabajo ya hecho>
**Breaking changes respecto de la primera entrega:** <NO | SÍ + qué>
**Contrato:** `docs/api-reference-vN.md` § <sección> — commit `<hash nuevo>`
```

**"Lo que NO cambió" no es relleno:** sin esa línea el frontend rehace trabajo que estaba bien.

### Caso B — el frontend YA está trabajando: sub-subtarea (LA ÚNICA excepción)

Acá invalidar el handoff sería **destructivo**: le tirarías a la basura el trabajo en curso. Y
bloquear al backend hasta que el frontend termine es igual de malo — se queda sin hacer nada
cuando claramente hay un fix o un update que puede hacer ya.

**La solución es no tocar la tarea en curso y abrir una propia:**

```
clickup_create_task
  name:    "T-<YYMMDD>-<iniciales>-<slug>"
  list_id: "901716272178"
  parent:  "<ID de la SUBTAREA que el frontend está haciendo>"   ← anidada, no hermana
```

Verificado empíricamente: **ClickUp de este workspace acepta tres niveles** (tarea principal →
subtarea → sub-subtarea) y respeta el `parent`; no la aplana a la raíz.

Y además:

1. **Vinculala** con `clickup_add_task_link` a la subtarea madre — el `parent` da la jerarquía, el
   link la deja visible desde los dos lados.
2. **Comentario en la subtarea madre** avisando que existe (con `notify_all: true`), para que
   quien está trabajando ahí sepa que viene más:

   ```
   **Rol:** backend
   **Acción:** TRABAJO DERIVADO — <tarea madre>
   **Motivo:** <por qué no se toca esta tarea: el frontend la está haciendo>
   **Sub-subtarea:** <ID> — <título>
   **Impacto en lo que estás haciendo:** <NINGUNO | qué se va a ver afectado>
   ```

   Ese último campo importa: si el trabajo derivado **no** afecta lo que el frontend está
   haciendo, que lo sepa y siga tranquilo. Si **sí** lo afecta, que lo sepa **ahora** y no cuando
   termine.
3. La sub-subtarea tiene su **ciclo completo e independiente**:
   `to do → in progress (backend) → update required → in progress (frontend) → complete`.
4. **La subtarea madre sigue su curso.** La cierra el frontend cuando termina lo suyo, sin esperar
   a la sub-subtarea.

### Profundidad: hasta tres niveles, y se para ahí

Tarea principal → subtarea → sub-subtarea. **No hay cuarto nivel.** Si sobre una sub-subtarea hace
falta otra derivación, se crea como **hermana** de ella (mismo `parent`) y vinculada, no anidada
más profundo.

Es una decisión de diseño, no un límite técnico (no se probó el cuarto nivel): más profundidad se
vuelve ilegible en la UI y en los filtros, y el objetivo de todo esto es que alguien pueda mirar el
tablero y entender qué está pasando.

## Resumen: qué cambia y en qué momento

Todos los cambios de estado ocurren en **ClickUp vía MCP**. Ningún estado vive solo en el repo.

| Momento | ClickUp | `TODO.md` |
| --- | --- | --- |
| Se detecta un ítem nuevo | — | Se agrega con su ID (`P-XX` o `T-…`) |
| No existe la subtarea | `create_task` (`parent: 86e2xzf9d`) + `add_task_link` si deriva | Se anota el ID devuelto |
| Backend empieza | `in progress` + `INICIO` (rol backend) | Ítem → 🟡 En curso |
| Backend termina, sin frontend | `complete` + `FIN` | Ítem → 🟢 Realizadas |
| Backend termina, con frontend | `update required` + `HANDOFF FRONTEND` | Ítem → 🔵 Pendiente de frontend |
| **Frontend reclama** | `in progress` + `INICIO` (**rol frontend**) | Ítem → 🟡 En curso, marcado como frontend |
| Frontend termina | `complete` + `FIN` | Ítem → 🟢 Realizadas |
| Fix de algo cerrado, sin frontend | `in progress` + `REAPERTURA` → después `complete` | 🟡 → 🟢 |
| Fix de algo cerrado, con frontend | `in progress` + `REAPERTURA` → después `update required` + `HANDOFF` | 🟡 → 🔵 |
| Backend re-toca algo en `update required`, **frontend no empezó**, y **cambia el contrato** | `HANDOFF INVALIDADO` (`notify_all`) → `in progress` → `update required` + `HANDOFF` de re-entrega | 🔵 → 🟡 → 🔵 |
| Backend re-toca algo en `update required` **sin cambiar el contrato** | Se **queda** en `update required` + comentario informativo | Se queda en 🔵 |
| **Backend necesita cambiar algo que el frontend YA está haciendo** | **NO se toca esa tarea.** `create_task` con `parent` = la subtarea + `add_task_link` + comentario `TRABAJO DERIVADO` (`notify_all`) en la madre | Sub-ítem nuevo en 🔴 Pendientes, referenciando a la madre |
| Se abandona | `on hold` + comentario | Ítem vuelve a 🔴 Pendientes |
| **El frontend devuelve la tarea** (`BLOQUEADO POR BACKEND`) | Llega en `on hold`. **No sale en ningún filtro habitual: mirala con `/tarea bloqueos`.** Verificá el bloqueo → `in progress` + `INICIO` (rol backend) → arreglá **solo lo que desbloquea** → `update required` + `FIN BACKEND (DESBLOQUEO)` | 🔵 → 🟡 → 🔵 |
| El bloqueo NO era real (el frontend leyó mal el contrato) | **No cambies el backend.** Comentario con la evidencia → de vuelta a `update required` | Se queda en 🔵 |
| El bloqueo era del **documento**, no del código | Arreglá el doc, no el código → de vuelta a `update required` citando el hash nuevo | Se queda en 🔵 |

### Qué campo se toca en cada transición

`—` significa **no lo mandes**, no "mandalo vacío". Omitir deja el valor como está; mandar `"none"`
lo borra.

| Transición | `start_date` | `due_date` | `assignees` | `priority` |
| --- | --- | --- | --- | --- |
| Crear la subtarea | — | — | el creador | según criterio (`normal` por default) |
| → `in progress` | hoy, **si venía vacío** | — | + ejecutor (unión) | — |
| → `update required` | — | **— (el error más fácil)** | + ejecutor (unión) | — |
| → `complete` | — | **hoy** | sin cambios | — |
| → `on hold` | — | — | sin cambios | — |
| Reapertura `complete` → `in progress` | **no se pisa** | **`"none"`** | + ejecutor (unión) | re-evaluar |
| Devuelta con `BLOQUEADO POR BACKEND` | no se pisa | nunca la tuvo | + ejecutor (unión) | **`urgent`** |

## Qué NO va a ClickUp

Credenciales, contenido de `.env`, volcados de datos de clientes, ni fragmentos de sentencias con
datos reales. ClickUp es un sistema externo: lo que se escribe ahí sale del repo. Eso vale
también para los **adjuntos**.

## Límites conocidos de la API vía MCP

Verificados, no asumidos:

- **No existe** herramienta para crear **espacios**. Solo carpetas, listas, tareas, documentos.
- **No se puede mover una lista** entre espacios o carpetas: `clickup_update_list` solo cambia
  `name` / `content` / `status`.
- **No existe** `delete_list` ni `delete_folder`.
- **Sí** se puede mover una tarea (`clickup_move_task`) y **el ID sobrevive** el movimiento.
- Una carpeta creada con `override_statuses: false` **hereda** los estados del espacio.
- `clickup_add_tag_to_task` **falla si el tag no existe** en el espacio: hay que crearlo antes a
  mano desde la UI.
- `clickup_resolve_assignees` con un email que **no es miembro** devuelve `null` **en esa posición
  del array, sin error**. Si no lo chequeás, terminás mandando `[null]` como lista de asignados.
- `"me"` resuelve **siempre** al dueño del token (`138069418`), nunca al ejecutor.
- `start_date` / `due_date` aceptan `YYYY-MM-DD` y `YYYY-MM-DD HH:MM`. `"none"` los limpia, pero
  **solo en `clickup_update_task`**: `clickup_create_task` no acepta ese valor.
- **NO verificado:** si `assignees` en `clickup_update_task` **reemplaza** la lista o la **agrega**.
  Por eso el protocolo manda siempre la unión completa: es correcto bajo las dos semánticas.
