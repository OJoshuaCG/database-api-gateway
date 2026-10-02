# Autenticación (sesión + administrador)

El gateway es una herramienta **interna**: no gestiona múltiples usuarios, a lo sumo un
**administrador** único. La autenticación usa una **cookie de sesión httpOnly firmada**
y toda la lógica de "quién está autenticado" pasa por una única dependencia,
`get_current_admin`, para poder migrar a SSO en el futuro sin tocar los endpoints.

Módulos: `app/core/auth.py`, `app/utils/security.py`, `app/routes/v1/auth.py`,
`app/controllers/auth_controller.py`.

## Piezas

| Pieza | Rol |
|---|---|
| `SessionMiddleware` (Starlette) | Cookie de sesión firmada con `itsdangerous`. Se añade en `create_versioned_app()`. |
| `app/utils/security.py` | Hashing de password con **Argon2id** (`hash_password`, `verify_password`). |
| `bootstrap_admin()` | Siembra el administrador de accesos de una instalación vacía (lifespan) desde `ADMIN_USERNAME`/`ADMIN_PASSWORD`; con `ADMIN_RECOVERY=1`, lo recupera. |
| `app/services/bootstrap_window.py` | Ventana de arranque: el único `access_admin` eleva sin segundo aprobador hasta que exista otro. |
| `get_current_admin` | Dependencia que exige sesión válida; devuelve `{id, username}`. |
| `AuthController` | Verifica credenciales contra la tabla `users`. |

## Flujo

```
POST /auth/login ──▶ AuthController.authenticate (Argon2 verify)
                       │ éxito
                       ▼
                 login_session(request, admin)   → request.session["admin_id"] = id
                       │
        cookie "gw_session" (httpOnly, firmada)  ◀── se envía al cliente

GET /servers (con cookie) ──▶ get_current_admin lee la sesión, recarga el usuario,
                              verifica is_active → 401 si algo falla
```

### Bootstrap del administrador

En el `lifespan` de `main.py` se llama `bootstrap_admin()`. En una instalación **vacía** (tabla
`users` sin filas) crea `ADMIN_USERNAME` con el password **hasheado con Argon2**, con
`gateway_role='viewer'` y **solo** la global `access_admin`: administra accesos y nada más. No es
`owner` ni `security_officer`. En producción, arrancar sin `ADMIN_PASSWORD` aborta el inicio.

Con usuarios ya creados **no siembra ni repara nada**, aunque no quede ningún `access_admin`
utilizable: lo dice en el log y pide `ADMIN_RECOVERY=1` (ver "Recuperación" abajo). Si el
arranque reparara solo, desactivar al administrador sería reversible por reinicio.

> **Instalaciones existentes.** El cambio de siembra aplica solo a instalaciones **nuevas**. Una
> instalación que se actualiza conserva su cuenta sembrada antes, que junta `owner` +
> `access_admin` + `security_officer` con la combinación **heredada** (`sod_exceptions`,
> `reason='grandfathered'`). No se parte sola: se reparte creando cuentas de una función (ver
> `api-reference-v29.md` §8.4 y §10).

### Primer arranque: la ventana de arranque

Toda elevación (rol `owner`, cualquier global, una capacidad exclusiva de `owner`) espera a un
**segundo** `access_admin`, y una instalación nueva tiene uno solo. Por eso el primer arranque
abre una **ventana de arranque**: mientras está abierta y hay un solo `access_admin` activo con
credencial, sus elevaciones se aplican en el acto y cada una se audita
`access.bootstrap_assignment`. El arranque loguea un warning con el vencimiento mientras siga
abierta, y `/auth/me.bootstrap_window` (`{open, closes_at}`, solo para `access_admin`) alimenta el
banner de la SPA.

El procedimiento esperado, con el administrador sembrado:

1. Entrar como `ADMIN_USERNAME`.
2. Crear un usuario con la global `security_officer` (servidores, catálogos, entornos, cifrado).
3. Crear un usuario `owner` (opera: bases, migraciones, exportaciones, consola SQL).
4. Crear el **segundo** `access_admin` y entregarle su invitación.
5. Cuando esa persona acepta la invitación, la ventana **se cierra para siempre**
   (`closed_reason='second_admin'`). Desde ahí, toda elevación queda pendiente y la aprueba el
   otro `access_admin`.

Si nadie completa el paso 5, la ventana se cierra igual al vencer `ACCESS_BOOTSTRAP_WINDOW_HOURS`
(72 h por defecto, `closed_reason='deadline'`). Las instalaciones que ya tenían dos o más
`access_admin` al migrar la reciben cerrada (`multiple_admins_at_upgrade`). Una instalación que
de verdad va a tener un solo administrador sigue usando `ACCESS_FOUR_EYES=False`.

