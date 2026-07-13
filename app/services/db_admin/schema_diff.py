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


class SchemaDiff(BaseModel):
    """Resultado del diff: cabecera + lista de ítems ya clasificados y ordenados."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_engine: str
    target_engine: str
    cross_flavor_warning: bool = False
    scope_note: str | None = None
    items: list[DiffItem] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, dict[str, int]]:
        """Conteos por (object_type -> change_type -> n)."""
        out: dict[str, dict[str, int]] = {}
        for it in self.items:
            out.setdefault(it.object_type, {})
            out[it.object_type][it.change_type] = (
                out[it.object_type].get(it.change_type, 0) + 1
            )
        return out

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
        if ix.unique:
            continue  # las UNIQUE van inline en el CREATE TABLE
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
        if tgt.primary_key:  # había PK -> hay que droppearla: destructivo
            risk = risk.merge(destructive=True)
        else:
            risk = risk.merge(needs_review=True)  # ADD PK valida datos existentes
        items.append(
            DiffItem(
                object_type="primary_key", object_name=f"{table}.PRIMARY", change_type="modified",
                phase=PHASE_ALTER_MODIFY, parent_table=table,
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
    )

    # --- unique constraints ------------------------------------------------- #
    items += _diff_collection(
        table, "unique_constraint", src.unique_constraints, tgt.unique_constraints,
        sig=_unique_signature,
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
    )

    # --- check constraints -------------------------------------------------- #
    items += _diff_collection(
        table, "check_constraint", src.check_constraints, tgt.check_constraints,
        sig=lambda c: _check_signature(c),
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
    )

    # --- índices (no PK/unique-constraint; match por firma) ----------------- #
    items += _diff_collection(
        table, "index", src.indexes, tgt.indexes,
        sig=_index_signature,
        new_phase=PHASE_ALTER_ADDITIVE, drop_phase=PHASE_ALTER_DESTRUCTIVE,
        new_risk=RiskFlags(lock_heavy=True),
        drop_risk=RiskFlags(destructive=True),
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
) -> list[DiffItem]:
    """
    Diffea una colección de sub-objetos de tabla (FK/índice/unique/check) por FIRMA
    de definición. El nombre autogenerado NO es criterio de identidad (se anota como
    secundario). ``opts`` (si se da) compara atributos extra de un match (p.ej. las
    opciones referenciales de una FK) -> genera un ítem 'modified'.
    """
    items: list[DiffItem] = []
    src_map: dict[tuple, Any] = {sig(o): o for o in src_objs}
    tgt_map: dict[tuple, Any] = {sig(o): o for o in tgt_objs}

    for s, sobj in src_map.items():
        if s not in tgt_map:
            name = getattr(sobj, "name", None)
            items.append(
                DiffItem(
                    object_type=object_type,
                    object_name=f"{table}.{name}" if name else f"{table}.<{object_type}>",
                    change_type="new", phase=new_phase, parent_table=table,
                    source_payload=sobj, risk=new_risk.model_copy(deep=True),
                )
            )
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
            elif _view_key(v) != _view_key(tgt_map[n]):
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


def _view_key(v: ViewInfo) -> tuple:
    return (normalize_body(v.definition), v.check_option or "", bool(v.security_definer),
            tuple(v.columns))


# ---- Rutinas --------------------------------------------------------------- #
def _diff_routines(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
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
        elif _routine_key(r) != _routine_key(tgt_map[key]):
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


def _routine_key(r: RoutineInfo) -> tuple:
    params = tuple((p.mode or "", p.type) for p in r.parameters)
    return (normalize_body(r.body), r.return_type or "", (r.language or "").lower(),
            (r.volatility or "").lower(), bool(r.security_definer), params)


# ---- Triggers -------------------------------------------------------------- #
def _diff_triggers(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
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
        elif _trigger_key(t) != _trigger_key(tgt_map[key]):
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


def _trigger_key(t: TriggerInfo) -> tuple:
    return (normalize_body(t.action), (t.timing or "").upper(),
            tuple(sorted(e.upper() for e in t.events)), (t.level or "").upper(),
            _norm_expr(t.when_condition))


# ---- Events (MySQL) -------------------------------------------------------- #
def _diff_events(source: SchemaSnapshot, target: SchemaSnapshot) -> list[DiffItem]:
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
        elif _event_key(e) != _event_key(tgt_map[n]):
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


def _event_key(e: EventInfo) -> tuple:
    return (normalize_body(e.body), _norm_expr(e.schedule))


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
# Orden de aplicación (pipeline de 9 fases)                                    #
# --------------------------------------------------------------------------- #
def _table_dep_order(names: list[str], tables_by_name: dict[str, TableSchema]) -> dict[str, int]:
    """Rango topológico por FK (referida antes que referente); alfabético en empates."""
    deps = {
        n: {fk.referred_table for fk in tables_by_name[n].foreign_keys
            if fk.referred_table in tables_by_name and fk.referred_table != n}
        for n in names if n in tables_by_name
    }
    rank: dict[str, int] = {}
    placed: set[str] = set()
    remaining = sorted(n for n in names if n in tables_by_name)
    level = 0
    progress = True
    while remaining and progress:
        progress = False
        for n in list(remaining):
            if deps[n] <= placed:
                rank[n] = level
                placed.add(n)
                remaining.remove(n)
                progress = True
        level += 1
    for n in remaining:  # ciclo/dep externa: al final, estable
        rank[n] = level
    return rank


def order_diff_items(
    items: list[DiffItem], source: SchemaSnapshot, target: SchemaSnapshot
) -> list[DiffItem]:
    """
    Ordena por fase (1..9) y, dentro de cada fase, con orden estable útil:
    - fase 2 (crear tablas): topológico por FK (padre antes que hijo);
    - fase 8 (borrar tablas): topológico INVERSO (hijo antes que padre);
    - resto: por (object_type, object_name).
    """
    src_by_name = {t.table: t for t in source.tables}
    tgt_by_name = {t.table: t for t in target.tables}
    new_tbl_names = [i.object_name for i in items if i.object_type == "table" and i.change_type == "new"]
    drop_tbl_names = [i.object_name for i in items if i.object_type == "table" and i.change_type == "dropped"]
    new_rank = _table_dep_order(new_tbl_names, src_by_name)
    drop_rank = _table_dep_order(drop_tbl_names, tgt_by_name)

    def key(it: DiffItem):
        if it.phase == PHASE_CREATE_TABLE and it.object_type == "table":
            return (it.phase, new_rank.get(it.object_name, 0), it.object_name)
        if it.phase == PHASE_DROP_TABLE and it.object_type == "table":
            # inverso: mayor rango primero
            return (it.phase, -drop_rank.get(it.object_name, 0), it.object_name)
        return (it.phase, 0, f"{it.object_type}:{it.object_name}")

    return sorted(items, key=key)


__all__ = [
    "RiskFlags", "DiffItem", "SchemaDiff", "RenderedStatement",
    "diff_snapshots", "order_diff_items",
    "canonical_type", "normalize_default", "normalize_body",
    "effective_collation", "effective_charset", "is_narrowing",
    "PHASE_CREATE_PREREQ", "PHASE_CREATE_TABLE", "PHASE_ALTER_ADDITIVE",
    "PHASE_ALTER_MODIFY", "PHASE_CREATE_REPLACE", "PHASE_DROP_DEPENDENT",
    "PHASE_ALTER_DESTRUCTIVE", "PHASE_DROP_TABLE", "PHASE_DROP_PREREQ",
]
