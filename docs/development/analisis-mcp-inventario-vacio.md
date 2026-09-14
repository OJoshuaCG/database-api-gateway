# Análisis del MCP: qué funciona, por qué `list_databases` viene vacío y cómo repararlo

Validación ejecutada contra el despliegue `http://db-gateway.dokploy.z/mcp` y contra una
réplica local del gate. Todo lo que dice este documento se corrió; lo que no se pudo correr
está declarado en la última sección.

---

## Resumen

**El MCP no está roto.** El transporte y la superficie de seguridad pasan 9 de 9 controles
contra producción. La lista vacía tampoco es un fallo: es el gate negando por default.

Ahora bien, el gate exige **ocho condiciones simultáneas**, y la documentación operativa
describe cinco. Falta justamente la que más pega en un inventario que creció por adopción, y
hay un defecto que impide diagnosticar el problema desde la API. Ese es el trabajo real.

| | Estado |
|---|---|
| Transporte MCP (JSON-RPC, status, headers, eras) | ✅ conforme, 9/9 |
| Autenticación y aislamiento del token | ✅ conforme |
| Superficie de tools | ⚠️ **una sola tool**: `list_databases` |
| Visibilidad del inventario | ⛔ vacía — 8 condiciones, todas `AND`, todas cerradas por default |
| Diagnóstico desde la API | ⛔ **imposible hoy**: el flag que decide se puede escribir pero no leer |

---

## Método

1. **Batería contra producción** con el token de agente real: 9 casos de transporte, errores
   y seguridad.
2. **Réplica local del gate**: se construyó la cadena completa (servidor → propietario →
   blueprint → proyecto → entorno → base) sobre una BD de metadatos efímera, y se apagó **una
   condición por vez** llamando a `reachable_databases()` directamente.
3. **Lectura del código** del gate, los schemas de salida y las rutas de escritura.

---

## Parte 1 — Qué funciona (verificado contra producción)

| Caso | Esperado | Obtenido |
|---|---|---|
| `initialize` con header `2025-11-25` | 200 y negocia la versión | ✅ 200, `2025-11-25` |
| `notifications/initialized` | 202 sin cuerpo | ✅ 202, 0 bytes |
| `tools/list` | 200 con las tools | ✅ 200, `list_databases` |
| `ping` | 200 | ✅ 200 |
| `tools/call` | 200, `isError: false` | ✅ 200 |
| Tool inexistente | 200 con `-32602` | ✅ |
| Método inexistente | 404 con `-32601` | ✅ |
| `GET` / `DELETE` al endpoint | 405 | ✅ los dos |
| `Origin` ajeno | 403 | ✅ |
| Token inválido / ausente | 401 | ✅ los dos |
| Batch JSON-RPC | 400 con `-32600` | ✅ |
| `Mcp-Session-Id` | se ignora y no se emite | ✅ |
| Cliente moderno (`2026-07-28`) sin `_meta` | sigue dando `-32020` | ✅ la validación no se debilitó |
| Versión desconocida | `-32022` con `data.supported` | ✅ las 5 versiones |

**Conclusión:** no hay nada que arreglar en el transporte. El bug de detección de era quedó
resuelto y el endurecimiento de seguridad sigue intacto.

**La superficie de tools es mínima a propósito:** existe `list_databases` y nada más. Un agente
no puede leer esquemas, ni consultar, ni ver migraciones. Ese es el alcance declarado de la v1,
no un defecto — pero conviene saberlo antes de esperar más del MCP.

---

## Parte 2 — Por qué `list_databases` viene vacío

El gate vive en `app/controllers/target_resolution.py::reachable_databases`. Es **una sola
consulta con todos los ejes en `AND`**, escrita así deliberadamente: como filtros sucesivos en
Python, cada uno sería un lugar donde alguien mete un `or`, y acá un `or` mal puesto entrega la
estructura de la base de un tercero a un agente.

### Las ocho condiciones, y el resultado de apagar cada una

Cada fila se verificó apagando **solo esa** condición sobre una cadena por lo demás completa:

