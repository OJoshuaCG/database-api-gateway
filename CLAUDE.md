# FastAPI Template - Guía para Agentes de IA

Este documento proporciona contexto y guías para agentes de IA que trabajen en este proyecto.

## Gestión de tareas — OBLIGATORIO ANTES DE TRABAJAR

**Antes de empezar cualquier tarea en este repo, invocá la skill `clickup-task-flow`.** Ahí está
el protocolo completo. Existe para que dos personas no trabajen lo mismo en paralelo, que en un
gateway con credenciales pseudo-root es riesgo operativo, no prolijidad.

- El **detalle** de cada tarea vive en `TODO.md` (raíz). El **estado** y **quién trabaja** viven
  en ClickUp: tarea principal `86e2xzf9d`, lista `901716272178`. Ante discrepancia, para el
  estado gana ClickUp.
- Ciclo ejecutable: **`/tarea P-XX`** para reclamar, **`/tarea fin P-XX`** para cerrar,
  **`/tarea frontend`** para ver lo que espera implementación visual, **`/tarea estado`** para
  ver qué hay en curso.
- Si una tarea está `in progress`, **se interrumpe** y se informa quién la tiene. No se avanza.

**Los estados significan exactamente esto, y solo esto:**

| Estado | Significa |
| --- | --- |
| `to do` | Libre |
| `in progress` | Alguien la está haciendo — **es lo que la reserva** |
| `on hold` | Detenida: trabada por algo externo, **o quedó a medias** |
| `update required` | **Backend listo, PENDIENTE DE FRONTEND.** Nada más |
| `complete` | Cerrada del todo |
| `reviewed` | **No se usa en este flujo** |

**Al terminar hay una bifurcación:** si el cambio necesita implementación visual, la tarea va a
`update required` con un comentario `HANDOFF FRONTEND` (endpoints, breaking changes, schemas, y
el contrato referenciado con su hash de commit) y **no se cierra** — la pasa a `complete` el
frontend. Si no necesita frontend, `complete` directo. Para decir que no lo necesita hay que
poder afirmar que **nada de lo que el frontend ya consume cambió**; ante la duda,
`update required`.

**REGLA DURA — la jerarquía es de DOS niveles:** tarea principal `86e2xzf9d` → subtareas. Las
**sub-subtareas** (tercer nivel) existen para **un solo escenario de emergencia**: el backend
necesita cambiar algo de una tarea que el frontend **está haciendo en ese momento**. **En ningún
otro caso.** No se usan para descomponer trabajo grande (eso va en el detalle del ítem en
`TODO.md`, o como tareas hermanas vinculadas), ni para agrupar, ni para separar backend de frontend
(eso lo hace el estado), ni "para que quede más ordenado". Si aparece un caso que parece
justificarlo, **no lo crees: planteáselo al usuario** y que se establezca como regla nueva.

**Los recorridos, explícitos** (`be` = backend, `fe` = frontend; las transiciones se componen):

```
backend puro              to do → in progress(be) → complete
backend con frontend      to do → in progress(be) → update required → in progress(fe) → complete(fe)
fix cerrado sin frontend  complete → in progress(be) → complete
fix cerrado con frontend  complete → in progress(be) → update required → in progress(fe) → complete(fe)
```

Un fix de algo `complete` que ahora afecta al frontend pasa **por `in progress`** (la reapertura es
lo que reserva la tarea) y termina en `update required`, no en `complete`.

**El frontend también reclama:** al tomar algo en `update required` lo pasa a `in progress` con un
`INICIO` de rol frontend. Por eso **el `INICIO` declara el rol obligatoriamente** — `in progress`
no dice por sí solo si es backend o frontend.

**Si el backend necesita volver a tocar algo ya entregado al frontend, depende de si el frontend
empezó:**

| Estado | Qué hace el backend |
| --- | --- |
| `update required` (el fe no la tomó) | **Reabre la misma tarea:** `HANDOFF INVALIDADO` con `notify_all` antes de tocar código, y al cerrar un handoff de re-entrega con el **delta** (incluyendo **qué NO cambió**, para que no se rehaga trabajo bueno). Si el cambio **no toca el contrato**, se queda en `update required` y solo se comenta. |
| `in progress` con `INICIO` de **frontend** | **No la toca.** Crea una **sub-subtarea** (`parent` = esa subtarea; verificado que ClickUp acepta 3 niveles) + `add_task_link` + comentario `TRABAJO DERIVADO` con `notify_all` en la madre, declarando el **impacto** en lo que el frontend está haciendo. Así nadie se bloquea: el fe no pierde su trabajo y el be no espera. **No hay cuarto nivel.** |

**Un fix de algo ya `complete` NO crea tarea nueva: se reabre la existente.** Tarea nueva solo
cuando el trabajo no se puede describir sin cambiar el objetivo declarado de la original, y en ese
caso se vincula con `clickup_add_task_link`.

**Cuatro reglas que si se saltan rompen el mecanismo en silencio:** (1) las subtareas del backlog
se llaman **`P-XX — <título>`** y las **nuevas** `T-<YYMMDD>-<iniciales>-<slug>` — para algo nuevo
**nunca** el siguiente `P-XX` libre, que es secuencial y dos personas simultáneas calculan el
mismo; (2) toda búsqueda va con **`include_closed: true`** (viene apagado por defecto, y sin él
una tarea ya terminada no aparece y se duplica); (3) después de crear una subtarea se
**re-verifica** que no haya duplicado antes de trabajar, porque la ventana entre buscar y crear no
se puede cerrar (gana la de `date_created` más antiguo); (4) la **identidad (que es el EMAIL de `git config user.email`, NUNCA el nombre: este repo tiene 4 nombres para un mismo email) del ejecutor va dentro
del texto** del comentario, porque el campo "autor" de ClickUp siempre dice la cuenta del token.

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

## Ejecución de tests (pytest) — NO por defecto

**Nunca ejecutes `pytest`** (ni acotado a un archivo/módulo, ni la suite completa) como parte
de tu propio flujo de verificación — ni el agente principal ni ningún subagente delegado. La
suite completa (`tests/`, ~690 tests) tarda varios minutos en este entorno (I/O lento de WSL2
sobre `/mnt/`) y correrla como "chequeo automático" tras cada cambio genera carga real en la
máquina del usuario sin que se haya pedido.

