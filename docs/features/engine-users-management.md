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
| PATCH | `/servers/{id}/users/password` | `EnginePasswordChangeIn` | `ALTER USER/ROLE` |
| DELETE | `/servers/{id}/users` | `?username=&host=&confirm_username=` | `DROP USER/ROLE` |
| POST | `/servers/{id}/users/add-host` | `AddHostIn` | `CREATE USER` (clon) |
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

## Limitaciones conocidas

- **Usuarios legacy**: el CRUD por identidad valida el username con la whitelist estricta
  (`^[A-Za-z_][A-Za-z0-9_]{0,62}$`). Usuarios preexistentes con nombres fuera de ese patrón
  (dígito inicial, `.`/`-`/`$`) no se pueden gestionar por estos endpoints todavía (fallan
  con 422, cerrado). `add-host`/`copy_grants` sí usan la whitelist ampliada.
- **`reveal-password` no tiene rate-limit dedicado** (sí gating admin + auditoría fail-closed).
  Follow-up sugerido: `@limiter.limit(...)` conservador y exclusión explícita de logging de
  body.

## Verificación

- **Tests**: `tests/test_api_engine_users.py` (adapter mockeado): vista agrupada
  (adopted/unmanaged/orphan, supports_hosts por motor), CRUD por identidad, adopción
  opcional, revelar contraseña (200/409/404), agregar host (hash/nueva contraseña, copy
  grants, 422 en PG) + test unitario puro de `_rewrite_grant_line`.
- **Pendiente**: verificación e2e contra motores reales (`add_user_host` /
  `copy_user_grants` con `SHOW CREATE USER` / `SHOW GRANTS` reales en MySQL 8 —
  `caching_sha2_password` — MariaDB y el 422 de PostgreSQL). El rewrite de grantee asume
  identificadores whitelisteados (sin comillas/backslash), por lo que el grantee que emite
  el motor coincide byte a byte con el construido.
