# API Reference v21 — Permisos de un usuario: consultar y otorgar

Addendum al contrato consolidado (`api-reference.md`) para el módulo de grants. Cubre el
ciclo completo sobre los permisos de **un usuario**: leerlos y asignarlos, sueltos o por
perfil, sobre una base o sobre muchas.

Dos partes:

- **§1–§5 — Consultar.** Endpoint **nuevo**: permisos por identidad, sin adoptar al usuario.
- **§6–§11 — Otorgar.** Endpoints que **ya existían y no estaban documentados acá**. No hay
  nada nuevo de backend en esta mitad: es contrato ya en producción que el frontend todavía
  no consume. §12 lista lo que **no** existe, para que no se planifique sobre humo.

---

# Parte A — Consultar permisos

## 1. El endpoint

```
GET /api/v1/servers/{server_id}/users/grants?username=<u>&host=<h>&database=<db>
```

| Query param | Obligatorio | Nota |
|---|---|---|
| `username` | Sí | Identidad en el motor, no un id de inventario. |
| `host` | No (default `%`) | **Ignorado en PostgreSQL** (no tiene hosts). |
| `database` | **Sí en PostgreSQL** | **Ignorado en MySQL/MariaDB** — ver §3. |

Respuesta (`data`):

```json
{
  "username": "app_user",
  "host": "10.0.%",
  "status": "unmanaged",
  "server_user_id": null,
  "grants": [
    { "level": "database", "object": "shop", "privileges": ["INSERT", "SELECT"], "with_grant_option": false },
    { "level": "table", "object": "shop.orders", "privileges": ["UPDATE"], "with_grant_option": false }
  ]
}
```

`status` es `adopted` | `unmanaged`, y `server_user_id` viene poblado solo en el primero.
**Ninguno de los dos sale del motor**: son el cruce contra el inventario del gateway, para que
la UI sepa si puede ofrecer las acciones que sí requieren fila (`/server-users/{id}/…`) o si
antes hay que adoptar.

`host` vuelve `null` en PostgreSQL, se haya mandado o no.

---

## 2. Cuál de los tres endpoints de consulta usar

| Pregunta | Endpoint | ¿Exige adopción? | Granularidad |
|---|---|---|---|
| ¿Qué permisos tiene **este usuario**? | `GET /servers/{id}/users/grants` *(nuevo)* | **No** | Un grant **por objeto** |
| ¿Qué permisos tiene **este usuario**? | `GET /server-users/{id}/grants` | **Sí** (404 del inventario) | Un grant **por objeto** |
| ¿**Quiénes** tienen permisos sobre **esta BD**? | `GET /servers/{id}/databases/{db}/users` | No | **Agregada** por usuario |

El nuevo y `GET /server-users/{id}/grants` devuelven el mismo detalle; **el nuevo no lo
reemplaza**: el que va por `user_id` sigue siendo el camino natural cuando ya tenés la fila.

La consulta inversa por BD **agrega todos los niveles en un solo `privileges[]`** y solo dice
en qué `levels` apareció el usuario. Con `levels: ["database","table"]` y
`privileges: ["SELECT","INSERT"]` **no podés saber** si el `INSERT` es sobre toda la BD o
sobre una tabla suelta. Para auditar de verdad, entrá por el endpoint nuevo.

---

## 3. La trampa de `database` (divergencia por motor)

Hereda el mismo contrato de `GET /server-users/{id}/grants`, y **no es simétrico**:

- **PostgreSQL**: `database` es **obligatorio**. Sin él, 422 — los grants de objeto viven
  dentro de una BD y hay que conectarse a ella para leerlos. La respuesta trae solo esa BD.
- **MySQL/MariaDB**: `database` se **ignora**. La respuesta trae los grants del usuario en
  **todo el servidor**, de todos los niveles. Si querés una sola BD, **filtrá vos** por el
  campo `object` de cada grant (`"shop"`, `"shop.orders"`, `"shop.orders(email)"`).

Mandar `database` en MySQL y asumir que acotó es el error fácil acá.

---

## 4. Errores de la consulta

| Código | Cuándo |
|---|---|
| 404 | El `server_id` no existe. |
| 404 | La identidad **no existe en el motor** (`context: {username, host}`). |
| 422 | `username` o `host` con caracteres no permitidos. |
| 422 | PostgreSQL sin `database`. |

**El 404 de identidad es deliberado y cuesta un `list_users()` extra.** Sin él, un typo en
`username` devolvería `grants: []` — indistinguible de *"existe y no tiene ningún
privilegio"*, que es justo la conclusión peligrosa en una auditoría de permisos.

Ese 404 **no** significa que el usuario no esté adoptado; para eso está `status`.

---

## 5. La consulta no muta nada

Read-only puro: no ejecuta DCL, no toca el inventario y **no genera evento de auditoría** —
igual que los demás listados de grants.

---

# Parte B — Otorgar permisos

> Todo lo de esta parte **ya está en producción**. Se documenta acá porque no tenía addendum
> propio y el frontend no lo consume todavía.

