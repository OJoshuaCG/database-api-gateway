"""
Motor de diff estructural PURO entre dos snapshots de esquema (mismo motor o
MySQL↔MariaDB). Sin conexión a BD, sin ORM: 100% función pura sobre los DTOs de
``dtos.py`` (``SchemaSnapshot`` y amigos). Es la única capa 100% verificable en CI
sin Docker.

Dirección (decisión de producto #1): ``source`` = estado deseado/referencia,
``target`` = la BD que se modificaría. Todo el diff describe "qué correr sobre
TARGET para que quede como SOURCE":
  - objeto en SOURCE y no en TARGET  -> change_type='new'      (crear en target)
  - objeto en TARGET y no en SOURCE  -> change_type='dropped'  (borrar de target)
  - objeto en ambos, difiere         -> change_type='modified'

Reglas anti-falsos-positivos (ver plan, sección "trampas a normalizar"):
  - matching por DEFINICIÓN, no por nombre autogenerado (FKs/índices/constraints);
  - canonicalización de tipos vía sqlglot (``int(11)`` == ``int`` en MySQL 8);
  - normalización de defaults (casts de PG, ``CURRENT_TIMESTAMP`` vs
    ``current_timestamp()``);
  - collation/charset "igual al default de la tabla/BD" == no-diff;
  - estado (AUTO_INCREMENT, last_value, reltuples, versión de extensión) excluido;
  - orden de columnas no es diff;
  - ENUM: MySQL en el string de tipo; PG como ``EnumTypeInfo``;
  - cuerpos procedurales: comparación normalizada "cambió/no cambió", nunca diff
    semántico de lógica.

Clasificación de riesgo por ítem (fail-closed): lo que no se puede demostrar
aditivo/seguro se marca destructivo/needs_review. NUNCA por regex sobre SQL final.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from app.services.db_admin.sql_dialect import strip_self_schema_qualifier
from app.services.db_admin.dtos import (
    CheckConstraintInfo,
    ColumnInfo,
    EventInfo,
    ForeignKeyInfo,
    IndexInfo,
    RoutineInfo,
    SchemaSnapshot,
    SequenceInfo,
    TableSchema,
    TriggerInfo,
    UniqueConstraintInfo,
    ViewInfo,
)

# --------------------------------------------------------------------------- #
# Mapeos y constantes                                                          #
# --------------------------------------------------------------------------- #
_SQLGLOT_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres"}
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})

# Familia de enteros por tamaño (rango) — para detectar narrowing bigint->int->...
_INT_RANK = {
    "tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "integer": 4, "bigint": 5,
    "utinyint": 1, "usmallint": 2, "umediumint": 3, "uint": 4, "uinteger": 4, "ubigint": 5,
}
_INT_TYPES = frozenset(_INT_RANK)
_STRING_TYPES = frozenset({"varchar", "char", "nchar", "nvarchar", "text", "tinytext", "mediumtext", "longtext"})
_BLOB_TYPES = frozenset({"blob", "tinyblob", "mediumblob", "longblob", "bytea"})
_DECIMAL_TYPES = frozenset({"decimal", "numeric"})

# DEFINER (MySQL) — segunda pasada defensiva; la captura ya lo sanea.
_DEFINER_RE = re.compile(
    r"\s+DEFINER\s*=\s*(`[^`]*`@`[^`]*`|'[^']*'@'[^']*'|\"[^\"]*\"@\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)
_PG_CAST_RE = re.compile(r"::[\w \"\.\[\]]+$")

# Umbral de similitud (0..1) para marcar un posible rename de tabla (advisory).
_RENAME_SIMILARITY = 0.7

# --------------------------------------------------------------------------- #
# Fases de aplicación (pipeline de 9 fases del plan)                            #
# --------------------------------------------------------------------------- #
PHASE_CREATE_PREREQ = 1       # extension -> type/enum -> sequence
PHASE_CREATE_TABLE = 2        # tablas nuevas (sin FKs inline)
PHASE_ALTER_ADDITIVE = 3      # add columns/índices/unique/check + TODAS las FKs
PHASE_ALTER_MODIFY = 4        # modify columns / PK / secuencias
PHASE_CREATE_REPLACE = 5      # vistas -> matviews -> rutinas -> triggers -> events
PHASE_DROP_DEPENDENT = 6      # drop de dependientes desaparecidos (inverso a 5)
PHASE_ALTER_DESTRUCTIVE = 7   # drop columns/constraints/índices/FK
PHASE_DROP_TABLE = 8          # drop de tablas eliminadas (inverso a 2)
PHASE_DROP_PREREQ = 9         # drop de secuencias/tipos/extensiones sin uso


# --------------------------------------------------------------------------- #
# DTOs de salida del motor de diff                                             #
# --------------------------------------------------------------------------- #
class RiskFlags(BaseModel):
    """Clasificación de riesgo de un ítem/sentencia (calculada en el motor)."""

    destructive: bool = False       # pérdida de datos posible (DROP, narrowing, ...)
    lock_heavy: bool = False        # bloqueo/reescritura de tabla probable
    data_conversion: bool = False   # conversión de datos (USING, re-encoding)
    needs_review: bool = False      # puede fallar o alterar datos; requiere revisión
    requires_individual_review: bool = False  # cuerpo no revisable (procedural)
    cross_flavor_warning: bool = False        # ítem de una comparación MySQL↔MariaDB
    possible_rename_of: str | None = None      # heurística advisory (nunca autogenera RENAME)

    def merge(self, **kw: Any) -> "RiskFlags":
        data = self.model_dump()
        for k, v in kw.items():
            if k == "possible_rename_of":
                data[k] = v or data.get(k)
            else:
                data[k] = bool(data.get(k)) or bool(v)
        return RiskFlags(**data)


class DiffItem(BaseModel):
    """
    Un cambio estructural. ``source_payload``/``target_payload`` llevan el DTO
    concreto de cada lado (estado deseado / estado actual) para que la Fase 3
    genere el DDL con precisión (antes y después exactos).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    object_type: str  # table|column|index|foreign_key|unique_constraint|check_constraint|
    #                    primary_key|view|materialized_view|routine|trigger|sequence|
    #                    enum_type|extension|event
    object_name: str  # nombre (cualificado con la tabla padre donde aplica)
    change_type: str  # new | modified | dropped
    phase: int
    parent_table: str | None = None
    source_payload: Any = None  # DTO del lado SOURCE (deseado) o None si change=dropped
    target_payload: Any = None  # DTO del lado TARGET (actual) o None si change=new
    changed_attributes: list[str] = Field(default_factory=list)
    risk: RiskFlags = Field(default_factory=RiskFlags)
    notes: list[str] = Field(default_factory=list)
    # --- orden de ejecución y dependencias (calculados por order_diff_items) --- #
    # ``depends_on`` = claves (``op_key``) de OTROS ítems de ESTE diff que deben
    # ejecutarse ANTES que este. Es la base de dos cosas distintas:
    #   1. el orden topológico de ejecución (``order_diff_items``);
    #   2. la validación de CIERRE de una selección parcial (el admin no puede adoptar
    #      "la vista" sin "la tabla que la vista lee", ni el ADD de un índice
    #      redefinido sin su DROP previo).
    # Solo lista dependencias que hay que CREAR/EJECUTAR acá: lo que ya existe en el
    # target no aparece (no hace falta seleccionarlo).
    depends_on: list[str] = Field(default_factory=list)
    execution_step: int = 0  # paso fino de ejecución (ver _STEP); 0 hasta ordenar

    def op_key(self) -> str:
        """
        Identidad ATÓMICA del cambio. Un ``DiffItem`` puede renderizar VARIAS
        sentencias (un índice redefinido = ``DROP`` + ``CREATE``; un ``PRIMARY KEY``
        cambiado = ``DROP`` + ``ADD``) y todas comparten esta clave: son
        indivisibles, seleccionar una sin la otra rompe la ejecución.
        """
        return f"{self.object_type}|{self.object_name}|{self.change_type}"


class SchemaDiff(BaseModel):
    """Resultado del diff: cabecera + lista de ítems ya clasificados y ordenados."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_engine: str
    target_engine: str
    cross_flavor_warning: bool = False
    scope_note: str | None = None
    items: list[DiffItem] = Field(default_factory=list)

    @property
    def has_destructive(self) -> bool:
        return any(i.risk.destructive for i in self.items)


class RenderedStatement(BaseModel):
    """Una sentencia DDL generada (Fase 3), con sus flags de riesgo (Fase 2)."""

    sql: str
    object_type: str
    object_name: str
    change_type: str
    phase: int
    risk: RiskFlags
    down_sql: str | None = None
    down_confirmed: bool = False  # True si el reverso es claramente seguro (aditivo)
    # Grupo ATÓMICO: varias sentencias del MISMO ``DiffItem`` (DROP+CREATE de un índice
    # redefinido, DROP+ADD de un PK) comparten ``op_group``. Seleccionar una sin las otras
    # produce un error garantizado del motor (``Duplicate key name``, ``Multiple primary
    # key defined``), así que la selección se valida por grupo, no por sentencia.
    op_group: str = ""
    # ``op_group`` de otros cambios que deben ejecutarse ANTES que este (ver DiffItem).
    depends_on: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Normalizadores puros                                                         #
# --------------------------------------------------------------------------- #
def canonical_type(type_str: str | None, engine: str) -> str:
    """
    Forma canónica de un tipo para comparar sin falsos positivos. Usa sqlglot por
    dialecto; en MySQL 8 descarta el display width de enteros (``int(11)``->``int``).
    Falla con gracia al normalizado textual si sqlglot no parsea el tipo.
    """
    raw = (type_str or "").strip()
    if not raw:
        return ""
    dialect = _SQLGLOT_DIALECT.get(engine, engine)
    try:
        dt = exp.DataType.build(raw, dialect=dialect)
    except Exception:
        return _fallback_type(raw)
    base = dt.this.value.lower() if dt.this is not None else raw.lower()
    params = [e.sql(dialect=dialect) for e in dt.expressions]
    # Enteros: el display width es cosmético desde MySQL 8 -> descartar.
    if base in _INT_TYPES:
        params = []
    canon = base
    if params:
        canon += "(" + ",".join(params) + ")"
    return canon


def _fallback_type(raw: str) -> str:
    s = re.sub(r"\s+", " ", raw.strip().lower())
    # int(11) -> int (display width de enteros)
    s = re.sub(r"\b(tinyint|smallint|mediumint|int|integer|bigint)\s*\(\s*\d+\s*\)", r"\1", s)
    return s


def normalize_default(value: str | None, engine: str) -> str | None:
    """
    Normaliza un DEFAULT para comparar: quita el cast de PG (``'x'::varchar``->``'x'``),
    unifica ``CURRENT_TIMESTAMP``/``current_timestamp()``/``now()``, y baja a minúsculas
    solo los tokens no literales (preserva la caja de los literales entre comillas).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = _PG_CAST_RE.sub("", s).strip()
    if not s:
        return None
    # Literal entre comillas: preservar caja (sensible: 'Active' != 'active').
    if s[0] in ("'", '"'):
        return s
    low = s.lower()
    collapsed = low.replace(" ", "")
    if collapsed in ("current_timestamp", "current_timestamp()", "now()", "localtimestamp"):
        return "current_timestamp"
    if collapsed in ("true", "'1'", "1") and engine in _MYSQL_FAMILY:
        # no forzamos bool aquí; solo dejamos el token bajado
        return low
    return low


