"""
Subsistema de ejecución asíncrona de jobs de conversión de charset/collation.

MISMO patrón que ``app/services/clone_runner.py`` (worker IN-PROCESS con
``ThreadPoolExecutor``, estado en la BD de metadatos, barrido de ``running`` huérfanos en el
``lifespan``) y a propósito un módulo SEPARADO: generalizar ``clone_runner`` en una
abstracción compartida es alcance de otra tarea, y duplicar ~90 líneas de administración de
pool cuesta menos que acoplar dos features que todavía pueden divergir.

POR QUÉ ASÍNCRONO: ``ALTER TABLE ... CONVERT TO CHARACTER SET`` REESCRIBE la tabla completa
(la doc de MySQL lo confirma: cambia el default de la tabla *y* todas las columnas de
caracteres), así que en tablas grandes tarda minutos u horas y bloquea. No cabe en una
llamada HTTP síncrona.

NO es durable: si el proceso se reinicia, los jobs ``running`` quedan ``interrupted``
(barrido de arranque) y se reintentan a mano. Una cola durable es endurecimiento futuro —
misma limitación conocida que el clon.

Este módulo es deliberadamente delgado: administra el pool, un guard in-process por BD y el
barrido de arranque. El pipeline real vive en
``CollationConversionController.run_job`` para mantener juntas la lógica de sesión/negocio.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.environments import COLLATION_CONVERSION_MAX_WORKERS
from app.core.logger import get_logger

logger = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Guards in-process por BD (clave física "server_id:db"). Serializan dos conversiones
# concurrentes de la MISMA BD dentro de este proceso. La serialización CROSS-PROCESO real la
# da el advisory lock del motor, que el pipeline sostiene UNA vez sobre una conexión dedicada
# durante TODAS las fases (BD → tablas → objetos) — ver ``MigrationRunner.advisory_lock``.
# Comparte espacio de claves con el clon y con schema-comparisons (misma ``lock_key``
# derivada del ``managed_database_id`` o de la clave sintética negativa), así que una
# conversión y un clon sobre la misma BD física NO pueden pisarse.
_db_guards: dict[str, threading.Lock] = {}
_guards_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=max(1, COLLATION_CONVERSION_MAX_WORKERS),
                    thread_name_prefix="collation",
                )
    return _executor


def database_guard(db_ref: str) -> threading.Lock:
    """Lock in-process para una BD (misma instancia por ``db_ref``)."""
    with _guards_lock:
        lock = _db_guards.get(db_ref)
        if lock is None:
            lock = threading.Lock()
            _db_guards[db_ref] = lock
        return lock


def enqueue(job_id: int) -> None:
    """Encola la ejecución del job en el pool de hilos."""
    _get_executor().submit(_run, job_id)


def _run(job_id: int) -> None:
    """Punto de entrada del worker: delega en el controller (import tardío: evita ciclos)."""
    from app.controllers.collation_conversion_controller import (
        CollationConversionController,
    )

    try:
        CollationConversionController().run_job(job_id)
    except Exception:  # noqa: BLE001 — el worker nunca debe morir sin dejar log
        logger.error(
            "Job de conversión de collation %s falló de forma inesperada", job_id,
            exc_info=True,
        )


def sweep_interrupted() -> int:
    """
    Marca ``running → interrupted`` los jobs colgados por un reinicio del proceso. Se llama
    en el ``lifespan`` de arranque. Devuelve cuántos se marcaron.
    """
    from app.controllers.collation_conversion_controller import (
        CollationConversionController,
    )

    try:
        return CollationConversionController().sweep_interrupted()
    except Exception:  # noqa: BLE001 — el arranque nunca debe romperse por esto
        logger.warning(
            "No se pudo barrer jobs de conversión de collation interrumpidos", exc_info=True
        )
        return 0