- Corré `pytest` **únicamente** si el usuario lo pide explícitamente en su mensaje (p. ej.
  "corré los tests", "verificá con pytest", "correr la suite", "confirmá que no rompiste
  nada con los tests").
- Si delegás una tarea a un subagente, **no le indiques que corra tests** salvo que el
  usuario lo haya pedido para esa tarea puntual — no lo agregues "por las dudas" ni como
  buena práctica genérica.
- En su lugar, verificá un cambio por otros medios: lectura cuidadosa del diff,
  `python -c "import ast; ast.parse(...)"` o compilación cuando aplique, revisión manual de
  la lógica. Sé explícito en tu respuesta sobre qué quedó **sin verificar** por esta razón,
  para que el usuario decida si quiere correr los tests él mismo o pedírtelo.

## Migraciones del gateway: UN SOLO head, siempre

Esto es sobre el `alembic/` **del gateway** (su propia BD de metadatos), no sobre el módulo
de migraciones de blueprints que viene más abajo.

**Regla: al crear una migración, su `down_revision` debe apuntar al head ACTUAL, y después
del merge tiene que seguir habiendo un solo head.** Verificalo con
`python scripts/check_migration_graph.py` (no necesita BD ni `.env`: lee el fuente con `ast`).

**Por qué es una regla y no una preferencia** (incidente del 2026-08-22, producción caída):
dos ramas crearon una migración cada una colgada del mismo padre `c7d8e9f0a1b2`. Git mergeó
los dos archivos **sin conflicto** —son archivos distintos que nunca se tocan— pero el DAG de
Alembic vive DENTRO del campo `down_revision`, así que quedaron dos puntas.
`alembic upgrade head` es una RESOLUCIÓN DE NOMBRE: con dos candidatos aborta antes de abrir
transacción, el `set -euo pipefail` del `entrypoint.sh` mata el contenedor y el gateway entra
en loop de reinicios. La BD no se corrompe (el fallo es previo a tocarla), pero no arranca.
Este modo de fallo **no lo ve el linter, ni el compilador, ni quien revisa el PR** —cada
migración es correcta por separado— **ni git**. Aparece recién en producción.

**Al arreglar una bifurcación: ENCADENAR, no `alembic merge heads`.** El merge deja head
único pero agrega una revisión vacía y conserva la bifurcación en la historia. Encadenar
(apuntar el `down_revision` de una al `revision` de la otra, y corregir el `Revises:` del
docstring para que no mienta) deja la historia lineal, que es lo que un despliegue directo
desde `main` necesita para ser predecible. Si las dos migraciones son disjuntas el orden es
indiferente; si no, va primero la que deja el esquema en el estado que la otra asume.

**Los `revision` de este repo se eligen A MANO** y con forma secuencial (`d3e4f5a6b7c8`,
`d8e9f0a1b2c3`), no con el hash aleatorio que genera `alembic revision`. Eso hace que dos
personas del mismo día elijan plausiblemente el mismo ID, y ese daño es **peor** que una
bifurcación: una de las dos migraciones queda inalcanzable y su DDL nunca se aplica **sin
que nada falle**. El guard también lo detecta.

Tres barreras, en orden de cuándo actúan: hook `.githooks/pre-push` (se instala una vez por
clon con `git config core.hooksPath .githooks`; se puede saltear con `--no-verify`), workflow
`.github/workflows/migration-graph.yml` (no se puede saltear), y pre-vuelo en
`docker/scripts/entrypoint.sh` (última red: convierte el mensaje opaco de Alembic en uno que
nombra las revisiones en conflicto y aclara que la BD no se tocó).

El guard **no usa `alembic.script.ScriptDirectory`** a propósito, y el motivo está medido:
`ScriptDirectory` **importa** los archivos de migración, así que su veredicto sale del
`__pycache__` cuando el bytecode está rancio. Reproducido durante la implementación: se editó
un `down_revision` a un valor de la misma longitud y se restauró dentro del mismo segundo;
CPython invalida bytecode comparando mtime (granularidad de 1 s) y tamaño, los dos
coincidieron, y Alembic dictaminó sobre una cadena que ya no estaba en el archivo. Para un
guard eso falla en la dirección PELIGROSA —aprobar un árbol roto— y justo después de un
rebase o un cambio de rama, que son las operaciones que producen las bifurcaciones. De ahí
que lea el fuente con `ast`.

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
    `split_sql_statements` (ver "Cuerpos procedurales" abajo).
  - `migration_integrity.py` — `compute_checksum`, `validate_version` (anti path-traversal),
    `version_sort_key` (orden NUMÉRICO, no lexicográfico).
  - `plan_integrity.py` — PURO: cierre de dependencias de una selección parcial
    (`expand_selection`/`check_closure`/`prune_unsatisfied`) + linter de invariantes del plan
    (`validate_statement_plan`). Lo consume schema-comparisons (adopt/execute).
- **Modelos**: `app/models/model_migration.py` (`ModelMigration`),
  `app/models/model_migration_statement.py` (`ModelMigrationStatement` — MANIFIESTO de
  sentencias con reverso emparejado, ver abajo), `app/models/database_migration_history.py`
  (espejo de auditoría) + enum `MigrationStatus`.
- **Controllers**: `model_migration_controller.py` (CRUD del blueprint, NO toca el motor),
  `managed_migration_controller.py` (apply/rollback/stamp/status/history/apply-all, SÍ toca
  el motor).
- **Rutas**: `app/routes/v1/model_migrations.py` + endpoints `/migrations/*` en
  `app/routes/v1/managed_databases.py`.

**Cuerpos procedurales en `split_sql_statements`** (fix): el `up_sql` de una migración se
vuelve a PARTIR por `;` para generar un `op.execute` por sentencia. Un
`CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT` con cuerpo `BEGIN…END` se cortaba en su primer
`;` interno (normalmente el `DECLARE`) → el motor recibía SQL truncado y respondía
`(1064, "…syntax… near '' at line N")`. Golpeaba sobre todo a la Opción A de
schema-comparisons (concatena los ítems con `;`); la Opción B nunca lo tuvo (no re-parte lo
ya renderizado). Ahora se reconocen dos vías: (1) la directiva **`DELIMITER <tok>`** (la de
`mysqldump` y la que ya emitía el `export`), consumida como directiva de CLIENTE y jamás
enviada al motor; (2) **conteo de bloques `BEGIN…END`**, activo **solo** dentro de una
sentencia que abre una rutina (`_ROUTINE_START_RE`, acepta `DEFINER=` con backticks o
comillas) — fuera de ahí un `BEGIN` es una TRANSACCIÓN y contarlo pegaría todo el script.
Sutileza del conteo: aperturas **solo** `BEGIN` y `CASE`; los cierres con sufijo
(`END IF`/`END WHILE`/`END LOOP`/`END REPEAT`) se neutralizan y `END CASE` + el `END` a
secas cierran. Así se equilibran tanto el `CASE` *statement* como el `CASE` *expresión* de
un `SELECT` (`… END`, que cerraría un bloque de más), y se esquiva `IF`/`REPEAT`, que son
**funciones** (`IF(a,b,c)`, `REPEAT('x',3)`) además de statements — por eso NO se cuentan.
**No era exclusivo de MySQL**: PostgreSQL estaba a salvo en sus funciones `plpgsql` por el
dollar-quoting, pero los cuerpos `BEGIN ATOMIC` de SQL/PSM (PG 14+) no lo llevan y también
se partían; ahora los cubre la vía 2. Un cambio de split invalida los checkpoints viejos,
pero eso ya estaba cubierto: `_resolve_resume_offset` compara `total_statements` y responde
409 en vez de reanudar a ciegas. `migration_progress.is_resumable` sigue excluyendo las
migraciones con rutinas — ahora por prudencia, no porque el split sea incorrecto.

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

**La contabilidad INTERNA del gateway NO es esquema del usuario (fix de producción,
2026-07-27)**: `identifiers.GATEWAY_TABLE_PREFIXES = ("_gw_v_", "_gw_stg_")` +
`is_gateway_internal_table`/`exclude_gateway_internal_tables`/`references_gateway_internal_table`.
CAUSA RAÍZ del incidente: los CUATRO caminos que enumeran tablas
(`base_adapter.structural_snapshot`, `list_tables`, `list_table_stats`, y `dump_structure` de
MySQL y PG) incluían `_gw_v_{slug}` —la tabla de versión de Alembic que el gateway crea DENTRO
de cada BD gestionada—. Al comparar una BD origen (sin ella) contra una gestionada destino (con
ella), el diff la veía "en target y no en source" → emitía **`DROP TABLE _gw_v_{slug}`**.
Adoptado como versión y aplicado: Alembic leía la versión actual OK, ejecutaba TODO el DDL
(incluido el DROP de su propia contabilidad) y moría al registrar la versión nueva con
`(1146, "Table '..._gw_v_...' doesn't exist")` — BD con los cambios aplicados pero SIN puntero
de versión, y en cuarentena. **La auto-reconciliación NO lo detecta**: el fallo ocurre en la
contabilidad de Alembic, no en una sentencia, así que el checkpoint queda `last == total` →
no hay "aplicación parcial" → `on_failure=auto` no dispara. El caso SIMÉTRICO es igual de
grave: origen gestionado + destino sin gestionar → `CREATE TABLE _gw_v_{slug_origen}` inyecta
una tabla de versión ajena. FIX en 3 capas: (1) los 4 snapshots excluyen esos prefijos;
(2) `create_migration`/`update_migration` **rechazan (422)** SQL que los nombre (ninguna
migración tiene motivo legítimo para tocarlos); (3) `_guard_gateway_internal_sql` **bloquea
(409)** el `apply`/`apply_all` de versiones creadas ANTES del fix, nombrando las ofensoras —
sin esto, aplicar la versión mala a OTRA BD del blueprint repetía el fallo.
`version_table_name` toma el prefijo de `GATEWAY_TABLE_PREFIXES` para que el nombre que se
CREA y el que se EXCLUYE no puedan divergir (test de invariante). `gw_ou_{hash}` (función +
trigger de PG que emula `ON UPDATE CURRENT_TIMESTAMP`) NO se excluye a propósito: implementa
el comportamiento de una COLUMNA del usuario, y filtrarlo haría que el diff lo recreara en
cada corrida. RECUPERACIÓN de una BD ya afectada: `PATCH` la versión para quitar esas
sentencias (permitido: la aplicación falló, no fue exitosa) → `stamp` en la versión que la BD
tiene FÍSICAMENTE aplicada (Alembic recrea la tabla de versión y sale de cuarentena).

**PostgreSQL: DDL TRANSACCIONAL — el estado parcial no existe** (`MigrationRunner.use_transactional_ddl`):
diferencia de motor más importante del módulo. PG ejecuta DDL transaccional, así que una
migración que falla en la sentencia 10 de 50 **se deshace sola** y el ledger nunca divergió del
plano físico. El `env.py` compartido SIEMPRE pidió `transaction_per_migration=True`, pero el
runner forzaba AUTOCOMMIT y lo anulaba — PG sufría un problema que no le corresponde. Ahora:
PG → conexión transaccional + advisory lock en **otra sesión** (`advisory_lock`; los locks de
SESIÓN sobreviven COMMIT/ROLLBACK, los de transacción serían `pg_advisory_xact_lock`) +
**checkpoint DESACTIVADO** (no es optimización sino CORRECCIÓN: el checkpoint se graba en la BD
del gateway con su propio commit; si la tx del destino se revierte, afirmaría "10 aplicadas"
sobre una BD virgen). MySQL/MariaDB → AUTOCOMMIT (commit implícito en cada DDL, atomicidad
imposible) + checkpoint. Se cae a AUTOCOMMIT si ALGUNA migración trae algo que PG no admite en
tx (`CREATE/DROP INDEX CONCURRENTLY`, `VACUUM`, `ALTER SYSTEM`, `CREATE/DROP DATABASE`,
`ALTER TYPE … ADD VALUE` — este último aunque PG12+ lo permita: el valor nuevo no se puede USAR
en la misma tx). `_read_current` commitea la tx implícita del SELECT (si no, Alembic no puede
abrir la suya). Verificado con SQLite hecho transaccional a propósito (el driver pysqlite NO
emite BEGIN para DDL por default → SQLite normal NO sirve como proxy de PG).

**Auto-protección ante un fallo** (`apply?on_failure=auto|reconcile|leave`, default `auto`):
solo aplica a MySQL/MariaDB. `auto` deshace lo aplicado SOLO si puede deshacerlo todo → la BD
vuelve limpia a su versión anterior y **NO queda en cuarentena**. La respuesta trae
`reconciliation`. **ANTI-PATRÓN que esto elimina**: `stamp --force` a la versión que falló +
`rollback`. El stamp AFIRMA que las 50 sentencias corrieron, así que el rollback ejecuta 50
reversos contra 10 cambios reales → los 40 restantes fallan (`doesn't exist`) y queda un TERCER
estado inconsistente; encima `force` descarta el checkpoint. El 409 de
`_guard_partial_checkpoint` ahora lo explica y ordena las vías correctas.

**Traducción MySQL→PostgreSQL (fix)**: sqlglot transpila bien expresiones/tipos pero emitía
VERBATIM el DDL de MySQL al escribir PG. Verificado invocando el transpilador real:
`DROP INDEX i ON t` → `DROP INDEX "i" ON "t"` (PG no acepta `ON`); `DROP FOREIGN KEY`/
`DROP INDEX`/`DROP CHECK` → se quedaban así (PG usa `DROP CONSTRAINT`), y `DROP CHECK` incluso
dejaba los **backticks**. Las 4 tienen reescritura exacta (`_rewrite_pg_statement`, aplicada
**por sentencia y con contexto de `ALTER TABLE`** — al script completo la 2ª pisaba a la 1ª).
`MODIFY COLUMN`/`CHANGE COLUMN`/`DROP PRIMARY KEY`/`ENGINE=`/`AUTO_INCREMENT=` NO son
traducibles → `_guard_untranslatable_sql` responde **422 antes de tocar el motor** pidiendo
`up_sql_postgresql`. Que `translate` devolviera `None` NO era defensa: `select_up_sql` caía al
`up_sql` base en MySQL crudo, igual de inválido. Transpilado **memoizado** (`lru_cache`, 80ms →
0.02ms) + pre-filtro regex sobre el SQL crudo en el guard. `apply_all` ahora SÍ corre
`_guard_cross_engine` y este guard (antes no corría ninguno: lote heterogéneo sin validar).

**`serial` de PostgreSQL (fix)**: la secuencia que respalda un `serial` está POSEÍDA por la
columna (`pg_depend.deptype='a'`) y el snapshot la excluye A PROPÓSITO, pero
`_render_column_def` emitía `DEFAULT nextval('t_id_seq'::regclass)` → `relation "t_id_seq"
does not exist` en el primer CREATE TABLE. **Rompía el clon PG→PG de cualquier tabla con
`id serial primary key`.** Fix: `PostgresAdapter._serial_type` rendea `SERIAL`/`BIGSERIAL`/
`SMALLSERIAL` (crea la secuencia, la asocia y fija el default en un paso). Solo para columnas
NOT NULL: `serial` implica NOT NULL, y una columna nullable con `nextval` (creada a mano, muy
inusual) queda con el default crudo — límite conocido y acotado.

**`snapshot_layout` — el OTRO camino de creación de versiones (fix)**: `order_statements`
ordenaba las clases no-tabla **ALFABÉTICAMENTE**, así que una `v_alpha` que lee de `v_zeta`
salía primero y el baseline fallaba con 1146/42P01 — el mismo bug que en el diff, en el otro
camino. Ahora los objetos con cuerpo se ordenan por dependencia real
(`_order_by_body_references`, reusa `_referenced_identifiers` de `schema_diff` + el
`depends_on` firme del dump). Además se reordenó `_CLASS_ORDER`: `routine` ANTES de
`view`/`matview` (una vista puede llamar una función y PG la valida al crearla) e `index`
DESPUÉS de `materialized_view` (en PG `pg_indexes` incluye los índices de las MATVIEWS y el
dump los emite como `object_type='index'` → `CREATE INDEX … ON mi_matview` antes de la
matview). Pendiente en este camino: `_persist_snapshot_versions` no escribe manifiesto (un
baseline que falla a mitad no es reconciliable) y `filter_statements` no valida cierre
(excluir `type` deja un baseline inaplicable en silencio).

**Endurecimiento de bordes de la reconciliación (2ª pasada, 2026-07-26)**: (1) el checkpoint
de `reconcile_partial` se decrementa recién cuando TODOS los reversos de un `seq` terminaron —
un reverso multi-sentencia (redefinición: `DROP nuevo; CREATE viejo`) aporta DOS entradas con
el mismo `seq`, y decrementar tras la primera afirmaba "sentencia deshecha" a medias (un
reintento saltearía la mitad restante en silencio). (2) Guards de dirección CRUZADA:
`_guard_partial_down_before_apply` (409: `apply` con un ROLLBACK a medio ejecutar congelaría
la versión N a medio deshacer — el ledger sigue en N y "no hay pendientes"; la salida es
reintentar el rollback, que retoma del checkpoint `down`); el guard de `stamp` ahora mira
AMBAS direcciones y `force` limpia ambas. (3) Rutinas invocadas por DDL de tabla
(PostgreSQL: `DEFAULT next_id()`, `CHECK (fn(x))`, GENERATED con función IMMUTABLE — MySQL no
lo permite): se ADELANTAN al paso 26 (`_STEP_ROUTINE_PREREQ` + `_table_ddl_routine_deps`, con
arista para el cierre de selección) — antes el CREATE TABLE moría con 42883 porque las
rutinas van en el paso 80. Exclusión de dependencia MUTUA fail-closed: si el cuerpo de la
rutina consulta una tabla del diff, NO se adelanta (una función SQL-language que consulta la
tabla se valida al crearse). Mismo hoist en `snapshot_layout._prereq_routines` para el
baseline. Un falso positivo del escaneo es inocuo (crear antes una rutina que no toca tablas
no rompe nada).

**Manifiesto de sentencias + reconciliación de una aplicación PARCIAL** (`model_migration_statements`,
tabla nueva; migración `e2f3a4b5c6d7`): cierra el agujero más grave del rollback. Alembic
escribe la versión en `_gw_v_{slug}` recién al TERMINAR el `upgrade()`, así que un `apply` que
muere en la sentencia k de N deja el ledger en N-1 y la BD con k sentencias de N ya commiteadas
(AUTOCOMMIT). `rollback` no lo veía: leía `current=N-1` y ejecutaba el `down_sql` de N-1 contra
una BD contaminada con parte de N. Con los blobs `up_sql`/`down_sql` era **ininferible** qué
deshacer (el down es una secuencia INDEPENDIENTE, con otra cantidad de sentencias — los cambios
sin reverso no aparecen). Ahora una fila por sentencia con su reverso EMPAREJADO y `seq` ==
índice del checkpoint. Piezas: guard **ROB2** (`rollback` → 409 con aplicación parcial pendiente);
`GET /migrations/status` informa `has_partial_application` + `reconcilable`;
`POST /managed-databases/{id}/migrations/reconcile-partial` (`confirm_version` obligatorio,
`dry_run`, `force`) ejecuta el reverso de las k sentencias en orden INVERSO. **NO es un
`downgrade`**: la versión nunca se aplicó, así que NO se toca la tabla de versión — es una
COMPENSACIÓN que devuelve el plano físico al estado que el ledger ya afirma. El checkpoint se
DECREMENTA tras cada reverso (si la reconciliación falla a mitad, retoma). Tres barreras
fail-closed sobre el manifiesto: mismo motor que el destino; concatenarlo debe reproducir
EXACTO el `up_sql` (igualdad sin splitter); el `PATCH` que cambia el SQL lo BORRA. Solo lo
escribe `adopt` de schema-comparisons; sin manifiesto → 409 con motivo, nunca a ciegas.
Efecto secundario: con manifiesto NO hay splitter, así que `is_resumable(..., manifest_pinned=True)`
habilita resumir cuerpos procedurales (las exclusiones por estado de sesión y `kind='data'`
siguen SIEMPRE). Pendiente: `create_from_snapshot` no escribe manifiesto todavía.

**Checkpoint de sentencia (resume automático tras fallo parcial)**: como el DDL no es
transaccional en MySQL/MariaDB (AUTOCOMMIT), una migración de N sentencias que falla a
mitad (p. ej. 3 de 50) dejaba antes solo dos salidas: reintentar desde cero (choca "ya
existe" en lo que sí se creó) o `stamp` a ciegas (riesgo real: un `rollback` posterior
ejecutaría el `down_sql` completo contra una BD con solo una fracción de los cambios
físicos). Fix: `app/services/db_admin/migration_progress.py` +
`migration_statement_progress` (tabla nueva, efímera — una fila solo mientras hay
progreso incompleto) graban qué sentencia fue la última exitosa; el próximo
`apply`/`rollback` retoma ahí (`migrations.py::_write_revision_files`/`_apply_one`/
`rollback_to`), sin re-ejecutar lo ya commiteado. **Fail-closed por diseño** (revisado
con `gateway-senior-python`/`gateway-db-dialects` antes de implementar, no solo
código): el checkpoint se **deshabilita** (todo-o-nada, como antes) si la migración es
`kind='data'`, tiene `has_non_portable=True`, o contiene sentencias de **estado de
sesión** (`SET`, `PREPARE`, `LOCK TABLES`, `USE`, transacciones explícitas) — el
splitter no entiende `BEGIN...END` de MySQL/MariaDB y una conexión nueva en el resume
pierde ese estado (`SET FOREIGN_KEY_CHECKS=0` de la sentencia 1 no sobrevive a un
resume que arranca en la 4). El checkpoint queda ligado al `checksum` de la migración:
editar `up_sql` con un checkpoint incompleto → **409** (tanto en `PATCH` de la
migración como en `stamp`, salvo `force=true`, que descarta el checkpoint y audita).
**Límite irreducible** (no lo resuelve ningún diseño de checkpoint): si la sentencia
que falló está genuinamente rota, o un `ALTER` multi-cláusula quedó parcialmente
aplicado por el motor, el resume solo evita re-tropezar con las sentencias previas —
la rota vuelve a fallar y sigue requiriendo fix-forward o reconciliación manual.
**No verificado contra motor real** (sin Docker/MySQL disponibles al implementarlo) ni
con `pytest` — solo `ast.parse`/compilación del codegen y ejecución directa de las
funciones puras. Pendiente antes de confiar en producción: ciclo apply-parcial→retoma
contra los 3 motores reales, y la migración Alembic nueva
(`d1e2f3a4b5c6_add_migration_statement_progress`) contra la BD del gateway real.

**`:` LITERAL en el DDL roto como bind param (fix, 2026-07-29)**: el codegen del archivo de
revisión Alembic (`migrations.py::_render_statement_calls`) emitía `op.execute({stmt!r})` con
un str PLANO. `op.execute` con string envuelve el SQL en `sqlalchemy.text()`, que interpreta
`:nombre` como BIND PARAM → un `:` LITERAL en el DDL (JSON de ejemplo en un `COMMENT` como
`{"discount_pct":15}`, o `::` de PostgreSQL) reventaba en tiempo de COMPILACIÓN con
`A value is required for bind parameter '15'` (el SQL nunca llegaba al motor). El camino
ad-hoc (`execute_adhoc`) YA lo evitaba con `exec_driver_sql`+`_escape_percent`, pero nunca se
aplicó al camino de Alembic. Trade-off opuesto entre caminos: `op.execute(str)`/`text()` rompe
con `:` pero el `%` es inofensivo; `exec_driver_sql` es seguro con `:`/`::` pero hay que
escapar `%`→`%%`. FIX: el codegen ahora emite `op.get_bind().exec_driver_sql(<stmt con %
escapado>)` — mismo criterio que `execute_adhoc`, cubre `up_sql` y `down_sql` (misma función),
y beneficia a TODOS los blueprints con `:` en su SQL. NO toca el `up_sql` almacenado →
checkpoints/checksums/resume intactos. RECUPERACIÓN de un blueprint afectado: el SQL guardado
es válido (no hay que hacer PATCH); como las tablas usan `CREATE TABLE IF NOT EXISTS`, re-lanzar
`apply` tras desplegar el fix es idempotente. Verificado con script puntual (reproducción del
bug + codegen + ciclo `command.upgrade` real contra SQLite) — no con `pytest`.

**`DELIMITER $$` leído como dollar-quoting de PostgreSQL (fix, 2026-07-30)**: `split_sql_statements`
procesaba la rama de dollar-quoting (`$tag$…$tag$`) ANTES del chequeo del terminador, así que con
`DELIMITER $$` activo el token `$$` se leía como apertura de un literal y se "cerraba" en el `$$`
**siguiente** → dos sentencias pegadas en una
(`DROP PROCEDURE IF EXISTS \`sp\`$$\n\nCREATE PROCEDURE \`sp\` (…`) que el motor rechaza con
`(1064, "…syntax… near '$$\n\nCREATE PROCEDURE …'")`. Con `//` (o `;;`, o `|`) nunca pasó: no
colisionan con nada del scanner — de ahí que el módulo se hubiera probado solo con `//`. El test
que sí usaba `$$` pasaba **por casualidad**: tenía UN solo `$$` de cierre en todo el script, así
que no había par que emparejar; el bug aparece desde el segundo `$$` (es decir, en cualquier
blueprint con más de una rutina, o con `DROP PROCEDURE IF EXISTS …$$` + `CREATE PROCEDURE …$$`).
FIX: mientras un `DELIMITER` haya fijado un terminador ≠ `;`, un token que arranca con `$` y
coincide con el terminador NO se trata como dollar-quote y cae al chequeo de fin de sentencia. El
dollar-quoting de PostgreSQL queda intacto porque ahí el delimitador sigue siendo `;` (tests de
`$$`/`$body$`/`DO $$`/`::` verificados). Dos bugs hermanos del mismo scanner, corregidos en el
mismo paso: (1) tanto la directiva `DELIMITER` como el reconocimiento de `CREATE PROCEDURE`
exigían el buffer **vacío**, así que un comentario previo (lo normal en un dump o en SQL escrito
a mano) hacía que `DELIMITER` viajara al motor (1064) y que el conteo de `BEGIN…END` no se
activara (cuerpo partido en su primer `;`) → ahora ambos toleran blancos/comentarios previos
(`_only_noise`); (2) una "sentencia" de puros comentarios (típico: comentarios al pie tras el
último terminador) se emitía y el motor la rechazaba con `(1065, 'Query was empty')` → ahora se
descarta. **Reparto por motor**: el `$$` como terminador es de MySQL/MariaDB (ahí estaba el
bug); PostgreSQL no usa `DELIMITER` y su dollar-quoting queda intacto (verificado: varias
funciones `plpgsql` con `$$`/`$body$` en un mismo script, tags anidados, `DO $$`, `::`, `%`) —
lo que PG SÍ gana son los dos bugs hermanos, porque un comentario antes de un
`CREATE FUNCTION … BEGIN ATOMIC` (SQL/PSM, sin dollar-quoting) le partía el cuerpo igual que a
MySQL. LÍMITE CONOCIDO: un script no puede usar `$$` como terminador y como dollar-quoting a la
vez (token ambiguo, gana el terminador); no es práctico porque `DELIMITER` no existe en
PostgreSQL — y antes del fix ese caso solo "andaba" con UN objeto en el script. NO toca el
`up_sql` almacenado (checksums/checkpoints intactos) y, como el patrón
`DROP PROCEDURE IF EXISTS` + `CREATE PROCEDURE` es idempotente, re-lanzar `apply` tras desplegar
alcanza; si el fallo dejó la BD en cuarentena (`status=error`, lo esperable: una migración
procedural no es resumible → sin checkpoint → la auto-reconciliación no dispara), el `apply` de
recuperación va con `force=true` y al terminar OK la devuelve a `active`. Tests en
`tests/test_sql_dialect.py`; verificado además el pipeline completo split→codegen→`exec_driver_sql`
(sin `pytest`: ejecución directa de las funciones, 37/37).

**Captura de RESULTADOS de `SELECT` dentro de una migración (P0, 2026-08-14)**: una migración
que verifica algo con un `SELECT` (filas sin backfill, duplicados que bloquean un UNIQUE)
ejecutaba la consulta y **tiraba el resultado** — Alembic no devuelve nada y el gateway solo
informaba "aplicada/falló", justo cuando lo que se quería mirar ya cambió. FIX: tabla nueva
`migration_select_results` (migración `f8a9b0c1d2e3`) + columna
`model_migrations.capture_selects` + módulo `app/services/db_admin/migration_results.py` +
hook en `migrations.py::_render_statement_calls` (emite
`migration_results.capture_statement(...)` en lugar de `exec_driver_sql` para las sentencias de
lectura). **PRIMERA excepción deliberada** a la regla "el gateway nunca almacena datos de
negocio" (`app/models/audit_log.py`), y por eso lleva TODAS las salvaguardas juntas: opt-in por
versión (`capture_selects`, default false) + `reviewed=false` obligatorio al activarlo (409 en
`apply` hasta aprobar, mismo mecanismo que el gate R1 de los baselines) + consentimiento por
corrida (`apply?allow_result_capture=true`, 409 antes de tocar el motor — un blueprint se
replica sobre N BDs de dueños distintos y quien aplica sobre UNA debe saber que va a extraer
filas de ESA base) + payload **cifrado con la DEK** (no legible por SQL directo contra la BD del
gateway: todo acceso pasa por el endpoint auditado) + lectura auditada **fail-closed ANTES de
descifrar** (criterio de `reveal_password`) + TTL (`MIGRATION_CAPTURE_TTL_HOURS=168`, purga en
el `lifespan`) + kill switch `MIGRATION_CAPTURE_ENABLED`. Endpoints
`GET`/`DELETE /managed-databases/{id}/migrations/{version}/select-results`; `apply`/`rollback`
solo devuelven PUNTEROS (`captured_select_count`, `select_results_available`) — nunca las filas,
que con `LOGGER_MIDDLEWARE_SHOW_BODY=true` irían al log. **PERSISTENCIA HÍBRIDA por motor**,
decidida por `use_transactional_ddl` (no se reimplementa el criterio): MySQL/MariaDB
(AUTOCOMMIT) escriben cada captura DE INMEDIATO con una sesión corta propia →
`durability='committed'`; PostgreSQL (transaccional) las ACUMULA en un buffer en memoria y las
vuelca cuando `command.upgrade/downgrade` retorna o lanza → `committed`/`rolled_back`. El buffer
de PG no es optimización: escribir en la BD del gateway con la transacción del destino abierta
deja esa conexión `idle in transaction` y el `idle_in_transaction_session_timeout` (15s en
`remote_engine`) puede ABORTAR la migración por una escritura lenta a la BD de metadatos. Tres
invariantes que NO se pueden romper (con test cada uno): (1) con `capture_selects=false` el
archivo de revisión generado es **byte a byte** el histórico; (2) la captura **no reescribe el
SQL** —a diferencia de la consola SQL, que empuja `LIMIT` con `_limited_sql`— porque el texto
debe coincidir con el `checksum`, así que el tope se aplica al capturar
(`fetchmany(max_rows+1)` para marcar `truncated` con certeza); (3) **no toca
`statement_lists`/`total_statements`**, o `_resolve_resume_offset` dispararía un 409 espurio.
Orden por sentencia: ejecutar → capturar (best-effort: un fallo de captura da `status='error'`
con motivo acotado, NUNCA `str(exc)` del motor —criterio R4—, y la migración sigue) →
`record_statement` sin cambios. La compuerta es `is_capturable` (pre-filtro regex barato +
`query_policy.classify_statement`, AST-first): acá un veredicto `blocked` significa SOLO "no
capturar", **nunca** "rechazar la migración" (un `GRANT`/`SET FOREIGN_KEY_CHECKS=0` en un
blueprint se sigue ejecutando igual). Módulo SEPARADO de `migration_progress` a propósito: ese
es maquinaria fail-closed que decide si resumir es seguro, este es un informe best-effort para
un humano. `query_runner._json_value` se extrajo a `value_json.py` (serializador compartido:
`Decimal`→str, `timedelta` de MySQL como HH:MM:SS, BLOB en hex, topes de profundidad/cardinalidad)
en vez de escribir un segundo criterio. El `PATCH` que cambia `up_sql` PURGA las capturas en la
misma transacción que ya borra el manifiesto. Verificado: 41 casos por ejecución directa (sin
`pytest`) + ciclo `command.upgrade` REAL contra SQLite con captura activa (persistida cifrada y
descifrada de vuelta) + `upgrade/downgrade/upgrade` de la migración en SQLite + `alembic check`
sin drift para la tabla nueva. **PENDIENTE e2e contra motores reales** (sin Docker): buffer de PG
vs `idle_in_transaction_session_timeout` y marca `rolled_back`, captura inmediata en
MySQL/MariaDB sobreviviendo a un fallo posterior, tipos nativos (`JSONB`, `TIME`, `bytea`), y la
migración contra la BD del gateway real. **`.env.example` NO se pudo actualizar** (permisos del
entorno): las 6 variables `MIGRATION_CAPTURE_*` están documentadas en
`app/core/environments.py` y en `docs/features/model-migrations.md`.