## 6. Vocabulario común: `level` y `object_ref`

Los tres caminos de otorgamiento comparten estas dos piezas.

**`level`** (`GrantLevel`): `global` · `database` · `schema` · `table` · `column` ·
`sequence` · `routine`. `schema` y `sequence` son **solo PostgreSQL**.

**`object_ref`**: qué campos importan depende del nivel.

| Nivel | Campos que se usan |
|---|---|
| `database` | `database` |
| `schema` *(PG)* | `database` + `schema` |
| `table` | `database` [+ `schema`] + `table` |
| `column` | `database` [+ `schema`] + `table` + `columns[]` |
| `sequence` *(PG)* | `database` + `schema` + `sequence` |
| `routine` | `database` [+ `schema`] + `routine` |

`schema` solo aplica a PostgreSQL (default `public`). En el JSON el campo se llama `schema`.

**Privilegios sensibles (GATE).** Algunos tokens (`ALL PRIVILEGES`, `GRANT OPTION`, …) están
clasificados como GATE: se pueden otorgar, pero la operación **audita la intención antes de
ejecutar** (fail-closed). `with_grant_option: true` cuenta como GATE aunque el privilegio no
lo sea. La UI debería pedir confirmación explícita en esos casos — ver `requires_confirmation`
en §8.

---

## 7. Otorgar un permiso suelto

```
POST /api/v1/server-users/{user_id}/grants
```

```json
{
  "level": "table",
  "object_ref": { "database": "shop", "table": "orders" },
  "privileges": ["SELECT", "INSERT"],
  "with_grant_option": false
}
```

Respuesta:

```json
{ "granted": true, "level": "table", "privileges": ["SELECT", "INSERT"], "with_grant_option": false }
```

**Un solo `object_ref` por llamada.** Para N objetos son N llamadas — ver §12.

| Código | Cuándo |
|---|---|
| 403 | La credencial pseudo-root del gateway **no puede delegar** esos privilegios (falta `WITH GRANT OPTION`). Se chequea **antes** de tocar el motor. |
| 404 | `user_id` no está en el inventario. |
| 422 | Privilegio inválido para ese motor/nivel. |

El `REVOKE` simétrico es `DELETE /server-users/{user_id}/grants` (mismo body + `cascade`, que
es solo PostgreSQL y exige el query `confirm_grantee`).

---

## 8. Perfiles de permisos: la plantilla

```
GET    /api/v1/permission-profiles?engine=<e>&active=<bool>
POST   /api/v1/permission-profiles
GET    /api/v1/permission-profiles/{profile_id}
PATCH  /api/v1/permission-profiles/{profile_id}
DELETE /api/v1/permission-profiles/{profile_id}
```

Crear:

```json
{
  "name": "solo-lectura",
  "engine": "mysql",
  "description": "Lectura sobre la BD y sus tablas",
  "is_active": true,
  "items": [
    { "level": "database", "privileges": ["SELECT"] },
    { "level": "table", "privileges": ["SELECT"] }
  ]
}
```

Cada item de la respuesta trae `requires_confirmation: bool` — **usalo para decidir si la UI
pide confirmación antes de aplicar el perfil.**

**Tres cosas que hay que entender antes de construir la UI:**

1. **La plantilla NO dice sobre qué objeto.** Un item es *"a nivel `table`, estos
   privilegios"* — pero no qué tabla. El objeto se manda **al aplicar** (§9).
2. **Un item por nivel.** Hay `UNIQUE (profile_id, level)`: no podés tener dos items `table`
   en el mismo perfil.
3. **`PATCH` con `items` REEMPLAZA la lista completa**, no hace merge. `engine` es
   **inmutable** (cambiarlo invalidaría los items).

**Aplicar un perfil es un snapshot, no una suscripción.** No crea relación viva
usuario↔perfil: si después editás el perfil, los usuarios ya asignados **no se
re-sincronizan**. Es un atajo para otorgar, no una política que se mantiene sola. La UI no
debería sugerir lo contrario.

---

## 9. Aplicar un perfil a UNA base

```
POST /api/v1/server-users/{user_id}/apply-profile/{profile_id}
```

```json
{
  "object_mappings": [
    { "level": "database", "object_ref": { "database": "shop" } },
    { "level": "table",    "object_ref": { "database": "shop", "table": "orders" } }
  ]
}
```

Respuesta: `{ profile_id, profile_name, engine, grants_applied, skipped_levels[], errors[] }`

**Los niveles del perfil que no mapees se omiten** y vuelven en `skipped_levels`. Si el perfil
tiene un item `column` y no le das objeto, ese item simplemente no se aplica.

| Código | Cuándo |
|---|---|
| 404 | `user_id` o `profile_id` inexistente. |
| 409 | El perfil está **desactivado** (`is_active: false`). |
| 422 | **Motor incompatible** — ver §10. |
| 422 | **No se aplicó ningún permiso** (`grants_applied == 0`). |

