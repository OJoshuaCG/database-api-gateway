"""
Endpoints de conversión de charset/collation de una base de datos (MySQL/MariaDB).

- POST /servers/{server_id}/databases/{database}/collation-conversions
                                            — crea un PLAN (lee el inventario, persiste).
- GET  /collation-conversions/{id}          — resumen + estado del job (polling).
- GET  /collation-conversions/{id}/objects  — inventario: tablas por collation + resumen
                                              agrupado + los 5 tipos de objeto congelados.
- POST /collation-conversions/{id}/preview  — resuelve el plan final + confirm_token.
- POST /collation-conversions/{id}/execute  — valida y ENCOLA la ejecución asíncrona.
- GET  /collation-conversions/{id}/items    — pasos ejecutados (paginado).
- POST /collation-conversions/{id}/cancel   — cancelación cooperativa.

La creación es anidada bajo ``/servers/...`` porque la BD se identifica por IDENTIDAD FÍSICA
(``server_id`` + nombre), funcione o no adoptada en el inventario — mismo patrón que el resto
del módulo de servidor-BDs. Una vez que existe el job, el resto cuelga de él.

Todo detrás de ``AdminDep``. Crear y previsualizar tocan el motor (lectura del inventario) →
10/min, igual que el clon; execute es la operación más sensible → 3/min. El resto es lectura
de la BD del gateway.
"""

from fastapi import APIRouter, Request

from app.controllers.collation_conversion_controller import (
    CollationConversionController,
)
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.collation_conversion import (
    CollationConversionCreate,
    CollationConversionExecuteIn,
    CollationConversionItemOut,
    CollationConversionPreviewIn,
    CollationConversionPreviewOut,
    CollationConversionSummaryOut,
    CollationInventoryOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(tags=["Collation Conversions"])


@router.post(
    "/servers/{server_id}/databases/{database}/collation-conversions",
    response_model=ApiResponse[CollationConversionSummaryOut],
    status_code=201,
)
@limiter.limit("10/minute")
def create_collation_conversion(
    request: Request,
    admin: AdminDep,
    server_id: int,
    database: str,
    payload: CollationConversionCreate,
):
    result = CollationConversionController().create_plan(
        server_id,
        database,
        target_charset=payload.target_charset,
        target_collation=payload.target_collation,
        admin=admin,
    )
    return success(data=result, message="Plan de conversión de collation creado.")


@router.get(
    "/collation-conversions/{job_id}",
    response_model=ApiResponse[CollationConversionSummaryOut],
)
def get_collation_conversion(admin: AdminDep, job_id: int):
    return success(data=CollationConversionController().get_plan(job_id))


@router.get(
    "/collation-conversions/{job_id}/objects",
    response_model=ApiResponse[CollationInventoryOut],
)
@limiter.limit("10/minute")
def list_collation_conversion_objects(request: Request, admin: AdminDep, job_id: int):
    return success(data=CollationConversionController().get_objects(job_id))


@router.post(
    "/collation-conversions/{job_id}/preview",
    response_model=ApiResponse[CollationConversionPreviewOut],
)
@limiter.limit("10/minute")
def preview_collation_conversion(
    request: Request, admin: AdminDep, job_id: int, payload: CollationConversionPreviewIn
):
    data = CollationConversionController().preview(
        job_id,
        tables=payload.tables,
        objects=[o.model_dump() for o in payload.objects],
        include_database_default=payload.include_database_default,
        force=payload.force,
    )
    return success(data=data)


@router.post(
    "/collation-conversions/{job_id}/execute",
    response_model=ApiResponse[CollationConversionSummaryOut],
)
@limiter.limit("3/minute")
def execute_collation_conversion(
    request: Request, admin: AdminDep, job_id: int, payload: CollationConversionExecuteIn
):
    result = CollationConversionController().execute(
        job_id,
        confirm_target_name=payload.confirm_target_name,
        confirm_token=payload.confirm_token,
        force=payload.force,
        admin=admin,
    )
    return success(data=result, message="Conversión de collation encolada.")


@router.get(
    "/collation-conversions/{job_id}/items",
    response_model=ApiResponse[list[CollationConversionItemOut]],
)
def list_collation_conversion_items(
    admin: AdminDep, job_id: int, pagination: PaginationDep
):
    items, total = CollationConversionController().list_items(
        job_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post(
    "/collation-conversions/{job_id}/cancel",
    response_model=ApiResponse[CollationConversionSummaryOut],
)
def cancel_collation_conversion(admin: AdminDep, job_id: int):
    return success(
        data=CollationConversionController().cancel(job_id, admin=admin),
        message="Cancelación solicitada.",
    )