**Tres bloqueantes de la revisión de seguridad de esa captura (2026-08-14)**:
**(B1) el `rollback` capturaba SIN ningún control.** El codegen emite `capture_statement`
también para las `down_statements` (`_render_revision` recibe UN solo flag `capture` y lo
aplica a los dos cuerpos), pero `rollback` no llamaba a ninguno de los guards que sí exige
`apply` → bastaba un `confirm_version` para extraer y persistir filas sin consentimiento.
Peor, había un camino completo: crear versión con `capture_selects=true` (nace
`reviewed=false`) → `stamp` a esa versión (sin gate alguno) → `rollback` sobre ella. FIX:
`rollback` acepta `allow_result_capture` (mismo patrón que `apply`) y llama
`_guard_reviewed_capture` + `_guard_capture_consent` ANTES de la auditoría de intento y del
runner. `_guard_reviewed_capture` ganó `migration_ids` para acotar el chequeo **al camino a
revertir**, NO al blueprint completo: el rollback es la vía de RECUPERACIÓN ante una migración
mala y bloquearlo por una versión futura sin revisar —que no se va a ejecutar— le quitaría al
operador su única salida (`apply` quedó mirando todo el blueprint en esta pasada; se corrigió
después, ver "Segunda pasada adversarial" más abajo).
`_guard_capture_consent` ganó `direction` (solo cambia el texto del 409: un mensaje que dice
"aplicación" manda al operador al endpoint equivocado). Defensa en profundidad:
`_guard_stamp_unreviewed_capture` da **409** al marcar una versión con captura sin revisar
(el stamp no ejecuta SQL, pero es lo que HABILITA su rollback), con `force=true` como escape
—necesario: una versión aplicada hace meses a la que después se le activó la captura queda
`reviewed=false`, y una BD que perdió su puntero de versión debe poder re-stampearla—.
**(B2) `reviewed=true` sobrevivía un cambio TOTAL del SQL.** Aprobar `SELECT 1` → `PATCH
up_sql='SELECT * FROM clientes'` (permitido: `_has_successful_application` es False mientras
no se aplicó con éxito) → `apply` pasaba el gate sin que nadie hubiera visto la consulta real.
FIX en `update_migration`: con `capture_selects` activo (el actual o el que se setea en el
mismo PATCH), cambiar `up_sql`/overrides/**`down_sql`** fuerza `reviewed=False` y se audita el
motivo. El `down_sql` va en una variable APARTE (`down_sql_changing`), **no** dentro de
`sql_fields_changing`: meterlo ahí habría bloqueado con 409 el flujo documentado de confirmar
el rollback DESPUÉS de aplicar (que es exactamente lo que pide el 409 de `rollback`) y habría
purgado el manifiesto sin motivo. El reset GANA sobre un `reviewed=true` enviado en la misma
llamada (mismo criterio que `capture_enabled_now`).
**(B3) el TTL solo purgaba en el arranque.** La purga vivía suelta en el `lifespan`, así que en
un proceso que corre semanas —lo normal— `MIGRATION_CAPTURE_TTL_HOURS` nunca volvía a purgar:
la retención de los ÚNICOS datos de negocio que el gateway guarda era una promesa falsa. FIX:
`main.py::_purge_captures_periodically`, tarea `asyncio` creada en el `lifespan` con
`MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES` (nueva, default 60; `0` desactiva). Corre con
`asyncio.to_thread` porque `purge_expired` es I/O SÍNCRONO y en el event loop bloquearía todas
las requests; se `cancel()` + `await` en el apagado (sin el await queda el warning "Task was
destroyed but it is pending"); un fallo de una pasada no mata el bucle. Verificado por
ejecución directa (43 checks) + HTTP con TestClient/SQLite para B2 (12 checks), **sin
`pytest`**; tests agregados en `tests/test_migration_results.py` y
`tests/test_api_model_migrations.py` (no ejecutados con `pytest`, política del proyecto).

**Segunda pasada adversarial de CORRECTITUD sobre esa captura (2026-08-14)** — 2 bloqueantes
verificados empíricamente + 4 defectos menores.
**(C1) un `SELECT` precedido por un COMENTARIO nunca se capturaba, en SILENCIO.** El
pre-filtro de `is_capturable` era `^\s*(select|with|table|values)`: solo toleraba BLANCOS. Pero
`split_sql_statements` **CONSERVA los comentarios dentro** de la sentencia que emite (solo
descarta las que son SOLO comentarios), y una sentencia de verificación real casi siempre lleva
delante el comentario que explica qué verifica → la feature **no funcionaba en su caso de uso
más común**. Agravante: el endpoint de lectura deriva `expected_indexes` con la MISMA función,
así que `missing_indexes` salía vacío y la respuesta mostraba `items: []` sin error ni aviso.
FIX: `sql_dialect.strip_leading_noise(text, hash_is_comment=)` (público, reusa el criterio de
comentarios del splitter — `_only_noise` ahora se define sobre ella, una sola implementación) +
`migration_results.executable_head`, y el pre-filtro se aplica a ese head. `#` es comentario
**solo en MySQL/MariaDB**: en PostgreSQL es el XOR de enteros y saltearlo borraría código
ejecutable (mismo matiz que `query_policy._scan_normalize`, por eso la función necesita el
motor). El clasificador AST recibe el SQL COMPLETO —sqlglot parsea comentarios sin problema
(verificado)— y el texto que se ejecuta/hashea no se altera. Comentario de bloque sin cerrar ⇒
no se captura (fail-closed).
**(C2) el kill switch global NO neutralizaba el gate de `reviewed`, y bloqueaba el `rollback`
sin salida.** `_guard_capture_consent` hacía early-return con `not MIGRATION_CAPTURE_ENABLED`;
`_guard_reviewed_capture` no. Con `MIGRATION_CAPTURE_ENABLED=False` el codegen no emite ni una
llamada de captura (capturar es FÍSICAMENTE imposible) y sin embargo `apply`/`apply_all`/
`rollback` seguían respondiendo 409 — y `rollback` **no tiene `force`** (el de `apply` solo
cubre cuarentena), así que se cerraba la vía de RECUPERACIÓN por un riesgo que no puede
materializarse. FIX: mismo early-return en `_guard_reviewed_capture`.
**(C2b) el gate de `reviewed` de `apply` miraba TODO el blueprint.** Su docstring lo justificaba
con la premisa "lo que se aplica es siempre un prefijo de la cadena", que es **falsa**:
`apply?version=X` aplica un prefijo ESTRICTO. Con 0001..0010, solo 0010 con
`capture_selects/reviewed=false` y la BD en 0005, `apply?version=0007` devolvía 409 nombrando
0010 —que esa corrida no iba a tocar— y ni el `dry_run` se podía previsualizar. FIX: el gate se
movió de `apply`/`apply_all` a **`_run_apply`** (el único camino de ejecución de ambos), donde
ya está calculado `compute_pending`, y se invoca con `migration_ids` de las PENDIENTES REALES
—igual que `rollback`—. Efectos: el `dry_run` ya no se bloquea (no ejecuta nada) y en
`apply_all` el 409 sale **por BD** con sus propias pendientes, no frenando el lote entero.
`_run_apply` ganó `model_id` para poder consultarlo.
**Menores de la misma pasada**: (a) limpiar un override por motor con `null`
(`{"up_sql_postgresql": null}`) NO revocaba `reviewed` — `sql_fields_changing` exigía
`is not None` pero la asignación ocurre con la sola presencia de la clave, así que el SQL
efectivo de PG pasaba a ser la traducción del `up_sql` base (el que extrae datos) con la
aprobación intacta: era el agujero B2 por otra puerta. Ahora es por PRESENCIA de la clave, con
`up_sql: null` como única excepción (no se asigna ⇒ no cambia nada). (b) cambiar solo el
`down_sql` ahora PURGA las capturas (reordena `down_statements` → las filas `direction='down'`
apuntarían a otra sentencia; el checksum solo las marcaba `stale`), sin meter `down_sql` en
`sql_fields_changing` (que sigue gobernando el freeze y el manifiesto del `up`).
(c) `MIGRATION_CAPTURE_MAX_BYTES` y `payload_bytes` medían CARACTERES (`len()` sobre el `str`
del JSON): con CJK/emoji el payload real pesaba 3-4× el tope y el campo mentía el nombre —
ahora `len(...encode("utf-8"))`, con una codificación por fila (no se re-codifica el acumulado:
sería O(n²)); y `truncated` ahora se marca también cuando UNA sola fila rebasa el presupuesto
(la fila se conserva, pero el campo significa "había más filas/bytes que los topes").
(d) `captured_select_count` contaba con un `COUNT` de la tabla, que acumula CORRIDAS
ANTERIORES: una versión aplicada con captura y luego revertida con un `down_sql` sin lecturas
hacía que el `rollback` informara `1` y **auditara una escritura que no ocurrió**. Ahora
`migration_results.finalize` devuelve las filas que ESA corrida escribió (contador
`_written` por migración/dirección para el camino AUTOCOMMIT, donde el buffer está vacío), viaja
en `MigrationResult.captured_results` y `_capture_pointer` solo suma y audita si hubo escritura
real; `count_results` se eliminó (dead code que invitaba a repetir el error).
(e) higiene: el `lifespan` de `main.py` envuelve su limpieza en `try/finally` (si el ciclo
cerraba por excepción quedaban la tarea de purga pendiente y los engines sin liberar);
`capture_statement` perdió el parámetro `engine` (muerto) y `_apply_one(buffered=)` ahora sí se
usa (fija `committed=not buffered` ante un fallo, mismo criterio que `rollback_to`);
`migration_results.begin()` barre el buffer en memoria al arrancar cada dirección (borde de un
`BaseException` entre captura y `finalize`); y las descripciones OpenAPI que decían "409 sin
tocar el motor" ahora aclaran que el gateway lee antes la versión del destino (no muta nada).
Verificado por ejecución directa de los tests (política: sin `pytest`): 69 casos en
`tests/test_migration_results.py` + 24 en `tests/test_api_migrations_apply_flow.py` /
`tests/test_api_migrations_stamp_and_edit.py` (con TestClient+SQLite y el runner mockeado) + sin
regresiones en `test_api_model_migrations.py` (28), `test_api_migrations_rollback_flow.py` (6),
`test_api_plan09_adopt_snapshot.py` (15), `test_sql_dialect.py` (40), `test_migration_runner.py`
(16), `test_migration_reconcile_partial.py` (26) y `test_query_policy.py` (83). **Sigue pendiente
el e2e contra motores reales.**

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

## Módulo de Diff de Esquema y Sincronización entre BDs

Compara la **estructura** de dos BDs gestionadas del mismo motor (o MySQL↔MariaDB) y, a partir
del diff, **adopta** el resultado como nueva versión de blueprint (Opción A) o lo **ejecuta
directo** ad-hoc (Opción B). Guía de uso: `docs/features/schema-comparison.md`.

- **Arquitectura de 3 capas** (`app/services/db_admin/`): (1) introspección →
  `ServerAdapter.structural_snapshot(database) -> SchemaSnapshot` (DTOs nuevos en `dtos.py`:
  tablas extendidas + vistas/rutinas/triggers/secuencias/enums/extensions/events); (2)
  **diff puro** `schema_diff.py::diff_snapshots(source, target) -> SchemaDiff` (sin motor, 100%
  testeable — matching por **definición**, no por nombre autogenerado; normalización
  anti-falsos-positivos de tipos/defaults/collation; clasificación destructiva fail-closed por
  ítem); (3) `ServerAdapter.render_diff(diff) -> list[RenderedStatement]` (DDL por dialecto).
- **Endpoints** (`app/routes/v1/schema_comparisons.py`): `POST /schema-comparisons` (snapshotea
  ambas BDs, diffea, persiste), `GET /schema-comparisons/{id}` (resumen), `GET .../items`
  (DDL paginado, dry-run obligatorio), `GET .../export` (descarga el diff como archivo
  `.sql`: todas las entidades o solo `item_ids` seleccionados + filtros
  `object_type`/`change_type` + `include_rollback`; NO usa `ApiResponse`, es file download;
  solo lee ítems ya calculados → funciona igual para BDs adoptadas o crudas; envuelve
  rutinas/triggers/events MySQL con `DELIMITER`; audita `schema_comparison.export`),
  `POST .../execute-preview` (resuelve modo/selección
  de Opción B SIN ejecutar, devuelve el `confirm_token`), `POST .../adopt` (Opción A,
  requiere que el target esté en el inventario Y tenga `model_id`, reusa
  `ModelMigrationController.create_migration`), `POST .../execute` (Opción B, 409 si el
  target TIENE `model_id`; `mode: all|all_except_destructive|custom` +
  `confirm_target_name` + `confirm_token` verificable con
  `SchemaComparisonController.execution_token(...)`; usa `MigrationRunner.execute_adhoc`, sin
  Alembic ni `_gw_v_{slug}`).
- **BDs sin adoptar**: cada lado (`source`/`target`) acepta `{lado}_database_id` (BD en
  inventario) O `{lado}_server_id`+`{lado}_database_name` (BD cruda de cualquier servidor
  dado de alta, sin necesidad de registrarla) — validado que exista en vivo. Una referencia
  cruda que coincide con una `ManagedDatabase` ya existente se **auto-resuelve** a esa BD
  (mismo lock/cuarentena/Opción A que si se hubiera pasado el id). Opción B sobre una BD
  genuinamente sin registrar usa una clave de lock sintética (negativa, determinística por
  `server_id`+nombre — nunca colisiona con un `managed_database_id` real) y no tiene
  concepto de cuarentena. Opción A da 422 si el target no está en el inventario.
- **Modelos**: `SchemaComparison`/`SchemaComparisonItem` (persistidos, con `source_fingerprint`/
  `target_fingerprint` — anti-TOCTOU: re-snapshotear y recomparar antes de adoptar/ejecutar, 409
  sin `force` si difiere). `SchemaComparison` guarda siempre `{lado}_server_id`/
  `{lado}_database_name` (identidad física) y `{lado}_database_id` (`int | None`, solo si
  está en inventario).
- **ORDEN DE EJECUCIÓN (fix mayor)**: `seq` es la ÚNICA fuente de verdad del orden; `phase`
  (1..9) quedó como etiqueta INFORMATIVA — **ordenar por `phase` produce un orden que el motor
  rechaza**. El orden viejo era `(phase, object_type, object_name)`, alfabético dentro de la
  fase, y eso solo bastaba para romper la migración: vista antes que la vista que lee (1146);
  `check_constraint` < `column` → CHECK antes de su columna (3813/42703); `foreign_key` <
  `unique_constraint`/`index` → FK sin clave de respaldo (errno 150); FK de fase 3 antes de la
  PK de fase 4 (errno 150); `column` < `foreign_key` → DROP COLUMN con FK viva (1828);
  `materialized_view` < `view` en PG (42P01); DROP de tabla padre antes que la hija (1451).
  Ahora `order_diff_items` ordena por **`_STEP`** (pasos finos que CRUZAN fases: prerrequisitos
  → [DROP adelantado] → CREATE TABLE → columnas → PK → índices/UNIQUE/CHECK → **FKs al final
  del bloque aditivo** → cuerpos → DROP de cuerpos → DROP de FKs → índices → UNIQUE → CHECK →
  columnas → tablas → prerrequisitos) y, dentro de cada paso, **topológicamente** con
  `build_dependency_graph` (tabla→tabla por FK; sub-objeto→su tabla nueva; columna nueva→el
  índice/CHECK/FK que la menciona; FK→la PK/UNIQUE/índice de la tabla referida; **cuerpo→los
  objetos que menciona su cuerpo** por escaneo de identificadores). Para los `dropped` la
  arista se INVIERTE (el dependiente cae primero, que es lo que exige PG). **Hoisting**: se
  ADELANTA el DROP de un cuerpo que bloquea un ALTER (PG rechaza `ALTER COLUMN TYPE` con una
  vista dependiente) y el DROP de una FK que bloquea un cambio de TIPO de sus columnas
  (MySQL 1832/3780). Bug PREEXISTENTE corregido en `_table_dep_order`: agregaba a `placed`
  DENTRO de la pasada, así que una hija visitada tras su padre heredaba su nivel (ambas en 0);
  invisible al crear (el desempate alfabético lo tapaba) pero fatal al borrar, que usa el rango
  INVERTIDO. La comparten el clon y `clone_dependencies`.
- **Grupos atómicos + cierre de dependencias de la selección** (`plan_integrity.py`, puro):
  la unidad de selección es el CAMBIO (`op_group` = `object_type|object_name|change_type`), no
  la sentencia — un índice/UNIQUE/CHECK/FK redefinido rendea `DROP viejo`+`CREATE nuevo` y un
  PK cambiado `DROP`+`ADD`; marcar solo una daba 1061/1068. `depends_on` (persistido en
  `schema_comparison_items`, JSON) lista los `op_group` que deben ir ANTES, solo de ítems de
  ESTA comparación. Política: `adopt`/`execute custom` → **422** con `missing_dependencies` +
  `suggested_item_ids` (selección explícita: no se recorta en silencio), salvo
  `auto_resolve_dependencies=true`; `all`/`all_except_destructive` → **poda transitiva** +
  `excluded_by_dependency` (el filtro por riesgo dejaba fuera una tabla `possible_rename_of`
  pero NO sus índices → `CREATE INDEX` sobre tabla inexistente). Endpoint nuevo
  `POST /schema-comparisons/{id}/resolve-selection`. **Linter de plan**
  (`validate_statement_plan`) verifica el INVARIANTE de orden antes de materializar la versión:
  `dependency_out_of_order`/`atomic_group_not_contiguous`/`duplicate_creation` son BLOQUEANTES
  (422 en el gateway, sin tocar el motor); `create_and_drop_same_object`/
  `destructive_without_rollback` son avisos (`plan_warnings`).
- **`down_sql` de una REDEFINICIÓN (fix)**: solo la 2ª sentencia del par llevaba reverso y ese
  reverso era `CREATE viejo` **sin borrar antes el nuevo** → el rollback de un índice/UNIQUE/
  CHECK/FK redefinido **fallaba SIEMPRE** con 1061/42P07 (el emparejamiento `pair_by_name` es
  por nombre, así que el objeto nuevo choca). Ahora `base_adapter._stmts(...)` adjunta el
  reverso COMPLETO a la ÚLTIMA sentencia del grupo (las demás en NULL) porque el `down_sql` de
  la versión se ensambla en orden INVERSO. Se agregaron reversos donde no había: vistas/
  matviews/rutinas/triggers/eventos, PK, `DROP TABLE` (estructura, nunca confirmado),
  UNIQUE/CHECK eliminados, secuencias, ENUM, extensiones. `ALTER TYPE … ADD VALUE` sigue SIN
  reverso a propósito. `down_confirmed` estricto: lo que VALIDA datos al recrearse (UNIQUE/
  CHECK/FK/`ADD PRIMARY KEY`) queda sugerido; pura definición (vista/rutina/trigger) se
  confirma; índice no único sí, único no. También se corrigió `_ri_column_modified`, que
  duplicaba el reverso en cada sentencia del grupo.
- **Gotchas**: dirección `source`(deseado)/`target`(a modificar) siempre explícita, nunca
  inferida; el modo automático `all_except_destructive` excluye TODO lo no-demostrablemente-
  aditivo (no solo `DROP`: narrowing de tipo, cambio de collation/charset, `possible_rename_of`);
  objetos procedurales (vistas/rutinas/triggers/events) llevan `requires_individual_review` y
  **nunca** entran en `all`/`all_except_destructive`, solo `custom`; PostgreSQL cubre solo el
  schema `public` (`scope_note` en la respuesta); v1 **no** autogenera `RENAME` (DROP+CREATE +
  heurística `possible_rename_of` advisory).
- **UNIQUE KEY reflejada DUPLICADA en MySQL/MariaDB (fix)**: SQLAlchemy refleja una misma
  `UNIQUE KEY` de MySQL/MariaDB en **dos** colecciones — `get_indexes()` (con `unique=True`)
  y `get_unique_constraints()` (que la marca con `duplicates_index`) — porque en esos motores
  una unique constraint *es* un índice. El snapshot guardaba ambas copias, así que el diff
  emitía **DOS** sentencias que crean la MISMA clave (`ALTER TABLE … ADD CONSTRAINT x UNIQUE`
  + `CREATE UNIQUE INDEX x`) y la segunda abortaba la migración con
  `(1061, "Duplicate key name 'x'")`. El camino de tabla NUEVA ya lo evitaba
  (`_new_table_child_items` saltaba `ix.unique`), pero el de tabla EXISTENTE no filtraba nada
  (el comentario "no PK/unique-constraint" documentaba una intención no implementada). Fix:
  `schema_diff.py::_index_backs_unique_constraint` + filtrado de `src.indexes`/`tgt.indexes`
  en `_diff_one_table` (**ambos** lados: si no, una unique key que sobra en el destino daba
  dos DROP y el segundo fallaba con 1091). El match es por **NOMBRE**, no por `ix.unique` a
  secas: en PostgreSQL un índice único puede ser autónomo sin constraint detrás (índice único
  **parcial** con `WHERE`) y descartarlo lo perdería en silencio — el fix también corrige esa
  pérdida latente en el camino de tabla nueva (afectaba al clon). Solo MySQL y Oracle marcan
  `duplicates_index`; PostgreSQL no duplica. Afecta a schema-comparisons (diff/adopt/execute)
  y al clon, que comparten `diff_snapshots`+`render_diff`. Tests en `tests/test_schema_diff.py`.
  **Nota operativa**: una versión de blueprint YA adoptada antes de este fix conserva el
  `up_sql` con la sentencia redundante — hay que corregirla con `PATCH` (permitido mientras no
  haya una aplicación EXITOSA; un intento fallido no congela el SQL).
- **FALSO POSITIVO de vista/rutina/trigger/evento al diffear una BD contra su CLON (fix,
  2026-07-27)**: MySQL/MariaDB guardan el cuerpo con el esquema CALIFICADO —
  `information_schema.VIEWS.VIEW_DEFINITION` devuelve SIEMPRE
  ``select `midb`.`t`.`col` from `midb`.`t` ``— así que dos BDs con el MISMO objeto lógico
  tienen cuerpos textualmente distintos: cada una lleva su propio nombre adentro. El diff
  comparaba los cuerpos crudos → **toda** vista/rutina/trigger/evento salía `modified` en
  cuanto los nombres de las dos BDs diferían (reproducido: 1 item; con el mismo nombre de BD,
  0 items). `normalize_body` NO alcanzaba: colapsa whitespace y quita el `DEFINER`, pero el
  nombre de la BD es parte del texto de la consulta. La `requalify_body_schema` que ya
  existía se aplica al SQL **renderizado** (después del diff), así que corregía el DDL
  generado pero no evitaba el falso positivo. Fix:
  `sql_dialect.strip_self_schema_qualifier` quita de cada lado el calificador de su base
  PROPIA antes de comparar, usado por `_view_key`/`_routine_key`/`_trigger_key`/`_event_key`.
  NO enmascara diferencias reales: una referencia a OTRA base se conserva en ambos lados
  (tests que lo verifican). Solo la forma con BACKTICKS, mismo criterio que
  `requalify_body_schema`, para que lo que el clon RE-ESCRIBE y lo que el diff NORMALIZA sean
  el mismo concepto. Tests en `tests/test_body_schema_qualifier_diff.py`.
- **Clasificación new/modified/dropped (fix)**: `primary_key` ya NO es siempre `modified`
  — agregar un PK donde no existía es `new`, eliminarlo por completo es `dropped`, solo un
  PK que cambió existiendo en ambos lados es `modified`. `index`/`unique_constraint`/
  `check_constraint`/`foreign_key` que se redefinen (mismo `name`, firma distinta) ahora se
  emparejan como un solo ítem `modified` en `_diff_collection` (`pair_by_name=True`) en vez
  de un par suelto `new`+`dropped` sin relación visible — sigue ejecutándose como DROP+CREATE
  por debajo, solo cambia clasificación/conteo (ver detalle y el gotcha de ejecución que
  cierra este fix en `docs/features/schema-comparison.md`). Emparejamiento fail-closed: nombre
  ambiguo (>1 candidato del mismo lado) no se fusiona. Pendiente: re-correr
  `scripts/verify_schema_diff_e2e.py` contra motores reales para estos nuevos renderers
  (`_ri_index_modified`/`_ri_unique_modified`/`_ri_check_modified`/`_ri_pk_changed`) — solo
  verificados con tests unitarios/SQLite hasta ahora.
  **Límite histórico YA CORREGIDO** (se deja anotado porque explica el síntoma): adoptar
  (Opción A) una rutina/trigger MySQL/MariaDB con cuerpo `BEGIN...END` fallaba porque
  `sql_dialect.py::split_sql_statements` cortaba el cuerpo en su primer `;` interno
  (normalmente el `DECLARE`) → `1064 ... near '' at line N` con el SQL truncado. Opción B
  nunca lo tuvo (no vuelve a partir el SQL ya renderizado). Ver
  "Cuerpos procedurales en `split_sql_statements`" en la sección de Plan 02.
  Verificación e2e contra motores reales (`scripts/verify_schema_diff_e2e.py`, requiere Docker):
  **ejecutada — 219 checks / 0 fallos** en MySQL/MariaDB/PostgreSQL. Migración Alembic
  (`schema_comparisons`/`schema_comparison_items`) verificada con ciclo completo
  upgrade/downgrade/upgrade contra **MariaDB real** (no solo SQLite): se encontró y corrigió
  un `downgrade()` roto (soltaba un índice FK-backed antes de `drop_table`, que MySQL/MariaDB
  rechaza) — mismo patrón presente en migraciones anteriores del repo, no corregidas (fuera
  de alcance; pendiente de auditoría propia).

## Módulo de Clonado de Bases de Datos

Clona estructura (y opcionalmente TODOS los datos) de una BD origen a una BD destino en
cualquier servidor — mismo u otro, mismo motor o distinto, origen/destino adoptados o crudos,
destino nuevo o existente. Guía de uso: `docs/features/database-clone.md`.

- **Flujo** (mismo patrón seguro que schema-comparisons): `POST /database-clones` (crea plan,
  snapshotea origen, persiste `CloneJob` + fingerprint + TTL) → `GET .../objects` (inventario +
  portabilidad + grafo de dependencias) → `POST .../resolve-selection` (cierre de dependencias,
  auto-select de la UI) → `POST .../preview` (plan final + `confirm_token`, sin ejecutar) →
  `POST .../execute` (valida token/nombre/fingerprint + `record_intent` fail-closed, ENCOLA el
  job async) → `GET .../{id}` (polling de estado) / `GET .../items` / `POST .../cancel`.
- **Archivos**: `app/controllers/clone_controller.py` (orquestador: `create_plan`/`preview`/
  `execute_clone`/`run_job` pipeline); `app/services/clone_runner.py` (worker in-process
  `ThreadPoolExecutor` + barrido `interrupted` en el `lifespan`); `app/services/db_admin/
  data_copy.py` (copia de datos por streaming, `executemany` parametrizado, FK-checks-off);
  `app/services/db_admin/clone_dependencies.py` (resolver puro FK+trigger firme + advisory);
  `app/models/clone_job.py` (`CloneJob`/`CloneJobItem`); `app/schemas/clone.py`;
  `app/routes/v1/database_clones.py`. Migración `b1c2d3e4f5a6`.
- **Reúso**: estructura = `diff_snapshots(origen_filtrado vs vacío)` + `adapter_destino.render_diff`
  (DDL nativo en el dialecto destino); limpieza objeto-por-objeto = `diff(vacío vs destino)` →
  DROPs; DDL/limpieza se ejecutan con `MigrationRunner.execute_adhoc` (advisory lock + AUTOCOMMIT,
  clave sintética negativa para BDs crudas — `_synthetic_lock_key`); adopt reusa
  `ManagedDatabaseController.adopt_database` + stamp.
- **Decisiones/gotchas**: datos por **streaming async** (no el seed capado, que sigue siendo
  "catálogo, no ETL"); `INSERT` parametrizado (NUNCA literales); datos NUNCA se traducen
  cross-engine (valores adaptados por driver; tipos riesgosos fallan por tabla y se reportan);
  cross-engine = clonar lo portable + reportar `skipped` (rutinas/triggers/events no portables;
  estructura cross-family fiable solo MySQL→PostgreSQL); `clean_mode` objects preserva la BD y
  su config, `drop_database` es reset total; auto-adopt SOLO en clon completo con origen
  gestionado con blueprint (requiere `adopt_owner_id` del servidor destino). **Durabilidad**:
  worker in-process, los jobs NO sobreviven un reinicio (quedan `interrupted`; cola durable =
  futuro).
- **Tablas grandes (copia de datos)**: la copia usa `database_connection(..., bulk=True)` con
  `REMOTE_BULK_STATEMENT_TIMEOUT_MS` (default 1h; `0`=sin límite) en lugar del interactivo de 15s
  (`REMOTE_STATEMENT_TIMEOUT_MS`) que cancelaría lotes grandes dejando datos parciales — se cachea
  un engine aparte por el flag `bulk`. Sesión de lectura en `READ COMMITTED` (evita inflar
  undo/history del origen; consistencia cross-tabla ya no garantizada). Progreso throttleado a
  ~3s (no un UPDATE por lote). Commit por lote en AUTOCOMMIT (sin transacción gigante) → un fallo
  a mitad NO es reanudable (reintentar = `drop_database`/dropear y recopiar). Lote por FILAS
  (`CLONE_DATA_BATCH_ROWS`), no por bytes → en tablas MUY anchas con BLOB/TEXT bajar el batch si
  aparece `max_allowed_packet`. Batching adaptativo por bytes + reanudación = futuro.
- **Objetos con cuerpo (vistas/rutinas/funciones/triggers/eventos)**: SÍ se clonan (no solo
  tablas); en el preview van al FINAL de `structure_statements` (fase 5, tras tablas/FKs). Se
  ejecutan como una sola sentencia (`exec_driver_sql`, no re-parte `;` → `BEGIN…END` no se rompe).
  Dos cuidados (`CloneController`): (1) **re-calificación de esquema** `_requalify_body` — MySQL/
  MariaDB inyectan el esquema ORIGEN en el cuerpo (p.ej. `VIEW_DEFINITION` trae `` `origen`.`t` ``);
  sin reescribir origen→destino, el clon leería de la BD ORIGEN (fuga cross-db); (2) **reintento
  diferido** `_run_body_statements` — resuelve dependencias vista→vista en cualquier orden (pasadas
  hasta sin progreso). `execute_adhoc` ganó `stop_on_error=False` para esto. Fix del ON UPDATE
  duplicado en `MySQLAdapter._render_column_def` (MariaDB pega `ON UPDATE` dentro del default).
  **Triggers/eventos se crean DESPUÉS de la fase de datos** (`_POST_DATA_BODY_TYPES` en
  `_run_phases`), no en la fase de estructura como vistas/rutinas: un trigger `AFTER INSERT`
  del origen que puebla OTRA tabla (p.ej. una pivote `users_modules_permissions`) se
  dispararía durante la copia de datos y duplicaría filas → `ER_DUP_ENTRY 1062` cuando la
  copia llega a esa tabla (con `upsert=False`, i.e. destino nuevo/limpiado). `FOREIGN_KEY_CHECKS=0`
  NO desactiva triggers en MySQL/MariaDB (PostgreSQL sí con `session_replication_role='replica'`),
  así que la única defensa portable es el ORDEN — mismo criterio que `mysqldump` (recrea triggers
  tras cargar datos). Los eventos van con los triggers por prudencia (efectos secundarios).
- **Fidelidad de TIPOS (MySQL/MariaDB)**: el tipo de columna se captura de
  `information_schema.COLUMN_TYPE` (hook `_column_extras` → `ex["column_type"]`, usado por
  `base_adapter` en vez de `str(reflected_type)`). `str(type)` PIERDE detalle crítico: `ENUM`/`SET`
  **sin lista de valores** → CREATE TABLE inválido (1064); `UNSIGNED` → rango corrupto; display
  width. Con el fix el DDL reproduce `enum('a','b')`/`bigint(20) unsigned`/`tinyint(1)` exactos
  (también mejora la exactitud del diff: dos ENUM con valores distintos ya no comparan iguales).
- **AUTO_INCREMENT que no encabeza la PK (MySQL/MariaDB)**: InnoDB exige que la columna
  AUTO_INCREMENT sea la primera columna de alguna clave de la MISMA sentencia `CREATE TABLE`
  (un índice creado después no cuenta). Tablas origen heredadas (p.ej. migradas de MyISAM, que sí
  lo tolera) pueden traer una PK compuesta con el autoincrement al final → reproducirla tal cual
  dispara `(1075, ...)` al primer `CREATE TABLE`. Fix en `MySQLAdapter._render_create_table`: si la
  PK/UNIQUE inline no cubre al autoincrement, se agrega automáticamente una `KEY` de apoyo (no
  reordena la PK ni cambia ningún objeto) + aviso en `warnings` del preview
  (`ClonePreviewOut`/`_ExecutionPlan.warnings`, `clone_controller.py::_autoincrement_pk_warnings`).
  Mismo `render_diff` usado por schema-comparisons, así que también lo cubre. **Nombre EXPLÍCITO
  de la KEY** (`` `_gw_autoinc_{columna}` ``, patrón `_gw_`): sin nombre, MySQL/MariaDB la
  auto-nombra igual que la columna (`id`) — si el origen YA tiene un índice real sobre esa
  columna (también auto-nombrado `id`), la sentencia posterior que lo recrea choca con
  `(1061, "Duplicate key name 'id'")`. Regresión real encontrada tras desplegar el fix anterior.
- **`%` literal en el DDL ejecutado (los 3 motores)**: `MigrationRunner.execute_adhoc` usa
  `conn.exec_driver_sql(stmt)` sin bind params (deliberado: `text()` rompería `::` de
  PostgreSQL). Pero SQLAlchemy distila un `parameters` ausente a `()` (nunca a `None`)
  antes de llegar al DBAPI, y pymysql/psycopg (los 3 `EngineType`, paramstyle
  `pyformat`/`format`) parsean placeholders `%s`/`%(name)s` en cuanto reciben params
  no-`None` → un `%` LITERAL en el DDL (columna `GENERATED ... AS (id % 10)`,
  `LIKE '%...%'`, `DATE_FORMAT(..., '%Y-%m-%d')`) revienta en CUALQUIERA de los 3 motores
  (pymysql: `unsupported format character`; psycopg: `incomplete placeholder` / `only
  '%s', '%b', '%t' are allowed as placeholders`) aunque el DDL sea válido. Verificado
  invocando el parser real de ambos drivers — la primera versión de este fix asumía
  incorrectamente que PostgreSQL no lo necesitaba y solo escapaba para MySQL/MariaDB.
  Fix (corregido): `MigrationRunner._escape_percent` duplica `%`→`%%`
  INCONDICIONALMENTE (sin distinguir `engine`) justo antes de `exec_driver_sql`; el DDL
  guardado en preview/historial conserva el texto sin escapar. Mismo punto usado por
  clone y por la ejecución ad-hoc de schema-comparisons.
- **FK checks durante la limpieza (`clean_mode=objects`)**: los `DROP TABLE` ya se ordenan
  en topológico INVERSO (`schema_diff.py::order_diff_items`/`_table_dep_order`, misma
  función que ordena los INSERT de datos, en reversa) — cubre el caso normal, pero no
  puede ver una FK desde OTRA base de datos del mismo servidor (snapshot de una sola BD) ni
  un ciclo de FKs intra-BD; ambos disparan `(1451, 'Cannot delete or update a parent
  row...')` en un `DROP TABLE` aislado. Fix (defensa en profundidad):
  `execute_adhoc(..., disable_fk_checks=True)` + `MigrationRunner._toggle_fk_checks`
  (mismo mecanismo que `data_copy.py::_set_fk_enforcement` para la fase de datos:
  `FOREIGN_KEY_CHECKS`/`session_replication_role`, best-effort). `CloneController` lo
  activa SOLO para `CLONE_ITEM_CLEAN`, no para estructura (que ya tiene su propio orden
  padre-antes-que-hijo). **Advertencia proactiva** (visibilidad, no solo mitigación):
  `ServerAdapter.external_fk_dependents(database)` consulta
  `information_schema.KEY_COLUMN_USAGE` a nivel de SERVIDOR (`server_connection`, no de
  una sola BD) para detectar columnas de OTRA BD del servidor con FK hacia `database` —
  solo MySQL/MariaDB (PostgreSQL no soporta FKs cross-database, hereda `[]` del default de
  `ServerAdapter`). `CloneController._external_fk_warnings` la consulta en el preview SOLO
  si `target_mode='existing'` y `clean_mode` implica DROP (`objects`/`drop_database`), y
  lista las columnas dependientes en `warnings` (hasta 5 + conteo del resto).
- Verificación: tests unit/API (FakeAdapter + runner síncrono) + `test_mysql_render.py` +
  `test_clone_body_objects.py`; **e2e contra MariaDB 11 real EJECUTADO** (tabla con `ON UPDATE`,
  vista, vista-sobre-vista en orden inverso, función, procedimiento, trigger, evento: todos
  clonados y re-calificados al destino). `scripts/verify_clone_e2e.py` cubre tablas/datos;
  cross-engine MySQL→PostgreSQL pendiente de corrida.

## Módulo de Usuarios del Motor (vista agrupada + CRUD por identidad)

Mejora la LECTURA y la GESTIÓN de los usuarios de un servidor. Guía de uso:
`docs/features/engine-users-management.md`.

- **Problema/asimetría por motor**: en MySQL/MariaDB `'user'@'hostA'` y `'user'@'hostB'`
  son cuentas SEPARADAS (redundancia visual al listar por `user@host`); en PostgreSQL un
  ROLE **no tiene host** (`supports_hosts=false`; el acceso por host vive en pg_hba.conf,
  fuera del alcance SQL). El adapter expone `supports_hosts` (MySQL/MariaDB True, PG False).
- **Vista agrupada** `GET /servers/{id}/users/grouped`: agrupa por username (una entrada +
  sus hosts como identidades) y CRUZA con el inventario → cada host se marca
  `adopted`/`unmanaged`/`orphan` (mismo cruce que `reconcile`). El frontend usa
  `supports_hosts` para ocultar host/agregar-host en PG.
- **CRUD por IDENTIDAD física** `(server_id, username, host)` — funciona con usuarios
  ADOPTADOS y NO adoptados (patrón "por identidad", como schema-comparisons con refs crudas):
  `POST /servers/{id}/users` (CREATE), `PATCH /servers/{id}/users/password` (ALTER),
  `DELETE /servers/{id}/users?username=&host=&confirm_username=` (DROP), todos sobre el
  motor. **Stateless por defecto** con `adopt` opcional (registra en `server_users` con la
  contraseña cifrada); si ya hay fila de inventario, el cambio de contraseña SIEMPRE la
  sincroniza.
- **Revelar contraseña** `POST /servers/{id}/users/reveal-password`: SOLO posible si el
  gateway fijó la contraseña (la guarda Fernet-reversible). El motor solo guarda un hash
  irreversible → una contraseña que el gateway nunca conoció NO se revela: `404` si no está
  en inventario, `409` si adoptado sin contraseña. Auditado (`server_user.password.reveal`).
- **Agregar host** `POST /servers/{id}/users/add-host` (solo MySQL/MariaDB; `422` en PG):
  clona `'user'@'source_host'` a `'user'@'new_host'`. `reuse_password=true` copia el HASH
  vía `SHOW CREATE USER` (reescribe solo el grantee; sirve incluso para el binario de
  `caching_sha2_password`); `false` exige `new_password`. `copy_grants=true` (opcional)
  replica permisos vía `SHOW GRANTS` reescribiendo el grantee (best-effort → `grants_error`;
  omite USAGE/PROXY/`IDENTIFIED BY`).
- **Archivos**: métodos nuevos en `server_user_controller.py` (`list_users_grouped`,
  `create_user_by_identity`, `set_password_by_identity`, `drop_user_by_identity`,
  `add_host`, `reveal_password`); adapters `supports_hosts` + `add_user_host`/
  `copy_user_grants` (base 422 → MySQL implementa, PG hereda 422); schemas en
  `app/schemas/server_user.py`; rutas en `app/routes/v1/servers.py`. **NO** hay tabla nueva
  (reusa `server_users`).
- **Gotchas / seguridad**: **guard anti auto-lockout (B1)** — crear/rotar/dropear/agregar-host
  sobre `Server.root_username` (la credencial pseudo-root, que NO es fila de `server_users`,
  así que el guard de `grant_controller` no la cubría) devuelve 409; replica el patrón de
  `grant_controller.py:206`. **Revelar contraseña audita fail-closed** (`record_intent`,
  `touched_engine=False`) antes de descifrar. El rewrite de grantee asume identificadores
  whitelisteados → coincide byte a byte con `SHOW CREATE USER`/`SHOW GRANTS`; `copy_grants`
  reejecuta DCL del motor (mismo servidor pseudo-root, omite USAGE/PROXY pero **sí** propaga
  globales/`WITH GRANT OPTION` — es "clonar cuenta"). Limitación: el CRUD por identidad usa
  whitelist ESTRICTA (rechaza usernames legacy con dígito inicial/`.-$`). Revisado por
  gateway-security: B1 (bloqueante) + R1 (reveal fail-closed) + R4 (no volcar str(exc))
  resueltos; R2 (rate-limit reveal)/R3 (auditar cada grant copiado)/R5 (whitelist legacy) =
  follow-up. Tests: `tests/test_api_engine_users.py` (adapter mockeado + `_rewrite_grant_line`
  + guard root). **Pendiente**: verificación e2e contra motores reales (add-host/copy-grants).

## Módulo de Ciclo de Vida de BDs a nivel servidor (crear/borrar/usuarios)

Crea y borra bases de datos directamente en un servidor y lista qué usuarios/roles tienen
permisos sobre una BD, **por identidad** `(server_id, database)` — funcione o no adoptada en
el inventario. Análogo al CRUD de usuarios por identidad. Guía de uso:
`docs/features/server-database-lifecycle.md`.

- **Rutas** (`app/routes/v1/servers.py`, prefijo `/servers`): `POST /{id}/databases` (crear;
  `register` opcional [alias del campo `register_inventory`, requiere `owner_id`] → delega a
  `ManagedDatabaseController.create_database(provision=True)`); `POST
  /{id}/databases/{db}/drop-preview` (emite `confirm_token` + `active_connections` +
  `is_managed`); `DELETE /{id}/databases/{db}` (borra en motor + limpia inventario si es
  managed); `GET /{id}/databases/{db}/users` (grantees por BD, cruzados con inventario).
- **Controller**: `app/controllers/server_database_controller.py` (`ServerDatabaseController`,
  solo orquesta; el motor vive en los adapters).
- **Confirmación de borrado (doble factor de backend)**: `confirm_target_name == nombre real`
  (422) + `confirm_token` (`app/services/confirm_token.py`: HMAC-SHA256 con `SECRET_KEY`,
  expiración EMBEBIDA, TTL 2 min; **stateless**, ligado a `(server_id, db_name)`; 422 si no
  corresponde/manipulado, 410 si expiró). El nombre obliga a identificar CUÁL BD; el token da
  frescura/anti-replay. `record_intent` fail-closed antes del DROP.
- **Guard de BDs de sistema (NUEVO)**: `identifiers.ensure_not_reserved_database` (409) —
  antes NADA impedía `DROP DATABASE mysql`. MySQL/MariaDB: `information_schema`/`mysql`/
  `performance_schema`/`sys`; PostgreSQL: `postgres`/`template0`/`template1`. Se aplica en
  crear y borrar.
- **Adapters** (`base`/`mysql`/`postgres`): `drop_database(..., force_disconnect=False)` (PG
  hace `pg_terminate_backend` sobre `pg_stat_activity` antes del DROP — obligatorio si hay
  conexiones; MySQL no-op); `active_connections(db)`; `list_database_grantees(db)` (consulta
  INVERSA por BD agrupada por grantee: MySQL incluye globales `*.*` marcados `is_global`; PG
  combina `pg_database.datacl`+`aclexplode`+owner con `table_privileges`). `drop_database` y
  las lecturas usan `allow_existing=True` (nombres legacy); `create_database` sigue estricto.
  DTO `DatabaseGranteeInfo` en `dtos.py`.
- **Sin migración Alembic** (token stateless; `register` reusa `ManagedDatabase`).
- **Gotchas**: `register` colisiona con `BaseModel` → el schema usa `register_inventory` con
  `alias="register"`. PG `collation` es LOCALE del SO (puede fallar con `invalid locale
  name`). **Pendiente**: verificación e2e contra motores reales (crear/listar/drop con
  `force_disconnect`; en PG las consultas `aclexplode`/`datacl`/`pg_terminate_backend`).
  Verificado hasta ahora solo con `FakeAdapter` (sin `pytest`, sin Docker).

## Módulo de Consola SQL (queries ad-hoc en modo seguro)

Ejecuta SQL arbitrario sobre una BD de un servidor destino **con el usuario del motor que
se elija**, para verificar permisos reales. Guía de uso: `docs/features/sql-query-console.md`.

- **Rutas** (`app/routes/v1/servers.py`): `POST /{id}/query/preview` (clasifica, estima
  impacto, emite `confirm_token`), `POST /{id}/query/execute`, `GET /{id}/query/history`.
- **Archivos**: `query_policy.py` (PURO: clasificación + blocklist + estimación + redacción),
  `query_runner.py` (conexión, transacción, topes, normalización de valores),
  `query_console_controller.py`, `schemas/query_console.py`, `models/query_execution.py`
  (migración `a3b4c5d6e7f8`). Env nuevas `QUERY_*`.
- **Niveles**: `read` (directo) / `write` / `ddl` (ambos exigen `confirm_target_name` +
  `confirm_token`) / `blocked` (**403 sin tocar el motor, ni confirmando**). El nivel del
  lote es el MÁXIMO de sus sentencias. `UPDATE`/`DELETE` piden confirmación **tengan o no
  `WHERE`** (requisito explícito).
- **Clasificación por AST (sqlglot), NUNCA por palabras clave**, y tomando el peligro
  máximo de CUALQUIER nodo del árbol, no solo la raíz: `WITH d AS (DELETE … RETURNING *)
  SELECT * FROM d` tiene raíz `Select`. **Fail-closed** en todo borde: SQL ilegible, tipo
  no mapeado o sentencia OPACA (`exp.Command` — verificado que sqlglot degrada ahí `GRANT`,
  `CREATE USER`, `ALTER SYSTEM`, `CREATE EXTENSION`, `REPLACE INTO`, `DO`, `SET ROLE`,
  `CALL`, `RENAME TABLE`, `EXPLAIN ANALYZE`) ⇒ peligroso.
- **La blocklist de TEXTO no es redundante con el AST**: `FLUSH PRIVILEGES` parsea como un
  `exp.Alias` (indistinguible de una expresión inofensiva) y el DCL entero cae en `Command`
  sin estructura. Corre sobre texto normalizado (`_scan_normalize`: sin comentarios, con el
  CONTENIDO de los literales vaciado) para no marcar `WHERE accion='GRANT'` ni dejar evadir
  con `/*x*/GRANT`. El comentario EJECUTABLE de MySQL `/*!40101 … */` se conserva como
  código y se le descarta el número de versión (si no, desplaza los patrones anclados en
  `^`). El escaneo se repite sobre el LOTE CRUDO porque `split_sql_statements` descarta las
  "sentencias" que son solo comentarios.
- **`read` lo hace cumplir el MOTOR**: transacción de solo lectura (`START TRANSACTION READ
  ONLY` en MySQL/MariaDB, `SET TRANSACTION READ ONLY` en PG). Ninguna clasificación
  estática puede saber si `SELECT fn()` escribe. ORDEN de preparación de sesión: timeouts →
  read-only → `SET ROLE` (invertirlo rompe: `SET TRANSACTION READ ONLY` deja de estar
  permitido una vez que la tx ejecutó algo).
- **Lo prohibido va MÁS ALLÁ de lo destructivo obvio** porque el gateway conecta como
  pseudo-root y el motor SÍ lo permitiría: DCL (evita el módulo de permisos y su auditoría),
  acceso a archivos del host (`COPY … FROM PROGRAM` ≈ RCE, `INTO OUTFILE`, `pg_read_file`),
  estado global (`SET GLOBAL`, `ALTER SYSTEM`, `FLUSH`, `KILL`), `CREATE/DROP DATABASE|SCHEMA`
  (tienen endpoint propio), control de sesión/transacción, `SET ROLE`/`RESET ROLE`, SQL
  dinámico (`PREPARE`/`EXECUTE` ejecutarían texto sin clasificar), escritura sobre esquemas
  del sistema (LEERLOS sí: es parte de probar permisos), `_gw_v_*`/`_gw_stg_*`, y la propia
  BD de metadatos del gateway (match por host+puerto+nombre, 409).
- **Estimación de impacto**: el AST del `UPDATE`/`DELETE` se transforma en `SELECT COUNT(*)`
  con el MISMO `WHERE`, ejecutado con la MISMA credencial. Solo se emite cuando es EXACTO:
  `DELETE … USING` de PG deja la tabla fuera de alcance y `UPDATE a JOIN b` contaría filas
  del producto → `null` (la confirmación se exige igual). Red de seguridad: si el conjunto
  de tablas del COUNT no coincide con el del original, se descarta.
- **Un rechazo del motor es HTTP 200 con `success:false`**, no un 403: una query denegada es
  una PRUEBA EXITOSA. Por eso este camino NO usa `map_driver_error` (traduce 1142/42501 a un
  403 genérico que oculta el mensaje del motor). `map_driver_error` sigue cubriendo los
  fallos de INFRAESTRUCTURA.
- **`confirm_token` atado al SQL**: `confirm_token.issue/verify` ganaron `subject` opcional
  (`{sql_hash}|{modo}|{usuario}|{rol}`). Sin eso, `(operación, server_id, db)` es igual para
  cualquier consulta sobre esa base → se podría previsualizar un `SELECT` y ejecutar un
  `DROP` con el mismo token. Los usos históricos (sin `subject`) quedan intactos.
- **FIX de un bug LATENTE pre-existente**: `remote_engine.get_engine` cacheaba por
  `(server_id, bd, bulk, local_infile)` — **sin el usuario**. Un `ServerTarget` con otra
  credencial devolvía el engine pseudo-root: la prueba "como usuario limitado" corría como
  root, en silencio y dando verde. La clave ahora incluye usuario y timeout efectivo
  (`statement_timeout_ms` nuevo, que la consola necesita porque el interactivo de 15s es
  corto).
- **Modos de conexión**: `admin` (pseudo-root, con warning explícito), `stored` (Fernet del
  inventario), `provided` (contraseña del request, NUNCA persistida), `impersonate` (**solo
  PG**: `SET ROLE`; MySQL/MariaDB dan 422 — su `SET ROLE` solo alcanza roles ya otorgados).
- **SEGUNDA PASADA ADVERSARIAL (2026-08-02)** — 4 BLOQUEANTES, todos corregidos.
  (1) `stream_results=True` a nivel de CONEXIÓN hacía que SQLAlchemy enrutara TODA sentencia
  por un cursor con nombre, y psycopg lo compone como `DECLARE … CURSOR FOR <stmt>` (solo
  acepta consultas) → **PostgreSQL no funcionaba en absoluto**: moría en el
  `SET TRANSACTION READ ONLY` de `_prepare_session` y salía como 502 "no se pudo conectar".
  Los tests no lo vieron porque SQLite declara `supports_server_side_cursors=False` y el flag
  queda en no-op. (2) El tope de filas del lado del gateway NO acotaba el TRANSPORTE:
  `SSCursor.close()` de pymysql llama `_finish_unbuffered_query()`, que gira leyendo hasta el
  EOF —el comentario del propio driver dice que no hay forma de que MySQL deje de mandar—, así
  que `SELECT * FROM tabla_de_50M` con tope de 1000 traía igual las 50M. Fix de (1)+(2): la
  política emite `fetch_sql` con el `LIMIT` EMPUJADO AL MOTOR (`_limited_sql`; no se acota si
  cambia la semántica: `FOR UPDATE`, `INTO`, `LIMIT` propio menor, o no-`SELECT`/`UNION`) y se
  eliminó `stream_results`. (3) `DELIMITER //` agrupaba varias sentencias del servidor en UNA
  unidad del splitter → el keyword peligroso dejaba de estar en `^` y **toda la blocklist
  anclada se evadía** (GRANT/DROP DATABASE/SET GLOBAL/PREPARE/DROP USER/SET ROLE pasaban a
  `write`/`ddl` CONFIRMABLES); ídem `SELECT 1/*!;DROP DATABASE x*/`. Fix: la blocklist corre por
  SEGMENTO entre `;` del texto normalizado (seguro: los literales ya están vaciados) y
  `DELIMITER` es `blocked` — no hace falta, el splitter reconoce `BEGIN…END` por sí solo.
  Efecto colateral aceptado: un cuerpo de rutina con `COMMIT;` queda bloqueado (fail-closed:
  crear rutina con DCL + llamarla era la escalada). (4) `#` NO es comentario en PostgreSQL (es
  el XOR de enteros): `SELECT id # 0, lo_import('/etc/shadow') FROM t` salía `read` → se
  ejecutaba SIN confirmación NI auditoría (`record_intent` solo corre si no es lectura), con
  lectura arbitraria de archivos del host. `_scan_normalize` ahora recibe el `engine`.
