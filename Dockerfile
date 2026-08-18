# ─────────────────────────────────────────────────────────────────────────────
# Builder: instala dependencias con uv y genera el entorno virtual
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

# Copiar binario de uv desde la imagen oficial (más rápido que pip install uv)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Evitar que uv descargue Python (usamos el del sistema base)
# UV_LINK_MODE=copy es necesario en Docker (no hay hardlinks entre capas)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Instalar dependencias primero (esta capa se cachea si pyproject.toml/uv.lock no cambian)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copiar el código fuente e instalar el proyecto
COPY . .
RUN uv sync --frozen --no-dev


# ─────────────────────────────────────────────────────────────────────────────
# Production: imagen final mínima y segura
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS production

# uv disponible en producción para comandos de entorno (alembic, etc.)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Dependencias de sistema mínimas (curl para el healthcheck, gosu para que el
# entrypoint pueda arrancar como root y bajar privilegios a appuser)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root para mayor seguridad
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Copiar entorno virtual y código desde el builder (con ownership correcto)
COPY --from=builder --chown=appuser:appuser /app /app

# Hacer ejecutable el entrypoint
RUN chmod +x /app/docker/scripts/entrypoint.sh

# Directorio de artefactos de exportación (volumen exports_data): se crea con el
# ownership correcto ANTES de montar el volumen encima. Docker copia el
# contenido/ownership de este directorio al crear el volumen nombrado por
# primera vez; sin esto queda root:root y appuser no puede escribir ahí.
RUN mkdir -p /app/exports && chown appuser:appuser /app/exports && chmod 0700 /app/exports

# Sin USER acá a propósito: el contenedor arranca como root para que el
# entrypoint pueda corregir el ownership de volúmenes YA EXISTENTES (creados
# antes de este fix, o con otro ownership por cualquier motivo) antes de bajar
# privilegios a appuser con gosu. Ver docker/scripts/entrypoint.sh.

# Virtual env en PATH para ejecutar uvicorn/alembic directamente
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app" \
    UV_PYTHON_DOWNLOADS=0

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/docker/scripts/entrypoint.sh"]