| # | Condición | Dónde vive | Al apagarla |
|---|---|---|---|
| 0 | El token tiene la capacidad `blueprints:read` | `assert_agent_scope` | **403**, no lista vacía |
| 1 | El blueprint de la base está vinculado al proyecto del token | `JOIN ProjectDatabaseModel` | invisible |
| 2 | Ese blueprint pertenece a **exactamente un** proyecto | subquery `exclusivos` | invisible |
| 3 | La base **tiene** blueprint (`model_id` no nulo) | `JOIN DatabaseModel` | invisible |
| 4 | La base **tiene** entorno (`environment_id` no nulo) | `JOIN Environment` | invisible |
| 5 | `Environment.allows_agent_access = true` | filtro | invisible |
| 6 | `ManagedDatabase.agent_access_allowed = true` | filtro | invisible |
| 7 | `ManagedDatabase.agent_access_blocked = false` | filtro | invisible |

Además: si el proyecto supera `MCP_MAX_OBJECTS` (500) bases alcanzables, la llamada devuelve
**413** en vez de truncar — una lista cortada le haría creer al agente que no hay más.

### Los tres defaults que explican el arranque en cero

```
ManagedDatabase.agent_access_allowed   default=False   server_default="0"
ManagedDatabase.agent_access_blocked   default=False   server_default="0"
Environment.allows_agent_access        default=False   server_default="0"
```

Todo nace cerrado. **Eso es correcto y no hay que cambiarlo**: el opt-in por base es el eje que
decide el alcance, y si alcanzara con habilitar el entorno, encender uno abriría de golpe todas
sus bases — incluidas las que nadie revisó y las que se creen después.

### La condición que más probablemente te está pegando

Condición **3**: la base tiene que tener blueprint.

`ManagedDatabase.model_id` es **nullable**, y `AdoptDatabaseIn.model_id` está declarado
`int | None = Field(None, ..., "Blueprint a vincular (opcional)")`. O sea: **una base adoptada
sin blueprint es perfectamente legal en el inventario y permanentemente invisible para el
MCP**, sin ningún mensaje que lo diga.

Si el inventario creció adoptando bases que ya existían en los motores —que es el camino normal
cuando se incorpora un servidor con datos— hay una buena probabilidad de que varias tengan
`model_id` nulo. Y esa condición **no figura** en la checklist documentada.

---

## Parte 3 — Los tres defectos encontrados

### D1 · El flag que decide el acceso se puede escribir, pero no leer (bloqueante)

`PUT /api/v1/managed-databases/{id}/agent-access` existe y escribe las dos columnas, con
auditoría fail-closed al abrir. Pero su `response_model` es `ApiResponse[ManagedDatabaseOut]`, y
**`ManagedDatabaseOut` no incluye `agent_access_allowed` ni `agent_access_blocked`**. Tampoco
los incluye ningún `GET`.

Consecuencia concreta: un operador **no puede saber por la API** si una base está abierta a
agentes. Ni antes de escribir, ni después. El propio endpoint que acabás de llamar no te
devuelve lo que escribiste. Para auditar el estado hoy hay que entrar a la BD de metadatos a
mano — exactamente lo que `set_agent_access` fue creado para evitar.

Esto ya estaba anotado como bloqueante del frontend en `TODO.md`; acá queda confirmado desde el
código y elevado: **también bloquea el diagnóstico y la auditoría**, no solo la pantalla.

### D2 · La checklist documentada tiene cinco condiciones y el gate exige ocho

`docs/features/mcp-para-colaboradores.md` §A.5 lista cinco. Comparado con el gate real, **no
menciona que la base deba tener blueprint asignado** (condición 3). Su punto 2 dice "su
blueprint pertenece a un solo proyecto", que presupone que lo tiene, pero nunca lo exige.

Un operador que siga esa checklist al pie sobre una base adoptada sin blueprint la va a dar por
correcta y va a seguir sin ver nada, sin ninguna pista de qué le falta.

### D3 · El `note` del tool nombra una sola causa de ocho

`list_databases` devuelve siempre:

> "Vacío significa que ninguna base del proyecto tiene el opt-in de agentes todavía, no que haya
> fallado la consulta."

La primera mitad es valiosa —evita que el agente lo lea como fallo y reintente— pero la segunda
afirma **una** causa concreta (`agent_access_allowed`) cuando hay ocho posibles. Si el problema
real es un blueprint compartido entre dos proyectos, ese texto manda a mirar el lugar
equivocado.