- **Otros fixes de la misma pasada**: la clave del cache de engines no incluía la CONTRASEÑA
  → probar un usuario con clave INCORRECTA reusaba el engine de la correcta y respondía
  "conectó" (la herramienta mentía sobre lo único que existe para verificar); se agregó huella
  `blake2b` salada por proceso + cuantización del timeout en tramos de 5s + cota FIFO de 256
  engines (la clave depende de valores que elige el CLIENTE).
  `is_gateway_metadata_target` comparaba host por TEXTO → con la BD del gateway en el host `db`
  de un compose bastaba registrar `172.18.0.2` (misma máquina, y pasa el anti-SSRF por diseño)
  para dropear `audit_log`/`servers`/`server_users`; ahora resuelve ambos hosts a IPs e
  intersecta, fail-closed. Faltaban en la blocklist: `SET LOCAL/SESSION ROLE`, `set_config()`
  (se bloquea ENTERA: los literales llegan vaciados y su 1er argumento es indistinguible),
  `DISCARD`, UDF por `SONAME` (ejecución de código nativo en MySQL/MariaDB), `CREATE LANGUAGE`,
  FDW/`dblink`/`FEDERATED`/`SUBSCRIPTION` (SSRF iniciado por el MOTOR, invisible al net_guard),
  `pg_terminate_backend` y familia (salían `read`), `SET search_path`/`statement_timeout`/
  `foreign_key_checks` (anulan las garantías del runner; `search_path` además desalinea el COUNT
  del preview respecto del DELETE confirmado), `ALTER DATABASE/SCHEMA`, y `` `mysql`.`user` ``
  con backticks (se escanea también la variante sin comillas). `FOR UPDATE`/`INTO @` escondidos
  en un `/*! */` eran invisibles al AST (sqlglot NO tokeniza ese contenido) → `_TEXT_ELEVATORS`
  como respaldo textual. Timeouts de MySQL/MariaDB incompletos: `max_execution_time` solo aplica
  a SELECT de solo lectura y `lock_wait_timeout` viene con default de UN AÑO → ahora se emiten
  los 4 (ambos motores; cada uno es no-op en el otro, y un MariaDB dado de alta como `mysql` es
  un error de inventario frecuente). `_is_auth_like` depende del MODO (un 1045 con la credencial
  pseudo-root es gateway mal configurado, NO el resultado de una prueba) y suma `22023` —el
  SQLSTATE real de un `SET ROLE` a un rol inexistente en PG, porque `check_role()` no fija
  errcode— y `1130`. SAVEPOINT por conteo en `estimate_impact` (en PG un COUNT fallido abortaba
  la tx y los demás devolvían null). Cierre transaccional AISLADO: un `commit()` fallido
  descartaba TODOS los resultados de un lote ya ejecutado. `ddl_persisted` y `policy_miss`
  (SQLSTATE 25006 / errno 1792 = la política clasificó mal) nuevos en la respuesta. Excepciones
  no-SQLAlchemy capturadas (`UnicodeDecodeError` del fetch: SQLAlchemy no envuelve ese camino)
  sin volcar bytes del cliente. El `preview` ahora AUDITA (tocaba el motor con la credencial
  elegida sin rastro = oráculo de contraseñas a 30/min). Los motivos del 403 pasaron de
  `context` a `public_context`: `context` solo se expone en development, así que en PRODUCCIÓN
  el operador recibía "hay sentencias prohibidas" sin saber cuál ni por qué.
