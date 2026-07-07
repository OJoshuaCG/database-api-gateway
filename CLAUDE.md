# FastAPI Template - Guía para Agentes de IA

Este documento proporciona contexto y guías para agentes de IA que trabajen en este proyecto.

## Descripción del Proyecto

**Template de FastAPI** diseñado para ser la base de nuevos proyectos. Incluye configuración robusta, mejores prácticas y herramientas esenciales para desarrollo profesional.

### Arquitectura: Pseudo-MVC (Sin Vista)

**Routes → Controllers → Models → Database**

- **Routes** (`app/routes/`): Definen endpoints, validan entrada con Pydantic schemas
- **Controllers** (`app/controllers/`): Lógica de negocio y orquestación
- **Models** (`app/models/`): Interacción con base de datos (SQL directo o ORM)

### Arquitectura de API Versioning

Cada versión de API es una **sub-app FastAPI independiente** montada en el app principal:

```
main.py (FastAPI principal)
  ├── GET /health          ← en el app principal, sin middlewares de versión
  ├── /api/v1 → v1_app    ← sub-app con su propio stack de middlewares
  └── /api/v2 → v2_app    ← sub-app independiente (a futuro)
```

`create_versioned_app()` en `app/core/versioned_app.py` crea sub-apps con todo configurado: middlewares, handlers de excepciones, rate limiting, CORS, documentación.

## Estructura de Carpetas

```
fastapi-template/
├── app/
│   ├── core/
│   │   ├── environments.py     # Todas las variables de entorno
│   │   ├── logger.py           # Sistema de logging centralizado
│   │   ├── context.py          # ContextVars de request (Request ID, IP, etc.)
│   │   ├── database.py         # Gestión de conexiones (pool SQLAlchemy)
│   │   ├── limiter.py          # Singleton Limiter de SlowAPI
│   │   └── versioned_app.py    # Factory create_versioned_app()
│   ├── controllers/            # Lógica de negocio (MVC)
│   ├── exceptions/
│   │   ├── AppHttpException.py # Excepción personalizada con tracking
│   │   ├── HandlerExceptions.py# Handlers globales de excepciones
│   │   └── __init__.py
│   ├── middleware/
│   │   ├── ContextMiddleware.py    # Request ID + ContextVars
│   │   ├── LoggerMiddleware.py     # Logging de requests/responses
│   │   └── RequestSizeMiddleware.py# Límite de tamaño de request
│   ├── models/
│   │   ├── base.py             # DeclarativeBase + TimestampMixin SQLAlchemy 2.0
│   │   ├── user.py             # Modelo ORM de ejemplo
│   │   ├── *_model.py          # Modelos de datos (SQL directo)
│   │   └── __init__.py         # CRÍTICO: todos los modelos deben importarse aquí
│   ├── routes/
│   │   ├── health.py           # GET /health (en app principal)
│   │   └── v1/
│   │       ├── __init__.py     # Router v1 que agrupa sub-routers
│   │       └── test.py         # Endpoints de ejemplo/testing
│   ├── schemas/                # Schemas Pydantic (opcional)
│   └── utils/
│       ├── response.py         # ApiResponse[T], success(), paginated(), empty()
│       ├── pagination.py       # PaginationParams, PaginationDep
│       ├── file_upload.py      # save_upload(), save_uploads()
│       └── dict_utils.py       # Sanitización de dicts (usado por database.py)
├── alembic/
│   ├── versions/               # Migraciones generadas
│   └── env.py                  # Configuración Alembic integrada con el proyecto
├── docs/                       # Documentación completa
│   ├── features/               # Por feature: cors, rate-limiting, pagination, etc.
│   └── development/            # Guías de desarrollo
├── uploads/                    # Archivos temporales de upload (.gitkeep)
├── main.py                     # Punto de entrada
├── pyproject.toml              # Dependencias y configuración
└── .env.example                # Template de variables de entorno
```

## Componentes Clave

### `app/core/environments.py`

Central de todas las variables de entorno. Al agregar una nueva variable, siempre agregarla aquí y documentarla en `.env.example`.

Variables actuales:

