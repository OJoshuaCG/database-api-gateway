# Manejo de usuarios del motor (vista agrupada + CRUD por identidad)

Este módulo mejora la lectura de los usuarios de un servidor y permite gestionarlos
(CRUD, cambio de contraseña, revelar contraseña, **agregar hosts**) **por identidad
física** — funcione o no adoptado el usuario en el inventario del gateway.

## El problema que resuelve

En MySQL/MariaDB un usuario **no es una entidad única**: `'alice'@'localhost'` y
`'alice'@'%'` son **cuentas separadas**, cada una con su propia contraseña y sus propios
grants. El listado plano (`GET /servers/{id}/users`) devuelve un `user@host` por cuenta,
así que un mismo nombre aparece repetido N veces — difícil de leer.

En PostgreSQL un ROLE **no tiene host** (el acceso por host se controla en `pg_hba.conf`,
fuera del alcance SQL). Un usuario = un rol.

## Asimetría por motor (clave)

| Concepto | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Identidad de usuario | `'user'@'host'` (varias por nombre) | un ROLE (una por nombre) |
| `supports_hosts` | `true` | `false` |
| Agregar host | ✅ `CREATE USER 'u'@'nuevo'` | ❌ 422 (se gestiona en pg_hba.conf) |
| Contraseña | hash por cuenta | hash por rol |

El frontend debe leer `supports_hosts` de la respuesta agrupada y, si es `false`, ocultar
la columna host y el botón "agregar host".

## Vista agrupada y reconciliada

```
GET /servers/{server_id}/users/grouped
```

Cruza el **plano en vivo** (motor) con el **inventario** (`server_users`) y agrupa por
username. Cada identidad se marca:

- `adopted` — existe fila en el inventario (gestionada por el gateway).
- `unmanaged` — existe solo en el motor (adoptable).
- `orphan` — existe solo en el inventario (borrada por fuera del gateway → drift).

```jsonc
{
  "dialect": "mysql",
  "supports_hosts": true,
  "users": [
    {
      "username": "alice",
      "identity_count": 3,
      "identities": [
        { "host": "localhost", "status": "adopted",   "server_user_id": 12,
          "has_password": true, "is_active": true, "notes": null },
        { "host": "%",         "status": "unmanaged", "server_user_id": null,
          "has_password": false, "is_active": null, "notes": null },
        { "host": "10.0.0.5",  "status": "unmanaged", "server_user_id": null,
          "has_password": false, "is_active": null, "notes": null }
      ]
    }
  ]
}
```

### UX de frontend sugerida

- **Una fila por username** (sin redundancia), con badge de conteo (`alice · 3 hosts`).
- **Fila expandible / drawer** con las identidades; cada host con chips de estado
  (`Adoptado`/`Sin adoptar`/`Huérfano`, `con contraseña`, `activo`).
- **Acción por username: "Agregar host"** (oculta si `supports_hosts=false`).
- **Acciones por identidad**: cambiar contraseña, revelar contraseña, eliminar, ver grants.

## CRUD por identidad (adoptados y NO adoptados)

Todos operan por `(server_id, username, host)` directamente sobre el motor. **No exigen**
que el usuario esté adoptado. Si existe fila de inventario que coincide, se sincroniza.

| Verbo | Ruta | Cuerpo / query | Efecto en motor |
|---|---|---|---|
| POST | `/servers/{id}/users` | `EngineUserCreateIn` | `CREATE USER` |
| PATCH | `/servers/{id}/users/password` | `EnginePasswordChangeIn` | `ALTER USER/ROLE` (1 host) |
| PATCH | `/servers/{id}/users/password-all-hosts` | `EnginePasswordChangeAllHostsIn` | `ALTER USER/ROLE` (todos los hosts en vivo) |
| DELETE | `/servers/{id}/users` | `?username=&host=&confirm_username=` | `DROP USER/ROLE` |
| POST | `/servers/{id}/users/add-host` | `AddHostIn` | `CREATE USER` (clon) |
| POST | `/servers/{id}/users/adopt-all-hosts` | `AdoptAllHostsIn` | — (solo inventario) |
| POST | `/servers/{id}/users/define-password` | `DefineKnownPasswordIn` | — (solo inventario) |
| POST | `/servers/{id}/users/reveal-password` | `EngineRevealPasswordIn` | — (solo lectura) |

