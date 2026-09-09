# Guía de Inicio Rápido

Esta guía te lleva desde cero hasta tener el proyecto corriendo con todas sus funcionalidades.

## Requisitos Previos

- **Python 3.13+** — `python --version`
- **Git** — para clonar el repositorio
- **uv** — gestor de paquetes (se instala en el paso 1)
- **Base de datos** — SQLite (incluido), MySQL/MariaDB 5.7+, o PostgreSQL (opcionales)

---

## Paso 1: Instalar uv

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Verificar
uv --version
```

## Paso 2: Clonar el Proyecto

```bash
git clone <url-de-tu-repositorio>
cd fastapi-template
```

**Si inicias un proyecto nuevo desde esta plantilla:**

```bash
rm -rf .git
git init
git add .
git commit -m "Initial commit from fastapi-template"
```

## Paso 3: Instalar Dependencias

```bash
uv sync
```

`uv sync` crea automáticamente `.venv/` e instala todo lo declarado en `pyproject.toml`.

## Paso 4: Configurar Variables de Entorno

```bash
cp .env.example .env
```

Editar `.env` con tus valores. Variables mínimas para arrancar:

```env
# Aplicación
APP_ENV=development
APP_NAME="Mi Proyecto"
SECRET_KEY=genera_una_clave_aqui

# Base de datos (SQLite por defecto — no requiere configuración extra)
DB_ENGINE=sqlite
DB_NAME=development

# CORS — cambia * por tus orígenes en producción
CORS_ORIGINS=*
```

**Generar SECRET_KEY:**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Referencia completa de variables

```env
# ======= Application =======
APP_ENV=development          # development | production
APP_NAME="FastAPI Project"   # Nombre que aparece en los docs de Swagger
SECRET_KEY=...               # Clave secreta para JWT y cifrado

# ======= Logger =======
LOGGER_LEVEL=INFO                        # DEBUG | INFO | WARNING | ERROR
LOGGER_MIDDLEWARE_ENABLED=True           # Activar/desactivar logging de requests
LOGGER_MIDDLEWARE_SHOW_HEADERS=False     # Mostrar headers en logs
LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS=True # Mostrar query params en logs
LOGGER_MIDDLEWARE_SHOW_BODY=True         # Mostrar body en logs
LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS=True  # False = reemplaza URL con template de ruta
LOGGER_EXCEPTIONS_ENABLED=False         # Loguear excepciones capturadas

# ======= Docs =======
DOCS_ENABLED=True            # False = desactiva /docs, /redoc y /openapi.json

# ======= Rate Limiting =======
RATE_LIMIT_DEFAULT=100/minute  # Límite global por IP. Formato: {n}/{second|minute|hour|day}

# ======= Pagination =======
PAGINATION_MAX_SIZE=50       # Máx items/página. Hard cap en código: 200.

# ======= Request Size =======
REQUEST_MAX_SIZE_MB=10       # Tamaño máximo del body en MB (POST, PUT, PATCH)

# ======= CORS =======
# Orígenes separados por coma.
# IMPORTANTE: "*" es incompatible con allow_credentials=True en browsers.
# En producción: https://app.com,https://admin.app.com
CORS_ORIGINS=*

# ======= Database =======
DB_ENGINE=sqlite             # sqlite | mysql+pymysql | postgresql+psycopg2
DB_HOST=localhost
DB_USER=username
DB_PASS=password
DB_NAME=database
DB_PORT=3306
```

## Paso 5: Configurar Base de Datos

### Opción A — SQLite (recomendado para desarrollo)

No requiere instalación adicional. Solo asegúrate de tener en `.env`:

```env
DB_ENGINE=sqlite
DB_NAME=development
```

```bash
uv run alembic upgrade head
```

### Opción B — MySQL / MariaDB

```bash
# Crear base de datos
mysql -u root -p -e "
  CREATE DATABASE nombre_bd CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
  CREATE USER 'usuario'@'localhost' IDENTIFIED BY 'contraseña';
  GRANT ALL PRIVILEGES ON nombre_bd.* TO 'usuario'@'localhost';
  FLUSH PRIVILEGES;
"
```

En `.env`:
```env
DB_ENGINE=mysql+pymysql
DB_HOST=localhost
DB_USER=usuario
DB_PASS=contraseña
DB_NAME=nombre_bd
DB_PORT=3306
```

```bash
uv run alembic upgrade head
```

### Opción C — PostgreSQL

```bash
uv add psycopg2-binary
```

En `.env`:
```env
DB_ENGINE=postgresql+psycopg2
DB_HOST=localhost
DB_USER=usuario
DB_PASS=contraseña
DB_NAME=nombre_bd
DB_PORT=5432
```

## Paso 6: Ejecutar

```bash
# Desarrollo con hot-reload
uv run uvicorn main:app --reload

# Puerto y host específicos
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8080

# Producción (múltiples workers)
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Paso 7: Verificar

| URL | Qué verificar |
|---|---|
| `http://localhost:8000/health` | Retorna `{"status": "ok", ...}` |
| `http://localhost:8000/api/v1/docs` | Swagger UI de v1 |

**Ejemplo de log en consola (si LOGGER_MIDDLEWARE_ENABLED=True):**
```
2026-03-08 10:30:15 [INFO] a1b2c3d4 | Host: 127.0.0.1 | Request: GET /health
2026-03-08 10:30:15 [INFO] a1b2c3d4 | Host: 127.0.0.1 | Response: GET /health | Status: 200 | Duration: 0.003s
```

---

## Solución de Problemas

### `ModuleNotFoundError: No module named 'app'`

```bash
# Siempre usar uv run
uv run uvicorn main:app --reload
# No: python main.py
```

### `Access denied for user ...` (MySQL)

Verificar que `.env` tenga los valores correctos y que el usuario tenga permisos en la BD.

### `Can't locate revision identified by '...'` (Alembic)

```bash
uv run alembic stamp head   # Marcar estado actual
uv run alembic upgrade head # Aplicar migraciones
```

### Puerto 8000 en uso

```bash
# Linux / macOS
lsof -ti:8000 | xargs kill -9

# Windows (PowerShell)
Get-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess | Stop-Process
```

---

## Próximos Pasos

1. Leer [Estructura del Proyecto](project-structure.md)
2. Entender el [Versionado de API](features/api-versioning.md)
3. Ver cómo usar las [Respuestas Estándar](features/response-format.md)
4. Agregar tus propios modelos y endpoints
