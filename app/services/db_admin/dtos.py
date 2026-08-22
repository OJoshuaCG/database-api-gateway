"""
DTOs de retorno de los adaptadores. Son Pydantic models para serializarse
directamente en las respuestas de la API (`ApiResponse[T]`). NUNCA contienen
datos de filas de las tablas gestionadas: solo estructura/metadatos.
"""

import enum

from pydantic import BaseModel, ConfigDict, Field


class GrantLevel(str, enum.Enum):
    """
    Nivel de entidad sobre el que se otorga/revoca un privilegio.

    Fase 1 (object-level): DATABASE, SCHEMA (solo PG), TABLE, COLUMN,
    SEQUENCE (solo PG), ROUTINE. GLOBAL y los niveles raros (TYPE, LANGUAGE,
    FDW, ...) y la membresía de roles se incorporan en fases posteriores
    (ver docs/plans/07-gestion-granular-de-permisos.md).
    """

    GLOBAL = "global"
    DATABASE = "database"
    SCHEMA = "schema"
    TABLE = "table"
    COLUMN = "column"
    SEQUENCE = "sequence"
    ROUTINE = "routine"


class ConnectionInfo(BaseModel):
    ok: bool
    dialect: str
    server_version: str | None = None


class EngineUserInfo(BaseModel):
    username: str
    host: str | None = None  # solo MySQL/MariaDB


class DatabaseGranteeInfo(BaseModel):
    """
    Usuario/rol con ALGÚN privilegio sobre una base de datos (consulta INVERSA, agrupada
    por grantee). ``levels`` lista los niveles por los que tiene relación con la BD
    (``global``/``database``/``table``/``column``/``schema``/``sequence``/``routine``).
    ``is_global=True`` (solo MySQL/MariaDB) marca privilegios globales ``*.*`` que aplican
    a TODAS las BDs, no solo a esta. ``host`` es None en PostgreSQL (un ROLE no tiene host).
    """

    username: str
    host: str | None = None
    privileges: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)
    is_global: bool = False


class ExternalFkDependent(BaseModel):
    """
    FK desde una tabla de OTRA base de datos del mismo servidor hacia una tabla de la BD
    consultada. Solo aplica a motores con FKs cross-database (MySQL/MariaDB) — PostgreSQL
    no las soporta por arquitectura (una BD no puede referenciar tablas de otra BD).
    """

    schema_name: str  # BD de la tabla que tiene la FK (NO la BD consultada)
    table: str
    column: str
    constraint: str | None = None
    referenced_table: str  # tabla de la BD consultada
    referenced_column: str


class RoutineRef(BaseModel):
    """Identidad de una rutina para grants de EXECUTE/ALTER ROUTINE."""

    kind: str  # FUNCTION | PROCEDURE
    name: str


class ObjectRef(BaseModel):
    """
    Objeto destino de un GRANT/REVOKE. Los campos relevantes dependen del nivel:
    DATABASE→database; SCHEMA(PG)→database+schema; TABLE/COLUMN→database[+schema]+table
    (+columns); SEQUENCE(PG)→database+schema+sequence; ROUTINE→database[+schema]+routine.
    `schema` solo aplica a PostgreSQL (default 'public').
    """

    model_config = ConfigDict(populate_by_name=True)

    database: str | None = None
    db_schema: str | None = Field(default=None, alias="schema")
    table: str | None = None
    columns: list[str] = Field(default_factory=list)
    sequence: str | None = None
    routine: RoutineRef | None = None


class GrantInfo(BaseModel):
    """Un privilegio efectivo de un grantee (resultado de la introspección)."""

    level: GrantLevel
    object: str | None = None  # objeto cualificado (p.ej. "appdb.items"); None = global
    privileges: list[str]
    with_grant_option: bool = False


class ComputedInfo(BaseModel):
    """Columna generada/computada (``GENERATED ALWAYS AS (...)``)."""

    sqltext: str  # expresión generadora (tal cual la reporta el motor)
    persisted: bool = False  # STORED (True) vs VIRTUAL (False)


