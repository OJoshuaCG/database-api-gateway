from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import remote_engine
from app.core.auth import bootstrap_admin
from app.core.versioned_app import create_versioned_app
from app.routes.health import router as health_router
from app.routes.v1.routes import router as v1_router
from app.services.privilege_catalog import seed_privileges


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Arranque: sembrar el administrador único y el catálogo de privilegios.
    bootstrap_admin()
    seed_privileges()
    # Asegurar una DEK persistida (envelope encryption) en sistema fresco; idempotente.
    from app.core import crypto as _crypto

    _crypto.bootstrap_dek()
    yield
    # Apagado: liberar los engines de conexión a servidores destino.
    remote_engine.dispose_all()


# === Main app
# Solo gestiona rutas no versionadas (/health).
# No tiene docs propios ni middlewares; cada sub-app versionada
# es autocontenida con su propia configuración.
app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.include_router(health_router)

# === API v1
# Docs disponibles en /api/v1/docs y /api/v1/redoc
v1_app = create_versioned_app("v1")
v1_app.include_router(v1_router)
app.mount("/api/v1", v1_app)

# === API v2 (ejemplo — descomentar cuando sea necesario)
# from app.routes.v2.routes import router as v2_router
# v2_app = create_versioned_app("v2")
# v2_app.include_router(v2_router)
# app.mount("/api/v2", v2_app)
