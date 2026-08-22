"""
Endpoints de clonación de bases de datos entre servidores.

- POST /database-clones                       — crea un PLAN (snapshotea el origen, persiste).
- GET  /database-clones/{id}                  — resumen + estado del job (polling).
- GET  /database-clones/{id}/objects          — inventario del origen + portabilidad + grafo.
- POST /database-clones/{id}/resolve-selection — cierre de dependencias (auto-select de la UI).
- POST /database-clones/{id}/preview          — manda el SPEC, lo congela, devuelve plan + confirm_token.
- POST /database-clones/{id}/execute          — valida y ENCOLA la ejecución asíncrona.
- GET  /database-clones/{id}/items            — pasos ejecutados (paginado).
- POST /database-clones/{id}/cancel           — cancelación cooperativa.

Todo detrás de ``AdminDep``. Crear toca el motor (snapshot del origen) → 10/min;
execute es la operación más sensible → 3/min. El resto es solo lectura.
"""

from fastapi import APIRouter, Query, Request

from app.controllers.clone_controller import CloneController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.clone import (
    CloneClosureOut,
    CloneCreate,
    CloneExecuteIn,
    CloneInventoryOut,
    CloneItemOut,
    ClonePreviewIn,
    ClonePreviewOut,
    CloneResolveSelectionIn,
    CloneSummaryOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/database-clones", tags=["Database Clones"])


@router.post("", response_model=ApiResponse[CloneSummaryOut], status_code=201)
@limiter.limit("10/minute")
def create_clone_plan(request: Request, admin: AdminDep, payload: CloneCreate):
    result = CloneController().create_plan(payload.model_dump(), admin=admin)
    return success(data=result, message="Plan de clonación creado.")


@router.get("/{job_id}", response_model=ApiResponse[CloneSummaryOut])
def get_clone(admin: AdminDep, job_id: int):
    return success(data=CloneController().get_plan(job_id))


@router.get("/{job_id}/objects", response_model=ApiResponse[CloneInventoryOut])
@limiter.limit("10/minute")
def list_clone_objects(
    request: Request,
    admin: AdminDep,
    job_id: int,
    include_data_stats: bool = Query(
        False,
        description=(
            "Incluir la estimación de filas por tabla (una consulta de catálogo por tabla; "
            "opt-in para no encarecer el inventario de una BD con cientos de tablas)."
        ),
    ),
):
    return success(
        data=CloneController().list_objects(job_id, with_estimates=include_data_stats)
    )


@router.post("/{job_id}/resolve-selection", response_model=ApiResponse[CloneClosureOut])
@limiter.limit("10/minute")
def resolve_clone_selection(request: Request, admin: AdminDep, job_id: int, payload: CloneResolveSelectionIn):
    data = CloneController().resolve_selection(
        job_id, [s.model_dump() for s in payload.selection]
    )
    return success(data=data)


@router.post(
    "/{job_id}/preview",
    response_model=ApiResponse[ClonePreviewOut],
    responses={
        409: {"description": "El plan expiró, o el job ya se ejecutó y no se puede re-previsualizar."},
        422: {
            "description": (
                "El spec es incoherente (ver 'code' en public_context) o el esquema del "
                "destino no admite la copia de datos."
            )
        },
    },
)
@limiter.limit("10/minute")
def preview_clone(request: Request, admin: AdminDep, job_id: int, payload: ClonePreviewIn):
    """
    Manda el SPEC, lo congela y devuelve el plan exacto + el ``confirm_token``.

    Solo se aplica lo que VIENE en el cuerpo: un campo ausente deja el valor que el plan ya
    tenía. Si el esquema del destino impide la copia, la respuesta es **200 con
    ``blocking_issues`` y sin token** — el plan se puede ver, pero no confirmar.
    """
    # ``model_fields_set`` distingue "no lo mandó" de "lo mandó en null", que acá no es lo
    # mismo: ``selection: null`` significa CLON COMPLETO y su ausencia significa "no toques
    # la selección".
    spec = payload.model_dump()
    data = CloneController().preview(
        job_id, spec=spec, sent=set(payload.model_fields_set)
    )
    return success(data=data)


@router.post("/{job_id}/execute", response_model=ApiResponse[CloneSummaryOut])
@limiter.limit("3/minute")
def execute_clone(request: Request, admin: AdminDep, job_id: int, payload: CloneExecuteIn):
    result = CloneController().execute_clone(
        job_id,
        confirm_target_name=payload.confirm_target_name,
        confirm_token=payload.confirm_token,
        force=payload.force,
        admin=admin,
    )
    return success(data=result, message="Clonación encolada.")


@router.get("/{job_id}/items", response_model=ApiResponse[list[CloneItemOut]])
def list_clone_items(admin: AdminDep, job_id: int, pagination: PaginationDep):
    items, total = CloneController().list_items(
        job_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("/{job_id}/cancel", response_model=ApiResponse[CloneSummaryOut])
def cancel_clone(admin: AdminDep, job_id: int):
    return success(data=CloneController().cancel(job_id), message="Cancelación solicitada.")
