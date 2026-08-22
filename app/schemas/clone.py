"""
Schemas Pydantic del recurso CloneJob (clonación de una BD hacia un servidor destino).

**Todos los schemas de ENTRADA declaran ``extra="forbid"``.** Sin eso Pydantic ignora en
silencio cualquier campo desconocido, y en este módulo ese silencio es peligroso: un typo
(``data.on_existng``) haría que el eje caiga a su default y que el operador ejecute algo
distinto de lo que pidió, sin un solo error. Con ``forbid`` un cliente viejo que mande un
campo retirado recibe un 422 explícito en vez de un clon silenciosamente distinto.

**El SPEC se manda en ``preview``, no en ``create``.** ``create`` fija la identidad de los
dos lados y el modo de contenedor (lo que hay que validar en vivo contra el motor);
``preview`` recibe qué copiar, lo CONGELA y emite el ``confirm_token`` — igual que el
export. El motivo es concreto: el catálogo de objetos del origen solo se puede listar con un
``job_id`` (``GET /database-clones/{id}/objects``), así que exigir la selección en ``create``
obliga a elegir a ciegas y a recrear el plan —con su snapshot en vivo, a 10/min— por cada
retoque.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.db_admin.clone_spec import CopyIntent, DataOnExisting

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


_SELECTION_MODES = Literal["all", "include", "all_except"]
_DATA_SELECTION_MODES = Literal["none", "all", "include", "all_except"]


class CloneStructureSpec(BaseModel):
    """
    Selección DECLARATIVA de estructura. Se resuelve contra el catálogo del origen con
    ``export_spec.resolve_selection`` (la misma función que el export), así que ``all_except``
    y los patrones se comportan igual en los dos módulos.

    ``all_except`` existe porque es el flujo real ("marco todo y quito tres"): expresarlo con
    ``include`` obliga a enumerar el catálogo entero, lista que envejece en cuanto alguien
    crea una tabla.
    """

    model_config = ConfigDict(extra="forbid")

    mode: _SELECTION_MODES = Field(
        "all", description="all = todo el origen; include/all_except usan 'names' y patrones."
    )
    types: list[_OBJECT_TYPES] = Field(
        default_factory=list,
        description="Filtro por tipo de objeto; vacío = todos los tipos del catálogo.",
    )
    names: list[str] = Field(
        default_factory=list,
        description=(
            "Nombres exactos. El match es por NOMBRE, sin tipo: si una tabla y una rutina se "
            "llaman igual, entran las dos — usá 'types' para desambiguar."
        ),
    )
    include_patterns: list[str] = Field(
        default_factory=list,
        description="Patrones fnmatch sobre nombres del catálogo (nunca SQL): 'fact_*'.",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list, description="Patrones a quitar. La exclusión GANA sobre la inclusión."
    )


class CloneDataSpec(BaseModel):
    """
    Selección de datos: es un eje **propio**, no un booleano colgado de la estructura.

    Los datos solo salen de TABLAS. La selección se cierra por FK igual que la de estructura:
    sin ese cierre, copiar 'orders' sin 'customers' inserta filas huérfanas y no falla,
    porque la fase de datos corre con las FKs desactivadas.
    """

    model_config = ConfigDict(extra="forbid")

    mode: _DATA_SELECTION_MODES = Field("none", description="none = no copiar filas.")
    names: list[str] = Field(default_factory=list, description="Nombres de tabla exactos.")
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    on_existing: DataOnExisting | None = Field(
        None,
        description=(
            "Qué hacer si la tabla destino ya tiene filas. OBLIGATORIO con copy='data_only' "
            "y no admitido en los otros modos (allá las tablas las crea este job y nacen "
            "vacías). 'upsert' sobre una tabla SIN clave primaria degrada a INSERT simple, "
            "así que reejecutar el job duplicaría filas: el preview lo avisa."
        ),
    )


class CloneCharsetSpec(BaseModel):
    """
    Charset/collation de la BD destino. Discriminado por ``mode``: con ``keep`` los otros dos
    campos no pueden venir, para que no se acepten y se ignoren en silencio.

    Solo aplica cuando este job CREA la BD. Convertir la collation de una BD existente es
    otra operación y tiene su propio módulo.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["keep", "override"] = Field(
        "keep",
        description=(
            "keep = heredar del origen si es el mismo motor, o el default del motor destino "
            "si es cross-engine (comportamiento histórico). override = el par indicado."
        ),
    )
    charset: str | None = Field(None, max_length=50)
    collation: str | None = Field(None, max_length=100)

    @model_validator(mode="after")
    def _values_only_when_overriding(self) -> "CloneCharsetSpec":
        if self.mode == "keep" and (self.charset is not None or self.collation is not None):
            raise ValueError(
                "Con mode='keep' no se envían charset/collation: se heredan del origen. "
                "Usá mode='override' para elegirlos."
            )
        if self.mode == "override" and self.charset is None and self.collation is None:
            raise ValueError("Con mode='override' hay que indicar charset y/o collation.")
        return self


class CloneCreate(BaseModel):
    """
    Crea un PLAN de clonación.

    Origen: EXACTAMENTE una representación — ``source_database_id`` (BD registrada) o
    ``source_server_id`` + ``source_database_name`` (BD cruda de cualquier servidor).

    Destino: SIEMPRE por ``target_server_id`` + ``target_database_name`` (puede no existir
    todavía si ``target_mode='new'``). ``target_database_id`` es opcional e informativo
    (solo si esa BD ya está en el inventario).

    Acá NO va el spec de qué copiar: eso se manda en ``preview`` (ver el docstring del
    módulo). ``include_data`` y ``selection`` se siguen aceptando como atajo LEGACY y se
    traducen al spec nuevo.
    """

    model_config = ConfigDict(extra="forbid")

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
    include_data: bool = Field(
        False,
        description=(
            "LEGACY. Atajo de copy='structure_and_data' (True) / 'structure_only' (False). "
            "Se traduce al spec nuevo; no se puede combinar con enviar 'copy' en el preview."
        ),
    )
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
        description=(
            "Refs EXACTAS (tipo + nombre) a clonar; NULL = clon COMPLETO. Es la vía que usa "
            "el wizard tras marcar objetos del catálogo. Alternativa declarativa: el bloque "
            "'structure' del preview."
        ),
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

    model_config = ConfigDict(extra="forbid")

    selection: list[CloneObjectRef] = Field(..., min_length=1)