### Recuperación (`ADMIN_RECOVERY=1`)

Si una instalación se queda sin ningún `access_admin` utilizable (desactivado por SQL a mano, la
única cuenta perdió la global), se arranca **una vez** con `ADMIN_RECOVERY=1`. Ese arranque:

- reactiva la cuenta `ADMIN_USERNAME` (o la crea como `viewer` + `access_admin` si no existe);
- le devuelve **solo** `access_admin`: nunca `security_officer` ni `owner`, y no le quita lo que ya
  tenía;
- **no toca la contraseña** (recupera a quien la tiene; no es un reseteo);
- reabre la ventana de arranque con plazo nuevo, para que esa cuenta pueda recomponer el resto;
- audita `access.admin_recovery` y loguea un warning.

Después hay que **quitar el flag**: cada arranque con él vuelve a reabrir la ventana. Si ya hay
otro `access_admin` con credencial, la ventana se vuelve a cerrar en el mismo arranque.

**El ancla de confianza es el acceso al servidor.** Quien puede fijar variables de entorno y
reiniciar el proceso ya controla el gateway: lee `SECRET_KEY`, la BD de metadatos y las
credenciales cifradas. El flag no le da nada que no tenga; lo que garantiza es que la recuperación
sea un acto explícito, acotado y auditado, y que un reinicio común nunca reviva una cuenta.

Las dos globales no se solapan: `access_admin` = `{access.admin}` (usuarios, accesos,
capacidades puntuales, tokens) y `security_officer` = `{policy.admin, servers.admin,
catalogs.write, environments.write}`. Un `security_officer` **sin** `access_admin` no administra
usuarios, y un `access_admin` sin `security_officer` no rota el cifrado (`api-reference-v29.md`).

**Separación de deberes.** `security_officer` no puede convivir con `owner` (en ninguna forma)
ni con `access_admin` en una cuenta: los escritores responden `409 access.sod_conflict` salvo un
`sod_override` con motivo (auditado, vence en 7 días como mucho), y el lector descarta
`security_officer` si la combinación no tiene excepción viva. El admin sembrado ANTES de C4 junta
las tres cosas; la migración `f8b0d2e4a6c9` le **heredó** la combinación (`sod_exceptions`,
`reason='grandfathered'`) y el arranque la reporta (`access.sod_grandfathered`). Contrato completo en `api-reference-v29.md` §8; la regla, en
`app/core/separation_of_duties.py`.

**Segundo aprobador para las elevaciones.** Quien administra accesos ya no tiene un techo por
tenencia ("no das más de lo que tenés"): `access_admin` asigna cualquier rol, global o capacidad
puntual, sea él `viewer` u `owner`. A cambio, toda **elevación** —rol `owner` (base o por
alcance), cualquier global, una capacidad puntual exclusiva de `owner` o un `sod_override`— queda
**pendiente** hasta que OTRO `access_admin` la apruebe (`POST /access-requests/{id}/approve`).
Lo que no eleva, y siempre las bajas, se aplica en el acto; la respuesta con algo pendiente es
`202 access.elevation_pending`. Es lo que impide que un solo administrador se cree un títere
`owner` y entre con su invitación. Con un solo administrador real (y solo entonces),
`ACCESS_FOUR_EYES=False` deja elevar sin segundo aprobador: el arranque avisa y cada elevación se
audita `access.elevation_unapproved`. Contrato en `api-reference-v29.md` §9; la ventana de
arranque que deja a una instalación nueva crear su segundo `access_admin`, en §10.

> `is_superuser` **se retiró**: se escribía en tres lugares y no se leía en ninguno para
> autorizar, así que no era "todavía no hay permisos" sino un sistema multiusuario sin puerta.
> Retirarlo no cambió el contrato: `AdminOut` sigue siendo `{id, username}`.

### La dependencia `get_current_admin`

```python
from app.core.auth import AdminDep   # = Annotated[dict, Depends(get_current_admin)]

@router.get("/algo")
def endpoint(admin: AdminDep):
    # admin == {"id": 1, "username": "admin"}
    ...
```

- Lee `request.session["admin_id"]`; si no hay → `AppHttpException(401)`.
- Recarga el usuario de la BD y verifica `is_active` (revocación efectiva: desactivar
  el usuario invalida la sesión en el siguiente request).

## Endpoints

```http
POST /api/v1/auth/login     # {username, password} → set-cookie; rate-limit 5/min
POST /api/v1/auth/logout    # limpia la sesión
GET  /api/v1/auth/me        # admin actual
```

**Login:**

