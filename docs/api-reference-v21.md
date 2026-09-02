# API Reference v21 — Permisos de un usuario por identidad (adoptado o no)

Addendum al contrato consolidado (`api-reference.md`) para el módulo de grants. Agrega **un
endpoint nuevo** de solo lectura; no cambia ninguno existente.

Ataca un límite duro: el detalle fino de permisos de un usuario **exigía adoptarlo**. El
gateway administra servidores de terceros donde la mayoría de las cuentas nunca pasaron por
el inventario, y adoptar es una **escritura** — un precio absurdo para responder una pregunta
de solo lectura.

---

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

## 2. Cuál de los tres endpoints de permisos usar

Ya había dos consultas de permisos. Ahora son tres, y **no son intercambiables**:

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
sobre una tabla suelta. Para auditar de verdad, entrá por este endpoint nuevo.

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

## 4. Errores

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

## 5. Nada más cambia

Read-only puro: no ejecuta DCL, no toca el inventario y **no genera evento de auditoría** —
igual que los demás listados de grants. `GET /server-users/{id}/grants`,
`GET /servers/{id}/databases/{db}/users` y el resto del módulo conservan su contrato.