def normalize_body(sql: str | None) -> str:
    """Cuerpo procedural comparable: quita DEFINER, colapsa whitespace, sin ';' final."""
    if not sql:
        return ""
    s = _DEFINER_RE.sub("", sql)
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(";").strip()


def _norm_expr(value: str | None) -> str:
    """Normaliza una expresión SQL corta (default de índice, check, when) para comparar."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).rstrip(";").strip()


def effective_collation(col: ColumnInfo, table: TableSchema) -> str | None:
    """
    Collation EFECTIVA de la columna aplicando la regla de herencia: si coincide con el
    default de la tabla (o de la BD), se trata como "no explícita" (None) para no reportar
    ruido. La divergencia real (tabla/BD) se reporta a nivel ``storage_options``.
    """
    col_coll = (col.collation or "").strip()
    if not col_coll:
        return None
    table_default = (table.storage_options.get("collation") or "").strip()
    db_default = (table.storage_options.get("db_collation") or "").strip()
    if col_coll and (col_coll == table_default or col_coll == db_default):
        return None
    return col_coll or None


def effective_charset(col: ColumnInfo, table: TableSchema) -> str | None:
    col_cs = (col.charset or "").strip()
    if not col_cs:
        return None
    table_default = (table.storage_options.get("charset") or "").strip()
    db_default = (table.storage_options.get("db_charset") or "").strip()
    if col_cs and (col_cs == table_default or col_cs == db_default):
        return None
    return col_cs or None


# --------------------------------------------------------------------------- #
# Firmas de identidad (matching por definición, no por nombre)                 #
# --------------------------------------------------------------------------- #
def _fk_signature(fk: ForeignKeyInfo) -> tuple:
    return (
        tuple(fk.columns),
        fk.referred_table,
        tuple(fk.referred_columns),
    )


def _fk_options(fk: ForeignKeyInfo) -> tuple:
    return (
        (fk.on_delete or "no action").lower(),
        (fk.on_update or "no action").lower(),
        bool(fk.deferrable),
        (fk.initially or "").lower(),
    )


def _index_signature(ix: IndexInfo) -> tuple:
    return (
        tuple(ix.columns),
        bool(ix.unique),
        (ix.method or "").lower(),
        _norm_expr(ix.predicate),
        tuple(_norm_expr(e) for e in ix.expressions),
        tuple(ix.include_columns),
    )


def _unique_signature(uc: UniqueConstraintInfo) -> tuple:
    return tuple(uc.columns)


def _check_signature(ck: CheckConstraintInfo) -> str:
    return _norm_expr(ck.sqltext)


# --------------------------------------------------------------------------- #
# Detección de narrowing / conversiones (para clasificación destructiva)       #
# --------------------------------------------------------------------------- #
def _split_canon(canon: str) -> tuple[str, list[str]]:
    m = re.match(r"^([a-z0-9_]+)(?:\((.*)\))?$", canon)
    if not m:
        return canon, []
    base = m.group(1)
    params = [p.strip() for p in (m.group(2) or "").split(",")] if m.group(2) else []
    return base, params


def _enum_values(canon: str) -> list[str] | None:
    base, params = _split_canon(canon)
    if base != "enum":
        return None
    return params


def is_narrowing(src_canon: str, tgt_canon: str) -> bool:
    """
    ¿Convertir la columna de ``tgt`` (actual) a ``src`` (deseado) puede PERDER datos?
    Fail-closed: solo devuelve True cuando se puede demostrar el estrechamiento.
    """
    if src_canon == tgt_canon:
        return False
    sb, sp = _split_canon(src_canon)
    tb, tp = _split_canon(tgt_canon)

    # ENUM: quitar/renombrar valores es destructivo.
    se, te = _enum_values(src_canon), _enum_values(tgt_canon)
    if se is not None and te is not None:
        return not set(te).issubset(set(se))  # target tiene valores que source no

    # Enteros: bigint -> int -> smallint -> tinyint es narrowing.
    if sb in _INT_RANK and tb in _INT_RANK:
        return _INT_RANK[sb] < _INT_RANK[tb]

    # varchar/char: menos longitud es narrowing.
    if sb in _STRING_TYPES and tb in _STRING_TYPES:
        # text/blob -> varchar/char (con longitud) es narrowing
        if tb in ("text", "tinytext", "mediumtext", "longtext") and sb in ("varchar", "char", "nchar", "nvarchar"):
            return True
        s_len = int(sp[0]) if sp and sp[0].isdigit() else None
        t_len = int(tp[0]) if tp and tp[0].isdigit() else None
        if s_len is not None and t_len is not None:
            return s_len < t_len
        return False

    # decimal/numeric: menor precisión o escala es narrowing.
    if sb in _DECIMAL_TYPES and tb in _DECIMAL_TYPES:
        s_prec = int(sp[0]) if len(sp) >= 1 and sp[0].isdigit() else None
        t_prec = int(tp[0]) if len(tp) >= 1 and tp[0].isdigit() else None
        s_scale = int(sp[1]) if len(sp) >= 2 and sp[1].isdigit() else 0
        t_scale = int(tp[1]) if len(tp) >= 2 and tp[1].isdigit() else 0
        if s_prec is not None and t_prec is not None:
            return s_prec < t_prec or s_scale < t_scale
        return False

    # blob/text -> tipo más chico
    if tb in _BLOB_TYPES and sb not in _BLOB_TYPES:
        return True
    return False


def _base_family(canon: str) -> str:
    base, _ = _split_canon(canon)
    if base in _INT_RANK:
        return "int"
    if base in _STRING_TYPES:
        return "string"
    if base in _DECIMAL_TYPES or base in ("float", "double", "real"):
        return "numeric"
    if base in _BLOB_TYPES:
        return "binary"
    if "timestamp" in base or "datetime" in base or base == "date" or base == "time":
        return "temporal"
    return base


def _is_safe_widening(src_canon: str, tgt_canon: str) -> bool:
    """Widening claramente seguro (misma familia, mayor capacidad)."""
    sb, sp = _split_canon(src_canon)
    tb, tp = _split_canon(tgt_canon)
    if sb in _INT_RANK and tb in _INT_RANK:
        return _INT_RANK[sb] >= _INT_RANK[tb]
    if sb in ("varchar", "char", "nchar", "nvarchar") and tb in ("varchar", "char", "nchar", "nvarchar"):
        s_len = int(sp[0]) if sp and sp[0].isdigit() else None
        t_len = int(tp[0]) if tp and tp[0].isdigit() else None
        if s_len is not None and t_len is not None:
            return s_len >= t_len
    return False


# --------------------------------------------------------------------------- #
# Clasificación de un cambio de columna                                        #
# --------------------------------------------------------------------------- #
def _classify_column_modification(
    src: ColumnInfo, tgt: ColumnInfo, src_tbl: TableSchema, tgt_tbl: TableSchema, engine: str
) -> tuple[list[str], RiskFlags]:
    changed: list[str] = []
    risk = RiskFlags()

    src_type = canonical_type(src.type, engine)
    tgt_type = canonical_type(tgt.type, engine)
    if src_type != tgt_type:
        changed.append("type")
        risk = risk.merge(lock_heavy=True)
        if is_narrowing(src_type, tgt_type):
            risk = risk.merge(destructive=True, data_conversion=True)
        elif not _is_safe_widening(src_type, tgt_type):
            risk = risk.merge(needs_review=True, data_conversion=True)
        if engine == "postgresql" and _base_family(src_type) != _base_family(tgt_type):
            risk = risk.merge(needs_review=True, data_conversion=True)

    # nullability
    if bool(src.nullable) != bool(tgt.nullable):
        changed.append("nullable")
        if tgt.nullable and not src.nullable:
            # se agrega NOT NULL: puede fallar si hay NULLs -> no demostrablemente seguro
            risk = risk.merge(needs_review=True, lock_heavy=True)

    # default
    src_def = normalize_default(src.default, engine)
    tgt_def = normalize_default(tgt.default, engine)
    if src_def != tgt_def:
        changed.append("default")
        if src_def is None and tgt_def is not None:
            # DROP DEFAULT: excluido del modo automático (destructivo por plan)
            risk = risk.merge(destructive=True)

    # collation / charset (re-encoding: destructivo)
    if effective_collation(src, src_tbl) != effective_collation(tgt, tgt_tbl):
        changed.append("collation")
        risk = risk.merge(destructive=True, data_conversion=True)
    if effective_charset(src, src_tbl) != effective_charset(tgt, tgt_tbl):
        changed.append("charset")
        risk = risk.merge(destructive=True, data_conversion=True)

    # computed / identity
    if _computed_key(src) != _computed_key(tgt):
        changed.append("computed")
        risk = risk.merge(needs_review=True)
    if _identity_key(src) != _identity_key(tgt):
        changed.append("identity")
        risk = risk.merge(needs_review=True)

    # autoincrement
    if bool(src.autoincrement) != bool(tgt.autoincrement):
        changed.append("autoincrement")
        risk = risk.merge(needs_review=True)

    # on_update (MySQL)
    if _norm_expr(src.on_update) != _norm_expr(tgt.on_update):
        changed.append("on_update")

    # comment (cosmético estructural: sin flags de riesgo)
    if (src.comment or "") != (tgt.comment or ""):
        changed.append("comment")

    return changed, risk


def _computed_key(col: ColumnInfo) -> tuple | None:
    if col.computed is None:
        return None
    return (_norm_expr(col.computed.sqltext), bool(col.computed.persisted))


def _identity_key(col: ColumnInfo) -> tuple | None:
    if col.identity is None:
        return None
    return (bool(col.identity.always), col.identity.start, col.identity.increment)


def _column_is_additive_safe(col: ColumnInfo) -> bool:
    """Una columna NUEVA es aditiva-segura solo si es nullable o tiene default."""
    return bool(col.nullable) or col.default is not None or col.computed is not None


# --------------------------------------------------------------------------- #
# Heurística de rename (advisory, nunca autogenera RENAME)                     #
# --------------------------------------------------------------------------- #
def _table_col_sigset(tbl: TableSchema, engine: str) -> set[tuple[str, str]]:
    return {(c.name, canonical_type(c.type, engine)) for c in tbl.columns}


def _similarity(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    denom = max(len(a), len(b)) or 1
    return inter / denom


# --------------------------------------------------------------------------- #
# Diff principal                                                               #
# --------------------------------------------------------------------------- #
def _index_by_name(objs: list, attr: str = "name") -> dict[str, Any]:
    return {getattr(o, attr): o for o in objs}


def diff_snapshots(source: SchemaSnapshot, target: SchemaSnapshot) -> SchemaDiff:
    """
    Compara ``source`` (deseado) contra ``target`` (actual) y devuelve el diff con
    ítems clasificados por riesgo y ordenados por fase de aplicación.
    """
    cross_flavor = (
        source.source_engine != target.source_engine
        and source.source_engine in _MYSQL_FAMILY
        and target.source_engine in _MYSQL_FAMILY
    )
    engine = target.source_engine  # el DDL se genera para el motor del TARGET

    items: list[DiffItem] = []
    items += _diff_extensions(source, target)
    items += _diff_enum_types(source, target)
    items += _diff_sequences(source, target)
    items += _diff_tables(source, target, engine)
    items += _diff_views(source, target)
    items += _diff_routines(source, target)
    items += _diff_triggers(source, target)
    items += _diff_events(source, target)

    if cross_flavor:
        for it in items:
            it.risk = it.risk.merge(cross_flavor_warning=True)

    scope_note = None
    if source.source_engine == "postgresql" or target.source_engine == "postgresql":
        scope_note = (
            "PostgreSQL: el diff cubre solo el schema 'public'. Objetos en otros "
            "schemas quedan fuera de esta comparación."
        )

    items = order_diff_items(items, source, target)
    return SchemaDiff(
        source_engine=source.source_engine,
        target_engine=target.source_engine,
        cross_flavor_warning=cross_flavor,
        scope_note=scope_note,
        items=items,
    )


# ---- Tablas y sus sub-objetos --------------------------------------------- #
def _diff_tables(source: SchemaSnapshot, target: SchemaSnapshot, engine: str) -> list[DiffItem]:
    items: list[DiffItem] = []
    src_tbls = _index_by_name(source.tables, "table")
    tgt_tbls = _index_by_name(target.tables, "table")

    new_names = [n for n in src_tbls if n not in tgt_tbls]
    dropped_names = [n for n in tgt_tbls if n not in src_tbls]
    common = [n for n in src_tbls if n in tgt_tbls]

    # Heurística de rename de tablas (advisory).
    rename_new, rename_dropped = _detect_table_renames(
        {n: src_tbls[n] for n in new_names},
        {n: tgt_tbls[n] for n in dropped_names},
        engine,
    )

    for n in sorted(new_names):
        risk = RiskFlags()
        if n in rename_new:
            risk = risk.merge(destructive=True, possible_rename_of=rename_new[n])
        tbl = src_tbls[n]
        items.append(
            DiffItem(
                object_type="table", object_name=n, change_type="new",
                phase=PHASE_CREATE_TABLE, source_payload=tbl, risk=risk,
                notes=([f"posible rename de '{rename_new[n]}'"] if n in rename_new else []),
            )
        )
        # FKs de la tabla nueva SIEMPRE en fase separada (evita FKs circulares entre
        # tablas nuevas). Índices no-únicos también aparte (portabilidad PG).
        items += _new_table_child_items(tbl)
    for n in sorted(dropped_names):
        risk = RiskFlags(destructive=True)
        if n in rename_dropped:
            risk = risk.merge(possible_rename_of=rename_dropped[n])
        items.append(
            DiffItem(
                object_type="table", object_name=n, change_type="dropped",
                phase=PHASE_DROP_TABLE, target_payload=tgt_tbls[n], risk=risk,
                notes=([f"posible rename a '{rename_dropped[n]}'"] if n in rename_dropped else []),
            )
        )

    for n in sorted(common):
        items += _diff_one_table(src_tbls[n], tgt_tbls[n], engine)
    return items


def _index_backs_unique_constraint(
    ix: IndexInfo, uniques: list[UniqueConstraintInfo]
) -> bool:
    """
    ¿Este índice ÚNICO es el MISMO objeto físico que una unique constraint de la tabla?

    MySQL/MariaDB no distinguen ambos conceptos: una ``UNIQUE KEY`` es a la vez constraint
    e índice, y SQLAlchemy la refleja **DUPLICADA** — aparece en ``get_indexes()`` (con
    ``unique=True``) *y* en ``get_unique_constraints()`` (que además la marca con
    ``duplicates_index`` = el mismo nombre). Verificado en
    ``sqlalchemy/dialects/mysql/base.py`` (solo MySQL y Oracle marcan ``duplicates_index``;
    PostgreSQL no duplica).

    Sin descartar la copia redundante, el diff emite **DOS** sentencias que crean la MISMA
    clave — ``ALTER TABLE … ADD CONSTRAINT x UNIQUE (…)`` y
    ``CREATE UNIQUE INDEX x ON …`` — y la segunda falla con
    ``(1061, "Duplicate key name 'x'")``, abortando la migración a mitad.

    El match es por **NOMBRE** (no por el simple hecho de que el índice sea único): en
    PostgreSQL un índice único puede ser un objeto autónomo SIN constraint detrás — el caso
    típico es un índice único **parcial** (``CREATE UNIQUE INDEX … WHERE deleted_at IS
    NULL``) — y descartarlo lo perdería del diff en silencio. Solo se compara por conjunto
    de columnas cuando a alguno de los dos lados le falta el nombre.
    """
    if not ix.unique:
        return False
    if ix.predicate or ix.expressions:
        # Un índice parcial/funcional NUNCA puede ser el respaldo de una unique constraint.
        return False
    for uc in uniques:
        if uc.name and ix.name:
            if uc.name == ix.name:
                return True
        elif list(uc.columns) == list(ix.columns):
            return True
    return False


def _new_table_child_items(tbl: TableSchema) -> list[DiffItem]:
    """FKs (todas) e índices no-únicos de una tabla NUEVA, como ítems de fase 3."""
    items: list[DiffItem] = []
    for fk in tbl.foreign_keys:
        items.append(DiffItem(
            object_type="foreign_key",
            object_name=f"{tbl.table}.{fk.name}" if fk.name else f"{tbl.table}.<fk>",
            change_type="new", phase=PHASE_ALTER_ADDITIVE, parent_table=tbl.table,
            source_payload=fk, risk=RiskFlags(lock_heavy=True),
        ))
    for ix in tbl.indexes:
        if _index_backs_unique_constraint(ix, tbl.unique_constraints):
            continue  # ya va inline (CONSTRAINT … UNIQUE) en el CREATE TABLE
        items.append(DiffItem(
            object_type="index",
            object_name=f"{tbl.table}.{ix.name}" if ix.name else f"{tbl.table}.<index>",
            change_type="new", phase=PHASE_ALTER_ADDITIVE, parent_table=tbl.table,
            source_payload=ix, risk=RiskFlags(lock_heavy=True),
        ))
    return items


def _detect_table_renames(
    new_tbls: dict[str, TableSchema], dropped_tbls: dict[str, TableSchema], engine: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Empareja 1:1 tablas nuevas y eliminadas por similitud de firma de columnas."""
    rename_new: dict[str, str] = {}
    rename_dropped: dict[str, str] = {}
    used_dropped: set[str] = set()
    new_sigs = {n: _table_col_sigset(t, engine) for n, t in new_tbls.items()}
    drop_sigs = {n: _table_col_sigset(t, engine) for n, t in dropped_tbls.items()}
    for nname in sorted(new_tbls):
        best, best_score = None, 0.0
        for dname in sorted(dropped_tbls):
            if dname in used_dropped:
                continue
            score = _similarity(new_sigs[nname], drop_sigs[dname])
            if score > best_score:
                best, best_score = dname, score
        if best is not None and best_score >= _RENAME_SIMILARITY:
            rename_new[nname] = best
            rename_dropped[best] = nname
            used_dropped.add(best)
    return rename_new, rename_dropped


