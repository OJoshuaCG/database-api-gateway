"""
Resolución de dependencias entre objetos de una BD para la clonación selectiva
(módulo PURO: opera sobre un ``SchemaSnapshot`` en memoria, sin motor ni ORM).

La granularidad de selección es por OBJETO de primer nivel: tabla, vista,
materialized_view, rutina, trigger, sequence, enum_type, extension, event. Una tabla
es atómica (arrastra sus columnas/índices/constraints).

Dos clases de aristas de dependencia (decisión de producto, ver plan):

  - AUTORITATIVAS (fiables, modeladas en los DTOs): tabla→tabla vía ``ForeignKeyInfo``
    y trigger→tabla vía ``TriggerInfo.table``. El cierre de estas se AGREGA
    automáticamente a la selección (no se puede clonar un objeto sin ellas).
  - ADVISORY (best-effort, no bloqueante): referencias detectadas por escaneo de nombres
    dentro de los cuerpos de vistas/rutinas/triggers (``definition``/``body``/``action``).
    Los cuerpos NO se parsean semánticamente (sqlglot no es fiable con ``BEGIN…END`` de
    MySQL). Se DEVUELVEN como sugerencias para que la UI las resalte, nunca se agregan
    en silencio. Misma filosofía que ``possible_rename_of`` del diff.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.services.db_admin.dtos import SchemaSnapshot
from app.services.db_admin.schema_diff import _table_dep_order

# Tipos de objeto de primer nivel seleccionables.
OBJECT_TYPES = (
    "table", "view", "materialized_view", "routine", "trigger",
    "sequence", "enum_type", "extension", "event",
)


class ObjectRef(BaseModel):
    """Referencia a un objeto de primer nivel (identidad = tipo + nombre)."""

    object_type: str
    name: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.object_type, self.name)


class DependencyEdge(BaseModel):
    """Una arista de dependencia ``from_ref`` → ``to_ref``."""

    from_type: str
    from_name: str
    to_type: str
    to_name: str
    reason: str  # 'foreign_key' | 'trigger_table' | 'body_reference'
    authoritative: bool


class ClosureResult(BaseModel):
    """
    Resultado de resolver el cierre de dependencias de una selección.

    - ``selected``: lo que pidió el usuario (normalizado, tal cual existe en el snapshot).
    - ``added``: objetos AGREGADOS por el cierre autoritativo (deben incluirse sí o sí).
    - ``closure``: ``selected`` + ``added`` (la selección efectiva final).
    - ``edges``: aristas autoritativas dentro del cierre (para dibujar el grafo).
    - ``advisory``: sugerencias best-effort (objetos referenciados en cuerpos que NO están
      en el cierre) — la UI las resalta, no se agregan solas.
    - ``table_order``: orden topológico (padre antes que hijo) de las tablas del cierre.
    - ``warnings``: avisos (p. ej. objetos seleccionados inexistentes en el snapshot).
    """

    selected: list[ObjectRef] = Field(default_factory=list)
    added: list[ObjectRef] = Field(default_factory=list)
    closure: list[ObjectRef] = Field(default_factory=list)
    edges: list[DependencyEdge] = Field(default_factory=list)
    advisory: list[DependencyEdge] = Field(default_factory=list)
    table_order: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Índice de objetos del snapshot                                              #
# --------------------------------------------------------------------------- #
def _index_objects(snap: SchemaSnapshot) -> dict[tuple[str, str], object]:
    """Mapa ``(object_type, name) -> DTO`` de todos los objetos de primer nivel."""
    idx: dict[tuple[str, str], object] = {}
    for t in snap.tables:
        idx[("table", t.table)] = t
    for v in snap.views:
        idx[("materialized_view" if v.is_materialized else "view", v.name)] = v
    for r in snap.routines:
        idx[("routine", r.name)] = r
    for tg in snap.triggers:
        idx[("trigger", tg.name)] = tg
    for s in snap.sequences:
        idx[("sequence", s.name)] = s
    for e in snap.enum_types:
        idx[("enum_type", e.name)] = e
    for x in snap.extensions:
        idx[("extension", x.name)] = x
    for ev in snap.events:
        idx[("event", ev.name)] = ev
    return idx


def _authoritative_edges(snap: SchemaSnapshot) -> list[DependencyEdge]:
    """FK (tabla→tabla) y trigger→tabla. Solo aristas hacia objetos que existen."""
    tables = {t.table for t in snap.tables}
    edges: list[DependencyEdge] = []
    for t in snap.tables:
        seen: set[str] = set()
        for fk in t.foreign_keys:
            ref = fk.referred_table
            if ref in tables and ref != t.table and ref not in seen:
                seen.add(ref)
                edges.append(DependencyEdge(
                    from_type="table", from_name=t.table,
                    to_type="table", to_name=ref,
                    reason="foreign_key", authoritative=True,
                ))
    for tg in snap.triggers:
        if tg.table in tables:
            edges.append(DependencyEdge(
                from_type="trigger", from_name=tg.name,
                to_type="table", to_name=tg.table,
                reason="trigger_table", authoritative=True,
            ))
    return edges


def _body_of(obj: object, object_type: str) -> str:
    if object_type in ("view", "materialized_view"):
        return getattr(obj, "definition", "") or ""
    if object_type == "routine":
        return getattr(obj, "body", "") or ""
    if object_type == "trigger":
        return getattr(obj, "action", "") or ""
    if object_type == "event":
        return getattr(obj, "body", "") or ""
    return ""


# Objetos cuyo nombre buscamos DENTRO de otros cuerpos (candidatos de referencia).
_ADVISORY_TARGET_TYPES = ("table", "view", "materialized_view", "routine", "sequence")
# Tipos cuyos cuerpos escaneamos.
_ADVISORY_SOURCE_TYPES = ("view", "materialized_view", "routine", "trigger", "event")


def _advisory_edges(
    snap: SchemaSnapshot, idx: dict[tuple[str, str], object]
) -> list[DependencyEdge]:
    """
    Escaneo best-effort: por cada objeto con cuerpo, busca por límite de palabra los
    nombres de OTROS objetos candidatos. No es autoritativo (falsos positivos/negativos
    posibles): sqlglot no parsea de forma fiable estos cuerpos.
    """
    candidates: list[tuple[str, str, re.Pattern]] = []
    for (otype, name), _obj in idx.items():
        if otype in _ADVISORY_TARGET_TYPES and name:
            # \b no funciona bien con nombres que empiezan/terminan en no-alfanum; los
            # nombres SQL válidos son [A-Za-z0-9_$], así que \b sirve. Case-insensitive.
            candidates.append((otype, name, re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)))

    edges: list[DependencyEdge] = []
    for (otype, name), obj in idx.items():
        if otype not in _ADVISORY_SOURCE_TYPES:
            continue
        body = _body_of(obj, otype)
        if not body:
            continue
        for cand_type, cand_name, pat in candidates:
            if cand_type == otype and cand_name == name:
                continue  # no auto-referencia
            if pat.search(body):
                edges.append(DependencyEdge(
                    from_type=otype, from_name=name,
                    to_type=cand_type, to_name=cand_name,
                    reason="body_reference", authoritative=False,
                ))
    return edges


def build_graph(snap: SchemaSnapshot) -> tuple[list[DependencyEdge], list[DependencyEdge]]:
    """Devuelve ``(authoritative_edges, advisory_edges)`` del snapshot completo."""
    idx = _index_objects(snap)
    return _authoritative_edges(snap), _advisory_edges(snap, idx)


def resolve_closure(
    snap: SchemaSnapshot, selected: list[ObjectRef]
) -> ClosureResult:
    """
    Calcula el cierre AUTORITATIVO de la selección + las sugerencias advisory.

    El cierre agrega, transitivamente, las tablas referidas por FK de cualquier tabla
    incluida y la tabla dueña de cualquier trigger incluido. Idempotente y determinista.
    """
    idx = _index_objects(snap)
    auth_edges = _authoritative_edges(snap)
    # Aristas salientes autoritativas por nodo origen.
    out: dict[tuple[str, str], list[DependencyEdge]] = {}
    for e in auth_edges:
        out.setdefault((e.from_type, e.from_name), []).append(e)

    warnings: list[str] = []
    sel_keys: list[tuple[str, str]] = []
    seen_sel: set[tuple[str, str]] = set()
    for ref in selected:
        key = (ref.object_type, ref.name)
        if key not in idx:
            warnings.append(f"objeto seleccionado inexistente en el origen: {ref.object_type}:{ref.name}")
            continue
        if key not in seen_sel:
            seen_sel.add(key)
            sel_keys.append(key)

    # Cierre autoritativo por BFS.
    closure_keys: set[tuple[str, str]] = set(sel_keys)
    used_edges: list[DependencyEdge] = []
    frontier = list(sel_keys)
    while frontier:
        cur = frontier.pop()
        for e in out.get(cur, []):
            tgt = (e.to_type, e.to_name)
            used_edges.append(e)
            if tgt not in closure_keys:
                closure_keys.add(tgt)
                frontier.append(tgt)

    added_keys = [k for k in closure_keys if k not in seen_sel]

    # Advisory: referencias en cuerpos de objetos del cierre hacia objetos FUERA del cierre.
    all_advisory = _advisory_edges(snap, idx)
    advisory: list[DependencyEdge] = [
        e for e in all_advisory
        if (e.from_type, e.from_name) in closure_keys
        and (e.to_type, e.to_name) not in closure_keys
    ]

    # Orden topológico de las tablas del cierre (padre antes que hijo).
    tbl_names = [name for (otype, name) in closure_keys if otype == "table"]
    tables_by_name = {t.table: t for t in snap.tables}
    rank = _table_dep_order(tbl_names, tables_by_name)
    table_order = sorted(tbl_names, key=lambda n: (rank.get(n, 0), n))

    def _refs(keys) -> list[ObjectRef]:
        return [ObjectRef(object_type=ot, name=nm) for (ot, nm) in keys]

    # Aristas del cierre (dedup, solo entre nodos del cierre) para el grafo de la UI.
    edge_seen: set[tuple] = set()
    closure_edges: list[DependencyEdge] = []
    for e in used_edges:
        sig = (e.from_type, e.from_name, e.to_type, e.to_name, e.reason)
        if sig not in edge_seen:
            edge_seen.add(sig)
            closure_edges.append(e)

    return ClosureResult(
        selected=_refs(sel_keys),
        added=_refs(sorted(added_keys)),
        closure=_refs(sorted(closure_keys)),
        edges=closure_edges,
        advisory=advisory,
        table_order=table_order,
        warnings=warnings,
    )
