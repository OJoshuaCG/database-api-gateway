"""
``search_schema``: busca ESTRUCTURA (nombres y comentarios) en una base cuyo nombre exacto el
agente no conoce. Nunca filas, nunca valores.

MISMA PUERTA QUE ``get_schema``, SIN ATAJOS
-------------------------------------------
Pasa por ``ctx.open_readonly``: gate de ocho ejes, credencial de SOLO LECTURA verificada y
sesión ``READ ONLY`` con ``MCP_SESSION_MAX_SECONDS``. Una base que el token no ve responde
``mcp.not_found`` igual que en las demás tools. No se agregó ningún método al façade ni ningún
privilegio al motor: se usan ``object_index()`` y ``table_schemas()``, que ya existen, y los
grants de solo lectura (``SELECT``, ``SHOW VIEW``, ``TRIGGER``, ``EVENT``) ya alcanzan.

LA CONSULTA NO TOCA EL MOTOR
----------------------------
Se compara en proceso (``app/mcp/search_matcher.py``). No hay ``LIKE`` ni parámetro: ``%`` o
``'`` en ``query`` son texto inerte, y no existe camino por el que el agente aporte SQL.

COSTO ACOTADO, Y LO ACOTADO SE DICE
-----------------------------------
Nombres de tablas, vistas, rutinas y triggers salen del índice (barato) y se buscan SIEMPRE.
Columnas y comentarios de tabla exigen leer el detalle de cada tabla (una lectura de catálogo
por tabla): se leen hasta ``MCP_SEARCH_MAX_TABLES`` tablas, las de nombre afín a la consulta
primero, y dentro de la mitad de ``MCP_SESSION_MAX_SECONDS`` — un presupuesto propio y menor que
el de la sesión para poder devolver lo leído en vez de perderlo en un ``mcp.session_timeout``.
Lo que quede sin leer NO se oculta: ``truncated`` + ``truncated_reasons`` + un warning. No hay
caché: ni la versión de ``check_freshness`` ni nada del inventario prueba que los comentarios no
cambiaron, y servir estructura vieja a un agente es peor que pagar la lectura.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.environments import MCP_SEARCH_MAX_TABLES, MCP_SESSION_MAX_SECONDS
from app.mcp import search_matcher as sm
from app.mcp.context import ToolContext
from app.mcp.tools._envelope import Tracker, clean
from app.mcp.tools.catalog import _database_id, _envelope, _invalid, _warnings
from app.schemas import mcp as out
from app.services import mcp_catalog as codes

SEARCH_KINDS: tuple[str, ...] = ("table", "view", "column", "routine", "trigger")
#: Por defecto, lo que un agente busca cuando no sabe un nombre. Rutinas y triggers son opt-in:
#: solo hay nombre (el índice no trae más) y ensucian la búsqueda de tablas y columnas.
DEFAULT_KINDS: tuple[str, ...] = ("table", "view", "column")
QUERY_MIN = 2
QUERY_MAX = 100
LIMIT_DEFAULT = 20
LIMIT_MAX = 50
#: Cada resultado lleva a lo sumo esto de comentario; el resto se corta y se marca en
#: ``clipped_fields``. 50 resultados * ~200 caracteres deja la respuesta muy por debajo del
#: presupuesto de bytes del dispatcher.
COMMENT_MAX_CHARS = 200
#: Tablas por lote de lectura: el presupuesto de tiempo se mira entre lotes.
_BATCH = 10
_SOFT_BUDGET_SECONDS = MCP_SESSION_MAX_SECONDS * 0.5

_monotonic = time.monotonic


@dataclass(slots=True)
class _Hit:
    kind: str
    name: str
    table: str | None
    column: str | None
    data_type: str | None
    key_flags: list[str]
    references: str | None
    comment: str | None
    match: sm.Match


def _query(params: dict) -> sm.ParsedQuery:
    crudo = params.get("query")
    if not isinstance(crudo, str):
        raise _invalid("'query' es obligatorio: una cadena de texto.")
    limpio = crudo.strip()
    if not limpio or not (QUERY_MIN <= len(limpio) <= QUERY_MAX):
        raise _invalid(f"'query' tiene que tener entre {QUERY_MIN} y {QUERY_MAX} caracteres.")
    parsed = sm.parse_query(limpio)
    if parsed is None:
        raise _invalid("'query' tiene que incluir al menos una letra o un número.")
    return parsed


def _search_kinds(params: dict) -> tuple[str, ...]:
    pedidos = params.get("kinds")
    if pedidos is None:
        return DEFAULT_KINDS
    if not isinstance(pedidos, list) or not pedidos:
        raise _invalid("'kinds' tiene que ser una lista no vacía.")
    if any(not isinstance(k, str) or k not in SEARCH_KINDS for k in pedidos):
        raise _invalid(f"'kinds' admite solo {list(SEARCH_KINDS)}.")
    return tuple(k for k in SEARCH_KINDS if k in pedidos)


def _limit(params: dict) -> int:
    valor = params.get("limit", LIMIT_DEFAULT)
    if not isinstance(valor, int) or isinstance(valor, bool) or not (1 <= valor <= LIMIT_MAX):
        raise _invalid(f"'limit' tiene que ser un entero entre 1 y {LIMIT_MAX}.")
    return valor


def _scan_order(q: sm.ParsedQuery, tablas: list[str]) -> list[str]:
    """Las tablas de nombre afín primero, después el resto; cada grupo alfabético y estable."""
    afines = [t for t in tablas if sm.score_entry(q, name=t) is not None]
    marcadas = set(afines)
    return sorted(afines) + sorted(t for t in tablas if t not in marcadas)


def _column_flags(t) -> tuple[dict[str, list[str]], dict[str, str]]:
    """``{columna: [flags]}`` y ``{columna: 'tabla.columna'}`` (FK) de una tabla."""
    flags: dict[str, list[str]] = {}

    def _add(col: str, flag: str) -> None:
        if flag not in flags.setdefault(col, []):
            flags[col].append(flag)

    for c in t.columns:
        if c.primary_key:
            _add(c.name, "primary_key")
    refs: dict[str, str] = {}
    for fk in t.foreign_keys:
        for i, col in enumerate(fk.columns):
            _add(col, "foreign_key")
            if i < len(fk.referred_columns):
                refs[col] = f"{fk.referred_table}.{fk.referred_columns[i]}"
    for uc in t.unique_constraints:
        if len(uc.columns) == 1:
            _add(uc.columns[0], "unique")
    for ix in t.indexes:
        if ix.unique and len(ix.columns) == 1 and not ix.expressions:
            _add(ix.columns[0], "unique")
    return flags, refs


def _hits_of_table(q: sm.ParsedQuery, t, kinds: tuple[str, ...]) -> list[_Hit]:
    hits: list[_Hit] = []
    if "table" in kinds:
        m = sm.score_entry(q, name=t.table, comment=t.comment)
        if m is not None:
            hits.append(_Hit("table", t.table, t.table, None, None, [], None, t.comment, m))
    if "column" in kinds:
        flags, refs = _column_flags(t)
        for c in t.columns:
            m = sm.score_entry(q, name=c.name, parent=t.table, comment=c.comment, is_column=True)
            if m is not None:
                hits.append(
                    _Hit(
                        "column",
                        c.name,
                        t.table,
                        c.name,
                        c.type,
                        flags.get(c.name, []),
                        refs.get(c.name),
                        c.comment,
                        m,
                    )
                )
    return hits


def _name_only_hits(q: sm.ParsedQuery, kind: str, nombres, *, table_of_self: bool) -> list[_Hit]:
    hits: list[_Hit] = []
    for n in nombres:
        m = sm.score_entry(q, name=n)
        if m is not None:
            hits.append(
                _Hit(kind, n, n if table_of_self else None, None, None, [], None, None, m)
            )
    return hits


def _map_hit(h: _Hit, i: int, tracker: Tracker) -> out.SearchHitOut:
    comentario = clean(h.comment)
    ruta = f"data.hits[{i}].comment"
    if comentario and len(comentario) > COMMENT_MAX_CHARS:
        comentario = comentario[:COMMENT_MAX_CHARS]
        tracker.clipped.append(ruta)
    # ``free_text`` es la MISMA puerta que usa get_schema: sanea y anota en ``untrusted_fields``.
    referencia_kind = "table" if h.kind in ("table", "column") else h.kind
    referencia_name = h.table if h.kind == "column" else h.name
    return out.SearchHitOut(
        kind=h.kind,
        name=clean(h.name),
        table=clean(h.table),
        column=clean(h.column),
        data_type=clean(h.data_type),
        key_flags=list(h.key_flags),
        references=clean(h.references),
        comment=tracker.free_text(comentario, ruta),
        score=h.match.score,
        matched_on=h.match.matched_on,
        matched_tokens=list(h.match.tokens),
        get_schema_object=out.ObjectRefOut(kind=referencia_kind, name=clean(referencia_name)),
    )


def search_schema(ctx: ToolContext, params: dict) -> dict:
    """
    Busca tablas, vistas, columnas (y opcionalmente rutinas y triggers) por nombre y comentario.

    Valida todo ANTES de abrir la conexión. Los resultados salen ordenados por puntaje y con un
    desempate estable; ``total_matches`` cuenta todas las coincidencias aunque ``limit`` recorte.
    Una tabla de la que no se leyó el detalle (tope o tiempo) igual se busca por NOMBRE.
    """
    from app.controllers.target_resolution import exclude_internal_tables

    database_id = _database_id(params)
    q = _query(params)
    kinds = _search_kinds(params)
    limit = _limit(params)
    quiere_detalle = "table" in kinds or "column" in kinds

    reasons: list[str] = []
    hits: list[_Hit] = []
    escaneadas = 0

    with ctx.open_readonly(database_id) as (resuelta, facade):
        inicio = _monotonic()
        indice = facade.object_index()
        tablas = exclude_internal_tables(indice.get("table", []))
        warnings = _warnings(resuelta, facade, bodies_requested=False)

        orden = _scan_order(q, tablas) if quiere_detalle else []
        a_leer = orden[:MCP_SEARCH_MAX_TABLES]
        if len(orden) > len(a_leer):
            reasons.append("scan_cap")
        leidas: set[str] = set()
        for i in range(0, len(a_leer), _BATCH):
            if _monotonic() - inicio >= _SOFT_BUDGET_SECONDS:
                reasons.append("time_budget")
                break
            for t in facade.table_schemas(a_leer[i : i + _BATCH]):
                leidas.add(t.table)
                hits += _hits_of_table(q, t, kinds)
        escaneadas = len(leidas)

        # Lo que el detalle no cubrió se busca igual por nombre, para no perder la tabla que se
        # llama como la consulta solo porque quedó fuera del escaneo.
        if "table" in kinds:
            hits += _name_only_hits(
                q, "table", [t for t in tablas if t not in leidas], table_of_self=True
            )
        if "view" in kinds:
            hits += _name_only_hits(q, "view", indice.get("view", []), table_of_self=True)
        if "routine" in kinds:
            hits += _name_only_hits(q, "routine", indice.get("routine", []), table_of_self=False)
        if "trigger" in kinds:
            hits += _name_only_hits(q, "trigger", indice.get("trigger", []), table_of_self=False)

    hits.sort(key=lambda h: sm.sort_key(h.match.score, h.name, h.kind, h.table, h.column))
    total = len(hits)
    if total > limit:
        reasons.append("results_limit")
    elegidos = hits[:limit]

    if "scan_cap" in reasons or "time_budget" in reasons:
        warnings.append(
            out.WarningOut(
                code=codes.WARN_SEARCH_SCAN_TRUNCATED,
                message=(
                    f"Se leyó el detalle de {escaneadas} de {len(tablas)} tablas: las demás se "
                    "buscaron solo por nombre. Acotá la consulta o usá list_objects con "
                    "'name_prefix'."
                ),
            )
        )
    if "results_limit" in reasons:
        warnings.append(
            out.WarningOut(
                code=codes.WARN_SEARCH_RESULTS_TRUNCATED,
                message=f"Hay {total} coincidencias y se devuelven las {len(elegidos)} mejores.",
            )
        )

    tracker = Tracker()
    data = out.SchemaSearchOut(
        query_tokens=list(q.tokens),
        hits=[_map_hit(h, i, tracker) for i, h in enumerate(elegidos)],
        count=len(elegidos),
        total_matches=total,
        truncated=bool(reasons),
        truncated_reasons=reasons,
        scanned_tables=escaneadas,
        total_tables=len(tablas),
        searched_kinds=list(kinds),
        next_step=out.SEARCH_NEXT_STEP,
    )
    return _envelope(data, resuelta=resuelta, tracker=tracker, warnings=warnings)
