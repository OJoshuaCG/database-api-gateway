"""
Endpoints de PROYECTOS — agrupación de blueprints.

CRUD puro sobre el inventario del gateway (no toca ningún motor destino), más los
vínculos N:M con ``database_models``.

Se exponen DOS routers: ``router`` bajo ``/projects`` y ``model_router`` bajo
``/database-models`` para la vista inversa ("¿de qué proyectos es este blueprint?").
Tener el segundo acá y no en ``database_models.py`` mantiene todo el código de proyectos
en un archivo; el precedente es ``model_migrations.py``, que ya monta un router propio
sobre ``/database-models``.
"""

from fastapi import APIRouter

from app.controllers.project_controller import ProjectController
from app.core.auth import AdminDep
from app.schemas.database_model import DatabaseModelOut
from app.schemas.project import (
    ProjectBlueprintsIn,
    ProjectBlueprintsLinkOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/projects", tags=["Projects"])
model_router = APIRouter(prefix="/database-models", tags=["Projects"])


@router.get("", response_model=ApiResponse[list[ProjectOut]])
def list_projects(admin: AdminDep, pagination: PaginationDep):
    items, total = ProjectController().list_projects(
        limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ProjectOut], status_code=201)
def create_project(admin: AdminDep, payload: ProjectCreate):
    created = ProjectController().create_project(payload.model_dump(), admin=admin)
    return success(data=created, message="Proyecto creado.")


@router.get("/{project_id}", response_model=ApiResponse[ProjectOut])
def get_project(admin: AdminDep, project_id: int):
    return success(data=ProjectController().get_project(project_id))


@router.patch("/{project_id}", response_model=ApiResponse[ProjectOut])
def update_project(admin: AdminDep, project_id: int, payload: ProjectUpdate):
    updated = ProjectController().update_project(
        project_id, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=updated, message="Proyecto actualizado.")


@router.delete("/{project_id}", response_model=ApiResponse[None])
def delete_project(admin: AdminDep, project_id: int):
    """
    Borra el proyecto y sus vínculos. **Los blueprints NO se borran.**

    Es una regla del módulo, no una casualidad de la implementación: un blueprint es el
    esquema que replican bases de datos reales con datos reales, y un agrupador jamás
    puede arrastrarlas. Tampoco hace falta confirmar el nombre como en los borrados
    destructivos del gateway: acá no se pierde nada recuperable con dos llamadas.
    """
    unlinked = ProjectController().delete_project(project_id, admin=admin)
    return empty(
        f"Proyecto eliminado. {unlinked} blueprint(s) desvinculado(s); ninguno fue borrado."
    )


@router.get(
    "/{project_id}/blueprints", response_model=ApiResponse[list[DatabaseModelOut]]
)
def list_project_blueprints(admin: AdminDep, project_id: int):
    """Blueprints del proyecto. Sin paginar: son unidades, no miles."""
    return success(data=ProjectController().list_project_blueprints(project_id))


@router.post(
    "/{project_id}/blueprints",
    response_model=ApiResponse[ProjectBlueprintsLinkOut],
)
def link_blueprints(admin: AdminDep, project_id: int, payload: ProjectBlueprintsIn):
    """
    Vincula uno o varios blueprints al proyecto. Idempotente y todo-o-nada.

    Un blueprint ya vinculado sale en ``already_linked`` (200, no error); un id inexistente
    devuelve 422 con la lista y no vincula ninguno.
    """
    result = ProjectController().link_blueprints(
        project_id, payload.model_ids, admin=admin
    )
    return success(data=result, message="Blueprints vinculados al proyecto.")


@router.delete(
    "/{project_id}/blueprints/{model_id}", response_model=ApiResponse[None]
)
def unlink_blueprint(admin: AdminDep, project_id: int, model_id: int):
    """Suelta el vínculo. El blueprint queda intacto, con sus migraciones y sus BDs."""
    ProjectController().unlink_blueprint(project_id, model_id, admin=admin)
    return empty("Blueprint desvinculado del proyecto (el blueprint no se borró).")


@model_router.get(
    "/{model_id}/projects", response_model=ApiResponse[list[ProjectOut]]
)
def list_model_projects(admin: AdminDep, model_id: int):
    """Proyectos a los que pertenece este blueprint (puede ser ninguno o varios)."""
    return success(data=ProjectController().list_model_projects(model_id))
