"""
Endpoints de ENTORNOS de despliegue.

CRUD puro sobre el inventario del gateway (no toca ningún motor destino), pero cada fila es
POLÍTICA: ``blocks_destructive_migrations`` se evalúa antes de aplicar una migración y puede
negar la operación. De ahí dos cosas que no son las de un CRUD normal:

- **``confirm_slug`` para debilitar.** Apagar el bloqueo de destructivas, desactivar el
  entorno, o quitarle el default exige repetir el slug. Precedente del gesto:
  ``confirm_target_name`` del DROP DATABASE y ``confirm_username`` del borrado de usuarios.
- **``DELETE`` sin ``force``.** Exige cero BDs asignadas (409 con el conteo si no). La vía de
  retiro de un entorno que todavía tiene BDs es ``is_active=false``.

Se pagina como ``/projects`` aunque sean pocas filas: el helper del frontend que consume
listados exige el bloque ``pagination``, y "traer todo" ya se resuelve pidiendo el tamaño
máximo de página.
"""

from fastapi import APIRouter, Query

from app.controllers.environment_controller import EnvironmentController
from app.core.auth import AdminDep
from app.schemas.environment import EnvironmentCreate, EnvironmentOut, EnvironmentUpdate
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/environments", tags=["Environments"])

_CONFIRM_SLUG_DESC = (
    "Repetir el slug del entorno. OBLIGATORIO solo cuando el cambio DEBILITA la política "
    "(apagar blocks_destructive_migrations, apagar is_active, o quitar is_default) o al "
    "borrar el entorno por defecto."
)


@router.get("", response_model=ApiResponse[list[EnvironmentOut]])
def list_environments(
    admin: AdminDep,
    pagination: PaginationDep,
    only_active: bool = Query(
        False,
        description=(
            "Solo los entornos asignables. Nombre alineado con 'only_enabled' del catálogo "
            "de charsets, que es el mismo tipo de filtro sobre el mismo tipo de catálogo."
        ),
    ),
):
    """Entornos ordenados por (rank, id) — el orden de promoción, con desempate estable."""
    items, total = EnvironmentController().list_environments(
        only_active=only_active, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[EnvironmentOut], status_code=201)
def create_environment(admin: AdminDep, payload: EnvironmentCreate):
    created = EnvironmentController().create_environment(
        payload.model_dump(), admin=admin
    )
    return success(data=created, message="Entorno creado.")


@router.get("/{environment_id}", response_model=ApiResponse[EnvironmentOut])
def get_environment(admin: AdminDep, environment_id: int):
    return success(data=EnvironmentController().get_environment(environment_id))


@router.patch("/{environment_id}", response_model=ApiResponse[EnvironmentOut])
def update_environment(
    admin: AdminDep,
    environment_id: int,
    payload: EnvironmentUpdate,
    confirm_slug: str | None = Query(None, max_length=60, description=_CONFIRM_SLUG_DESC),
):
    """
    PATCH parcial. ``slug`` no se puede editar (es la identidad que se audita y se confirma).

    Un cambio que debilita la política sin ``confirm_slug`` devuelve 422 con
    ``environment.confirmation_required`` y el slug esperado en ``public_context``.
    """
    updated = EnvironmentController().update_environment(
        environment_id,
        payload.model_dump(exclude_unset=True),
        confirm_slug=confirm_slug,
        admin=admin,
    )
    return success(data=updated, message="Entorno actualizado.")


@router.delete("/{environment_id}", response_model=ApiResponse[None])
def delete_environment(
    admin: AdminDep,
    environment_id: int,
    confirm_slug: str | None = Query(None, max_length=60, description=_CONFIRM_SLUG_DESC),
):
    """
    Borra el entorno. **Exige que no tenga ninguna BD asignada** → 409
    ``environment.has_databases`` con ``database_count`` si la tiene.

    NO hay ``?force=true`` que desclasifique en masa: desclasificar N BDs de producción de un
    plumazo es irreversible sobre N filas a la vez, y ningún DELETE de este repo tiene un flag
    así. Para retirar un entorno que todavía tiene BDs, ``is_active=false``.
    """
    EnvironmentController().delete_environment(
        environment_id, confirm_slug=confirm_slug, admin=admin
    )
    return empty(message="Entorno borrado.")