```python
# App
APP_ENV        # development | production
APP_NAME       # Nombre de la aplicación
SECRET_KEY     # Clave secreta
DOCS_ENABLED   # True/False — habilitar /docs y /redoc

# Logger
LOGGER_LEVEL                        # DEBUG|INFO|WARNING|ERROR|CRITICAL
LOGGER_MIDDLEWARE_ENABLED           # True/False
LOGGER_MIDDLEWARE_SHOW_HEADERS      # True/False
LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS # True/False
LOGGER_MIDDLEWARE_SHOW_BODY         # True/False
LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS  # True = path real, False = template
LOGGER_EXCEPTIONS_ENABLED          # True/False
LOGGER_MIDDLEWARE_ERRORS_ONLY      # True/False — True suprime logs normales; errores (4xx/5xx) siempre registran REQUEST+ERROR+RESPONSE

# Database
DB_HOST, DB_USER, DB_PASS, DB_NAME, DB_PORT

# CORS
CORS_ORIGINS   # Orígenes separados por coma. "*" para todos

# Rate Limiting
RATE_LIMIT_DEFAULT        # "100/minute", "10/second", "1000/hour"
RATE_LIMIT_REDIS_ENABLED  # True/False — False = memoria del proceso, True = Redis
RATE_LIMIT_REDIS_URL      # URI de Redis (solo si RATE_LIMIT_REDIS_ENABLED=True)

# Pagination
PAGINATION_MAX_SIZE  # Default 50, hard cap en código: 200

# Request Size
REQUEST_MAX_SIZE_MB  # Default 10
```

### `app/core/versioned_app.py` — Factory de Sub-Apps

```python
def create_versioned_app(
    version: str,
    excluded_request_size_paths: list[str] | None = None
) -> FastAPI:
```

Configura automáticamente en orden de ejecución:
1. `RequestSizeMiddleware` — rechaza requests grandes
2. `CORSMiddleware` — CORS con `CORS_ORIGINS`
3. `ContextMiddleware` — Request ID + ContextVars
4. `LoggerMiddleware` — logging (si `LOGGER_MIDDLEWARE_ENABLED`)
5. `SlowAPIMiddleware` — rate limiting

También registra los 4 handlers de excepciones: `AppHttpException`, `RequestValidationError`, `RateLimitExceeded`, `Exception`.

### `app/core/limiter.py`

Singleton `Limiter` de SlowAPI compartido entre todas las versiones. Importar directamente para usar `@limiter.limit()`.

### `app/utils/response.py`

Estandariza todas las respuestas exitosas con `ApiResponse[T]`.

```python
from app.utils.response import ApiResponse, success, paginated, empty

# Respuesta con datos
return success(data=obj)
return success(data=obj, message="Creado exitosamente")

# Lista paginada
return paginated(items, total=total, pagination=pagination)

# Sin datos (DELETE, acciones void)
return empty("Eliminado exitosamente")
return empty()
```

Los campos `None` se excluyen automáticamente del JSON (via `@model_serializer`). No usar `response_model_exclude_none=True` en cada endpoint.

### `app/utils/pagination.py`

```python
from app.utils.pagination import PaginationDep

@router.get("/", response_model=ApiResponse[list[ItemOut]])
async def list_items(pagination: PaginationDep):
    items = model.find_all(limit=pagination.size, offset=pagination.offset)
    total = model.count()
    return paginated(items, total=total, pagination=pagination)
```

`PaginationDep = Annotated[PaginationParams, Depends(PaginationParams)]`. Query params: `?page=1&size=20`.

### `app/utils/file_upload.py`

```python
from app.utils.file_upload import save_upload, save_uploads

file_info = await save_upload(
    file,
    allowed_types=["image/jpeg", "image/png"],
    max_size_mb=2,
)
file_path = Path(file_info["path"])
try:
    content = file_path.read_bytes()
    # procesar...
finally:
    file_path.unlink(missing_ok=True)  # SIEMPRE eliminar el temporal
```

`uploads/` contiene archivos temporales. Deben eliminarse después de procesar.

### `app/exceptions/`

**`AppHttpException`** — Excepción personalizada que captura automáticamente archivo/función/línea:

```python
from app.exceptions import AppHttpException

raise AppHttpException(
    message="Usuario no encontrado",
    status_code=404,
    context={"user_id": user_id}  # solo visible en development
)
```

**Handlers registrados automáticamente** por `create_versioned_app()`:
- `app_exception_handler` — para `AppHttpException`
- `validation_exception_handler` — para `RequestValidationError` (errores Pydantic)
- `rate_limit_handler` — para `RateLimitExceeded` (SlowAPI 429)
- `generic_exception_handler` — para cualquier `Exception` no controlada

### `app/core/context.py`

ContextVars disponibles en cualquier parte del código durante el ciclo de vida de la request:

```python
from app.core.context import (
    current_http_identifier,  # str — Request ID (16 hex chars)
    current_request_ip,       # str — IP del cliente
    current_request_method,   # str — GET, POST, etc.
    current_request_route,    # str — /users/{user_id}
    current_user_id,          # str | None — para establecer desde auth middleware
)
```

