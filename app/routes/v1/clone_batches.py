"""
Endpoints de LOTES de clonación — copiar N bases de un servidor a otro en un gesto.

- POST /database-clone-batches                 — arma el PLAN del lote (valida lo barato).
- GET  /database-clone-batches                 — historial (paginado).
- GET  /database-clone-batches/{id}            — cabecera + counts (polling).
- GET  /database-clone-batches/{id}/items      — una fila por base (paginado).
- POST /database-clone-batches/{id}/execute    — confirmación agregada y ENCOLA el recorrido.
- POST /database-clone-batches/{id}/cancel     — cancelación cooperativa (propaga a la fila en curso).
- GET  /database-clone-batches/{id}/retry-candidates — qué se puede reintentar y qué no.
- POST /database-clone-batches/{id}/retry-failed     — arma un lote NUEVO con lo reintentable.

Todo detrás de ``AdminDep``. Los límites siguen el criterio de la familia: crear el plan toca
el motor (una lista de bases por servidor) → 10/min; ``execute`` y ``retry-failed`` son las
operaciones sensibles → 3/min; el polling → 30/min; la lectura pura de la BD del gateway, sin
decorador.

El lote **no tiene ``preview``**, y no es un olvido: el plan de cada base se resuelve cuando le
toca el turno, así que no existe un momento intermedio donde previsualizar. Lo que se confirma
es el CONJUNTO de pares origen→destino, que es lo que ata ``confirm_token``.
"""

from fastapi import APIRouter, Request

from app.controllers.clone_batch_controller import CloneBatchController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.clone_batch import (
    CloneBatchCreateIn,
    CloneBatchExecuteIn,
    CloneBatchItemOut,
    CloneBatchOut,
    CloneBatchRetryOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/database-clone-batches", tags=["Database Clone Batches"])


@router.post(
    "",
    response_model=ApiResponse[CloneBatchOut],
    status_code=201,
    responses={
        422: {
            "description": (
                "El lote no es representable: vacío, por encima del tope, con nombres destino "
                "repetidos, con un clean_mode destructivo, o sin ninguna fila ejecutable. El "
                "motivo viaja en public_context.code."
            )
        },
    },
)
@limiter.limit("10/minute")
def create_clone_batch_plan(request: Request, admin: AdminDep, payload: CloneBatchCreateIn):
    """
    Arma el plan del lote sin fotografiar ninguna base.

    Las filas que no se pueden clonar por el estado del servidor (el destino ya existe, no
    existe, es una base de sistema…) **no rebotan la petición**: quedan marcadas como
    ``blocked`` con su código, para que el operador vea todos los motivos de una vez en lugar
    de corregir de a una.
    """
    result = CloneBatchController().create_batch_plan(payload.model_dump(), admin=admin)
    return success(data=result, message="Plan del lote creado.")


@router.get("", response_model=ApiResponse[list[CloneBatchOut]])
def list_clone_batches(admin: AdminDep, pagination: PaginationDep):
    items, total = CloneBatchController().list_batches(
        offset=pagination.offset, limit=pagination.size
    )
    return paginated(items, total=total, pagination=pagination)


@router.get("/{batch_id}", response_model=ApiResponse[CloneBatchOut])
@limiter.limit("30/minute")
def get_clone_batch(request: Request, admin: AdminDep, batch_id: int):
    """Cabecera + ``counts`` derivados en vivo. Es el latido del polling del lote."""
    return success(data=CloneBatchController().get_batch(batch_id))


@router.get("/{batch_id}/items", response_model=ApiResponse[list[CloneBatchItemOut]])
def list_clone_batch_items(admin: AdminDep, batch_id: int, pagination: PaginationDep):
    items, total = CloneBatchController().list_items(
        batch_id, offset=pagination.offset, limit=pagination.size
    )
    return paginated(items, total=total, pagination=pagination)


@router.post(
    "/{batch_id}/execute",
    response_model=ApiResponse[CloneBatchOut],
    responses={
        409: {"description": "El lote ya no está pendiente, o su conjunto de bases cambió."},
        410: {"description": "El plan del lote expiró."},
        422: {"description": "El nombre del servidor o el token no coinciden."},
    },
)
@limiter.limit("3/minute")
def execute_clone_batch(
    request: Request, admin: AdminDep, batch_id: int, payload: CloneBatchExecuteIn
):
    result = CloneBatchController().execute_batch(
        batch_id,
        confirm_server_name=payload.confirm_server_name,
        confirm_token=payload.confirm_token,
        admin=admin,
    )
    return success(data=result, message="Lote de clonación encolado.")


@router.post("/{batch_id}/cancel", response_model=ApiResponse[CloneBatchOut])
def cancel_clone_batch(admin: AdminDep, batch_id: int):
    """
    Cancela el lote y **también** el job de la fila en curso. Sin esa propagación, cancelar
    solo evitaba que arrancaran las siguientes y la base que se estaba copiando seguía hasta
    el final, que en una tabla grande son horas.
    """
    result = CloneBatchController().cancel_batch(batch_id, admin=admin)
    return success(data=result, message="Cancelación del lote solicitada.")


@router.get("/{batch_id}/retry-candidates", response_model=ApiResponse[CloneBatchRetryOut])
def clone_batch_retry_candidates(admin: AdminDep, batch_id: int):
    """
    Parte las filas no exitosas según si el destino quedó intacto.

    Las de ``needs_manual`` alcanzaron a copiar datos: el destino tiene filas parciales y el
    lote no puede limpiarlo (no admite modos destructivos), así que se resuelven con el
    asistente de a una.
    """
    return success(data=CloneBatchController().retry_candidates(batch_id))


@router.post(
    "/{batch_id}/retry-failed",
    response_model=ApiResponse[CloneBatchOut],
    status_code=201,
    responses={422: {"description": "No hay ninguna fila reintentable en este lote."}},
)
@limiter.limit("3/minute")
def retry_clone_batch(request: Request, admin: AdminDep, batch_id: int):
    """
    Arma un lote NUEVO con las filas reintentables. Vuelve a pasar por la confirmación
    agregada a propósito: el estado de los servidores cambió desde el plan original.
    """
    result = CloneBatchController().retry_failed(batch_id, admin=admin)
    return success(data=result, message="Lote de reintento creado.")