def _diff_one_table(src: TableSchema, tgt: TableSchema, engine: str) -> list[DiffItem]:
    items: list[DiffItem] = []
    table = src.table

    # --- columnas (match por nombre; orden no es diff) ---------------------- #
    src_cols = _index_by_name(src.columns)
    tgt_cols = _index_by_name(tgt.columns)
    new_cols = [n for n in src_cols if n not in tgt_cols]
    dropped_cols = [n for n in tgt_cols if n not in src_cols]
    common_cols = [n for n in src_cols if n in tgt_cols]

    col_rename_new, col_rename_dropped = _detect_column_renames(
        {n: src_cols[n] for n in new_cols}, {n: tgt_cols[n] for n in dropped_cols}, engine
    )

    for n in sorted(new_cols):
        col = src_cols[n]
        risk = RiskFlags()
        if not _column_is_additive_safe(col):
            risk = risk.merge(needs_review=True, lock_heavy=True)
        if n in col_rename_new:
            risk = risk.merge(destructive=True, possible_rename_of=col_rename_new[n])
        items.append(
            DiffItem(
                object_type="column", object_name=f"{table}.{n}", change_type="new",
                phase=PHASE_ALTER_ADDITIVE, parent_table=table, source_payload=col, risk=risk,
                notes=([f"posible rename de '{col_rename_new[n]}'"] if n in col_rename_new else []),
            )
        )
    for n in sorted(dropped_cols):
        risk = RiskFlags(destructive=True)
        if n in col_rename_dropped:
            risk = risk.merge(possible_rename_of=col_rename_dropped[n])
        items.append(
            DiffItem(
                object_type="column", object_name=f"{table}.{n}", change_type="dropped",
                phase=PHASE_ALTER_DESTRUCTIVE, parent_table=table, target_payload=tgt_cols[n],
                risk=risk,
                notes=([f"posible rename a '{col_rename_dropped[n]}'"] if n in col_rename_dropped else []),
            )
        )
    for n in sorted(common_cols):
        changed, risk = _classify_column_modification(
            src_cols[n], tgt_cols[n], src, tgt, engine
        )
        if changed:
            phase = PHASE_ALTER_MODIFY
            items.append(
                DiffItem(
                    object_type="column", object_name=f"{table}.{n}", change_type="modified",
                    phase=phase, parent_table=table,
                    source_payload=src_cols[n], target_payload=tgt_cols[n],
                    changed_attributes=changed, risk=risk,
                )
            )

    # --- primary key -------------------------------------------------------- #
    if list(src.primary_key) != list(tgt.primary_key):
        risk = RiskFlags(lock_heavy=True)
        if tgt.primary_key and src.primary_key:
            change_type = "modified"  # PK existía en ambos lados, cambió
            risk = risk.merge(destructive=True)
        elif tgt.primary_key:
            change_type = "dropped"  # había PK, ahora no -> se elimina
            risk = risk.merge(destructive=True)
        else:
            change_type = "new"  # no había PK -> se agrega
            risk = risk.merge(needs_review=True)  # ADD PK valida datos existentes
        items.append(
            DiffItem(
                object_type="primary_key", object_name=f"{table}.PRIMARY", change_type=change_type,
                phase=PHASE_ALTER_MODIFY, parent_table=table,
                # Ambos DTOs siempre poblados (aun en new/dropped): el renderer necesita ver
                # las dos tablas completas para decidir DROP/ADD (a diferencia de otras entidades).
                source_payload=src, target_payload=tgt, risk=risk,
                changed_attributes=["columns"],
            )
        )

    # --- foreign keys (match por firma de definición) ----------------------- #
    items += _diff_collection(
        table, "foreign_key", src.foreign_keys, tgt.foreign_keys,
        sig=_fk_signature,
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        opts=_fk_options, modify_phase=PHASE_ALTER_ADDITIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
        modify_risk=RiskFlags(destructive=True, lock_heavy=True),  # drop+add
        pair_by_name=True,  # mismo nombre, firma distinta (p.ej. cambia la tabla referida)
    )

    # --- unique constraints ------------------------------------------------- #
    items += _diff_collection(
        table, "unique_constraint", src.unique_constraints, tgt.unique_constraints,
        sig=_unique_signature,
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
        modify_phase=PHASE_ALTER_MODIFY,
        modify_risk=RiskFlags(destructive=True, lock_heavy=True),  # drop+add
        pair_by_name=True,
    )

    # --- check constraints -------------------------------------------------- #
    items += _diff_collection(
        table, "check_constraint", src.check_constraints, tgt.check_constraints,
        sig=lambda c: _check_signature(c),
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
        modify_phase=PHASE_ALTER_MODIFY,
        modify_risk=RiskFlags(destructive=True, lock_heavy=True),  # drop+add
        pair_by_name=True,
    )

    # --- índices (no PK/unique-constraint; match por firma) ----------------- #
    # Se descartan, en AMBOS lados, los índices únicos que son el mismo objeto físico que
    # una unique constraint ya diffeada arriba (MySQL/MariaDB los reflejan duplicados; ver
    # ``_index_backs_unique_constraint``). Filtrar el lado TARGET también es necesario: si
    # no, una unique key que sobra en el destino se reportaría dos veces (un
    # ``unique_constraint`` dropped + un ``index`` dropped) y el segundo DROP fallaría
    # porque el primero ya eliminó la clave.
    src_indexes = [
        ix for ix in src.indexes
        if not _index_backs_unique_constraint(ix, src.unique_constraints)
    ]
    tgt_indexes = [
        ix for ix in tgt.indexes
        if not _index_backs_unique_constraint(ix, tgt.unique_constraints)
    ]
    items += _diff_collection(
        table, "index", src_indexes, tgt_indexes,
        sig=_index_signature,
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
        modify_phase=PHASE_ALTER_MODIFY,
        modify_risk=RiskFlags(destructive=True, lock_heavy=True),  # drop+add
        pair_by_name=True,
    )
    return items


