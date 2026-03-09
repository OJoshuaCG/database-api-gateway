from fastapi import APIRouter

from app.exceptions import AppHttpException
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(tags=["test"], prefix="/test")

# ---------------------------------------------------------------------------
# Ejemplos de uso del envelope estándar de respuesta
# ---------------------------------------------------------------------------


@router.get("/ping", response_model=ApiResponse[dict])
async def ping():
    """Respuesta simple con data."""
    return success(data={"message": "pong!"})


@router.get("/paginated", response_model=ApiResponse[list[dict]])
async def paginated_example(pagination: PaginationDep):
    """Respuesta paginada — data y pagination al mismo nivel."""
    _mock_items = [{"id": i, "name": f"Item {i}"} for i in range(1, 51)]
    page_items = _mock_items[pagination.offset : pagination.offset + pagination.size]
    return paginated(page_items, total=len(_mock_items), pagination=pagination)


@router.delete("/resource/{resource_id}", response_model=ApiResponse[None])
async def delete_example(resource_id: int):
    """Respuesta sin data — solo message (ej: DELETE)."""
    return empty(f"Recurso {resource_id} eliminado exitosamente")


# ---------------------------------------------------------------------------
# Ejemplos de manejo de errores (el formato detail es independiente al envelope)
# ---------------------------------------------------------------------------


@router.put("/custom-error")
async def custom_error():
    """Demuestra que AppHttpException retorna detail, independiente del envelope."""
    raise AppHttpException("Custom error")


@router.post("/syntax-error")
async def syntax_error():
    if None > 0:
        return success(data={"message": "Syntax error!"})
    return success(data={"message": "No syntax error!"})
