"""
Writer del artefacto ``sql`` — **generador incremental**, sin motor y sin I/O propio.

Es la pieza que convierte "un snapshot + un spec + una fuente de filas" en el TEXTO del
artefacto. Tres propiedades gobiernan el diseño, y las tres son criterios de aceptación del
diseño (``docs/plans/10-exportacion-de-bases-de-datos.md``), no preferencias de estilo:

1. **Streaming obligatorio (§8.1).** ``iter_sql`` es un generador: nunca materializa el
   artefacto, ni el contenido de una tabla, ni siquiera una sentencia entera de más de
   ``max_statement_bytes``. El consumo de memoria es plano e independiente del tamaño de la
   tabla. El corte de sentencia se manda **por bytes**; ``rows_per_statement`` es solo un
   techo superior, porque una tabla con ``LONGTEXT`` revienta cualquier límite por conteo.

2. **Ni un ``if engine ==`` (§7).** Toda diferencia de dialecto entra por los métodos
   ``export_*`` del adapter. Agregar un cuarto motor tiene que ser "implementar el adapter y
   nada más"; si el writer supiera qué es MySQL, esa promesa sería falsa desde el primer
   parche.

3. **Determinismo (§8.3).** Dos exportaciones del mismo esquema sin cambios producen el
   MISMO artefacto byte a byte (salvo el encabezado, suprimible con
   ``script_comments: false``). Los metadatos volátiles —fecha, id de job— viven en el
   manifiesto, no en el script, para que dos volcados se puedan diffear sin recortarles la
   cabecera. Una tabla sin PK y sin tupla de columnas ordenable sale **sin orden
   garantizado** y se marca ``deterministic: false``: fingir determinismo ahí sería mentir.

El writer **no abre conexiones**. Las filas llegan por un ``RowSource`` (en producción,
``export_session.ExportSession``, que las lee dentro de la transacción de consistencia del
job; en los tests, una fuente en memoria). Esa costura es lo que hace que todo lo de arriba
se pueda verificar sin una base de datos delante.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, NamedTuple, Protocol

from app.core.environments import (
    EXPORT_BATCH_ROWS,
    EXPORT_MAX_STATEMENT_BYTES,
    EXPORT_ROWS_PER_STATEMENT,
)
from app.services.db_admin import export_spec as espec
from app.services.db_admin.dtos import (
    ColumnInfo,
    SchemaSnapshot,
    TableSchema,
)
from app.services.db_admin.schema_diff import RenderedStatement, diff_snapshots
from app.services.db_admin.sql_dialect import strip_self_schema_qualifier
from app.services.db_admin.sql_literals import (
    UnsupportedValueError,
    render_value,
    render_value_text,
)

# Versión del GENERADOR. Va en el encabezado y en el manifiesto: si dos artefactos del mismo
# esquema difieren, lo primero que hay que poder descartar es que los generó otra versión.
# Se sube cuando cambia el TEXTO emitido, aunque el cambio sea cosmético — precisamente
# porque el contrato del §8.3 es la igualdad byte a byte.
GENERATOR_VERSION = "1.0"

# Tipos que se emiten ANTES de las tablas (§8.4, pasos 3-4).
_PREREQUISITE_TYPES = frozenset({"extension", "enum_type", "sequence"})
# Tipos cuyo DDL es un CUERPO (pasos 7-11). El orden ENTRE ellos ya viene resuelto en la
# lista de objetos que congeló el preview (rutinas antes que vistas, §8.4 corregido).
_BODY_TYPES = frozenset({"view", "materialized_view", "routine", "trigger", "event"})
# Sub-objetos de una tabla que ``render_diff`` emite como sentencias aparte. Son los que
# ``constraints_placement`` mueve al final (después de los datos) o deja pegados a su tabla.
_CHILD_TYPES = frozenset(
    {"index", "unique_constraint", "check_constraint", "foreign_key", "primary_key"}
)

_OK = "ok"
_SKIPPED = "skipped"
_ERROR = "error"

# Tamaño objetivo del trozo en los formatos de datos. No es un tope de memoria (una fila
# suelta puede ser mayor): es el grano con el que se le habla al empaquetador, para que un
# volcado de un millón de filas no haga un millón de llamadas a ``write``.
_TEXT_CHUNK_BYTES = 64 * 1024

# Ordinales del nombre de archivo cuando se emite un archivo por objeto. El prefijo NUMÉRICO
# es lo que documenta el ORDEN DE EJECUCIÓN (§10.3): quien descomprime el zip ejecuta los
# archivos en orden alfabético y obtiene exactamente el orden en que el gateway los generó.
# Los bloques dejan hueco de sobra entre fases para que nunca haya que reordenar nada.
_ORD_PROLOGUE = 0
_ORD_OBJECT_BASE = 10_000
_ORD_DATA_BASE = 50_000
_ORD_CONSTRAINTS = 70_000
_ORD_COUNTERS = 80_000
_ORD_EPILOGUE = 90_000


class Chunk(NamedTuple):
    """
    Un trozo de artefacto y a qué archivo LÓGICO pertenece.

    ``entry`` es ``None`` cuando el artefacto es un flujo único (``organization='single'``);
    con un archivo por objeto lleva el nombre del archivo, SIN extensión (la pone el
    empaquetador, que es quien conoce el formato y la compresión).

    ``prologue=True`` marca un trozo que hay que **repetir al principio de cada fragmento**
    cuando el archivo se parte por tamaño: la fila de encabezado de un CSV y la marca de
    orden de bytes. Un ``part02`` sin encabezado es un archivo que ningún importador lee
    igual que el ``part01``, y descubrirlo es tarde.
    """

    entry: str | None
    text: str
    prologue: bool = False


class _Cursor:
    """
    A qué archivo lógico se está escribiendo ahora mismo.

    Es un objeto mutable compartido (y no un parámetro) porque el emisor lo lee en cada
    trozo y el generador lo mueve entre secciones. Con ``organization='single'`` se queda en
    ``None`` de punta a punta y el artefacto sale **byte a byte idéntico** al de F4 — esa
    igualdad es la razón de que el troceado se haya implementado como una etiqueta sobre el
    generador existente y no como un segundo generador.
    """

    __slots__ = ("name",)

    def __init__(self, name: str | None = None):
        self.name = name


# --------------------------------------------------------------------------- #
# Contratos de entrada                                                         #
# --------------------------------------------------------------------------- #


class RowSource(Protocol):
    """
    De dónde salen las filas. La implementación real es ``ExportSession`` (una conexión, una
    transacción, streaming); en los tests es una lista.
    """

    def iter_rows(
        self, select_sql: str, *, batch_rows: int = ...
    ) -> Iterator[Sequence[Any]]: ...


class ExportDialect(Protocol):
    """
    El subconjunto de ``ServerAdapter`` que el writer usa. Existe para documentar la costura:
    todo lo que el writer necesita saber del motor está en esta lista y en ningún otro lado.
    """

    dialect: str

    def render_diff(self, diff) -> list[RenderedStatement]: ...
    def export_scope_ddl(self, database, mode, **kwargs) -> list[str]: ...
    def export_entity_drop(self, object_type, name, **kwargs) -> str: ...
    def export_session_preamble(self, **kwargs) -> list[str]: ...
    def export_session_epilogue(self) -> list[str]: ...
    def export_use_scope(self, database) -> str | None: ...
    def export_counter_reset(self, table, value, **kwargs) -> str | None: ...
    def export_body_wrapper(self, object_type) -> tuple[str, str] | None: ...
    def export_definer_clause(self, sql, **kwargs) -> str: ...
    def export_make_idempotent(self, sql, object_type) -> str | None: ...
    def export_row_order_by(self, table) -> list[str]: ...
    def export_insert_wrapper(self, table, columns, **kwargs) -> tuple[str, str]: ...


# --------------------------------------------------------------------------- #
# Resultado                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class ExportItemStat:
    """
    Lo que pasó con UN objeto. Se persiste como ``export_job_items`` (§12) y alimenta el
    reporte de incidencias del §14.

    ``reason`` es un motivo ACOTADO y de vocabulario cerrado. **Nunca** ``str(exc)`` del
    motor: el mensaje de un driver puede incrustar VALORES de filas (``Duplicate entry
    'alice@x.com'``), y eso convertiría el reporte de un job en una fuga de datos por la
    puerta de atrás (criterio R4, §9.5).
    """

    seq: int
    object_type: str
    object_name: str
    phase: str
    status: str = _OK
    reason: str | None = None
    rows_exported: int | None = None
    bytes_written: int = 0
    deterministic: bool | None = None


@dataclass
class ExportStats:
    """Totales de la corrida + el reporte por objeto + los avisos que hay que mostrar."""

    bytes_written: int = 0
    statements: int = 0
    rows_exported: int = 0
    objects_exported: int = 0
    tables_with_data: int = 0
    items: list[ExportItemStat] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # ¿Todos los objetos salieron ``ok``? El §14 exige que un artefacto parcial NUNCA se
    # entregue sin marca inequívoca, y esta es la señal que la produce.
    complete: bool = True

    def _add(self, item: ExportItemStat) -> ExportItemStat:
        self.items.append(item)
        return item

    def warn(self, message: str) -> None:
        # Los avisos se deduplican: "las particiones no se reproducen" repetido 200 veces
        # (una por tabla) entierra a los demás y no aporta nada.
        if message not in self.warnings:
            self.warnings.append(message)


# --------------------------------------------------------------------------- #
# Saneamiento del snapshot (§7 del prompt / ``sanitize``)                      #
# --------------------------------------------------------------------------- #
# Las opciones de saneamiento se aplican TRANSFORMANDO EL SNAPSHOT antes de renderizar, no
# parcheando el texto del DDL después. Es la diferencia entre una operación bien definida
# sobre datos tipados y una expresión regular sobre SQL generado: lo segundo es cómo se
# rompe un ``COMMENT`` que contiene la palabra ``ENGINE``.
#
# La excepción es ``definer``, que sí es una reescritura de texto: la cláusula vive dentro
# del cuerpo capturado del objeto, que el snapshot guarda como una sola cadena.

# Claves de ``storage_options`` que son OPCIONES DE MOTOR (§7.1) y desaparecen con
# ``engine_specific_options: false``. ``charset``/``collation`` NO están: no son opciones de
# motor sino del juego de caracteres, y las gobierna ``charset_override``.
_ENGINE_OPTION_KEYS = frozenset({"engine", "row_format", "key_block_size", "tablespace"})


def sanitize_snapshot(
    snapshot: SchemaSnapshot,
    spec: espec.ExportSpec,
    *,
    keys: frozenset[tuple[str, str]] | None = None,
) -> SchemaSnapshot:
    """
    Aplica al snapshot el filtro de selección y las opciones de ``sanitize`` que son
    propiedades ESTRUCTURALES (comentarios, opciones de motor, charset).

    Función pura: mismo snapshot + mismo spec ⇒ mismo resultado. Es lo que hace testeable el
    saneamiento sin renderizar una sola línea de SQL.
    """
    san = spec.sanitize
    override = san.charset_override
    forcing = override.mode == espec.CharsetOverrideMode.override

    def _keep(object_type: str, name: str) -> bool:
        return keys is None or (object_type, name) in keys

    def _column(col: ColumnInfo) -> ColumnInfo:
        data = col.model_dump()
        if not san.object_comments:
            data["comment"] = None
        if forcing:
            # Solo se pisa lo que YA tenía valor explícito: forzar un COLLATE en cada
            # columna que lo heredaba de la tabla infla el DDL y, peor, congela una
            # herencia que el destino podría querer resolver a su manera.
            if data.get("charset"):
                data["charset"] = override.charset
            if data.get("collation"):
                data["collation"] = override.collation
        return ColumnInfo(**data)

    def _table(tbl: TableSchema) -> TableSchema:
        data = tbl.model_dump()
        data["columns"] = [_column(c) for c in tbl.columns]
        if not san.object_comments:
            data["comment"] = None
        options = dict(tbl.storage_options)
        if not san.engine_specific_options:
            for key in _ENGINE_OPTION_KEYS:
                options.pop(key, None)
        if forcing:
            for key, value in (
                ("charset", override.charset),
                ("collation", override.collation),
                ("db_charset", override.charset),
                ("db_collation", override.collation),
            ):
                if key in options or value is not None:
                    options[key] = value
        data["storage_options"] = {k: v for k, v in options.items() if v is not None}
        return TableSchema(**data)

    return SchemaSnapshot(
        database=snapshot.database,
        source_engine=snapshot.source_engine,
        db_charset=override.charset if forcing else snapshot.db_charset,
        db_collation=override.collation if forcing else snapshot.db_collation,
        tables=[_table(t) for t in snapshot.tables if _keep("table", t.table)],
        views=[
            v
            for v in snapshot.views
            if _keep("materialized_view" if v.is_materialized else "view", v.name)
        ],
        routines=[r for r in snapshot.routines if _keep("routine", r.name)],
        triggers=[t for t in snapshot.triggers if _keep("trigger", t.name)],
        sequences=[s for s in snapshot.sequences if _keep("sequence", s.name)],
        enum_types=[e for e in snapshot.enum_types if _keep("enum_type", e.name)],
        extensions=[x for x in snapshot.extensions if _keep("extension", x.name)],
        events=[e for e in snapshot.events if _keep("event", e.name)],
    )


def _empty_like(snapshot: SchemaSnapshot) -> SchemaSnapshot:
    """Snapshot vacío del mismo motor: el "destino" contra el que se diffea para crear todo."""
    return SchemaSnapshot(database=snapshot.database, source_engine=snapshot.source_engine)


# --------------------------------------------------------------------------- #
# Plan de DDL                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class _RenderedObject:
    """Las sentencias que crean UN objeto, más las de sus sub-objetos (índices, FKs)."""

    statements: list[str] = field(default_factory=list)
    children: list[tuple[str, str]] = field(default_factory=list)  # (object_type, sql)


def _render_plan(
    snapshot: SchemaSnapshot, adapter: ExportDialect
) -> dict[tuple[str, str], _RenderedObject]:
    """
    DDL de creación de todos los objetos del snapshot, agrupado por objeto.

    Se reutiliza el pipeline ``diff_snapshots(origen, vacío)`` + ``render_diff`` — el mismo
    que usan el clon y schema-comparisons— en vez de escribir un tercer renderer de
    ``CREATE``. Ese pipeline ya tiene resueltos y con test de regresión los casos que un
    renderer nuevo volvería a romper: el ``AUTO_INCREMENT`` que no encabeza la PK
    (``1075``), la ``UNIQUE KEY`` reflejada por duplicado (``1061``), el ``serial`` de
    PostgreSQL y la fidelidad de ``ENUM``/``UNSIGNED`` en MySQL.

    El agrupamiento por objeto usa ``parent_table`` de los ``DiffItem`` y NO parte el
    ``object_name`` por el punto: un nombre legado puede contener ``.`` y ahí el corte
    asignaría el índice a una tabla inexistente.

    A los objetos con CUERPO se les quita además el calificador de su propia base — ver
    ``_strip_own_schema``.
    """
    diff = diff_snapshots(snapshot, _empty_like(snapshot))
    parents = {
        (it.object_type, it.object_name): it.parent_table for it in diff.items
    }
    plan: dict[tuple[str, str], _RenderedObject] = {}
    for stmt in adapter.render_diff(diff):
        raw_key = (stmt.object_type, stmt.object_name)
        parent = parents.get(raw_key)
        if stmt.object_type in _CHILD_TYPES and parent:
            owner = plan.setdefault(("table", parent), _RenderedObject())
            owner.children.append((stmt.object_type, stmt.sql))
        else:
            plan.setdefault(_catalog_key(*raw_key), _RenderedObject()).statements.append(
                _strip_own_schema(stmt.sql, stmt.object_type, snapshot)
            )
    return plan


def _strip_own_schema(sql: str, object_type: str, snapshot: SchemaSnapshot) -> str:
    """
    Quita del cuerpo el calificador de la base de ORIGEN (solo vistas/rutinas/triggers/
    eventos, y solo en la familia MySQL).

    MySQL/MariaDB guardan la definición con el esquema CALIFICADO:
    ``information_schema.VIEWS.VIEW_DEFINITION`` devuelve siempre
    ``select `origen`.`t`.`col` from `origen`.`t```. Emitir eso tal cual convierte el
    artefacto en una FUGA: restaurado en una base con otro nombre —el caso normal, si no
    para qué exportar— las vistas y rutinas siguen leyendo de la base de ORIGEN, en
    silencio y con los permisos de quien restaure.

    El clon resuelve lo mismo RE-CALIFICANDO (``CloneController._requalify_body``), porque
    ahí el destino se conoce. En un export no se conoce, así que la respuesta correcta es
    QUITAR el calificador: sin él, el motor resuelve contra la base activa de quien ejecuta
    el script, que es exactamente la semántica que se espera de un volcado. Es el mismo
    criterio con el que ``mysqldump`` emite los cuerpos.

    Una referencia a OTRA base se conserva (``strip_self_schema_qualifier`` solo saca la
    propia): si el objeto de verdad cruza de base, eso es parte de su definición y borrarlo
    sería cambiarla.
    """
    if object_type not in _BODY_TYPES:
        return sql
    return strip_self_schema_qualifier(
        sql, snapshot.database, snapshot.source_engine
    )


def _catalog_key(object_type: str, object_name: str) -> tuple[str, str]:
    """
    Nombre del diff → nombre del CATÁLOGO.

    Las dos capas usan vocabularios que coinciden en todo menos en un punto: el diff
    identifica una rutina como ``"PROCEDURE:sp_x"`` (el tipo forma parte de su identidad,
    porque en un mismo esquema puede haber una función y un procedimiento homónimos)
    mientras que el catálogo —y por tanto la selección congelada del preview— la nombra
    ``"sp_x"``. Sin esta traducción, TODA rutina quedaba fuera del artefacto marcada como
    ``no_ddl_rendered``: sin error, sin fallo, simplemente ausente.
    """
    if object_type == "routine" and ":" in object_name:
        return (object_type, object_name.split(":", 1)[1])
    return (object_type, object_name)


# --------------------------------------------------------------------------- #
# Emisión                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportTarget:
    """Todo lo que el writer necesita saber del destino, ya resuelto por el controller."""

    database: str
    engine: str
    # Objetos en el ORDEN DE EMISIÓN congelado por el preview (§5.2). El writer no reordena:
    # ese orden es el que el ``confirm_token`` hasheó y el que el operador vio.
    objects: Sequence[espec.CatalogObject]
    # Tablas cuyos DATOS se exportan (⊆ objetos de tipo ``table``, verificado en el preview).
    data_tables: Sequence[str] = ()
    # Metadatos SOLO para el encabezado, y solo si ``script_comments`` está activo.
    engine_version: str | None = None
    job_id: int | None = None
    generated_at: str | None = None
    # ¿La transacción de lectura cubrió también el catálogo? Sale de
    # ``ExportSession.supports_consistent_structure``, es decir del motor real (§6.2).
    consistent_structure: bool = False


def _sql_entry(ordinal: int, label: str) -> str:
    """
    Nombre del archivo lógico de una sección, con su ordinal de EJECUCIÓN por delante.

    Cinco dígitos fijos para que el orden alfabético y el numérico coincidan siempre (con
    ``part{NN}`` pasa lo mismo y por eso el empaquetador también rellena con ceros).
    """
    return f"{ordinal:05d}-{espec.sanitize_filename(label, fallback='objeto')}"


def write_sql(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    sink: Callable[[str], Any],
) -> ExportStats:
    """
    Genera el artefacto ``sql`` y lo escribe en ``sink``, devolviendo las estadísticas.

    ``sink`` es cualquier callable que acepte texto (el ``.write`` de un archivo abierto en
    modo texto, por ejemplo). El writer **no abre ni cierra nada**: quien es dueño del
    archivo de spool es el almacenamiento (F4), que es también quien sabe de TTL, checksum y
    purga.
    """
    stats = ExportStats()
    for chunk in iter_sql(spec, target, snapshot, adapter, source, stats):
        sink(chunk)
    return stats


def iter_sql(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    stats: ExportStats,
) -> Iterator[str]:
    """
    El artefacto ``sql`` como un flujo de TEXTO plano, sin partir en archivos.

    Es la firma histórica (F3/F4) y se conserva intacta: lo único que hace es quitarle la
    etiqueta de archivo a ``iter_sql_sections``. Un ``organization='single'`` produce
    exactamente los mismos bytes que antes de F5, que es lo que garantiza que el determinismo
    del §8.3 no se rompió al agregar el troceado.
    """
    for chunk in iter_sql_sections(spec, target, snapshot, adapter, source, stats):
        yield chunk.text


def iter_sql_sections(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    stats: ExportStats,
) -> Iterator[Chunk]:
    """
    El generador. Rinde el artefacto por trozos y va llenando ``stats`` a medida que avanza.

    ``stats`` entra como parámetro (y no como valor de retorno) justamente porque esto es un
    generador: quien lo consume trozo a trozo —el almacenamiento de F4, o el test de memoria
    plana— necesita el mismo objeto de estadísticas que se está llenando, no uno que solo
    existiría al terminar.

    Cada trozo viaja etiquetado con el archivo lógico al que pertenece. Con
    ``organization='per_object'`` esa etiqueta parte el script en un archivo por objeto —el
    modo que permite versionar el esquema en un repositorio (§15, punto 3)— y el ordinal del
    nombre documenta el orden en que hay que ejecutarlos. Con ``'single'`` la etiqueta es
    ``None`` en todos y el resultado es un único flujo.
    """
    san = spec.sanitize
    per_object = spec.output.organization == espec.Organization.per_object
    cursor = _Cursor()

    def _section(ordinal: int, label: str) -> None:
        """Mueve el cursor al archivo de esta sección (no-op si el artefacto es único)."""
        if per_object:
            cursor.name = _sql_entry(ordinal, label)

    keys = frozenset((o.object_type, o.name) for o in target.objects)
    clean = sanitize_snapshot(snapshot, spec, keys=keys)
    plan = _render_plan(clean, adapter)
    tables_by_name = {t.table: t for t in clean.tables}
    data_tables = [
        name for name in target.data_tables if name in tables_by_name
    ]
    definer = espec.resolve_definer(san.definer, target.engine)

    charset, collation = _effective_charset(spec, clean)
    emitter = _Emitter(stats, cursor)

    # 1) Encabezado y preámbulo de sesión.
    _section(_ORD_PROLOGUE, "prologo")
    if san.script_comments:
        yield from emitter.raw(_header(spec, target, clean, definer))
    if san.session_preamble:
        yield from emitter.comment(san, "Preparación de la sesión")
        # ``suspend_constraints=True`` SIEMPRE: el artefacto carga datos, y aunque las FKs
        # propias vayan diferidas el destino puede tener otras apuntando a estas tablas. Qué
        # hacer con el pedido lo decide el adapter — en PostgreSQL suspender FKs exige
        # superusuario y ahí la respuesta correcta es no emitir nada (ver su preámbulo).
        for stmt in adapter.export_session_preamble(
            charset=charset, collation=collation, suspend_constraints=True
        ):
            yield from emitter.statement(stmt)

    # 2) Contenedor y contexto.
    scope_mode = str(spec.structure.scope_ddl)
    if scope_mode != str(espec.ScopeDdl.NONE):
        yield from emitter.comment(san, f"Base de datos {target.database}")
        for stmt in adapter.export_scope_ddl(
            target.database,
            scope_mode,
            charset=charset,
            collation=collation,
            if_exists=spec.structure.drop_if_exists,
        ):
            yield from emitter.statement(stmt)
    use_scope = adapter.export_use_scope(target.database)
    if use_scope:
        yield from emitter.statement(use_scope)

    # La envoltura transaccional arranca DESPUÉS del contenedor: en PostgreSQL un
    # ``CREATE/DROP DATABASE`` dentro de un bloque transaccional falla (la matriz ya prohíbe
    # combinar ambos, esto es la defensa en profundidad).
    if san.transaction_wrap:
        yield from emitter.statement("START TRANSACTION")

    # 3) Objetos, en el orden congelado por el preview.
    deferred: list[tuple[str, str, str]] = []  # (tabla, object_type, sql)
    counters: list[tuple[str, TableSchema]] = []
    seq = 0
    entity_mode = str(spec.structure.entity_ddl)
    emits_structure = entity_mode != str(espec.EntityDdl.NONE)

    for obj in target.objects:
        seq += 1
        phase = _phase_of(obj.object_type)
        item = stats._add(
            ExportItemStat(
                seq=seq,
                object_type=obj.object_type,
                object_name=obj.name,
                phase=phase,
            )
        )
        if obj.object_type == "table" and obj.name in tables_by_name:
            # Se registra ANTES del corte por ``emits_structure``: en una exportación "solo
            # datos" no hay CREATE TABLE, pero el contador del destino igual puede necesitar
            # el ajuste después de insertar filas.
            counters.append((obj.name, tables_by_name[obj.name]))
        rendered = plan.get((obj.object_type, obj.name))
        if not emits_structure:
            # Exportación "solo datos" (§5.3): el objeto entra igual en el reporte, con su
            # motivo, para que nadie lo busque en el artefacto y crea que se perdió.
            item.status = _SKIPPED
            item.reason = "structure_disabled"
            continue
        if rendered is None or not rendered.statements:
            item.status = _SKIPPED
            item.reason = "no_ddl_rendered"
            stats.warn(
                f"No se pudo generar el DDL de {obj.object_type} '{obj.name}': el objeto "
                "queda fuera del artefacto."
            )
            continue

        start_bytes = stats.bytes_written
        _section(_ORD_OBJECT_BASE + seq, f"{obj.object_type}-{obj.name}")
        yield from emitter.comment(san, f"{obj.object_type} {obj.name}")
        if entity_mode == str(espec.EntityDdl.DROP_CREATE):
            yield from emitter.statement(
                adapter.export_entity_drop(
                    obj.object_type,
                    obj.name,
                    payload=_payload_for(clean, obj),
                    if_exists=spec.structure.drop_if_exists,
                    cascade=spec.structure.drop_cascade,
                )
            )
        for sql in rendered.statements:
            yield from emitter.object_statement(
                adapter, spec, sql, obj.object_type, definer, stats
            )
        # Sub-objetos de una tabla (índices y FKs): pegados a su tabla o al final.
        for child_type, child_sql in rendered.children:
            if san.constraints_placement == espec.ConstraintsPlacement.deferred:
                deferred.append((obj.name, child_type, child_sql))
            else:
                yield from emitter.object_statement(
                    adapter, spec, child_sql, child_type, definer, stats
                )
        item.bytes_written = stats.bytes_written - start_bytes
        stats.objects_exported += 1

    # 4) Datos.
    if data_tables and spec.data.insert_variant != espec.InsertVariant.none:
        for index, name in enumerate(data_tables, start=1):
            _section(_ORD_DATA_BASE + index, f"datos-{name}")
            yield from _emit_table_data(
                spec, adapter, source, tables_by_name[name], stats, san, cursor
            )
    elif data_tables:
        stats.warn(
            "Se seleccionaron tablas con datos pero data.insert_variant='none': el "
            "artefacto no lleva ninguna fila."
        )

    # 5) Índices, UNIQUE, CHECK y FKs diferidos (§8.4, paso 6).
    if deferred:
        _section(_ORD_CONSTRAINTS, "indices-y-claves-foraneas")
        yield from emitter.comment(san, "Índices y claves foráneas")
        for _table, child_type, child_sql in deferred:
            yield from emitter.object_statement(
                adapter, spec, child_sql, child_type, definer, stats
            )

    # 6) Contadores de autoincremento (§8.4, paso 12).
    _section(_ORD_COUNTERS, "contadores")
    yield from _emit_counters(spec, adapter, source, counters, stats, san, emitter)

    # 7) Epílogo: RESTAURA lo que tocó el preámbulo. Un script que deja la sesión con
    #    FOREIGN_KEY_CHECKS=0 es un fallo grave, así que el epílogo es obligatorio cuando
    #    hubo preámbulo — no una opción aparte que alguien pueda apagar.
    _section(_ORD_EPILOGUE, "epilogo")
    if san.transaction_wrap:
        yield from emitter.statement("COMMIT")
    if san.session_preamble:
        yield from emitter.comment(san, "Restauración de la sesión")
        for stmt in adapter.export_session_epilogue():
            yield from emitter.statement(stmt)

    if san.partitions:
        # No hay forma de reproducirlas desde el ``SchemaSnapshot``: no captura la cláusula
        # ``PARTITION BY``. Se declara en vez de dejar creer que viajaron (una tabla
        # particionada restaurada sin sus particiones "funciona" y se degrada en silencio).
        stats.warn(
            "Las particiones no se reproducen en el artefacto: el snapshot estructural no "
            "captura la cláusula PARTITION BY. Las tablas particionadas se recrean sin "
            "particionar."
        )

    stats.complete = all(i.status != _ERROR for i in stats.items)
    if not stats.complete and san.script_comments:
        yield from emitter.raw(
            "-- EXPORTACIÓN INCOMPLETA — ver el reporte de incidencias del job\n"
        )


# --------------------------------------------------------------------------- #
# Emisión: helpers                                                             #
# --------------------------------------------------------------------------- #


class _Emitter:
    """
    Escribe trozos y lleva la cuenta de bytes y sentencias.

    Los bytes se miden sobre la codificación UTF-8, no sobre ``len()`` del ``str``: con
    CJK o emoji un carácter pesa 3-4 bytes y el tamaño informado del artefacto mentiría
    (mismo error que ya se corrigió en ``MIGRATION_CAPTURE_MAX_BYTES``).
    """

    def __init__(self, stats: ExportStats, cursor: _Cursor | None = None):
        self.stats = stats
        # Sin cursor, todo sale en el mismo archivo lógico: es lo que necesita quien use el
        # emisor fuera del generador principal.
        self.cursor = cursor if cursor is not None else _Cursor()

    def raw(self, text: str) -> Iterator[Chunk]:
        if not text:
            return
        self.stats.bytes_written += len(text.encode("utf-8"))
        yield Chunk(self.cursor.name, text)

    def prologue(self, text: str) -> Iterator[Chunk]:
        """
        Un trozo que el empaquetador REPITE al principio de cada fragmento.

        Cuenta en los bytes igual que cualquier otro (los bytes que se repiten también pesan
        en el artefacto), pero viaja marcado para que el ``part02`` de un CSV salga con su
        fila de encabezado.
        """
        if not text:
            return
        self.stats.bytes_written += len(text.encode("utf-8"))
        yield Chunk(self.cursor.name, text, True)

    def comment(self, san: espec.SanitizeOptions, title: str) -> Iterator[Chunk]:
        if not san.script_comments:
            return
        yield from self.raw(f"\n-- {title}\n")

    def statement(self, sql: str) -> Iterator[Chunk]:
        body = sql.rstrip().rstrip(";")
        if not body.strip():
            return
        self.stats.statements += 1
        yield from self.raw(f"{body};\n")

    def object_statement(
        self,
        adapter: ExportDialect,
        spec: espec.ExportSpec,
        sql: str,
        object_type: str,
        definer: espec.DefinerMode,
        stats: ExportStats,
    ) -> Iterator[Chunk]:
        """
        Una sentencia de DDL de objeto, con las tres transformaciones que dependen del motor:
        ``DEFINER``, idempotencia (``IF NOT EXISTS``) y envoltura ``DELIMITER``.
        """
        body = sql.rstrip().rstrip(";")
        body = adapter.export_definer_clause(
            body, mode=str(definer), value=spec.sanitize.definer_value
        )
        if str(spec.structure.entity_ddl) == str(espec.EntityDdl.CREATE_IF_NOT_EXISTS):
            idempotent = adapter.export_make_idempotent(body, object_type)
            if idempotent is None:
                stats.warn(
                    f"El motor no admite una forma idempotente de crear objetos de tipo "
                    f"'{object_type}': esas sentencias fallan si el objeto ya existe."
                )
            else:
                body = idempotent
        wrapper = adapter.export_body_wrapper(object_type)
        self.stats.statements += 1
        if wrapper is not None:
            prefix, suffix = wrapper
            yield from self.raw(f"{prefix}{body}{suffix}\n")
        else:
            yield from self.raw(f"{body};\n")


def _phase_of(object_type: str) -> str:
    if object_type in _PREREQUISITE_TYPES:
        return "prerequisites"
    if object_type in _BODY_TYPES:
        return "bodies"
    return "structure"


def _payload_for(snapshot: SchemaSnapshot, obj: espec.CatalogObject):
    """DTO del objeto, que el ``DROP`` de algunos motores necesita (rutina, trigger)."""
    if obj.object_type == "routine":
        return next((r for r in snapshot.routines if r.name == obj.name), None)
    if obj.object_type == "trigger":
        return next((t for t in snapshot.triggers if t.name == obj.name), None)
    if obj.object_type in ("view", "materialized_view"):
        return next((v for v in snapshot.views if v.name == obj.name), None)
    return None


def _effective_charset(
    spec: espec.ExportSpec, snapshot: SchemaSnapshot
) -> tuple[str | None, str | None]:
    """El par charset/collation del script: el forzado, o el del origen."""
    override = spec.sanitize.charset_override
    if override.mode == espec.CharsetOverrideMode.override:
        return override.charset, override.collation
    return snapshot.db_charset, snapshot.db_collation


def _header(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    definer: espec.DefinerMode,
) -> str:
    """
    Encabezado de metadatos (§14). Solo se emite con ``script_comments`` activo, que es
    también la forma de obtener un artefacto comparable byte a byte entre dos corridas.
    """
    lines = [
        "-- =========================================================================",
        f"-- Exportación de '{target.database}' — gateway de administración de BD",
        f"-- Motor: {target.engine}"
        + (f" {target.engine_version}" if target.engine_version else ""),
        f"-- Generador: {GENERATOR_VERSION}",
        f"-- Objetos: {len(target.objects)} | tablas con datos: {len(target.data_tables)}",
        f"-- Opciones: estructura={spec.structure.entity_ddl} "
        f"contenedor={spec.structure.scope_ddl} datos={spec.data.insert_variant} "
        f"definer={definer} constraints={spec.sanitize.constraints_placement}",
    ]
    if target.generated_at:
        lines.append(f"-- Fecha: {target.generated_at}")
    if target.job_id is not None:
        lines.append(f"-- Job: {target.job_id}")
    if not target.consistent_structure:
        lines.append(
            "-- AVISO: la estructura NO está garantizada como del mismo instante que los "
            "datos (ver §6.2: en MySQL/MariaDB el diccionario de datos no participa del "
            "snapshot MVCC)."
        )
    lines.append(
        "-- =========================================================================",
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Datos                                                                        #
# --------------------------------------------------------------------------- #


def _insertable_columns(table: TableSchema) -> tuple[list[ColumnInfo], list[str]]:
    """
    Columnas que viajan en el ``INSERT`` y las que se excluyen.

    Las columnas GENERADAS/calculadas se excluyen SIEMPRE: el motor las calcula y rechaza
    que se les asigne un valor (``The value specified for generated column … is not
    allowed``). Incluirlas produce un script que falla en su primera fila — es un caso con
    test.
    """
    keep = [c for c in table.columns if c.computed is None]
    dropped = [c.name for c in table.columns if c.computed is not None]
    return keep, dropped


def _select_sql(
    dialect: str,
    table: TableSchema,
    columns: Sequence[ColumnInfo],
    order_by: Sequence[str],
    row_filter: espec.RowFilter,
) -> str:
    """
    La consulta de lectura. Los identificadores salen del CATÁLOGO y van delimitados; el
    ``where`` ya lo validó ``export_spec.validate_row_filter`` con la maquinaria de
    ``query_policy`` (debe ser una condición de lectura simple sobre ESTA tabla), así que
    acá solo se inserta — validar dos veces con dos criterios distintos es cómo se abre el
    hueco en el que se actualizó menos.

    Por eso mismo la cadena la ARMA ``export_spec.build_row_select_sql`` y no este módulo:
    el validador llama al mismo constructor, con el mismo ``ORDER BY`` y el mismo
    ``LIMIT``, así que lo validado y lo ejecutado son la MISMA cadena y no pueden volver a
    divergir.
    """
    return espec.build_row_select_sql(
        dialect,
        table.table,
        [c.name for c in columns],
        where=row_filter.where,
        order_by=order_by,
        limit=row_filter.limit,
    )


def _emit_table_data(
    spec: espec.ExportSpec,
    adapter: ExportDialect,
    source: RowSource,
    table: TableSchema,
    stats: ExportStats,
    san: espec.SanitizeOptions,
    cursor: _Cursor | None = None,
) -> Iterator[Chunk]:
    """
    Vuelca las filas de UNA tabla en sentencias acotadas por bytes.

    El corte lo manda ``max_statement_bytes`` y no ``rows_per_statement``: una tabla con una
    columna ``LONGTEXT`` puede tener filas de megabytes, y un INSERT de 200 de esas supera
    el ``max_allowed_packet`` del destino — el artefacto se genera bien y falla al
    ejecutarse, que es el peor momento para enterarse.
    """
    dialect = adapter.dialect
    data = spec.data
    columns, excluded = _insertable_columns(table)
    if not columns:
        stats._add(
            ExportItemStat(
                seq=len(stats.items) + 1,
                object_type="table_data",
                object_name=table.table,
                phase="data",
                status=_SKIPPED,
                reason="all_columns_generated",
                rows_exported=0,
            )
        )
        return

    include_list = data.include_column_list
    if excluded and not include_list:
        # Sin lista de columnas, el número de valores tiene que coincidir con el de columnas
        # de la tabla — y acá no coincide porque se excluyeron las generadas. Se fuerza la
        # lista en vez de emitir un INSERT que el motor rechaza.
        include_list = True
        stats.warn(
            f"La tabla '{table.table}' tiene columnas generadas: se incluye la lista de "
            "columnas en sus INSERT aunque data.include_column_list sea false."
        )

    order_by = adapter.export_row_order_by(table)
    deterministic = bool(order_by)
    item = stats._add(
        ExportItemStat(
            seq=len(stats.items) + 1,
            object_type="table_data",
            object_name=table.table,
            phase="data",
            rows_exported=0,
            deterministic=deterministic,
        )
    )
    if not deterministic:
        stats.warn(
            f"La tabla '{table.table}' sale sin orden garantizado (no tiene clave primaria "
            "ni una tupla de columnas ordenable): dos exportaciones pueden diferir."
        )

    prefix, suffix = adapter.export_insert_wrapper(
        table.table,
        [c.name for c in columns] if include_list else [],
        variant=str(data.insert_variant),
        primary_key=list(table.primary_key),
    )
    max_bytes = max(1024, min(int(data.max_statement_bytes), EXPORT_MAX_STATEMENT_BYTES))
    max_rows = max(1, int(data.rows_per_statement or EXPORT_ROWS_PER_STATEMENT))

    emitter = _Emitter(stats, cursor)
    select_sql = _select_sql(
        dialect,
        table,
        columns,
        order_by,
        data.per_object.get(table.table) or espec.RowFilter(),
    )
    if san.script_comments:
        yield from emitter.raw(f"\n-- datos de {table.table}\n")

    start_bytes = stats.bytes_written
    pending: list[str] = []
    pending_bytes = 0
    rows = 0
    ncols = len(columns)

    def _flush() -> Iterator[Chunk]:
        nonlocal pending, pending_bytes
        if not pending:
            return
        stats.statements += 1
        yield from emitter.raw(f"{prefix}\n" + ",\n".join(pending) + f"{suffix};\n")
        pending = []
        pending_bytes = 0

    for row in source.iter_rows(select_sql, batch_rows=EXPORT_BATCH_ROWS):
        try:
            tuple_sql = "  (" + ", ".join(
                render_value(row[i], dialect) for i in range(ncols)
            ) + ")"
        except UnsupportedValueError as exc:
            # Fail-closed del §8.2: un tipo que el renderizador no conoce NO se serializa
            # "a lo que salga". Se corta la tabla acá y se reporta con un motivo de
            # vocabulario cerrado — jamás el mensaje del driver, que puede traer valores.
            yield from _flush()
            item.status = _ERROR
            item.reason = f"unsupported_type:{exc}"
            item.rows_exported = rows
            stats.warn(
                f"La tabla '{table.table}' se cortó en la fila {rows + 1}: hay un valor de "
                "un tipo que no se puede representar como literal SQL."
            )
            break
        size = len(tuple_sql.encode("utf-8")) + 2
        if pending and (pending_bytes + size > max_bytes or len(pending) >= max_rows):
            yield from _flush()
        pending.append(tuple_sql)
        pending_bytes += size
        rows += 1
    else:
        yield from _flush()
        item.rows_exported = rows

    item.bytes_written = stats.bytes_written - start_bytes
    stats.rows_exported += rows
    stats.tables_with_data += 1


def _emit_counters(
    spec: espec.ExportSpec,
    adapter: ExportDialect,
    source: RowSource,
    counters: Iterable[tuple[str, TableSchema]],
    stats: ExportStats,
    san: espec.SanitizeOptions,
    emitter: _Emitter,
) -> Iterator[Chunk]:
    """
    Ajuste de contadores de autoincremento (§8.4, paso 12).

    ``auto`` los emite solo para las tablas que se exportaron CON datos: en una tabla sin
    filas un ``AUTO_INCREMENT=5000`` es basura que confunde a quien lea el script. ``omit``
    no emite ninguno y ``keep`` los emite todos.

    El VALOR lo provee la fuente (``counter_value``), no el snapshot: es estado, no
    estructura, y el ``SchemaSnapshot`` deliberadamente no lo captura. Una fuente que no lo
    implemente devuelve ``None`` y no se emite nada — nunca se inventa un valor, porque un
    contador equivocado deja la tabla generando ids que ya existen.
    """
    mode = spec.sanitize.autoincrement
    if mode == espec.AutoincrementMode.omit:
        return
    reader = getattr(source, "counter_value", None)
    if reader is None:
        return
    with_data = {i.object_name for i in stats.items if i.object_type == "table_data"}
    emitted = False
    for name, table in counters:
        if mode == espec.AutoincrementMode.auto and name not in with_data:
            continue
        column = next(
            (c.name for c in table.columns if c.autoincrement or c.identity is not None),
            None,
        )
        if column is None:
            continue
        value = reader(name, column)
        sql = adapter.export_counter_reset(name, value, column=column)
        if not sql:
            continue
        if not emitted:
            emitted = True
            yield from emitter.comment(san, "Contadores de autoincremento")
        yield from emitter.statement(sql)


# --------------------------------------------------------------------------- #
# Formatos de DATOS: csv, json y ndjson (F5)                                    #
# --------------------------------------------------------------------------- #
# Los tres comparten TODO lo que no es la forma del archivo —qué columnas viajan, con qué
# ``ORDER BY``, qué filtro por objeto, cómo se reporta un tipo no representable— y por eso
# esa parte vive en helpers comunes. Lo único que cada formato aporta es cómo se rinde una
# fila y qué la envuelve.
#
# Ninguno transporta estructura EJECUTABLE (la matriz lo hace cumplir): a lo sumo llevan un
# manifiesto DESCRIPTIVO, que es documentación legible por máquina y no un script.

# Nombre del archivo del manifiesto de esquema cuando se emite uno por objeto.
_MANIFEST_ENTRY = "_esquema"


def _data_tables_of(
    target: ExportTarget, tables_by_name: dict[str, TableSchema]
) -> list[TableSchema]:
    """Las tablas con datos del plan congelado, en su orden y solo las que existen."""
    return [tables_by_name[n] for n in target.data_tables if n in tables_by_name]


def _table_data_item(
    stats: ExportStats, table: TableSchema, *, deterministic: bool
) -> ExportItemStat:
    return stats._add(
        ExportItemStat(
            seq=len(stats.items) + 1,
            object_type="table_data",
            object_name=table.table,
            phase="data",
            rows_exported=0,
            deterministic=deterministic,
        )
    )


def _prepare_table_data(
    spec: espec.ExportSpec,
    adapter: ExportDialect,
    table: TableSchema,
    stats: ExportStats,
) -> tuple[list[ColumnInfo], str, ExportItemStat] | None:
    """
    Columnas, consulta y ficha de reporte de una tabla, para los formatos de datos.

    Devuelve ``None`` cuando no hay nada que volcar (todas las columnas son generadas), ya
    con el ítem ``skipped`` registrado.

    Las columnas GENERADAS se excluyen también acá, y no solo en el camino ``sql``, por dos
    razones: el destino natural de un csv es volver a entrar por ``LOAD DATA``/``COPY``, que
    rechazan igual que un ``INSERT`` que se les asigne un valor; y tener dos criterios sobre
    qué columnas "son" la tabla haría que un artefacto ``csv`` y uno ``sql`` de la misma
    exportación no describieran lo mismo.
    """
    columns, excluded = _insertable_columns(table)
    if not columns:
        stats._add(
            ExportItemStat(
                seq=len(stats.items) + 1,
                object_type="table_data",
                object_name=table.table,
                phase="data",
                status=_SKIPPED,
                reason="all_columns_generated",
                rows_exported=0,
            )
        )
        return None
    if excluded:
        stats.warn(
            "Las columnas generadas quedan fuera del volcado de datos: el motor las calcula "
            "y rechaza que se les asigne un valor al reimportar."
        )
    order_by = adapter.export_row_order_by(table)
    deterministic = bool(order_by)
    if not deterministic:
        stats.warn(
            f"La tabla '{table.table}' sale sin orden garantizado (no tiene clave primaria "
            "ni una tupla de columnas ordenable): dos exportaciones pueden diferir."
        )
    item = _table_data_item(stats, table, deterministic=deterministic)
    select_sql = _select_sql(
        adapter.dialect,
        table,
        columns,
        order_by,
        spec.data.per_object.get(table.table) or espec.RowFilter(),
    )
    return columns, select_sql, item


def _flush_size(spec: espec.ExportSpec) -> int:
    """
    Cada cuántos bytes se le entrega un trozo al empaquetador.

    Normalmente ~64 KB, pero **nunca más que ``split_max_bytes``**: el empaquetador solo
    puede cortar un fragmento ENTRE trozos, así que un trozo más grande que el fragmento
    haría que la fragmentación no ocurriera nunca (y el usuario recibiría un solo archivo
    donde pidió varios, sin ningún error). Es el acoplamiento mínimo entre las dos capas y
    por eso vive en una función con nombre.
    """
    split = int(spec.output.split_max_bytes or 0)
    return min(_TEXT_CHUNK_BYTES, split) if split else _TEXT_CHUNK_BYTES


def _emit_rows(
    *,
    source: RowSource,
    select_sql: str,
    emitter: _Emitter,
    stats: ExportStats,
    item: ExportItemStat,
    table: TableSchema,
    render_row: Callable[[Sequence[Any], int], str],
    flush_bytes: int = _TEXT_CHUNK_BYTES,
) -> Iterator[Chunk]:
    """
    Recorre las filas y las rinde en trozos acotados. Devuelve el número de filas emitidas.

    Es el bucle COMÚN de los tres formatos de datos, con el mismo fail-closed del camino
    ``sql``: un valor de un tipo que no se puede representar **corta la tabla** y se reporta
    con un motivo de vocabulario cerrado. Nunca ``str`` del error del driver: puede llevar
    valores de filas dentro (criterio R4, §9.5).

    El consumo de memoria es plano: en el buffer solo vive el trozo en curso, no la tabla.
    """
    buffer: list[str] = []
    buffered = 0
    rows = 0
    for row in source.iter_rows(select_sql, batch_rows=EXPORT_BATCH_ROWS):
        try:
            text = render_row(row, rows)
        except UnsupportedValueError as exc:
            if buffer:
                yield from emitter.raw("".join(buffer))
            item.status = _ERROR
            item.reason = f"unsupported_type:{exc}"
            item.rows_exported = rows
            stats.warn(
                f"La tabla '{table.table}' se cortó en la fila {rows + 1}: hay un valor de "
                "un tipo que no se puede representar en este formato."
            )
            return rows
        size = len(text.encode("utf-8"))
        if buffer and buffered + size > flush_bytes:
            # Se vacía ANTES de pasarse, no después: con ``split_max_bytes`` pequeño el
            # trozo ES la unidad de corte del fragmento, y uno que se pasa arrastra el
            # fragmento entero por encima del tope. Una fila nunca se parte.
            yield from emitter.raw("".join(buffer))
            buffer = []
            buffered = 0
        buffer.append(text)
        buffered += size
        rows += 1
    if buffer:
        yield from emitter.raw("".join(buffer))
    item.rows_exported = rows
    return rows


def _report_structure_objects(
    spec: espec.ExportSpec, target: ExportTarget, stats: ExportStats
) -> None:
    """
    Registra en el reporte los objetos de estructura que un formato de datos NO puede llevar.

    Parece ruido y no lo es: el §14 exige decir **qué se omitió y por qué**. Sin estos ítems,
    una exportación ``csv`` de una base con vistas y rutinas produce un artefacto en el que
    esos objetos simplemente no están, sin ninguna huella de que existían.
    """
    reason = "manifest_only" if spec.output.schema_manifest else "format_data_only"
    data = set(target.data_tables)
    for seq, obj in enumerate(target.objects, start=1):
        if obj.object_type == "table" and obj.name in data:
            continue  # sus datos sí viajan; el ítem lo pone la fase de datos
        stats._add(
            ExportItemStat(
                seq=seq,
                object_type=obj.object_type,
                object_name=obj.name,
                phase=_phase_of(obj.object_type),
                status=_SKIPPED,
                reason=reason,
            )
        )


def _finish_data_table(stats: ExportStats, item: ExportItemStat, start_bytes: int) -> None:
    item.bytes_written = stats.bytes_written - start_bytes
    stats.rows_exported += item.rows_exported or 0
    stats.tables_with_data += 1


def _finalize(stats: ExportStats) -> bool:
    """Marca ``complete`` con el MISMO criterio del camino ``sql`` y lo devuelve."""
    stats.complete = all(i.status != _ERROR for i in stats.items)
    return stats.complete


# --------------------------------------------------------------------------- #
# csv                                                                          #
# --------------------------------------------------------------------------- #


def csv_field(text: str | None, opts: espec.CsvOptions) -> str:
    """
    Un campo del formato delimitado. ``None`` es NULL; ``""`` es la cadena vacía.

    **Esta función es la que hace distinguibles NULL y cadena vacía**, que
    ``render_value_text`` deja explícitamente en manos del llamador (devuelve ``""`` para
    ambos). El criterio es el de ``COPY … WITH CSV`` de PostgreSQL:

    - NULL → ``null_representation`` **sin comillas** (por defecto, campo vacío);
    - ``""`` → **siempre cuoteado**, para que no se lea como el campo vacío de un NULL;
    - un texto igual al centinela de nulos → también cuoteado, por lo mismo;
    - cualquier otro se cuotea solo si lo necesita (separador, comilla o salto de línea),
      que es lo que mantiene el archivo legible y el volcado estable.

    Se escribe a mano en vez de usar ``csv.writer`` porque ``QUOTE_NOTNULL`` resuelve el par
    NULL/``''`` pero no admite un centinela distinto del campo vacío: con
    ``null_representation='\\N'`` el centinela saldría cuoteado y volvería a confundirse con
    la cadena literal ``\\N``. Además el módulo ``csv`` escribe a un archivo y acá se rinde
    texto.
    """
    if text is None:
        return opts.null_representation
    quote = opts.quote_char
    needs_quotes = (
        text == ""
        or (opts.null_representation != "" and text == opts.null_representation)
        or opts.delimiter in text
        or quote in text
        or "\r" in text
        or "\n" in text
        or (opts.escape_char is not None and opts.escape_char in text)
    )
    if not needs_quotes:
        return text
    if opts.escape_char is None:
        body = text.replace(quote, quote * 2)  # RFC 4180
    else:
        esc = opts.escape_char
        body = text.replace(esc, esc * 2).replace(quote, esc + quote)
    return f"{quote}{body}{quote}"


def iter_csv(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    stats: ExportStats,
) -> Iterator[Chunk]:
    """
    Formato delimitado: **solo datos, un archivo por tabla** (la matriz lo hace cumplir).

    La fila de encabezado y la marca de orden de bytes se emiten como trozos ``prologue``:
    si el archivo se parte por tamaño, el empaquetador las repite al principio de cada
    fragmento. Un ``part02`` sin encabezado no es el mismo archivo que el ``part01``.
    """
    opts = spec.csv
    eol = opts.line_terminator.text
    binary = str(spec.output.binary_encoding)
    clean = sanitize_snapshot(snapshot, spec)
    tables_by_name = {t.table: t for t in clean.tables}
    cursor = _Cursor()
    emitter = _Emitter(stats, cursor)

    _report_structure_objects(spec, target, stats)

    for table in _data_tables_of(target, tables_by_name):
        cursor.name = espec.sanitize_filename(table.table, fallback="tabla")
        prepared = _prepare_table_data(spec, adapter, table, stats)
        if prepared is None:
            continue
        columns, select_sql, item = prepared
        start_bytes = stats.bytes_written

        preamble = "﻿" if opts.bom else ""
        if opts.header:
            preamble += (
                opts.delimiter.join(csv_field(c.name, opts) for c in columns) + eol
            )
        # El encabezado se repite en cada fragmento, no solo en el primero.
        yield from emitter.prologue(preamble)

        ncols = len(columns)

        def _row(row: Sequence[Any], _index: int, ncols=ncols) -> str:
            return (
                opts.delimiter.join(
                    csv_field(
                        None
                        if row[i] is None
                        else render_value_text(row[i], binary_encoding=binary),
                        opts,
                    )
                    for i in range(ncols)
                )
                + eol
            )

        yield from _emit_rows(
            source=source,
            select_sql=select_sql,
            emitter=emitter,
            stats=stats,
            item=item,
            table=table,
            render_row=_row,
            flush_bytes=_flush_size(spec),
        )
        _finish_data_table(stats, item, start_bytes)

    _finalize(stats)


# --------------------------------------------------------------------------- #
# json / ndjson                                                                #
# --------------------------------------------------------------------------- #


def json_value(value, *, binary_encoding: str):
    """
    Un valor como tipo NATIVO de JSON cuando se puede, y como texto cuando no.

    ``None``/``bool``/``int``/``float`` salen nativos: convertirlos en cadenas obligaría a
    todo consumidor a re-tipar el archivo y haría inútil el formato para una integración.

    ``Decimal`` sale como **cadena**, igual que en ``sql_literals`` y en ``value_json``:
    JSON no tiene decimal exacto y pasarlo por punto flotante perdería dígitos en silencio
    de un ``DECIMAL(30,10)``. Un entero grande sí sale como número —es JSON válido—; que un
    parser de 64 bits flojos lo redondee es problema del consumidor, no del artefacto.

    El resto (fechas, ``timedelta``, ``UUID``, binarios, JSON anidado) pasa por
    ``render_value_text``, que ya es el criterio único del proyecto para eso; los binarios
    respetan ``binary_encoding``.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise UnsupportedValueError("float no finito")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedValueError("decimal no finito")
        return str(value)
    return render_value_text(value, binary_encoding=binary_encoding)