### Adopción opcional (`adopt`)

Por defecto las operaciones son **stateless**: solo tocan el motor, no crean fila de
inventario. Con `adopt=true` (en create / add-host / cambio de contraseña sobre un usuario
sin fila) el gateway además **registra** el usuario en `server_users`, guardando la
contraseña cifrada (Fernet) — lo que habilita revelarla luego. Si ya existe fila de
inventario, el cambio de contraseña **siempre** la sincroniza.

### `confirm_username` (DELETE)

Igual que el DELETE del inventario: para ejecutar `DROP USER` en el motor, `confirm_username`
debe repetir exactamente el username (doble intención). Si el usuario posee BDs gestionadas
en el inventario → 409 (reasignar/eliminar esas BDs primero).

## Revelar contraseña — límite criptográfico

```
POST /servers/{server_id}/users/reveal-password   { "username": "...", "host": "%" }
```

- El **motor** solo guarda un **hash irreversible**: una contraseña que el gateway nunca
  conoció es **irrecuperable**.
- El **gateway** guarda `password_encrypted` con Fernet (reversible) **solo cuando él fijó
  la contraseña** (create/rotación vía gateway).

Por eso:

- Usuario **no** en el inventario → `404` (adóptalo/gestiónalo por el gateway primero).
- Usuario adoptado **sin** contraseña conocida (`password_encrypted = NULL`) → `409`
  ("solo se puede rotar, no revelar").
- Usuario cuya contraseña fijó el gateway → `200` con la contraseña en claro.

La acción se **audita** (`server_user.password.reveal`). La contraseña nunca aparece en los
listados; revelar es una acción explícita y puntual.

## Agregar host (`add-host`) — solo MySQL/MariaDB

```
POST /servers/{server_id}/users/add-host
```
```jsonc
{
  "username": "alice",
  "source_host": "%",          // cuenta origen desde la que se clona
  "new_host": "10.0.0.5",      // nuevo host
  "reuse_password": true,       // true: copia el hash; false: exige new_password
  "new_password": null,
  "copy_grants": false,         // opcional: replica los permisos del origen
  "adopt": false                // opcional: registra la nueva identidad en el inventario
}
```

- **Misma contraseña** (`reuse_password=true`): se toma la sentencia que el propio motor
  emite con `SHOW CREATE USER` para la cuenta origen (escapa correctamente el hash de auth,
  incluso el binario de `caching_sha2_password`) y solo se **reescribe el grantee** (host).
  El gateway **no descubre** la contraseña en claro.
- **Nueva contraseña** (`reuse_password=false` + `new_password`):
  `CREATE USER 'alice'@'10.0.0.5' IDENTIFIED BY '<nueva>'`.
- **`copy_grants=true`**: lee `SHOW GRANTS` de la cuenta origen y reejecuta cada sentencia
  con el grantee reescrito. Best-effort (un fallo no revierte la creación del host; se
  reporta en `grants_error`). Fail-closed: omite el `USAGE` base, los grants `PROXY` y
  cualquier línea con credencial embebida (`IDENTIFIED BY` de motores viejos).
- **PostgreSQL** → `422` (el rol no tiene host).

## Adopción masiva de hosts

```
POST /servers/{server_id}/users/adopt-all-hosts
```
```jsonc
{
  "username": "alice",
  "known_password": null,   // opcional: ver "Definir vs. rotar" más abajo
  "notes": null
}
```

Adopta de una sola vez **todas las identidades en vivo** de un username (en vez de
llamar `POST /server-users/adopt` una vez por host). Verifica primero contra el motor
(`adapter.list_users()`) qué hosts existen realmente → `404` si el username no existe
en absoluto. Nunca ejecuta `CREATE USER`. Es **fail-tolerant por host**: un host ya
adoptado se reporta `already_adopted` sin abortar el resto del lote.

```jsonc
{
  "username": "alice",
  "dialect": "mysql",
  "total_hosts": 3,
  "adopted": 2,
  "results": [
    { "host": "localhost", "status": "adopted",         "server_user_id": 41 },
    { "host": "%",         "status": "already_adopted", "server_user_id": 12 },
    { "host": "10.0.0.5",  "status": "adopted",          "server_user_id": 42 }
  ]
}
```

