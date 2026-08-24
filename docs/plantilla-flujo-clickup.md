# Plantilla — Flujo de tareas ClickUp + Claude Code

> **Qué es este documento.** La receta completa para reproducir, en otro proyecto, el mecanismo
> de gestión de tareas que usa `database-api-gateway`: Claude Code valida, reclama, comenta y
> cierra tareas en ClickUp antes y después de cada unidad de trabajo, con un hook que hace
> **imposible olvidarse** del protocolo.
>
> Está parametrizado con marcadores `{{ASI}}`. Reemplazalos todos y no queda nada del proyecto
> origen. Los anexos A, B y C son los archivos reales, derivados automáticamente de los del
> repo origen — no transcritos a mano.

---

## 0. Aclaración importante: no hay ningún webhook saliente

Conviene fijar el vocabulario antes de replicar, porque son **dos** mecanismos distintos y es
fácil confundirlos:

| Se le suele decir | Qué es realmente | Dónde vive |
| --- | --- | --- |
| "el webhook" | El **hook `UserPromptSubmit`** de Claude Code: un script **local** que el harness ejecuta en **cada** prompt y cuyo stdout se inyecta en el contexto del turno | `.claude/hooks/recordar-protocolo.sh` |
| "la integración" | El **servidor MCP remoto de ClickUp** (`https://mcp.clickup.com/mcp`), conectado como *connector* de claude.ai por OAuth | Configuración de la cuenta, no del repo |

**No existe ningún webhook saliente ni endpoint propio.** ClickUp no llama a nada nuestro: es
Claude quien llama a ClickUp por MCP. Y el hook no hace red — corre en cada turno, así que una
llamada de red ahí sería un impuesto por prompt.

Esta distinción importa al replicar: el conector MCP es **por cuenta** (se configura una vez y
sirve para todos los proyectos), mientras que el hook, la skill y el comando son **por repo** y
hay que copiarlos a cada proyecto.

---

## 1. Inventario de piezas

| Archivo | Qué hace | ¿Se versiona? | ¿Se parametriza? |
| --- | --- | --- | --- |
| `.claude/skills/{{SKILL_NOMBRE}}/SKILL.md` | El protocolo completo. Claude lo carga **solo**, cuando la petición matchea su `description` | Sí | **Sí** (Anexo A) |
| `.claude/commands/tarea.md` | Slash command `/tarea` con sus modos ejecutables | Sí | **Sí** (Anexo B) |
| `.claude/hooks/recordar-protocolo.sh` | Hook `UserPromptSubmit`: inyecta el recordatorio en **cada** prompt | Sí | No (Anexo C) |
| `.claude/settings.json` | Registra el hook en el harness | Sí | No |
| `.claude/.tarea-actual` | Claim local de una línea. Lo escribe RECLAMAR, lo borra CERRAR | **No** — gitignored | No |
| `TODO.md` (raíz) | Espejo del **detalle** de cada tarea | Sí | **Sí** |
| `CLAUDE.md` § "Gestión de tareas" | Resumen del protocolo en el contexto **siempre activo** | Sí | **Sí** (Anexo D) |
| `.claude/agents/frontend-planning.md` | **Opcional.** Convierte el handoff en un plan de frontend. La skill y el comando lo nombran | Sí | No — se copia tal cual |

### Por qué son tres capas y no una

No es redundancia: cada capa cubre un modo de fallo que las otras no.

1. **El hook** es lo único que el modelo **no puede omitir**. Lo ejecuta el harness, no el
   modelo, así que sobrevive a un contexto comprimido en una sesión larga y a que el agente
   simplemente no se acuerde. Es el piso.
2. **El bloque en `CLAUDE.md`** está siempre en contexto y da el resumen operativo: los estados,
   los recorridos y las cuatro reglas que rompen el mecanismo en silencio si se saltan. Alcanza
   para decidir bien sin cargar nada más.
3. **La skill** trae el protocolo completo, y se carga **solo cuando hace falta**. Meter sus 600
   líneas en `CLAUDE.md` las pondría en cada turno de cada conversación, incluidas las que no
   tocan tareas.

El **comando `/tarea`** es la vía ejecutable de la skill: para el uso diario ("reclamá P-07"),
un slash command es más barato y más determinístico que confiar en que el matcheo semántico
dispare.

**Si tuvieras que quedarte con una sola pieza, quedate con el hook.** Un protocolo perfecto que
se olvida no es un protocolo.

---

## 2. Requisito previo — el conector MCP de ClickUp

Se hace **una vez por cuenta**, no por proyecto.

1. En claude.ai → *Settings* → *Connectors*, conectá **ClickUp** (`https://mcp.clickup.com/mcp`)
   y completá el OAuth.
2. Verificá desde la terminal, en el proyecto nuevo:

   ```bash
   claude mcp list
   ```

   Tiene que aparecer:

   ```
   claude.ai ClickUp: https://mcp.clickup.com/mcp - ✔ Connected
   ```

**Si dice `! Needs authentication`, pará acá.** Todo lo demás depende de esto y un protocolo
apoyado en un conector caído produce lo peor posible: un agente que cree que reclamó la tarea.

### Las herramientas de ClickUp son *deferred*

No vienen con el esquema cargado: aparecen solo por nombre. Antes de invocarlas hay que traer
su esquema con `ToolSearch`:

```
ToolSearch(query: "select:clickup_filter_tasks,clickup_create_task,clickup_update_task,clickup_create_comment", max_results: 10)
```

Las que usa este flujo: `clickup_get_workspace_hierarchy`, `clickup_filter_tasks`,
`clickup_get_task`, `clickup_create_task`, `clickup_update_task`, `clickup_create_comment`,
`clickup_get_task_comments`, `clickup_add_task_link`, `clickup_attach_task_file`.

### Ojo: una skill **global** de ClickUp puede competir con esta

Antes de instalar nada, revisá si ya tenés una skill de ClickUp a nivel **usuario**:

```bash
fd . ~/.claude/skills -d 1 -t d | rg -i clickup
```