Ese último 422 es deliberado: aplicar cero permisos y devolver 200 dejaba el motivo enterrado
en `skipped_levels`/`errors` y la UI lo mostraba como éxito. El `context` trae
`skipped_levels` y `errors` para explicar por qué no se aplicó nada.

**Best-effort por item.** Un nivel que falla se reporta en `errors` y **no aborta los demás**.
El perfil puede quedar aplicado **parcialmente**, y **nunca hay rollback** de los grants ya
otorgados. Mostrá `errors` siempre, incluso con `grants_applied > 0`.

---

## 10. Compatibilidad de motor (no es un match de nombre)

Si `profile.engine` ≠ motor del servidor, **no se rechaza de una**:

1. Si son de la **misma familia** (mysql ↔ mariadb), se valida **privilegio por privilegio**
   contra el catálogo del motor real.
2. Si todos los tokens son válidos ahí, **se aplica**.
3. Si alguno no lo es, o las familias son distintas (mysql ↔ postgresql) → **422**, con
   `context.incompatible_items` listando exactamente cuáles.

O sea: un perfil MySQL **sí** puede aplicarse a un servidor MariaDB, salvo que use algún
privilegio que MariaDB no tenga. La UI no debería filtrar los perfiles solo por igualdad de
`engine`: perdería casos válidos.

---

## 11. Aplicar un perfil a N bases — el bulk

```
POST /api/v1/server-users/{user_id}/apply-profile/{profile_id}/bulk
```

**Este es el endpoint que el frontend no está usando.** Mismo usuario, mismo perfil, N bases,
una sola llamada.

```json
{
  "databases": ["shop_a", "shop_b", "shop_c"],
  "object_mappings": [
    { "level": "database", "object_ref": {} },
    { "level": "table",    "object_ref": { "table": "orders" } }
  ]
}
```

**`object_mappings` es una PLANTILLA, no una lista de destinos.** El campo `database` de cada
`object_ref` **se ignora y se sobreescribe** con la BD de la iteración. El resto
(`schema`/`table`/`columns`/`sequence`/`routine`) se reusa tal cual. Por eso el ejemplo manda
`{}` en el nivel `database`: sería redundante.

Esto asume **el mismo esquema relativo en cada base** — el caso multi-tenant de una BD por
cliente. Si `orders` no existe en `shop_c`, ese ítem falla solo para `shop_c`.

`databases` acepta **1 a 100** nombres. Son **nombres del motor, no ids de inventario**: sirve
para bases adoptadas y no adoptadas por igual.

Respuesta:

```json
{
  "profile_id": 3, "profile_name": "solo-lectura", "engine": "mysql",
  "total_databases": 3,
  "results": [
    { "database": "shop_a", "grants_applied": 2, "skipped_levels": [], "errors": [], "ok": true },
    { "database": "shop_c", "grants_applied": 1, "skipped_levels": [], "errors": ["table: ..."], "ok": false }
  ]
}
```

**Siempre 200, incluso si TODAS las bases fallaron.** Este es el punto que más importa para la
UI: acá **no** rige el 422 de §9. El estado real está en `results[].ok`, `grants_applied` y
`errors`. Una pantalla que solo mire el status HTTP va a reportar éxito sobre un lote entero
fallido.

Ojo con el matiz: las validaciones **previas** al lote sí cortan con error — 404 (usuario o
perfil inexistente), 409 (perfil desactivado) y 422 (motor incompatible, §10) se evalúan una
sola vez, antes de tocar ninguna base. Lo que nunca produce un status de error es el
**resultado** del lote.

Best-effort **a dos niveles**: un nivel que falla no aborta esa BD, y una BD que falla no
aborta el lote.

**Rate limit: `5/minute`.** No es arbitrario. Con `NullPool`, cada `can_grant` + `grant_object`
abre **su propia conexión remota**: una llamada de 100 bases × M niveles puede abrir cientos
de conexiones y retener un worker del threadpool decenas de segundos. **Partí en tandas de
~20 bases** en lugar de agotar la cota de 100 — la latencia crece con
`len(databases) × niveles`.

**Nunca revoca: solo agrega privilegios.** Por eso un error acá es recuperable con un
`REVOKE`, y por eso el límite es más permisivo que el de las operaciones destructivas de
fan-out (clone/execute, `3/minute`).

---

## 12. Lo que NO existe (no planificar sobre esto)

| Caso | Estado |
|---|---|
| Permiso suelto → **N bases** en una llamada | ❌ No existe. Son N llamadas a §7. |
| **Perfil + permisos sueltos** combinados en una llamada | ❌ No existe. Son dos llamadas y dos resultados que la UI tiene que coser. |
| Otorgar / aplicar perfil **por identidad** (usuario no adoptado) | ❌ No existe. Todo el otorgamiento cuelga de `user_id`: hay que adoptar primero. |
| `POST /server-users/provision` con `profile_id` | ❌ Acepta `initial_grants` sueltos, no un perfil. |

La **consulta** (Parte A) sí funciona sin adopción. El **otorgamiento** (Parte B) no. Esa
asimetría es el límite actual del módulo.