## Flujo de Trabajo: Crear Nueva Feature

### 1. Modelo ORM (si necesita tabla nueva)

```python
# app/models/post.py
from app.models.base import Base, TimestampMixin
from sqlalchemy.orm import Mapped, mapped_column

class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column()
    content: Mapped[str | None] = mapped_column(default=None)
```

Importar en `app/models/__init__.py`:
```python
from app.models.post import Post
__all__ = [..., "Post"]
```

Generar y aplicar migración:
```bash
uv run alembic revision --autogenerate -m "add posts table"
uv run alembic upgrade head
```

### 2. Modelo de Datos (SQL directo)

```python
# app/models/post_model.py
from app.core.database import Database
from app.core.environments import DB_HOST, DB_USER, DB_PASS, DB_NAME, DB_PORT

class PostModel:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def find_by_id(self, post_id: int):
        return self.db.execute_query(
            "SELECT * FROM posts WHERE id = :id",
            {"id": post_id},
            fetchone=True
        )

    def find_all(self, limit: int, offset: int) -> list:
        return self.db.execute_query(
            "SELECT * FROM posts LIMIT :limit OFFSET :offset",
            {"limit": limit, "offset": offset},
            fetchone=False
        )

    def count(self) -> int:
        result = self.db.execute_query(
            "SELECT COUNT(*) as total FROM posts",
            fetchone=True
        )
        return result["total"]

    def create(self, data: dict):
        return self.db.execute_query(
            "INSERT INTO posts (title, content) VALUES (:title, :content)",
            data
        )
```

### 3. Controlador

```python
# app/controllers/post_controller.py
from app.models.post_model import PostModel
from app.exceptions import AppHttpException

class PostController:
    def __init__(self):
        self.post_model = PostModel()

    def get_post(self, post_id: int):
        post = self.post_model.find_by_id(post_id)
        if not post:
            raise AppHttpException("Post no encontrado", 404, {"post_id": post_id})
        return post
```

### 4. Schema Pydantic (opcional pero recomendado)

```python
# app/schemas/post.py
from pydantic import BaseModel, Field

class PostCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str | None = None

class PostOut(BaseModel):
    id: int
    title: str
    content: str | None
    created_at: str

    model_config = {"from_attributes": True}
```

### 5. Routes

```python
# app/routes/v1/posts.py
from fastapi import APIRouter
from app.controllers.post_controller import PostController
from app.schemas.post import PostCreate, PostOut
from app.utils.response import ApiResponse, success, paginated, empty
from app.utils.pagination import PaginationDep

router = APIRouter(prefix="/posts", tags=["Posts"])

@router.get("/", response_model=ApiResponse[list[PostOut]])
async def list_posts(pagination: PaginationDep):
    controller = PostController()
    posts = controller.post_model.find_all(pagination.size, pagination.offset)
    total = controller.post_model.count()
    return paginated(posts, total=total, pagination=pagination)

@router.get("/{post_id}", response_model=ApiResponse[PostOut])
async def get_post(post_id: int):
    return success(data=PostController().get_post(post_id))

@router.post("/", response_model=ApiResponse[PostOut], status_code=201)
async def create_post(post: PostCreate):
    created = PostController().create_post(post.model_dump())
    return success(data=created, message="Post creado exitosamente")

@router.delete("/{post_id}", response_model=ApiResponse[None])
async def delete_post(post_id: int):
    PostController().delete_post(post_id)
    return empty("Post eliminado exitosamente")
```

### 6. Registrar en Router v1

```python
# app/routes/v1/__init__.py
from fastapi import APIRouter
from app.routes.v1.posts import router as posts_router

router = APIRouter()
router.include_router(posts_router)
```

## Patrones y Convenciones

### Formato de Respuestas

**SIEMPRE** usar `ApiResponse[T]` como `response_model` y los helpers `success()`, `paginated()`, `empty()`.

```python
# ✅ Correcto
@router.get("/{id}", response_model=ApiResponse[UserOut])
async def get_user(id: int):
    return success(data=controller.get_user(id))

# ❌ Incorrecto — rompe el formato estándar
@router.get("/{id}")
async def get_user(id: int):
    return {"id": 1, "name": "John"}
```

### Errores

**SIEMPRE** usar `AppHttpException` en vez de `HTTPException`:

```python
# ✅ Correcto
raise AppHttpException("Usuario no encontrado", 404, {"user_id": user_id})

# ❌ Incorrecto
from fastapi import HTTPException
raise HTTPException(status_code=404, detail="Not found")
```

### Rate Limiting por Ruta

```python
from fastapi import Request
from app.core.limiter import limiter

@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, credentials: LoginSchema):
    # request: Request es REQUERIDO para que SlowAPI funcione
    ...
```

### Seguridad SQL

```python
# ✅ SIEMPRE usar parámetros
db.execute_query("SELECT * FROM users WHERE id = :id", {"id": user_id})

# ❌ NUNCA concatenar strings — SQL injection
db.execute_query(f"SELECT * FROM users WHERE id = {user_id}")
```

### Logging con Request ID

```python
from app.core.logger import get_logger
from app.core.context import current_http_identifier

logger = get_logger(__name__)

def some_function():
    request_id = current_http_identifier.get()
    logger.info(f"{request_id} | Operación completada")
```

### File Upload

```python
from fastapi import UploadFile, File
from pathlib import Path
from app.utils.file_upload import save_upload

@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    file_info = await save_upload(
        file,
        allowed_types=["image/jpeg", "image/png"],
        max_size_mb=2,
    )
    file_path = Path(file_info["path"])
    try:
        content = file_path.read_bytes()
        # procesar...
        return success(data={"processed": True})
    finally:
        file_path.unlink(missing_ok=True)  # siempre eliminar
```

## Nombres de Archivos y Clases

- **Modelos ORM**: `app/models/post.py` → clase `Post`
- **Modelos SQL**: `app/models/post_model.py` → clase `PostModel`
- **Controladores**: `app/controllers/post_controller.py` → clase `PostController`
- **Routes**: `app/routes/v1/posts.py` → variable `router`
- **Schemas**: `app/schemas/post.py` → clases `PostCreate`, `PostOut`
- **Clases**: `PascalCase`
- **Funciones/variables**: `snake_case`

## Tecnologías Clave

- **FastAPI** con sub-app mounting para API versioning
- **SQLAlchemy 2.0** — ORM con sintaxis `Mapped[]`, `mapped_column()`
- **Alembic** — migraciones automáticas
- **SlowAPI** — rate limiting por IP
- **Pydantic v2** — validación y serialización
- **uv** — gestor de paquetes ultrarrápido
- **Ruff** — linter y formateador
- **Python 3.13+**

## Comandos Útiles

```bash
# Desarrollo
uv run uvicorn main:app --reload
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8080

# Migraciones
uv run alembic revision --autogenerate -m "descripción"
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic current
uv run alembic history

# Dependencias
uv add <paquete>
uv remove <paquete>
uv sync
```

## Módulo de Migraciones de Blueprints (Plan 02)

Sistema de **migraciones versionadas de blueprints** (`DatabaseModel`): el admin sube
deltas SQL por API y el gateway los aplica/revierte/marca sobre las N bases de datos
gestionadas que replican el blueprint. **NO usa el `alembic/` del gateway**: usa Alembic
como **librería embebida** contra cada BD destino (archivos en `migrations/_shared/`,
distinto de `alembic/`). Guía de uso: `docs/features/model-migrations.md`.

Archivos del módulo:
- **Servicios** (`app/services/db_admin/`):
  - `migrations.py` — `MigrationRunner` (Alembic embebido, advisory lock por BD,
    conexión en AUTOCOMMIT, archivos de revisión en tempdir, dry-run, cuarentena).
  - `sql_dialect.py` — `SqlTranslator` (MySQL→PostgreSQL con sqlglot), `RollbackGenerator`,
    `split_sql_statements`.
  - `migration_integrity.py` — `compute_checksum`, `validate_version` (anti path-traversal),
    `version_sort_key` (orden NUMÉRICO, no lexicográfico).
- **Modelos**: `app/models/model_migration.py` (`ModelMigration`),
  `app/models/database_migration_history.py` (espejo de auditoría) + enum `MigrationStatus`.
- **Controllers**: `model_migration_controller.py` (CRUD del blueprint, NO toca el motor),
  `managed_migration_controller.py` (apply/rollback/stamp/status/history/apply-all, SÍ toca
  el motor).
- **Rutas**: `app/routes/v1/model_migrations.py` + endpoints `/migrations/*` en
  `app/routes/v1/managed_databases.py`.

