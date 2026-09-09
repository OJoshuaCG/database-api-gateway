"""
Endpoints de la autorización del gateway.

``GET /authz/catalog`` está detrás de ``self.read`` —que los tres roles tienen— y no es
público: el catálogo es el mapa de la política, y el 403 de ``require()`` usa un código cerrado
justamente para no filtrarlo por fuerza bruta. Publicárselo a un anónimo tiraría ese control.
"""

from fastapi import APIRouter

from app.controllers.authz_controller import AuthzController
from app.core.authz import GatewayAdmin, SelfRead
from app.schemas.authz import CapabilityRowOut, ScopeReadinessOut
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


@router.get("/scope-readiness", response_model=ApiResponse[ScopeReadinessOut])
def scope_readiness(actor: GatewayAdmin):
    """
    Qué pasaría si se empezara a otorgar acceso por alcance, HOY.

    **Se pide antes de crear el primer grant restrictivo.** Una BD sin ``environment_id`` no
    resuelve al entorno por defecto —ése es el más permisivo— sino al **más protegido**, así que
    otorgar "lector en producción" también le saca a esa persona el acceso a toda base que nadie
    clasificó. El reporte dice cuántas son y en qué servidores.

    Detrás de ``gateway.admin`` porque su audiencia es quien administra accesos, y es quien
    tiene que actuar sobre el resultado. **Cero conexiones al motor.**
    """
    return success(data=AuthzController().scope_readiness())