- **Verificación**: 145 casos en 4 archivos (`test_query_policy.py` 101, `test_query_console_security.py`,
  `test_query_runner_execution.py` 20 con doble de conexión, `test_api_query_console.py` 24 de
  extremo a extremo con el runner mockeado) + migración con ciclo upgrade/downgrade/upgrade en
  SQLite. Todo por ejecución DIRECTA de las funciones, **sin `pytest`** (política del proyecto).
  **PENDIENTE e2e contra motores reales**: `scripts/verify_query_console_e2e.py` está escrito
  pero NO ejecutado (sin Docker). Lo crítico a confirmar ahí es (a) que la tx READ ONLY rechace
  DE VERDAD una escritura mal clasificada —es la garantía central del diseño—, (b) `SET ROLE`
  con RLS en PG, (c) que el `LIMIT` empujado evite bajar la tabla entera, (d) los mensajes
  nativos de rechazo por permisos en los 3 motores.

## Módulo de Exportación de Bases de Datos (Plan 10)

Exporta **estructura y/o datos** de cualquier BD de un servidor dado de alta (adoptada o no,
por identidad `server_id`+nombre) a un artefacto descargable, en modo **estrictamente de solo
lectura** sobre el origen: toda sentencia destructiva existe únicamente como TEXTO dentro del
artefacto. Guía de uso: `docs/features/database-export.md`; contrato frontend:
`docs/api-reference-v10.md`; diseño: `docs/plans/10-exportacion-de-bases-de-datos.md`.

