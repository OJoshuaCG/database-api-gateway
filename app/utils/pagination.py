from typing import Annotated

from fastapi import Depends, Query

from app.core.environments import PAGINATION_MAX_SIZE

# Tamaño por defecto cuando el cliente no especifica ?size=
_DEFAULT_SIZE = 20


class PaginationParams:
    """
    Dependencia de paginación reutilizable en cualquier endpoint.

    Provee page, size y offset listos para usar en consultas SQL.
    Para construir la respuesta paginada usar 'paginated()' de app.utils.response.

    Uso en un route:
        from app.utils.pagination import PaginationDep
        from app.utils.response import ApiResponse, paginated

        @router.get("/users", response_model=ApiResponse[list[UserOut]])
        async def list_users(pagination: PaginationDep):
            users = model.find_all(limit=pagination.size, offset=pagination.offset)
            total = model.count()
            return paginated(users, total=total, pagination=pagination)

    Query params aceptados:
        ?page=1&size=20
    """

    def __init__(
        self,
        page: int = Query(1, ge=1, description="Número de página (inicia en 1)"),
        size: int = Query(
            _DEFAULT_SIZE,
            ge=1,
            le=PAGINATION_MAX_SIZE,
            description=f"Elementos por página (máximo {PAGINATION_MAX_SIZE})",
        ),
    ):
        self.page = page
        self.size = size
        # offset listo para usar directamente en SQL: LIMIT size OFFSET offset
        self.offset: int = (page - 1) * size


# Alias de tipo para usar en signatures de endpoints
PaginationDep = Annotated[PaginationParams, Depends(PaginationParams)]