---

## Parte 4 — Cómo repararlo (operación, sin tocar código)

Requiere sesión de administrador. `$BASE` es la URL del gateway, `$CSRF` el token CSRF de la
cookie, y `cookies.txt` la sesión.

### Paso 1 · Identificar el proyecto del token

El token ya trae proyecto: si no lo tuviera, la llamada habría dado 403 en vez de lista vacía.
Ese dato lo tiene quien emitió el token.

### Paso 2 · Verificar qué blueprints ve el proyecto

```bash
curl -s -b cookies.txt "$BASE/api/v1/projects/$PROJECT_ID/blueprints"
```

Si viene vacío, **nada del proyecto puede ser alcanzable**. Vinculá los blueprints:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/projects/$PROJECT_ID/blueprints" \
  -d '{"model_ids":[1,2]}'
```

### Paso 3 · Verificar la exclusividad de cada blueprint

```bash
curl -s -b cookies.txt "$BASE/api/v1/database-models/$MODEL_ID/projects"
```

Si devuelve **más de un proyecto**, ese blueprint queda excluido del gate por completo — no es
que se filtre, es que desaparece. Soltá el vínculo sobrante:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -X DELETE "$BASE/api/v1/projects/$OTRO_PROJECT_ID/blueprints/$MODEL_ID"
```

### Paso 4 · Verificar que cada base tenga blueprint y entorno

```bash
curl -s -b cookies.txt "$BASE/api/v1/managed-databases"
```

Buscá las que tengan `"model_id": null` o `"environment_id": null`. **Esas son invisibles.**
Asignales blueprint y entorno con el `PATCH` del módulo.

> Ojo con `model_version`: es derivada y no se escribe a mano. Si la base ya está en una versión
> concreta del blueprint, el camino es `stamp`, no un `PATCH`.

### Paso 5 · Abrir el entorno

```bash
curl -s -b cookies.txt "$BASE/api/v1/environments"        # mirá allows_agent_access
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PATCH "$BASE/api/v1/environments/$ENV_ID" \
  -d '{"allows_agent_access": true}'
```

### Paso 6 · Abrir cada base, una por una

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PUT "$BASE/api/v1/managed-databases/$DB_ID/agent-access" \
  -d '{"allowed": true, "blocked": false}'
```

Los dos campos son obligatorios a propósito: un parcial dejaría al operador creyendo que cerró
el acceso cuando solo tocó una palanca.

**Acá se cruza el D1:** la respuesta no te va a mostrar el estado resultante. La única
confirmación real hoy es volver a llamar `list_databases` desde el MCP y ver si la base apareció.

### Paso 7 · Confirmar

```bash
curl -s -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $GATEWAY_MCP_TOKEN" \
  -H 'Content-Type: application/json' -H 'MCP-Protocol-Version: 2025-11-25' \
  -H 'Accept: application/json, text/event-stream' -H 'Mcp-Name: list_databases' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_databases","arguments":{}}}'
```

### Atajo: diagnóstico directo sobre la BD de metadatos

Mientras D1 no esté resuelto, esta consulta dice **por qué** cada base no aparece. Es de solo
lectura. Reemplazá `:project_id`.

```sql
SELECT  md.id,
        md.name,
        CASE WHEN md.model_id       IS NULL THEN 'FALTA: blueprint'        END AS c3,
        CASE WHEN md.environment_id IS NULL THEN 'FALTA: entorno'          END AS c4,
        CASE WHEN e.allows_agent_access = 0 THEN 'FALTA: entorno cerrado'  END AS c5,
        CASE WHEN md.agent_access_allowed = 0 THEN 'FALTA: opt-in de base' END AS c6,
        CASE WHEN md.agent_access_blocked = 1 THEN 'FALTA: base vetada'    END AS c7,
        CASE WHEN pdm.project_id IS NULL THEN 'FALTA: blueprint no esta en el proyecto' END AS c1,
        CASE WHEN cnt.n > 1 THEN 'FALTA: blueprint compartido entre proyectos' END AS c2
FROM managed_databases md
LEFT JOIN environments e   ON e.id = md.environment_id
LEFT JOIN project_database_models pdm
       ON pdm.model_id = md.model_id AND pdm.project_id = :project_id
