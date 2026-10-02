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
| `bootstrap_admin()` | Siembra el admin al arrancar (lifespan) desde `ADMIN_USERNAME`/`ADMIN_PASSWORD`. |
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

En el `lifespan` de `main.py` se llama `bootstrap_admin()`: si no existe el usuario
`ADMIN_USERNAME`, lo crea con el password **hasheado con Argon2**, con
`gateway_role='owner'` y con las dos capacidades globales (`access_admin` y
`security_officer`). Es idempotente. En producción, arrancar sin `ADMIN_PASSWORD` aborta el
inicio.

Las tres cosas se fijan EXPLÍCITAMENTE y no se heredan de defaults: `users.gateway_role` tiene
`server_default='viewer'` a propósito —para que ninguna fila nazca con privilegio— y `owner`
no alcanza solo, porque `servers.admin`, `catalogs.write`, `access.admin` y `policy.admin` viven
**únicamente** en las capacidades globales. Sin ellas, el admin recién sembrado no podría dar de
alta un servidor, administrar usuarios ni rotar la clave de datos.

Las dos globales no se solapan: `access_admin` = `{access.admin}` (usuarios, accesos,
capacidades puntuales, tokens) y `security_officer` = `{policy.admin, servers.admin,
catalogs.write, environments.write}`. Un `security_officer` **sin** `access_admin` no administra
usuarios, y un `access_admin` sin `security_officer` no rota el cifrado (`api-reference-v29.md`).

**Separación de deberes.** `security_officer` no puede convivir con `owner` (en ninguna forma)
ni con `access_admin` en una cuenta: los escritores responden `409 access.sod_conflict` salvo un
`sod_override` con motivo (auditado, vence en 7 días como mucho), y el lector descarta
`security_officer` si la combinación no tiene excepción viva. El admin sembrado junta las tres
cosas, así que `bootstrap_admin` le **hereda** la combinación (`sod_exceptions`,
`reason='grandfathered'`) al sembrarlo o revivirlo, y el arranque la reporta
(`access.sod_grandfathered`). Contrato completo en `api-reference-v29.md` §8; la regla, en
`app/core/separation_of_duties.py`.

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
