"""
Schemas Pydantic del recurso ``CollationConversionJob`` — conversión del charset/collation
de una base de datos.

DOS modos, según el motor del servidor (el usuario no lo elige):

- ``universal`` (MySQL/MariaDB): BD + tablas + los 5 tipos de objeto que CONGELAN la
  collation de la sesión que los creó.
- ``columns`` (PostgreSQL): SOLO ``ALTER TABLE ... ALTER COLUMN ... TYPE ... COLLATE ...``.
  Sin ``ALTER DATABASE`` (el ``ENCODING``/``LC_COLLATE`` es inmutable) y sin recreación de
  objetos (PostgreSQL resuelve la collation dinámicamente).

Los campos que solo aplican a un modo son OPCIONALES y quedan en ``null`` en el otro.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Valor del modo por defecto en las salidas. Se escribe literal (y no importando la
# constante del modelo) para no acoplar los schemas al módulo ORM: es el mismo criterio que
# el resto de ``app/schemas``, que solo importa de ``app.models.enums``.
COLLATION_MODE_UNIVERSAL = "universal"

# Los CINCO tipos de objeto cuyo collation queda congelado al crearlos y que por lo tanto
# necesitan DROP+CREATE verbatim. ``table`` no está: una tabla se convierte con
# ``ALTER TABLE ... CONVERT TO CHARACTER SET`` y se selecciona por ``tables``.
_FROZEN_OBJECT_TYPES = Literal["procedure", "function", "trigger", "event", "view"]


# --------------------------------------------------------------------------- #
# Entrada                                                                      #
# --------------------------------------------------------------------------- #
class CollationObjectRef(BaseModel):
    """Referencia a un objeto con collation congelada (tipo + nombre)."""

    object_type: _FROZEN_OBJECT_TYPES
    name: str = Field(..., min_length=1, max_length=512)


class CollationConversionCreate(BaseModel):
    """
    Crea un PLAN de conversión de charset/collation para una BD de un servidor.

    La BD se identifica por PATH (``/servers/{server_id}/databases/{database}``), así que
    acá solo viaja el objetivo. Funciona con BDs adoptadas y crudas (patrón "por
    identidad", igual que el resto del módulo de servidor-BDs).
    """

    target_charset: str | None = Field(
        None, min_length=1, max_length=64,
        description=(
            "Charset objetivo. OBLIGATORIO en MySQL/MariaDB (modo universal): el par "
            "(charset, collation) debe estar HABILITADO en el catálogo global. NO se debe "
            "enviar en PostgreSQL (modo columns): no existe charset por columna ni por tabla "
            "y el ENCODING de la base es inmutable tras el CREATE DATABASE (422 si se envía)."
        ),
    )
    target_collation: str = Field(
        ..., min_length=1, max_length=64,
        description=(
            "Collation objetivo. Es obligatoria: unificar la collation es el objetivo de la "
            "operación, y dejar que el motor elija la default haría el resultado ambiguo. "
            "MySQL/MariaDB: el PAR (charset, collation) debe estar habilitado en el catálogo "
            "global del gateway. PostgreSQL: debe ser el nombre EXACTO (case-sensitive) de "
            "una fila de pg_collation de ESE servidor — otro catálogo, se lee en vivo y se "
            "expone en `available_collations` del inventario."
        ),
    )


class CollationConversionPreviewIn(BaseModel):
    """
    Resuelve el plan final SIN ejecutar: qué se convertirá y qué se recreará, más el
    ``confirm_token`` a reenviar en ``/execute``.

    Ambas listas son EXPLÍCITAS a propósito: convertir tablas grandes y recrear rutinas son
    operaciones caras/sensibles, así que el gateway nunca las asume. Listas vacías son
    válidas (p. ej. recrear solo los objetos sin tocar tablas).
    """

    tables: list[str] = Field(
        default_factory=list,
        description="Tablas a convertir con ALTER TABLE ... CONVERT TO CHARACTER SET.",
    )
    objects: list[CollationObjectRef] = Field(
        default_factory=list,
        description="Objetos a recrear (DROP + CREATE verbatim con la collation objetivo).",
    )
    include_database_default: bool = Field(
        True,
        description=(
            "True = emitir ALTER DATABASE con el charset/collation objetivo (cambia solo el "
            "DEFAULT para objetos nuevos, no toca las tablas existentes)."
        ),
    )
    force: bool = Field(
        False,
        description="Continuar aunque el inventario haya cambiado desde que se creó el plan.",
    )


class CollationConversionExecuteIn(BaseModel):
    """Confirma y ENCOLA la ejecución asíncrona de la conversión."""

    confirm_target_name: str = Field(
        ..., min_length=1,
        description="Doble intención: debe coincidir con el nombre de la BD a convertir.",
    )
    confirm_token: str = Field(
        ..., min_length=1,
        description="Token del preview (recomputado server-side; solo se compara).",
    )
    force: bool = Field(
        False,
        description=(
            "Forzar si la BD gestionada está en cuarentena (status=error) o si el "
            "inventario cambió desde el preview."
        ),
    )


# --------------------------------------------------------------------------- #
# Salida                                                                        #
# --------------------------------------------------------------------------- #
class CollationColumnOut(BaseModel):
    """
    Una columna de texto con su collation actual. Solo se llena en el modo ``columns``
    (PostgreSQL), donde la COLUMNA es la única unidad de cambio posible.

    ``current_collation`` es ``null`` cuando la columna no tiene ``COLLATE`` explícito:
    hereda la collation por defecto de la base (``is_default_collation=true``). Esa columna
    igual necesita conversión, porque para el motor "la default de la base" es una collation
    DISTINTA de la concreta que se elija como objetivo.
    """

    name: str
    data_type: str = Field(
        ..., description="Tipo exacto con sus parámetros (text, character varying(255), …)."
    )
    current_collation: str | None = None
    is_default_collation: bool = False


class CollationTableOut(BaseModel):
    """Una tabla con su charset/collation actual y si necesita conversión."""

    name: str
    charset: str | None = None
    collation: str | None = None
    mismatched_columns: int = Field(
        0,
        description=(
            "Columnas de texto con una collation distinta de la objetivo. Una tabla cuyo "
            "default ya es el objetivo puede tener columnas con COLLATE explícito distinto."
        ),
    )
    needs_conversion: bool = True
    columns: list[CollationColumnOut] | None = Field(
        None,
        description=(
            "Columnas de texto de la tabla. Solo en el modo columns (PostgreSQL); null en "
            "MySQL/MariaDB, donde la unidad de conversión es la tabla entera."
        ),
    )


class CollationGroupOut(BaseModel):
    """Cuántas tablas comparten un mismo par (charset, collation)."""

    charset: str | None = None
    collation: str | None = None
    table_count: int = 0
    column_count: int | None = Field(
        None,
        description=(
            "Solo en el modo columns (PostgreSQL): cuántas COLUMNAS usan esta collation "
            "(ahí table_count pasa a ser 'en cuántas tablas aparece'). null en MySQL/MariaDB."
        ),
    )


class CollationOptionOut(BaseModel):
    """Una collation disponible en el servidor destino (fila de pg_collation)."""

    name: str
    provider: str | None = Field(
        None, description="c = libc, i = ICU, b = builtin (PostgreSQL 17+)."
    )
    deterministic: bool = Field(
        True,
        description=(
            "false = collation NO determinista (solo ICU): no admite LIKE ni expresiones "
            "regulares sobre esas columnas en PostgreSQL 12–17."
        ),
    )


class CollationObjectOut(BaseModel):
    """
    Un objeto con collation CONGELADA en el momento de su creación.

    ``collation_connection`` es la collation de la sesión que lo creó: la que heredaron los
    parámetros VARCHAR/CHAR de la rutina, las variables DECLARE del trigger/evento o los
    literales del cuerpo de la vista. Es la señal de qué quedó desactualizado.
    """

    object_type: str
    name: str
    character_set_client: str | None = None
    collation_connection: str | None = None
    database_collation: str | None = Field(
        None,
        description=(
            "Default de la BD al crear el objeto. Siempre null en las VISTAS: "
            "information_schema.VIEWS no expone esa columna en MySQL ni en MariaDB."
        ),
    )
    is_outdated: bool = False


class CollationInventoryOut(BaseModel):
    """Inventario de la BD: default, tablas, resumen agrupado y objetos congelados."""

    job_id: int
    database: str
    engine: str
    mode: str = Field(
        COLLATION_MODE_UNIVERSAL,
        description=(
            "universal (MySQL/MariaDB: BD + tablas + los 5 tipos de objeto congelados) | "
            "columns (PostgreSQL: solo ALTER COLUMN ... COLLATE). Lo determina el motor."
        ),
    )
    db_charset: str | None = Field(
        None, description="MySQL/MariaDB: charset default de la BD. PostgreSQL: ENCODING."
    )
    db_collation: str | None = Field(
        None,
        description=(
            "MySQL/MariaDB: collation default de la BD. PostgreSQL: LC_COLLATE de la base "
            "(INMUTABLE; se informa como contexto, esta operación no lo cambia)."
        ),
    )
    target_charset: str | None = None
    target_collation: str
    tables: list[CollationTableOut] = Field(default_factory=list)
    summary: list[CollationGroupOut] = Field(
        default_factory=list,
        description="Cuántos pares (charset, collation) distintos hay y cuántas tablas en cada uno.",
    )
    objects: list[CollationObjectOut] = Field(
        default_factory=list,
        description=(
            "Objetos con collation congelada. SIEMPRE vacío en el modo columns: PostgreSQL "
            "resuelve la collation dinámicamente y no hay nada que recrear."
        ),
    )
    available_collations: list[CollationOptionOut] = Field(
        default_factory=list,
        description=(
            "Modo columns: collations que EXISTEN en este servidor (pg_collation, leído en "
            "vivo) para armar el selector. Vacío en MySQL/MariaDB, donde el objetivo se "
            "valida contra el catálogo global del gateway."
        ),
    )
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CollationConversionStepOut(BaseModel):
    """Un paso del plan resuelto (sin ejecutar)."""

    object_type: str
    object_name: str
    action: str = Field(
        ...,
        description=(
            "Modo universal: alter_database | convert_table | recreate | skip. "
            "Modo columns: convert_columns | skip."
        ),
    )
    sql: str | None = None
    reason: str | None = Field(None, description="Motivo si action='skip'.")
    columns: list[str] | None = Field(
        None,
        description=(
            "Modo columns: qué columnas altera este paso. Van TODAS en la misma sentencia "
            "ALTER TABLE (una sola pasada y un solo lock por tabla)."
        ),
    )


class CollationConversionPreviewOut(BaseModel):
    """Plan final resuelto SIN ejecutar + confirm_token."""

    job_id: int
    database: str
    mode: str = COLLATION_MODE_UNIVERSAL
    target_charset: str | None = None
    target_collation: str
    include_database_default: bool = Field(
        True,
        description=(
            "Siempre false en el modo columns: el ENCODING/LC_COLLATE de una base "
            "PostgreSQL es inmutable tras el CREATE DATABASE."
        ),
    )
    steps: list[CollationConversionStepOut] = Field(default_factory=list)
    tables_to_convert: int = 0
    tables_skipped: int = 0
    columns_to_convert: int = Field(
        0, description="Modo columns: total de columnas que se van a alterar. 0 en universal."
    )
    objects_to_recreate: int = 0
    missing: list[CollationObjectRef] = Field(
        default_factory=list,
        description=(
            "Objetos seleccionados que ya no existen en la BD (desaparecieron entre el "
            "inventario y el preview). Se reportan y se excluyen; no abortan el plan."
        ),
    )
    missing_tables: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confirm_token: str


class CollationConversionSummaryOut(BaseModel):
    """Cabecera + estado de un job de conversión (polling)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    database_name: str
    database_id: int | None = None
    engine: str
    mode: str = Field(..., description="universal (MySQL/MariaDB) | columns (PostgreSQL)")
    target_charset: str | None = None
    target_collation: str
    previous_db_charset: str | None = None
    previous_db_collation: str | None = None
    status: str
    phase: str | None = None
    progress: dict | None = None
    error: str | None = None
    expired: bool = False
    created_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CollationConversionItemOut(BaseModel):
    """Un paso ejecutado del job con su resultado."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    seq: int
    object_type: str
    object_name: str
    previous_charset: str | None = None
    previous_collation: str | None = None
    status: str | None = None
    error: str | None = None
    grants_captured: int | None = None
    grants_reapplied: int | None = None
    grants_error: str | None = None
    columns_affected: int | None = Field(
        None, description="Modo columns: cuántas columnas cambió el paso. null en universal."
    )
    execution_ms: int | None = None
    executed_at: datetime | None = None
