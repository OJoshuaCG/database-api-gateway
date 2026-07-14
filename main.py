from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import remote_engine
from app.core.auth import bootstrap_admin
from app.core.environments import CORS_ORIGINS
from app.core.versioned_app import (
    PathScopedCORSMiddleware,
    cors_allow_credentials,
    create_versioned_app,
)
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
# Solo gestiona rutas no versionadas (/health). No tiene docs propios ni el resto de los
# middlewares (rate limiting, sesión, tamaño de request); cada sub-app versionada es
# autocontenida con su propia configuración.
#
# CORS es la EXCEPCIÓN: /health no está montado bajo ninguna sub-app versionada, así que
# sin su propio CORS queda fuera de cualquier configuración y el navegador bloquea la
# lectura de la respuesta desde un origen distinto (p. ej. el frontend en dev,
# http://localhost:5173) aunque la respuesta SÍ llegue. /health no usa cookies de sesión
# (no hay SessionMiddleware en este app), así que reusar CORS_ORIGINS aquí es seguro: no
# hay credencial que proteger en esta ruta.
#
# OJO: se usa PathScopedCORSMiddleware, NO CORSMiddleware directo. Un middleware del app
# principal envuelve TAMBIÉN las sub-apps montadas (/api/v1 más abajo) — un CORS "global"
# aquí, con allow_methods=["GET"] (pensado solo para /health), interceptaría el preflight
# de cualquier POST/PUT/DELETE de /api/v1/* ANTES de llegar al CORSMiddleware propio de
# esa sub-app y lo rechazaría (bug real ya detectado: login roto en CORS). Acotarlo a
# "/health" evita interferir con las sub-apps versionadas por completo.
app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    PathScopedCORSMiddleware,
    path_prefix="/health",
    allow_origins=CORS_ORIGINS,
    allow_credentials=cors_allow_credentials(CORS_ORIGINS),
    allow_methods=["GET"],
    allow_headers=["*"],
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