def _detect_column_renames(
    new_cols: dict[str, ColumnInfo], dropped_cols: dict[str, ColumnInfo], engine: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Rename de columna advisory: 1 nueva + 1 eliminada del MISMO tipo canónico."""
    if len(new_cols) != 1 or len(dropped_cols) != 1:
        return {}, {}
    (nname, ncol), = new_cols.items()
    (dname, dcol), = dropped_cols.items()
    if canonical_type(ncol.type, engine) == canonical_type(dcol.type, engine):
        return {nname: dname}, {dname: nname}
    return {}, {}


def _diff_collection(
    table: str,
    object_type: str,
    src_objs: list,
    tgt_objs: list,
    *,
    sig,
    new_phase: int,
    drop_phase: int,
    new_risk: RiskFlags,
    drop_risk: RiskFlags,
    opts=None,
    modify_phase: int | None = None,
    modify_risk: RiskFlags | None = None,
    pair_by_name: bool = False,
) -> list[DiffItem]:
    """
    Diffea una colección de sub-objetos de tabla (FK/índice/unique/check) por FIRMA
    de definición. El nombre autogenerado NO es criterio de identidad (se anota como
    secundario). ``opts`` (si se da) compara atributos extra de un match (p.ej. las
    opciones referenciales de una FK) -> genera un ítem 'modified'.

    ``pair_by_name`` (si True): entre los objetos que NO matchearon por firma (o sea,
    los candidatos a 'new'/'dropped'), empareja los que comparten el mismo ``name`` en
    ambos lados como UN SOLO ítem 'modified' (redefinición: mismo nombre, definición
    distinta) en vez de un par suelto new+dropped. Fail-closed: si el nombre no es
    único de cada lado, no empareja (queda como new+dropped, comportamiento actual).
    """
    items: list[DiffItem] = []
    src_map: dict[tuple, Any] = {sig(o): o for o in src_objs}
    tgt_map: dict[tuple, Any] = {sig(o): o for o in tgt_objs}

    new_pending: dict[tuple, Any] = {}
    dropped_pending: dict[tuple, Any] = {}

    for s, sobj in src_map.items():
        if s not in tgt_map:
            new_pending[s] = sobj
        elif opts is not None and modify_phase is not None:
            tobj = tgt_map[s]
            if opts(sobj) != opts(tobj):
                name = getattr(sobj, "name", None)
                items.append(
                    DiffItem(
                        object_type=object_type,
                        object_name=f"{table}.{name}" if name else f"{table}.<{object_type}>",
                        change_type="modified", phase=modify_phase, parent_table=table,
                        source_payload=sobj, target_payload=tobj,
                        risk=(modify_risk or RiskFlags()).model_copy(deep=True),
                        changed_attributes=["options"],
                    )
                )
            else:
                _maybe_name_note(items, sobj, tobj)
        else:
            _maybe_name_note(items, src_map[s], tgt_map[s])

    for t, tobj in tgt_map.items():
        if t not in src_map:
            dropped_pending[t] = tobj

    paired_new_keys: set = set()
    paired_dropped_keys: set = set()
    if pair_by_name:
        new_by_name: dict[str, list] = {}
        for key, obj in new_pending.items():
            name = getattr(obj, "name", None)
            if name:
                new_by_name.setdefault(name, []).append(key)
        dropped_by_name: dict[str, list] = {}
        for key, obj in dropped_pending.items():
            name = getattr(obj, "name", None)
            if name:
                dropped_by_name.setdefault(name, []).append(key)
        for name in sorted(set(new_by_name) & set(dropped_by_name)):
            nkeys, dkeys = new_by_name[name], dropped_by_name[name]
            if len(nkeys) != 1 or len(dkeys) != 1:
                continue  # nombre ambiguo de algún lado: fail-closed, se deja new+dropped
            nkey, dkey = nkeys[0], dkeys[0]
            sobj, tobj = new_pending[nkey], dropped_pending[dkey]
            phase = modify_phase if modify_phase is not None else new_phase
            risk = (modify_risk or RiskFlags(destructive=True, lock_heavy=True)).model_copy(deep=True)
            items.append(
                DiffItem(
                    object_type=object_type,
                    object_name=f"{table}.{name}",
                    change_type="modified", phase=phase, parent_table=table,
                    source_payload=sobj, target_payload=tobj, risk=risk,
                    changed_attributes=["definition"],
                    notes=["redefinición detectada por nombre igual, definición distinta"],
                )
            )
            paired_new_keys.add(nkey)
            paired_dropped_keys.add(dkey)

    for key, sobj in new_pending.items():
        if key in paired_new_keys:
            continue
        name = getattr(sobj, "name", None)
        items.append(
            DiffItem(
                object_type=object_type,
                object_name=f"{table}.{name}" if name else f"{table}.<{object_type}>",
                change_type="new", phase=new_phase, parent_table=table,
                source_payload=sobj, risk=new_risk.model_copy(deep=True),
            )
        )
    for key, tobj in dropped_pending.items():
        if key in paired_dropped_keys:
            continue
        name = getattr(tobj, "name", None)
        items.append(
            DiffItem(
                object_type=object_type,
                object_name=f"{table}.{name}" if name else f"{table}.<{object_type}>",
                change_type="dropped", phase=drop_phase, parent_table=table,
                target_payload=tobj, risk=drop_risk.model_copy(deep=True),
            )
        )
    return items


def _maybe_name_note(items: list[DiffItem], sobj, tobj) -> None:
    """Un match por firma con nombres distintos: no es un cambio estructural (no-op)."""
    # Intencionalmente no emite ítem: el nombre autogenerado es secundario (evita ruido).
    return None


# ---- Vistas / matviews ----------------------------------------------------- #
def _diff_views(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    items: list[DiffItem] = []
    # Cada lado se compara con el calificador de su PROPIA base quitado: en MySQL/MariaDB
    # el cuerpo lleva el nombre de la BD adentro y, sin esto, una BD contra su clon
    # reportaría todas las vistas como modificadas (ver strip_self_schema_qualifier).
    src_ctx = (source.database, source.source_engine)
    tgt_ctx = (target.database, target.source_engine)
    for is_mat, otype in ((False, "view"), (True, "materialized_view")):
        src_map = {v.name: v for v in source.views if v.is_materialized == is_mat}
        tgt_map = {v.name: v for v in target.views if v.is_materialized == is_mat}
        for n in sorted(src_map):
            v = src_map[n]
            if n not in tgt_map:
                risk = RiskFlags(requires_individual_review=True)
                items.append(DiffItem(
                    object_type=otype, object_name=n, change_type="new",
                    phase=PHASE_CREATE_REPLACE, source_payload=v, risk=risk,
                ))
            elif _view_key(v, *src_ctx) != _view_key(tgt_map[n], *tgt_ctx):
                risk = RiskFlags(requires_individual_review=True)
                # Cambiar columnas de una vista/matview obliga DROP+CREATE.
                if list(v.columns) != list(tgt_map[n].columns):
                    risk = risk.merge(needs_review=True)
                    if is_mat:
                        risk = risk.merge(destructive=True)  # matview: recrear pierde datos derivados
                items.append(DiffItem(
                    object_type=otype, object_name=n, change_type="modified",
                    phase=PHASE_CREATE_REPLACE, source_payload=v, target_payload=tgt_map[n],
                    risk=risk, changed_attributes=["definition"],
                ))
        for n in sorted(tgt_map):
            if n not in src_map:
                items.append(DiffItem(
                    object_type=otype, object_name=n, change_type="dropped",
                    phase=PHASE_DROP_DEPENDENT, target_payload=tgt_map[n],
                    risk=RiskFlags(destructive=True),
                ))
    return items


def _view_key(v: ViewInfo, database: str = "", engine: str = "") -> tuple:
    body = strip_self_schema_qualifier(v.definition, database, engine)
    return (normalize_body(body), v.check_option or "", bool(v.security_definer),
            tuple(v.columns))


# ---- Rutinas --------------------------------------------------------------- #
def _diff_routines(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    src_ctx = (source.database, source.source_engine)
    tgt_ctx = (target.database, target.source_engine)
    src_map = {(r.kind.upper(), r.name): r for r in source.routines}
    tgt_map = {(r.kind.upper(), r.name): r for r in target.routines}
    items: list[DiffItem] = []
    for key in sorted(src_map):
        r = src_map[key]
        if key not in tgt_map:
            items.append(DiffItem(
                object_type="routine", object_name=f"{r.kind}:{r.name}", change_type="new",
                phase=PHASE_CREATE_REPLACE, source_payload=r,
                risk=RiskFlags(requires_individual_review=True),
            ))
        elif _routine_key(r, *src_ctx) != _routine_key(tgt_map[key], *tgt_ctx):
            items.append(DiffItem(
                object_type="routine", object_name=f"{r.kind}:{r.name}", change_type="modified",
                phase=PHASE_CREATE_REPLACE, source_payload=r, target_payload=tgt_map[key],
                risk=RiskFlags(requires_individual_review=True), changed_attributes=["body"],
            ))
    for key in sorted(tgt_map):
        if key not in src_map:
            r = tgt_map[key]
            items.append(DiffItem(
                object_type="routine", object_name=f"{r.kind}:{r.name}", change_type="dropped",
                phase=PHASE_DROP_DEPENDENT, target_payload=r,
                risk=RiskFlags(destructive=True, requires_individual_review=True),
            ))
    return items


def _routine_key(r: RoutineInfo, database: str = "", engine: str = "") -> tuple:
    params = tuple((p.mode or "", p.type) for p in r.parameters)
    body = strip_self_schema_qualifier(r.body, database, engine)
    return (normalize_body(body), r.return_type or "", (r.language or "").lower(),
            (r.volatility or "").lower(), bool(r.security_definer), params)


# ---- Triggers -------------------------------------------------------------- #
def _diff_triggers(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    src_ctx = (source.database, source.source_engine)
    tgt_ctx = (target.database, target.source_engine)
    src_map = {(t.table, t.name): t for t in source.triggers}
    tgt_map = {(t.table, t.name): t for t in target.triggers}
    items: list[DiffItem] = []
    for key in sorted(src_map):
        t = src_map[key]
        if key not in tgt_map:
            items.append(DiffItem(
                object_type="trigger", object_name=t.name, change_type="new",
                phase=PHASE_CREATE_REPLACE, parent_table=t.table, source_payload=t,
                risk=RiskFlags(requires_individual_review=True),
            ))
        elif _trigger_key(t, *src_ctx) != _trigger_key(tgt_map[key], *tgt_ctx):
            items.append(DiffItem(
                object_type="trigger", object_name=t.name, change_type="modified",
                phase=PHASE_CREATE_REPLACE, parent_table=t.table,
                source_payload=t, target_payload=tgt_map[key],
                risk=RiskFlags(requires_individual_review=True), changed_attributes=["action"],
            ))
    for key in sorted(tgt_map):
        if key not in src_map:
            t = tgt_map[key]
            items.append(DiffItem(
                object_type="trigger", object_name=t.name, change_type="dropped",
                phase=PHASE_DROP_DEPENDENT, parent_table=t.table, target_payload=t,
                risk=RiskFlags(destructive=True, requires_individual_review=True),
            ))
    return items


def _trigger_key(t: TriggerInfo, database: str = "", engine: str = "") -> tuple:
    action = strip_self_schema_qualifier(t.action, database, engine)
    return (normalize_body(action), (t.timing or "").upper(),
            tuple(sorted(e.upper() for e in t.events)), (t.level or "").upper(),
            _norm_expr(t.when_condition))


# ---- Events (MySQL) -------------------------------------------------------- #
def _diff_events(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    src_ctx = (source.database, source.source_engine)
    tgt_ctx = (target.database, target.source_engine)
    src_map = {e.name: e for e in source.events}
    tgt_map = {e.name: e for e in target.events}
    items: list[DiffItem] = []
    for n in sorted(src_map):
        e = src_map[n]
        if n not in tgt_map:
            items.append(DiffItem(
                object_type="event", object_name=n, change_type="new",
                phase=PHASE_CREATE_REPLACE, source_payload=e,
                risk=RiskFlags(requires_individual_review=True),
            ))
        elif _event_key(e, *src_ctx) != _event_key(tgt_map[n], *tgt_ctx):
            items.append(DiffItem(
                object_type="event", object_name=n, change_type="modified",
                phase=PHASE_CREATE_REPLACE, source_payload=e, target_payload=tgt_map[n],
                risk=RiskFlags(requires_individual_review=True), changed_attributes=["body"],
            ))
    for n in sorted(tgt_map):
        if n not in src_map:
            items.append(DiffItem(
                object_type="event", object_name=n, change_type="dropped",
                phase=PHASE_DROP_DEPENDENT, target_payload=tgt_map[n],
                risk=RiskFlags(destructive=True, requires_individual_review=True),
            ))
    return items


def _event_key(e: EventInfo, database: str = "", engine: str = "") -> tuple:
    body = strip_self_schema_qualifier(e.body, database, engine)
    return (normalize_body(body), _norm_expr(e.schedule))


# ---- Secuencias (standalone) ----------------------------------------------- #
def _diff_sequences(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    src_map = {s.name: s for s in source.sequences}
    tgt_map = {s.name: s for s in target.sequences}
    items: list[DiffItem] = []
    for n in sorted(src_map):
        s = src_map[n]
        if n not in tgt_map:
            items.append(DiffItem(
                object_type="sequence", object_name=n, change_type="new",
                phase=PHASE_CREATE_PREREQ, source_payload=s, risk=RiskFlags(),
            ))
        elif _sequence_key(s) != _sequence_key(tgt_map[n]):
            items.append(DiffItem(
                object_type="sequence", object_name=n, change_type="modified",
                phase=PHASE_ALTER_MODIFY, source_payload=s, target_payload=tgt_map[n],
                risk=RiskFlags(needs_review=True),
                changed_attributes=["definition"],
            ))
    for n in sorted(tgt_map):
        if n not in src_map:
            items.append(DiffItem(
                object_type="sequence", object_name=n, change_type="dropped",
                phase=PHASE_DROP_PREREQ, target_payload=tgt_map[n],
                risk=RiskFlags(destructive=True),
            ))
    return items


def _sequence_key(s: SequenceInfo) -> tuple:
    # NUNCA se incluye last_value (estado). start_value tampoco dispara narrowing.
    return (s.data_type or "", s.increment, s.min_value, s.max_value, bool(s.cycle))


# ---- Tipos ENUM (PG) ------------------------------------------------------- #
def _diff_enum_types(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    src_map = {e.name: e for e in source.enum_types}
    tgt_map = {e.name: e for e in target.enum_types}
    items: list[DiffItem] = []
    for n in sorted(src_map):
        e = src_map[n]
        if n not in tgt_map:
            items.append(DiffItem(
                object_type="enum_type", object_name=n, change_type="new",
                phase=PHASE_CREATE_PREREQ, source_payload=e, risk=RiskFlags(),
            ))
        elif list(e.values) != list(tgt_map[n].values):
            risk = RiskFlags(needs_review=True)
            # quitar/reordenar valores obliga recrear el tipo y columnas dependientes
            if not set(tgt_map[n].values).issubset(set(e.values)):
                risk = risk.merge(destructive=True)
            items.append(DiffItem(
                object_type="enum_type", object_name=n, change_type="modified",
                phase=PHASE_CREATE_PREREQ, source_payload=e, target_payload=tgt_map[n],
                risk=risk, changed_attributes=["values"],
            ))
    for n in sorted(tgt_map):
        if n not in src_map:
            items.append(DiffItem(
                object_type="enum_type", object_name=n, change_type="dropped",
                phase=PHASE_DROP_PREREQ, target_payload=tgt_map[n],
                risk=RiskFlags(destructive=True),
            ))
    return items


# ---- Extensiones (PG) ------------------------------------------------------ #
def _diff_extensions(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
    src_map = {e.name: e for e in source.extensions}
    tgt_map = {e.name: e for e in target.extensions}
    items: list[DiffItem] = []
    for n in sorted(src_map):
        if n not in tgt_map:
            items.append(DiffItem(
                object_type="extension", object_name=n, change_type="new",
                phase=PHASE_CREATE_PREREQ, source_payload=src_map[n], risk=RiskFlags(),
            ))
        # version-only diff: COSMÉTICO -> no genera ítem.
    for n in sorted(tgt_map):
        if n not in src_map:
            items.append(DiffItem(
                object_type="extension", object_name=n, change_type="dropped",
                phase=PHASE_DROP_PREREQ, target_payload=tgt_map[n],
                risk=RiskFlags(destructive=True),
            ))
    return items


# --------------------------------------------------------------------------- #
# Orden de aplicación                                                          #
# --------------------------------------------------------------------------- #
# Las 9 FASES (``PHASE_*``) son la etiqueta gruesa que se muestra en la API. NO
# alcanzan para ordenar la ejecución: dentro de una fase el desempate alfabético por
# ``object_type`` producía errores garantizados del motor, y entre fases el número no
# siempre refleja la dependencia real. Casos verificados que fallaban:
#
#   - fase 3, ``check_constraint`` < ``column`` (alfabético): un CHECK sobre una columna
#     NUEVA se creaba antes de la columna -> MySQL 3813 / PostgreSQL 42703.
#   - fase 3, ``foreign_key`` < ``index``/``unique_constraint``: una FK se creaba antes
#     del índice/UNIQUE que necesita en la tabla referida -> MySQL errno 150.
#   - fase 3 (FK) ANTES de fase 4 (PK): una FK contra una tabla cuya PRIMARY KEY se
#     agrega en el mismo diff -> MySQL errno 150.
#   - fase 5, alfabético: ``event`` < ``materialized_view`` < ``routine`` < ``trigger``
#     < ``view``. Una vista que lee de OTRA vista, o una matview de PostgreSQL sobre una
#     vista, se creaba antes de su dependencia -> 1146 / 42P01.
#   - fase 7, ``column`` < ``foreign_key``: se borraba una columna todavía usada por una
#     FK que también se borra -> MySQL 1828.
#
# ``_STEP`` es el orden de ejecución REAL (grano fino, atravesando fases cuando la
# dependencia lo exige). Cada valor es un peldaño del pipeline de un DBA:
#
#   prerrequisitos -> [drop de cuerpos que bloquean ALTERs] -> CREATE TABLE ->
#   columnas -> PK -> índices/UNIQUE/CHECK -> FKs -> cuerpos (vistas/rutinas/...) ->
#   drop de cuerpos -> FKs -> índices -> UNIQUE -> CHECK -> columnas -> tablas ->
#   secuencias/tipos/extensiones
#
# Reglas de DBA que codifica (cada una es un error real que se evita):
#   * una FK se agrega SIEMPRE al final del bloque aditivo: para entonces ya existen la
#     PK/UNIQUE de la tabla referida y el índice de la columna referente;
#   * un CHECK/índice/UNIQUE se agrega DESPUÉS de las columnas nuevas que menciona;
#   * al borrar se recorre el camino INVERSO: primero las FKs (MySQL 1553/1828), después
#     índices/UNIQUE/CHECK, después las columnas y al final las tablas;
#   * los objetos con cuerpo se crean después de TODA la estructura y se ordenan entre sí
#     por dependencia real (vista sobre vista, rutina llamada por un trigger).
_STEP: dict[tuple[str, str], int] = {
    # --- prerrequisitos ---------------------------------------------------- #
    ("extension", "new"): 10,
    ("enum_type", "new"): 12,
    ("enum_type", "modified"): 13,
    ("sequence", "new"): 14,
    # --- estructura: creación ---------------------------------------------- #
    ("table", "new"): 30,
    ("column", "new"): 40,
    ("column", "modified"): 42,
    ("primary_key", "new"): 44,
    ("primary_key", "modified"): 44,
    ("primary_key", "dropped"): 44,
    ("sequence", "modified"): 46,
    ("index", "new"): 50,
    ("unique_constraint", "new"): 52,
    ("check_constraint", "new"): 54,
    ("index", "modified"): 56,
    ("unique_constraint", "modified"): 58,
    ("check_constraint", "modified"): 60,
    ("foreign_key", "new"): 70,
    ("foreign_key", "modified"): 72,
    # --- objetos con cuerpo: crear/reemplazar ------------------------------ #
    ("view", "new"): 80,
    ("view", "modified"): 80,
    ("materialized_view", "new"): 80,
    ("materialized_view", "modified"): 80,
    ("routine", "new"): 80,
    ("routine", "modified"): 80,
    ("trigger", "new"): 80,
    ("trigger", "modified"): 80,
    ("event", "new"): 80,
    ("event", "modified"): 80,
    # --- objetos con cuerpo: borrar ---------------------------------------- #
    ("view", "dropped"): 90,
    ("materialized_view", "dropped"): 90,
    ("routine", "dropped"): 90,
    ("trigger", "dropped"): 90,
    ("event", "dropped"): 90,
    # --- estructura: destrucción (camino inverso) -------------------------- #
    ("foreign_key", "dropped"): 100,
    ("index", "dropped"): 102,
    ("unique_constraint", "dropped"): 104,
    ("check_constraint", "dropped"): 106,
    ("column", "dropped"): 110,
    ("table", "dropped"): 120,
    ("sequence", "dropped"): 130,
    ("enum_type", "dropped"): 132,
    ("extension", "dropped"): 134,
}

# Paso al que se ADELANTA el DROP de un objeto con cuerpo que bloquea un ALTER/DROP de
# columna del que depende. PostgreSQL rechaza ``ALTER TABLE … ALTER COLUMN TYPE`` y
# ``DROP COLUMN`` si una vista/matview depende de esa columna ("cannot alter type of a
# column used by a view or rule"), así que la vista tiene que caer ANTES del ALTER, no
# después. MySQL/MariaDB no validan cuerpos de vista, pero adelantar el DROP es
# igualmente correcto ahí (el objeto se borra en ambos casos).
_STEP_BODY_DROP_EARLY = 20

# Paso al que se ADELANTA el DROP de una FK que bloquea un cambio de TIPO de alguna de sus
# columnas. MySQL/MariaDB rechazan ``MODIFY COLUMN`` sobre una columna que participa en una
# FK viva (``1832 Cannot change column …: used in a foreign key constraint``), tanto del
# lado referente como del referido (``3780``: los tipos tienen que seguir siendo
# compatibles). Si el diff elimina esa FK igual, hay que soltarla ANTES del ALTER en vez de
# en la fase destructiva (paso 100), que va mucho después.
_STEP_FK_DROP_EARLY = 38

# Paso al que se ADELANTA una RUTINA que el DDL de una tabla referencia. PostgreSQL permite
# funciones del usuario en un ``DEFAULT`` (``next_id()``), en un ``CHECK`` y en una columna
# ``GENERATED`` (si es IMMUTABLE) — y las valida al ejecutar el ``CREATE TABLE``/``ALTER``.
# Con las rutinas en el paso 80 (después de las tablas, que es lo correcto para el caso
# común: funciones que CONSULTAN tablas), ese CREATE TABLE fallaba con ``function … does
# not exist`` (42883). Solo se adelantan las rutinas efectivamente referenciadas por DDL de
# tabla, y NUNCA si el cuerpo de la rutina menciona a su vez una tabla del diff (dependencia
# mutua: una función SQL-language que consulta la tabla se valida al crearse — ahí gana el
# orden normal y el caso queda como estaba, fail-closed). MySQL/MariaDB no admiten funciones
# almacenadas en DEFAULT/CHECK/GENERATED, así que esto no los afecta.
_STEP_ROUTINE_PREREQ = 26

# Desempate por tipo cuando NO hay dependencia detectable entre dos objetos con cuerpo:
# lo que suele ser dependencia de otros va primero. Solo se usa dentro del mismo nivel
# topológico, así que nunca contradice una dependencia real.
_BODY_TYPE_ORDER = {
    "routine": 0,        # una vista/trigger puede llamar a una función
    "view": 1,
    "materialized_view": 2,  # una matview suele leer de vistas
    "trigger": 3,        # un trigger llama rutinas y vive sobre una tabla
    "event": 4,          # un evento suele llamar rutinas
}

# Tipos cuyo cuerpo puede REFERENCIAR otros objetos por nombre.
_BODY_TYPES = frozenset({"view", "materialized_view", "routine", "trigger", "event"})

# Identificadores dentro de un cuerpo SQL: `backtick`, "doble comilla" o palabra suelta.
_BODY_IDENT_RE = re.compile(r"`([^`]+)`|\"([^\"]+)\"|\b([A-Za-z_][A-Za-z_0-9$]*)\b")


def _table_dep_order(names: list[str], tables_by_name: dict[str, TableSchema]) -> dict[str, int]:
    """
    Rango topológico por FK (tabla referida antes que la referente); alfabético en
    empates. Un ciclo (o cualquier resto no colocable) va al final, de forma estable.

    Las FKs hacia tablas que NO están en ``names`` se ignoran: son dependencias FUERA
    del conjunto que se ordena (p. ej. una tabla nueva con FK a una tabla que YA existe
    en el destino). Contarlas hacía que esa tabla nunca se pudiera "colocar" y cayera al
    bucket de ciclos junto a sus dependientes, destruyendo el orden topológico del resto
    del lote. Para el clon —que pasa TODAS las tablas— el comportamiento es idéntico.

    El rango es un NIVEL topológico real: todas las tablas colocables en una misma pasada
    comparten nivel y ``placed`` se actualiza al TERMINAR la pasada, no en el medio. Antes
    se agregaba a ``placed`` dentro del bucle, así que una hija visitada después de su
    padre en la MISMA pasada heredaba su nivel (padre e hija ambas en 0). Eso no se notaba
    al crear —el desempate alfabético dejaba al padre primero por casualidad— pero rompía
    el DROP, que ordena por rango INVERTIDO: con ambas en 0, el desempate alfabético
    borraba la tabla PADRE primero y el motor respondía
    ``(1451, 'Cannot delete or update a parent row')``.
    """
    name_set = {n for n in names if n in tables_by_name}
    deps = {
        n: {
            fk.referred_table
            for fk in tables_by_name[n].foreign_keys
            if fk.referred_table in name_set and fk.referred_table != n
        }
        for n in name_set
    }
    rank: dict[str, int] = {}
    placed: set[str] = set()
    remaining = sorted(name_set)
    level = 0
    while remaining:
        ready = [n for n in remaining if deps[n] <= placed]
        if not ready:
            break
        for n in ready:
            rank[n] = level
            remaining.remove(n)
        placed.update(ready)
        level += 1
    for n in remaining:  # ciclo/dep externa: al final, estable
        rank[n] = level
    return rank


def _referenced_identifiers(text: str | None) -> set[str]:
    """Identificadores (en minúsculas) que aparecen en un cuerpo SQL."""
    if not text:
        return set()
    out: set[str] = set()
    for m in _BODY_IDENT_RE.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            out.add(name.lower())
    return out


def _body_text(item: DiffItem) -> str:
    """Texto del cuerpo de un objeto procedural, del lado que corresponda al cambio."""
    payload = item.target_payload if item.change_type == "dropped" else item.source_payload
    if payload is None:
        return ""
    for attr in ("definition", "body", "action"):
        value = getattr(payload, attr, None)
        if value:
            return str(value)
    return ""


def _bare_object_name(item: DiffItem) -> str:
    """
    Nombre "desnudo" del objeto, comparable contra los identificadores de un cuerpo.

    Las rutinas se nombran ``KIND:nombre`` (``PROCEDURE:sp_x``) y los sub-objetos de
    tabla ``tabla.objeto``; acá interesa el último segmento.
    """
    name = item.object_name
    if item.object_type == "routine" and ":" in name:
        name = name.split(":", 1)[1]
    return name.lower()


def _topological_levels(
    keys: list[str], must_run_before: dict[str, set[str]]
) -> dict[str, int]:
    """
    Nivel topológico de cada clave: 0 si no depende de nadie del lote, 1+max(niveles de
    sus dependencias) si depende. Un CICLO no revienta: las claves involucradas quedan
    todas en el último nivel (orden estable por nombre) — fail-closed, se ejecuta en un
    orden arbitrario pero determinístico en vez de abortar el diff completo.
    """
    level: dict[str, int] = {}
    pending = sorted(keys)
    guard = 0
    while pending and guard <= len(keys):
        guard += 1
        progressed = False
        for k in list(pending):
            deps = must_run_before.get(k, set()) & set(keys)
            if all(d in level for d in deps if d != k):
                level[k] = 1 + max((level[d] for d in deps if d != k and d in level), default=-1)
                pending.remove(k)
                progressed = True
        if not progressed:
            break
    fallback = max(level.values(), default=-1) + 1
    for k in pending:
        level[k] = fallback
    return level


def build_dependency_graph(items: list[DiffItem]) -> dict[str, set[str]]:
    """
    Grafo ``op_key -> {op_keys que deben ejecutarse ANTES}`` sobre los ítems de ESTE
    diff. Es la fuente de verdad tanto del orden topológico como de la validación de
    cierre de una selección parcial (adopción / ejecución ad-hoc de un subconjunto).

    Solo se registran aristas hacia ítems del MISMO diff: una dependencia que ya existe
    en el destino no necesita crearse ni seleccionarse. Aristas que se detectan:

    - **tabla nueva -> tabla nueva** por FK (la referida antes que la referente);
    - **sub-objeto de tabla -> su tabla nueva** (índice/FK/columna/PK de una tabla que se
      crea en este mismo diff);
    - **columna nueva -> el índice/UNIQUE/CHECK/FK que la menciona** (la columna primero);
    - **FK nueva -> PK/UNIQUE/índice de la tabla referida** creados acá (MySQL errno 150);
    - **cuerpo -> tablas y otros cuerpos que menciona** (vista sobre vista, matview sobre
      vista, trigger sobre su tabla, rutina llamada por un trigger/evento);
    - **cuerpo eliminado -> cuerpo eliminado que lo referencia** (arista INVERTIDA: el
      dependiente se borra primero, que es lo que exige PostgreSQL);
    - **tabla eliminada -> sub-objetos y cuerpos eliminados que la usan**.
    """
    by_key = {it.op_key(): it for it in items}
    deps: dict[str, set[str]] = {k: set() for k in by_key}

    def add(key: str, before: str) -> None:
        if before in by_key and before != key:
            deps[key].add(before)

    new_tables = {
        it.object_name for it in items if it.object_type == "table" and it.change_type == "new"
    }
    dropped_tables = {
        it.object_name for it in items if it.object_type == "table" and it.change_type == "dropped"
    }

    # 1) tabla nueva -> tabla nueva (FK). La tabla referida se crea primero.
    for it in items:
        if it.object_type == "table" and it.change_type == "new":
            tbl = it.source_payload
            for fk in getattr(tbl, "foreign_keys", []) or []:
                if fk.referred_table in new_tables:
                    add(it.op_key(), f"table|{fk.referred_table}|new")
        # Al BORRAR la arista se invierte: la tabla HIJA cae primero, si no el motor
        # rechaza el DROP de la padre (MySQL 1451 / PostgreSQL 2BP01).
        elif it.object_type == "table" and it.change_type == "dropped":
            tbl = it.target_payload
            for fk in getattr(tbl, "foreign_keys", []) or []:
                if fk.referred_table in dropped_tables:
                    add(f"table|{fk.referred_table}|dropped", it.op_key())

    # 2) sub-objetos de tabla: dependen de su tabla (nueva) o la tabla eliminada depende
    #    de ellos (los sub-objetos se borran antes que la tabla).
    for it in items:
        parent = it.parent_table
        if not parent:
            continue
        if it.change_type in ("new", "modified") and parent in new_tables:
            add(it.op_key(), f"table|{parent}|new")
        if it.change_type == "dropped" and parent in dropped_tables:
            add(f"table|{parent}|dropped", it.op_key())

    # 3) columnas nuevas -> constraints/índices que las mencionan.
    new_cols_by_table: dict[str, dict[str, str]] = {}
    for it in items:
        if it.object_type == "column" and it.change_type == "new" and it.parent_table:
            col = getattr(it.source_payload, "name", None)
            if col:
                new_cols_by_table.setdefault(it.parent_table, {})[col.lower()] = it.op_key()
    for it in items:
        if it.change_type == "dropped" or not it.parent_table:
            continue
        cols_here = new_cols_by_table.get(it.parent_table)
        if not cols_here:
            continue
        if it.object_type in ("index", "unique_constraint", "foreign_key", "primary_key"):
            used = _constraint_columns(it)
            for c in used:
                if c.lower() in cols_here:
                    add(it.op_key(), cols_here[c.lower()])
        elif it.object_type == "check_constraint":
            # Un CHECK menciona columnas dentro de una expresión: se escanea el texto.
            mentioned = _referenced_identifiers(
                getattr(it.source_payload, "sqltext", None)
            )
            for cname, ckey in cols_here.items():
                if cname in mentioned:
                    add(it.op_key(), ckey)

    # 4) FK nueva -> la clave (PK/UNIQUE/índice) de la tabla REFERIDA que se crea acá.
    #    MySQL exige que la columna referida esté indexada al crear la FK (errno 150).
    key_providers: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for it in items:
        if it.change_type == "dropped" or not it.parent_table:
            continue
        if it.object_type in ("primary_key", "unique_constraint", "index"):
            cols = tuple(c.lower() for c in _constraint_columns(it))
            if cols:
                key_providers.setdefault(it.parent_table, []).append((cols, it.op_key()))
    for it in items:
        if it.object_type != "foreign_key" or it.change_type == "dropped":
            continue
        fk = it.source_payload
        referred = getattr(fk, "referred_table", None)
        ref_cols = tuple(c.lower() for c in (getattr(fk, "referred_columns", []) or []))
        for cols, provider in key_providers.get(referred, []):
            # La clave sirve de respaldo si EMPIEZA por las columnas referidas.
            if cols[: len(ref_cols)] == ref_cols:
                add(it.op_key(), provider)
        # La tabla REFERIDA también es dependencia directa: sin ella la FK no se puede
        # crear (y una selección parcial que la omita fallaría con errno 150 / 42P01).
        if referred in new_tables:
            add(it.op_key(), f"table|{referred}|new")

    # 5) tabla eliminada -> FKs ENTRANTES eliminadas (desde otras tablas). Sin borrar
    #    primero la FK que la referencia, el DROP TABLE falla (MySQL 1451/3730).
    for it in items:
        if it.object_type != "foreign_key" or it.change_type != "dropped":
            continue
        referred = getattr(it.target_payload, "referred_table", None)
        if referred in dropped_tables:
            add(f"table|{referred}|dropped", it.op_key())

    # 6) tabla/columna nueva -> el TIPO ENUM o la SECUENCIA que usa.
    #    Muy común en PostgreSQL: una columna declarada ``estado mi_estado`` o con default
    #    ``nextval('mi_seq'::regclass)`` no se puede crear si el tipo/la secuencia no
    #    existen. El orden ya lo garantizan los pasos (prerrequisitos antes que tablas);
    #    la arista hace falta para el CIERRE de una selección parcial: elegir la tabla sin
    #    su ENUM fallaba con "type does not exist".
    prereq_by_name: dict[str, list[str]] = {}
    for it in items:
        if it.object_type in ("enum_type", "sequence") and it.change_type in ("new", "modified"):
            prereq_by_name.setdefault(it.object_name.lower(), []).append(it.op_key())
    if prereq_by_name:
        for it in items:
            if it.change_type == "dropped":
                continue
            if it.object_type == "table":
                columns = getattr(it.source_payload, "columns", []) or []
            elif it.object_type == "column":
                columns = [it.source_payload] if it.source_payload is not None else []
            else:
                continue
            mentioned: set[str] = set()
            for c in columns:
                # El TIPO y el DEFAULT son los dos lugares donde una columna nombra un
                # tipo ENUM o una secuencia. También la expresión de una columna generada.
                parts = [getattr(c, "type", ""), getattr(c, "default", "") or ""]
                computed = getattr(c, "computed", None)
                if computed is not None:
                    parts.append(getattr(computed, "sqltext", "") or "")
                mentioned |= _referenced_identifiers(" ".join(str(p) for p in parts))
            for name in mentioned:
                for provider in prereq_by_name.get(name, []):
                    add(it.op_key(), provider)

    # 7) tabla/columna/CHECK nueva -> la RUTINA que su DDL invoca (PostgreSQL).
    #    ``DEFAULT next_id()``, ``CHECK (validar(x))`` o una columna GENERATED con función
    #    inmutable se validan al ejecutar el CREATE/ALTER: la función tiene que existir
    #    antes. La arista alimenta el cierre de selección; el ORDEN lo resuelve el hoist
    #    de ``order_diff_items`` (las rutinas viven en el paso 80, después de las tablas).
    for consumer_key, routine_keys in _table_ddl_routine_deps(items).items():
        for rkey in routine_keys:
            add(consumer_key, rkey)

    # 8) objetos con cuerpo: dependencias por nombre mencionado en el cuerpo.
    body_items = [it for it in items if it.object_type in _BODY_TYPES]
    providers: dict[str, list[str]] = {}
    for it in items:
        if it.object_type == "table" or it.object_type in _BODY_TYPES:
            providers.setdefault(_bare_object_name(it), []).append(it.op_key())
    for it in body_items:
        mentioned = _referenced_identifiers(_body_text(it))
        own = _bare_object_name(it)
        # Un trigger depende SIEMPRE de su tabla (dependencia firme, no textual).
        if it.object_type == "trigger" and it.parent_table:
            mentioned.add(it.parent_table.lower())
        for name in mentioned:
            if name == own:
                continue
            for cand_key in providers.get(name, []):
                cand = by_key[cand_key]
                if it.change_type == "dropped":
                    # Al BORRAR la arista se invierte: el dependiente cae primero
                    # (PostgreSQL rechaza borrar un objeto del que otro depende).
                    if cand.change_type == "dropped":
                        add(cand_key, it.op_key())
                elif cand.change_type in ("new", "modified"):
                    add(it.op_key(), cand_key)
    return deps


def _constraint_columns(item: DiffItem) -> list[str]:
    """Columnas que un sub-objeto de tabla (índice/UNIQUE/FK/PK) toca del lado SOURCE."""
    payload = item.source_payload
    if payload is None:
        return []
    if item.object_type == "primary_key":
        return list(getattr(payload, "primary_key", []) or [])
    return list(getattr(payload, "columns", []) or [])


def _hoist_blocking_body_drops(
    items: list[DiffItem], deps: dict[str, set[str]]
) -> set[str]:
    """
    ``op_key`` de los DROP de objetos con cuerpo que hay que ADELANTAR porque bloquean
    un ALTER/DROP de columna, o el DROP de la tabla, de la que dependen.

    PostgreSQL rechaza ``ALTER COLUMN TYPE``/``DROP COLUMN``/``DROP TABLE`` mientras una
    vista o matview dependa de ese objeto ("cannot drop … because other objects depend on
    it"). El orden natural (cuerpos al paso 90, después de los ALTER del 42/110) fallaría
    siempre. Solo se adelantan los DROP cuyo cuerpo menciona una tabla realmente afectada
    por un cambio de columna o por su propia eliminación — no todos, para no reordenar de
    más.
    """
    touched_tables = {
        it.parent_table
        for it in items
        if it.parent_table
        and it.object_type == "column"
        and it.change_type in ("modified", "dropped")
    }
    touched_tables |= {
        it.object_name
        for it in items
        if it.object_type == "table" and it.change_type == "dropped"
    }
    if not touched_tables:
        return set()
    touched_lower = {t.lower() for t in touched_tables if t}
    hoisted: set[str] = set()
    for it in items:
        if it.object_type not in _BODY_TYPES or it.change_type != "dropped":
            continue
        mentioned = _referenced_identifiers(_body_text(it))
        if it.object_type == "trigger" and it.parent_table:
            mentioned.add(it.parent_table.lower())
        if mentioned & touched_lower:
            hoisted.add(it.op_key())
    # Cierre: si se adelanta un cuerpo, también los cuerpos que deben caer ANTES que él
    # (sus dependientes), o quedarían huérfanos apuntando a un objeto ya borrado.
    changed = True
    while changed:
        changed = False
        for key in list(hoisted):
            for dep in deps.get(key, set()):
                if dep not in hoisted and _is_body_drop(dep):
                    hoisted.add(dep)
                    changed = True
    return hoisted


def _is_body_drop(op_key: str) -> bool:
    parts = op_key.split("|")
    return len(parts) == 3 and parts[0] in _BODY_TYPES and parts[2] == "dropped"


def _hoist_blocking_fk_drops(items: list[DiffItem]) -> set[str]:
    """
    ``op_key`` de los DROP de FK que hay que ADELANTAR porque bloquean un cambio de TIPO
    de alguna de sus columnas.

    MySQL/MariaDB rechazan ``MODIFY COLUMN`` mientras una FK use esa columna
    (``1832``), y también si el cambio rompe la compatibilidad de tipos con el otro lado
    (``3780``). El caso típico: el origen cambió el tipo de la columna Y eliminó la FK — el
    orden natural (columna en el paso 42, DROP de FK en el 100) falla siempre. Se adelanta
    SOLO la FK que realmente toca una columna cuyo TIPO cambia: no todas, para no reordenar
    de más ni perder la garantía de que los DROP van al final.
    """
    retyped: dict[str, set[str]] = {}
    for it in items:
        if (
            it.object_type == "column"
            and it.change_type == "modified"
            and it.parent_table
            and "type" in it.changed_attributes
        ):
            name = getattr(it.source_payload, "name", None)
            if name:
                retyped.setdefault(it.parent_table, set()).add(name.lower())
    if not retyped:
        return set()
    hoisted: set[str] = set()
    for it in items:
        if it.object_type != "foreign_key" or it.change_type != "dropped":
            continue
        fk = it.target_payload
        own = {c.lower() for c in (getattr(fk, "columns", []) or [])}
        ref = {c.lower() for c in (getattr(fk, "referred_columns", []) or [])}
        referred_table = getattr(fk, "referred_table", None)
        if (it.parent_table and own & retyped.get(it.parent_table, set())) or (
            referred_table and ref & retyped.get(referred_table, set())
        ):
            hoisted.add(it.op_key())
    return hoisted


def _table_ddl_routine_deps(items: list[DiffItem]) -> dict[str, set[str]]:
    """
    ``op_key`` de tabla/columna/CHECK -> rutinas del diff que su DDL invoca.

    PostgreSQL valida al ejecutar el ``CREATE TABLE``/``ALTER`` cualquier función usada en
    un ``DEFAULT``, un ``CHECK`` o una columna ``GENERATED`` — si la función se crea
    después (paso 80, tras las tablas), el DDL muere con ``42883 function … does not
    exist``. MySQL/MariaDB no admiten funciones almacenadas ahí, así que nunca aporta
    aristas para ellos.

    Exclusión de dependencia MUTUA (fail-closed): si el CUERPO de la rutina menciona a su
    vez una tabla del diff, NO se registra la arista ni se adelanta — una función
    SQL-language que consulta la tabla se valida al crearse, y adelantarla rompería ese
    caso. Ahí se conserva el orden actual (tabla primero) y el caso patológico
    (dependencia circular real) sigue requiriendo un override manual, como hasta ahora.
    """
    routines: dict[str, str] = {}  # nombre desnudo -> op_key
    routine_bodies: dict[str, str] = {}
    for it in items:
        if it.object_type == "routine" and it.change_type in ("new", "modified"):
            bare = _bare_object_name(it)
            routines[bare] = it.op_key()
            routine_bodies[bare] = _body_text(it)
    if not routines:
        return {}
    table_names = {
        it.object_name.lower()
        for it in items
        if it.object_type == "table" and it.change_type in ("new", "modified")
    }

    def _ddl_texts(it: DiffItem) -> list[str]:
        if it.object_type == "table":
            tbl = it.source_payload
            cols = getattr(tbl, "columns", []) or []
            checks = getattr(tbl, "check_constraints", []) or []
        elif it.object_type == "column":
            cols = [it.source_payload] if it.source_payload is not None else []
            checks = []
        elif it.object_type == "check_constraint":
            cols, checks = [], [it.source_payload] if it.source_payload is not None else []
        else:
            return []
        parts: list[str] = []
        for c in cols:
            parts.append(str(getattr(c, "default", "") or ""))
            computed = getattr(c, "computed", None)
            if computed is not None:
                parts.append(str(getattr(computed, "sqltext", "") or ""))
        for ck in checks:
            parts.append(str(getattr(ck, "sqltext", "") or ""))
        return parts

    out: dict[str, set[str]] = {}
    for it in items:
        if it.change_type == "dropped":
            continue
        texts = _ddl_texts(it)
        if not texts:
            continue
        mentioned = _referenced_identifiers(" ".join(texts))
        for bare, rkey in routines.items():
            if bare not in mentioned:
                continue
            # Dependencia mutua: el cuerpo de la rutina usa una tabla del diff -> no tocar.
            body_refs = _referenced_identifiers(routine_bodies.get(bare, ""))
            if body_refs & table_names:
                continue
            out.setdefault(it.op_key(), set()).add(rkey)
    return out


def order_diff_items(
    items: list[DiffItem], source: SchemaSnapshot, target: SchemaSnapshot
) -> list[DiffItem]:
    """
    Ordena los ítems por ORDEN DE EJECUCIÓN real (``_STEP``) y, dentro de cada paso, por
    dependencia topológica. Además puebla ``depends_on``/``execution_step`` en cada ítem
    (los consume la validación de cierre de selección y el renderer).

    Dentro de cada paso:
      - ``table new``: topológico por FK (padre antes que hijo);
      - ``table dropped``: topológico INVERSO (hijo antes que padre);
      - objetos con cuerpo: topológico por referencias del cuerpo (vista sobre vista,
        matview sobre vista, trigger/evento sobre la rutina que llaman), invertido para
        los DROP;
      - resto: topológico por el grafo de dependencias y, en empate, alfabético estable.
    """
    deps = build_dependency_graph(items)
    hoisted = _hoist_blocking_body_drops(items, deps)
    hoisted_fks = _hoist_blocking_fk_drops(items)
    # Rutinas que el DDL de una tabla invoca (DEFAULT/CHECK/GENERATED en PostgreSQL):
    # se crean ANTES de las tablas o el CREATE TABLE muere con 42883.
    hoisted_routines = {
        rkey for rkeys in _table_ddl_routine_deps(items).values() for rkey in rkeys
    }

    src_by_name = {t.table: t for t in source.tables}
    tgt_by_name = {t.table: t for t in target.tables}
    new_tbl_names = [
        i.object_name for i in items if i.object_type == "table" and i.change_type == "new"
    ]
    drop_tbl_names = [
        i.object_name for i in items if i.object_type == "table" and i.change_type == "dropped"
    ]
    new_rank = _table_dep_order(new_tbl_names, src_by_name)
    drop_rank = _table_dep_order(drop_tbl_names, tgt_by_name)

    # Paso de ejecución de cada ítem (con el adelanto de los DROP bloqueantes aplicado).
    step_of: dict[str, int] = {}
    for it in items:
        key = it.op_key()
        base = _STEP.get((it.object_type, it.change_type))
        if base is None:
            # Combinación no prevista: se ejecuta con los cuerpos (paso 80), que es el
            # punto más tardío de la fase de creación. Nunca se descarta un ítem.
            base = 80
        if key in hoisted:
            base = _STEP_BODY_DROP_EARLY
        elif key in hoisted_fks:
            base = _STEP_FK_DROP_EARLY
        elif key in hoisted_routines:
            base = _STEP_ROUTINE_PREREQ
        step_of[key] = base

    # Nivel topológico DENTRO de cada paso (una dependencia de otro paso ya queda
    # ordenada por el paso; mezclar niveles entre pasos distintos no aporta).
    levels: dict[str, int] = {}
    by_step: dict[int, list[DiffItem]] = {}
    for it in items:
        by_step.setdefault(step_of[it.op_key()], []).append(it)
    for group in by_step.values():
        keys = [it.op_key() for it in group]
        keyset = set(keys)
        local = {k: (deps.get(k, set()) & keyset) for k in keys}
        levels.update(_topological_levels(keys, local))

    for it in items:
        it.depends_on = sorted(deps.get(it.op_key(), set()))
        it.execution_step = step_of[it.op_key()]

    def key(it: DiffItem):
        k = it.op_key()
        step = step_of[k]
        level = levels.get(k, 0)
        if it.object_type == "table" and it.change_type == "new":
            return (step, level, new_rank.get(it.object_name, 0), 0, it.object_name)
        if it.object_type == "table" and it.change_type == "dropped":
            # inverso: mayor rango primero (la hija antes que la padre)
            return (step, level, -drop_rank.get(it.object_name, 0), 0, it.object_name)
        type_order = _BODY_TYPE_ORDER.get(it.object_type, 0)
        if it.change_type == "dropped" and it.object_type in _BODY_TYPES:
            type_order = -type_order  # al borrar, el orden por tipo también se invierte
        return (step, level, 0, type_order, f"{it.object_type}:{it.object_name}")

    return sorted(items, key=key)


__all__ = [
    "RiskFlags", "DiffItem", "SchemaDiff", "RenderedStatement",
    "build_dependency_graph", "diff_snapshots", "order_diff_items",
    "canonical_type", "normalize_default", "normalize_body",
    "effective_collation", "effective_charset", "is_narrowing",
    "PHASE_CREATE_PREREQ", "PHASE_CREATE_TABLE", "PHASE_ALTER_ADDITIVE",
    "PHASE_ALTER_MODIFY", "PHASE_CREATE_REPLACE", "PHASE_DROP_DEPENDENT",
    "PHASE_ALTER_DESTRUCTIVE", "PHASE_DROP_TABLE", "PHASE_DROP_PREREQ",
]
