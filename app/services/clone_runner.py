"""
Subsistema de ejecución asíncrona de jobs de clonación.

DECISIÓN (documentada, ver plan): worker IN-PROCESS (``ThreadPoolExecutor``) — el I/O de
SQLAlchemy es bloqueante y síncrono, así que un pool de hilos es el encaje natural sin
introducir una cola externa. El estado vive en la BD de metadatos (``clone_jobs``), de modo
que el polling del frontend funciona aunque haya varios workers de uvicorn. NO es durable:
si el proceso se reinicia, los jobs ``running`` quedan ``interrupted`` (barrido en el
``lifespan``) y se reintentan a mano. Una cola durable (Celery/RQ) es endurecimiento futuro.

Este módulo es deliberadamente delgado: solo administra el pool, un guard in-process por
BD destino (serializa clones concurrentes a la misma BD dentro del proceso) y el barrido de
arranque. El pipeline real (limpiar → estructura → datos → adopt) vive en
``CloneController.run_job`` para mantener juntas la lógica de sesión/negocio.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.environments import CLONE_MAX_WORKERS
from app.core.logger import get_logger

logger = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Guards in-process por BD destino (clave física "server_id:db"). Serializan dos clones
# concurrentes a la misma BD destino DENTRO de este proceso (evitan lanzar el pipeline dos
# veces sin ni siquiera contender el lock del motor). La serialización CROSS-PROCESO real la
# da el advisory lock del motor, que ``CloneController._pipeline`` sostiene UNA vez sobre una
# conexión dedicada durante TODAS las fases (limpiar → estructura → datos → adopt), no por
# sentencia — ver ``MigrationRunner.advisory_lock``.
_target_guards: dict[str, threading.Lock] = {}
_guards_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=max(1, CLONE_MAX_WORKERS), thread_name_prefix="clone"
                )
    return _executor


def target_guard(target_ref: str) -> threading.Lock:
    """Lock in-process para una BD destino (misma instancia por ``target_ref``)."""
    with _guards_lock:
        lock = _target_guards.get(target_ref)
        if lock is None:
            lock = threading.Lock()
            _target_guards[target_ref] = lock
        return lock


def enqueue(job_id: int) -> None:
    """Encola la ejecución del job en el pool de hilos."""
    _get_executor().submit(_run, job_id)


def _run(job_id: int) -> None:
    """Punto de entrada del worker: delega en el controller (importado tarde para evitar ciclos)."""
    from app.controllers.clone_controller import CloneController

    try:
        CloneController().run_job(job_id)
    except Exception:  # noqa: BLE001 — el worker nunca debe morir silenciosamente sin log
        logger.error("Job de clonación %s falló de forma inesperada", job_id, exc_info=True)


def sweep_interrupted() -> int:
    """
    Marca ``running → interrupted`` los jobs colgados por un reinicio del proceso. Se llama
    en el ``lifespan`` de arranque. Devuelve cuántos se marcaron.
    """
    from app.controllers.clone_controller import CloneController

    try:
        return CloneController().sweep_interrupted()
    except Exception:  # noqa: BLE001 — el arranque nunca debe romperse por esto
        logger.warning("No se pudo barrer jobs de clonación interrumpidos", exc_info=True)
        return 0
