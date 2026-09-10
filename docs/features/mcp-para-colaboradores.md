# Enlazar el MCP del gateway en la máquina de cada persona

Guía operativa. Dos partes: lo que hace **quien administra** (una vez, y después una vez por
persona) y lo que hace **cada colaborador** en su propia máquina.

> **Antes de empezar, lo que hoy sirve y lo que no.** El servidor expone **una sola tool**,
> `list_databases`, que devuelve el inventario alcanzable: nombre, motor, entorno, blueprint y
> versión aplicada de cada base. **Todavía no devuelve el esquema** — `list_objects`, `get_schema`
> y `check_freshness` no están implementadas porque son consultas al catálogo de cada motor y no
> se pudieron verificar sin Docker. Si el objetivo es que la IA vea las tablas, esto **no lo
> resuelve todavía**; lo que resuelve es que sepa qué bases existen y en qué versión están.

---

## Parte A — Quien administra: preparar el gateway (una sola vez)

Hace falta una sesión con **`gateway.admin`**, que solo tienen las capacidades globales
`access_admin` y `security_officer` — el rol `owner` no la tiene.

### A.1 Encender el servidor

Nace **apagado**. En el `.env` del gateway:

```bash
MCP_ENABLED=true
```

Y reiniciar. Sin esto, todo request al MCP recibe `503 mcp.disabled`.

Variables opcionales, con sus defaults:

| Variable | Default | Qué hace |
|---|---|---|
| `MCP_TOKEN_MAX_TTL_DAYS` | `90` | Tope de vida de un token. No hay tokens perpetuos |
| `MCP_MAX_OBJECTS` | `500` | Tope de bases por respuesta, evaluado **antes** de consultar |
| `MCP_MAX_BODY_KIB` | `256` | Tope del cuerpo de un mensaje |
| `MCP_RATE_LIMIT` | `120/minute` | Límite de tasa **por token** (no por IP) |

### A.2 Conseguir una sesión para las llamadas siguientes

Los endpoints de administración van con cookie de sesión **y** token CSRF. Con `curl`:

```bash
BASE=https://TU-HOST

# login: guarda las cookies en un archivo
curl -s -c cookies.txt -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"TU-PASSWORD"}'

# el token CSRF sale de la cookie que el servidor acaba de setear
CSRF=$(awk '/gw_csrf/{print $7}' cookies.txt)
```

De acá en adelante, toda escritura lleva `-b cookies.txt -H "X-CSRF-Token: $CSRF"`.

> **Por qué el header.** Todo `POST`/`PATCH`/`PUT`/`DELETE` de la API lo exige. Y **el token rota
> con la sesión**: si volvés a hacer login, hay que releer la cookie.

### A.3 Agrupar los blueprints en un proyecto

**El alcance de un token es un proyecto.** Un token no puede ser global: sin proyecto no alcanza
ninguna base.

```bash
# crear el proyecto y vincularle blueprints de una vez
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/projects" \
  -d '{"name":"Omnicanal","model_ids":[3,7]}'

# o vincular más después
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/projects/1/blueprints" -d '{"model_ids":[9]}'
```

> **Cuidado con los blueprints compartidos.** Si un blueprint pertenece a **más de un** proyecto,
> sus bases quedan fuera del alcance de **todos** los agentes. Es deliberado y es fail-closed: el
> modelo no tiene forma de expresar *para qué proyecto* se abrió una base, así que ante la duda no
> se entrega. Si necesitás que un agente vea esas bases, el blueprint tiene que estar en un solo
> proyecto.

### A.4 Abrir el acceso: son DOS niveles, y los dos niegan por default

**Nada es visible hasta que se abre explícitamente.** Y hay que abrir en los dos niveles:

```bash
# 1) el ENTORNO. Encenderlo exige repetir el slug: habilita una superficie de lectura
#    nueva sobre bases de terceros, así que cuenta como debilitamiento de la política.
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PATCH "$BASE/api/v1/environments/2?confirm_slug=development" \
  -d '{"allows_agent_access":true}'

# 2) cada BASE, una por una
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PUT "$BASE/api/v1/managed-databases/7/agent-access" \
  -d '{"allowed":true,"blocked":false}'
```

> **Por qué base por base y no solo el entorno.** Con solo el flag del entorno, encenderlo
> abriría de golpe **todas** sus bases — incluidas las que nadie revisó y **las que se creen
> después**. El opt-in por base es el eje que decide el alcance; el flag del entorno es la
> condición previa.