class IdentityInfo(BaseModel):
    """Columna IDENTITY de PostgreSQL (``GENERATED {ALWAYS|BY DEFAULT} AS IDENTITY``)."""

    always: bool = False  # ALWAYS (True) vs BY DEFAULT (False)
    start: int | None = None
    increment: int | None = None


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    default: str | None = None
    primary_key: bool = False
    autoincrement: bool = False
    comment: str | None = None
    # --- extendido (Plan diff estructural) ---------------------------------- #
    collation: str | None = None  # collation EFECTIVO de la columna (ver nota de herencia)
    charset: str | None = None  # solo MySQL/MariaDB (PG no tiene charset por columna)
    computed: ComputedInfo | None = None
    identity: IdentityInfo | None = None
    on_update: str | None = None  # MySQL ``ON UPDATE CURRENT_TIMESTAMP`` (de EXTRA)


class ForeignKeyInfo(BaseModel):
    name: str | None = None
    columns: list[str]
    referred_table: str
    referred_columns: list[str]
    # --- extendido: opciones referenciales (de get_foreign_keys()['options']) - #
    on_delete: str | None = None  # CASCADE | SET NULL | RESTRICT | NO ACTION | SET DEFAULT
    on_update: str | None = None
    deferrable: bool | None = None  # PG
    initially: str | None = None  # PG: DEFERRED | IMMEDIATE
    referred_schema: str | None = None


class IndexInfo(BaseModel):
    name: str | None
    columns: list[str]
    unique: bool
    # --- extendido ---------------------------------------------------------- #
    method: str | None = None  # btree | hash | gin | gist | ... (PG: postgresql_using)
    predicate: str | None = None  # índice parcial PG (postgresql_where)
    expressions: list[str] = Field(default_factory=list)  # índice funcional (expr por columna)
    column_sort: dict[str, list[str]] = Field(
        default_factory=dict,
        description="col -> ['desc','nulls_first'] cuando el orden no es el default.",
    )
    include_columns: list[str] = Field(default_factory=list)  # PG INCLUDE (covering)


class CheckConstraintInfo(BaseModel):
    """CHECK constraint. Se compara por ``sqltext`` normalizado, no por nombre."""

    name: str | None = None
    sqltext: str


class UniqueConstraintInfo(BaseModel):
    """UNIQUE constraint (distinta de un índice único). Se compara por columnas."""

    name: str | None = None
    columns: list[str]


class TableSchema(BaseModel):
    database: str
    table: str
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKeyInfo]
    indexes: list[IndexInfo]
    # --- extendido (Plan diff estructural) ---------------------------------- #
    comment: str | None = None
    storage_options: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Opciones por tabla/BD: ``engine``, ``charset``, ``collation`` (default de "
            "la tabla) y ``db_collation``/``db_charset`` (default de la BD, para resolver "
            "la herencia de collation a nivel columna). Solo MySQL/MariaDB llenan engine."
        ),
    )
    check_constraints: list[CheckConstraintInfo] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraintInfo] = Field(default_factory=list)
    primary_key_name: str | None = None  # nombre del constraint PK (secundario/informativo)


# --------------------------------------------------------------------------- #
# Snapshot estructural CANÓNICO (Plan diff) — objetos parseados a DTOs           #
# --------------------------------------------------------------------------- #
# A diferencia de ``StructureDump`` (DDL de texto para re-aplicar), estos DTOs      #
# describen la IDENTIDAD ESTRUCTURAL comparable de cada objeto. Los cuerpos          #
# procedurales (vistas/rutinas/triggers/events) se guardan como texto normalizado    #
# porque no hay diff semántico fiable (ver schema_diff.py).                          #
class ViewInfo(BaseModel):
    name: str
    is_materialized: bool = False
    definition: str  # cuerpo tal cual del motor (DEFINER ya saneado)
    columns: list[str] = Field(default_factory=list)  # nombres de columna (para CREATE OR REPLACE)
    check_option: str | None = None  # CASCADED | LOCAL (WITH CHECK OPTION)
    security_definer: bool = False


