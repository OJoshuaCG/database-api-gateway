"""
Distribución de un snapshot en versiones de blueprint (snapshot selectivo).

Módulo PURO (sin tocar ningún motor): decide cómo repartir las sentencias
estructurales de un ``StructureDump`` y sus datos-semilla en una o varias
migraciones (``ModelMigration``), según el layout elegido:

- ``single``   : todo el esquema seleccionado en una sola versión (comportamiento
  histórico) + una versión de datos por tabla.
- ``by_class`` : una versión por CLASE de objeto (tablas → vistas → vistas
  materializadas → rutinas → triggers → events), datos al final.
- ``manual``   : el usuario agrupa los objetos por versión (buckets ordenados); el
  gateway valida la coherencia TOPOLÓGICA y asigna los números de versión.

Invariantes que garantiza:
- Las TABLAS se emiten en orden de dependencia por FK (``depends_on``), corrigiendo
  el orden alfabético del dump (que podía romper el re-apply con FKs cruzadas).
- Los DATOS van SIEMPRE en la(s) última(s) versión(es), aislados de la estructura y
  con una versión por tabla (rollback granular por PK).
- Un objeto nunca queda en una versión anterior a una de sus dependencias.

``validate_manual_layout`` es la puerta: devuelve TODAS las violaciones (no la primera)
para un error 422 accionable, sin exponer SQL ni valores crudos.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.db_admin.dtos import DumpStatement, SeedResult
from app.services.db_admin.sql_dialect import RollbackGenerator

# Orden canónico de re-aplicación por clase de objeto (prerequisitos → dependientes).
_CLASS_ORDER = [
    "extension",
    "type",
    "sequence",
    "table",
    "index",
    "view",
    "materialized_view",
    "routine",
    "trigger",
    "event",
]
_CLASS_RANK = {name: i for i, name in enumerate(_CLASS_ORDER)}

# Objetos procedurales cuyo cuerpo NO es traducible cross-engine (atan al motor).
_NON_PORTABLE_TYPES = frozenset({"routine", "trigger", "event"})

# Prerequisitos de las tablas: deben ir en una versión <= la de CUALQUIER tabla.
_PREREQ_TYPES = frozenset({"extension", "type", "sequence"})

# Objetos cuyas dependencias NO parseamos (podrían referenciar cualquier tabla): regla
# conservadora → deben ir en una versión >= la de TODAS las tablas.
_AFTER_ALL_TABLES_TYPES = frozenset({"view", "materialized_view", "routine"})

# Grupos del layout by_class, en orden. Las tablas arrastran sus prerequisitos e índices
# en la MISMA versión (nunca separar una tabla de sus índices/constraints).
_BY_CLASS_GROUPS: list[tuple[frozenset[str], str]] = [
    (frozenset({"extension", "type", "sequence", "table", "index"}), "Estructura base (tablas)"),
    (frozenset({"view"}), "Vistas"),
    (frozenset({"materialized_view"}), "Vistas materializadas"),
    (frozenset({"routine"}), "Rutinas"),
    (frozenset({"trigger"}), "Triggers"),
    (frozenset({"event"}), "Events"),
]


@dataclass
class VersionPlan:
    """Una versión a materializar como ``ModelMigration`` (la persiste el controller)."""

    name: str
    kind: str  # 'schema' | 'data'
    up_sql: str
    down_sql_suggested: str | None
    has_non_portable: bool
    object_counts: dict[str, int] = field(default_factory=dict)


def class_rank(object_type: str) -> int:
    """Rango de orden de una clase de objeto (las desconocidas van al final)."""
    return _CLASS_RANK.get(object_type, len(_CLASS_ORDER))


# --------------------------------------------------------------------------- #
# Filtrado por include/exclude                                                 #
# --------------------------------------------------------------------------- #
def filter_statements(
    statements: list[DumpStatement],
    *,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    include_objects: list[dict] | None = None,
    exclude_objects: list[dict] | None = None,
) -> list[DumpStatement]:
    """
    Aplica los filtros de selección de objetos. ``include_*`` (si se dan) restringe;
    ``exclude_*`` siempre quita. Los objetos se identifican por ``(object_type, name)``.
    """
    inc_types = set(include_types) if include_types else None
    exc_types = set(exclude_types or [])
    inc_objs = {(o["object_type"], o["name"]) for o in (include_objects or [])} or None
    exc_objs = {(o["object_type"], o["name"]) for o in (exclude_objects or [])}

    out: list[DumpStatement] = []
    for s in statements:
        key = (s.object_type, s.name)
        if inc_types is not None and s.object_type not in inc_types:
            continue
        if s.object_type in exc_types:
            continue
        if inc_objs is not None and key not in inc_objs:
            continue
        if key in exc_objs:
            continue
        out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Orden topológico de tablas por FK + orden canónico global                    #
# --------------------------------------------------------------------------- #
def _topo_sort_tables(tables: list[DumpStatement]) -> list[DumpStatement]:
    """
    Ordena tablas por dependencia de FK (referida antes que referente), alfabético
    dentro de cada "capa". Ante un ciclo de FK, emite el resto en orden alfabético
    (best-effort): el ciclo se resolvería con constraints diferidas, pero al menos no
    empeora el orden alfabético previo.
    """
    nameset = {t.name for t in tables}
    by_name = {t.name: t for t in tables}
    deps = {
        t.name: {d for d in t.depends_on if d in nameset and d != t.name} for t in tables
    }
    ordered: list[DumpStatement] = []
    placed: set[str] = set()
    remaining = sorted(nameset)
    progress = True
    while remaining and progress:
        progress = False
        for n in list(remaining):
            if deps[n] <= placed:
                ordered.append(by_name[n])
                placed.add(n)
                remaining.remove(n)
                progress = True
    # Ciclo (o dep externa): agregar lo restante en orden alfabético estable.
    for n in remaining:
        ordered.append(by_name[n])
    return ordered


def order_statements(statements: list[DumpStatement]) -> list[DumpStatement]:
    """Ordena por clase canónica; dentro de las tablas, orden topológico por FK."""
    by_class: dict[str, list[DumpStatement]] = {}
    for s in statements:
        by_class.setdefault(s.object_type, []).append(s)

    result: list[DumpStatement] = []
    for cls in _CLASS_ORDER:
        items = by_class.get(cls, [])
        if not items:
            continue
        if cls == "table":
            result.extend(_topo_sort_tables(items))
        else:
            result.extend(sorted(items, key=lambda s: s.name))
    # Clases desconocidas: al final, deterministas.
    known = set(_CLASS_ORDER)
    unknown = [s for s in statements if s.object_type not in known]
    result.extend(sorted(unknown, key=lambda s: (s.object_type, s.name)))
    return result


# --------------------------------------------------------------------------- #
# Render de SQL de una versión                                                 #
# --------------------------------------------------------------------------- #
def _render_schema_sql(statements: list[DumpStatement]) -> str:
    """Concatena el DDL (una sentencia por objeto, terminada en ';')."""
    return "\n\n".join(f"{s.ddl.rstrip().rstrip(';')};" for s in statements)


def _counts(statements: list[DumpStatement]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in statements:
        counts[s.object_type] = counts.get(s.object_type, 0) + 1
    return counts


def _schema_version(
    name: str, statements: list[DumpStatement], *, source_engine: str
) -> VersionPlan:
    ordered = order_statements(statements)
    up_sql = _render_schema_sql(ordered)
    # Rollback SUGERIDO solo si el origen es MySQL/MariaDB (dialecto de referencia del
    # RollbackGenerator); para PostgreSQL se deja None (no arriesgar un DROP mal parseado).
    suggested = None
    if source_engine in ("mysql", "mariadb"):
        suggested = RollbackGenerator().generate(up_sql)
    return VersionPlan(
        name=name,
        kind="schema",
        up_sql=up_sql,
        down_sql_suggested=suggested,
        has_non_portable=any(s.object_type in _NON_PORTABLE_TYPES for s in ordered),
        object_counts=_counts(ordered),
    )


def _data_version(seed: SeedResult, *, name: str | None = None) -> VersionPlan:
    return VersionPlan(
        name=name or f"Datos: {seed.table}",
        kind="data",
        up_sql=seed.up_sql or "",
        down_sql_suggested=seed.down_sql,
        has_non_portable=False,
        object_counts={"data": seed.row_count},
    )


# --------------------------------------------------------------------------- #
# Construcción de versiones                                                    #
# --------------------------------------------------------------------------- #
def build_versions(
    *,
    layout: str,
    selected: list[DumpStatement],
    seeds: list[SeedResult],
    baseline_name: str,
    source_engine: str,
    manual_buckets: list[dict] | None = None,
) -> list[VersionPlan]:
    """Produce la lista ordenada de versiones a materializar según el layout."""
    if layout == "manual":
        return _build_manual(selected, seeds, manual_buckets or [], source_engine)

    plans: list[VersionPlan] = []
    if layout == "by_class":
        for types, gname in _BY_CLASS_GROUPS:
            group = [s for s in selected if s.object_type in types]
            if group:
                # La primera versión (tablas) toma el nombre base del baseline.
                label = baseline_name if types is _BY_CLASS_GROUPS[0][0] else gname
                plans.append(_schema_version(label, group, source_engine=source_engine))
    else:  # single
        if selected:
            plans.append(_schema_version(baseline_name, selected, source_engine=source_engine))

    # Datos SIEMPRE al final, una versión por tabla.
    for seed in seeds:
        plans.append(_data_version(seed))
    return plans


def _build_manual(
    selected: list[DumpStatement],
    seeds: list[SeedResult],
    buckets: list[dict],
    source_engine: str,
) -> list[VersionPlan]:
    """Materializa los buckets manuales (ya validados) en orden."""
    seed_by_table = {s.table: s for s in seeds}
    # Índice (type,name) -> lista de statements (soporta rutinas homónimas en PG).
    by_key: dict[tuple[str, str], list[DumpStatement]] = {}
    for s in selected:
        by_key.setdefault((s.object_type, s.name), []).append(s)

    plans: list[VersionPlan] = []
    for i, bucket in enumerate(buckets, start=1):
        data_tables = bucket.get("data_tables") or []
        objects = bucket.get("objects") or []
        if data_tables:
            for table in data_tables:
                seed = seed_by_table.get(table)
                if seed is not None:
                    plans.append(
                        _data_version(seed, name=(bucket.get("name") or f"Datos: {table}"))
                    )
        else:
            stmts: list[DumpStatement] = []
            for ref in objects:
                stmts.extend(by_key.get((ref["object_type"], ref["name"]), []))
            plans.append(
                _schema_version(
                    bucket.get("name") or f"Versión {i}", stmts, source_engine=source_engine
                )
            )
    return plans


# --------------------------------------------------------------------------- #
# Validación topológica del split manual                                       #
# --------------------------------------------------------------------------- #
def _violation(obj: str, obj_type: str, bucket: int, reason: str, **extra) -> dict:
    v = {"object": obj, "object_type": obj_type, "version": bucket, "reason": reason}
    v.update(extra)
    return v


def _hint(v: dict) -> str:
    """
    Mensaje legible en español para una violación, con los datos concretos ya
    sustituidos (objeto, versión, etc.). Se adjunta a cada violación como ``hint`` y
    también alimenta el ``message`` del 422 (ver ``create_from_snapshot``), que SÍ se
    devuelve en todos los entornos (a diferencia de ``context``, solo en desarrollo).
    """
    reason = v["reason"]
    obj, otype, ver = v.get("object") or "", v.get("object_type") or "", v.get("version")
    if reason == "mixed_schema_and_data":
        return (
            f"La versión {ver} mezcla objetos de estructura y tablas de datos; cada "
            "versión debe ser SOLO de estructura o SOLO de datos."
        )
    if reason == "empty_bucket":
        return f"La versión {ver} está vacía; agrégale objetos o tablas de datos, o quítala de manual_layout."
    if reason == "duplicate_assignment":
        return (
            f"El objeto '{obj}' ({otype}) está asignado a la versión {ver} y también a la "
            f"{v.get('also_in_version')}; déjalo en una sola versión."
        )
    if reason == "duplicate_data_assignment":
        return (
            f"La tabla de datos '{obj}' está asignada a la versión {ver} y también a la "
            f"{v.get('also_in_version')}; déjala en una sola versión."
        )
    if reason == "unassigned_object":
        return (
            f"El objeto '{obj}' ({otype}) fue seleccionado (include/exclude) pero no se "
            "asignó a ninguna versión de manual_layout."
        )
    if reason == "unknown_object":
        return (
            f"La versión {ver} incluye el objeto '{obj}' ({otype}), que no forma parte de "
            "la selección actual (revisa include_object_types/include_objects/exclude_objects)."
        )
    if reason == "unassigned_data_table":
        return (
            f"Se pidieron datos de la tabla '{obj}' (en 'data_tables'), pero ninguna versión "
            "de 'manual_layout' la declara en su lista 'data_tables'. Agrega un bucket con "
            f"\"data_tables\": [\"{obj}\"] (normalmente al final, después de crear la tabla)."
        )
    if reason == "unknown_data_table":
        return (
            f"La versión {ver} declara datos de la tabla '{obj}', que no fue solicitada en "
            "'data_tables'. Agrégala ahí o quita la referencia de este bucket."
        )
    if reason == "dependency_in_later_version":
        return (
            f"La tabla '{obj}' está en la versión {ver}, pero depende de la tabla "
            f"'{v.get('depends_on')}', que está en la versión {v.get('dependency_version')} "
            f"(posterior). Mueve '{v.get('depends_on')}' a la versión {ver} o antes."
        )
    if reason == "prerequisite_after_a_table":
        return (
            f"El objeto '{obj}' ({otype}) está en la versión {ver}, pero debe ir en la "
            f"versión {v.get('must_be_at_most')} o antes (los prerequisitos van antes de "
            "cualquier tabla)."
        )
    if reason == "must_be_after_all_tables":
        return (
            f"El objeto '{obj}' ({otype}) está en la versión {ver}, pero debe ir en la "
            f"versión {v.get('must_be_at_least')} o después (después de TODAS las tablas)."
        )
    if reason == "schema_after_data":
        return (
            f"La versión {ver} es de estructura, pero hay una versión de datos anterior "
            f"(la {v.get('first_data_version')}). Las versiones de datos deben ir siempre al final."
        )
    if reason == "data_table_structure_not_included":
        return (
            f"Se pidieron datos de la tabla '{obj}', pero su estructura (CREATE TABLE) no "
            "está incluida en ninguna versión del blueprint."
        )
    if reason == "data_before_table_structure":
        return (
            f"Los datos de '{obj}' están en la versión {ver}, pero la estructura de esa "
            f"tabla está en la versión {v.get('table_structure_version')} (igual o "
            "posterior). Los datos deben ir DESPUÉS de crear la tabla."
        )
    return f"Revisa la asignación de '{obj}' en manual_layout (versión {ver})."


def validate_manual_layout(
    selected: list[DumpStatement],
    seed_by_table: dict[str, SeedResult],
    buckets: list[dict],
    *,
    skipped_data_tables: set[str] | None = None,
) -> list[dict]:
    """
    Valida que un layout manual sea aplicable (forward-only) y devuelve TODAS las
    violaciones. Vacío => válido. Los números de ``version`` en las violaciones son
    1-based (posición del bucket). Cada violación incluye ``hint``: mensaje legible en
    español con los datos ya sustituidos (para que el frontend lo muestre tal cual sin
    mantener su propia tabla de traducción). Reglas:

    1. Cada bucket es de esquema XOR de datos (no mezcla), y no vacío.
    2. Cobertura: todo objeto/tabla seleccionado va en exactamente un bucket; sin
       referencias a objetos no seleccionados ni duplicados.
    3. Dependencias explícitas (FK, trigger→tabla, índice→tabla): la dependencia no
       puede estar en un bucket POSTERIOR.
    4. Prerequisitos (extension/type/sequence): en un bucket <= el de CUALQUIER tabla.
    5. Vistas/matviews/rutinas: en un bucket >= el de TODAS las tablas (conservador).
    6. Datos: los buckets de datos van DESPUÉS de todos los de esquema; la estructura de
       la tabla sembrada debe estar en un bucket ANTERIOR.

    ``skipped_data_tables``: nombres de tablas pedidas en ``data_tables`` que NO llegaron a
    extraerse (sin filas, sin PK, tipo no soportado, oversize…). Referenciarlas en un bucket
    NO es una violación (la tabla simplemente no genera versión — se reporta en
    ``skipped_tables`` de la respuesta, igual que en ``single``/``by_class``); solo es
    violación (``unknown_data_table``) si el bucket referencia una tabla que NUNCA se pidió.
    """
    violations: list[dict] = []

    # --- Clasificar buckets y detectar mezcla/vacío --------------------------- #
    bucket_kinds: list[str] = []
    for i, bucket in enumerate(buckets, start=1):
        has_obj = bool(bucket.get("objects"))
        has_data = bool(bucket.get("data_tables"))
        if has_obj and has_data:
            violations.append(_violation("", "", i, "mixed_schema_and_data"))
            bucket_kinds.append("mixed")
        elif not has_obj and not has_data:
            violations.append(_violation("", "", i, "empty_bucket"))
            bucket_kinds.append("empty")
        else:
            bucket_kinds.append("data" if has_data else "schema")

    # --- Mapear objeto/tabla -> bucket(s) y detectar duplicados --------------- #
    obj_bucket: dict[tuple[str, str], int] = {}
    for i, bucket in enumerate(buckets, start=1):
        for ref in bucket.get("objects") or []:
            key = (ref["object_type"], ref["name"])
            if key in obj_bucket:
                violations.append(
                    _violation(ref["name"], ref["object_type"], i, "duplicate_assignment",
                               also_in_version=obj_bucket[key])
                )
            else:
                obj_bucket[key] = i

    data_bucket: dict[str, int] = {}
    for i, bucket in enumerate(buckets, start=1):
        for table in bucket.get("data_tables") or []:
            if table in data_bucket:
                violations.append(
                    _violation(table, "data", i, "duplicate_data_assignment",
                               also_in_version=data_bucket[table])
                )
            else:
                data_bucket[table] = i

    # --- Conjuntos seleccionados --------------------------------------------- #
    selected_keys = {(s.object_type, s.name) for s in selected}
    table_names = {s.name for s in selected if s.object_type == "table"}
    table_buckets = [obj_bucket[("table", t)] for t in table_names if ("table", t) in obj_bucket]
    max_table_bucket = max(table_buckets) if table_buckets else None
    min_table_bucket = min(table_buckets) if table_buckets else None

    # Cobertura: seleccionados sin asignar / asignados no seleccionados.
    for key in selected_keys:
        if key not in obj_bucket:
            violations.append(_violation(key[1], key[0], 0, "unassigned_object"))
    for key, i in obj_bucket.items():
        if key not in selected_keys:
            violations.append(_violation(key[1], key[0], i, "unknown_object"))

    skipped_names = skipped_data_tables or set()
    for table in seed_by_table:
        if table not in data_bucket:
            violations.append(_violation(table, "data", 0, "unassigned_data_table"))
    for table, i in data_bucket.items():
        if table not in seed_by_table and table not in skipped_names:
            # Referencia a una tabla que NUNCA se pidió en 'data_tables' (typo/omisión real);
            # si se pidió pero se omitió por un guardrail, NO es violación (ver docstring).
            violations.append(_violation(table, "data", i, "unknown_data_table"))

    # --- Dependencias explícitas --------------------------------------------- #
    for s in selected:
        key = (s.object_type, s.name)
        my_bucket = obj_bucket.get(key)
        if my_bucket is None:
            continue
        for dep in s.depends_on:
            dep_bucket = obj_bucket.get(("table", dep))
            if dep_bucket is not None and dep_bucket > my_bucket:
                violations.append(
                    _violation(s.name, s.object_type, my_bucket, "dependency_in_later_version",
                               depends_on=dep, dependency_version=dep_bucket)
                )

    # --- Prerequisitos antes de todas las tablas ----------------------------- #
    if min_table_bucket is not None:
        for s in selected:
            if s.object_type in _PREREQ_TYPES:
                b = obj_bucket.get((s.object_type, s.name))
                if b is not None and b > min_table_bucket:
                    violations.append(
                        _violation(s.name, s.object_type, b, "prerequisite_after_a_table",
                                   must_be_at_most=min_table_bucket)
                    )

    # --- Vistas/matviews/rutinas después de todas las tablas ----------------- #
    if max_table_bucket is not None:
        for s in selected:
            if s.object_type in _AFTER_ALL_TABLES_TYPES:
                b = obj_bucket.get((s.object_type, s.name))
                if b is not None and b < max_table_bucket:
                    violations.append(
                        _violation(s.name, s.object_type, b, "must_be_after_all_tables",
                                   must_be_at_least=max_table_bucket)
                    )

    # --- Datos al final + estructura de la tabla antes ----------------------- #
    schema_bucket_indices = [i for i, k in enumerate(bucket_kinds, start=1) if k == "schema"]
    data_bucket_indices = [i for i, k in enumerate(bucket_kinds, start=1) if k == "data"]
    if data_bucket_indices and schema_bucket_indices:
        first_data = min(data_bucket_indices)
        for si in schema_bucket_indices:
            if si > first_data:
                violations.append(_violation("", "schema", si, "schema_after_data",
                                              first_data_version=first_data))
    for table, i in data_bucket.items():
        tbucket = obj_bucket.get(("table", table))
        if tbucket is None:
            violations.append(_violation(table, "data", i, "data_table_structure_not_included"))
        elif tbucket >= i:
            violations.append(_violation(table, "data", i, "data_before_table_structure",
                                         table_structure_version=tbucket))

    for v in violations:
        v["hint"] = _hint(v)
    return violations