`blocked` es el **veto de emergencia**: gana sobre `allowed` y **no tiene override** — ni `force`,
ni nada. Es la palanca para cortar el acceso a una base sin tocar nada más.

Abrir una base se audita **fail-closed**: si el rastro no se puede persistir, la apertura no
ocurre.

### A.5 Las cinco condiciones que tienen que cumplirse

Para que una base aparezca en `list_databases`, **todas**:

1. pertenece al proyecto del token;
2. su blueprint pertenece a **un solo** proyecto;
3. tiene entorno asignado (una base sin clasificar nunca es alcanzable);
4. su entorno tiene `allows_agent_access = true`;
5. la base tiene `agent_access_allowed = true` **y** `agent_access_blocked = false`.

Si falta cualquiera, la base **no aparece** — y el listado **no dice que existe**. Eso es a
propósito: enumerar lo negado sería decirle al agente qué hay del otro lado.

---

## Parte B — Quien administra: dar de alta a una persona

### B.1 Emitir un token, uno por máquina

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/api-tokens" \
  -d '{"name":"laptop-de-ana","project_id":1,"expires_in_days":30}'
```

Respuesta (recortada):

```jsonc
{ "data": {
    "id": 4,
    "token_id": "J5uBh8FFa52o3b7xKq2w",
    "token": "dbgw.J5uBh8FFa52o3b7xKq2w.el-secreto-de-256-bits",  // ← UNA sola vez
    "name": "laptop-de-ana",
    "project_id": 1,
    "scopes": ["blueprints.read"],
    "expires_at": "2026-10-09T12:00:00"
} }
```

Tres reglas que no son burocracia:

- **El `token` se muestra una sola vez.** Lo que se guarda es su HMAC, así que no hay forma de
  volver a mostrarlo. Si se pierde, se emite otro.
- **Uno por máquina o repo**, y el `name` lo dice. Un token compartido entre seis máquinas es un
  token que **nadie revoca**, porque romperlo rompe a los seis.
- **Máximo 90 días.** Un token de agente vive en el `.mcp.json` del repo de otra gente: es la
  credencial con más probabilidad de terminar commiteada.

### B.2 Entregarlo

Por un canal privado (gestor de contraseñas, mensaje directo). **No** por el chat del equipo ni
por correo a una lista.

El `token_id` —la primera parte, después de `dbgw.`— **no es secreto** y es lo que aparece en la
auditoría: sirve para hablar de "el token de Ana" sin exponer nada.

### B.3 Revocar

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -X DELETE "$BASE/api/v1/api-tokens/4"
```

**No se deshace.** Un token que alguien creyó muerto y no lo está es peor que emitir uno nuevo. Si
la persona sigue en el equipo, se le emite otro.

Para ver qué tokens hay, con su último uso:

```bash
curl -s -b cookies.txt "$BASE/api/v1/api-tokens?size=50"
```

---

## Parte C — Cada colaborador, en su máquina

Tres pasos. Necesita: el **host del gateway** y su **token**.

### C.1 Guardar el token en el entorno, nunca en un archivo del repo

**bash / zsh** (`~/.bashrc`, `~/.zshrc`):

```bash
export GATEWAY_MCP_TOKEN='dbgw.J5uBh8FFa52o3b7xKq2w.el-secreto'
```

**PowerShell** (perfil, `$PROFILE`):

```powershell
$env:GATEWAY_MCP_TOKEN = 'dbgw.J5uBh8FFa52o3b7xKq2w.el-secreto'
```

Después, abrir una terminal nueva (o `source ~/.zshrc`).

### C.2 Registrar el servidor

Hay dos formas, y **la elección importa**:

**Opción recomendada — `.mcp.json` del repo, con la variable de entorno.** El repo declara el
servidor una vez, y cada persona pone su propio token en su entorno. Claude Code expande `${VAR}`
en `headers` y en `url`.

```jsonc
// .mcp.json — SE COMMITEA. Por eso va la variable y nunca el literal.
{
  "mcpServers": {
    "gateway": {
      "type": "http",
      "url": "${GATEWAY_URL:-https://gateway.interno}/mcp",
      "headers": { "Authorization": "Bearer ${GATEWAY_MCP_TOKEN}" }
    }
  }
}
```