class RoutineParam(BaseModel):
    name: str | None = None
    mode: str | None = None  # IN | OUT | INOUT | VARIADIC
    type: str = ""


class RoutineInfo(BaseModel):
    name: str
    kind: str  # FUNCTION | PROCEDURE
    parameters: list[RoutineParam] = Field(default_factory=list)
    return_type: str | None = None
    language: str | None = None
    volatility: str | None = None  # PG: IMMUTABLE | STABLE | VOLATILE
    deterministic: bool | None = None  # MySQL
    security_definer: bool = False
    body: str  # DDL completo (CREATE ...), DEFINER saneado, normalizado para comparar


class TriggerInfo(BaseModel):
    name: str
    table: str
    timing: str | None = None  # BEFORE | AFTER | INSTEAD OF
    events: list[str] = Field(default_factory=list)  # INSERT | UPDATE | DELETE
    level: str | None = None  # ROW | STATEMENT
    when_condition: str | None = None
    action: str  # DDL completo del trigger (DEFINER saneado, normalizado)


class SequenceInfo(BaseModel):
    """Secuencia standalone. NUNCA incluye ``last_value`` (estado, no estructura)."""

    name: str
    data_type: str | None = None
    increment: int | None = None
    min_value: int | None = None
    max_value: int | None = None
    start_value: int | None = None
    cycle: bool = False


class EnumTypeInfo(BaseModel):
    """Tipo ENUM de PostgreSQL (objeto de catálogo). Los valores van ORDENADOS."""

    name: str
    values: list[str]


class ExtensionInfo(BaseModel):
    """Extensión de PostgreSQL. ``version`` es COSMÉTICO (no dispara diff)."""

    name: str
    version: str | None = None


class EventInfo(BaseModel):
    """Evento del scheduler de MySQL/MariaDB."""

    name: str
    schedule: str | None = None
    body: str  # DDL completo (DEFINER saneado, normalizado)


class SchemaSnapshot(BaseModel):
    """
    Snapshot estructural canónico y COMPLETO de una BD (solo estructura, jamás filas).

    Es la entrada del motor de diff puro (``schema_diff.diff_snapshots``). ``captured_at``
    y la versión de extensión son COSMÉTICOS (no disparan diff); el resto es estructural.
    PostgreSQL cubre solo el schema ``public`` (limitación conocida, ver ``scope_note``).
    """

    database: str
    source_engine: str  # 'mysql' | 'mariadb' | 'postgresql'
    captured_at: str | None = None
    # Default de charset/collation A NIVEL DE BASE DE DATOS (el que hereda toda tabla/columna
    # nueva sin cláusula explícita). NO dispara diff — su propósito es que el clon pueda crear la
    # BD destino con el mismo default que la origen (mismo motor). MySQL/MariaDB lo llena desde
    # ``information_schema.SCHEMATA``; PostgreSQL best-effort desde ``pg_database``; ``None`` si no
    # se pudo determinar (el clon cae al default del motor).
    db_charset: str | None = None
    db_collation: str | None = None
    tables: list[TableSchema] = Field(default_factory=list)
    views: list[ViewInfo] = Field(default_factory=list)
    routines: list[RoutineInfo] = Field(default_factory=list)
    triggers: list[TriggerInfo] = Field(default_factory=list)
    sequences: list[SequenceInfo] = Field(default_factory=list)
    enum_types: list[EnumTypeInfo] = Field(default_factory=list)
    extensions: list[ExtensionInfo] = Field(default_factory=list)
    events: list[EventInfo] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Snapshot estructural (Plan 09) — dump de DDL para crear un blueprint baseline #
# --------------------------------------------------------------------------- #
class DumpStatement(BaseModel):
    """Una sentencia DDL del dump estructural, etiquetada por tipo de objeto."""

    object_type: str  # table | view | materialized_view | routine | trigger | sequence | type | extension | event
    name: str
    ddl: str
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "Nombres de TABLAS de las que depende este objeto (FK entre tablas, "
            "trigger→tabla, índice→tabla). Base para el orden topológico y la validación "
            "del split manual. Solo aristas baratas/fiables; vistas/rutinas no se parsean."
        ),
    )


