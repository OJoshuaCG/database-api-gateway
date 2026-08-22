"""
Schemas Pydantic de PROYECTOS (agrupación de blueprints).

El tope de la descripción vive acá y no en la columna (``Text``): subirlo el día que 5000
no alcancen es cambiar una constante, no migrar el esquema.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Tope de la descripción. Requisito actual: 5000 caracteres.
DESCRIPTION_MAX_LENGTH = 5000

_NAME_DESC = "Nombre del proyecto (único). Se recortan los espacios de los extremos."
_DESCRIPTION_DESC = (
    f"Descripción larga del proyecto, hasta {DESCRIPTION_MAX_LENGTH} caracteres. "
    "Enviar null la limpia."
)
_MODEL_IDS_DESC = (
    "IDs de blueprints a vincular. La operación es IDEMPOTENTE: un blueprint ya vinculado "
    "no es un error, sale informado en 'already_linked'. Si algún id no existe se devuelve "
    "422 con la lista y NO se vincula ninguno (todo o nada)."
)


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("El nombre no puede ser solo espacios.")
    return cleaned


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150, description=_NAME_DESC)
    description: str | None = Field(
        None, max_length=DESCRIPTION_MAX_LENGTH, description=_DESCRIPTION_DESC
    )
    model_ids: list[int] | None = Field(
        None,
        description=(
            "Blueprints a vincular en el mismo alta (opcional). Mismas reglas que "
            "POST /projects/{id}/blueprints."
        ),
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return _clean_name(value)


class ProjectUpdate(BaseModel):
    """
    PATCH parcial. Sin campos enviados no cambia nada (el controller usa
    ``exclude_unset``), y ``description: null`` sí limpia la descripción — es la única
    forma de distinguir "no enviado" de "vaciar".
    """

    name: str | None = Field(None, min_length=1, max_length=150, description=_NAME_DESC)
    description: str | None = Field(
        None, max_length=DESCRIPTION_MAX_LENGTH, description=_DESCRIPTION_DESC
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        return _clean_name(value) if value is not None else None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    blueprint_count: int = Field(
        0, description="Cantidad de blueprints vinculados (una sola query por listado)"
    )
    created_at: datetime
    updated_at: datetime


class ProjectBlueprintsIn(BaseModel):
    model_ids: list[int] = Field(..., min_length=1, description=_MODEL_IDS_DESC)


class ProjectBlueprintsLinkOut(BaseModel):
    """Resultado de vincular: qué se agregó y qué ya estaba, sin ambigüedad."""

    project_id: int
    linked: list[int] = Field(
        default_factory=list, description="Blueprints vinculados en ESTA llamada"
    )
    already_linked: list[int] = Field(
        default_factory=list, description="Blueprints que ya pertenecían al proyecto"
    )
    blueprint_count: int = Field(0, description="Total de blueprints del proyecto tras la operación")