```bash
curl -c cookies.txt -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"cambia-esto"}'
# → {"data": {"id": 1, "username": "admin"}, "message": "Sesión iniciada."}
```

El mensaje de error de credenciales es **genérico** (`"Credenciales inválidas."`) para
no revelar si el usuario existe.

## Configuración

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=              # OBLIGATORIO en producción (sin él, no se siembra admin)
SESSION_SECRET=             # firma de la cookie; si vacío, se deriva de SECRET_KEY
SESSION_MAX_AGE=28800       # duración de la sesión en segundos (8h)
SESSION_COOKIE_SECURE=      # vacío = sigue a APP_ENV=="production"; True/False la desacopla
STEP_UP_ENFORCED=True       # exige contraseña fresca para las capacidades con step-up
STEP_UP_TTL_SECONDS=300     # duración de la ventana de step-up (login o POST /auth/step-up)
ACCESS_FOUR_EYES=True       # elevaciones con segundo aprobador
ACCESS_BOOTSTRAP_WINDOW_HOURS=72  # ventana de arranque: horas desde el primer arranque
ADMIN_RECOVERY=             # 1 SOLO para recuperar el administrador de accesos; quitar después
```

La cookie es `httpOnly`, `same_site=lax` y `https_only` según `SESSION_COOKIE_SECURE`
(por defecto, igual a `APP_ENV=="production"`).

### `SESSION_COOKIE_SECURE`

Controla el flag `Secure` de la cookie de forma independiente de `APP_ENV`. Por defecto
seguir a `APP_ENV` es correcto: en producción, un navegador rechaza silenciosamente una
cookie `Secure` si el sitio se sirve por HTTP plano (login "exitoso" pero cualquier otro
endpoint devuelve 401, porque la cookie nunca se guardó/reenvió — ver
[troubleshooting en dokploy-deployment.md](../dokploy-deployment.md)).

Fijar `SESSION_COOKIE_SECURE=False` con `APP_ENV=production` permite operar sin TLS
delante del gateway (todas las demás validaciones de producción — `SECRET_KEY`,
`SESSION_SECRET` independiente, `CORS_ORIGINS` sin `*` — se mantienen intactas). Es un
downgrade de seguridad real: la cookie de sesión del admin (acceso completo a la
administración de credenciales pseudo-root de los servidores destino) viajaría sin
cifrar, exponible a cualquiera en la misma red. El arranque loguea un `WARNING`
explícito cuando esta combinación está activa. Usar solo como diagnóstico temporal
mientras se termina de configurar HTTPS, nunca como configuración final.

## Seguridad

- **Hashing Argon2id** (recomendado por OWASP) para el password del admin.
- **Rate limiting** en `login` (`@limiter.limit("5/minute")`) contra fuerza bruta.
- **No-fuga en logs:** el body de `/auth/login` se oculta por completo en el
  `LoggerMiddleware`, y los campos sensibles se enmascaran en cualquier otro endpoint
  (ver [logging](logging.md)).
- `same_site=lax` mitiga CSRF en peticiones cross-site; para un frontend en navegador,
  recuerda fijar `CORS_ORIGINS` a orígenes específicos (no `*`).

## Step-up ("sudo mode")

Las capacidades marcadas `requires_step_up` en el catálogo (`/authz/catalog`; las del actor en
`/auth/me` → `step_up_capabilities`) exigen haber **confirmado la contraseña hace menos de
`STEP_UP_TTL_SECONDS`** (300 s). Sin eso responden `403` con
`public_context = {"code": "access.step_up_required", "step_up_ttl_seconds": 300}`.

- **La ventana es de la SESIÓN, por tiempo y no deslizante.** La abre el login (una contraseña
  recién tipeada cuenta) y la renueva `POST /api/v1/auth/step-up {password}`. Usarla no la
  estira. No es de un solo uso: cubre los flujos preview → execute sin pedir dos veces.
- **Cuándo se pide:** método no seguro, **o** capacidad que divulga (`exports/{id}/content`,
  `download`, capturas de SELECT), **o** método desconocido (fail-closed). Un `GET` que no
  divulga —listar usuarios del gateway, el `delete-plan` de una versión— no lo pide.
- **Excepción: cancelar no pide step-up.** Frenar una operación destructiva nunca puede costar
  más que lanzarla. Son exactamente cuatro rutas, todas `POST .../cancel`: clonado
  (`/database-clones/{job_id}/cancel`), lote de clonado
  (`/database-clone-batches/{batch_id}/cancel`), conversión de collation
  (`/collation-conversions/{job_id}/cancel`) y lote de conversión
  (`/database-models/{model_id}/collation-conversions/{batch_id}/cancel`). La cancelación de una
  exportación usa `exports.execute`, que no exige step-up. **Las capas 1 y 2 siguen valiendo**:
  sin la capacidad en ese destino, `403 access.forbidden`. La exención se declara con
  `step_up=False` en `require`/`require_at` y tiene que figurar, con su motivo, en
  `STEP_UP_EXEMPT` de `scripts/check_route_capabilities.py`; el chequeo 7 rechaza una exención
  sin entrada o que no sea un `POST .../cancel`.
- **Orden:** capa 1 (`access.forbidden`) → capa 2 → step-up. A quien le falta la capacidad le
  llega el `403 access.forbidden`, nunca un pedido de contraseña.
- **Reintento seguro:** el `403` sale de la dependencia o del guard al tope del handler, antes
  de cualquier efecto. `GET .../content` no consume el artefacto con este 403.
- **Fallos:** contraseña incorrecta → `400 auth.step_up_failed` (no `401`: no debe disparar el
  logout de la SPA), con `attempts_remaining`. Al **quinto fallo seguido** la sesión se revoca
  (`step_up_failed`) y la respuesta es `401 auth.session_step_up_failed`. Límite: 5/min por
  usuario + IP.
- **El `sid` no rota** en el step-up: el token CSRF de los requests en vuelo sigue valiendo.
- **Agentes:** un token nunca tiene una capacidad con step-up (invariante 11 del catálogo).
- `STEP_UP_ENFORCED=False` lo apaga (el arranque avisa) y `/auth/me` publica
  `step_up_enforced: false` para que la SPA no pida nada.

El diseño completo está en el docstring de `app/core/step_up.py`.

## Sesiones

Las sesiones viven del lado del servidor (`gateway_sessions`); la cookie lleva solo el `sid`
firmado. Vencen por **vida absoluta** (`SESSION_ABSOLUTE_MAX_HOURS`, default 12, contra
`created_at`) y por **inactividad** (`SESSION_IDLE_MINUTES`, default 60, contra `last_seen_at`).
Toda revocación tacha la fila con un motivo de vocabulario cerrado (`revoked_reason`) y el
siguiente request de esa sesión responde `401 auth.session_<motivo>`, con un mensaje propio por
motivo (`_MENSAJE_401` en `app/core/auth.py`).

| Ruta | Capacidad | Qué hace |
|---|---|---|
| `GET /auth/sessions` | `self.read` | Las sesiones vivas propias, con `sid_prefix` (8 caracteres) y `current`. |
| `POST /auth/sessions/revoke-others` | `self.read` | Cierra las propias **menos la actual** (motivo `admin_revoked`). |
| `GET /gateway-users/{id}/sessions` | `access.admin` | Las sesiones vivas de OTRA persona: `created_at`, `last_seen_at`, `expires_at`, `ip`. **Sin `sid` ni prefijo.** |
| `POST /gateway-users/{id}/sessions/revoke` | `access.admin` + step-up | Cierra **todas** las sesiones vivas de otra persona (motivo `access_admin_revoked` → `401 auth.session_access_admin_revoked`). Sobre uno mismo, `409 access.self_modification_forbidden`. Auditado `gateway_user.sessions_revoked` con la cantidad. |

Además cortan sesiones, como efecto: cambiar el rol o desactivar la cuenta (`role_change`),
cambiar la contraseña propia (`password_change`: todas, y la respuesta abre una sesión nueva), cinco
fallos seguidos de step-up (`step_up_failed`) y el logout (`logout`).

**Revocar sesiones no toca la contraseña.** Ante una credencial filtrada, la revocación
administrativa va junto con desactivar la cuenta; si no, quien tiene la contraseña vuelve a
entrar. Las filas ya vencidas y sin tachar no se cuentan ni se re-etiquetan: conservan el motivo
real (`idle`/`absolute`) que les pone el próximo intento.

**La auditoría la lee otra función.** Quien corta sesiones (`access_admin`) no lee
`GET /audit-log`: eso es `policy.admin` (`security_officer`), para que el revisado no se revise a
sí mismo. Contrato completo de las dos piezas en `api-reference-v29.md` §11.

## Migración a SSO (futuro)

Como todos los endpoints dependen de `get_current_admin`, sustituir el mecanismo por
**OIDC/SSO corporativo** (Authlib + IdP) o añadir roles no requiere cambiar los
endpoints. Ver [plan 06](../plans/06-operacion-seguridad-observabilidad.md).

## Pruebas

`tests/test_api_auth.py` (login/logout/me, 401 sin sesión, credenciales inválidas,
validación) y `tests/test_security.py` (Argon2). El rate-limit se verifica en vivo
(5 × 200 → 429).

---

**Siguiente**: [Gestión de servidores](server-management.md)
