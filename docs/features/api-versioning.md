# API Versionada

## Concepto

Cada versión de la API es una **sub-aplicación FastAPI independiente** montada en el app principal. Esto permite:

- Documentación separada por versión (`/api/v1/docs`, `/api/v2/docs`)
- Middlewares y handlers propios por versión
- Coexistencia de v1 y v2 sin afectarse mutuamente
- Migración gradual de endpoints

## Estructura

```
main.py  (app principal — solo /health y los mounts)
├── GET /health
├── /api/v1  → v1_app (sub-aplicación FastAPI)
│   ├── GET /docs
│   ├── GET /redoc
│   └── /auth, /servers, /managed-databases, …
└── /api/v2  → v2_app (cuando sea necesario)
    ├── GET /docs
    └── /...
```

## `create_versioned_app()` — La Factory

Toda la configuración de middlewares y handlers está centralizada en `app/core/versioned_app.py`.

```python
v1_app = create_versioned_app("v1")
```

Esto configura automáticamente en la sub-app:
- `RequestSizeMiddleware` (validación de tamaño)
- `CORSMiddleware` (orígenes desde `CORS_ORIGINS`)
- `ContextMiddleware` (Request ID, ContextVars)
- `LoggerMiddleware` (si `LOGGER_MIDDLEWARE_ENABLED=True`)
- `SlowAPIMiddleware` + `limiter` (rate limiting global)
- Handlers: `AppHttpException`, `RequestValidationError`, `RateLimitExceeded`, `Exception`
- Docs en `/docs` y `/redoc` (si `DOCS_ENABLED=True`)

## Agregar Rutas a v1

### 1. Crear el router

```python
# app/routes/v1/users.py
from fastapi import APIRouter
from app.controllers.user_controller import UserController
from app.utils.response import ApiResponse, success, paginated, empty
from app.utils.pagination import PaginationDep

router = APIRouter(prefix="/users", tags=["Users"])

@router.get("/", response_model=ApiResponse[list[dict]])
async def list_users(pagination: PaginationDep):
    controller = UserController()
    users = controller.list_users()
    return paginated(users, total=len(users), pagination=pagination)

@router.get("/{user_id}", response_model=ApiResponse[dict])
async def get_user(user_id: int):
    controller = UserController()
    return success(data=controller.get_user(user_id))
```

### 2. Registrar en el agregador de v1

```python
# app/routes/v1/routes.py
from fastapi import APIRouter
from app.routes.v1 import test, users  # agregar users

router = APIRouter()
router.include_router(test.router)
router.include_router(users.router)   # agregar
```

## Agregar v2

### 1. Crear la estructura de rutas

```
app/routes/v2/
├── __init__.py
├── routes.py       # Agregador de v2
└── users.py        # Puede reusar, modificar o reemplazar endpoints de v1
```

### 2. Registrar en `main.py`

```python
# main.py — descomentar las líneas de v2
from app.routes.v2.routes import router as v2_router

v2_app = create_versioned_app("v2")
v2_app.include_router(v2_router)
app.mount("/api/v2", v2_app)
```

v1 queda intacto. Los clientes que usen `/api/v1/...` no se ven afectados.

## Exclusiones de RequestSizeMiddleware por Versión

Si una versión necesita rutas excluidas del middleware de tamaño:

```python
v1_app = create_versioned_app(
    "v1",
    excluded_request_size_paths=["/special-upload"],
)
```

## Rutas Disponibles

| Ruta | Descripción |
|---|---|
| `GET /health` | Health check — sin versión, sin rate limiting |
| `GET /api/v1/docs` | Swagger UI de v1 |
| `GET /api/v1/redoc` | ReDoc de v1 |
| `GET /api/v1/openapi.json` | Schema OpenAPI de v1 |

> Los `/api/v1/test/*` del template **ya no se montan** (`app/routes/v1/routes.py`): eran 7
> rutas sin `AdminDep` en un gateway con credenciales pseudo-root, y dos de ellas escribían
> archivos a disco sin autenticación. Para el smoke test de "¿levanta?" está `GET /health`.