No reemplaza al `POST /server-users/adopt` singular (Plan 09), que sigue existiendo
para adoptar una sola identidad puntual.

## Definir vs. rotar contraseña (individual o todos los hosts)

Son dos operaciones **deliberadamente distintas** y no deben confundirse:

| | Toca el motor | Uso típico | Endpoint(s) |
|---|---|---|---|
| **Definir** (`define-password`) | ❌ Nunca (solo cifra y guarda) | El admin humano YA sabe cuál es la contraseña real vigente y solo quiere que el gateway la recuerde, para poder revelarla después | `POST /servers/{id}/users/define-password` |
| **Rotar/cambiar** (`password[-all-hosts]`) | ✅ Sí (`ALTER USER/ROLE` real) | Se quiere cambiar la contraseña de verdad, o no se conoce la actual | `PATCH /servers/{id}/users/password`, `PATCH /servers/{id}/users/password-all-hosts` |

Ambas admiten alcance **individual** (un host específico) o **global** (todos los
hosts en vivo del username). El alcance se controla con un campo explícito — **nunca**
sobrecargando `host`, porque `"%"` ya es un host real de MySQL, no un significado de
"todos".

### Definir contraseña conocida

```jsonc
// scope="host": solo esa identidad
{ "username": "alice", "scope": "host", "host": "localhost", "known_password": "Secr3t!", "overwrite": false }

// scope="all_hosts": todas las identidades en vivo del username
{ "username": "alice", "scope": "all_hosts", "known_password": "Secr3t!", "adopt_if_missing": true }
```

- Nunca ejecuta `ALTER USER`: **es responsabilidad del admin** que el valor coincida
  con la contraseña real del motor. Si se equivoca, `reveal-password` devolverá un
  valor incorrecto sin que el gateway pueda detectarlo (no hay forma de verificarlo sin
  tocar el motor).
- `adopt_if_missing=true` registra en el inventario los hosts en vivo sin fila previa.
  Sin la flag, esos hosts se reportan `skipped_not_found`.
- **`overwrite=true` es obligatorio** para sobrescribir una identidad que YA tenía una
  contraseña guardada por el gateway; sin el flag, esa identidad se reporta
  `conflict_needs_overwrite` y no se toca (evita reemplazos accidentales de un valor
  que ya era revelable correctamente). Sobrescribir SÍ audita fail-closed
  (`record_intent`) antes de escribir, misma clase de riesgo que `reveal-password`.

### Rotar contraseña en todos los hosts

```jsonc
{
  "username": "alice",
  "new_password": "N3wP4ss!",
  "confirm_username": "alice",
  "adopt_if_missing": false
}
```

- `confirm_username` debe repetir exactamente el `username` (doble intención, mismo
  patrón que `DROP USER`) porque esta operación ejecuta `ALTER USER` real e
  irreversible sobre N cuentas de una sola vez.
- **Fail-tolerant por host**: un fallo en un host no aborta la rotación de los demás;
  la respuesta reporta `status="error"` con el detalle para el host que falló, dejando
  claro que ese host quedó con la contraseña anterior mientras los demás ya rotaron
  (estado real divergente en el motor, no solo en el inventario — revisar `results`).
- El endpoint individual `PATCH /servers/{id}/users/password` no cambia: sigue
  operando sobre un solo host, sin `confirm_username`.

## Seguridad

- Todos los endpoints requieren admin autenticado.
- Identificadores validados por whitelist + quoting (doble defensa); host validado con
  `validate_host`.
- **Guard anti auto-lockout (crítico)**: los endpoints por identidad operan directo sobre
  el motor, saltándose el inventario. Por eso replican el guard de `grant_controller`:
  crear/rotar/dropear/agregar-host sobre `Server.root_username` (la credencial pseudo-root
  del gateway, que normalmente **no** es una fila de `server_users`) devuelve **409**. Sin
  este guard, un `DROP USER` o un cambio de contraseña sobre esa cuenta dejaría al gateway
  sin control del servidor de forma irreversible.