- **Flujo** (4º módulo de la familia; se copia el patrón de clon/schema-comparisons/collation):
  `POST /servers/{sid}/databases/{db}/database-exports` (plan, snapshot + `source_fingerprint` +
  TTL 24 h) → `GET /database-exports/{id}/objects` → `POST .../resolve-selection` (cierre de
  dependencias) → `POST .../preview` (**congela** la selección + `confirm_token`; con
  `dry_run_only` valida sin congelar) → `POST .../execute` (doble factor + re-snapshot +
  `record_intent` fail-closed, ENCOLA) → `GET .../{id}` (polling) / `.../items` / `.../cancel` /
  `.../manifest` → `.../download` (FileResponse con `Range`) o `.../content` (texto plano).
  `GET /servers/{sid}/databases/{db}/export-capabilities` publica todo lo que el formulario
  necesita.
- **Archivos**: `app/services/db_admin/export_spec.py` (PURO: enums, matriz de compatibilidad,
  resolución de selección, validación del `where`, plantillas de nombre),
  `sql_literals.py` (`render_value`/`render_value_text`, extraído y generalizado de
  `snapshot_data` SIN tocar `build_seed`), `export_writer.py` (generador incremental
  sql|csv|json|ndjson, `iter_artifact` es el único punto de entrada del controller),
  `export_session.py` (transacción de lectura + plazo duro), `app/services/export_package.py`
  (organización/split/compresión/índice/marca de incompleto), `export_storage.py` (spool,
  sha256, TTL, purga, huérfanos), `export_runner.py` (3ª copia del worker in-process),
  `app/controllers/export_controller.py`, `app/routes/v1/database_exports.py`,
  `app/models/export_job.py` (`ExportJob`/`ExportJobItem`/`ExportArtifact`),
  `app/schemas/export.py`, migración `a9b0c1d2e3f4`. 18 variables `EXPORT_*`.
