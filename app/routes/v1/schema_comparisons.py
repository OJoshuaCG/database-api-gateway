"""
Endpoints de comparaciones estructurales entre dos BDs gestionadas (Plan diff).

- POST /schema-comparisons                    — crea (snapshotea ambas, diff, persiste).
- GET  /schema-comparisons/{id}               — resumen (conteos, warnings, scope_note).
- GET  /schema-comparisons/{id}/items         — detalle paginado del DDL (dry-run/preview).
- POST /schema-comparisons/{id}/execute-preview — resuelve modo/selección de Opción B
  SIN ejecutar (devuelve las sentencias exactas + el ``confirm_token`` a reenviar).
- POST /schema-comparisons/{id}/adopt         — Opción A: nueva versión de blueprint.
- POST /schema-comparisons/{id}/execute       — Opción B: ejecución directa ad-hoc.

Todo detrás de ``AdminDep``. La creación toca ambos motores (introspección, coste
alto → 10/min); adopt/execute son las operaciones más sensibles → 3/min (alineado
con apply-all).
"""

from fastapi import APIRouter, Query, Request

from app.controllers.schema_comparison_controller import SchemaComparisonController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.schema_comparison import (
    AdoptComparisonIn,
    AdoptComparisonOut,
    ExecuteComparisonIn,
    ExecuteComparisonOut,
    ExecutePreviewIn,
    ExecutePreviewOut,
    SchemaComparisonCreate,
    SchemaComparisonItemOut,
    SchemaComparisonSummaryOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(prefix="/schema-comparisons", tags=["Schema Comparisons"])


@router.post("", response_model=ApiResponse[SchemaComparisonSummaryOut], status_code=201)
@limiter.limit("10/minute")
def create_comparison(request: Request, admin: AdminDep, payload: SchemaComparisonCreate):
    result = SchemaComparisonController().create_comparison(
        source_database_id=payload.source_database_id,
        source_server_id=payload.source_server_id,
        source_database_name=payload.source_database_name,
        target_database_id=payload.target_database_id,
        target_server_id=payload.target_server_id,
        target_database_name=payload.target_database_name,
        admin=admin,
    )
    return success(data=result, message="Comparación de esquema creada.")


@router.get("/{comparison_id}", response_model=ApiResponse[SchemaComparisonSummaryOut])
def get_comparison(admin: AdminDep, comparison_id: int):
    return success(data=SchemaComparisonController().get_comparison(comparison_id))


@router.get(
    "/{comparison_id}/items",
    response_model=ApiResponse[list[SchemaComparisonItemOut]],
)
def list_comparison_items(
    admin: AdminDep,
    comparison_id: int,
    pagination: PaginationDep,
    object_type: str | None = Query(None, description="Filtra por tipo de objeto."),
    change_type: str | None = Query(
        None, pattern=r"^(new|modified|dropped)$", description="Filtra por tipo de cambio."
    ),
):
    items, total = SchemaComparisonController().list_items(
        comparison_id,
        object_type=object_type,
        change_type=change_type,
        limit=pagination.size,
        offset=pagination.offset,
    )
    return paginated(items, total=total, pagination=pagination)


@router.post(
    "/{comparison_id}/adopt", response_model=ApiResponse[AdoptComparisonOut]
)
@limiter.limit("3/minute")
def adopt_comparison(
    request: Request, admin: AdminDep, comparison_id: int, payload: AdoptComparisonIn
):
    result = SchemaComparisonController().adopt_comparison(
        comparison_id,
        selected_item_ids=payload.selected_item_ids,
        name=payload.name,
        description=payload.description,
        execute_immediately=payload.execute_immediately,
        admin=admin,
    )
    msg = f"Versión {result['version']} creada desde la comparación."
    if result["executed"]:
        msg += " Aplicada al target."
    return success(data=result, message=msg)


@router.post(
    "/{comparison_id}/execute-preview", response_model=ApiResponse[ExecutePreviewOut]
)
def preview_execution(
    admin: AdminDep, comparison_id: int, payload: ExecutePreviewIn
):
    """
    Resuelve un modo/selección SIN ejecutar nada: devuelve las sentencias exactas y el
    ``confirm_token`` a reenviar en ``POST .../execute``. Solo lectura (no toca el
    motor destino) — el frontend debe llamarlo antes de pedirle confirmación al usuario.
    """
    result = SchemaComparisonController().preview_execution(
        comparison_id,
        mode=payload.mode,
        selected_item_ids=payload.selected_item_ids,
    )
    return success(data=result)


@router.post(
    "/{comparison_id}/execute", response_model=ApiResponse[ExecuteComparisonOut]
)
@limiter.limit("3/minute")
def execute_comparison(
    request: Request,
    admin: AdminDep,
    comparison_id: int,
    payload: ExecuteComparisonIn,
    force: bool = Query(
        False, description="Override de cuarentena tras un fallo previo (inspeccionado)."
    ),
):
    result = SchemaComparisonController().execute_comparison(
        comparison_id,
        mode=payload.mode,
        selected_item_ids=payload.selected_item_ids,
        confirm_target_name=payload.confirm_target_name,
        confirm_token=payload.confirm_token,
        force=force,
        admin=admin,
    )
    suffix = " (con fallo)" if result["failed"] else ""
    msg = f"Ejecutadas {result['applied_count']}/{result['total']} sentencia(s){suffix}."
    return success(data=result, message=msg)