def _json_dump(payload) -> str:
    """
    Serialización COMPACTA y con las claves en el orden en que se construyeron.

    ``ensure_ascii=False`` para no inflar el archivo escapando cada acento, y separadores sin
    espacios porque el artefacto es para una máquina. Nada de ``sort_keys``: el orden de las
    columnas es el del catálogo y perderlo rompería la comparabilidad de dos volcados (§8.3).
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def build_schema_manifest(
    spec: espec.ExportSpec, target: ExportTarget, snapshot: SchemaSnapshot
) -> dict:
    """
    Manifiesto DESCRIPTIVO del esquema: columnas, tipos, claves, índices y relaciones.

    **No es un script y no permite restaurar nada**, y por eso lo dice el propio documento
    en ``executable: false`` y en ``note``: alguien va a encontrar este archivo dentro de un
    zip dentro de seis meses y tiene que saber en el primer renglón que no sirve para
    recrear la base. Para eso está el formato ``sql``.

    No lleva metadatos volátiles (fecha, id de job): igual que el encabezado del script, se
    deja fuera lo que cambia entre dos corridas idénticas para que los volcados se puedan
    comparar (§8.3). Esos datos viven en el manifiesto del ARTEFACTO
    (``GET /database-exports/{id}/manifest``), que es otra cosa.
    """
    keys = frozenset((o.object_type, o.name) for o in target.objects)
    clean = sanitize_snapshot(snapshot, spec, keys=keys)
    data_tables = set(target.data_tables)
    return {
        "executable": False,
        "note": (
            "Documentación del esquema legible por máquina. NO es un script ejecutable: "
            "desde este archivo no se puede recrear ni restaurar la base. Para eso hay que "
            "exportar en formato 'sql'."
        ),
        "generator_version": GENERATOR_VERSION,
        "database": clean.database,
        "engine": target.engine,
        "tables": [
            {
                "name": t.table,
                "comment": t.comment,
                "has_data_in_artifact": t.table in data_tables,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "nullable": c.nullable,
                        "default": None if c.default is None else str(c.default),
                        "primary_key": bool(c.primary_key),
                        "autoincrement": bool(c.autoincrement),
                        "generated": c.computed is not None,
                        "comment": c.comment,
                        "charset": c.charset,
                        "collation": c.collation,
                    }
                    for c in t.columns
                ],
                "primary_key": list(t.primary_key),
                "indexes": [
                    {"name": i.name, "columns": list(i.columns), "unique": bool(i.unique)}
                    for i in t.indexes
                ],
                "foreign_keys": [
                    {
                        "name": fk.name,
                        "columns": list(fk.columns),
                        "referred_table": fk.referred_table,
                        "referred_columns": list(fk.referred_columns),
                    }
                    for fk in t.foreign_keys
                ],
            }
            for t in clean.tables
        ],
        "views": [
            {"name": v.name, "materialized": bool(v.is_materialized)} for v in clean.views
        ],
        "routines": [{"name": r.name, "kind": r.kind} for r in clean.routines],
        "triggers": [{"name": t.name, "table": t.table} for t in clean.triggers],
        "sequences": [{"name": s.name} for s in clean.sequences],
        "enum_types": [{"name": e.name} for e in clean.enum_types],
        "events": [{"name": e.name} for e in clean.events],
    }


def _manifest_entry_name(target: ExportTarget) -> str:
    """
    Nombre del archivo del manifiesto, sin chocar con el de una tabla.

    Una tabla llamada ``_esquema`` es rarísima pero posible, y dos entradas homónimas dentro
    de un zip son un archivo que se pisa al descomprimir — el peor final posible para el
    volcado de esa tabla.
    """
    taken = {espec.sanitize_filename(n, fallback="tabla") for n in target.data_tables}
    name = _MANIFEST_ENTRY
    while name in taken:
        name += "_"
    return name


def iter_json(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    stats: ExportStats,
) -> Iterator[Chunk]:
    """
    Formato estructurado en un **arreglo** por tabla.

    Dos formas, según la organización, y ninguna es ambigua:

    - ``per_object``: cada archivo es un arreglo puro ``[{…},{…}]`` — el "arreglo único" del
      §8.1, y lo que espera cualquier consumidor;
    - ``single``: como un archivo no puede contener dos arreglos y seguir siendo JSON, se
      envuelve en un objeto ``{"tables":{"users":[…]}}`` con los metadatos y, si se pidió,
      el manifiesto. Es la única forma válida de meter N tablas en un documento.

    ``complete`` va al FINAL del envoltorio a propósito: es lo último que se sabe. Con
    ``on_error='stop'`` la generación se corta antes de emitirlo y el documento queda
    truncado —o sea, inválido—, que es la señal correcta: un JSON a medias tiene que fallar
    al parsearse, no parecer completo.
    """
    per_object = spec.output.organization == espec.Organization.per_object
    binary = str(spec.output.binary_encoding)
    clean = sanitize_snapshot(snapshot, spec)
    tables_by_name = {t.table: t for t in clean.tables}
    cursor = _Cursor()
    emitter = _Emitter(stats, cursor)

    _report_structure_objects(spec, target, stats)
    tables = _data_tables_of(target, tables_by_name)

    if per_object and spec.output.schema_manifest:
        cursor.name = _manifest_entry_name(target)
        yield from emitter.raw(
            _json_dump(build_schema_manifest(spec, target, snapshot))
        )

    if not per_object:
        head: dict = {
            "database": target.database,
            "engine": target.engine,
            "format": str(spec.format),
            "generator_version": GENERATOR_VERSION,
        }
        # Los volátiles solo con ``script_comments``, igual que el encabezado del script: sin
        # ellos dos corridas idénticas dan el mismo archivo byte a byte (§8.3).
        if spec.sanitize.script_comments:
            if target.generated_at:
                head["generated_at"] = target.generated_at
            if target.job_id is not None:
                head["job_id"] = target.job_id
        if spec.output.schema_manifest:
            head["manifest"] = build_schema_manifest(spec, target, snapshot)
        opening = _json_dump(head)[:-1]  # sin la llave de cierre: seguimos escribiendo
        yield from emitter.raw(f"{opening}{',' if head else ''}\"tables\":{{")

    emitted = 0
    for table in tables:
        if per_object:
            cursor.name = espec.sanitize_filename(table.table, fallback="tabla")
        prepared = _prepare_table_data(spec, adapter, table, stats)
        if prepared is None:
            if per_object:
                # El archivo existe igual, vacío pero válido: su ausencia se leería como
                # "esa tabla no se pidió", que es otra cosa.
                yield from emitter.raw("[]")
            continue
        columns, select_sql, item = prepared
        start_bytes = stats.bytes_written
        names = [c.name for c in columns]

        if per_object:
            yield from emitter.raw("[")
        else:
            # La coma separa lo YA EMITIDO, no la posición en la lista: una tabla saltada
            # (todas sus columnas generadas) dejaría un ``{,"orders":[…]}`` y el documento
            # entero no parsearía.
            prefix = "," if emitted else ""
            yield from emitter.raw(f'{prefix}{_json_dump(table.table)}:[')
        emitted += 1

        def _row(row: Sequence[Any], position: int, names=names) -> str:
            payload = {
                name: json_value(row[i], binary_encoding=binary)
                for i, name in enumerate(names)
            }
            return ("," if position else "") + _json_dump(payload)

        yield from _emit_rows(
            source=source,
            select_sql=select_sql,
            emitter=emitter,
            stats=stats,
            item=item,
            table=table,
            render_row=_row,
            flush_bytes=_flush_size(spec),
        )
        # El corchete se cierra SIEMPRE, también cuando la tabla se cortó por un valor no
        # representable: un arreglo abierto deja el archivo entero sin parsear.
        yield from emitter.raw("]")
        _finish_data_table(stats, item, start_bytes)

    complete = _finalize(stats)
    if not per_object:
        yield from emitter.raw(f'}},"complete":{_json_dump(complete)}}}')


def iter_ndjson(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    stats: ExportStats,
) -> Iterator[Chunk]:
    """
    Un registro por LÍNEA: la variante que se procesa en flujo y la que usan las
    integraciones de verdad. No es un formato secundario del json, es el otro caso de uso.

    La forma de la línea depende de la organización, y es **uniforme dentro de cada una**
    (que una exportación de una tabla y otra de tres produjeran líneas distintas sería la
    peor trampa posible para un consumidor):

    - ``per_object``: cada línea es el objeto de la fila, PELADO. Es el ndjson que esperan
      ``jq``, ``COPY … FROM``, BigQuery o Spark, y por eso es la forma recomendada.
    - ``single``: como un archivo lleva varias tablas, cada línea va envuelta en
      ``{"table":…,"row":{…}}``. Sin la envoltura no habría forma de saber a qué tabla
      pertenece cada línea.

    Un ndjson SÍ se puede partir por tamaño (cada línea es un documento completo), a
    diferencia del json — de ahí que la matriz prohíba ``split_max_bytes`` allá y acá no.
    """
    per_object = spec.output.organization == espec.Organization.per_object
    binary = str(spec.output.binary_encoding)
    clean = sanitize_snapshot(snapshot, spec)
    tables_by_name = {t.table: t for t in clean.tables}
    cursor = _Cursor()
    emitter = _Emitter(stats, cursor)

    _report_structure_objects(spec, target, stats)

    if spec.output.schema_manifest:
        manifest = build_schema_manifest(spec, target, snapshot)
        if per_object:
            cursor.name = _manifest_entry_name(target)
            yield from emitter.raw(_json_dump(manifest) + "\n")
        else:
            # Primera línea del archivo. Se distingue de una fila porque las filas llevan
            # ``table``/``row`` y esta lleva ``manifest``.
            yield from emitter.raw(_json_dump({"manifest": manifest}) + "\n")

    for table in _data_tables_of(target, tables_by_name):
        if per_object:
            cursor.name = espec.sanitize_filename(table.table, fallback="tabla")
        prepared = _prepare_table_data(spec, adapter, table, stats)
        if prepared is None:
            continue
        columns, select_sql, item = prepared
        start_bytes = stats.bytes_written
        names = [c.name for c in columns]
        table_name = table.table

        def _row(row: Sequence[Any], _position: int, names=names, tname=table_name) -> str:
            payload = {
                name: json_value(row[i], binary_encoding=binary)
                for i, name in enumerate(names)
            }
            if not per_object:
                payload = {"table": tname, "row": payload}
            return _json_dump(payload) + "\n"

        yield from _emit_rows(
            source=source,
            select_sql=select_sql,
            emitter=emitter,
            stats=stats,
            item=item,
            table=table,
            render_row=_row,
            flush_bytes=_flush_size(spec),
        )
        _finish_data_table(stats, item, start_bytes)

    if not _finalize(stats) and not per_object:
        # Marca en banda de artefacto parcial (§14). En ``per_object`` no hace falta: ese
        # camino siempre va en contenedor y el empaquetador agrega la entrada de aviso.
        yield from emitter.raw(_json_dump({"incomplete": True}) + "\n")


# --------------------------------------------------------------------------- #
# Despachador                                                                  #
# --------------------------------------------------------------------------- #


def iter_artifact(
    spec: espec.ExportSpec,
    target: ExportTarget,
    snapshot: SchemaSnapshot,
    adapter: ExportDialect,
    source: RowSource,
    stats: ExportStats,
) -> Iterator[Chunk]:
    """
    El artefacto, en el formato y la organización que pide el spec, como trozos etiquetados.

    Es el ÚNICO punto de entrada del writer para el controller: qué formato se escribe es una
    decisión del spec, no del llamador, y centralizarla acá es lo que evita que la próxima
    fase agregue un ``if`` en el pipeline de ejecución.
    """
    match spec.format:
        case espec.Format.csv:
            yield from iter_csv(spec, target, snapshot, adapter, source, stats)
        case espec.Format.json:
            yield from iter_json(spec, target, snapshot, adapter, source, stats)
        case espec.Format.ndjson:
            yield from iter_ndjson(spec, target, snapshot, adapter, source, stats)
        case _:
            if spec.output.organization == espec.Organization.per_object:
                yield from iter_sql_sections(
                    spec, target, snapshot, adapter, source, stats
                )
            else:
                # Se llama al nombre GLOBAL a propósito: es el punto que los tests de API
                # sustituyen para ejercitar el pipeline sin generar SQL de verdad.
                for text in iter_sql(spec, target, snapshot, adapter, source, stats):
                    yield Chunk(None, text)


__all__ = [
    "GENERATOR_VERSION",
    "Chunk",
    "ExportDialect",
    "ExportItemStat",
    "ExportStats",
    "ExportTarget",
    "RowSource",
    "build_schema_manifest",
    "csv_field",
    "iter_artifact",
    "iter_csv",
    "iter_json",
    "iter_ndjson",
    "iter_sql",
    "iter_sql_sections",
    "json_value",
    "sanitize_snapshot",
    "write_sql",
]