LEFT JOIN (SELECT model_id, COUNT(DISTINCT project_id) AS n
             FROM project_database_models GROUP BY model_id) cnt
       ON cnt.model_id = md.model_id
ORDER BY md.id;
```

Una fila con todas las columnas `c*` en `NULL` es una base que **sí** debería aparecer en el MCP.

Dos aclaraciones de lectura:

- **Una base sin blueprint marca `c3` y `c1` a la vez.** No son dos problemas: sin `model_id` el
  vínculo con el proyecto tampoco puede existir. Arreglá `c3` y `c1` se apaga solo.
- En MySQL/MariaDB y SQLite los booleanos se comparan con `0`/`1` como está escrito. En
  PostgreSQL, cambiá `= 0` por `IS NOT TRUE` y `= 1` por `IS TRUE`.

> **Esta consulta se verificó contra el gate real**, no solo se escribió: sobre un inventario de
> prueba con una base por cada causa de exclusión, marcó las siete por el motivo correcto y la
> única fila que dejó limpia fue exactamente la única que `reachable_databases()` devuelve.

---

## Parte 5 — Cambios de código propuestos

### C1 · Exponer el estado de acceso de agentes (resuelve D1) — prioridad alta

Agregar a `ManagedDatabaseOut`:

```python
agent_access_allowed: bool = False
agent_access_blocked: bool = False
```

y sumarlos a `ManagedDatabaseController._serialize`. Es un cambio **aditivo**: no rompe ningún
consumidor. Desbloquea de una vez el diagnóstico, la auditoría y la pantalla de agent-access que
el `TODO.md` tiene frenada.

> Para el frontend: `EnvironmentOut` ya publica `allows_agent_access`, así que el zod de
> `ManagedDatabaseOut` necesita los dos campos nuevos como `.nullish()` — con `safeParse`, una
> divergencia descarta la respuesta entera.

### C2 · Corregir la checklist documentada (resuelve D2) — prioridad alta, costo cero

En `docs/features/mcp-para-colaboradores.md` §A.5, agregar la condición faltante y renombrar la
sección, que hoy promete cinco:

> 1. la base **tiene un blueprint asignado** (`model_id` no nulo) — una base adoptada sin
>    blueprint nunca es alcanzable;

Y en la tabla de diagnóstico, la fila "La lista viene vacía" debería apuntar a la consulta SQL
de arriba en vez de a una relectura de la checklist.

### C3 · Hacer honesto el `note` del tool (resuelve D3) — prioridad media

En `app/mcp/tools/inventory.py`, cambiar el texto por uno que no comprometa una sola causa:

```python
"note": (
    "Vacío no es un fallo de la consulta: el acceso de agentes niega por default y "
    "exige varias condiciones simultáneas (proyecto, blueprint, entorno y opt-in por "
    "base). Quien administre el gateway puede verificarlas."
),
```

### C4 · Considerar un endpoint de diagnóstico — prioridad baja, decisión de producto

Un `GET /managed-databases/{id}/agent-access/diagnosis` que devuelva qué condición falla haría
innecesario el SQL a mano.

**Pero ojo con el diseño:** tendría que ser una ruta de **administrador**, nunca alcanzable por
un token de agente. El gate está construido sobre no enumerar lo negado, y un diagnóstico
expuesto al agente sería justamente el oráculo de inventario que el orden de los ejes existe
para evitar.

---

## Parte 6 — Qué NO se verificó

- **Los datos reales de producción.** La API REST del despliegue responde `401`: hace falta una
  sesión de administrador que no se pidió. Todo lo que dice este documento sobre *cuál*
  condición está fallando en tu instalación es **una hipótesis ordenada por probabilidad**, no
  un hecho medido. La consulta SQL de la Parte 4 lo convierte en hecho en un minuto.
- **La suite completa** (~690 tests). Se corrieron `tests.test_mcp_protocol` (33/33) y
  `tests.test_mcp_server` (29/29).
- **El comportamiento con más de 500 bases alcanzables** (el 413 de `MCP_MAX_OBJECTS`): está
  leído en el código, no ejercitado.
- **Los motores reales.** `list_databases` no abre ninguna conexión, así que nada de esto
  depende de que los motores estén levantados — pero tampoco los ejercita.
