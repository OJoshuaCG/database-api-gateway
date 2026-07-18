"""Schemas Pydantic del recurso CloneJob (clonación de una BD hacia un servidor destino)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_OBJECT_TYPES = Literal[
    "table", "view", "materialized_view", "routine", "trigger",
    "sequence", "enum_type", "extension", "event",
]


# --------------------------------------------------------------------------- #
# Entrada                                                                      #
# --------------------------------------------------------------------------- #
class CloneObjectRef(BaseModel):
    """Referencia a un objeto de primer nivel (tipo + nombre)."""

    object_type: _OBJECT_TYPES
    name: str = Field(..., min_length=1, max_length=512)


class CloneCreate(BaseModel):
    """
    Crea un PLAN de clonación.

    Origen: EXACTAMENTE una representación — ``source_database_id`` (BD registrada) o
    ``source_server_id`` + ``source_database_name`` (BD cruda de cualquier servidor).

    Destino: SIEMPRE por ``target_server_id`` + ``target_database_name`` (puede no existir
    todavía si ``target_mode='new'``). ``target_database_id`` es opcional e informativo
    (solo si esa BD ya está en el inventario).
    """

    # Origen
    source_database_id: int | None = Field(None, ge=1)
    source_server_id: int | None = Field(None, ge=1)
    source_database_name: str | None = Field(None, min_length=1, max_length=64)

    # Destino
    target_server_id: int = Field(..., ge=1)
    target_database_name: str = Field(..., min_length=1, max_length=64)
    target_database_id: int | None = Field(
        None, ge=1, description="managed_database_id del destino si ya está en inventario."
    )
    target_mode: Literal["new", "existing"] = Field(
        ..., description="new = crear la BD destino; existing = usar una BD ya existente."
    )

    # Opciones
    include_data: bool = Field(False, description="True = estructura + datos; False = solo estructura.")
    clean_mode: Literal["none", "objects", "drop_database"] = Field(
        "none",
        description=(
            "Solo aplica a target existente. none = preservar; objects = borrar objeto por "
            "objeto (preserva la BD y su config); drop_database = reset total (recrea la BD)."
        ),
    )
    adopt_target: bool = Field(
        False,
        description=(
            "Si el origen es una BD gestionada con blueprint y el clon es COMPLETO, adopta el "
            "destino y le asigna/stampa el blueprint y versión del origen. Ignorado en clon parcial."
        ),
    )
    adopt_owner_id: int | None = Field(
        None, ge=1,
        description="ServerUser del servidor DESTINO que será owner del registro adoptado. Requerido si adopt_target.",
    )
    selection: list[CloneObjectRef] | None = Field(
        None,
        description="Objetos a clonar; NULL = clon COMPLETO (toda la estructura del origen).",
    )

    @model_validator(mode="after")
    def _exactly_one_source_representation(self) -> "CloneCreate":
        by_id = self.source_database_id is not None
        by_raw = self.source_server_id is not None or self.source_database_name is not None
        if by_id and by_raw:
            raise ValueError(
                "Para 'source' indica SOLO source_database_id, o SOLO "
                "(source_server_id + source_database_name), nunca ambas."
            )
        if not by_id and not by_raw:
            raise ValueError(
                "Para 'source' falta la identificación: source_database_id, o "
                "(source_server_id + source_database_name)."
            )
        if by_raw and not (self.source_server_id is not None and self.source_database_name is not None):
            raise ValueError(
                "Para 'source' por servidor, source_server_id y source_database_name son AMBOS obligatorios."
            )
        if self.adopt_target and self.adopt_owner_id is None:
            raise ValueError("adopt_target=true requiere adopt_owner_id (owner del servidor destino).")
        return self


class CloneResolveSelectionIn(BaseModel):
    """Resuelve el cierre de dependencias de una selección (para el auto-select de la UI)."""

    selection: list[CloneObjectRef] = Field(..., min_length=1)


class ClonePreviewIn(BaseModel):
    """
    Resuelve la selección/opciones finales SIN ejecutar: devuelve el resumen exacto de lo
    que se hará + el ``confirm_token`` a reenviar en ``/execute``.
    """

    selection: list[CloneObjectRef] | None = Field(
        None, description="NULL = clon completo. Reemplaza la selección del plan."
    )


class CloneExecuteIn(BaseModel):
    """Confirma y ENCOLA la ejecución asíncrona del clon."""

    confirm_target_name: str = Field(
        ..., min_length=1, description="Doble intención: debe coincidir con el nombre de la BD destino."
    )
    confirm_token: str = Field(
        ..., min_length=1, description="Token del preview (recomputado server-side; solo se compara)."
    )
    force: bool = Field(
        False, description="Forzar si el destino gestionado está en cuarentena (status=error)."
    )


# --------------------------------------------------------------------------- #
# Salida                                                                        #
# --------------------------------------------------------------------------- #
class CloneObjectOut(BaseModel):
    """Un objeto del origen con su portabilidad al motor destino y estimación de filas."""

    object_type: str
    name: str
    portable: bool = Field(..., description="True si el objeto puede clonarse al motor destino.")
    portability_reason: str | None = Field(
        None, description="Motivo si no es portable (p. ej. cuerpo procedural cross-engine)."
    )
    row_estimate: int | None = Field(
        None, description="Estimación de filas (solo tablas, si include_data)."
    )


class CloneDependencyEdgeOut(BaseModel):
    from_type: str
    from_name: str
    to_type: str
    to_name: str
    reason: str
    authoritative: bool


class CloneClosureOut(BaseModel):
    """Cierre de dependencias resuelto (autoritativo) + sugerencias advisory."""

    selected: list[CloneObjectRef] = Field(default_factory=list)
    added: list[CloneObjectRef] = Field(default_factory=list)
    closure: list[CloneObjectRef] = Field(default_factory=list)
    edges: list[CloneDependencyEdgeOut] = Field(default_factory=list)
    advisory: list[CloneDependencyEdgeOut] = Field(default_factory=list)
    table_order: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CloneInventoryOut(BaseModel):
    """Inventario completo de objetos del origen + grafo de dependencias + portabilidad."""

    objects: list[CloneObjectOut] = Field(default_factory=list)
    authoritative_edges: list[CloneDependencyEdgeOut] = Field(default_factory=list)
    advisory_edges: list[CloneDependencyEdgeOut] = Field(default_factory=list)
    cross_engine: bool = False
    scope_note: str | None = None


class CloneSummaryOut(BaseModel):
    """Cabecera + estado de un job de clonación."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_server_id: int
    source_database_name: str
    source_database_id: int | None = None
    source_engine: str
    target_server_id: int
    target_database_name: str
    target_database_id: int | None = None
    target_engine: str
    target_mode: str
    include_data: bool
    clean_mode: str
    adopt_target: bool
    cross_engine: bool = False
    status: str
    phase: str | None = None
    progress: dict | None = None
    error: str | None = None
    expired: bool = False
    created_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CloneItemOut(BaseModel):
    """Un paso ejecutado del job (limpieza/estructura/datos/adopt) con su resultado."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    seq: int
    kind: str
    object_type: str
    object_name: str
    status: str | None = None
    error: str | None = None
    rows_copied: int | None = None
    execution_ms: int | None = None
    executed_at: datetime | None = None


class ClonePreviewStatementOut(BaseModel):
    kind: str  # clean | structure
    object_type: str
    object_name: str
    sql: str


class ClonePreviewDataTableOut(BaseModel):
    table: str
    row_estimate: int | None = None
    upsert: bool


class ClonePreviewOut(BaseModel):
    """Resultado de resolver el plan final SIN ejecutar: qué se hará + confirm_token."""

    job_id: int
    target_database_id: int | None = None
    cross_engine: bool = False
    clean_statements: list[ClonePreviewStatementOut] = Field(default_factory=list)
    structure_statements: list[ClonePreviewStatementOut] = Field(default_factory=list)
    data_tables: list[ClonePreviewDataTableOut] = Field(default_factory=list)
    skipped: list[CloneObjectOut] = Field(default_factory=list)
    will_adopt: bool = False
    warnings: list[str] = Field(default_factory=list)
    confirm_token: str
