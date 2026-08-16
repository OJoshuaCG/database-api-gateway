"""
Schemas Pydantic del recurso ``ExportJob`` (exportación de una BD a un artefacto).

Todo el vocabulario (los enumerados del §4 del diseño) viene de
``app.services.db_admin.export_spec``: acá NO se redeclara ni un valor. La razón es la
misma por la que la matriz de compatibilidad se publica y se hace cumplir con la MISMA
estructura de datos — dos listas de valores admitidos divergen, y el borde HTTP terminaría
aceptando algo que el módulo puro rechaza (o al revés). Estos schemas son la capa de
transporte; la semántica vive en ``export_spec``.

``ExportSpecIn`` es el espejo Pydantic de ``export_spec.ExportSpec``. Se convierte con
``ExportSpec.from_dict(model_dump(mode="json"))``, que es el MISMO camino por el que se
revive el spec persistido en ``export_jobs.spec`` — así un spec que entra por la API y uno
que vuelve de la base pasan por idéntica validación.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services.db_admin.export_spec import (
    DEFAULT_FILENAME_TEMPLATE,
    AutoincrementMode,
    BinaryEncoding,
    CharsetOverrideMode,
    Compression,
    ConstraintsPlacement,
    DataSelectionMode,
    DefinerMode,
    Delivery,
    EntityDdl,
    Format,
    InsertVariant,
    LineTerminator,
    OnError,
    Organization,
    ScopeDdl,
    SelectionMode,
)

# --------------------------------------------------------------------------- #
# Referencias a objetos                                                        #
# --------------------------------------------------------------------------- #


class ExportObjectRef(BaseModel):
    """Referencia a un objeto de primer nivel (identidad = tipo + nombre)."""

    object_type: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=512)


# --------------------------------------------------------------------------- #
# El ExportSpec (§4) como cuerpo de la petición                                #
# --------------------------------------------------------------------------- #


class ExportStructureIn(BaseModel):
    """
    Opciones de DDL.

    ``scope_ddl``/``entity_ddl`` son ENUMERADOS de cuatro valores y no dos booleanos
    (``drop`` + ``create``) porque con banderas el estado "eliminar sin crear" ES
    representable y hay que parchearlo con validación dispersa; con el enumerado
    sencillamente no existe (§4.1).
    """

    scope_ddl: ScopeDdl = ScopeDdl.NONE
    entity_ddl: EntityDdl = EntityDdl.CREATE
    drop_if_exists: bool = True
    drop_cascade: bool = False
    confirm_scope_drop: str | None = Field(
        None,
        max_length=64,
        description=(
            "Nombre de la BD re-tecleado. Obligatorio si scope_ddl=DROP_CREATE: el "
            "artefacto va a contener un DROP DATABASE."
        ),
    )


class ExportSelectionIn(BaseModel):
    """
    Selección de ESTRUCTURA.

    Los patrones son glob (``fnmatch``) evaluados contra los nombres que devolvió el
    CATÁLOGO del motor. Nunca llegan a una consulta: son filtrado en memoria sobre cadenas
    que el propio motor produjo.
    """

    mode: SelectionMode = SelectionMode.all
    types: list[str] = Field(default_factory=list, max_length=32)
    names: list[str] = Field(default_factory=list, max_length=5000)
    include_patterns: list[str] = Field(default_factory=list, max_length=200)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=200)


class ExportRowFilterIn(BaseModel):
    """Filtro por objeto. ``where`` es la ÚNICA entrada libre que roza una consulta."""

    where: str | None = Field(
        None,
        max_length=4000,
        description=(
            "Condición de lectura simple sobre esa MISMA tabla. Se valida con "
            "query_policy (debe clasificar 'read') y se rechazan subconsultas, CTEs y "
            "referencias a otras tablas, antes de tocar el motor."
        ),
    )
    limit: int | None = Field(None, ge=1)


class ExportDataIn(BaseModel):
    """
    Selección de DATOS (⊆ estructura, §5.3) y forma de las filas.

    Los datos solo salen de TABLAS: no hay forma de pedir "los datos de una vista".
    """

    mode: DataSelectionMode = DataSelectionMode.none
    names: list[str] = Field(default_factory=list, max_length=5000)
    include_patterns: list[str] = Field(default_factory=list, max_length=200)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=200)
    insert_variant: InsertVariant = InsertVariant.insert
    rows_per_statement: int = Field(200, ge=1, le=100_000)
    max_statement_bytes: int = Field(1_048_576, ge=1024)
    include_column_list: bool = True
    per_object: dict[str, ExportRowFilterIn] = Field(default_factory=dict)


class ExportCharsetOverrideIn(BaseModel):
    mode: CharsetOverrideMode = CharsetOverrideMode.keep
    charset: str | None = Field(None, max_length=64)
    collation: str | None = Field(None, max_length=64)


class ExportSanitizeIn(BaseModel):
    """
    Qué se limpia del DDL emitido.

    ``script_comments`` (encabezado y separadores DEL SCRIPT) y ``object_comments``
    (``COMMENT`` del ESQUEMA) son opciones SEPARADAS: los segundos son parte de la
    definición y perderlos es una pérdida de información real.
    """

    script_comments: bool = True
    object_comments: bool = True
    # ``auto`` = "el servidor elige el valor aplicable a este motor" (``omit`` en la familia
    # MySQL, ``keep`` en PostgreSQL). Es el default porque cualquier valor concreto rompe a
    # un motor: con ``omit``, un cuerpo ``{}`` —la llamada canónica— daba 422 contra
    # PostgreSQL, donde la matriz prohíbe ``omit``/``replace``.
    definer: DefinerMode = DefinerMode.auto
    definer_value: str | None = Field(None, max_length=256)
    autoincrement: AutoincrementMode = AutoincrementMode.auto
    engine_specific_options: bool = False
    partitions: bool = True
    constraints_placement: ConstraintsPlacement = ConstraintsPlacement.deferred
    session_preamble: bool = True
    transaction_wrap: bool = False
    charset_override: ExportCharsetOverrideIn = Field(
        default_factory=ExportCharsetOverrideIn
    )


class ExportCsvIn(BaseModel):
    """
    Dialecto del formato delimitado (§8.1). Solo se aplica con ``format='csv'``.

    Los caracteres se validan en ``export_spec`` y no solo acá: un separador de dos
    caracteres o un escape igual a la comilla producen un archivo AMBIGUO, y ese juicio es
    del módulo puro —que es el que también revive un spec persistido— y no del borde HTTP.

    ``null_representation`` es lo que hace distinguibles NULL y cadena vacía: el nulo sale
    SIN comillas y la cadena vacía SIEMPRE cuoteada (``""``), igual que en
    ``COPY … WITH CSV`` de PostgreSQL.
    """

    # El tope de longitud es una cota de cordura, no la regla: que tengan que ser EXACTAMENTE
    # un carácter lo juzga ``export_spec`` para que el rechazo salga con el código estable y
    # la opción culpable (``export.incompatible_option`` + ``field``), y no como un error de
    # validación de Pydantic que el cliente tiene que interpretar aparte.
    delimiter: str = Field(",", max_length=8)
    quote_char: str = Field('"', max_length=8)
    escape_char: str | None = Field(
        None, max_length=8, description="NULL = se duplica la comilla (RFC 4180)."
    )
    line_terminator: LineTerminator = LineTerminator.lf
    header: bool = True
    null_representation: str = Field(
        "",
        max_length=32,
        description=(
            "Texto con el que se escribe un NULL, sin comillas. Vacío = campo vacío. No "
            "puede contener el separador, la comilla ni un salto de línea."
        ),
    )
    bom: bool = Field(
        False,
        description=(
            "Marca de orden de bytes al principio de cada archivo y de cada fragmento. "
            "Excel la necesita para leer UTF-8; no la combines con file_encoding "
            "'utf-8-sig' o saldría duplicada."
        ),
    )


class ExportOutputIn(BaseModel):
    organization: Organization = Organization.single
    split_max_bytes: int | None = Field(None, ge=1024)
    compression: Compression = Compression.none
    filename_template: str = Field(DEFAULT_FILENAME_TEMPLATE, max_length=200)
    # Se valida con la matriz (``export.incompatible_option``) y no con un validador de
    # Pydantic, para que el cliente reciba el mismo código estable que el resto de las
    # opciones de salida. Whitelist: utf-8, utf-8-sig, latin-1, cp1252 (y sus alias).
    file_encoding: str = Field(
        "utf-8",
        max_length=32,
        description=(
            "Codificación del artefacto. Solo utf-8, utf-8-sig, latin-1 o cp1252: el "
            "archivo se escribe por trozos y un códec con estado (utf-16/utf-32) "
            "incrustaría su marca de orden de bytes en cada uno."
        ),
    )
    delivery: Delivery = Delivery.file
    binary_encoding: BinaryEncoding = BinaryEncoding.hex
    schema_manifest: bool = Field(
        False,
        description=(
            "Agrega al artefacto un manifiesto DESCRIPTIVO del esquema (columnas, tipos, "
            "claves, índices, relaciones). Solo json/ndjson. NO es un script: desde ahí no "
            "se restaura nada, y el propio documento lo declara en 'executable: false'."
        ),
    )


class ExportSpecIn(BaseModel):
    """El ``ExportSpec`` del §4 completo. Se persiste íntegro en ``export_jobs.spec``."""

    format: Format = Format.sql
    structure: ExportStructureIn = Field(default_factory=ExportStructureIn)
    selection: ExportSelectionIn = Field(default_factory=ExportSelectionIn)
    data: ExportDataIn = Field(default_factory=ExportDataIn)
    sanitize: ExportSanitizeIn = Field(default_factory=ExportSanitizeIn)
    output: ExportOutputIn = Field(default_factory=ExportOutputIn)
    csv: ExportCsvIn = Field(default_factory=ExportCsvIn)
    on_error: OnError = OnError.continue_
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description=(
            "Clave de reintento. Reenviar la MISMA clave con el MISMO spec devuelve el "
            "plan ya creado en vez de disparar una segunda exportación; con un spec "
            "distinto es 409."
        ),
    )


class ExportCreate(ExportSpecIn):
    """
    Cuerpo de ``POST /servers/{sid}/databases/{db}/database-exports``.

    El cuerpo ES el spec: el servidor y la base salen de la ruta (patrón "por identidad",
    igual que collation-conversion), así que no se repiten acá.
    """


class ExportResolveIn(BaseModel):
    """Resuelve la selección y su cierre de dependencias SIN congelar nada."""

    selection: ExportSelectionIn | None = Field(
        None, description="Reemplaza la selección de estructura del plan. NULL = la del plan."
    )
    data: ExportDataIn | None = Field(
        None, description="Reemplaza la selección de datos del plan. NULL = la del plan."
    )
    auto_resolve_dependencies: bool = Field(
        False,
        description=(
            "Con selección explícita (mode=include), agrega automáticamente las "
            "dependencias faltantes en vez de responder 422. Nunca se recorta en "
            "silencio: lo agregado viaja en 'added'."
        ),
    )


class ExportPreviewIn(BaseModel):
    """
    Valida el spec entero, congela la selección y emite el ``confirm_token`` (§2.3.3).

    ``dry_run_only`` es el modo "solo advertencias" (§15, punto 10): valida y reporta sin
    congelar la selección ni emitir token. Sirve para que el formulario muestre las
    consecuencias mientras el usuario todavía está eligiendo opciones.
    """

    spec: ExportSpecIn | None = Field(
        None, description="Reemplaza el spec del plan. NULL = usa el persistido."
    )
    auto_resolve_dependencies: bool = False
    dry_run_only: bool = False
    include_sample: bool = Field(
        False, description="Pide una muestra corta del artefacto (si el generador puede darla)."
    )


class ExportExecuteIn(BaseModel):
    """
    Confirmación de la ejecución: **doble factor**, igual que el clon y el borrado de BDs.

    ``confirm_target_name`` obliga a identificar CUÁL base se va a leer entera —no solo a
    apretar "sí"— y ``confirm_token`` prueba que lo que se confirma es EXACTAMENTE el plan
    que se previsualizó: si cambió un objeto, su orden o una sola opción del spec, el hash
    cambia y la ejecución se rechaza con 422.
    """

    confirm_target_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Nombre real de la base de datos, re-tecleado.",
    )
    confirm_token: str = Field(
        ...,
        min_length=16,
        max_length=64,
        description="El ``confirm_token`` que devolvió el último preview de ESTE plan.",
    )


# --------------------------------------------------------------------------- #
# Capacidades (§11.1)                                                          #
# --------------------------------------------------------------------------- #


class ExportScopeOut(BaseModel):
    """Ámbito de la exportación. ``scope_note`` declara las limitaciones por motor."""

    kind: str = "database"
    name: str
    scope_note: str | None = None


class ExportFormatOut(BaseModel):
    name: str
    supports_structure: bool | str = Field(
        ...,
        description=(
            "True (sql), False (csv) o 'manifest_only' (json/ndjson: la estructura viaja "
            "como metadato, no como sentencias ejecutables)."
        ),
    )
    supports_data: bool = True
    one_file_per_table: bool = False


class ExportOptionOut(BaseModel):
    """Una opción del spec con sus valores admitidos para ESTE motor."""

    values: list[str] = Field(default_factory=list)
    default: str | bool | int | None = None
    applicable: bool = Field(
        True,
        description=(
            "False = el concepto no existe en este motor. Caso testigo: "
            "'sanitize.definer' en PostgreSQL, donde la propiedad del objeto y SECURITY "
            "DEFINER no son el DEFINER de MySQL."
        ),
    )
    destructive: list[str] = Field(default_factory=list)


class ExportCompatibilityRuleOut(BaseModel):
    """Una regla de la matriz, tal como la evalúa el servidor."""

    when: dict = Field(default_factory=dict)
    forbids: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    reason: str
    blocking: bool = True
    code: str


class ExportCapabilitiesOut(BaseModel):
    """
    Lo que el cliente necesita para construir el formulario sin adivinar.

    La matriz que se publica acá es la MISMA lista que evalúa el servidor: publicar una
    promesa que el servidor no cumple es peor que no publicar nada.
    """

    engine: str
    engine_version: str | None = None
    scope: ExportScopeOut
    object_types: list[str] = Field(default_factory=list)
    formats: list[ExportFormatOut] = Field(default_factory=list)
    options: dict[str, ExportOptionOut] = Field(default_factory=dict)
    compatibility: list[ExportCompatibilityRuleOut] = Field(default_factory=list)
    csv_dialect: dict = Field(
        default_factory=dict,
        description=(
            "Valores por defecto y restricciones del dialecto delimitado, para que el "
            "formulario no tenga que adivinar cuáles son de un solo carácter."
        ),
    )
    packaging: dict = Field(
        default_factory=dict,
        description=(
            "Cómo se empaqueta el artefacto: qué combinaciones producen varios archivos, "
            "en qué contenedor viajan y con qué convención se nombran los fragmentos."
        ),
    )
    limits: dict[str, int] = Field(default_factory=dict)
    error_codes: list[str] = Field(default_factory=list)
    charset_collation_catalog_url: str | None = None


# --------------------------------------------------------------------------- #
# Catálogo (§2.3.2)                                                            #
# --------------------------------------------------------------------------- #


class ExportCatalogObjectOut(BaseModel):
    """Un objeto del catálogo con los metadatos que informan la selección."""

    object_type: str
    name: str
    estimated_rows: int | None = Field(
        None,
        description=(
            "ESTIMACIÓN del catálogo del motor (TABLE_ROWS / reltuples), no un conteo "
            "exacto: contar de verdad exigiría recorrer cada tabla del origen."
        ),
    )
    size_bytes: int | None = Field(
        None,
        description=(
            "Tamaño en disco. Hoy siempre NULL: ningún método del adapter lo expone "
            "todavía (llega con los métodos export_* del §7)."
        ),
    )
    charset: str | None = None
    collation: str | None = None
    has_primary_key: bool | None = None
    has_triggers: bool | None = None
    is_materialized: bool | None = None
    row_filter: bool = Field(
        False, description="El spec del plan define un where/limit para este objeto."
    )


class ExportCatalogOut(BaseModel):
    """
    Catálogo paginado y filtrable.

    No usa el envelope ``paginated()`` porque la respuesta lleva metadatos de CATÁLOGO
    (``scope_note``, tipos presentes, conteo por tipo) que una lista plana no puede
    transportar; la paginación viaja dentro con los mismos campos.
    """

    engine: str
    database: str
    scope_note: str | None = None
    object_types: list[str] = Field(default_factory=list)
    counts_by_type: dict[str, int] = Field(default_factory=dict)
    objects: list[ExportCatalogObjectOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 0
    excluded_internal: list[str] = Field(
        default_factory=list,
        description=(
            "Tablas de contabilidad interna del gateway (_gw_v_/_gw_stg_) descartadas. "
            "Se informan para que nadie las busque en el artefacto y crea que se perdieron."
        ),
    )


# --------------------------------------------------------------------------- #
# Cierre de dependencias (§5.3 / §5.4)                                         #
# --------------------------------------------------------------------------- #


class ExportDependencyEdgeOut(BaseModel):
    from_type: str
    from_name: str
    to_type: str
    to_name: str
    reason: str
    authoritative: bool


class ExportClosureOut(BaseModel):
    """Selección resuelta + qué se agregó, qué se podó y qué quedó advertido."""

    structure: list[ExportObjectRef] = Field(default_factory=list)
    data: list[str] = Field(default_factory=list)
    added: list[ExportObjectRef] = Field(
        default_factory=list,
        description="Objetos AGREGADOS por el cierre (solo con auto_resolve_dependencies).",
    )
    excluded_by_dependency: list[ExportObjectRef] = Field(
        default_factory=list,
        description=(
            "Objetos PODADOS en una selección automática porque una dependencia suya "
            "quedó fuera. Un CREATE INDEX sobre una tabla excluida es un script roto."
        ),
    )
    edges: list[ExportDependencyEdgeOut] = Field(default_factory=list)
    advisory: list[ExportDependencyEdgeOut] = Field(
        default_factory=list,
        description="Referencias detectadas en cuerpos hacia objetos fuera del cierre (best-effort).",
    )
    excluded_internal: list[str] = Field(default_factory=list)
    unknown_names: list[str] = Field(
        default_factory=list, description="Nombres pedidos que el catálogo no tiene."
    )
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Preview (§2.3.3)                                                             #
# --------------------------------------------------------------------------- #


class ExportPlannedObjectOut(BaseModel):
    """Un objeto del plan, en el ORDEN EXACTO en que va a salir en el artefacto."""

    seq: int
    object_type: str
    name: str
    phase: str
    step: int = Field(
        ...,
        description=(
            "Paso fino del orden de emisión (mismo criterio que schema_diff._STEP). Es la "
            "fuente de verdad del orden; 'phase' es una etiqueta legible."
        ),
    )
    with_data: bool = False
    estimated_rows: int | None = None
    deterministic: bool = True


class ExportPreviewOut(BaseModel):
    """Plan resuelto y congelado + estimaciones + avisos + ``confirm_token``."""

    job_id: int
    engine: str
    database: str
    format: str
    scope_note: str | None = None
    objects: list[ExportPlannedObjectOut] = Field(default_factory=list)
    data_tables: list[str] = Field(default_factory=list)
    estimated_rows: int = 0
    estimated_bytes: int = Field(
        0,
        description=(
            "Estimación GRUESA (filas × ancho nominal de las columnas): el catálogo no "
            "expone el tamaño real de una tabla. Sirve para decidir inline y para avisar "
            "de un artefacto enorme, no como cifra exacta."
        ),
    )
    inline_delivery_viable: bool = True
    inline_max_bytes: int = 0
    warnings: list[str] = Field(default_factory=list)
    advisories: list[ExportCompatibilityRuleOut] = Field(
        default_factory=list,
        description="Reglas de la matriz que se cumplen pero NO bloquean (avisos).",
    )
    excluded_by_dependency: list[ExportObjectRef] = Field(default_factory=list)
    sample: str | None = Field(
        None,
        description=(
            "Muestra corta del artefacto. NULL mientras el generador no exista: "
            "inventar una muestra que no salga del writer real sería mentir sobre el "
            "resultado."
        ),
    )
    confirm_token: str | None = Field(
        None,
        description=(
            "Hash del PLAN RESUELTO. NULL en dry_run_only, que valida sin congelar nada."
        ),
    )


# --------------------------------------------------------------------------- #
# Job, ítems y manifiesto                                                      #
# --------------------------------------------------------------------------- #


class ExportSummaryOut(BaseModel):
    """Cabecera + estado del job (lo que consume el polling)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    database_name: str
    database_id: int | None = None
    engine: str
    format: str
    status: str
    phase: str | None = None
    progress: dict | None = None
    error: str | None = None
    expired: bool = False
    structure_drift_detected: bool = False
    has_resolved_selection: bool = False
    idempotency_key: str | None = None
    created_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ExportItemOut(BaseModel):
    """Resultado de un objeto (reporte de incidencias del §14)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    seq: int
    object_type: str
    object_name: str
    phase: str | None = None
    status: str | None = None
    reason: str | None = None
    rows_exported: int | None = None
    bytes_written: int | None = None
    deterministic: bool | None = None
    execution_ms: int | None = None
    executed_at: datetime | None = None


class ExportManifestObjectOut(BaseModel):
    object_type: str
    name: str
    status: str | None = None
    rows_exported: int | None = None
    bytes_written: int | None = None
    deterministic: bool | None = None
    reason: str | None = None


class ExportManifestOut(BaseModel):
    """
    Inventario verificable del artefacto (§10.4).

    Permite comprobar integridad y auditar qué salió **sin abrir el archivo** — que es
    justo lo que se quiere de una exportación de datos.
    """

    job_id: int
    engine: str
    engine_version: str | None = None
    database: str
    format: str
    complete: bool = Field(
        True, description="False = el job no terminó ok; el artefacto es parcial."
    )
    structure_drift_detected: bool = False
    generator_version: str | None = None
    spec: dict = Field(default_factory=dict)
    objects: list[ExportManifestObjectOut] = Field(default_factory=list)
    total_rows: int = 0
    byte_size: int | None = None
    sha256: str | None = None
    part_count: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