- **Auditoría**: toda operación que toca el motor se audita; `add-host` y `drop` registran la
  **intención** fail-closed antes de ejecutar. **Revelar contraseña** (divulgación de un
  secreto en claro) audita **fail-closed** (`record_intent`) antes de descifrar/retornar: si
  el rastro no se persiste, el secreto no sale.
- `copy_grants` reejecuta DDL/DCL leído del motor: es el **mismo** servidor que el gateway
  ya administra con pseudo-root (no se cruza una frontera de confianza nueva). Fail-closed:
  omite el `USAGE` base y los grants `PROXY`. **Advertencia**: replica fielmente privilegios
  **globales** (`ALL ON *.*`, `SUPER`, …) y `WITH GRANT OPTION` de la cuenta origen — es la
  semántica esperada de "clonar la cuenta", pero úsalo con criterio (over-provisioning).
- **Operaciones masivas** (`adopt-all-hosts`, `define-password`, `password-all-hosts`):
  el guard anti auto-lockout se evalúa **una sola vez sobre el username**, antes de
  iterar hosts (no repetido por ítem). `password-all-hosts` exige `confirm_username`
  por el mismo motivo que `DROP USER` (ALTER real e irreversible sobre N cuentas).
  `define-password` audita con una acción (`server_user.password.define`)
  inequívocamente distinta de la rotación real (`server_user.password.rotate_batch` /
  `server_user.update`), para que un análisis de auditoría pueda filtrar sin ambigüedad
  qué fue una rotación real en el motor vs. qué fue solo el gateway memorizando un dato.

## Limitaciones conocidas

- **Usuarios legacy**: el CRUD por identidad valida el username con la whitelist estricta
  (`^[A-Za-z_][A-Za-z0-9_]{0,62}$`). Usuarios preexistentes con nombres fuera de ese patrón
  (dígito inicial, `.`/`-`/`$`) no se pueden gestionar por estos endpoints todavía (fallan
  con 422, cerrado). `add-host`/`copy_grants` sí usan la whitelist ampliada.
- **`reveal-password` no tiene rate-limit dedicado** (sí gating admin + auditoría fail-closed).
  Follow-up sugerido: `@limiter.limit(...)` conservador y exclusión explícita de logging de
  body.
- **`define-password` no verifica el valor contra el motor**: al no ejecutar ALTER USER,
  no hay forma de confirmar que `known_password` coincida con la contraseña real vigente.
  Si el admin se equivoca, `reveal-password` devolverá ese valor incorrecto sin que el
  gateway pueda detectarlo — es una limitación aceptada por diseño (sin 2FA/verificación
  adicional por ahora).
- **Rotación batch puede quedar parcial**: si `password-all-hosts` falla en un host, ese
  host conserva la contraseña anterior mientras los demás ya rotaron — un estado real
  divergente en el motor, no solo en el inventario. Revisar siempre `results` por host.
- **Sin tope de hosts por request** en las operaciones masivas todavía (análogo al
  `max_databases` de `apply-all` de migraciones). Follow-up sugerido si un username
  acumula muchos hosts históricos por drift.

## Verificación

- **Tests**: `tests/test_api_engine_users.py` (adapter mockeado): vista agrupada
  (adopted/unmanaged/orphan, supports_hosts por motor), CRUD por identidad, adopción
  opcional, revelar contraseña (200/409/404), agregar host (hash/nueva contraseña, copy
  grants, 422 en PG), adopción masiva de hosts, definir contraseña conocida
  (individual/global, overwrite), rotación de contraseña en todos los hosts
  (confirm_username, fail-tolerante por host) + test unitario puro de `_rewrite_grant_line`.
- **Pendiente**: verificación e2e contra motores reales (`add_user_host` /
  `copy_user_grants` con `SHOW CREATE USER` / `SHOW GRANTS` reales en MySQL 8 —
  `caching_sha2_password` — MariaDB y el 422 de PostgreSQL). El rewrite de grantee asume
  identificadores whitelisteados (sin comillas/backslash), por lo que el grantee que emite
  el motor coincide byte a byte con el construido. Las tres operaciones masivas nuevas
  (adopt-all-hosts/define-password/password-all-hosts) también quedan pendientes de
  verificación e2e contra motores reales.
