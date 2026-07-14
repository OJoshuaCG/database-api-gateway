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
from fastapi.middleware.cors import CORSMiddleware
from app.core.environments import CORS_ORIGINS
from app.core.versioned_app import cors_allow_credentials

app.add_middleware(
    CORSMiddleware,
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
