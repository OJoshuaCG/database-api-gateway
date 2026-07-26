"""
Integridad de un PLAN de sentencias derivado de un diff estructural — 100% puro
(sin motor, sin ORM, sin sesión), igual que ``schema_diff.py``.

Resuelve dos problemas distintos que causaban fallos de ejecución REALES:

1. **Selección parcial sin cierre de dependencias.** El admin elige un subconjunto de
   ítems de una comparación (``POST /schema-comparisons/{id}/adopt`` o
   ``.../execute`` con ``mode=custom``) y el gateway lo aceptaba tal cual. Elegir "la
   vista" sin "la tabla que la vista lee", o el ``ADD`` de un índice redefinido sin su
   ``DROP`` previo, produce un error garantizado del motor a mitad de la migración.
   ``expand_selection``/``check_closure`` lo resuelven con el grafo ``depends_on`` que
   ya calcula ``schema_diff.build_dependency_graph``.

2. **Grupos atómicos partidos.** Un solo cambio lógico puede rendear VARIAS sentencias
   (un índice/UNIQUE/CHECK/FK redefinido = ``DROP`` + ``CREATE``; un ``PRIMARY KEY``
   cambiado = ``DROP`` + ``ADD``). Todas comparten ``op_group``: seleccionar una sin las
   otras deja el objeto duplicado (``1061 Duplicate key name``) o la tabla con dos PKs
   (``1068 Multiple primary key defined``). La unidad de selección es el GRUPO.

Y agrega una tercera barrera, la más importante para no repetir la clase de bug que
motivó este módulo: ``validate_statement_plan`` verifica el INVARIANTE de orden del plan
ya ensamblado (toda dependencia aparece antes que su dependiente). Si el ordenador
regresa alguna vez, la creación de la versión de blueprint FALLA en el gateway en vez de
escribir un ``up_sql`` que reventará contra el motor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Tipos de objeto cuya creación es "una por nombre": dos ítems que crean el mismo objeto
# en el mismo plan son un error de construcción, no una coincidencia.
_UNIQUE_CREATION_TYPES = frozenset(
    {"table", "view", "materialized_view", "routine", "trigger", "event", "sequence",
     "enum_type", "extension", "index", "unique_constraint", "check_constraint",
     "foreign_key", "column", "primary_key"}
)


@dataclass(frozen=True)
class PlanItem:
    """
    Vista plana de UNA sentencia del plan (persistida o en memoria).

    ``op_group`` agrupa las sentencias del mismo cambio lógico; ``depends_on`` lista los
    ``op_group`` que deben ejecutarse ANTES. Ambos los calcula el motor de diff.
    """

    id: int
    seq: int
    op_group: str
    depends_on: tuple[str, ...] = ()
    object_type: str = ""
    object_name: str = ""
    change_type: str = ""
    has_down_sql: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class PlanFinding:
    """Un hallazgo del linter. ``blocking=True`` => no se debe ejecutar el plan."""

    code: str
    message: str
    blocking: bool
    op_group: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ClosureResult:
    """Resultado de cerrar una selección sobre el grafo de dependencias."""

    selected_groups: tuple[str, ...]      # grupos finales (pedidos + agregados)
    added_groups: tuple[str, ...]         # los que hubo que agregar para que cierre
    item_ids: tuple[int, ...]             # ids de sentencia, en orden de ejecución
    added_item_ids: tuple[int, ...]


def _by_group(items: list[PlanItem]) -> dict[str, list[PlanItem]]:
    out: dict[str, list[PlanItem]] = {}
    for it in items:
        out.setdefault(it.op_group, []).append(it)
    for group in out.values():
        group.sort(key=lambda i: i.seq)
    return out


def groups_of(items: list[PlanItem], item_ids: list[int]) -> list[str]:
    """``op_group`` (sin duplicar, en orden de ejecución) de los ítems indicados."""
    wanted = set(item_ids)
    seen: list[str] = []
    for it in sorted(items, key=lambda i: i.seq):
        if it.id in wanted and it.op_group not in seen:
            seen.append(it.op_group)
    return seen


def expand_selection(items: list[PlanItem], item_ids: list[int]) -> ClosureResult:
    """
    Cierra una selección: agrega TODA sentencia necesaria para que lo elegido pueda
    ejecutarse, y completa los grupos atómicos partidos.

    Dos expansiones, ambas obligatorias:
      - **grupo completo**: elegir una sentencia arrastra a las demás de su ``op_group``;
      - **cierre transitivo**: elegir un grupo arrastra sus ``depends_on``, y los de esos.

    El resultado sale SIEMPRE en orden de ejecución (``seq``), nunca en el orden en que
    el cliente mandó los ids: el orden del plan es del gateway, no del cliente.
    """
    by_group = _by_group(items)
    requested = set(groups_of(items, item_ids))
    selected = set(requested)
    stack = list(requested)
    while stack:
        group = stack.pop()
        for it in by_group.get(group, []):
            for dep in it.depends_on:
                if dep in by_group and dep not in selected:
                    selected.add(dep)
                    stack.append(dep)
    ordered_ids = [it.id for it in sorted(items, key=lambda i: i.seq) if it.op_group in selected]
    explicit = set(item_ids)
    return ClosureResult(
        selected_groups=tuple(
            g for g in _groups_in_order(items) if g in selected
        ),
        added_groups=tuple(
            g for g in _groups_in_order(items) if g in selected and g not in requested
        ),
        item_ids=tuple(ordered_ids),
        added_item_ids=tuple(i for i in ordered_ids if i not in explicit),
    )


def _groups_in_order(items: list[PlanItem]) -> list[str]:
    out: list[str] = []
    for it in sorted(items, key=lambda i: i.seq):
        if it.op_group not in out:
            out.append(it.op_group)
    return out


def check_closure(items: list[PlanItem], item_ids: list[int]) -> dict[str, list[str]]:
    """
    Dependencias FALTANTES de una selección: ``{op_group elegido: [op_groups que faltan]}``.
    Vacío => la selección es ejecutable en cuanto al orden/dependencias.

    Incluye los grupos atómicos partidos: si se eligió una sentencia de un grupo pero no
    todas, el grupo se reporta como dependiente de sí mismo (partido).
    """
    by_group = _by_group(items)
    chosen_ids = set(item_ids)
    chosen_groups = set(groups_of(items, item_ids))
    missing: dict[str, list[str]] = {}
    for group in chosen_groups:
        gaps: list[str] = []
        members = by_group.get(group, [])
        if any(m.id not in chosen_ids for m in members):
            gaps.append(group)  # grupo atómico incompleto
        for it in members:
            for dep in it.depends_on:
                if dep in by_group and dep not in chosen_groups and dep not in gaps:
                    gaps.append(dep)
        if gaps:
            missing[group] = sorted(set(gaps))
    return missing


def prune_unsatisfied(
    items: list[PlanItem], item_ids: list[int]
) -> tuple[list[int], list[str]]:
    """
    Quita (transitivamente) todo lo que quedó sin sus dependencias, en vez de fallar.

    Es la política correcta para los modos AUTOMÁTICOS (``all``,
    ``all_except_destructive``): el filtro por riesgo puede dejar fuera una dependencia
    y arrastrar consigo a sus dependientes. Caso real: una tabla nueva marcada
    ``destructive`` por la heurística ``possible_rename_of`` queda excluida de
    ``all_except_destructive``, pero sus índices/FKs (no destructivos) NO — y
    ``CREATE INDEX`` sobre una tabla inexistente aborta la migración. Para una selección
    EXPLÍCITA (``custom``/adopt) se usa ``check_closure`` y se falla con 422: ahí el
    admin dijo exactamente qué quería y hay que decírselo, no recortarlo en silencio.
    """
    by_group = _by_group(items)
    keep = set(groups_of(items, item_ids))
    removed: list[str] = []
    changed = True
    while changed:
        changed = False
        for group in sorted(keep):
            deps = {
                dep
                for it in by_group.get(group, [])
                for dep in it.depends_on
                if dep in by_group
            }
            if not deps <= keep:
                keep.discard(group)
                removed.append(group)
                changed = True
    kept_ids = [it.id for it in sorted(items, key=lambda i: i.seq) if it.op_group in keep]
    return kept_ids, sorted(set(removed))


def validate_statement_plan(items: list[PlanItem]) -> list[PlanFinding]:
    """
    Verifica los INVARIANTES de un plan ya ordenado y cerrado, justo antes de
    materializarlo como versión de blueprint o de ejecutarlo ad-hoc.

    Comprobaciones (``blocking`` = el plan fallaría casi con certeza):

    - ``dependency_out_of_order`` (bloqueante): una dependencia aparece DESPUÉS de su
      dependiente. Es el invariante que se rompía en todos los bugs de orden conocidos
      (vista antes que su tabla, CHECK antes que su columna, FK antes que la UNIQUE que
      necesita, DROP COLUMN antes del DROP de su FK). Barrera de último recurso: si el
      ordenador regresa, se falla acá y no contra el motor.
    - ``atomic_group_not_contiguous`` (bloqueante): las sentencias de un grupo quedaron
      intercaladas con otras (el ``DROP``/``CREATE`` de una redefinición debe ir junto).
    - ``duplicate_creation`` (bloqueante): dos ítems crean el MISMO objeto.
    - ``create_and_drop_same_object`` (informativo): el mismo objeto se crea y se borra
      en el mismo plan — casi siempre un rename detectado como par suelto.
    - ``destructive_without_rollback`` (informativo): sentencia destructiva sin reverso;
      la versión no podrá revertirse automáticamente.
    """
    findings: list[PlanFinding] = []
    ordered = sorted(items, key=lambda i: i.seq)
    if not ordered:
        return findings

    present_groups = {it.op_group for it in ordered}
    first_pos: dict[str, int] = {}
    last_pos: dict[str, int] = {}
    for pos, it in enumerate(ordered):
        first_pos.setdefault(it.op_group, pos)
        last_pos[it.op_group] = pos

    # --- orden de dependencias ---------------------------------------------- #
    for it in ordered:
        for dep in it.depends_on:
            if dep not in present_groups:
                continue
            if last_pos[dep] > first_pos[it.op_group]:
                findings.append(PlanFinding(
                    code="dependency_out_of_order",
                    message=(
                        f"'{_human(it.op_group)}' se ejecuta antes de su dependencia "
                        f"'{_human(dep)}'. El motor fallaría (objeto inexistente o "
                        "clave sin respaldo)."
                    ),
                    blocking=True, op_group=it.op_group,
                    detail={"depends_on": dep},
                ))

    # --- grupos atómicos ----------------------------------------------------- #
    positions_by_group: dict[str, list[int]] = {}
    for pos, it in enumerate(ordered):
        positions_by_group.setdefault(it.op_group, []).append(pos)
    for group, positions in positions_by_group.items():
        if positions[-1] - positions[0] != len(positions) - 1:
            findings.append(PlanFinding(
                code="atomic_group_not_contiguous",
                message=(
                    f"Las sentencias de '{_human(group)}' quedaron separadas por otras. "
                    "Un cambio que se rendea como DROP+CREATE debe ejecutarse junto."
                ),
                blocking=True, op_group=group,
                detail={"positions": positions},
            ))

    # --- creaciones duplicadas / contradictorias ----------------------------- #
    creations: dict[tuple[str, str], list[str]] = {}
    drops: set[tuple[str, str]] = set()
    for it in ordered:
        if it.object_type not in _UNIQUE_CREATION_TYPES:
            continue
        ident = (it.object_type, it.object_name)
        if it.change_type == "new":
            creations.setdefault(ident, []).append(it.op_group)
        elif it.change_type == "dropped":
            drops.add(ident)
    for ident, groups in creations.items():
        distinct = sorted(set(groups))
        if len(distinct) > 1:
            findings.append(PlanFinding(
                code="duplicate_creation",
                message=(
                    f"El plan crea dos veces {ident[0]} '{ident[1]}' "
                    f"({', '.join(distinct)}). La segunda fallaría con 'ya existe'."
                ),
                blocking=True, op_group=distinct[0], detail={"groups": distinct},
            ))
        if ident in drops:
            findings.append(PlanFinding(
                code="create_and_drop_same_object",
                message=(
                    f"El plan crea Y borra {ident[0]} '{ident[1]}'. Revisa si es un "
                    "rename: el orden importa y el resultado puede no ser el esperado."
                ),
                blocking=False, op_group=distinct[0],
            ))

    # --- reversibilidad ------------------------------------------------------ #
    no_down = sorted({it.op_group for it in ordered if it.destructive and not it.has_down_sql})
    if no_down:
        findings.append(PlanFinding(
            code="destructive_without_rollback",
            message=(
                f"{len(no_down)} cambio(s) destructivo(s) sin reverso: la versión no "
                "podrá revertirse automáticamente."
            ),
            blocking=False, detail={"groups": no_down[:20]},
        ))
    return findings


def _human(op_group: str) -> str:
    """``index|t.ix_x|modified`` -> ``index t.ix_x (modified)`` para mensajes de error."""
    parts = op_group.split("|")
    if len(parts) == 3:
        return f"{parts[0]} {parts[1]} ({parts[2]})"
    return op_group


def blocking(findings: list[PlanFinding]) -> list[PlanFinding]:
    return [f for f in findings if f.blocking]


__all__ = [
    "ClosureResult",
    "PlanFinding",
    "PlanItem",
    "blocking",
    "check_closure",
    "expand_selection",
    "groups_of",
    "prune_unsatisfied",
    "validate_statement_plan",
]