Las skills de `~/.claude/skills/` están activas en **todos** los proyectos. Si hay una ahí que
también habla de ClickUp, va a matchear los mismos pedidos que esta (`"postea las tareas en
ClickUp"`) y el agente puede cargar la equivocada — o las dos.

En la máquina donde se escribió esta plantilla existe
`~/.claude/skills/clickup-task-sync-sdd-phase-clickup-sync/`, y sus reglas **contradicen** al
protocolo del repo en cinco puntos:

| Tema | Skill global | Protocolo del repo |
| --- | --- | --- |
| Asignación | Siempre a un usuario fijo, sin preguntar | No asigna |
| Jerarquía | Tarea suelta por unidad de trabajo | **Subtarea** de la tarea paraguas, siempre |
| Nombre | Título imperativo libre | `P-XX — …` o `T-<YYMMDD>-<iniciales>-<slug>` |
| Idioma | Artefactos en inglés por defecto | Comentarios en el idioma del tablero, con el **email** adentro |
| Handoff / búsqueda | Sin `update required`; no exige `include_closed` | Ambos son **obligatorios** |

**No hay resolución automática de este conflicto**, así que decidí a conciencia:

1. **Desinstalar la global** si el flujo del repo la reemplaza (mové la carpeta fuera de
   `~/.claude/skills/`), **y** borrá su bloque de registro en `~/.claude/CLAUDE.md` si lo tiene
   — si no, el contexto global sigue mandando a cargar una skill que ya no existe.
2. **O convivir**, pero entonces acotá la `description` de cada una para que no se pisen: la
   global solo para proyectos sin protocolo propio, la del repo con un "en este repositorio"
   explícito al frente.

Dejar las dos con descripciones que se solapan es la peor opción: el comportamiento pasa a
depender de cuál matchee mejor ese día, y eso no se puede depurar.

---

## 3. Parámetros a recolectar ANTES de crear nada

Los IDs salen de una sola llamada:

```
clickup_get_workspace_hierarchy
```

| Marcador | Qué es | Ejemplo (repo origen) |
| --- | --- | --- |
| `{{PROYECTO}}` | Nombre del repo | `database-api-gateway` |
| `{{WORKSPACE_ID}}` | ID del workspace | `9017559023` |
| `{{ESPACIO_NOMBRE}}` / `{{ESPACIO_ID}}` | Espacio | `Cero208` / `90172691192` |
| `{{CARPETA_NOMBRE}}` / `{{CARPETA_ID}}` | Carpeta | `Desarrollo` / `901710687203` |
| `{{LISTA_NOMBRE}}` / `{{LISTA_ID}}` | Lista que recibe las tareas | `Database Gateway` / `901716272178` |
| `{{TAREA_PARAGUAS_ID}}` | Tarea principal de la que cuelgan **todas** las subtareas | `86e2xzf9d` |
| `{{SKILL_NOMBRE}}` | Nombre de la skill (kebab-case) | `clickup-task-flow` |
| `{{DOC_CONTRATO}}` | Doc que referencia el handoff al frontend | `docs/api-reference-vN.md` |
| `{{POR_QUE_IMPORTA}}` | Una o dos frases: **por qué** en ESTE proyecto el trabajo duplicado es caro | ver abajo |

### `{{POR_QUE_IMPORTA}}` no es relleno

En el repo origen dice:

> En un gateway que administra servidores de BD con credenciales pseudo-root, eso no es
> prolijidad: es riesgo operativo.

Es la frase que sostiene todo el protocolo. Un agente que sabe **por qué** una regla existe la
aplica bien en el caso que la regla no previó; uno que solo tiene la regla, la aplica al pie de
la letra y falla en cuanto aparece un borde. Escribí la tuya con el costo real de tu proyecto —
si no se te ocurre ninguno, probablemente no necesitás este protocolo.

---

## 4. Preparar el tablero en ClickUp

**Esto va primero, y en buena parte a mano.** La API vía MCP no crea espacios ni configura
estados de lista.

1. **Estados de la lista.** Configuralos en la UI de ClickUp. El flujo usa cinco:

   | Estado | Significado — **uno solo, y exacto** |
   | --- | --- |
   | `to do` | Libre, nadie la tomó |
   | `in progress` | Alguien la está haciendo — **es lo que reserva la tarea** |
   | `on hold` | Detenida: trabada por algo externo, **o quedó a medias** |
   | `update required` | **Backend listo, PENDIENTE DE FRONTEND.** Nada más |
   | `complete` | Cerrada del todo |

   Si tu lista trae estados de más (el repo origen tiene un `reviewed` heredado), **documentalos
   como no usados** en vez de dejarlos sin definir. Un estado sin significado declarado se lo
   inventa el primero que lo vea.

   **Los nombres tienen que coincidir carácter por carácter.** `clickup_update_task` manda el
   estado como string literal (`status: "update required"`), así que si en tu lista se llama
   `Update Required` o `pendiente frontend`, la llamada falla o —peor— el estado no cambia y el
   agente sigue creyendo que reclamó la tarea. Si los renombrás, actualizá los strings en la
   skill y en el comando: `rg -n 'update required|in progress|on hold' .claude/`.

2. **La tarea paraguas.** Creá una tarea en la lista que represente al proyecto entero. Su ID es
   `{{TAREA_PARAGUAS_ID}}`. **Todas** las unidades de trabajo van como subtareas de esa: nunca
   una tarea suelta en la lista.

3. **Los tags, antes de usarlos.** `clickup_add_tag_to_task` **falla si el tag no existe** en el
   espacio. Creá los que vayas a usar desde la UI, o no uses tags.

---

## 5. Crear los archivos

