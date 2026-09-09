"""
Endpoints de la autorización del gateway.

``GET /authz/catalog`` está detrás de ``self.read`` —que los tres roles tienen— y no es
público: el catálogo es el mapa de la política, y el 403 de ``require()`` usa un código cerrado
justamente para no filtrarlo por fuerza bruta. Publicárselo a un anónimo tiraría ese control.
"""

from fastapi import APIRouter

from app.controllers.authz_controller import AuthzController
from app.core.authz import SelfRead
from app.schemas.authz import CapabilityRowOut
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/authz", tags=["Authz"])


@router.get("/catalog", response_model=ApiResponse[list[CapabilityRowOut]])
def capability_catalog(actor: SelfRead):
    """
    El catálogo de capacidades: qué existe, qué muta, qué divulga y qué rol lo tiene.

    Es la MISMA estructura que hace cumplir ``require()``, derivada de ella — no una
    descripción paralela. Publicar una promesa que el servidor no cumple es peor que no
    publicarla.
    """
    return success(data=AuthzController().catalog())
