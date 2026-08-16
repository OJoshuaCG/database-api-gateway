"""
Subsistema de ejecución asíncrona de jobs de EXPORTACIÓN de bases de datos.

TERCERA COPIA CONSCIENTE del patrón de ``clone_runner.py`` y
``collation_conversion_runner.py`` (worker IN-PROCESS con ``ThreadPoolExecutor``, estado en
la BD de metadatos, barrido de ``running`` huérfanos en el ``lifespan``). Se copia en vez de
extraer la abstracción compartida por el mismo motivo que la segunda: unificar tres runners
y dos vocabularios de ítem es un refactor propio, con su propia superficie de regresión, y
acoplar hoy tres features que todavía pueden divergir cuesta más que ~90 líneas de
administración de pool. **Queda anotado como deuda** (§16.6 del diseño): el cuarto módulo de
esta familia debería ser el disparador de unificarlas, no éste.

POR QUÉ ASÍNCRONO: una exportación lee la base ENTERA del origen y puede tardar horas. No
cabe en una llamada HTTP, y el §19.5 exige progreso, cancelación y reintento — los tres
incompatibles con transmitir el resultado en vivo por la misma conexión HTTP.

**DIVERGENCIA DELIBERADA DEL §6.5: la exportación NO toma el advisory lock del motor.**
Clon, schema-comparisons y conversión de collation sí lo toman, sobre un espacio de claves
compartido, durante todo su pipeline. Acá sería un error:

- la exportación es de **solo lectura**: no hay nada que serializar por corrección;
- sostener el lock exclusivo durante horas **bloquearía** clones, conversiones y migraciones
  sobre esa BD sin ninguna ganancia;
- la consistencia de este módulo la da el **snapshot MVCC** de ``export_session``, que es un
  mecanismo distinto de la exclusión mutua — y que además el lock no podría reemplazar,
  porque el hueco de estructura en MySQL (§6.2) es del diccionario de datos, no de
  concurrencia con el gateway.

Lo que sí se toma es el guard **in-process** de acá abajo, que acota exportaciones
simultáneas de la misma BD dentro de este proceso: barato, sin efecto cross-proceso y sin
bloquear a nadie más. Si alguien "arregla" esto agregando el advisory lock, va a introducir
un bloqueo de horas sobre la base de un tercero.

NO es durable: si el proceso se reinicia, los jobs ``running`` quedan ``interrupted``
(barrido de arranque) y se relanzan a mano — misma limitación conocida que el clon.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.environments import EXPORT_MAX_WORKERS
from app.core.logger import get_logger

logger = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Guards in-process por BD de ORIGEN (clave física "server_id:db"). Serializan dos
# exportaciones concurrentes de la MISMA base dentro de este proceso. NO comparten espacio de
# claves con el advisory lock del motor (ver el docstring del módulo): son mecanismos
# distintos y la exportación solo usa éste.
_db_guards: dict[str, threading.Lock] = {}
_guards_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            # Doble chequeo: entre el ``is None`` de afuera y el lock, otro hilo pudo haberlo
            # creado ya. Sin esto se instancian dos pools y uno queda huérfano.
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=max(1, EXPORT_MAX_WORKERS),
                    thread_name_prefix="export",
                )
    return _executor


def database_guard(db_ref: str) -> threading.Lock:
    """Lock in-process para una BD de origen (misma instancia por ``db_ref``)."""
    with _guards_lock:
        lock = _db_guards.get(db_ref)
        if lock is None:
            lock = threading.Lock()
            _db_guards[db_ref] = lock
        return lock


# Jobs ADMITIDOS: encolados y todavía sin terminar (los que esperan turno y el que corre).
# La cola del ``ThreadPoolExecutor`` no tiene tope, y en la BD un job encolado sigue
# ``pending`` hasta que el worker lo reclama: sin este contador, el techo de concurrencia
# solo veía ``running`` —a lo sumo ``EXPORT_MAX_WORKERS``— y la COLA quedaba sin acotar.
# Contarlo en la BD no era una alternativa: ``pending`` incluye los planes creados y nunca
# ejecutados, que no son trabajo admitido y bloquearían el endpoint sin motivo.
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def inflight_count() -> int:
    """Cuántos jobs hay admitidos (en cola + en ejecución) en ESTE proceso."""
    with _inflight_lock:
        return len(_inflight)


def enqueue(job_id: int) -> None:
    """Encola la generación del artefacto en el pool de hilos."""
    with _inflight_lock:
        _inflight.add(job_id)
    try:
        _get_executor().submit(_run, job_id)
    except BaseException:
        # Si el submit falla, el job NO quedó admitido: dejarlo contado inflaría el techo
        # para siempre y terminaría rechazando exportaciones legítimas.
        with _inflight_lock:
            _inflight.discard(job_id)
        raise


def _run(job_id: int) -> None:
    """Punto de entrada del worker: delega en el controller (import tardío: evita ciclos)."""
    from app.controllers.export_controller import ExportController

    try:
        ExportController().run_job(job_id)
    except Exception:
        logger.error(
            "Job de exportación %s falló de forma inesperada", job_id, exc_info=True
        )
    finally:
        with _inflight_lock:
            _inflight.discard(job_id)


def sweep_interrupted() -> int:
    """
    Marca ``running → interrupted`` los jobs colgados por un reinicio del proceso. Se llama
    en el ``lifespan`` de arranque. Devuelve cuántos se marcaron.

    El artefacto a medio escribir de esos jobs no se toca acá: lo borra
    ``export_storage.sweep_orphans()``, que es quien conoce el directorio de spool.
    """
    from app.controllers.export_controller import ExportController

    try:
        return ExportController().sweep_interrupted()
    except Exception:
        logger.warning(
            "No se pudo barrer jobs de exportación interrumpidos", exc_info=True
        )
        return 0
