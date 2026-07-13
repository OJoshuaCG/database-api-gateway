"""Schemas Pydantic del recurso SchemaComparison (diff estructural entre dos BDs)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.model_migration import MigrationApplyOut, ModelMigrationOut


# --------------------------------------------------------------------------- #
# Entrada                                                                      #
# --------------------------------------------------------------------------- #
class SchemaComparisonCreate(BaseModel):
    """
    Cada lado (source/target) se identifica de forma INDEPENDIENTE por UNA de dos vías:

    - ``X_database_id``: una ``ManagedDatabase`` ya registrada en el inventario, o
    - ``X_server_id`` + ``X_database_name``: una BD CRUDA de un servidor dado de alta,
      aunque nunca se haya registrado en el inventario del gateway.

    El ``model_validator`` exige EXACTAMENTE una de las dos representaciones por lado
    (nunca ambas, nunca ninguna). Así se puede comparar cualquier BD de un servidor —
    no solo las adoptadas/provisionadas.
    """

    source_database_id: int | None = Field(
        None, ge=1, description="BD de referencia registrada (managed_database_id)."
    )
    source_server_id: int | None = Field(
        None, ge=1, description="Servidor del source (con source_database_name, BD cruda)."
    )
    source_database_name: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Nombre de la BD source en el motor (con source_server_id, BD cruda).",
    )
    target_database_id: int | None = Field(
        None, ge=1, description="BD a modificar registrada (managed_database_id)."
    )
    target_server_id: int | None = Field(
        None, ge=1, description="Servidor del target (con target_database_name, BD cruda)."
    )
    target_database_name: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Nombre de la BD target en el motor (con target_server_id, BD cruda).",
    )

    @model_validator(mode="after")
    def _exactly_one_representation_per_side(self) -> "SchemaComparisonCreate":
        self._validate_side(
            "source",
            self.source_database_id,
            self.source_server_id,
            self.source_database_name,
        )
        self._validate_side(
            "target",
            self.target_database_id,
            self.target_server_id,
            self.target_database_name,
        )
        return self

    @staticmethod
    def _validate_side(
        side: str, database_id: int | None, server_id: int | None, database_name: str | None
    ) -> None:
        by_id = database_id is not None
        by_raw = server_id is not None or database_name is not None
        if by_id and by_raw:
            raise ValueError(
                f"Para '{side}' indica SOLO {side}_database_id, o SOLO "
                f"({side}_server_id + {side}_database_name), nunca ambas representaciones."
            )
        if not by_id and not by_raw:
            raise ValueError(
                f"Para '{side}' falta la identificación: indica {side}_database_id, o "
                f"({side}_server_id + {side}_database_name)."
            )
        if by_raw and not (server_id is not None and database_name is not None):
            raise ValueError(
                f"Para '{side}' por servidor, {side}_server_id y {side}_database_name "
                f"son AMBOS obligatorios."
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
    # Siempre poblados: identifican la BD física de cada lado (servidor + nombre), venga
    # el input por managed_database_id o por (server_id + database_name) crudo.
    source_server_id: int
    source_database_name: str
    target_server_id: int
    target_database_name: str
    # managed_database_id de cada lado si esa BD está en el inventario; NULL si es una BD
    # cruda no registrada. El frontend lo usa para saber si mostrar la Opción A (adopt).
    source_database_id: int | None = None
    target_database_id: int | None = None
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
    # NULL si el target es una BD cruda no registrada en el inventario.
    target_database_id: int | None = None
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
    # NULL si el target es una BD cruda no registrada en el inventario.
    target_database_id: int | None = None
    mode: str
    total: int = 0
    applied_count: int = 0
    failed: bool = False
    statements: list[ExecuteStatementResultOut] = Field(default_factory=list)
