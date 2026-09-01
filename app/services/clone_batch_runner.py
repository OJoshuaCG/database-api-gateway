"""
Subsistema de ejecución asíncrona de LOTES de clonación.

Por qué un executor propio y no el de ``clone_runner``
------------------------------------------------------
Un lote recorre sus filas EN SERIE, llamando a ``CloneController.run_job`` de forma
sincrónica dentro de su propio hilo. Si en vez de eso encolara sus N hijos en el pool de
``clone_runner`` (``CLONE_MAX_WORKERS``, default 2), pasarían dos cosas malas: correrían de
a dos —el lote dejaría de ser serie— y un lote de 12 bases monopolizaría el pool durante
horas, dejando sin turno a cualquier clon suelto que un operador lance mientras tanto.

``CLONE_BATCH_MAX_WORKERS`` (default 1) gobierna cuántos LOTES corren a la vez, no cuántas
filas dentro de un lote. Las filas son serie siempre y eso no es configurable: el
paralelismo entre bases multiplicaría el daño de un error sin un beneficio medido.

Durabilidad: la misma que el resto de la familia. NO es una cola durable — si el proceso se
reinicia, el lote queda ``interrupted`` (barrido en el ``lifespan``) y se relanza a mano con
"reintentar las que faltaron". Con un lote de horas la ventana de un reinicio es mucho más
grande que con un clon suelto, así que ``WORKERS=1`` en el arranque de uvicorn deja de ser un
default cómodo y pasa a ser un requisito: el barrido marca ``interrupted`` todo lo que esté
``running``, sin distinguir de qué proceso es.

NOTA (``P-23``): éste es el CUARTO módulo de esta familia con el mismo esqueleto de runner
(clon, collation, export y lote). El ticket pide unificarlos y sigue abierto: acá NO se
unificó nada, para no atar una feature nueva a un refactor de tres módulos en producción.
Lo que sí cambió es el conteo — quien tome ``P-23`` ahora tiene cuatro copias, no tres.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.environments import CLONE_BATCH_MAX_WORKERS
from app.core.logger import get_logger

logger = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Guard in-process por SERVIDOR destino (no por BD, como en ``clone_runner``): un lote
# escribe sobre N bases del mismo servidor, así que la clave natural es el servidor. Evita
# que dos lotes hacia el mismo destino se pisen dentro de este proceso. La serialización
# cross-proceso real la sigue dando el advisory lock del motor que toma cada job.
_server_guards: dict[int, threading.Lock] = {}
_guards_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        # Doble chequeo bajo lock: dos requests concurrentes en el arranque podrían crear
        # dos pools, y el segundo se perdería con sus hilos ya levantados.
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=max(1, CLONE_BATCH_MAX_WORKERS),
                    thread_name_prefix="clone-batch",
                )
    return _executor


def server_guard(server_id: int) -> threading.Lock:
    """Lock in-process por servidor destino (misma instancia por ``server_id``)."""
    with _guards_lock:
        lock = _server_guards.get(server_id)
        if lock is None:
            lock = threading.Lock()
            _server_guards[server_id] = lock
        return lock


def enqueue(batch_id: int) -> None:
    """Encola el recorrido del lote en el pool de hilos."""
    _get_executor().submit(_run, batch_id)


def _run(batch_id: int) -> None:
    """Punto de entrada del worker: delega en el controller (importado tarde por los ciclos)."""
    from app.controllers.clone_batch_controller import CloneBatchController

    try:
        CloneBatchController().run_batch(batch_id)
    except Exception:  # noqa: BLE001 — el worker nunca debe morir silenciosamente sin log
        logger.error("Lote de clonación %s falló de forma inesperada", batch_id, exc_info=True)


def sweep_interrupted() -> int:
    """
    Cierra los lotes que un reinicio dejó ``running``. Se llama en el ``lifespan`` de arranque.

    **Tiene que correr DESPUÉS de ``clone_runner.sweep_interrupted``**: ese barrido es el que
    pasa a ``interrupted`` los ``CloneJob`` colgados, y el estado de una fila del lote se lee
    del job cuando existe. Al revés, el lote se cerraría mirando filas que todavía figuran
    ``running`` y contaría mal el desenlace.
    """
    from app.controllers.clone_batch_controller import CloneBatchController

    try:
        return CloneBatchController().sweep_interrupted()
    except Exception:  # noqa: BLE001 — el arranque nunca debe romperse por esto
        logger.warning("No se pudo barrer lotes de clonación interrumpidos", exc_info=True)
        return 0
