import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.core import remote_engine
from app.core.auth import bootstrap_admin
from app.core.environments import (
    CORS_ORIGINS,
    EXPORT_PURGE_INTERVAL_MINUTES,
    MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES,
    MIGRATION_CAPTURE_TTL_HOURS,
)
from app.core.logger import get_logger
from app.core.versioned_app import (
    PathScopedCORSMiddleware,
    cors_allow_credentials,
    create_versioned_app,
)
from app.routes.health import router as health_router
from app.routes.v1.routes import router as v1_router
from app.services.charset_catalog import seed_charset_options
from app.services.db_admin import migration_results
from app.services.privilege_catalog import seed_privileges

logger = get_logger(__name__)


async def _purge_captures_periodically(ttl_hours: int, interval_seconds: int) -> None:
    """
    Repite la purga por TTL de las capturas de ``SELECT`` mientras el proceso viva.

    Hacerlo SOLO en el arranque volvía el TTL una promesa falsa: un gateway que corre semanas
    (lo normal en producción) nunca volvía a purgar, y estos son los ÚNICOS datos de negocio
    que el gateway persiste. La tarea duerme primero: la purga del arranque ya corrió.

    ``purge_expired`` es I/O SÍNCRONO (SQLAlchemy) contra la BD del gateway, así que va a un
    hilo con ``asyncio.to_thread``. Ejecutarlo directo acá bloquearía el event loop y con él
    TODAS las requests en vuelo — el mismo criterio por el que los handlers que hacen I/O
    bloqueante se declaran ``def`` y no ``async def``.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(migration_results.purge_expired, ttl_hours)
        except asyncio.CancelledError:
            # Apagado en curso: propagar para que la tarea termine de verdad.
            raise
        except Exception:  # noqa: BLE001 — la retención no puede tumbar el proceso
            # ``purge_expired`` ya traga sus propios errores; esto cubre lo que quede fuera
            # (p. ej. no poder crear el hilo) para que un fallo no mate el bucle entero.
            logger.warning(
                "Falló una pasada de la purga periódica de capturas de SELECT", exc_info=True
            )


async def _purge_export_artifacts_periodically(interval_seconds: int) -> None:
    """
    Repite la purga por TTL de los artefactos de exportación mientras el proceso viva.

    MISMA plantilla que ``_purge_captures_periodically`` y por el mismo motivo: un artefacto
    es un archivo con los datos del origen EN CLARO (no hay enmascarado), su TTL es de
    minutos, y una purga que solo corre al arrancar convierte esa retención en una promesa
    falsa en un gateway que corre semanas.

    ``purge_expired`` es I/O SÍNCRONO (borrado de archivos + SQLAlchemy), así que va a un
    hilo con ``asyncio.to_thread``: ejecutarlo en el event loop bloquearía TODAS las requests
    en vuelo. La tarea duerme primero porque la purga del arranque ya corrió, y un fallo de
    una pasada no mata el bucle.

    **No** llama a ``sweep_orphans``: ese barrido borra también los archivos ``.part``, y
    acá sí puede haber una exportación escribiendo el suyo. El barrido de huérfanos corre
    solo al arrancar, cuando ningún job de este proceso está vivo.
    """
    from app.services import export_storage

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(export_storage.purge_expired)
        except asyncio.CancelledError:
            # Apagado en curso: propagar para que la tarea termine de verdad.
            raise
        except Exception:  # noqa: BLE001 — la retención no puede tumbar el proceso
            logger.warning(
                "Falló una pasada de la purga periódica de artefactos de exportación",
                exc_info=True,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Arranque: sembrar el administrador único y los catálogos (privilegios y
    # charsets/collations). Ambos seeds son idempotentes y PRESERVAN los toggles del
    # operador; hacen falta acá además de en la migración porque un esquema creado con
    # ``Base.metadata.create_all`` (tests, dev rápido) no pasa por Alembic.
    bootstrap_admin()
    seed_privileges()
    seed_charset_options()
    # Asegurar una DEK persistida (envelope encryption) en sistema fresco; idempotente.
    from app.core import crypto as _crypto

    _crypto.bootstrap_dek()
    # Barrido: jobs asíncronos que quedaron 'running' por un reinicio → 'interrupted'.
    # Cada worker in-process tiene su propio barrido (los pools son independientes).
    from app.services import (
        clone_runner,
        collation_conversion_runner,
        export_runner,
        export_storage,
    )

    clone_runner.sweep_interrupted()
    collation_conversion_runner.sweep_interrupted()
    export_runner.sweep_interrupted()
    # Artefactos de exportación: primero se purgan los vencidos y después se barren los
    # HUÉRFANOS (archivos sin fila viva, típicamente de un ``kill -9`` a mitad de la
    # generación). Sin el segundo barrido, un artefacto con los datos del origen en claro se
    # queda en disco para siempre. Solo acá puede borrar los ``.part``: en el arranque no hay
    # ningún job de este proceso escribiendo.
    export_storage.purge_expired()
    export_storage.sweep_orphans()
    # Retención: los resultados de SELECT capturados durante una migración son DATOS DE
    # NEGOCIO y no se guardan indefinidamente. Se purgan los vencidos
    # (MIGRATION_CAPTURE_TTL_HOURS). No lanza: un fallo acá no puede impedir el arranque.
    migration_results.purge_expired(MIGRATION_CAPTURE_TTL_HOURS)
    # ...y se sigue purgando periódicamente: la del arranque sola no cumple el TTL en un
    # proceso de larga vida (ver el docstring de _purge_captures_periodically).
    purge_task: asyncio.Task | None = None
    if MIGRATION_CAPTURE_TTL_HOURS > 0 and MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES > 0:
        purge_task = asyncio.create_task(
            _purge_captures_periodically(
                MIGRATION_CAPTURE_TTL_HOURS,
                MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES * 60,
            ),
            name="migration-capture-purge",
        )
    export_purge_task: asyncio.Task | None = None
    if EXPORT_PURGE_INTERVAL_MINUTES > 0:
        export_purge_task = asyncio.create_task(
            _purge_export_artifacts_periodically(EXPORT_PURGE_INTERVAL_MINUTES * 60),
            name="export-artifact-purge",
        )
    try:
        yield
    finally:
        # La limpieza va en ``finally``: si el ciclo de vida se cierra por una excepción (o el
        # servidor cancela el lifespan), un ``yield`` "pelado" saltearía esto y quedarían la
        # tarea periódica pendiente ("Task was destroyed but it is pending") y los engines a
        # los servidores destino sin liberar.
        #
        # Cancelar la tarea periódica y ESPERARLA: sin el await, el intérprete cierra el loop
        # con la tarea todavía pendiente.
        for task in (purge_task, export_purge_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        # Liberar los engines de conexión a servidores destino.
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