class StructureDump(BaseModel):
    """
    Dump estructural COMPLETO de una BD (solo estructura, jamás filas).

    ``statements`` ya viene en ORDEN DE DEPENDENCIA listo para re-aplicar
    (extensions/types → tablas → secuencias → rutinas → vistas → triggers → events).
    ``has_non_portable`` indica que incluye objetos procedurales (rutinas/triggers/
    eventos) cuyo cuerpo NO es traducible cross-engine por sqlglot: el blueprint
    resultante queda atado a ``source_engine``.
    """

    database: str
    source_engine: str  # 'mysql' | 'mariadb' | 'postgresql'
    statements: list[DumpStatement]
    has_non_portable: bool = False
    table_stats: "list[TableStat] | None" = Field(
        default=None,
        description=(
            "Estimación de filas por tabla para informar la selección de datos-semilla. "
            "Solo se llena en el preview con ?include_data_stats=true; NUNCA en el dump base."
        ),
    )

    @property
    def object_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.statements:
            counts[s.object_type] = counts.get(s.object_type, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Datos-semilla (snapshot selectivo) — extracción OPT-IN de filas de catálogo   #
# --------------------------------------------------------------------------- #
class TableStat(BaseModel):
    """
    Estimación por tabla para informar la selección de datos-semilla (preview).

    Solo métricas: NUNCA valores de filas. ``estimated_rows`` es una ESTIMACIÓN del
    catálogo del motor (``information_schema.TABLES.TABLE_ROWS`` / ``pg_class.reltuples``),
    no un conteo exacto. ``has_primary_key=False`` => la tabla NO puede sembrarse (el
    upsert idempotente y el rollback por PK requieren clave primaria).
    """

    table: str
    estimated_rows: int
    # ``estimated_rows`` NO puede representar "no sé": los dos motores colapsan lo
    # desconocido a 0 (PostgreSQL cuando ``reltuples`` es -1 —tabla nunca analizada—, MySQL
    # cuando ``TABLE_ROWS`` es NULL), así que una tabla de 40 millones recién restaurada se
    # informa como vacía. Este flag distingue "0 filas" de "el catálogo no lo sabe" SIN
    # cambiar el tipo del campo: pasarlo a ``int | None`` rompería el contrato de
    # ``GET /servers/{id}/databases/{db}/snapshot`` (el schema del frontend lo declara no
    # nullable y está embebido, así que un solo null invalida toda la respuesta) y volvería
    # fail-open el guard de capacidad del export, que trataría lo desconocido como cero.
    estimated_rows_known: bool = True
    has_primary_key: bool


class SeedResult(BaseModel):
    """
    Datos-semilla de UNA tabla, ya renderizados como SQL idempotente + rollback por PK.

    SEGURIDAD: aunque ``up_sql``/``down_sql`` contienen los valores como literales SQL
    (destinados a persistirse en la migración de datos), este DTO NO se serializa en las
    respuestas de la API. ``included=False`` => la tabla se omitió; ``reason`` es un
    código estable (``no_primary_key`` | ``no_rows`` | ``oversize_rows`` | ``oversize_bytes``).
    """

    table: str
    included: bool
    reason: str | None = None
    row_count: int = 0
    primary_key: list[str] = Field(default_factory=list)
    up_sql: str | None = None
    down_sql: str | None = None


# ``StructureDump.table_stats`` referencia ``TableStat`` (definido más abajo): resolver
# la forward-ref ahora que el nombre existe en el namespace del módulo.
StructureDump.model_rebuild()


# --------------------------------------------------------------------------- #
# Conversión de charset/collation (feature collation-conversion)               #
# --------------------------------------------------------------------------- #
class ColumnCollationInfo(BaseModel):
    """
    Una columna COLACIONABLE (de texto) con su collation actual. Solo se llena en el modo
    ``columns`` (PostgreSQL), donde la COLUMNA es la única unidad de cambio posible.

    ``current_collation`` es el nombre de un objeto de ``pg_collation`` (p. ej. ``en_US``,
    ``C``, ``es-ES-x-icu``) — NO el locale del ``LC_COLLATE`` de la BD, que es otro espacio
    de valores. Es ``None`` cuando la columna NO tiene un ``COLLATE`` explícito: en ese caso
    hereda la collation por defecto de la base (``is_default_collation=True``).

    ``data_type`` es el tipo EXACTO tal como lo reporta el catálogo del motor
    (``format_type``: ``text``, ``character varying(255)``, ``citext``…). Se conserva
    verbatim porque el ``ALTER TABLE ... ALTER COLUMN ... TYPE`` que cambia la collation
    tiene que repetir el MISMO tipo con sus parámetros: cambiarlo por descuido convertiría
    una operación de collation en una migración de tipos.
    """

    name: str
    data_type: str
    current_collation: str | None = None
    is_default_collation: bool = False


class TableCollationInfo(BaseModel):
    """
    Charset/collation ACTUAL de una tabla + cuántas de sus columnas de texto están en
    una collation distinta de la pedida.

    ``mismatched_columns`` es la parte que no se puede deducir de ``collation``: una tabla
    cuyo default YA es el objetivo puede tener columnas con ``COLLATE`` explícito distinto
    (MySQL/MariaDB lo permiten por columna). Decidir "esta tabla no necesita conversión"
    mirando solo el default sería incorrecto, y ``ALTER TABLE ... CONVERT TO CHARACTER SET``
    REESCRIBE la tabla completa, así que saltearla cuando no hace falta es un ahorro real.

    ``columns`` SOLO se llena en el modo ``columns`` (PostgreSQL). En MySQL/MariaDB queda
    ``None`` (no ``[]``: la diferencia entre "este motor no reporta columnas acá" y "esta
    tabla no tiene columnas de texto" es informativa para el frontend). En PostgreSQL
    ``charset``/``collation`` van siempre en ``None``: no existe charset por tabla y la
    collation es un atributo de COLUMNA, no de tabla.
    """

    name: str
    charset: str | None = None
    collation: str | None = None
    mismatched_columns: int = 0
    needs_conversion: bool = True
    columns: list[ColumnCollationInfo] | None = None


class CollationGroup(BaseModel):
    """
    Cuántas tablas comparten un mismo par (charset, collation). Resumen del inventario.

    ``column_count`` solo se llena en el modo ``columns`` (PostgreSQL), donde el resumen
    agrupa por la collation de las COLUMNAS: ``table_count`` pasa a ser "en cuántas tablas
    aparece esta collation" y ``column_count`` "cuántas columnas la usan". En MySQL/MariaDB
    queda ``None`` y ``table_count`` conserva su significado original.
    """

    charset: str | None = None
    collation: str | None = None
    table_count: int = 0
    column_count: int | None = None


class CollationOptionInfo(BaseModel):
    """
    Una collation DISPONIBLE en el servidor destino (fila de ``pg_collation``).

    Es un catálogo POR SERVIDOR y se lee EN VIVO: qué collations existen depende de los
    locales instalados en el SO de esa máquina (y de si el binario trae ICU), así que no
    hay lista global posible. NO se confunde con ``charset_collation_options`` (el catálogo
    global del gateway), que describe el ``ENCODING``/``LC_COLLATE`` con el que se CREA una
    base: son dos espacios de valores distintos.

    ``deterministic=False`` (PostgreSQL 12+, solo ICU) tiene consecuencias reales: esas
    collations no admiten comparación por patrón (``LIKE``, expresiones regulares) ni los
    operadores ``*_pattern_ops`` de índice.
    """

    name: str
    provider: str | None = None  # 'c' (libc) | 'i' (ICU) | 'd' (default) | 'b' (builtin, PG17)
    deterministic: bool = True


class CollatableForeignKey(BaseModel):
    """
    Un par de columnas COLACIONABLES unidas por una FOREIGN KEY dentro de una misma BD.

    Existe para advertir sobre una conversión PARCIAL en PostgreSQL: si un lado queda con
    una collation explícita y el otro con otra, las comparaciones entre ambos fallan al
    ejecutarse. Es el análogo del aviso de FK del modo ``universal``, pero con la semántica
    de PostgreSQL (conflicto en tiempo de CONSULTA), no la de MySQL (rechazo del DDL).
    """

    constraint: str | None = None
    table: str
    column: str
    referenced_table: str
    referenced_column: str


class CollationObjectInfo(BaseModel):
    """
    Uno de los CINCO tipos de objeto cuyo collation queda CONGELADO en el momento de su
    creación (PROCEDURE, FUNCTION, TRIGGER, EVENT, VIEW).

    ``collation_connection`` es la collation de la SESIÓN que creó el objeto: es la que
    heredaron los parámetros ``VARCHAR``/``CHAR`` de una rutina, las variables ``DECLARE``
    de un trigger/evento y los literales de texto del cuerpo de una vista. NO es "la
    collation del objeto" en sentido estricto (un objeto no tiene una), pero es LA señal de
    qué quedó desactualizado tras cambiar el collation de la BD/tablas.

    ``database_collation`` es el default que tenía la BD en ese momento. ``None`` cuando el
    motor no lo expone para ese tipo de objeto (``information_schema.VIEWS`` no trae esa
    columna) o cuando la consulta no pudo leerlo.
    """

    object_type: str  # procedure | function | trigger | event | view
    name: str
    character_set_client: str | None = None
    collation_connection: str | None = None
    database_collation: str | None = None
    is_outdated: bool = Field(
        False,
        description=(
            "True si collation_connection difiere de la collation objetivo: el objeto "
            "necesita DROP+CREATE para dejar de arrastrar la collation vieja."
        ),
    )


class CollationInventory(BaseModel):
    """
    Inventario de conversión de charset/collation de una BD MySQL/MariaDB: default de la
    BD, tablas con su collation actual, resumen agrupado y los 5 tipos de objeto con
    collation congelado.

    ``notes`` lleva avisos de lectura parcial (p. ej. ``information_schema.EVENTS`` ausente
    en una instalación mínima): la lectura es best-effort por consulta, nunca aborta el
    inventario completo.

    En el modo ``columns`` (PostgreSQL) el mismo DTO se llena distinto y a propósito:
    ``objects`` es SIEMPRE ``[]`` (PostgreSQL resuelve la collation dinámicamente: no hay
    nada congelado que recrear), cada tabla trae sus ``columns``, y ``available_collations``
    trae el catálogo VIVO de ``pg_collation`` de ese servidor.
    """

    database: str
    engine: str
    db_charset: str | None = None
    db_collation: str | None = None
    target_charset: str | None = None
    target_collation: str | None = None
    tables: list[TableCollationInfo] = Field(default_factory=list)
    summary: list[CollationGroup] = Field(default_factory=list)
    objects: list[CollationObjectInfo] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    available_collations: list[CollationOptionInfo] = Field(
        default_factory=list,
        description=(
            "Collations que EXISTEN en este servidor (modo columns/PostgreSQL). Vacío en "
            "MySQL/MariaDB, donde el objetivo se valida contra el catálogo global."
        ),
    )


class RoutineGrantInfo(BaseModel):
    """
    Un privilegio a nivel de RUTINA (``EXECUTE`` / ``ALTER ROUTINE`` / ``GRANT OPTION``)
    sobre una PROCEDURE o FUNCTION concreta.

    Existe porque MySQL/MariaDB BORRAN los privilegios de rutina al hacer ``DROP
    PROCEDURE``/``DROP FUNCTION`` (viven en ``mysql.procs_priv``, con la rutina como parte
    de la clave): recrear la rutina sin reaplicarlos la deja sin permisos y rompe a quien
    la invocaba. Las TABLAS, TRIGGERS y EVENTS no necesitan esto (un trigger/evento no
    tiene privilegios propios; una tabla no se dropea en esta operación).
    """

    username: str
    host: str = "%"
    routine_type: str  # PROCEDURE | FUNCTION
    routine_name: str
    privileges: list[str] = Field(default_factory=list)
    grant_option: bool = False
