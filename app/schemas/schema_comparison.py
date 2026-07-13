"""Schemas Pydantic del recurso SchemaComparison (diff estructural entre dos BDs)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.model_migration import MigrationApplyOut, ModelMigrationOut


# --------------------------------------------------------------------------- #
# Entrada                                                                      #
# --------------------------------------------------------------------------- #
class SchemaComparisonCreate(BaseModel):
    source_database_id: int = Field(
        ..., ge=1, description="BD de referencia (estado deseado)."
    )
    target_database_id: int = Field(
        ..., ge=1, description="BD que se modificaría (recibe el DDL derivado)."
    )


class AdoptComparisonIn(BaseModel):
    """Opción A: adoptar el DDL seleccionado como nueva versión del blueprint del target."""

    selected_item_ids: list[int] = Field(
        ..., min_length=1, description="IDs de las sentencias a incluir en la nueva versión."
    )
    name: str = Field(..., min_length=1, max_length=200, description="Nombre de la versión.")
    description: str | None = Field(
        None, max_length=1000, description="Descripción opcional (no persistida hoy)."
    )
    execute_immediately: bool = Field(
        False,
        description=(
            "Si true, aplica la versión recién creada al target por el camino normal "
            "(ManagedMigrationController.apply, con todos sus guards)."
        ),
    )


class ExecutePreviewIn(BaseModel):
    """
    Resuelve un modo/selección de Opción B SIN ejecutar nada: devuelve las sentencias
    exactas y el ``confirm_token`` a reenviar en ``POST .../execute``. El frontend no
    puede calcular ese token por su cuenta (requeriría replicar el filtro por
    ``risk_flags`` sobre TODOS los ítems paginados y el formato exacto de serialización
    del servidor) — este es el único camino soportado para obtenerlo.
    """

    mode: Literal["all", "all_except_destructive", "custom"] = Field(...)
    selected_item_ids: list[int] | None = Field(
        None, description="Requerido si mode=custom."
    )


class ExecuteComparisonIn(BaseModel):
    """Opción B: ejecución directa ad-hoc sobre el target (solo BDs SIN blueprint)."""

    mode: Literal["all", "all_except_destructive", "custom"] = Field(
        ...,
        description=(
            "all = todo salvo objetos que requieren revisión individual; "
            "all_except_destructive = además excluye lo destructivo; "
            "custom = exactamente selected_item_ids."
        ),
    )
    selected_item_ids: list[int] | None = Field(
        None, description="Requerido si mode=custom: IDs exactos de las sentencias a ejecutar."
    )
    confirm_target_name: str = Field(
        ...,
        min_length=1,
        description="Doble intención: debe coincidir con el nombre de la BD target.",
    )
    confirm_token: str = Field(
        ...,
        min_length=1,
        description=(
            "Hash (SHA256) del conjunto EXACTO a ejecutar. Recomputado server-side; "
            "solo se usa para comparar. Liga la confirmación al DDL mostrado."
        ),
    )


# --------------------------------------------------------------------------- #
# Salida                                                                       #
# --------------------------------------------------------------------------- #
class SchemaComparisonSummaryOut(BaseModel):
    """Resumen de una comparación (cabecera + conteos)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_database_id: int
    target_database_id: int
    source_engine: str
    target_engine: str
    cross_flavor_warning: bool = False
    scope_note: str | None = None
    item_count: int = 0
    counts: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description="object_type -> change_type -> nº de objetos distintos.",
    )
    has_destructive: bool = False
    expired: bool = False
    created_at: datetime
    expires_at: datetime


class SchemaComparisonItemOut(BaseModel):
    """Una sentencia DDL derivada, con su riesgo y (si se ejecutó) su resultado."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    comparison_id: int
    seq: int
    object_type: str
    object_name: str
    change_type: str
    phase: int
    sql: str
    risk_flags: dict = Field(default_factory=dict)
    down_sql: str | None = None
    down_confirmed: bool = False
    execution_status: str | None = None
    execution_error: str | None = None
    executed_at: datetime | None = None


class AdoptComparisonOut(BaseModel):
    """Resultado de adoptar una comparación como versión de blueprint (Opción A)."""

    comparison_id: int
    model_id: int
    version: str
    statements: int = Field(0, description="Nº de sentencias incluidas en la versión.")
    executed: bool = False
    migration: ModelMigrationOut
    apply_result: MigrationApplyOut | None = None


class ExecutePreviewStatementOut(BaseModel):
    item_id: int
    object_type: str
    object_name: str
    sql: str
    risk_flags: dict = Field(default_factory=dict)


class ExecutePreviewOut(BaseModel):
    """Resultado de resolver un modo/selección: sentencias exactas + token a reenviar."""

    comparison_id: int
    target_database_id: int
    mode: str
    statements: list[ExecutePreviewStatementOut] = Field(default_factory=list)
    confirm_token: str


class ExecuteStatementResultOut(BaseModel):
    item_id: int
    object_type: str
    object_name: str
    status: str  # applied | failed | skipped
    error: str | None = None
    execution_ms: int | None = None


class ExecuteComparisonOut(BaseModel):
    """Resultado de la ejecución directa ad-hoc (Opción B)."""

    comparison_id: int
    target_database_id: int
    mode: str
    total: int = 0
    applied_count: int = 0
    failed: bool = False
    statements: list[ExecuteStatementResultOut] = Field(default_factory=list)
