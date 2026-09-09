# Contrato de autorización: capacidades, `/auth/me` y el 403

Addendum sobre **toda** la API v1. Antes, un endpoint solo exigía **sesión válida**: quien entraba
podía hacer todo. Ahora **cada endpoint declara una capacidad** de un vocabulario cerrado, y el
servidor la exige.

**No hay cambios incompatibles para el administrador sembrado**: nace con rol `owner` y las dos
capacidades globales, o sea las 29 capacidades. Todo lo que la SPA hace hoy sigue funcionando
igual. Lo que cambia es que ahora **existe** una respuesta 403 donde antes no podía haberla, y
que `/auth/me` publica con qué decidir la UI.

---

## 1. `GET /api/v1/auth/me` — aditivo

Sigue devolviendo `id` y `username`; se agregan campos. **Ojo con el `safeParse` del envelope
completo que hace la SPA de este repo**: una divergencia de un campo descarta la respuesta
entera, así que cada campo nuevo va `.nullish()`, nunca `.optional()`.

```jsonc
{
  "id": 1,
  "username": "admin",
  "role": "owner",                    // rol efectivo = máximo sobre los alcances
  "capabilities": ["databases.read", "..."],   // las EFECTIVAS
  "global_capabilities": ["access_admin", "security_officer"],
  "scope_roles": [{ "scope_type": "environment", "scope_id": 3, "role": "operator" }],
  "step_up_capabilities": ["databases.drop", "..."],
  "catalog_version": "9f2c1a…"        // sha256 corto, para invalidar caché del cliente
}
```

- **`capabilities` no es una lista paralela**: se deriva del MISMO predicado que hace cumplir el
  servidor. Es la única fuente para habilitar o deshabilitar controles.
- **Es una pista de UI. Decide el servidor, siempre.** Ocultar un botón no es autorización.
- **`step_up_capabilities` se publica y todavía NO se exige.** Está para que la UI pueda pedir la
  contraseña *antes* de mandar la operación en vez de descubrirlo por un error; el mecanismo de
  reautenticación es una fase posterior. **No construyas un flujo que dependa de que el servidor
  rechace por falta de step-up: hoy no lo hace.**

## 2. `GET /api/v1/authz/catalog` — el vocabulario completo

Detrás de `self.read`, o sea cualquier sesión. Devuelve una fila por capacidad con los metadatos
que la UI necesita para una pantalla de administración de accesos:

| Campo | Para qué sirve |
|---|---|
| `id`, `module`, `level` | `modulo.nivel`, p. ej. `databases.drop` |
| `label` | etiqueta en español, lista para mostrar |
| `mutates` | si cambia estado |
| `discloses` | **si expone datos o credenciales** — es un eje INDEPENDIENTE de `mutates` |
| `requires_step_up` | si va a pedir reautenticación (ver la advertencia de arriba) |
| `agent_allowed` | techo de lo que puede vivir en un token del servidor MCP |
| `scope_axis` | `global` \| `environment` \| `server` |
| `roles`, `global_capabilities` | quién la tiene |

`catalog_version` de `/auth/me` cambia cuando cambia el catálogo: úsalo como clave de caché.

**Los dos ejes son independientes y eso importa para la UI.** `exports.download`,
`engine_users.secrets` y `blueprints.captures` **no destruyen nada** pero divulgan: una pantalla
que agrupe por "peligrosidad" mirando solo `mutates` las va a pintar como inofensivas.

## 3. El 403

```jsonc
{ "detail": { "msg": "No tienes permiso para esta operación.",
              "type": "AppHttpException",
              "public_context": { "code": "access.forbidden" } } }
```

**El código es cerrado y NO nombra la capacidad que falta**, a propósito: un mensaje como "falta
`servers.admin`" le da a un atacante un mapa de la superficie por fuerza bruta de 403. No intentes
parsear qué faltó — usá `capabilities` de `/auth/me` para no llegar hasta acá.

## 4. Rutas donde un PARÁMETRO sube el requisito

Cinco endpoints piden **más** capacidad según el payload. Son los que la UI tiene que reflejar
deshabilitando el control, porque el usuario ya está en la pantalla y el 403 llega recién al
enviar:

| Endpoint | Con este parámetro | Pide además |
|---|---|---|
| `POST /database-models/from-snapshot` | `data_tables` (datos-semilla) | `blueprints.captures` |
| `POST /database-models/{id}/migrations` | `capture_selects: true` | `blueprints.captures` |
| `PATCH /database-models/{id}/migrations/{v}` | `capture_selects: true` | `blueprints.captures` |
| `DELETE /managed-databases/{id}` | `drop_remote=true` | `databases.drop` |
| `DELETE /server-users/{id}` | `drop_remote=true` | `engine_users.drop` |

Y dos que exigen dos capacidades **siempre**, porque crean una versión de blueprint desde otro
módulo: `POST /schema-comparisons/{id}/adopt` y
`POST /database-models/{id}/collation-conversions/{batch}/blueprint-version` piden
`blueprints.write` además de la propia.

En los tres primeros, **apagar** la captura no pide nada extra: solo encenderla.

## 5. Módulos, niveles y quién los tiene

| Módulo | Niveles | `viewer` | `operator` | `owner` | Solo global |
|---|---|:--:|:--:|:--:|---|
| `self` | `read` | ✅ | ✅ | ✅ | |
| `servers` | `read` | ✅ | ✅ | ✅ | |
| | `admin` | | | | `security_officer` |
| `engine_users` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | ✅ | ✅ | |
| | `drop` | | | ✅ | |
| | `secrets` 🔓 | | | ✅ | |
| `databases` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | ✅ | ✅ | |
| | `drop` | | | ✅ | |
| `blueprints` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | ✅ | ✅ | |
| | `apply` | | | ✅ | |
| | `captures` 🔓 | | | ✅ | |
| `schema_diff` | `read` | ✅ | ✅ | ✅ | |
| | `execute` | | | ✅ | |
| `clones` | `read` | ✅ | ✅ | ✅ | |
| | `execute` 🔓 | | | ✅ | |
| `collation` | `read` | ✅ | ✅ | ✅ | |
| | `execute` | | ✅ | ✅ | |
| `exports` | `read` | ✅ | ✅ | ✅ | |
| | `execute` | | ✅ | ✅ | |
| | `download` 🔓 | | | ✅ | |
| `sql_console` | `history` | ✅ | ✅ | ✅ | |
| | `execute` 🔓 | | | ✅ | |
| `catalogs` | `read` | ✅ | ✅ | ✅ | |
| | `write` | | | | `security_officer` |
| `environments` | `read` | ✅ | ✅ | ✅ | |
| `gateway` | `admin` | | | | `access_admin`, `security_officer` |

🔓 = **divulga** (`discloses: true`).

**Tres asignaciones que sorprenden y son deliberadas:**

- **`servers.admin` no lo tiene `owner`.** Editar un servidor puede **re-apuntar un `server_id` a
  otro host**, y con eso se redirige cada operación futura de todo operador. Es política de
  infraestructura, no operación.
- **`catalogs.write` y los mutantes de `/environments` tampoco.** `privileges.is_active` decide
  qué se puede otorgar y `blocks_destructive_migrations` decide si una migración destructiva
  corre: **toda fila que un guard lee es una frontera de privilegio**, así que su escritor
  necesita al menos el privilegio del guard que puede apagar.
- **`clones.execute` está en `owner` y no en `operator`** aunque su nombre lo emparente con
  `collation.execute`: un clon **copia DATOS**, y meter la base de producción de un cliente en un
  entorno de desarrollo es divulgación.

## 6. Lo que este addendum NO trae todavía

- **Alcance por destino.** `scope_roles` se publica, pero el chequeo por entorno o por servidor de
  cada objeto concreto es una fase posterior: hoy la capacidad se evalúa global. **No presentes en
  la UI un alcance que el servidor todavía no aplica.**
- **Step-up.** Ver §1.
- **Administración de usuarios.** `/gateway-users` y `/api-tokens` no existen aún; el único usuario
  es el administrador sembrado. Su rol se cambia hoy en la BD.
