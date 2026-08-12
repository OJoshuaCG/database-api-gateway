"""
Endpoints del catálogo global de charsets/collations (``/charset-collation-options``).

Define qué combinaciones puede ELEGIR el operador al crear una base de datos: la creación
(``POST /servers/{id}/databases`` y ``POST /managed-databases``) valida contra este catálogo
ANTES de tocar el motor y responde 422 si la combinación no está habilitada.

Caso de uso típico del frontend: poblar el selector con
``GET /charset-collation-options?engine_family=mysql&only_enabled=true`` (la fila
``is_default`` es la opción preseleccionada).

NO hay DELETE a propósito: deshabilitar (``PATCH {"enabled": false}``) ya saca la combinación
del menú, y conservar la fila mantiene legible el histórico de BDs creadas con ella. Borrarla
solo agregaría una vía de pérdida de contexto sin resolver nada.
"""

from fastapi import APIRouter, Query, Request

from app.controllers.charset_collation_controller import CharsetCollationController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.charset_collation_option import (
    CharsetCollationOptionCreate,
    CharsetCollationOptionOut,
    CharsetCollationOptionUpdate,
)
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/charset-collation-options", tags=["Charset & Collation"])


@router.get("", response_model=ApiResponse[list[CharsetCollationOptionOut]])
def list_charset_collation_options(
    admin: AdminDep,
    engine_family: str | None = Query(
        None, description="mysql (cubre MySQL y MariaDB) | postgresql"
    ),
    only_enabled: bool = Query(
        False, description="true = solo las combinaciones que el gateway permite elegir"
    ),
):
    items = CharsetCollationController().list_options(
        engine_family=engine_family, only_enabled=only_enabled
    )
    return success(data=items)


@router.post(
    "", response_model=ApiResponse[CharsetCollationOptionOut], status_code=201
)
@limiter.limit("20/minute")
def create_charset_collation_option(
    request: Request, admin: AdminDep, payload: CharsetCollationOptionCreate
):
    """Agrega una combinación no sembrada. Nace DESHABILITADA salvo ``enabled=true`` explícito."""
    row = CharsetCollationController().create_option(
        payload.model_dump(), admin=admin
    )
    return success(data=row, message="Combinación agregada al catálogo.")


@router.patch(
    "/{option_id}", response_model=ApiResponse[CharsetCollationOptionOut]
)
@limiter.limit("20/minute")
def update_charset_collation_option(
    request: Request,
    admin: AdminDep,
    option_id: int,
    payload: CharsetCollationOptionUpdate,
):
    """Habilita/deshabilita la combinación y/o la marca como default de su familia."""
    row = CharsetCollationController().update_option(
        option_id, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=row, message="Catálogo actualizado.")
