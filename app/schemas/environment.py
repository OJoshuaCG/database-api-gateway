"""
Schemas Pydantic de ENTORNOS de despliegue.

``EnvironmentOut`` expone TODOS los flags de política, no solo los datos de presentación: sin
ellos la SPA no puede avisar "esta operación va a producción y las destructivas están
bloqueadas" ANTES de disparar el apply, y el operador se entera por un error. Un contrato que
solo devuelve el nombre y el color obliga a que la advertencia viva hardcodeada en el cliente.

``slug`` se valida con un patrón cerrado en minúsculas y se normaliza acá, no en el motor. Es
deliberado: el slug se compara contra input del usuario (es el gesto de confirmación para
debilitar la política) y la comparación de strings del motor depende de la collation de la BD
del gateway — MySQL/MariaDB comparan case-insensitive por default, PostgreSQL no. Sin
normalizar, ``Production`` sería violación de único en un motor y una segunda fila en el otro,
y ``confirm_slug=PRODUCTION`` confirmaría en uno y no en el otro. Mismo criterio que
``app/services/charset_catalog.py``.
"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.environment_catalog import ENVIRONMENT_COLORS

_NAME_DESC = "Nombre legible del entorno (único). Se recortan los espacios de los extremos."
_SLUG_DESC = (
    "Identificador estable en minúsculas (a-z, 0-9 y guiones), único. Es lo que se audita y "
    "lo que hay que repetir en 'confirm_slug' para debilitar la política del entorno."
)
_RANK_DESC = (
    "Orden de promoción: MENOR = MÁS TEMPRANO. No es único; el desempate es por id. Hoy solo "
    "ordena el listado; el gate de promoción entre entornos es trabajo posterior."
)
_COLOR_DESC = f"Color de presentación. Uno de: {' | '.join(ENVIRONMENT_COLORS)}. Enviar null lo limpia."
_IS_DEFAULT_DESC = (
    "Entorno que se asigna a una BD nueva que no lo especifica. A lo sumo uno True: al "
    "encender este, se apaga el anterior en la misma transacción. Un entorno inactivo NO "
    "puede ser default."
)
_IS_ACTIVE_DESC = (
    "Si se puede asignar a BDs nuevas. Es la vía de retiro de un entorno que todavía tiene "
    "BDs asignadas, porque el borrado exige que no tenga ninguna."
)
_BLOCKS_DESC = (
    "POLÍTICA APLICADA: si es True, aplicar una versión con sentencias destructivas "
    "(DROP / TRUNCATE / DELETE sin WHERE / ALTER DROP COLUMN) a una BD de este entorno se "
    "rechaza. Vale para el apply masivo Y para el apply por BD. 'force' NO lo saltea."
)
_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$|^[a-z0-9]$"


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("El nombre no puede ser solo espacios.")
    return cleaned


def _clean_slug(value: str) -> str:
    """Normaliza a la forma canónica ANTES de persistir o comparar (ver docstring del módulo)."""
    return value.strip().lower()


class EnvironmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60, description=_NAME_DESC)
    slug: str = Field(..., min_length=1, max_length=60, description=_SLUG_DESC)
    rank: int = Field(0, ge=0, le=10_000, description=_RANK_DESC)
    color: str | None = Field(None, description=_COLOR_DESC)
    is_default: bool = Field(False, description=_IS_DEFAULT_DESC)
    is_active: bool = Field(True, description=_IS_ACTIVE_DESC)
    blocks_destructive_migrations: bool = Field(False, description=_BLOCKS_DESC)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("slug")
    @classmethod
    def _norm_slug(cls, value: str) -> str:
        cleaned = _clean_slug(value)
        if not re.match(_SLUG_PATTERN, cleaned):
            raise ValueError(
                "El slug debe ser minúsculas, dígitos y guiones, empezando y terminando en "
                "letra o dígito (p. ej. 'production', 'staging-eu')."
            )
        return cleaned

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in ENVIRONMENT_COLORS:
            raise ValueError(f"Color inválido. Permitidos: {', '.join(ENVIRONMENT_COLORS)}.")
        return value


class EnvironmentUpdate(BaseModel):
    """
    PATCH parcial: el controller usa ``exclude_unset``, así que un campo no enviado no cambia,
    y ``color: null`` sí limpia el color (es la única forma de distinguir los dos casos).

    ``slug`` NO se puede editar. No es una omisión: el slug es la identidad estable que se
    audita y el gesto de confirmación para debilitar la política. Si se pudiera renombrar,
    (a) el historial de auditoría dejaría de resolver a qué entorno se refería cada entrada, y
    (b) el seed del arranque dejaría de reconocer la fila. Para renombrar la presentación está
    ``name``.
    """

    name: str | None = Field(None, min_length=1, max_length=60, description=_NAME_DESC)
    rank: int | None = Field(None, ge=0, le=10_000, description=_RANK_DESC)
    color: str | None = Field(None, description=_COLOR_DESC)
    is_default: bool | None = Field(None, description=_IS_DEFAULT_DESC)
    is_active: bool | None = Field(None, description=_IS_ACTIVE_DESC)
    blocks_destructive_migrations: bool | None = Field(None, description=_BLOCKS_DESC)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        return _clean_name(value) if value is not None else None

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in ENVIRONMENT_COLORS:
            raise ValueError(f"Color inválido. Permitidos: {', '.join(ENVIRONMENT_COLORS)}.")
        return value


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    rank: int
    color: str | None = None
    is_default: bool = False
    is_active: bool = True
    blocks_destructive_migrations: bool = False
    database_count: int = Field(
        0,
        description=(
            "BDs gestionadas asignadas a este entorno (una sola query por página). Es el "
            "número que el borrado exige en cero, así que la SPA puede avisar antes del 409."
        ),
    )
    created_at: datetime
    updated_at: datetime