- **`scope_ddl`/`entity_ddl` son un ENUMERADO de 4 valores, no dos booleanos**
  (`NONE|CREATE|DROP_CREATE|CREATE_IF_NOT_EXISTS`): con banderas el estado "eliminar sin crear"
  ES representable y hay que parchearlo con validación dispersa; con el enumerado no existe —
  la regla es el TIPO. `DROP_CREATE` y `CREATE_IF_NOT_EXISTS` NO son redundantes: son las dos
  idempotencias incompatibles entre sí que la gente quiere. `DROP_CREATE` exige
  `confirm_scope_drop` == nombre real.
- **Estructura y datos son DOS conjuntos** con restricción `data ⊆ selection` verificada en el
  servidor (422 `export.data_without_structure`), con **una excepción decidida**: con ambos
  `*_ddl` en `NONE` la exportación es "solo datos" y la restricción no aplica — es la ÚNICA
  forma en que csv/json/ndjson pueden existir. Los datos solo salen de TABLAS.
- **Consistencia ASIMÉTRICA por motor (§6.2, no es un bug)**: PG cubre catálogo y datos
  (`execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)` + un
  `SELECT 1` que fuerza el snapshot — **NO un `BEGIN` crudo**: psycopg abre la tx al primer
  execute y un `BEGIN` nuestro llegaría segundo, el servidor lo ignoraría con un aviso y la
  sesión quedaría en READ COMMITTED **sin que nada fallara**). MySQL/MariaDB cubre **solo
  datos**: el snapshot de InnoDB es MVCC de FILAS y el diccionario de datos no participa;
  congelarlo exigiría `FLUSH TABLES WITH READ LOCK`, que bloquea el servidor entero (misma
  limitación de `mysqldump --single-transaction`). Se reporta con un aviso en el preview y con
  `structure_drift_detected` (el fingerprint se re-verifica AL TERMINAR, dentro de la sesión).
  La familia MySQL se abre en **3 pasos** (`SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE
  READ` → `SET SESSION TRANSACTION READ ONLY` → `START TRANSACTION WITH CONSISTENT SNAPSHOT`) y
  no con la lista separada por coma, que no está soportada de forma uniforme.
- **El export NO toma el advisory lock, y es deliberado**: es de solo lectura, no hay nada que
  serializar, y sostener el lock durante horas bloquearía clones/conversiones/migraciones sobre
  esa BD sin ganancia — la consistencia la da el MVCC, que es otro mecanismo (y el lock tampoco
  arreglaría el hueco de MySQL, que es de diccionario de datos). Solo se toma el guard
  **in-process** `export_runner.database_guard`. Está anotado en el docstring del runner: quien
  "arregle" esto agregando el lock introduce un bloqueo de horas sobre la base de un tercero.
- **Inyección de conexión en `ServerAdapter`** (el cambio caro, toca código compartido por 5
  features): `base_adapter._conn_ctx(database, conn)` + `conn: Connection | None = None`
  keyword-only en `structural_snapshot`, `dump_structure`, `list_tables` y `list_table_stats`.
  Con `conn` dado **no se cierra la conexión ni se toca su nivel de aislamiento** (revertiría la
  tx REPEATABLE READ del job). Aditivo: los 5 consumidores llaman posicionalmente y siguen igual.
- **`idle_in_transaction_session_timeout` desacoplado SIN tocar `remote_engine`**: se fija con
  un `SET` de SESIÓN sobre la conexión propia (`EXPORT_IDLE_TRANSACTION_TIMEOUT_MS`, 5 min), que
  gana sobre el `-c` de la URL. Cambiar los `connect_args` habría metido otro eje en la clave del
  cache de engines y habría afectado a los otros 5 consumidores para resolver el problema de uno.
  Best-effort: si el motor lo rechaza, va a `progress.degradations`.
- **Rutinas ANTES que vistas** en el orden de emisión (el §8.4 del diseño las tenía al revés):
  en PG una vista que llama a una función se valida al crearse. Se reusa la ÚNICA fuente del
  repo (`schema_diff._BODY_TYPE_ORDER`/`snapshot_layout._CLASS_ORDER`) — mantener dos órdenes
  para lo mismo es cómo se reintrodujo ese bug la vez anterior. El orden fino lo calcula
  `_order_for_emission` (`_STEP` + topológico) y **el writer no reordena nada**: emite lo que el
  preview congeló y el `confirm_token` hasheó.
- **Determinismo (§8.3) es requisito, no adorno**: con `script_comments:false` dos corridas dan
  el artefacto byte a byte idéntico (los metadatos volátiles viven en el manifiesto, y las
  entradas del zip llevan fecha fija 1980-01-01). `resolve_selection` usa `fnmatchcase` y NO
  `fnmatch` a propósito: éste aplica `os.path.normcase` y en Windows el MISMO spec resolvería
  distinto según el SO. Tabla sin PK → se ordena por la tupla completa de columnas si todas son
  ordenables; si no (`blob`/`text`/`json`/`bytea`/`tsvector`, y en PG además `point`/`polygon`/
  `box`/`line`, que **no tienen operador de orden** y harían FALLAR la consulta), sale
  `deterministic:false` + aviso. Fail-closed: fingir determinismo sería mentir.
- **Lo que NO se emite, a propósito**: `SET session_replication_role='replica'` en PG (exige
  superusuario y abortaría el script con `ON_ERROR_STOP`; el default
  `constraints_placement='deferred'` ya pone índices y FKs después de los datos).
  `scope_ddl='CREATE_IF_NOT_EXISTS'` es **422 en PG** (`CREATE DATABASE IF NOT EXISTS` no existe);
  `entity_ddl='CREATE_IF_NOT_EXISTS'` es best-effort por tipo (`export_make_idempotent` → `None`
  ⇒ CREATE normal + aviso: el `IF NOT EXISTS` de rutinas/triggers depende de la VERSIÓN del motor
  DESTINO, que el gateway no conoce). `insert_variant='replace'` es 422 en PG.
- **`sanitize.definer` tiene un 4º valor `auto` (default)**: resuelve a `omit` en la familia
  MySQL y a `keep` en PG. Sin él, la llamada canónica (`{}`) daba 422 contra PG, donde la matriz
  prohíbe `omit`/`replace` (allá el DEFINER no es el mismo concepto: propiedad del objeto y
  `SECURITY DEFINER` son mecanismos distintos, `applicable:false` en capabilities). **`keep` hoy
  es indistinguible de `omit`**: los cuerpos del `SchemaSnapshot` ya vienen sin DEFINER
  (`_strip_definer_clause` corre al capturarlos); se implementa igual para el día que haya una
  fuente que sí lo traiga. Implicación de seguridad: un volcado que conserva
  ``DEFINER=`root`@`localhost`` crea en el destino objetos que corren como root.
- **`script_comments` y `object_comments` son opciones SEPARADAS**: la primera es el encabezado
  y los separadores del SCRIPT (apagarla es lo que da el determinismo); la segunda son los
  `COMMENT` del ESQUEMA, que son parte de la definición y perderlos es pérdida de información.
- **La matriz de compatibilidad se PUBLICA y se HACE CUMPLIR con la misma estructura**
  (`compatibility_matrix()` / `validate_compatibility()`): publicar una promesa que el servidor
  no cumple es peor que no publicarla. `_NEUTRAL` define el valor neutro de cada opción,
  `structure.*` es comodín, `"ruta=valor"` prohíbe un valor concreto. **Multiarchivo ⇒ zip
  SIEMPRE** (`effective_compression` eleva `none`→`zip`; eso NO es 422, es una resolución con
  aviso — pero `gzip`+multiarchivo SÍ es 422, porque ahí el usuario tiene alternativa).
- **El `where` por objeto es la única entrada libre que roza una consulta**: se arma la consulta
  REAL, se clasifica con `query_policy.classify_statement` (debe dar `read`, fail-closed), se
  rechazan subconsultas/CTEs/UNION y el conjunto de tablas del AST debe ser EXACTAMENTE `{t}`
  (más ninguna columna calificada con otra base/tabla) → 422 `export.invalid_row_filter` antes
  de tocar el motor, con `reason` de vocabulario cerrado y **sin devolver el texto del filtro**
  (regla anti-reflexión). `filename_template` tiene whitelist de 5 tokens y el nombre lo sanea
  el servidor (incluido el valor sustituido).
- **Entrega**: `FileResponse` y no `StreamingResponse` porque la Starlette del repo (0.50) ya
  implementa `Range` ahí. **Un solo uso** (`EXPORT_SINGLE_USE_DOWNLOAD`) — pero una descarga
  GENUINAMENTE parcial NO consume, o se rompería la reanudación que el `Range` habilita. Lo
  decide la COBERTURA del rango (`_range_covers_whole_file`, pura, fail-closed hacia no borrar),
  no la presencia de la cabecera: con esa lectura ingenua un `Range: bytes=0-` bajaba el archivo
  entero y lo dejaba disponible — el "un solo uso" se anulaba con una cabecera. El
  `download_count` se incrementa SIEMPRE.
  Inline: 409 `export.inline_too_large` accionable, **nunca truncar en silencio**. TTL 30 min +
  purga periódica en el `lifespan` (`asyncio.to_thread`; hacerlo solo en el arranque volvería el
  TTL una promesa falsa — mismo error ya corregido en las capturas de SELECT) + barrido de
  huérfanos al arrancar (sin él, un `kill -9` deja artefactos sensibles en disco para siempre).
  Directorio 0700 en volumen PROPIO `exports_data:/app/exports`, nunca el de uploads.
- **Auditoría**: `database_export.plan` (`record`, **`touched_engine=True`** — el flag significa
  "contactó el motor", no "lo mutó", y el plan snapshotea en vivo), `.execute` (`record_intent`
  fail-closed ANTES de encolar + `record` al terminar), `.download` (`record_intent`
  fail-closed, `touched_engine=False`, **antes de que salga un solo byte** — mismo criterio que
  `reveal_password`: una exportación de datos es una DIVULGACIÓN, no una lectura más).
- **RIESGO ACEPTADO EXPLÍCITO: no hay enmascarado.** El módulo permite extraer datos personales
  o regulados EN CLARO y no ofrece ningún control técnico para evitarlo. El único control
  compensatorio en pie es la auditoría, que por eso no es negociable. `EXPORT_ENABLED=False` es
  el kill switch (409 en todos los endpoints, y el worker lo re-comprueba al arrancar el job: la
  vía de salida tiene que poder cerrarse sin esperar a que la cola se vacíe). Si el gateway pasa
  a tratar datos regulados, esto es un **bloqueante de cumplimiento**.
- **Limitaciones conocidas**: (1) estructura no consistente en MySQL/MariaDB; (2) los cuerpos
  salen SIN el calificador de la base de origen (era una FUGA, corregida — ver abajo); (3) el
  artefacto de PG con `DROP_CREATE` no es ejecutable de un tirón (falta el `\connect` que emite
  `pg_dump --create`; el preview lo avisa); (4) las **particiones no viajan** (el
  `SchemaSnapshot` no captura `PARTITION BY`) → aviso en vez de degradación silenciosa; (5) **no
  se emite `REFRESH MATERIALIZED VIEW`**; (6) los jobs NO son durables (worker in-process);
  (7) PG solo schema `public`; (8) **3ª copia del runner + 2 vocabularios de ítem divergentes**
  (`applied/failed/skipped` del clon vs `ok/error/skipped` de collation y export) = deuda
  anotada, el 4º módulo de la familia debería unificarlos.
- **Verificado** (ejecución directa, sin `pytest`): **87 checks HTTP** de extremo a extremo
  (`tests/test_api_database_exports.py`, TestClient+SQLite+adapter falso), **27 checks de ciclo
  real** con el writer real y `MySQLAdapter` real sin motor (execute → run_job → artefacto en
  disco → descarga con y sin `Range` → hook `counter_value` → purga por TTL → barrido de
  huérfanos), **81** del writer, **96** del spec, **23** de literales, **17** de endurecimiento
  (`tests/test_export_hardening.py`), **89** de `query_policy` y **18** de
  `query_console_security` sin regresión, y la migración con ciclo upgrade/downgrade/upgrade en
  SQLite.
- **PENDIENTE — NADA se probó contra motores reales**: `scripts/verify_export_e2e.py` está
  **escrito pero NUNCA EJECUTADO** (sin Docker; mismo caso que `verify_query_console_e2e.py`).
  Lo que solo un motor real puede confirmar y por lo tanto NO está verificado: (a) que el
  artefacto **se ejecute** y el esquema resultante coincida con el origen vía `diff_snapshots`
  —la prueba de aceptación principal—; (b) que la transacción de `export_session` se abra DE
  VERDAD (3 pasos de MySQL y `postgresql_readonly` de psycopg) y que el `SET
  idle_in_transaction_session_timeout` sea aceptado; (c) `export_counter_value_sql`
  (`information_schema.TABLES.AUTO_INCREMENT` y **`pg_sequence_last_value`, un builtin NO
  documentado**); (d) el determinismo byte a byte contra un motor; (e) que un csv y un ndjson se
  REIMPORTEN; (f) los valores límite (binario con `\x00`, fechas extremas, `Decimal` de precisión
  arbitraria, multibyte); (g) que quitar el calificador propio del cuerpo (fix de la fuga) produzca
  objetos que el motor acepte al restaurar en una base con OTRO nombre. Tampoco se verificó la migración contra la BD del gateway real ni el
  consumo de memoria plano / la cancelación con una tabla de millones de filas.
  **`.env.example` NO se pudo actualizar** (permisos del entorno): las 18 variables `EXPORT_*`
  están documentadas en `app/core/environments.py` y en `docs/features/database-export.md`.

**Revisión de seguridad post-implementación (2026-08-16) — 3 bloqueantes + 5 recomendaciones +
1 fuga, TODOS aplicados** (cada uno con test de regresión que falla sin el fix, verificado):

**(B1a) `/*M!` de MariaDB evadía la blocklist ENTERA — y esto golpeaba a la CONSOLA SQL, no solo
al export.** `query_policy._scan_normalize` reconocía el comentario ejecutable `/*!` de MySQL
pero **no `/*M!`**, el prefijo exclusivo de MariaDB: su contenido se descartaba como comentario
común, la blocklist nunca lo veía y **el motor lo ejecutaba igual**. Con la credencial
pseudo-root, `SELECT a FROM t WHERE 1=1 /*M!100000 INTO OUTFILE '/tmp/x' */` es escritura de
archivo arbitraria en el host de la base del cliente, y salía clasificado `read` (sin
confirmación ni auditoría). FIX: `_EXECUTABLE_COMMENT_PREFIXES = ("/*M!", "/*m!", "/*!")` +
`_executable_comment_prefix` (devuelve el LARGO para que el llamador salte 3 o 4 según el
prefijo, sin duplicar la tabla). Se reconoce en los **tres** motores a propósito (fail-closed: un
MariaDB dado de alta como `mysql` es un error de inventario frecuente, y conservar texto de más
solo sobre-bloquea). El número de versión se sigue descartando: si quedara, desplazaría los
patrones anclados en `^` y la evasión seguiría por otra puerta.

**(B1b) el filtro de filas confiaba en que sqlglot y el motor coincidieran en qué es código — y
no coinciden.** `export_spec.validate_row_filter` funda su garantía central ("el conjunto de
tablas del AST debe ser EXACTAMENTE `{t}`", sin subconsultas/CTE/UNION) en el árbol de sqlglot,
que **no tokeniza el contenido de un `/*! */`**. Pasaban
`1=1 /*!50000 UNION SELECT user,authentication_string FROM mysql.user */` y
`1=1 /*M!100000 INTO OUTFILE …*/` con un árbol que solo veía `1=1`. FIX: se rechaza **cualquier**
token de comentario (`--`, `/*`, `*/`, y `#` solo en la familia MySQL — en PostgreSQL es el XOR
de enteros y prohibirlo sería prohibir un operador legítimo; mismo matiz que `_scan_normalize`),
`reason='comment_not_allowed'`. Un filtro de exportación no tiene ningún uso legítimo para un
comentario, así que la validación deja de depender de un empate entre parser y motor.

