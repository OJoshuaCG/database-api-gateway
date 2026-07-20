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
`ADMIN_USERNAME`, lo crea con el password **hasheado con Argon2** y `is_superuser=True`.
Es idempotente. En producción, arrancar sin `ADMIN_PASSWORD` aborta el inicio.

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