### 5.1 `.claude/settings.json`

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PROJECT_DIR}/.claude/hooks/recordar-protocolo.sh\""
          }
        ]
      }
    ]
  }
}
```

Si el archivo ya existe, **fusioná la clave `hooks`**, no lo sobrescribas.

### 5.2 `.claude/hooks/recordar-protocolo.sh`

Copiá el **Anexo C** y dale permiso de ejecución:

```bash
chmod +x .claude/hooks/recordar-protocolo.sh
```

### 5.3 `.claude/skills/{{SKILL_NOMBRE}}/SKILL.md`

Copiá el **Anexo A** y reemplazá todos los marcadores.

### 5.4 `.claude/commands/tarea.md`

Copiá el **Anexo B** y reemplazá todos los marcadores.

### 5.5 `.gitignore`

```gitignore
# Claim local del protocolo de tareas — NUNCA se versiona
.claude/.tarea-actual

# Estado de máquina de Claude Code (no es del repo)
.claude/settings.local.json
.claude/plans/
.claude/.cache/
.claude/*.tmp
.claude/worktrees/
```

Fijate en lo que **NO** está en esa lista: `.claude/settings.json`, `.claude/hooks/`,
`.claude/skills/` y `.claude/commands/` **sí se versionan**. Son el protocolo, y si no viajan con
el repo cada persona trabaja con reglas distintas. Lo que se ignora es lo que es **de tu máquina**:
el claim, los permisos locales, los planes y los worktrees.

**El claim local no se versiona.** Es estado de esta máquina y de esta persona: commitearlo haría
que el repo afirme que vos tenés una tarea tomada en la máquina de otro.

### 5.6 `TODO.md` (raíz)

Esqueleto mínimo — el detalle lo va llenando el uso:

```markdown
# TODO — {{PROYECTO}}

> **Espejo detallado de la tarea de ClickUp.** Este archivo es la fuente de verdad del
> **detalle**; ClickUp es la fuente de verdad del **estado** y de **quién** está trabajando.
> El protocolo completo está en la skill `{{SKILL_NOMBRE}}`. No se trabaja nada sin pasar por ahí.

## Tarea principal en ClickUp

| Campo | Valor |
| --- | --- |
| **Task ID** | `{{TAREA_PARAGUAS_ID}}` |
| **URL** | https://app.clickup.com/t/{{TAREA_PARAGUAS_ID}} |
| **Espacio** | {{ESPACIO_NOMBRE}} (`{{ESPACIO_ID}}`) |
| **Carpeta** | {{CARPETA_NOMBRE}} (`{{CARPETA_ID}}`) |
| **Lista** | {{LISTA_NOMBRE}} (`{{LISTA_ID}}`) |
| **Workspace** | `{{WORKSPACE_ID}}` |

## 🔴 Pendientes

| # | Ítem | Detalle | Subtarea |
| --- | --- | --- | --- |
| P-01 | <título> | <detalle largo> | — |

## 🟡 En curso

| # | Ítem | Ejecutor | Desde | Subtarea |
| --- | --- | --- | --- | --- |

## 🔵 Pendiente de frontend

| # | Ítem | Resumen del handoff | Subtarea |
| --- | --- | --- | --- |

## 🟢 Realizadas

| # | Ítem | Qué se hizo | Qué quedó SIN verificar | Subtarea |
| --- | --- | --- | --- | --- |
```

**La columna "Qué quedó SIN verificar" no es opcional.** Es normal que algo no se haya probado;
lo que no es aceptable es que no esté dicho.

**El ID de la subtarea se anota como link Markdown**, no pelado:
`[86e2y1abc](https://app.clickup.com/t/86e2y1abc)`. Un ID suelto obliga a armar la URL a mano
cada vez.

### 5.7 El bloque de `CLAUDE.md`

Va **arriba de todo**, antes de la descripción del proyecto: es lo primero que tiene que leer
cualquier agente. Contenido en el **Anexo D** — como los otros, se deriva del archivo real, así
que no puede quedar desfasado del protocolo que describe.

---

## 6. Verificación — el orden importa

Hacelo en este orden: cada paso depende del anterior.

```bash
# 0. No hay una skill GLOBAL de ClickUp compitiendo con la del repo
fd . ~/.claude/skills -d 1 -t d | rg -i clickup
#    Esperado: sin resultados — o una que ya acotaste a conciencia (ver §2)

# 1. El conector responde
claude mcp list | rg -i clickup

# 2. El hook es ejecutable y no falla
bash .claude/hooks/recordar-protocolo.sh; echo "exit=$?"
#    Esperado: el texto "Ninguna tarea reclamada en este repo." y exit=0

# 3. El hook detecta un claim
echo "P-01 — prueba (subtarea 86xxxx, reclamada por vos@ejemplo.com)" > .claude/.tarea-actual
bash .claude/hooks/recordar-protocolo.sh; echo "exit=$?"
#    Esperado: "TAREA EN CURSO: P-01 — prueba …" y exit=0
rm -f .claude/.tarea-actual

# 4. El claim está ignorado
git check-ignore -v .claude/.tarea-actual
#    Esperado: una línea señalando la regla del .gitignore

# 5. No quedaron marcadores sin reemplazar
rg -n '\{\{[A-Z_]+\}\}' .claude/ TODO.md CLAUDE.md
#    Esperado: SIN resultados
```

Después, **dentro de Claude Code**, en una sesión nueva:

| Prueba | Qué confirma | Resultado esperado |
| --- | --- | --- |
| Mandá cualquier prompt | El hook corre en cada turno | Aparece la línea `[protocolo de tareas] …` |
| `/tarea estado` | El comando existe y el MCP responde | Lista el estado real del tablero |
| "arreglá el bug X" (sin reclamar) | La skill se auto-dispara | El agente **para** y pide reclamar primero |

**La tercera es la que de verdad importa.** Las dos primeras prueban que los archivos están; la
tercera prueba que el mecanismo **cambia el comportamiento del agente**, que es el punto.

---

## 7. Invariantes — lo que NO se toca al adaptar

Cada una existe porque su ausencia produce un fallo **silencioso**. Cambiarlas rompe el
mecanismo sin que nada avise.

| Invariante | Qué pasa si se saca |
| --- | --- |
| **`include_closed: true` en toda búsqueda** | Viene **apagado por defecto**. Sin él, una tarea ya `complete` no aparece y se crea un duplicado exacto |
| **La identidad es el EMAIL, nunca el nombre** | Todos los comentarios se publican con la cuenta del token del MCP, así que el campo "autor" de ClickUp **no sirve** para detectar colisiones. Y la misma persona en dos máquinas suele tener nombres distintos → se ve como dos personas y el protocolo la interrumpe contra sí misma |
| **Re-verificar duplicados después de crear** | ClickUp no tiene locks ni "crear si no existe" atómico. La ventana entre buscar y crear **no se puede cerrar**; solo detectar. Sin esto, el duplicado es invisible hasta el merge |
| **Desempate determinístico** (`date_created` más antiguo; si empatan, `id` menor) | Sin una regla determinística, los dos lados de la carrera concluyen cosas distintas y siguen los dos |
| **IDs nuevos con fecha + iniciales, nunca "el siguiente `P-XX`"** | Un esquema secuencial **colisiona**: dos personas que arrancan a la vez calculan el mismo, y el prefijo anti-duplicados termina apuntando a dos trabajos distintos |
| **`in progress` ANTES de escribir código** | Es lo único que reserva la tarea. Después de escribir el código, ya perdiste la carrera |
| **Un estado, un significado** | Si `update required` significa además "quedó a medias", el filtro del frontend se llena de ruido y el mecanismo pierde el sentido |
| **El hook nunca falla ni bloquea** | Sin `set -e` y con `exit 0` incondicional. Un hook que rompe el prompt se desactiva a los dos días, y con él todo el protocolo |
| **El hook no hace red** | Corre en **cada** turno. Una llamada HTTP ahí es un impuesto por prompt |
| **Nada sensible a ClickUp** | Ni credenciales, ni `.env`, ni datos de clientes — **tampoco en adjuntos**. ClickUp es un sistema externo: lo que se escribe ahí sale del repo |

---

## 8. Lo que SÍ se adapta

- **Los IDs y nombres.** Todos los `{{MARCADORES}}` de §3.
- **`{{POR_QUE_IMPORTA}}`.** Escribí el costo real de tu proyecto, no copies el del origen.
- **El nombre del comando.** `/tarea` es arbitrario; renombralo (`/task`, `/ticket`) cambiando el
  nombre del archivo en `.claude/commands/`. **Ojo:** el hook y la skill lo referencian por
  nombre, y a diferencia del nombre de la skill no está parametrizado. Corré
  `rg -n '/tarea' .claude/` antes de dar el rename por hecho — si queda una referencia vieja,
  el recordatorio del hook manda a un comando que no existe.
- **El esquema de IDs del backlog.** `P-XX` es una convención del origen. Lo que **no** se
  negocia es que el ID sea **estable y no calculado en el momento**.
- **El agente `frontend-planning`.** La skill y el comando lo nombran como consumidor natural del
  handoff, en **tres** lugares. Si tu proyecto no lo tiene: `rg -n 'frontend-planning' .claude/`
  y sacá las referencias — una skill que manda a un agente inexistente hace que el agente salga
  a buscarlo y pierda el turno. Si lo querés, copiá también
  `.claude/agents/frontend-planning.md` del repo origen: es una pieza independiente del flujo,
  no está en los anexos.
- **La política de tests.** La skill dice "es normal que algo quede sin verificar (ver la política
  de tests en `CLAUDE.md`)". Ajustala a la tuya.

### Si tu proyecto no tiene frontend separado

Esto es más que borrar líneas, así que decidilo a conciencia:

1. Sacá el estado **`update required`** y todo el bloque de handoff.
2. El ciclo colapsa a: `to do → in progress → complete`.
3. **No reutilices `update required` para otra cosa.** Si lo necesitás para "esperando review",
   está bien — pero entonces documentá **ese** significado y solo ese. La regla que sostiene el
   flujo es *un estado, un significado*, no cuáles son los estados.
4. `on hold` **se queda**. Es el estado de "quedó a medias" y no tiene reemplazo.

### Si trabaja una sola persona

El protocolo sigue teniendo sentido, pero por otra razón: deja de ser anti-colisión y pasa a ser
**registro**. En ese caso podés relajar la re-verificación de duplicados (no hay con quién
correr), pero **conservá `include_closed: true`** — la carrera que evita no es solo contra otra
persona, también contra tu propio yo de hace tres semanas.

---

## 9. Límites conocidos de la API vía MCP

Verificados en el proyecto origen, no asumidos:

- **No existe** herramienta para crear **espacios**. Solo carpetas, listas, tareas, documentos.
- **No se puede mover una lista** entre espacios o carpetas: `clickup_update_list` solo cambia
  `name` / `content` / `status`.
- **No existe** `delete_list` ni `delete_folder`.
- **Sí** se puede mover una tarea (`clickup_move_task`) y **el ID sobrevive** el movimiento.
- Una carpeta creada con `override_statuses: false` **hereda** los estados del espacio.
- `clickup_add_tag_to_task` **falla si el tag no existe** en el espacio: hay que crearlo antes a
  mano desde la UI.
- **Tres niveles de jerarquía funcionan** (tarea → subtarea → sub-subtarea) y el `parent` se
  respeta: no se aplana a la raíz. El **cuarto nivel no se probó** — la regla de parar en tres es
  de diseño, no un límite técnico conocido.
- **Todos los comentarios se publican con la cuenta del token**, sin importar quién ejecute. Por
  eso la identidad va dentro del texto.

---

## 10. Prompt de arranque

Para que Claude Code lo monte solo en el proyecto nuevo. Pegalo tal cual, con este documento
accesible en el repo:

```
Leé la plantilla en <ruta>/plantilla-flujo-clickup.md y montá el flujo de tareas de ClickUp
en este proyecto.

Antes de crear ningún archivo:

1. Verificá que el conector MCP de ClickUp responda (`claude mcp list`). Si no, pará y avisá.
2. Traé la jerarquía con `clickup_get_workspace_hierarchy` y mostrame los espacios, carpetas y
   listas que encontrás.
3. Preguntame, UNA pregunta por vez, qué lista recibe las tareas y cuál es la tarea paraguas
   (o si hay que crearla).
4. Mostrame la tabla de marcadores de la §3 con los valores que vas a usar, y esperá mi OK.

Recién con mi OK: creá los archivos de la §5 con los marcadores reemplazados, y corré la
verificación de la §6. Reportame qué pasó cada chequeo y qué quedó sin verificar.

No inventes IDs de ClickUp. Si algo no lo podés resolver, preguntá en vez de asumir.
```

---


## Anexo A — `.claude/skills/{{SKILL_NOMBRE}}/SKILL.md`

El protocolo completo. Claude lo carga solo cuando la petición matchea su `description`,
así que **esa descripción es el disparador** — si la reescribís, dejale los verbos de
acción y el "usar SIEMPRE antes de empezar".

````markdown
---
name: {{SKILL_NOMBRE}}
description: Protocolo obligatorio de gestión de tareas del proyecto {{PROYECTO}} — validar, reclamar, comentar y cerrar tareas en ClickUp vía MCP, decidir si algo es tarea nueva o va sobre una existente, hacer el handoff al frontend, y mantener TODO.md sincronizado. Usar SIEMPRE antes de empezar cualquier tarea, implementación, fix, verificación o refactor en este repo, y otra vez al terminarla. También al preguntar qué hay pendiente o en qué estado está algo.
---

# Gestión de tareas: ClickUp + TODO.md

Este protocolo existe para que **dos personas no trabajen lo mismo en paralelo**.
{{POR_QUE_IMPORTA}}

## Coordenadas

| Campo | Valor |
| --- | --- |
| Tarea principal (paraguas) | `{{TAREA_PARAGUAS_ID}}` |
| URL | https://app.clickup.com/t/{{TAREA_PARAGUAS_ID}} |
| Espacio | {{ESPACIO_NOMBRE}} (`{{ESPACIO_ID}}`) |
| Carpeta | {{CARPETA_NOMBRE}} (`{{CARPETA_ID}}`) |
| Lista | {{LISTA_NOMBRE}} (`{{LISTA_ID}}`) |
| Workspace | `{{WORKSPACE_ID}}` |

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

La jerarquía normal es **dos niveles**: tarea principal `{{TAREA_PARAGUAS_ID}}` → subtareas. Y se queda ahí.

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
| **Fix** de algo que ya está `complete` | **Misma tarea: se REABRE** a `in progress` con un comentario que diga qué se rompió y por qué se reabre. Cerrarla de nuevo al terminar. |
| **Feature** que extiende la tarea sin cambiar su objetivo | **Misma tarea.** Se actualiza la descripción + comentario. |
| **Feature** que cambia el objetivo, o toca módulos que la original no tocaba | **Tarea nueva, vinculada.** |
| Alguien quiere **rehacer** desde cero algo ya `complete` | **Tarea nueva, vinculada.** No es un fix: es trabajo distinto sobre el mismo terreno. |

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

## Paso 1 — Validar antes de empezar (sin excepciones)

**1.1** Buscá el ítem en `TODO.md`. Si la columna `Subtarea` ya tiene un ID → `clickup_get_task`
con ese ID. Camino corto y exacto.

**1.2** Si no tiene ID, buscá en ClickUp **antes de crear nada**:

```
clickup_filter_tasks
  list_ids:        ["{{LISTA_ID}}"]
  include_closed:  true          ← OBLIGATORIO
  subtasks:        true
```

**`include_closed: true` no es opcional.** Viene **apagado por defecto**, así que sin él una
tarea ya `complete` **no aparece** y se crea un duplicado exacto. Si la respuesta trae
`has_more: true`, paginá con `next_page` hasta que sea `false`.

**1.3** Compará por el **prefijo del ID**, no por el título.

### Qué hacer según lo que se encuentre

| Estado | Acción |
| --- | --- |
| `in progress` | Leé el último `INICIO` con `clickup_get_task_comments` para saber **quién** la tiene, **desde cuándo** y con qué **rol**. Si venís a hacer lo mismo que esa persona → **INTERRUMPIR**. Si el `INICIO` es de **frontend** y vos venís a cambiar backend → **no la toques: sub-subtarea** (ver "El backend necesita volver a tocar…"). |
| `update required` | **Backend hecho, falta el frontend.** Si lo que venís a hacer es el **frontend**, tomala. Si venís a hacer **más backend**, avisá que hay un handoff pendiente y decidí con el usuario si es un fix de esa tarea o trabajo nuevo. |
| `complete` | **NO es un portazo:** informá que ya se hizo, con el resumen del comentario `FIN`. Después aplicá la prueba de "¿tarea nueva o la misma?" (arriba): si es un **fix**, se **reabre**; si es trabajo distinto, **tarea nueva vinculada**. |
| `to do` | Libre. Se puede tomar. |
| `on hold` | **Leé primero el último comentario**, que dice por qué se detuvo y dónde quedó. Si es un **`BLOQUEADO POR BACKEND`**, el frontend te la devolvió: es trabajo tuyo y tiene prioridad — hay una implementación de frontend parada esperándote (ver "El frontend devolvió una tarea"). Si no, es una tarea que quedó a medias y se puede retomar normalmente. |
| `reviewed` | Estado **no usado** en este flujo. Preguntá antes de asumir qué significa. |
| No existe | Crear la subtarea (abajo). |

### Crear la subtarea (solo si de verdad no existe)

```
clickup_create_task
  name:     "P-XX — <título>"        ← ítem del backlog de TODO.md
            "T-260821-oc-<slug>"     ← ítem nuevo: NUNCA el siguiente P-XX libre
  list_id:  "{{LISTA_ID}}"
  parent:   "{{TAREA_PARAGUAS_ID}}"              ← siempre subtarea, nunca tarea suelta
```

Después de crearla, **en este orden**:

1. **Re-verificá** que no haya duplicado (ver "La ventana de colisión").
2. Si deriva de otra tarea, **vinculala** con `clickup_add_task_link`.
3. Escribí el ID devuelto en la columna `Subtarea` de `TODO.md`, **como link Markdown**:
   `[86e2y1abc](https://app.clickup.com/t/86e2y1abc)`. Un ID pelado obliga a armar la URL a mano.
4. Si el ítem era nuevo, agregalo a la tabla de 🔴 Pendientes con su ID `T-…`.

## Paso 2 — Al empezar

```
clickup_update_task
  task_id: "<subtarea>"
  status:  "in progress"
```

**Esto es lo que reserva la tarea.** Va **antes** de escribir la primera línea de código.

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
clickup_update_task  →  status: "complete"
clickup_create_comment  →  bloque FIN
```

**Criterio para decir "no requiere frontend" con honestidad:** no alcanza que *vos* no hayas
tocado el frontend. Hay que poder afirmar que **nada de lo que el frontend ya consume cambió**:
ni rutas, ni forma de la respuesta, ni códigos de error, ni campos obligatorios del request.
Si tenés dudas, **va a `update required`** — un handoff de más cuesta un comentario; uno de
menos deja el frontend roto sin que nadie se entere.

### 3b. SÍ requiere frontend → `update required` + handoff

```
clickup_update_task  →  status: "update required"
clickup_create_comment  →  bloque HANDOFF FRONTEND
```

La tarea **no se cierra**. Queda en `update required` hasta que el frontend termine, y **es el
frontend quien la pasa a `complete`**. Así el equipo de frontend filtra por ese estado y
encuentra exactamente lo que le toca, con el contexto ya escrito — sin crear tareas nuevas ni
salir a buscar qué cambió.

### En los dos casos

Mové el ítem en `TODO.md`:
- `complete` → **🟢 Realizadas**, con el detalle completo: archivos tocados, decisiones, y muy
  especialmente **qué quedó sin verificar**.
- `update required` → sección **🔵 Pendiente de frontend**, con el resumen del handoff.

Es normal que algo quede sin verificar (ver la política de tests en `CLAUDE.md`); lo que no es
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
**Contrato:** `{{DOC_CONTRATO}}` § <sección> — commit `<hash corto>`
**Qué tiene que hacer el frontend:** <2 o 3 líneas concretas, no "implementar la UI">
```

### El contrato se REFERENCIA, no se adjunta

Poné **ruta + sección + hash del commit**. Un archivo adjunto queda **congelado**: en cuanto el
doc cambia, el adjunto miente y el frontend implementa contra un contrato viejo. El hash le dice
exactamente qué versión mirar.

**Excepción:** si el equipo de frontend **no tiene acceso al repo del backend**, ahí sí adjuntá el
`{{DOC_CONTRATO}}` con `clickup_attach_task_file` — pero dejá igual el hash en el
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
siendo lo que reserva la tarea.

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
**Contrato:** `{{DOC_CONTRATO}}` § <sección> — commit `<hash nuevo>`
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
**Contrato:** `{{DOC_CONTRATO}}` § <sección> — commit `<hash nuevo>`
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
  list_id: "{{LISTA_ID}}"
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
| No existe la subtarea | `create_task` (`parent: {{TAREA_PARAGUAS_ID}}`) + `add_task_link` si deriva | Se anota el ID devuelto |
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
````


## Anexo B — `.claude/commands/tarea.md`

El slash command `/tarea`. Es la vía ejecutable de la skill: cinco modos (RECLAMAR,
CERRAR, PENDIENTES DE FRONTEND, CONSULTAR, RESOLVER) resueltos por el argumento.

````markdown
---
description: Valida, reclama o cierra una tarea del proyecto en ClickUp siguiendo el protocolo anti-colisión
---

Argumento recibido: `$ARGUMENTS`

Cargá primero la skill `{{SKILL_NOMBRE}}` (es la fuente del protocolo) y ejecutá el modo que
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
   Si no → `clickup_filter_tasks` con `list_ids: ["{{LISTA_ID}}"]`, **`include_closed: true`**,
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
   - **`complete`** → **no es un portazo.** Informá el resumen del comentario `FIN` y aplicá la
     prueba del objetivo declarado:
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
       name:    "P-XX — <título del ítem en TODO.md>"   ← ítem del backlog
                "T-<YYMMDD>-<iniciales>-<slug>"          ← ítem NUEVO
       # iniciales = parte local del email, 8 chars:
       # git config user.email | cut -d@ -f1 | tr -cd "a-z0-9" | cut -c1-8
       list_id: "{{LISTA_ID}}"
       parent:  "{{TAREA_PARAGUAS_ID}}"
     ```
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
   - `clickup_update_task` → `status: "in progress"` (esto es lo que la reserva)
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
  list_id: "{{LISTA_ID}}"
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

1. Resolvé la identidad del ejecutor: **`git config user.email`** (solo el email).
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

- `clickup_update_task` → `status: "complete"`
- `clickup_create_comment` → bloque `FIN`, con **`Sin verificar:`** completo y honesto
- Mové el ítem a **🟢 Realizadas** en `TODO.md` con el detalle completo

### 4b. SÍ requiere frontend

- `clickup_update_task` → `status: "update required"` (**no** `complete`)
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
  **Contrato:** `{{DOC_CONTRATO}}` § <sección> — commit `<hash corto>`
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

- `clickup_update_task` → `status: "update required"`
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
  **Contrato:** `{{DOC_CONTRATO}}` § <sección> — commit `<hash nuevo>`
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
  list_ids: ["{{LISTA_ID}}"]
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
  list_ids: ["{{LISTA_ID}}"]
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

**2. Reclamala:** `clickup_update_task` → `status: "in progress"`, y comentario `INICIO` con
**`Rol: backend`**.

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
**Contrato:** `{{DOC_CONTRATO}}` § <sección> — commit `<hash nuevo>`
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

```
clickup_filter_tasks
  list_ids:       ["{{LISTA_ID}}"]
  include_closed: true
  subtasks:       true
```

Paginá hasta `has_more: false` y presentá:

- Qué está **`in progress`**, con quién la tiene (del comentario `INICIO`) y desde cuándo
- Qué está **`update required`** — o sea, backend listo y **frontend pendiente**
- Qué está **`on hold`**, con el motivo y dónde quedó. **Separá las que tengan un comentario
  `BLOQUEADO POR BACKEND`**: no son tareas dormidas, es el frontend devolviéndote trabajo con una
  implementación parada del otro lado. Van primero
- Qué ítems de `TODO.md` siguen sin subtarea (libres para tomar)
- **Duplicados sospechosos**: dos o más subtareas con el mismo ID, o con slugs equivalentes
  creados el mismo día. Es el residuo de una carrera perdida que nadie detectó a tiempo —
  reportalos con sus `date_created` para que se pueda decidir cuál sobrevive
- Si aparece alguna en **`reviewed`**: señalala como anomalía, ese estado no se usa

---

## Reglas que no se negocian en ningún modo

- **`include_closed: true`** en toda búsqueda. Viene apagado por defecto y sin él una tarea ya
  terminada no aparece → se crea un duplicado exacto.
- **La identidad del ejecutor es el EMAIL** (`git config user.email`), no el nombre, y va DENTRO
  del texto del comentario. El campo "autor" de ClickUp
  siempre dice la cuenta del token, así que no sirve para detectar colisiones.
- **Toda subtarea cuelga de `{{TAREA_PARAGUAS_ID}}`.** Nunca una tarea suelta en la lista.
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
````


## Anexo C — `.claude/hooks/recordar-protocolo.sh`

El hook `UserPromptSubmit`. **No lleva marcadores**: se copia tal cual.
Acordate del `chmod +x`.

````bash
#!/usr/bin/env bash
# Hook UserPromptSubmit — recuerda el protocolo de gestión de tareas en cada prompt.
#
# Lo ejecuta el harness, no el modelo: por eso el recordatorio no se puede "olvidar"
# ni diluir cuando el contexto se comprime en una sesión larga. Lo que salga por stdout
# se inyecta en el contexto del turno.
#
# Protocolo completo: skill `{{SKILL_NOMBRE}}`. Detalle de las tareas: TODO.md.
#
# REGLA DE ORO: este hook NUNCA debe fallar ni bloquear el prompt. Sin `set -e`, y
# `exit 0` incondicional al final. Tampoco hace llamadas de red: corre en cada turno.

set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
CLAIM="$PROJECT_DIR/.claude/.tarea-actual"

if [ -f "$CLAIM" ]; then
    # Una sola línea, acotada: el archivo es local y editable a mano.
    EN_CURSO="$(tr -d '\r\n' < "$CLAIM" 2>/dev/null | cut -c1-200)"
    if [ -n "$EN_CURSO" ]; then
        printf '[protocolo de tareas] TAREA EN CURSO: %s\nAl terminar cerrala con `/tarea fin <P-XX>` (pone `complete` en ClickUp + comentario FIN y mueve el ítem a Realizadas en TODO.md). Si la abandonás a mitad, va a `on hold` o `update required`, nunca se deja en `in progress`.\n' "$EN_CURSO"
        exit 0
    fi
fi

printf '%s\n' '[protocolo de tareas] Ninguna tarea reclamada en este repo. Si lo que sigue es implementar, arreglar, verificar o refactorizar algo, primero invocá la skill `{{SKILL_NOMBRE}}` (o `/tarea P-XX`) para validar en ClickUp que nadie más la esté haciendo. Antes de sacar trabajo nuevo del backlog, mirá `/tarea bloqueos`: son las tareas que el frontend devolvió en `on hold` porque el backend no cumplió el handoff, no salen en ningún otro filtro, y del otro lado hay una implementación parada. Responder preguntas, leer código o explicar cosas NO requiere reclamar tarea.'
exit 0
````


## Anexo D — `CLAUDE.md § "Gestión de tareas"`

El bloque que va **al inicio** de `CLAUDE.md`, antes de la descripción del proyecto.
Es el resumen que está **siempre** en contexto: alcanza para decidir bien sin cargar la
skill entera.

````markdown
## Gestión de tareas — OBLIGATORIO ANTES DE TRABAJAR

**Antes de empezar cualquier tarea en este repo, invocá la skill `{{SKILL_NOMBRE}}`.** Ahí está
el protocolo completo. Existe para que dos personas no trabajen lo mismo en paralelo, que en un
gateway con credenciales pseudo-root es riesgo operativo, no prolijidad.

- El **detalle** de cada tarea vive en `TODO.md` (raíz). El **estado** y **quién trabaja** viven
  en ClickUp: tarea principal `{{TAREA_PARAGUAS_ID}}`, lista `{{LISTA_ID}}`. Ante discrepancia, para el
  estado gana ClickUp.
- Ciclo ejecutable: **`/tarea P-XX`** para reclamar, **`/tarea fin P-XX`** para cerrar,
  **`/tarea frontend`** para ver lo que espera implementación visual, **`/tarea estado`** para
  ver qué hay en curso.
- Si una tarea está `in progress`, **se interrumpe** y se informa quién la tiene. No se avanza.

**Los estados significan exactamente esto, y solo esto:**

| Estado | Significa |
| --- | --- |
| `to do` | Libre |
| `in progress` | Alguien la está haciendo — **es lo que la reserva** |
| `on hold` | Detenida: trabada por algo externo, **o quedó a medias**. También es **tu bandeja de entrada**: es donde el frontend devuelve lo que el backend no cumplió |
| `update required` | **Backend listo, PENDIENTE DE FRONTEND.** Nada más |
| `complete` | Cerrada del todo |
| `reviewed` | **No se usa en este flujo** |

**Al terminar hay una bifurcación:** si el cambio necesita implementación visual, la tarea va a
`update required` con un comentario `HANDOFF FRONTEND` (endpoints, breaking changes, schemas, y
el contrato referenciado con su hash de commit) y **no se cierra** — la pasa a `complete` el
frontend. Si no necesita frontend, `complete` directo. Para decir que no lo necesita hay que
poder afirmar que **nada de lo que el frontend ya consume cambió**; ante la duda,
`update required`.

**REGLA DURA — la jerarquía es de DOS niveles:** tarea principal `{{TAREA_PARAGUAS_ID}}` → subtareas. Las
**sub-subtareas** (tercer nivel) existen para **un solo escenario de emergencia**: el backend
necesita cambiar algo de una tarea que el frontend **está haciendo en ese momento**. **En ningún
otro caso.** No se usan para descomponer trabajo grande (eso va en el detalle del ítem en
`TODO.md`, o como tareas hermanas vinculadas), ni para agrupar, ni para separar backend de frontend
(eso lo hace el estado), ni "para que quede más ordenado". Si aparece un caso que parece
justificarlo, **no lo crees: planteáselo al usuario** y que se establezca como regla nueva.

**Los recorridos, explícitos** (`be` = backend, `fe` = frontend; las transiciones se componen):

```
backend puro              to do → in progress(be) → complete
backend con frontend      to do → in progress(be) → update required → in progress(fe) → complete(fe)
fix cerrado sin frontend  complete → in progress(be) → complete
fix cerrado con frontend  complete → in progress(be) → update required → in progress(fe) → complete(fe)
el fe devuelve la tarea   update required → in progress(fe) → on hold → in progress(be) → update required → in progress(fe) → complete(fe)
                                            (choca)          (BLOQUEADO   (DESBLOQUEO)
                                                              POR BACKEND)
```

Un fix de algo `complete` que ahora afecta al frontend pasa **por `in progress`** (la reapertura es
lo que reserva la tarea) y termina en `update required`, no en `complete`.

**El frontend también reclama:** al tomar algo en `update required` lo pasa a `in progress` con un
`INICIO` de rol frontend. Por eso **el `INICIO` declara el rol obligatoriamente** — `in progress`
no dice por sí solo si es backend o frontend.

**Si el backend necesita volver a tocar algo ya entregado al frontend, depende de si el frontend
empezó:**

| Estado | Qué hace el backend |
| --- | --- |
| `update required` (el fe no la tomó) | **Reabre la misma tarea:** `HANDOFF INVALIDADO` con `notify_all` antes de tocar código, y al cerrar un handoff de re-entrega con el **delta** (incluyendo **qué NO cambió**, para que no se rehaga trabajo bueno). Si el cambio **no toca el contrato**, se queda en `update required` y solo se comenta. |
| `in progress` con `INICIO` de **frontend** | **No la toca.** Crea una **sub-subtarea** (`parent` = esa subtarea; verificado que ClickUp acepta 3 niveles) + `add_task_link` + comentario `TRABAJO DERIVADO` con `notify_all` en la madre, declarando el **impacto** en lo que el frontend está haciendo. Así nadie se bloquea: el fe no pierde su trabajo y el be no espera. **No hay cuarto nivel.** |

**El camino de VUELTA del handoff: `BLOQUEADO POR BACKEND`.** Cuando el frontend empieza a
implementar y descubre que el backend no entrega lo que el handoff prometía —otra forma de
respuesta, falta un campo, el código de error no es el documentado, la ruta no existe— **no puede
devolverla a `update required`**: ese estado significa "falta el frontend", así que la dejaría en
su propio filtro y el backend no se enteraría nunca. La deja en **`on hold`** con un comentario
`BLOQUEADO POR BACKEND` y `notify_all: true`.

**Esas tareas no aparecen en ningún filtro que mires por costumbre** —no están en `to do`, ni en
`in progress`, ni en `update required`—, y del otro lado hay una implementación parada. Es el único
tramo del flujo con alguien esperando. Revisalas con **`/tarea bloqueos`** al arrancar el día,
antes de sacar trabajo nuevo del backlog.

Al recibir una: **verificá el bloqueo antes de aceptarlo.** Si el backend sí cumplía y el frontend
leyó mal el contrato, **no cambies el backend** — comentá con la evidencia y devolvela a
`update required`. Si el error estaba en el **documento** y no en el código, arreglá el doc. Si el
bloqueo es real: `in progress` (rol backend) → arreglá **solo lo que desbloquea**, porque el campo
`Dónde quedé` te dice qué implementación de frontend no podés romper → cerrá en **`update required`**
—nunca en `complete`— con un `FIN BACKEND (DESBLOQUEO)` que incluya **`Sigue sin resolverse`**. Ese
campo es el que más se omite: un desbloqueo parcial anunciado como completo hace que la tarea
rebote una segunda vez.

**Un fix de algo ya `complete` NO crea tarea nueva: se reabre la existente.** Tarea nueva solo
cuando el trabajo no se puede describir sin cambiar el objetivo declarado de la original, y en ese
caso se vincula con `clickup_add_task_link`.

**Cuatro reglas que si se saltan rompen el mecanismo en silencio:** (1) las subtareas del backlog
se llaman **`P-XX — <título>`** y las **nuevas** `T-<YYMMDD>-<iniciales>-<slug>` — para algo nuevo
**nunca** el siguiente `P-XX` libre, que es secuencial y dos personas simultáneas calculan el
mismo; (2) toda búsqueda va con **`include_closed: true`** (viene apagado por defecto, y sin él
una tarea ya terminada no aparece y se duplica); (3) después de crear una subtarea se
**re-verifica** que no haya duplicado antes de trabajar, porque la ventana entre buscar y crear no
se puede cerrar (gana la de `date_created` más antiguo); (4) la **identidad (que es el EMAIL de `git config user.email`, NUNCA el nombre: este repo tiene 4 nombres para un mismo email) del ejecutor va dentro
del texto** del comentario, porque el campo "autor" de ClickUp siempre dice la cuenta del token.
````
