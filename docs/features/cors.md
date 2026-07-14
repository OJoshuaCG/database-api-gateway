# CORS

## Configuración

```env
CORS_ORIGINS=http://localhost:3000,https://myapp.com
```

La variable acepta orígenes separados por coma. Se parsea automáticamente en `environments.py`.

```env
# Desarrollo — permitir todos los orígenes
CORS_ORIGINS=*

# Producción — orígenes específicos
CORS_ORIGINS=https://myapp.com,https://admin.myapp.com,https://api.myapp.com
```

## Configuración Actual

```python
CORSMiddleware(
    allow_origins=CORS_ORIGINS,   # Desde variable de entorno
    allow_credentials=True,        # Permite cookies y headers de auth
    allow_methods=["*"],           # Todos los métodos HTTP
    allow_headers=["*"],           # Todos los headers
)
```

Para personalizar métodos u headers específicos, editar `create_versioned_app()` en `app/core/versioned_app.py`.

## Advertencia: `*` + `credentials=True`

Los browsers rechazan respuestas con `Access-Control-Allow-Origin: *` cuando la request incluye credenciales (cookies, `Authorization` header). Si tu frontend envía credenciales, **debes definir orígenes específicos**:

```env
# ❌ No funciona con credenciales en browser
CORS_ORIGINS=*

# ✓ Funciona con credenciales
CORS_ORIGINS=http://localhost:3000,https://myapp.com
```

## Posición en el Stack de Middlewares

CORS es el segundo middleware en ejecutarse (después de `RequestSizeMiddleware`). Esto permite que las requests `OPTIONS` de preflight sean respondidas inmediatamente, antes de que se procesen en middlewares más internos.

## CORS en `/health`

El endpoint `/health` (y `/health/ready`) está en el app principal, **no** en la sub-app
versionada. Sin su propio `CORSMiddleware`, el navegador bloquea la LECTURA de la
respuesta desde un origen distinto (p. ej. el frontend en dev, `localhost:5173`), aunque
la respuesta llegue con 200 — síntoma típico: "CORS Missing Allow Origin" en devtools con
el body visible en la pestaña Network.

**Ya está resuelto** (`main.py`, aplicado al app principal junto con `app.include_router`):

```python
from app.core.environments import CORS_ORIGINS
from app.core.versioned_app import PathScopedCORSMiddleware, cors_allow_credentials

app.add_middleware(
    PathScopedCORSMiddleware,
    path_prefix="/health",
    allow_origins=CORS_ORIGINS,
    # cors_allow_credentials (no True fijo): con CORS_ORIGINS="*" da False, evitando la
    # combinación que los browsers rechazan (ver advertencia arriba). /health no usa
    # cookies de sesión de todos modos (no hay SessionMiddleware en el app principal).
    allow_credentials=cors_allow_credentials(CORS_ORIGINS),
    allow_methods=["GET"],
    allow_headers=["*"],
)
```

Test de regresión: `tests/test_health.py::test_health_endpoints_send_cors_header`.

### ⚠️ GOTCHA: NO usar `CORSMiddleware` directo en el app principal

Un intento inicial de este fix usó `app.add_middleware(CORSMiddleware, ...)` directo en el
app principal, con `allow_methods=["GET"]` (suficiente para `/health`, que solo tiene
rutas `GET`). Esto **rompió el preflight de TODA la API `/api/v1`**: el middleware del
app principal envuelve también las sub-apps montadas (`app.mount("/api/v1", v1_app)`),
así que interceptaba el preflight de cualquier `POST`/`PUT`/`DELETE` de `/api/v1/*`
**antes** de que llegara al `CORSMiddleware` propio de esa sub-app — y como
`allow_methods=["GET"]` no incluye `POST`, el navegador recibía
`400 Disallowed CORS method` al intentar loguearse (`Access-Control-Request-Method: POST`
a `/api/v1/auth/login`), pese a que la sub-app v1 sí lo permite. Bug real reportado por
el equipo de frontend, detectado en devtools como "CORS Preflight Did Not Succeed".

Por eso se usa `PathScopedCORSMiddleware` (`app/core/versioned_app.py`): envuelve
`CORSMiddleware` pero solo lo activa para paths que empiecen con `path_prefix`
(`"/health"`); para cualquier otro path (incluido todo `/api/v1/*` y futuras
`/api/v2/*`), pasa la request sin tocar, dejando que la sub-app versionada resuelva su
propio CORS sin interferencia. **Nunca agregar un `CORSMiddleware` sin acotar por path al
app principal** mientras existan sub-apps montadas debajo — cualquier middleware ahí
envuelve también a esas sub-apps.

Test de regresión:
`tests/test_health.py::test_v1_cors_preflight_not_blocked_by_main_app_cors`.