Gotchas clave: el runner corre en **AUTOCOMMIT** (el advisory lock de sesión sobrevive y
no deja una transacción sin commitear); las versiones se comparan/ordenan **numéricamente**.
Comportamientos (actualizados): `version` al crear es **opcional** → autoasignación secuencial
(`max+1`, con reintento ante colisión); `apply?version=X` aplica en **una llamada** todas las
pendientes hasta X (forward-only); `rollback` es **target-based** (`?confirm_version=` obligatorio
+ `?target_version=` opcional → revierte secuencialmente; valida `down_sql` de todo el camino);
un baseline de snapshot exige aprobación (`reviewed`) antes de aplicar. **Editar/borrar migraciones**:
`PATCH` puede corregir `up_sql`/overrides **solo si no hubo aplicación EXITOSA** (guard
`_has_successful_application`, no `_has_history` — un intento fallido no congela el SQL); al cambiar
`up_sql` hay que reenviar/limpiar los overrides en el mismo PATCH (409 si quedan obsoletos) y se
regenera `down_sql_suggested`. `DELETE` solo borra la **última** versión (la punta) y sin historial.
`stamp` además **saca la BD de cuarentena** (`error → active`). La respuesta de `apply`/
`rollback` es tipada (`MigrationApplyOut`/`MigrationRollbackOut`, con `from_version`→`to_version`).
Verificación e2e contra motores reales (`scripts/verify_migrations_e2e.py`, requiere Docker):
**ejecutada — 153 checks / 0 fallos** (cubre Plan 02 + Plan 09 + UX).

## Módulo de Adopción, Reconciliación y Snapshot (Plan 09)

Puente entre el **plano en vivo** (motor real) y el **inventario** del gateway. Guía de uso:
`docs/features/adoption-reconcile-snapshot.md`; detalle frontend: `docs/api-reference-v3.md`.

- **Endpoints**: `GET /servers/{id}/reconcile` (clasifica managed/unmanaged/orphan),
  `POST /managed-databases/adopt` y `POST /server-users/adopt` (registran objetos preexistentes SIN
  ejecutar DDL; `origin='adopted'`), `GET /servers/{id}/databases/{db}/snapshot` (dump estructural,
  solo estructura), `POST /database-models/from-snapshot` (blueprint baseline desde snapshot).
  `adopt` acepta `model_version` opcional (requiere `model_id`, validada pre-insert): hace **stamp-on-adopt**
  de esa versión en el motor para que el `apply` posterior no reintente crear lo ya existente. Resuelve el
  conflicto "la tabla ya existe" sin inyectar `IF NOT EXISTS` (que enmascararía drift).
- **Adapters** (`app/services/db_admin/`): `dump_structure()` por motor (MySQL `SHOW CREATE *`;
  PostgreSQL `pg_get_*def()` + reflexión `CreateTable`), orden topológico, `_strip_definer_clause`;
  DTOs `StructureDump`/`DumpStatement` en `dtos.py`.
- **Modelos**: `ManagedDatabase.origin`; `ModelMigration.source_engine/is_baseline/has_non_portable/
  reviewed`.
- **Gotchas**: un baseline de snapshot nace `reviewed=False` y `apply` da 409 hasta aprobarlo (R1);
  si trae objetos procedurales queda atado a `source_engine` (cross-engine guard → 422); el snapshot
  es DDL **no confiable** del motor (revisar antes de aplicar en masa).

## Documentación

- `docs/` — documentación completa por feature (ver `docs/features/model-migrations.md`
  para migraciones de blueprints)
- `README_MIGRATIONS.md` — migraciones Alembic de la **BD del gateway** (distinto del módulo de blueprints)
- `readme.md` — instalación y uso general
- FastAPI genera Swagger en `/api/v1/docs` y ReDoc en `/api/v1/redoc`
- Documentación deshabilitada si `DOCS_ENABLED=False`

## Próximos Pasos Comunes

### Autenticación JWT

```bash
uv add python-jose[cryptography] passlib[bcrypt]
```

1. Agregar campos auth al modelo `User`
2. Crear endpoints `/auth/login`, `/auth/register`
3. Crear `AuthMiddleware` que lee JWT y llama `current_user_id.set(user_id)`
4. Registrar middleware en `create_versioned_app()`

### Testing

```bash
uv add --group dev pytest pytest-asyncio httpx
```

Crear `tests/conftest.py` con `TestClient` y fixtures.

### Redis para Rate Limiting Multi-Worker

```python
# app/core/limiter.py
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri="redis://localhost:6379",
)
```

---

**Nota para Agentes**: Mantén consistencia con la arquitectura existente. Todo endpoint debe usar `ApiResponse[T]`. Todo error controlado debe usar `AppHttpException`. Consulta `docs/` para detalles de cada feature.