La primera vez que se abra Claude Code en ese repo, pide **aprobar** el servidor del proyecto. Es
esperado: un `.mcp.json` viene del repositorio y el cliente no lo confía solo.

**Opción alternativa — solo en su máquina**, sin tocar el repo:

```bash
claude mcp add --transport http --scope user gateway https://gateway.interno/mcp \
  --header "Authorization: Bearer $GATEWAY_MCP_TOKEN"
```

`--scope user` lo deja disponible en **todos** sus proyectos y **no** se commitea. Ojo: acá el
token queda **literal** en su configuración local, no expandido.

> **Lo que nunca hay que hacer**: poner el token literal en `.mcp.json`. Ese archivo se commitea,
> y el gate de secretos del CI protege el repo del gateway, **no** el de quien consume. Si el
> repo de tu equipo no tiene una regla de escaneo de secretos, conviene agregarla.

### C.3 Comprobar que quedó enlazado

```bash
claude mcp list
```

Buscá `gateway` con `✔ Connected`. Dentro de una sesión de Claude Code, `/mcp` muestra el detalle.

Y para verlo funcionando, pedile en lenguaje natural:

> «Listá las bases de datos que ves por el MCP del gateway»

**Si devuelve una lista vacía, no falló.** Significa que ninguna base tiene el opt-in todavía — el
propio resultado lo dice en su campo `note`. Es el estado normal el primer día.

---

## Si algo no conecta

| Qué ves | Qué pasó | Qué hacer |
|---|---|---|
| `503 mcp.disabled` | El servidor está apagado | `MCP_ENABLED=true` y reiniciar (A.1) |
| `401 mcp.token_invalid` | El token no existe, venció o está revocado | Es **un solo código para los tres**, a propósito. Quien administra lo distingue: en `audit_log`, `action='mcp.auth'` trae el motivo real en `detail` |
| `403` | Mandaste un `Origin` que no está en `CORS_ORIGINS` | Solo pasa desde un navegador. Un cliente MCP no manda `Origin` |
| `400` con `-32020` | Los headers no coinciden con el cuerpo | Casi siempre es un cliente viejo. Actualizá Claude Code |
| `400` con `-32022` | El cliente pide una versión de protocolo que el servidor no habla | La respuesta trae `data.supported` con las que sí |
| `413` | El cuerpo supera `MCP_MAX_BODY_KIB` | No debería pasar con un cliente normal |
| `Missing environment variable: GATEWAY_MCP_TOKEN` | La variable no está en el entorno de esa terminal | Terminal nueva, o `source` del perfil (C.1) |
| `⏸ Pending approval` en `claude mcp list` | El `.mcp.json` del repo no fue aprobado | Abrir Claude Code en ese repo y aceptar |
| La lista viene vacía | Ninguna base cumple las cinco condiciones | Revisar A.4 y A.5. **No es un error** |

Del lado del gateway, todo intento de autenticación deja una fila:

```sql
SELECT created_at, status, detail
FROM audit_log
WHERE action = 'mcp.auth'
ORDER BY id DESC LIMIT 20;
```

El `detail` de un rechazo dice el motivo (`rechazo=inexistente|hmac|revocado|expirado`) y el
`token_id`. **Nunca el secreto.**

---

## Cuando alguien se va del equipo

1. **Revocar sus tokens** (B.3). El acceso corta en el request siguiente, sin caché.
2. Verificar con `GET /api/v1/api-tokens` que no queda ninguno vivo a su nombre.
3. Su usuario del gateway se **desactiva**, nunca se borra: el username no se reusa jamás, porque
   `audit_log` lo desnormaliza y una persona nueva heredaría la apariencia de las filas viejas.

---

## Lo que este MCP nunca va a hacer

- **No acepta SQL del agente**, en ninguna versión futura. `sqlglot` no tokeniza los comentarios
  ejecutables `/*!` de MySQL ni `/*M!` de MariaDB, así que todo guard por AST sobre SQL arbitrario
  es evadible — fue una vulnerabilidad real de la consola SQL de este repo. Cuando haga falta ver
  datos, la vía son tools **parametrizados**.
- **No escribe nada.** El techo de capacidades de un token excluye todo lo que mute o divulgue, y
  la intersección se aplica dos veces: al emitir y al autenticar.
- **No usa la credencial pseudo-root.** Cuando lleguen las tools que leen el catálogo, van con una
  credencial de solo lectura propia del servidor.

El contrato técnico completo está en `docs/api-reference-v23.md` §9.