class ClonePreviewIn(BaseModel):
    """
    Manda el SPEC, lo congela y devuelve el plan exacto + el ``confirm_token``.

    **Todos los campos son opcionales y solo se aplica lo que VIENE.** Un campo ausente deja
    el valor que el plan ya tenía; no lo borra. Antes ``preview`` persistía
    ``selection = None`` en cada llamada, así que un ``POST /preview {}`` descartaba la
    selección que el operador había armado y devolvía —con token válido— el plan de un clon
    completo.
    """

    model_config = ConfigDict(extra="forbid")

    selection: list[CloneObjectRef] | None = Field(
        None,
        description=(
            "Refs EXACTAS. NULL explícito = clon completo. Mutuamente excluyente con "
            "'structure'."
        ),
    )
    copy_intent: CopyIntent | None = Field(
        None,
        description=(
            "structure_only | structure_and_data | data_only. 'data_only' copia SOLO filas: "
            "no emite una sola sentencia de DDL, así que exige un destino existente, "
            "clean_mode='none' y data.on_existing explícito."
        ),
    )
    structure: CloneStructureSpec | None = None
    data: CloneDataSpec | None = None
    target_charset: CloneCharsetSpec | None = None
    target_owner_user_id: int | None = Field(
        None, ge=1,
        description=(
            "ServerUser del servidor DESTINO que será OWNER de la BD creada. Solo "
            "PostgreSQL: en MySQL/MariaDB una base no tiene dueño y el campo no aplica."
        ),
    )

    @model_validator(mode="after")
    def _one_selection_language(self) -> "ClonePreviewIn":
        sent = self.model_fields_set
        if "selection" in sent and "structure" in sent:
            raise ValueError(
                "Elegí UNA forma de seleccionar la estructura: 'selection' (refs exactas) o "
                "'structure' (declarativa). Enviar las dos deja dos respuestas para la "
                "misma pregunta."
            )
        return self


class CloneExecuteIn(BaseModel):
    """Confirma y ENCOLA la ejecución asíncrona del clon."""

    model_config = ConfigDict(extra="forbid")

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
        None, description="Estimación de filas del catálogo (solo tablas, si se copian datos)."
    )
    row_estimate_known: bool = Field(
        True,
        description=(
            "False = el catálogo del motor NO sabe cuántas filas hay (PostgreSQL sin "
            "ANALYZE, TABLE_ROWS en NULL). Sin este campo, 'row_estimate: 0' es "
            "indistinguible de una tabla vacía y una tabla de millones se muestra como vacía."
        ),
    )
    has_primary_key: bool | None = Field(
        None,
        description="Solo tablas. False = un 'upsert' sobre ella degrada a INSERT simple.",
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
    row_estimate_known: bool = True
    has_primary_key: bool | None = None
    upsert: bool


class CloneCompatIssueOut(BaseModel):
    """
    Una incompatibilidad entre el esquema del origen y el del DESTINO, para una tabla que va
    a recibir filas. ``reason`` es de vocabulario CERRADO (nunca el mensaje del motor).
    """

    table: str
    reason: str
    blocking: bool
    column: str | None = None
    detail: dict = Field(default_factory=dict)


class CloneNoticeOut(BaseModel):
    """
    Aviso con CÓDIGO estable, para que el cliente lo mapee a su propio texto y a su propio
    peso visual en vez de matchear prosa con expresiones regulares.

    Convive con ``warnings: list[str]``, que se mantiene con su tipo original a propósito: el
    cliente valida la respuesta entera contra su schema y la descarta completa si algo no
    encaja, así que cambiarle el tipo a un campo existente rompería el polling de un clon en
    curso. ``notices`` es la versión estructurada del MISMO contenido.
    """

    code: str
    message: str
    severity: Literal["info", "warning"] = "warning"
    detail: dict = Field(default_factory=dict)


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
    # ---- Valores EFECTIVOS ya resueltos por el servidor ---------------------- #
    # El cliente renderiza lo que el servidor decidió en vez de re-derivarlo: si el
    # formulario reimplementa las reglas, las dos implementaciones divergen en silencio.
    copy_intent: str = Field(..., description="La intención EFECTIVA del plan congelado.")
    data_on_existing: str | None = Field(
        None, description="Resuelto: 'append' | 'upsert' | null si no se copian filas."
    )
    target_charset: str | None = Field(
        None, description="Charset canónico con el que se creará la BD destino (null = heredado)."
    )
    target_collation: str | None = None
    target_owner: str | None = Field(
        None, description="Username del OWNER que se pasará al CREATE DATABASE (solo PG)."
    )
    notices: list[CloneNoticeOut] = Field(default_factory=list)
    blocking_issues: list[CloneCompatIssueOut] = Field(
        default_factory=list,
        description=(
            "Incompatibilidades que IMPIDEN ejecutar. Si viene con contenido, "
            "'confirm_token' llega vacío: el plan se puede ver pero no confirmar."
        ),
    )
    confirm_token: str = Field(
        "", description="Vacío cuando hay 'blocking_issues'."
    )