**(B2) la cadena VALIDADA no era la cadena EJECUTADA.** El validador armaba
`SELECT … FROM t WHERE {text}` (un PREFIJO) y `export_writer._select_sql` construía
`… WHERE {where} ORDER BY … LIMIT …` **en una sola línea**. Con `where = "1=1 -- "` o `"1=1 #"`
el `ORDER BY` y el `LIMIT` **quedaban comentados**: la tabla salía ENTERA ignorando el `limit`
que el operador confirmó y que el `confirm_token` hasheó, `ensure_capacity` quedaba basado en una
estimación falsa y el manifiesto afirmaba `deterministic: true` sobre un volcado sin orden. FIX:
constructor **ÚNICO** `export_spec.build_row_select_sql` que llaman validador y writer, filtro
entre **paréntesis**, y se valida la cadena **FINAL** (con `ORDER BY` y `LIMIT`). El controller
pasa el `order_by` del adapter y el `limit` del `RowFilter`, y usa las columnas INSERTABLES
(sin generadas) igual que el writer. Dos defensas independientes: aunque B1b ya prohíbe el
comentario, los paréntesis impiden que un `OR` suelto se coma la precedencia de la cola.

**(B3) se podía exportar la propia base de metadatos del gateway.** `_validate_scope` solo hacía
`validate_identifier` + `ensure_not_reserved_database`; nada impedía apuntar el export a la BD
del gateway si su servidor está en el inventario. El artefacto se llevaría `servers` (incluido
`root_password_encrypted`), `server_users`, el **`audit_log` completo** —que es el único control
compensatorio que declara el §9.6— y `migration_select_results`. FIX: se reusa el guard que ya
usa la consola SQL, `query_policy.is_gateway_metadata_target` (resuelve ambos hosts a IPs e
interseca, así que registrar la IP en vez del hostname no lo evade) → **409
`export.scope_not_allowed`** (código nuevo en `ERROR_CODES`). El parámetro `target` de
`_validate_scope` es **obligatorio, sin default**: con un default, un llamador nuevo se saltearía
el guard en silencio, que es justo el modo de fallo que esto corrige. El guard es por BASE, no
por servidor.

**(FUGA) el export no re-calificaba el esquema en los cuerpos.** En MySQL/MariaDB
`VIEW_DEFINITION` devuelve el cuerpo con la BD de origen calificada
(`` select `origen`.`t`.`c` from `origen`.`t` ``), así que restaurar el artefacto en una base con
**otro nombre** dejaba las vistas/rutinas leyendo de la base de **ORIGEN**, en silencio y con los
permisos de quien restaure. El clon resuelve lo mismo RE-CALIFICANDO (`_requalify_body`) porque
ahí el destino se conoce; en un export **no se conoce**, así que la respuesta correcta es
**QUITAR** el calificador propio con `sql_dialect.strip_self_schema_qualifier` — sin él el motor
resuelve contra la base activa de quien ejecuta el script, que es la semántica esperada de un
volcado y el criterio de `mysqldump`. FIX: `export_writer._strip_own_schema`, aplicado en
`_render_plan` **solo a `_BODY_TYPES`** (vista/matview/rutina/trigger/evento). Una referencia a
OTRA base se conserva (es parte de la definición) y PostgreSQL no se toca (sus cuerpos no llevan
el nombre de la BASE; el ESQUEMA sí es parte de la definición).

**Recomendaciones de la misma pasada**: **(R1)** `Range` anulaba el "un solo uso" — ver
"Entrega" arriba. **(R2)** `preview` sobre un job ya ejecutado sobrescribía
`spec`/`resolved_selection`/`fingerprint`/`token` y el `GET /manifest` dejaba de describir el
artefacto entregado → `_guard_still_pending` (comparte el código `export.already_executed` con
`execute`, que ahora también lo usa). **(R3)** `Connection.execution_options()` muta la conexión
**in-place** (a diferencia de `Engine.execution_options()`) y la conexión del export vive el job
entero, así que `stream_results` quedaba pegado al re-snapshot final de drift y a
`counter_value` — el fallo documentado en `query_runner:42-48`, donde psycopg compone un cursor
con nombre como `DECLARE … CURSOR FOR <stmt>` y solo acepta consultas; ahora las opciones van
**por sentencia** (`text(sql).execution_options(...)`). **(R4)** `output.file_encoding` aceptaba
cualquier cadena: como se codifica **por trozo**, `utf-16` incrusta un BOM en cada `write` y el
artefacto sale corrupto mientras el sha256 lo declara íntegro → whitelist
(`ALLOWED_FILE_ENCODINGS` + alias) evaluada por la matriz, con el código estable
`export.incompatible_option`. **(R5)** `_guard_concurrency` contaba solo `running`: con un worker
el techo era 1 y la COLA quedaba sin acotar → `max(running_en_BD,
export_runner.inflight_count())`. **NO** se cuenta `pending` de la base: incluye los planes
creados y nunca ejecutados, que no son trabajo admitido y habrían bloqueado el endpoint por
planes viejos que nadie va a lanzar; el contador in-flight es exacto, no necesita migración y
un `submit` fallido lo descuenta.

**Follow-up (NO aplicados)**: **R6** el `confirm_token` no incluye `job_id` en su `subject`;
**R7** `_guard_owner` solo corre en la descarga; **R8** `_render_plan` materializa TODO el DDL en
memoria antes de emitir; **R9** `preview`/`objects` no auditan.

## Proyectos — agrupación de blueprints

Entidad **deliberadamente vacía** (nombre + descripción larga) cuyo único fin es agrupar
blueprints (`DatabaseModel`). Guía de uso: `docs/features/projects.md`.

- **Relación N:M y OPCIONAL en los dos sentidos**, en tabla pivote `project_database_models`
  con **PK compuesta** `(project_id, model_id)`. Una columna `project_id` en `database_models`
  habría forzado "un blueprint, un proyecto" y habría que migrarla el primer día que dos
  iniciativas compartan una base — que es el caso normal ("Citas" con 2 BDs, "Omnicanal" con
  4). Con PK compuesta un vínculo duplicado es imposible incluso ante un bug del controller.
- **REGLA DURA: borrar un proyecto borra la entidad y sus VÍNCULOS, nunca los blueprints**
  (y al revés: borrar un blueprint suelta su pertenencia, no borra proyectos). Un blueprint es
  el esquema que replican N BDs reales con datos reales; que un AGRUPADOR pueda arrastrarlas
  sería pérdida de datos por una operación de organización. Tres capas: los dos
  `ondelete="CASCADE"` apuntan al VÍNCULO; el controller borra los vínculos **explícitamente**
  en la misma transacción (**no es redundante**: SQLite no aplica FKs sin
  `PRAGMA foreign_keys`, así que en test el cascade del motor no dispara y quedarían filas
  huérfanas — por eso `DatabaseModelController.delete_model` también los limpia); y los tests
  `test_delete_project_keeps_blueprints` / `test_delete_blueprint_keeps_projects`. Por lo mismo
  el borrado NO pide `confirm_target_name`/`confirm_token`: no se pierde nada irrecuperable.
- **Endpoints** (`app/routes/v1/projects.py`, dos routers: `/projects` y `/database-models`
  para la vista inversa, precedente de `model_migrations.py`): `GET|POST /projects`,
  `GET|PATCH|DELETE /projects/{id}`, `GET|POST /projects/{id}/blueprints`,
  `DELETE /projects/{id}/blueprints/{model_id}`, `GET /database-models/{id}/projects`.
- **Vincular es IDEMPOTENTE y TODO-O-NADA**: un blueprint ya vinculado sale en
  `already_linked` con 200 (reenviar la selección completa desde la UI es la operación
  natural y no debe fallar); un id inexistente da **422** con `missing_model_ids` y **no
  vincula ninguno** — misma política de selección explícita que schema-comparisons.
- **Gotchas**: el tope de 5000 caracteres vive en el schema (`DESCRIPTION_MAX_LENGTH`), no en
  la columna, que es `Text` — subirlo no requiere migración, y un `VARCHAR(5000)` consumiría
  presupuesto del límite de 65 535 bytes por fila de MySQL/MariaDB (4 bytes por carácter con
  `utf8mb4`). `PATCH` asigna `description` por **presencia** de la clave (`null` la limpia; es
  la única forma de distinguirlo de "no enviado"), y `name` solo si no es `null`.
  `blueprint_count` sale de UNA query con `GROUP BY` para toda la página (por fila serían 21
  consultas para 20 proyectos). `GET /{id}/blueprints` no está paginado a propósito.
  `name` es **único** (409). Ninguna operación abre conexión a un motor
  (`touched_engine=False` en las 5 acciones auditadas `project.*`).
- **Verificado**: 22 checks HTTP por ejecución directa (TestClient+SQLite, sin `pytest`) +
  ciclo upgrade/downgrade/upgrade de la migración `d8e9f0a1b2c3` en SQLite + `alembic check`
  sin drift para las dos tablas nuevas + no-regresión en `test_api_database_models.py` (6).
  **Pendiente**: la migración contra la BD del gateway real.

## Perfiles de permisos: familia MySQL↔MariaDB y fallo silencioso de `apply-profile`

Guía de uso: `docs/features/permissions.md` (§ apply-profile) y `docs/api-reference-v2.md`
(§4 y §6).

**El filtro `?engine=` de `GET /permission-profiles` era IGUALDAD EXACTA (fix, 2026-08-04)**:
un perfil creado para `mariadb` no aparecía al pedir `?engine=mysql&active=true`, así que el
selector «Aplicar perfil» del frontend quedaba **vacío sin explicación** pese a ser un perfil
perfectamente aplicable. Y aunque apareciera, `apply_profile` lo rechazaba con **422** por lo
mismo. La noción de familia YA existía en el catálogo de privilegios
(`privileges.py::_MYSQL_FAMILY`/`_family`) pero **no** en perfiles. FIX: helpers públicos
`privileges.family_members`/`same_family`/`tokens_valid_for` (versión booleana de
`validate_privileges`, mismo criterio vía `classify`, fail-closed) + `list_profiles` filtra
por `engine.in_(familia)` y revalida **token a token** contra el motor pedido. **NO es
"aflojar el filtro"**: la pertenencia a la familia solo habilita el chequeo; lo que decide es
que cada privilegio sea otorgable allí. La asimetría real es UNA (`DELETE HISTORY`, exclusivo
de MariaDB — `_MARIADB_TABLE_EXTRA`), pero el chequeo es por token para no hardcodearla y
para que agregar un extra al catálogo no reabra el agujero. `?exact_engine=true` recupera la
igualdad estricta; PostgreSQL nunca se mezcla (`family_members('postgresql')` es un
singleton). `apply_profile` acepta el cruce con la misma regla y **recanonicaliza los tokens
contra el motor del SERVIDOR**, no el del perfil (si no, un token válido solo en el origen
llegaría crudo al `GRANT`).

**`apply-profile` mentía cuando no aplicaba nada (mismo fix)**: devolvía **200** con
`grants_applied: 0` si ningún nivel del perfil tenía `object_mapping` o si todos los GRANT
fallaban — el usuario veía éxito y ningún permiso nuevo. Ahora eso es **422** enumerando
`skipped_levels`/`errors` en el **`message`** (no solo en `context`, que únicamente se ve en
`development`). El caso **parcial** sigue siendo 200 con `errors[]` poblado, a propósito: el
endpoint es best-effort por diseño. La auditoría (`server_user.apply_profile`) se registra
**antes** de lanzar el 422 — el intento ocurrió y debe dejar rastro. Además: un perfil con
`is_active=false` ahora da **409** (antes se aplicaba igual, contradiciendo el `?active=true`
con el que la UI los ofrece), y `errors[]` ya **no transcribe `str(exc)` del motor** (podía
filtrar hosts/nombres internos/fragmentos de sentencia): el detalle va a `logger.exception`
con el Request ID. Criterio R4 de gateway-security, ya aplicado en usuarios del motor.

`PermissionProfileCreate` ahora acepta **`is_active` opcional (default `true`)**: el
controller ya hacía `data.get("is_active", True)`, pero el schema no lo admitía y el switch
«Activo» del formulario nunca se enviaba. Hipótesis descartadas por código durante el
diagnóstico, útiles para no re-investigarlas: `is_active` **nunca** nace `false`/`NULL`
(`default=True` + `server_default="1"` NOT NULL); el query param **se llama `active`**, no
`is_active`; el listado **no es paginado** (`success(data=[...])`); `created_at`/`updated_at`
no pueden salir `null` (`TimestampMixin`, NOT NULL desde el INSERT). Gotcha para el frontend:
el nivel **`global` no existe** en `_ALLOW` para ningún motor → un item `level:"global"` da
422 al crear el perfil.

Verificado con script puntual por HTTP (TestClient + SQLite, adapter mockeado): **21 checks /
0 fallos**, incluida la reproducción del escenario original (perfil `mariadb` + servidor
`mysql`) y la matriz de familia. Tests agregados en `tests/test_api_permission_profiles.py` y
`tests/test_grant_guards.py` — **no ejecutados con `pytest`** (regla del proyecto).
**Pendiente**: correr esos tests y verificar el cruce de familia contra MySQL y MariaDB
reales (sin Docker en el entorno donde se implementó).

## Documentación

- `docs/features/projects.md` — proyectos (agrupación N:M de blueprints)
- `docs/` — documentación completa por feature (ver `docs/features/model-migrations.md`
  para migraciones de blueprints; `docs/features/server-database-lifecycle.md` para
  crear/borrar BDs a nivel servidor y listar usuarios con permisos sobre una BD;
  `docs/features/database-export.md` + `docs/api-reference-v10.md` para la exportación de BDs)
- `docs/docker-deployment.md` — despliegue Docker en VPS plano (`docker-compose.yml`, nginx + Certbot propios)
- `docs/dokploy-deployment.md` — despliegue en Dokploy (`docker-compose.dokploy.yml`, sin nginx: usa el Traefik propio de Dokploy)
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

**Nota para Agentes**: Mantén consistencia con la arquitectura existente. Todo endpoint debe usar `ApiResponse[T]`. Todo error controlado debe usar `AppHttpException`. Consulta `docs/` para detalles de cada feature. **No ejecutes `pytest` salvo pedido explícito del usuario** (ver "Ejecución de tests (pytest) — NO por defecto" más arriba).
