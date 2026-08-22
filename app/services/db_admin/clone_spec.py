"""
Spec del clon: intención de copia, reglas de coherencia y guard de compatibilidad.

Módulo **PURO** (sin motor, sin ORM, sin HTTP): recibe DTOs y devuelve veredictos. Existe
para que la parte que decide *si una copia de datos es segura* se pueda testear sin Docker,
que es justo la parte donde vive la complejidad de esta feature.

Tres responsabilidades:

1. **La intención de copia** (``CopyIntent``) y su traducción a los enumerados del export
   (``EntityDdl``/``ScopeDdl``). El contrato público expone la INTENCIÓN y no el enumerado:
   de los cuatro valores de ``EntityDdl``, el clon solo puede cumplir dos
   (``DROP_CREATE`` destruiría permisos y triggers del destino, y ``CREATE_IF_NOT_EXISTS``
   está roto para todo lo que no sea una tabla — ``export_make_idempotent`` filtra por
   ``object_type`` y ``render_diff`` emite los tipos del diff, así que un
   ``ALTER TABLE … ADD CONSTRAINT`` o un ``CREATE INDEX`` saldrían crudos y morirían con
   1061/1826 dejando estructura parcial + cuarentena). Un enumerado donde la mitad de los
   valores responde 422 no es reúso: es una promesa que el servidor no cumple. Los
   enumerados siguen usándose ADENTRO para hablar el idioma de ``check_data_subset``.

2. **Las reglas de coherencia** del spec, como datos y no como condicionales dispersos.

3. **El guard de compatibilidad del destino** (``data_compat_issues``), cuya calibración es
   **por motor** y no por gusto — ver el docstring de esa función.

Todo lo que sale de acá lleva un ``code`` de vocabulario CERRADO (``ERROR_CODES``): el
frontend mapea códigos, no prosa. Sin eso vuelve a matchear mensajes con expresiones
regulares, que es lo que hace hoy.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.services.db_admin.dtos import ColumnInfo, SchemaSnapshot, TableSchema
from app.services.db_admin.export_spec import EntityDdl, ScopeDdl
from app.services.db_admin.privileges import same_family
from app.services.db_admin.schema_diff import canonical_type, is_narrowing

# --------------------------------------------------------------------------- #
# Vocabulario cerrado de códigos                                               #
# --------------------------------------------------------------------------- #
# Mismo patrón que ``export_spec.ERROR_CODES``. Viajan en ``public_context`` (que se ve
# SIEMPRE), no en ``context`` (que solo se expone en development).

CODE_CONFLICTING_OPTIONS = "clone.conflicting_options"
CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET = "clone.data_only_requires_existing_target"
CODE_EMPTY_PLAN = "clone.empty_plan"
CODE_ADOPT_REQUIRES_STRUCTURE = "clone.adopt_requires_structure"
CODE_DATA_WITHOUT_STRUCTURE = "clone.data_without_structure"
CODE_ON_EXISTING_REQUIRED = "clone.on_existing_required"
CODE_CHARSET_NOT_APPLICABLE = "clone.charset_not_applicable"
CODE_CHARSET_COMBINATION_DISABLED = "clone.charset_combination_disabled"
CODE_CHARSET_UNSUPPORTED_BY_ENGINE = "clone.charset_unsupported_by_engine"
CODE_OWNER_NOT_APPLICABLE = "clone.owner_not_applicable"
CODE_OWNER_INVALID = "clone.owner_invalid"
CODE_MISSING_DEPENDENCIES = "clone.missing_dependencies"
CODE_UNKNOWN_NAMES = "clone.unknown_names"
CODE_TARGET_SCHEMA_INCOMPATIBLE = "clone.target_schema_incompatible"
# El destino (o el origen) no se puede clonar por lo que ES, no por cómo se pidió: hoy, la
# propia base de metadatos del gateway y las bases de sistema del motor.
CODE_SCOPE_NOT_ALLOWED = "clone.scope_not_allowed"
CODE_SOURCE_FINGERPRINT_CHANGED = "clone.source_fingerprint_changed"
CODE_TARGET_FINGERPRINT_CHANGED = "clone.target_fingerprint_changed"
CODE_ALREADY_EXECUTED = "clone.already_executed"
CODE_PLAN_EXPIRED = "clone.plan_expired"
CODE_TARGET_QUARANTINED = "clone.target_quarantined"
CODE_TOKEN_MISMATCH = "clone.token_mismatch"
CODE_CONFIRM_NAME_MISMATCH = "clone.confirm_name_mismatch"
CODE_SOURCE_NOT_FOUND = "clone.source_not_found"
CODE_TARGET_NOT_FOUND = "clone.target_not_found"
CODE_TARGET_ALREADY_EXISTS = "clone.target_already_exists"
CODE_SAME_DATABASE = "clone.same_database"
CODE_ROW_COUNT_MISMATCH = "clone.row_count_mismatch"

# Avisos (no bloquean; viajan en ``warnings`` con su código).
WARN_TARGET_TRIGGERS_WILL_FIRE = "clone.target_triggers_will_fire"
WARN_UPSERT_WITHOUT_PRIMARY_KEY = "clone.upsert_without_primary_key"
WARN_CHARSET_DIFFERS_FROM_SOURCE = "clone.charset_differs_from_source"
WARN_OWNER_OBJECTS_NOT_REASSIGNED = "clone.owner_objects_not_reassigned"
WARN_TYPES_NOT_VERIFIED_CROSS_FAMILY = "clone.types_not_verified_cross_family"
WARN_SCHEMA_DIFFERENCE = "clone.target_schema_difference"
WARN_AUTOINCREMENT_KEY_ADDED = "clone.autoincrement_key_added"
WARN_EXTERNAL_FK_DEPENDENTS = "clone.external_fk_dependents"
WARN_IDEMPOTENCY_NOT_EXPRESSIBLE = "clone.idempotency_not_expressible"

ERROR_CODES: frozenset[str] = frozenset(
    {
        CODE_CONFLICTING_OPTIONS,
        CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET,
        CODE_EMPTY_PLAN,
        CODE_ADOPT_REQUIRES_STRUCTURE,
        CODE_DATA_WITHOUT_STRUCTURE,
        CODE_ON_EXISTING_REQUIRED,
        CODE_CHARSET_NOT_APPLICABLE,
        CODE_CHARSET_COMBINATION_DISABLED,
        CODE_CHARSET_UNSUPPORTED_BY_ENGINE,
        CODE_OWNER_NOT_APPLICABLE,
        CODE_OWNER_INVALID,
        CODE_MISSING_DEPENDENCIES,
        CODE_UNKNOWN_NAMES,
        CODE_TARGET_SCHEMA_INCOMPATIBLE,
        CODE_SCOPE_NOT_ALLOWED,
        CODE_SOURCE_FINGERPRINT_CHANGED,
        CODE_TARGET_FINGERPRINT_CHANGED,
        CODE_ALREADY_EXECUTED,
        CODE_PLAN_EXPIRED,
        CODE_TARGET_QUARANTINED,
        CODE_TOKEN_MISMATCH,
        CODE_CONFIRM_NAME_MISMATCH,
        CODE_SOURCE_NOT_FOUND,
        CODE_TARGET_NOT_FOUND,
        CODE_TARGET_ALREADY_EXISTS,
        CODE_SAME_DATABASE,
        CODE_ROW_COUNT_MISMATCH,
    }
)

WARNING_CODES: frozenset[str] = frozenset(
    {
        WARN_TARGET_TRIGGERS_WILL_FIRE,
        WARN_UPSERT_WITHOUT_PRIMARY_KEY,
        WARN_CHARSET_DIFFERS_FROM_SOURCE,
        WARN_OWNER_OBJECTS_NOT_REASSIGNED,
        WARN_TYPES_NOT_VERIFIED_CROSS_FAMILY,
        WARN_SCHEMA_DIFFERENCE,
        WARN_AUTOINCREMENT_KEY_ADDED,
        WARN_EXTERNAL_FK_DEPENDENTS,
        WARN_IDEMPOTENCY_NOT_EXPRESSIBLE,
    }
)


# --------------------------------------------------------------------------- #
# Enumerados del contrato                                                      #
# --------------------------------------------------------------------------- #
# ``enum.StrEnum`` y no el mixin clásico: con ``(str, Enum)``, ``str(Miembro)`` devuelve
# ``"CopyIntent.data_only"`` en vez de ``"data_only"``, y estos valores se interpolan en
# mensajes, se persisten y entran al hash del ``confirm_token``. Mismo criterio (y misma
# nota) que ``export_spec``.


class CopyIntent(enum.StrEnum):
    """
    Qué se copia. Es la INTENCIÓN del operador, no un eje técnico.

    - ``structure_only``: la estructura seleccionada, sin filas (comportamiento histórico
      con ``include_data=false``).
    - ``structure_and_data``: estructura + filas (``include_data=true``).
    - ``data_only``: **solo filas**, contra objetos que ya existen en el destino. No se
      emite una sola sentencia de DDL.
    """

    structure_only = "structure_only"
    structure_and_data = "structure_and_data"
    data_only = "data_only"


class DataOnExisting(enum.StrEnum):
    """
    Qué hacer cuando la tabla destino ya tiene filas.

    ``truncate`` **no existe todavía a propósito** (queda para la Fase 2): vaciar exige el
    cierre por FK del lado del DESTINO, confirmación propia de pérdida de filas y una
    semántica de cancelación que hoy no está, y su comportamiento en MySQL con
    ``FOREIGN_KEY_CHECKS=0`` es indocumentado. Shipearlo sin eso sería el mismo error que
    se evitó al no shipear ``DROP_CREATE``.
    """

    append = "append"
    upsert = "upsert"


# --------------------------------------------------------------------------- #
# Derivaciones (puras)                                                         #
# --------------------------------------------------------------------------- #
def entity_ddl_for(intent: CopyIntent) -> EntityDdl:
    """DDL por OBJETO que implica la intención."""
    return EntityDdl.NONE if intent is CopyIntent.data_only else EntityDdl.CREATE


def scope_ddl_for(target_mode: str, clean_mode: str) -> ScopeDdl:
    """
    DDL del CONTENEDOR, derivado de los ejes que ya existen.

    ``scope_ddl`` **no se expone en el contrato**: ``target_mode`` + ``clean_mode`` ya
    responden esa pregunta, y publicar el enumerado además daría dos fuentes de verdad para
    lo mismo. Se deriva acá solo para poder reusar ``export_spec.check_data_subset`` sin
    tocarla — incluida su excepción ya razonada de "ambos DDL en NONE ⇒ la restricción
    datos ⊆ estructura no aplica", que es exactamente el caso de solo datos.
    """
    if target_mode == "new":
        return ScopeDdl.CREATE
    if clean_mode == "drop_database":
        return ScopeDdl.DROP_CREATE
    return ScopeDdl.NONE


def creates_database(target_mode: str, clean_mode: str) -> bool:
    """¿Este job ejecuta un ``CREATE DATABASE``? (lo único que puede fijar charset/owner)."""
    return scope_ddl_for(target_mode, clean_mode) in (ScopeDdl.CREATE, ScopeDdl.DROP_CREATE)


def legacy_upsert(target_mode: str, clean_mode: str) -> bool:
    """
    Derivación HISTÓRICA de ``upsert`` para los caminos legacy (``include_data``).

    Se conserva bit a bit para no cambiarle el plan a nadie, pero **no** se usa como default
    de ``data_only``: con la estructura creándose (``EntityDdl.CREATE``) la fase de datos
    nunca llegaba a una tabla preexistente —el ``CREATE TABLE`` fallaba antes—, así que
    "upsert" nunca se ejercitó contra filas ajenas. Heredarlo como default del modo nuevo
    sería estrenar un default destructivo disfrazado de compatibilidad; por eso en
    ``data_only`` el valor es OBLIGATORIO y explícito.
    """
    return clean_mode == "none" and target_mode == "existing"


# --------------------------------------------------------------------------- #
# Reglas de coherencia del spec                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpecViolation:
    """Una regla incumplida. ``code`` es de ``ERROR_CODES``; ``detail`` es serializable."""

    code: str
    message: str
    detail: dict = field(default_factory=dict)


def validate_spec(
    *,
    intent: CopyIntent,
    data_mode: str,
    on_existing: DataOnExisting | None,
    target_mode: str,
    clean_mode: str,
    adopt_target: bool,
    charset_override: bool,
    owner_requested: bool,
    target_engine: str,
) -> list[SpecViolation]:
    """
    Verifica que la combinación pedida sea representable Y ejecutable.

    Devuelve la lista completa (no corta en la primera): un formulario que recibe todas las
    objeciones de una vez no obliga al operador a descubrirlas de a una.
    """
    out: list[SpecViolation] = []
    wants_data = data_mode != "none"

    if intent is CopyIntent.data_only:
        if target_mode != "existing":
            out.append(
                SpecViolation(
                    CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET,
                    "El modo 'data_only' requiere una BD destino existente "
                    "(target_mode='existing'): no emite DDL, así que los objetos tienen "
                    "que estar ya creados.",
                    {"target_mode": target_mode},
                )
            )
        if clean_mode != "none":
            out.append(
                SpecViolation(
                    CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET,
                    f"El modo 'data_only' requiere clean_mode='none' (recibido "
                    f"'{clean_mode}'): la limpieza borraría los objetos en los que hay que "
                    f"insertar las filas.",
                    {"clean_mode": clean_mode},
                )
            )
        if not wants_data:
            out.append(
                SpecViolation(
                    CODE_EMPTY_PLAN,
                    "El modo 'data_only' con data.mode='none' no haría nada: no crea "
                    "estructura ni copia filas.",
                    {},
                )
            )
        if on_existing is None:
            out.append(
                SpecViolation(
                    CODE_ON_EXISTING_REQUIRED,
                    "En 'data_only' hay que declarar data.on_existing explícitamente "
                    "('append' o 'upsert'): la tabla destino puede tener filas y el "
                    "servidor no puede adivinar si se agregan o se sobreescriben.",
                    {"allowed": [v.value for v in DataOnExisting]},
                )
            )
        if adopt_target:
            out.append(
                SpecViolation(
                    CODE_ADOPT_REQUIRES_STRUCTURE,
                    "adopt_target no es válido en 'data_only': adoptar el destino y "
                    "stampearle la versión del blueprint afirma que la estructura la puso "
                    "este job, y en este modo no se crea ninguna.",
                    {},
                )
            )
    else:
        if on_existing is not None:
            out.append(
                SpecViolation(
                    CODE_CONFLICTING_OPTIONS,
                    "data.on_existing solo aplica a 'data_only'; en los otros modos las "
                    "tablas las crea este mismo job y nacen vacías.",
                    {"copy": str(intent)},
                )
            )

    if intent is CopyIntent.structure_only and wants_data:
        out.append(
            SpecViolation(
                CODE_CONFLICTING_OPTIONS,
                "'structure_only' no copia filas: quitá la selección de datos o usá "
                "'structure_and_data'.",
                {"data_mode": data_mode},
            )
        )
    if intent is CopyIntent.structure_and_data and not wants_data:
        out.append(
            SpecViolation(
                CODE_CONFLICTING_OPTIONS,
                "'structure_and_data' con data.mode='none' es 'structure_only': elegí uno "
                "de los dos.",
                {"data_mode": data_mode},
            )
        )

    if charset_override and not creates_database(target_mode, clean_mode):
        out.append(
            SpecViolation(
                CODE_CHARSET_NOT_APPLICABLE,
                "El charset/collation solo se puede elegir cuando este job CREA la BD "
                "destino (target_mode='new' o clean_mode='drop_database'). Para cambiar el "
                "de una BD existente está el módulo de conversión de collation.",
                {"target_mode": target_mode, "clean_mode": clean_mode},
            )
        )
    if owner_requested:
        if not creates_database(target_mode, clean_mode):
            out.append(
                SpecViolation(
                    CODE_OWNER_NOT_APPLICABLE,
                    "El owner solo se puede fijar cuando este job CREA la BD destino.",
                    {"target_mode": target_mode, "clean_mode": clean_mode},
                )
            )
        elif target_engine != "postgresql":
            out.append(
                SpecViolation(
                    CODE_OWNER_NOT_APPLICABLE,
                    "El owner de una base de datos no existe en MySQL/MariaDB: el campo no "
                    "aplica a este destino.",
                    {"target_engine": target_engine},
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Guard de compatibilidad del destino                                          #
# --------------------------------------------------------------------------- #
# Motivos, vocabulario CERRADO. No se transcribe nunca el mensaje del motor (criterio R4).
REASON_TARGET_NOT_INSPECTED = "target_not_inspected"
REASON_TABLE_MISSING = "table_missing"
REASON_TABLE_IS_VIEW = "table_is_view"
REASON_COLUMN_MISSING = "column_missing_in_target"
REASON_TARGET_NOT_NULL_NO_DEFAULT = "target_column_not_null_without_default"
REASON_TARGET_GENERATED = "target_column_generated"
REASON_TARGET_IDENTITY_ALWAYS = "target_column_identity_always"
REASON_TYPE_NARROWING = "type_narrowing"
REASON_UNSIGNED_TO_SIGNED = "unsigned_to_signed"
REASON_COLLATION_ON_KEY = "collation_differs_on_key_column"
REASON_COLLATION_DIFFERS = "collation_differs"
REASON_TARGET_UNIQUE_EXTRA = "target_unique_not_in_source"
REASON_TARGET_CHECK_EXTRA = "target_check_not_in_source"
REASON_TARGET_FK_OUTSIDE_SELECTION = "target_fk_references_table_outside_selection"
REASON_TYPE_DIFFERS = "type_differs"
REASON_TYPES_NOT_VERIFIED = "types_not_verified_cross_family"
REASON_TYPE_NOT_LOADABLE = "type_not_loadable_cross_family"
REASON_SOURCE_NULLABLE_TARGET_NOT_NULL = "source_nullable_target_not_null"

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})

# Tipos del DESTINO que el pipeline de datos no puede alimentar desde la otra familia: los
# valores llegan adaptados por el driver y un array/geométrico/tsvector recibiría el texto
# JSON de una lista Python, no su literal nativo (ver el docstring de
# ``data_copy._adapt_value``). Bloquear es mejor que copiar y corromper en silencio.
_NOT_LOADABLE_CROSS_FAMILY = (
    "[]", "array", "point", "line", "lseg", "box", "path", "polygon", "circle",
    "tsvector", "tsquery", "bit varying", "varbit",
)


@dataclass(frozen=True)
class CompatIssue:
    """
    Una incompatibilidad entre el esquema del origen y el del destino, para una tabla que
    va a recibir filas.

    ``blocking`` lo decide ESTE módulo (la calibración es por motor); quien llama decide
    qué hacer con la lista — el ``preview`` la devuelve y el ``execute`` la rechaza. El
    guard **no lanza**: si lanzara desde el armado del plan, el operador recibiría
    "incompatible" sin poder ver el plan ni el resto de los avisos.
    """

    table: str
    reason: str
    blocking: bool
    column: str | None = None
    detail: dict = field(default_factory=dict)


def _fold(name: str, engine: str) -> str:
    """
    Clave de comparación de un nombre de columna, **según el motor DESTINO**.

    MySQL/MariaDB resuelven nombres de columna sin distinguir mayúsculas; PostgreSQL sí las
    distingue. Se plega por el destino porque es el que tiene que resolver el nombre que el
    ``INSERT``/``COPY`` escribe: un origen con ``ID`` contra un destino PG con ``id`` DEBE
    salir como columna ausente (allá falla de verdad), y contra un destino MySQL no.
    """
    return name.lower() if engine in _MYSQL_FAMILY else name


def _is_unsigned(type_str: str | None) -> bool:
    return "unsigned" in (type_str or "").lower()


def _not_loadable(type_str: str | None) -> bool:
    low = (type_str or "").lower()
    return any(tok in low for tok in _NOT_LOADABLE_CROSS_FAMILY)


def _key_columns(table: TableSchema) -> set[str]:
    """Columnas que participan de la PK o de un UNIQUE del destino (crudo, sin plegar)."""
    cols: set[str] = set(table.primary_key or [])
    for uc in table.unique_constraints:
        cols.update(uc.columns)
    for ix in table.indexes:
        if ix.unique:
            cols.update(ix.columns)
    return cols


def data_compat_issues(
    *,
    source: SchemaSnapshot,
    target: SchemaSnapshot | None,
    data_columns: Mapping[str, Sequence[str]],
    source_engine: str,
    target_engine: str,
) -> list[CompatIssue]:
    """
    ¿Se pueden insertar las filas del origen en los objetos que YA tiene el destino?

    ``data_columns`` es ``tabla -> columnas insertables`` (las que el ``INSERT``/``COPY`` va
    a nombrar). Solo se le pasan las tablas cuya estructura este job **no** crea.

    **La calibración de bloqueante vs aviso es POR MOTOR, y no es una preferencia:**

    - En **PostgreSQL** ``COPY`` valida los tipos y aborta la tabla completa de forma
      atómica, así que el motor es la red de seguridad y un aviso alcanza.
    - En **MySQL/MariaDB** el motor **no puede fallar**: el pipeline escribe con
      ``LOAD DATA LOCAL INFILE``, y el modificador ``LOCAL`` se comporta SIEMPRE como
      ``IGNORE`` y **anula el ``sql_mode`` restrictivo** (documentado en
      ``data_copy._copy_writer_mysql``). Un truncado de string, un ``DECIMAL`` redondeado,
      un ``unsigned`` fuera de rango, un valor de ``ENUM`` que el destino no tiene o una
      colisión de clave única se convierten en **warnings o filas descartadas, sin error** —
      y ``rows_copied`` cuenta filas escritas al FIFO, no filas insertadas, así que el job
      reporta éxito con datos perdidos. Ahí este guard es la ÚNICA defensa, y esos casos
      **bloquean**.

    Cross-family (MySQL↔PostgreSQL) los tipos **no se comparan**: ``diff_snapshots`` y
    ``canonical_type`` están definidos para el mismo dialecto y entre familias los nombres
    difieren por diseño, así que compararlos daría un falso bloqueo en cada columna. Se
    verifica presencia y nulabilidad, se avisa que los tipos no se verificaron, y se bloquea
    la lista corta de tipos del destino que el pipeline no puede alimentar.
    """
    if not data_columns:
        return []
    if target is None:
        # Fail-closed: nadie inspeccionó el destino, así que no se puede afirmar que la
        # copia sea segura. Es un error de programación del llamador, no del operador.
        return [
            CompatIssue(
                table=next(iter(data_columns)),
                reason=REASON_TARGET_NOT_INSPECTED,
                blocking=True,
                detail={"tables": sorted(data_columns)},
            )
        ]

    strict_types = same_family(source_engine, target_engine)
    mysql_target = target_engine in _MYSQL_FAMILY
    # En la familia MySQL el motor no puede rechazar la fila, así que lo que en PG es un
    # aviso acá tiene que bloquear.
    fidelity_blocks = mysql_target

    tgt_tables = {t.table: t for t in target.tables}
    tgt_views = {v.name for v in target.views}
    src_tables = {t.table: t for t in source.tables}
    selected = set(data_columns)

    out: list[CompatIssue] = []
    for table in sorted(data_columns):
        src_t = src_tables.get(table)
        tgt_t = tgt_tables.get(table)
        if tgt_t is None:
            out.append(
                CompatIssue(
                    table=table,
                    reason=REASON_TABLE_IS_VIEW if table in tgt_views else REASON_TABLE_MISSING,
                    blocking=True,
                )
            )
            continue
        if src_t is None:  # defensivo: la tabla salió del snapshot del origen
            continue

        src_cols = {c.name: c for c in src_t.columns}
        tgt_by_key: dict[str, ColumnInfo] = {
            _fold(c.name, target_engine): c for c in tgt_t.columns
        }
        insertable = {_fold(n, target_engine) for n in data_columns[table]}
        key_cols = {_fold(c, target_engine) for c in _key_columns(tgt_t)}

        # 1) Columnas que el INSERT nombra y el destino no tiene / no admite escribir.
        for col_name in data_columns[table]:
            key = _fold(col_name, target_engine)
            tgt_c = tgt_by_key.get(key)
            if tgt_c is None:
                out.append(
                    CompatIssue(table, REASON_COLUMN_MISSING, True, column=col_name)
                )
                continue
            if tgt_c.computed is not None:
                out.append(
                    CompatIssue(table, REASON_TARGET_GENERATED, True, column=col_name)
                )
                continue
            if tgt_c.identity is not None and tgt_c.identity.always:
                out.append(
                    CompatIssue(
                        table, REASON_TARGET_IDENTITY_ALWAYS, True, column=col_name
                    )
                )
                continue

            src_c = src_cols.get(col_name)
            if src_c is None:
                continue

            if not strict_types:
                if _not_loadable(tgt_c.type):
                    out.append(
                        CompatIssue(
                            table, REASON_TYPE_NOT_LOADABLE, True, column=col_name,
                            detail={"target_type": tgt_c.type},
                        )
                    )
                continue

            # --- Fidelidad de tipo (solo misma familia) ------------------------- #
            src_canon = canonical_type(src_c.type, source_engine)
            tgt_canon = canonical_type(tgt_c.type, target_engine)
            if src_canon != tgt_canon:
                # OJO con la dirección: ``is_narrowing(src, tgt)`` está definido como
                # "convertir la columna de ``tgt`` (actual) a ``src`` (deseado) pierde
                # datos?", que es la dirección del DIFF. En una COPIA el flujo es el
                # contrario (el origen provee, el destino recibe), así que los argumentos
                # van INVERTIDOS respecto del uso del diff. Leer ``DiffItem.risk`` en vez
                # de esto clasifica al revés el 100% de los casos de longitud y rango.
                if is_narrowing(tgt_canon, src_canon):
                    out.append(
                        CompatIssue(
                            table, REASON_TYPE_NARROWING, fidelity_blocks, column=col_name,
                            detail={"source_type": src_c.type, "target_type": tgt_c.type},
                        )
                    )
                else:
                    out.append(
                        CompatIssue(
                            table, REASON_TYPE_DIFFERS, False, column=col_name,
                            detail={"source_type": src_c.type, "target_type": tgt_c.type},
                        )
                    )
            if _is_unsigned(src_c.type) and not _is_unsigned(tgt_c.type):
                out.append(
                    CompatIssue(
                        table, REASON_UNSIGNED_TO_SIGNED, fidelity_blocks, column=col_name,
                        detail={"source_type": src_c.type, "target_type": tgt_c.type},
                    )
                )
            if (src_c.collation or None) != (tgt_c.collation or None) and (
                src_c.collation or tgt_c.collation
            ):
                on_key = key in key_cols
                out.append(
                    CompatIssue(
                        table,
                        REASON_COLLATION_ON_KEY if on_key else REASON_COLLATION_DIFFERS,
                        # Una colación distinta en una columna de PK/UNIQUE COLAPSA filas:
                        # 'Alice' y 'alice' son dos filas en un origen `_bin` y la MISMA
                        # clave en un destino `_ci`, así que una se pierde (por el upsert o
                        # por el IGNORE del LOAD DATA) sin ningún error.
                        blocking=on_key and fidelity_blocks,
                        column=col_name,
                        detail={
                            "source_collation": src_c.collation,
                            "target_collation": tgt_c.collation,
                        },
                    )
                )
            if src_c.nullable and not tgt_c.nullable and tgt_c.default is None:
                out.append(
                    CompatIssue(
                        table, REASON_SOURCE_NULLABLE_TARGET_NOT_NULL, False,
                        column=col_name,
                    )
                )

        if not strict_types:
            out.append(CompatIssue(table, REASON_TYPES_NOT_VERIFIED, False))

        # 2) Columnas del destino que el INSERT NO nombra y el motor va a exigir.
        for tgt_c in tgt_t.columns:
            key = _fold(tgt_c.name, target_engine)
            if key in insertable:
                continue
            if tgt_c.computed is not None or tgt_c.identity is not None:
                continue  # las llena el motor
            if not tgt_c.nullable and tgt_c.default is None and not tgt_c.autoincrement:
                out.append(
                    CompatIssue(
                        table, REASON_TARGET_NOT_NULL_NO_DEFAULT, True, column=tgt_c.name
                    )
                )

        # 3) Restricciones que existen SOLO en el destino: validan datos que el origen
        #    nunca tuvo que satisfacer. En la familia MySQL una colisión de clave única se
        #    descarta en silencio, así que bloquea.
        src_unique = {tuple(u.columns) for u in src_t.unique_constraints}
        src_unique |= {tuple(i.columns) for i in src_t.indexes if i.unique}
        src_unique.add(tuple(src_t.primary_key or []))
        for uc in tgt_t.unique_constraints:
            if tuple(uc.columns) not in src_unique:
                out.append(
                    CompatIssue(
                        table, REASON_TARGET_UNIQUE_EXTRA, fidelity_blocks,
                        detail={"columns": list(uc.columns), "name": uc.name},
                    )
                )
        for ix in tgt_t.indexes:
            if ix.unique and tuple(ix.columns) not in src_unique:
                out.append(
                    CompatIssue(
                        table, REASON_TARGET_UNIQUE_EXTRA, fidelity_blocks,
                        detail={"columns": list(ix.columns), "name": ix.name},
                    )
                )
        src_checks = {(c.sqltext or "").strip() for c in src_t.check_constraints}
        for ck in tgt_t.check_constraints:
            if (ck.sqltext or "").strip() not in src_checks:
                out.append(
                    CompatIssue(
                        table, REASON_TARGET_CHECK_EXTRA, fidelity_blocks,
                        detail={"name": ck.name},
                    )
                )

        # 4) FKs del destino hacia tablas que NO reciben datos en este job. La fase de datos
        #    corre con las FKs desactivadas y el motor NUNCA las revalida, así que esto no
        #    falla: deja filas huérfanas de forma permanente y silenciosa.
        for fk in tgt_t.foreign_keys:
            if fk.referred_table not in selected and fk.referred_table != table:
                out.append(
                    CompatIssue(
                        table, REASON_TARGET_FK_OUTSIDE_SELECTION, True,
                        detail={
                            "columns": list(fk.columns),
                            "referred_table": fk.referred_table,
                        },
                    )
                )
    return out


def blocking(issues: Sequence[CompatIssue]) -> list[CompatIssue]:
    return [i for i in issues if i.blocking]


__all__ = [
    "CODE_ADOPT_REQUIRES_STRUCTURE",
    "CODE_ALREADY_EXECUTED",
    "CODE_CHARSET_COMBINATION_DISABLED",
    "CODE_CHARSET_NOT_APPLICABLE",
    "CODE_CHARSET_UNSUPPORTED_BY_ENGINE",
    "CODE_CONFIRM_NAME_MISMATCH",
    "CODE_CONFLICTING_OPTIONS",
    "CODE_DATA_ONLY_REQUIRES_EXISTING_TARGET",
    "CODE_DATA_WITHOUT_STRUCTURE",
    "CODE_EMPTY_PLAN",
    "CODE_MISSING_DEPENDENCIES",
    "CODE_ON_EXISTING_REQUIRED",
    "CODE_OWNER_INVALID",
    "CODE_OWNER_NOT_APPLICABLE",
    "CODE_PLAN_EXPIRED",
    "CODE_ROW_COUNT_MISMATCH",
    "CODE_SAME_DATABASE",
    "CODE_SCOPE_NOT_ALLOWED",
    "CODE_SOURCE_FINGERPRINT_CHANGED",
    "CODE_SOURCE_NOT_FOUND",
    "CODE_TARGET_ALREADY_EXISTS",
    "CODE_TARGET_FINGERPRINT_CHANGED",
    "CODE_TARGET_NOT_FOUND",
    "CODE_TARGET_QUARANTINED",
    "CODE_TARGET_SCHEMA_INCOMPATIBLE",
    "CODE_TOKEN_MISMATCH",
    "CODE_UNKNOWN_NAMES",
    "ERROR_CODES",
    "WARNING_CODES",
    "WARN_AUTOINCREMENT_KEY_ADDED",
    "WARN_CHARSET_DIFFERS_FROM_SOURCE",
    "WARN_EXTERNAL_FK_DEPENDENTS",
    "WARN_IDEMPOTENCY_NOT_EXPRESSIBLE",
    "WARN_OWNER_OBJECTS_NOT_REASSIGNED",
    "WARN_SCHEMA_DIFFERENCE",
    "WARN_TARGET_TRIGGERS_WILL_FIRE",
    "WARN_TYPES_NOT_VERIFIED_CROSS_FAMILY",
    "WARN_UPSERT_WITHOUT_PRIMARY_KEY",
    "CompatIssue",
    "CopyIntent",
    "DataOnExisting",
    "SpecViolation",
    "blocking",
    "creates_database",
    "data_compat_issues",
    "entity_ddl_for",
    "legacy_upsert",
    "scope_ddl_for",
    "validate_spec",
]
