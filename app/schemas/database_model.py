"""Schemas Pydantic del recurso DatabaseModel (blueprint/categoría)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.managed_database import ManagedDatabaseOut

_SLUG = r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$"
# Identificador legado (introspección): dígito inicial y `. - $` permitidos.
_OBJ_NAME = r"^[A-Za-z0-9_$][A-Za-z0-9_$.\-]{0,63}$"


_CHARSET_DESC = (
    "Juego de caracteres de REFERENCIA del esquema. Semántica de la familia MySQL: contra "
    "destinos PostgreSQL no se compara (usa encoding + lc_collate, que no son equivalentes)."
)
_COLLATION_DESC = (
    "Collation de REFERENCIA del esquema. Si está declarado, el validador avisa cuando una "
    "migración fuerza uno distinto. Vacío = se compara contra el collation real de las BDs."
)


class DatabaseModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=120, pattern=_SLUG)
    description: str | None = None
    current_version: str = Field("0.0.0", max_length=50)
    is_active: bool = True
    charset: str | None = Field(None, max_length=50, description=_CHARSET_DESC)
    collation: str | None = Field(None, max_length=100, description=_COLLATION_DESC)


class DatabaseModelUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    slug: str | None = Field(None, min_length=1, max_length=120, pattern=_SLUG)
    description: str | None = None
    current_version: str | None = Field(None, max_length=50)
    is_active: bool | None = None
    charset: str | None = Field(None, max_length=50, description=_CHARSET_DESC)
    collation: str | None = Field(None, max_length=100, description=_COLLATION_DESC)


class DatabaseModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None = None
    current_version: str
    is_active: bool
    charset: str | None = None
    collation: str | None = None
    created_at: datetime
    updated_at: datetime


# ─── Snapshot → blueprint baseline (Plan 09) ──────────────────────────────── #


_OBJECT_TYPES = (
    "table", "view", "materialized_view", "routine", "trigger",
    "sequence", "type", "extension", "index", "event",
)


class SnapshotObjectRef(BaseModel):
    """Referencia a un objeto del snapshot por tipo + nombre (para filtros/buckets)."""

    object_type: str = Field(..., description=f"Uno de: {', '.join(_OBJECT_TYPES)}")
    name: str = Field(..., min_length=1, max_length=64, pattern=_OBJ_NAME)


class SnapshotDataTable(BaseModel):
    """Tabla cuyos DATOS se sembrarán, con el modo de idempotencia del INSERT."""

    table: str = Field(..., min_length=1, max_length=64, pattern=_OBJ_NAME)
    mode: Literal["upsert", "insert_ignore"] = "upsert"


class SnapshotBucket(BaseModel):
    """
    Un bucket del layout ``manual`` = una versión. Es de ESQUEMA (``objects``) XOR de
    DATOS (``data_tables``), nunca ambos. El gateway asigna el número de versión por el
    ORDEN de la lista; el usuario no lo elige.
    """

    name: str | None = Field(None, max_length=200)
    objects: list[SnapshotObjectRef] = Field(default_factory=list)
    data_tables: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one(self) -> "SnapshotBucket":
        has_obj, has_data = bool(self.objects), bool(self.data_tables)
        if has_obj == has_data:
            raise ValueError(
                "cada bucket debe tener 'objects' XOR 'data_tables' (uno de los dos, no ambos ni vacío)"
            )
        return self


class FromSnapshotIn(BaseModel):
    """
    Crea un blueprint NUEVO desde el snapshot de una BD existente (snapshot selectivo).

    Por defecto (``layout='single'``, sin datos) reproduce el baseline estructural
    histórico. Permite ELEGIR qué migrar (include/exclude por tipo/nombre), dividir en
    versiones (``by_class`` o ``manual``) e incluir DATOS-semilla de catálogos. Toda
    migración generada queda atada al motor de origen si trae objetos no portables o
    datos, y nace pendiente de revisión (R1).
    """

    server_id: int = Field(..., ge=1)
    database: str = Field(..., min_length=1, max_length=64, description="BD existente a fotografiar")
    name: str = Field(..., min_length=1, max_length=100, description="Nombre del blueprint a crear")
    slug: str = Field(..., min_length=1, max_length=120, pattern=_SLUG)
    description: str | None = None
    baseline_name: str = Field(
        "Snapshot baseline", min_length=1, max_length=200,
        description="Nombre de la primera versión de esquema (v0001)",
    )

    # --- Selección de objetos --- #
    layout: Literal["single", "by_class", "manual"] = Field(
        "single",
        description=(
            "single: todo en una versión (histórico). by_class: una versión por clase "
            "(tablas→vistas→rutinas→triggers→…). manual: buckets definidos por el usuario."
        ),
    )
    include_object_types: list[str] | None = Field(
        None, description="Si se da, solo estos tipos de objeto se capturan."
    )
    exclude_object_types: list[str] | None = Field(
        None, description="Tipos de objeto a excluir (p. ej. ['routine','trigger'])."
    )
    include_objects: list[SnapshotObjectRef] | None = None
    exclude_objects: list[SnapshotObjectRef] | None = None

    # --- Datos-semilla (opt-in) --- #
    data_tables: list[SnapshotDataTable] = Field(
        default_factory=list,
        description="Tablas cuyos DATOS de catálogo se sembrarán (INSERT idempotente).",
    )
    on_oversize: Literal["skip", "error"] = Field(
        "skip",
        description="Qué hacer si una tabla supera el guardrail de filas/bytes: omitir o 422.",
    )
    confirm_data_rollback: bool = Field(
        False,
        description=(
            "Si true, confirma el rollback por PK de las versiones de datos (DELETE). "
            "Si false, queda solo como sugerencia (el rollback pide confirmación aparte)."
        ),
    )

    # --- Layout manual --- #
    manual_layout: list[SnapshotBucket] | None = Field(
        None, description="Buckets ordenados (solo con layout='manual')."
    )

    @model_validator(mode="after")
    def _check_manual(self) -> "FromSnapshotIn":
        if self.layout == "manual" and not self.manual_layout:
            raise ValueError("layout='manual' requiere 'manual_layout' con al menos un bucket")
        if self.layout != "manual" and self.manual_layout:
            raise ValueError("'manual_layout' solo se permite con layout='manual'")
        for t in (self.include_object_types or []) + (self.exclude_object_types or []):
            if t not in _OBJECT_TYPES:
                raise ValueError(f"tipo de objeto inválido: {t}")
        return self


class SnapshotVersionOut(BaseModel):
    """Resumen de una versión generada por el snapshot (sin SQL ni valores)."""

    version: str
    kind: str  # schema | data
    name: str
    object_counts: dict[str, int] = Field(default_factory=dict)
    has_non_portable: bool = False


class SnapshotSkippedTable(BaseModel):
    """Tabla de datos omitida y su motivo (código estable)."""

    table: str
    reason: str


class FromSnapshotOut(BaseModel):
    """Resultado de from-snapshot: el blueprint creado + resumen de las versiones."""

    model: DatabaseModelOut
    baseline_version: str
    source_engine: str
    has_non_portable: bool
    object_counts: dict[str, int]
    statements_captured: int
    total_versions: int = 1
    data_tables_captured: int = 0
    skipped_tables: list[SnapshotSkippedTable] = Field(default_factory=list)
    versions: list[SnapshotVersionOut] = Field(default_factory=list)


class ModelDatabaseStatusOut(ManagedDatabaseOut):
    """
    Una BD del blueprint con su estado de despliegue.

    Extiende ``ManagedDatabaseOut`` en vez de ser un recurso nuevo: es la misma entidad con
    tres datos más. Un endpoint hermano solo para esto habría duplicado el listado y obligado
    al cliente a cruzar dos respuestas.

    ``model_version`` es la copia que el gateway mantiene, no una lectura en vivo del motor.
    Para forzar la relectura, ``?refresh=true`` (🔌).
    """

    pending_count: int = 0
    pending_versions: list[str] = Field(default_factory=list)
    has_partial_application: bool = Field(
        False,
        description=(
            "Quedaron sentencias a medias de alguna versión. Ojo: 'model_version' NO lo "
            "refleja — Alembic solo registra la versión cuando el upgrade TERMINA."
        ),
    )
